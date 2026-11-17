"""Human gates: resolve ONE pull request, read verdicts off that pick, approve (merge),
reject (rerun) or hold on a close. Resolve-first — see `_gate_step`."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from enum import StrEnum

from sqlalchemy import BigInteger, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.merges import MergeRefused
from orchestrator.config import GateStep, expand
from orchestrator.db.models import (
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.events.previews import pending_change_request
from orchestrator.github import (
    CHANGE_REQUEST_MARKER,
    LABEL_BLOCKED,
    PullRequestState,
)
from orchestrator.github.state import RepoPermission, ReviewVerdict
from orchestrator.predicates import Unmet
from orchestrator.predicates.words import need
from orchestrator.worker.exits import _exit
from orchestrator.worker.failures import template_summary
from orchestrator.worker.gate_conflicts import (
    forget_mergeability,
    handle_conflict,
    last_mergeability,
    refuse_pending_as_merged,
    refused_merge_note,
)
from orchestrator.worker.reviewers import (
    GateReviewers,
    Ignored,
    PermissionOf,
    Qualification,
    Weighed,
    gate_reviewers,
    qualify,
)
from orchestrator.worker.templates import _pr_for, _pr_values
from orchestrator.worker.tick_context import (
    _advance,
    _apply_outcome,
    _current,
    _insert_run,
    _move_to,
    _next_attempt,
    _observed,
    _runs_for,
    _Tick,
    note_next_step_waiting,
)


async def _gate_step(tick: _Tick, step: GateStep) -> None:
    """Project "waiting for review", then act only on an *observed* decision.

    The worker never approves anything itself. `main` being protected and the App being unable to
    approve its own PR is what actually blocks a bad merge (design.md §7.3); this code only
    notices that a human did approve, and exercises the merge.
    """
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = _current(runs)

    # Both reads go through `_observed`: they ARE the gate's evaluation (the verdict became a
    # direct field read off the one resolved pick), and they run every tick a gate merely waits,
    # so an erroring listing here would starve the ticket's projection.
    #
    # Resolved BEFORE the gate is opened, so `_open_gate` names the pick on its event and renders
    # `{pr_url}` from it rather than resolving a second time under a wider matcher. That also puts
    # the listing ahead of the savepoint that writes the gate row: a raise here now means no row
    # was ever written, where a raise after it erased a fresh row and re-ran the whole sequence.
    pull_request = await _pr_for(tick, step.gate.pr, what=f"gate {step.name}: pull request listing")

    if latest is None or StepRunStatus(latest.status) in {
        StepRunStatus.APPROVED,
        StepRunStatus.REJECTED,
        # An exit cancels a waiting gate row (decision 29); re-entry opens a fresh one against
        # the PR as it is now.
        StepRunStatus.CANCELLED,
    }:
        latest = await _open_gate(
            tick,
            step,
            attempt=await _next_attempt(tick.session, tick.ticket.id, step.name),
            pull_request=pull_request,
        )
        if latest is None:
            return

    reviewers = gate_reviewers(
        step.gate, tick.config.roles, tick.values, bot_login=tick.ctx.bot_login
    )

    async def permission_of(login: str) -> RepoPermission:
        return await _observed(
            tick.source.repo_permission(login),
            what=f"gate {step.name}: repository permission of {login}",
        )

    # The gate's one review round trip: review observation is lazy, so the gate asks the seam for
    # its pick and only its pick. Asked even for a merged/closed pick, because review history
    # survives a merge and the approve arm must still see a frozen approval on one.
    verdict = (
        await _observed(
            tick.source.pr_review_verdict(pull_request.number),
            what=f"gate {step.name}: review verdict for PR #{pull_request.number}",
        )
        if pull_request is not None
        else ReviewVerdict()
    )

    # Before either verdict: the reviewer asked from the dashboard while the gate waited, and an
    # approval landing in the same poll must not advance past a request nobody has answered. It
    # never undoes a merge — `_change_request_refusal` turns a merged pick into a refusal.
    request = await pending_change_request(tick.session, tick.ticket.id, latest.id)
    if request is not None:
        refusal = await _change_request_refusal(tick, step, request, pull_request)
        if refusal is None and pull_request is not None:
            # Every standing GitHub change request is answered by this one rejection too, or it
            # would reject the rework again (`_answered_review_ids`); its text still reaches the
            # rework through `onReject.inputs`.
            standing = await _change_requests(
                verdict,
                reviewers,
                await _answered_review_ids(tick.session, tick.ticket.id, step.name),
                permission_of,
            )
            await _reject(
                tick,
                step,
                latest,
                pull_request,
                reason=f"changes requested in the dashboard by {request.details['requested_by']}",
                review_ids=_review_ids(verdict, standing.counted),
                change_request_id=request.id,
            )
            # The pull request keeps the reviewer's words for its review and its audit: the
            # dashboard is not where anyone reading the pull request later will look.
            tick.plan.pr_comments.append(
                (pull_request.number, _change_request_comment(step, request))
            )
            return
        await tick.emit(
            EventType.STEP_CHANGES_REFUSED,
            run=latest,
            step=step.name,
            attempt=latest.attempt,
            request_id=request.id,
            reason=refusal,
            reviewed_step=request.details.get("reviewed_step"),
        )

    # Resolve-first: every verdict below is read off `_find_pr`'s ONE pick, never existentially
    # over all glob matches. Asking `pr_approved` through `evaluate()` instead disagreed with the
    # pick two ways: a human who approves AND merges between polls left the gate wedged in
    # WAITING_GATE forever (the predicate is open-only, and neither rejection arm accepts a merged
    # PR), and with two open matching PRs the existential read could say "approved" while
    # `_approve` merged the newer, unapproved one. The predicate's head/base-glob arity cannot
    # name one resolved PR, so this is a direct field read.
    approvals = (
        await _approvals(verdict, reviewers, pull_request.head_sha, permission_of)
        if pull_request is not None and (pull_request.state == "open" or pull_request.merged)
        else Weighed()
    )

    # After the dashboard's change request, which is a reviewer's word on the work itself; before
    # the approval, because a head that cannot merge would only have its merge refused every poll.
    # A CLEAN or UNKNOWN pull request falls through to the arms below unchanged.
    if step.gate.on_approve.merge and await handle_conflict(tick, step, latest, pull_request):
        return

    if pull_request is not None and approvals.counted:
        # Approval wins even when the PR also carries an outstanding changes-requested review from
        # a different approver. A login's latest review is one verdict, so `approvals` and
        # `changes_requested` never name the same person; this is "A approved while B's earlier
        # request still stands", which `.factorial/` does not decide. Checking approval first is the
        # ruling: one qualifying approval advances, and neither rejection branch runs.
        #
        # An already-merged pick lands here too — the human approved and merged before this poll,
        # and `_approve`'s `not pull_request.merged` guard skips the re-merge, so the gate records
        # the decision instead of wedging. Only closed-WITHOUT-merge is excluded: closing is the
        # later, overriding act, so the gate holds even with a standing approval.
        login, why = next(iter(approvals.counted.items()))
        await _approve(tick, step, latest, pull_request, login, why)
        return

    if pull_request is not None and pull_request.state == "closed" and not pull_request.merged:
        await _hold_closed(tick, step, latest, pull_request)
        return

    # A PR need not be closed for "Request changes" to count — the same UX an approval gets. It
    # goes through the same rule as `_approvals`, because `pr_changes_requested`'s arity is
    # `(head)` only, with no `by` argument to filter through `evaluate()`: a non-approver's
    # requested changes must no more trigger a rejection than their approval triggers an advance.
    requests = Weighed()
    if pull_request is not None and pull_request.state == "open" and not pull_request.merged:
        requests = await _change_requests(
            verdict,
            reviewers,
            await _answered_review_ids(tick.session, tick.ticket.id, step.name),
            permission_of,
        )
        if requests.counted:
            login, why = next(iter(requests.counted.items()))
            await _reject(
                tick,
                step,
                latest,
                pull_request,
                reason=f"changes requested by {login}",
                review_ids=_review_ids(verdict, requests.counted),
                qualification=why,
            )
            return

    # A merged pick with NO qualifying approval — none, or one on an earlier commit than the head
    # it merged at — falls through every arm. The gate's mandate is a qualifying reviewer's
    # approval of what merged, and a merge is not one: blessing it would turn the App's tracked
    # review bypass on `main` into a silent auto-advance for exactly the merges that skipped
    # review. Nor is it a rejection — that arm means closed-without-merge. The ticket stays
    # visibly WAITING_GATE and recovers on a retroactive approval, which the arm above honors
    # without re-merging.
    if pull_request is not None:
        await _announce_ignored(
            tick, step, latest, pull_request, verdict, approvals.ignored, requests.ignored
        )


async def _open_gate(
    tick: _Tick, step: GateStep, *, attempt: int, pull_request: PullRequestState | None
) -> StepRun | None:
    """Create the gate's `step_run` and announce it — exactly once per attempt.

    `pull_request` is `_gate_step`'s own resolved pick, so the announcement names the PR the gate
    will actually act on. It is the one event a held ticket's reader is sent to, and a gate that
    opened before any PR existed says so with a null rather than an absent key.
    """
    run = StepRun(
        ticket_id=tick.ticket.id,
        step_name=step.name,
        kind=StepKind.HUMAN_GATE,
        attempt=attempt,
        status=StepRunStatus.WAITING,
        started_at=tick.now,
    )
    if not await _insert_run(tick, run):
        return None
    await tick.emit(
        EventType.GATE_WAITING,
        run=run,
        step=step.name,
        attempt=attempt,
        gate_type=step.gate.type,
        pr_number=None if pull_request is None else pull_request.number,
    )
    if pull_request is None:
        head, base = expand(step.gate.pr.head, tick.values), expand(step.gate.pr.base, tick.values)
        await note_next_step_waiting(
            tick,
            step.name,
            Unmet(
                (f"no pull request matches {head} into {base}",),
                (need("pr_open", (head, base)),),
            ),
        )

    _apply_outcome(tick, step.gate.waiting, extra=await _pr_values(tick, None, pull_request))
    tick.ticket.status = TicketStatus.WAITING_GATE
    return run


async def _approve(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState,
    approver: str,
    qualification: Qualification,
) -> None:
    # A null `merged_sha` on GATE_APPROVED covers two cases: `onApprove.merge` false, and a pick
    # already merged externally.
    merged_sha: str | None = None
    if step.gate.on_approve.merge and not pull_request.merged:
        projector = tick.projector()
        try:
            # `head_sha` IS the approved commit (`_approvals` weighed it), so the forge merges
            # what was approved or refuses — never a push that landed after the approval.
            merged_sha = await projector.merge_pr(pull_request.number, sha=pull_request.head_sha)
        except MergeRefused as refusal:
            if await _merge_refused(tick, step, run, pull_request, approver, refusal):
                # Only news: the same refusal every tick (a check still running) would otherwise
                # cost a forge read on each.
                forget_mergeability(tick, run)
            return

    run.status = StepRunStatus.APPROVED
    run.decided_by = approver
    run.ended_at = tick.now
    forget_mergeability(tick, run)
    await tick.emit(
        EventType.GATE_APPROVED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        decided_by=run.decided_by,
        pr_number=pull_request.number,
        merged_sha=merged_sha,
        qualified_as=qualification.basis,
        permission=qualification.permission,
    )
    await refuse_pending_as_merged(tick, step, run)
    extra = await _pr_values(tick, None, pull_request)
    await _advance(tick, step, run, step.gate.on_approve, extra=extra)


async def _merge_refused(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState,
    approver: str,
    refusal: MergeRefused,
) -> bool:
    """The forge declined the merge an approval asked for: keep waiting, and say why once. True
    when the refusal is news: its reason or the head differs from the last one on this run.

    Raising instead would fail the tick every poll and quarantine the ticket (decision 31) for
    what is a state of the pull request: a check still running, a CODEOWNERS review missing, a
    conflict. The merge is tried again every tick, and only a refusal that differs from the last
    one on this run is announced. A head last read as conflicting or behind its base does not
    clear by itself, so its comment names the fix instead of promising the merge.
    """
    last = await tick.session.scalar(
        select(Event.details)
        .where(Event.step_run_id == run.id, Event.type == EventType.GATE_MERGE_REFUSED)
        .order_by(Event.id.desc())
        .limit(1)
    )
    if (
        last is not None
        and last.get("head_sha") == pull_request.head_sha
        and last.get("reason") == refusal.message
    ):
        return False
    await tick.emit(
        EventType.GATE_MERGE_REFUSED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        pr_number=pull_request.number,
        head_sha=pull_request.head_sha,
        approved_by=approver,
        status=refusal.status,
        reason=refusal.message,
    )
    host = tick.host().forge.display.name
    state = await last_mergeability(tick.session, run, pull_request.head_sha)
    after = refused_merge_note(tick, step, pull_request, state) or (
        f"Factorial merges it as soon as {host} allows."
    )
    tick.plan.comments.append(
        f"Approved by `{approver}`, but {host} refused to merge {pull_request.url}: "
        f"{refusal.message}\n\n{after}"
    )
    return True


async def _hold_closed(
    tick: _Tick, step: GateStep, run: StepRun, pull_request: PullRequestState
) -> None:
    """The pick closed without a merge: hold the gate, never reject it (decision 79).

    A close is not a review, so nothing is sent back. The gate waits for the pull request to
    reopen, a Redo or an exit. A closed issue is that exit: decision 29 spares a waiting gate only
    because a merge closes the issue first, and a pick closed without a merge is not one.
    """
    if tick.issue.state == "closed":
        await _exit(tick, via="issue_closed")
        return
    announced = await tick.session.scalar(
        select(Event.id)
        .where(
            Event.step_run_id == run.id,
            Event.type == EventType.GATE_PR_CLOSED,
            Event.details["pr_number"].as_integer() == pull_request.number,
        )
        .limit(1)
    )
    if announced is not None:
        return
    await tick.emit(
        EventType.GATE_PR_CLOSED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        pr_number=pull_request.number,
        head_sha=pull_request.head_sha,
    )
    tick.plan.comments.append(
        f"{pull_request.url} was closed without merging, so `{step.name}` holds the ticket "
        "instead of sending the work back. To carry on, reopen it. To stop, close this issue."
    )


async def _announce_ignored(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState,
    verdict: ReviewVerdict,
    approvals: Mapping[str, Ignored],
    requests: Mapping[str, Ignored],
) -> None:
    """Tell the ticket, once per review, that a review decided nothing and why.

    Once per review id for the ticket's whole life, not per gate run: a CHANGES_REQUESTED review
    stays its author's latest after a rework, and the reopened gate weighs it again. A review
    with no id is never announced, because nothing could tell the next poll it had been."""
    weighed = [
        *((login, "approved", why) for login, why in approvals.items()),
        *((login, "changes_requested", why) for login, why in requests.items()),
    ]
    if not weighed:
        return
    announced = set(
        await tick.session.scalars(
            # Not `.as_integer()`: it casts to a 32-bit integer, and GitHub review ids are past it.
            select(cast(Event.details["review_id"].as_string(), BigInteger)).where(
                Event.ticket_id == tick.ticket.id,
                Event.type == EventType.GATE_REVIEW_IGNORED,
            )
        )
    )
    for login, kind, why in weighed:
        review_id = verdict.review_ids.get(login)
        if not review_id or review_id in announced:
            continue
        await tick.emit(
            EventType.GATE_REVIEW_IGNORED,
            run=run,
            step=step.name,
            attempt=run.attempt,
            pr_number=pull_request.number,
            reviewer=login,
            review_id=review_id,
            verdict=kind,
            reason=why.reason,
            permission=why.permission,
            required=step.gate.approver_permission,
        )
        tick.plan.comments.append(_ignored_comment(step, pull_request, login, kind, why))


def _ignored_comment(
    step: GateStep,
    pull_request: PullRequestState,
    login: str,
    kind: str,
    why: Ignored,
) -> str:
    review = "approval" if kind == "approved" else "request for changes"
    if why.reason == "app":
        because = "it comes from the App itself, and the App's reviews never count"
    elif why.reason == "bot":
        because = "it comes from a bot account that the gate's `approvers` do not name"
    elif why.reason == "not_named":
        because = f"`{login}` is not named in the gate's `approvers`"
    else:
        held = "no" if why.permission in (None, "none") else why.permission
        because = (
            f"`{login}` has {held} access to the repository, and the gate needs "
            f"{step.gate.approver_permission} or higher, or a name in its `approvers`"
        )
    return (
        f"The {review} by `{login}` on {pull_request.url} does not count at `{step.name}`: "
        f"{because}. `{step.name}` keeps waiting for a review that does."
    )


async def _reject(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState,
    *,
    reason: str,
    review_ids: Sequence[int] = (),
    change_request_id: int | None = None,
    qualification: Qualification | None = None,
) -> None:
    run.status = StepRunStatus.REJECTED
    run.ended_at = tick.now
    forget_mergeability(tick, run)
    await tick.emit(
        EventType.GATE_REJECTED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        pr_number=pull_request.number,
        # Read back by `worker.acceptance._unmoved_rejections`.
        head_sha=pull_request.head_sha,
        reason=reason,
        # Read back by `_answered_review_ids`.
        review_ids=list(review_ids),
        # Read back by `worker.inputs._dashboard_feedback`.
        change_request_id=change_request_id,
        qualified_as=None if qualification is None else qualification.basis,
        permission=None if qualification is None else qualification.permission,
    )
    on_reject = step.gate.on_reject
    if on_reject is None:
        # Nothing to loop back to: treat it as a blocked ticket rather than silently stalling.
        # Composed like `_retry_or_escalate`'s escalate application — apply the declared
        # `defaults.onFailure.escalate` first, so its `ticketLabel` wins over BLOCKED's fallback.
        # `GateStep` has no per-step `onFailure`, so a rejection with no rerun edge has only the
        # process-level default to draw on, and absent that nothing is invented.
        failure_reason = "gate rejected and no onReject is configured"
        failure_summary = (
            f"Changes were requested at {step.name}, and this process has no step to send the "
            "work back to (add onReject)."
        )
        on_failure = tick.config.defaults.on_failure
        if on_failure is not None:
            _apply_outcome(
                tick,
                on_failure.escalate,
                extra={
                    "step": step.name,
                    "attempt": str(run.attempt),
                    "failure_reason": failure_reason,
                    "failure_summary": template_summary(failure_summary),
                },
            )
        # NOT `if tick.plan.label is None`, unlike `_retry_or_escalate`: `_reject` can run in the
        # SAME tick as `_open_gate`, which already applied `gate.waiting`'s `ticketLabel`. A
        # None-guard would see that and never correct it to BLOCKED, leaving the ticket BLOCKED
        # internally while GitHub shows the WAITING label — after which `reconcile_ticket`'s
        # `issue.labels & PARKING_LABELS` check reads "already unblocked" every tick, reopens the
        # gate, and re-rejects: an unbounded same-tick livelock. Forcing BLOCKED whenever the
        # escalate declares no label closes it; `_apply_outcome` above already overwrote the label
        # unconditionally when one IS declared, so this only fires when there is none to defer to.
        if on_failure is None or on_failure.escalate.ticket_label is None:
            tick.plan.label = LABEL_BLOCKED
        tick.ticket.status = TicketStatus.BLOCKED
        await tick.emit(
            EventType.ESCALATED,
            run=run,
            step=step.name,
            attempt=run.attempt,
            pr_number=pull_request.number,
            failure_reason=failure_reason,
            failure_summary=failure_summary,
        )
        return

    _apply_outcome(tick, on_reject, extra=await _pr_values(tick, None, pull_request))
    # Rework is a new attempt of the earlier step; `_agent_step` sees a SUCCEEDED latest run for
    # the step it has been sent back to and starts attempt N+1, the number `_enter_step` predicts.
    await _move_to(tick, on_reject.rerun_step, from_step=step.name, reason="reject_rerun")


async def _approvals(
    verdict: ReviewVerdict,
    reviewers: GateReviewers,
    head_sha: str,
    permission_of: PermissionOf,
) -> Weighed:
    """The approvals of the gate's PR at `head_sha`, weighed. The first counted one is both the
    verdict `_gate_step` acts on and the audit trail's `decided_by`.

    Reads the ONE resolved PR, which `pr_approved`'s head/base-glob arity cannot name. `verdict`
    is non-optional: a Protocol implementation missing `pr_review_verdict` should crash loudly,
    whereas an Optional would turn that defect into a gate that silently never approves. Who
    counts is `worker/reviewers.py`'s rule; a review that does not count decides nothing.

    Only an approval of the head itself is weighed (`ReviewVerdict.approvals_at`). Without that,
    an approval given before later commits kept deciding: two gates on one PR passed the second on
    the first one's approval, and a rework merged on the approval of the work it replaced."""
    return await qualify(
        reviewers, verdict.approvals_at(head_sha), bots=verdict.bots, permission_of=permission_of
    )


async def _change_requests(
    verdict: ReviewVerdict,
    reviewers: GateReviewers,
    answered: Collection[int],
    permission_of: PermissionOf,
) -> Weighed:
    """The latest reviews requesting changes that no earlier rejection answered, weighed. A
    direct field check for the same reason as `_approvals`: `pr_changes_requested`'s arity is
    `(head)` only, with no `by` argument to filter on. Only unanswered requests are weighed, so
    an answered one costs no lookup."""
    unanswered = [
        login
        for login in verdict.changes_requested
        if (review_id := verdict.review_ids.get(login)) is None or review_id not in answered
    ]
    return await qualify(reviewers, unanswered, bots=verdict.bots, permission_of=permission_of)


def _review_ids(verdict: ReviewVerdict, logins: Iterable[str]) -> list[int]:
    """The ids of `logins`' latest reviews, for `_answered_review_ids` to read back."""
    return [
        review_id
        for login in logins
        if (review_id := verdict.review_ids.get(login)) is not None
    ]


async def _answered_review_ids(session: AsyncSession, ticket_id: int, gate: str) -> set[int]:
    """The change requests this gate's earlier rejections sent back for rework.

    GitHub keeps a CHANGES_REQUESTED review as the reviewer's latest after new commits
    (dismiss-stale clears approvals only). Read as a fresh verdict, one review rejected every
    rework, with nothing in between and no cap on rework attempts (factorial#132). By review id,
    not by time: a request left while the rework runs is new, and still rejects. Every
    generation's rejections, so a review answered before a restart stays answered after it."""
    rejected = select(StepRun.id).where(
        StepRun.ticket_id == ticket_id,
        StepRun.step_name == gate,
        StepRun.status == StepRunStatus.REJECTED,
    )
    recorded = await session.scalars(
        select(Event.details["review_ids"]).where(
            Event.step_run_id.in_(rejected), Event.type == EventType.GATE_REJECTED
        )
    )
    return {review_id for ids in recorded if ids for review_id in ids}


def _change_request_comment(gate: GateStep, request: Event) -> str:
    """The pull request comment that carries an honoured change request: who sent it and what it
    sends back, then the feedback exactly as the API accepted it."""
    details = request.details
    return (
        f"{CHANGE_REQUEST_MARKER}\n"
        f"**Changes requested from the dashboard** by {details['requested_by']} at "
        f"`{gate.name}`, for attempt {details['reviewed_attempt']} of "
        f"`{details['reviewed_step']}`. The next attempt receives this text as its change "
        f"request.\n\n---\n\n{details['feedback']}"
    )


class ChangesRefusal(StrEnum):
    """`step_changes_refused.reason`, beside the `step` (the gate) and `reviewed_step` it names.
    A redo answers a request with `redone` (`worker.redos`)."""

    NO_PULL_REQUEST = "no_pull_request"
    ALREADY_MERGED = "already_merged"
    #: The gate's `onReject` does not rerun `reviewed_step`.
    NOT_SENT_BACK = "not_sent_back"
    #: A newer attempt of `reviewed_step` than the one reviewed exists.
    NEWER_ATTEMPT = "newer_attempt"


async def _change_request_refusal(
    tick: _Tick, gate: GateStep, request: Event, pull_request: PullRequestState | None
) -> ChangesRefusal | None:
    """Why the loop cannot honour `request` now, or None when it can.

    The API checked all of this when it accepted the request; the loop checks again because the
    ticket may have moved between that check and this lease.
    """
    if pull_request is None:
        return ChangesRefusal.NO_PULL_REQUEST
    if pull_request.merged:
        return ChangesRefusal.ALREADY_MERGED
    reviewed_step = request.details.get("reviewed_step")
    target = tick.config.rerun_agent_step(gate.name)
    if target is None or target.name != reviewed_step:
        return ChangesRefusal.NOT_SENT_BACK
    runs = await _runs_for(tick.session, tick.ticket, str(reviewed_step))
    if not runs or runs[-1].id != request.details.get("reviewed_step_run_id"):
        return ChangesRefusal.NEWER_ATTEMPT
    return None
