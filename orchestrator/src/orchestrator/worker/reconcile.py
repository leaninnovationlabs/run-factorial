"""observe → evaluate → act → project, for one ticket (design.md §3.1).

The rules this module enforces:

* Desired behaviour comes from the pinned snapshot. `process_version.content` is re-parsed every
  tick; the repo's current `process.yaml` is never consulted for an in-flight ticket (§2.8).
* Observed state comes from polling — a `StateSource` snapshot taken at the top of the tick. A
  webhook only decides *when* this runs.
* Acceptance is the postconditions, never the agent's report (§2.4). A run that reports
  `succeeded` and whose postconditions do not hold is recorded as failed and retried.
* A tick that changes nothing writes nothing — no events, no GitHub calls. Projection is computed
  as a whole desired state and compared against what was observed, so the loop is safe to run
  every 30 seconds.
* A failed evaluation must not silence projection. A predicate, or the gate's own verdict read,
  that raises has answered nothing: the act phase's SAVEPOINT takes back the half-made transition,
  the error is logged (not evented — an infrastructure failure is not a state transition), and
  `_project` still runs on the last persisted step/status. See `_EvaluationUnavailable`/`_observed`.

Event conventions, where design.md §3.1 leaves room:

* `step_started` is emitted once per (step, attempt), when the `step_run` row is created — not
  when `current_step` changes. Emitting on both entry and spawn would double-count in every read
  model derived from the log.
* `step_entered` is the other half of that split, appended by `tick_context._enter_step` wherever
  `current_step` IS assigned, in the same unit of work. It exists because the ticket page reads
  `current_step` for its graph and this log for its timeline, so without it the graph names a step
  the log has not mentioned until that step's first run row exists. Its `attempt` is a prediction
  and may never be redeemed; see `EventType.STEP_ENTERED`.
* a run that exceeds its timeout is cancelled, recorded as `timeout`, and reported with
  `agent_failed` (`reason="timeout"`): §5 has no distinct timeout event, and failure is what the
  retry/escalate path consumes.
"""


from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.bindings import Bindings
from orchestrator.config import (
    ActionStep,
    AgentStep,
    CheckStep,
    GateStep,
    ProcessConfig,
)
from orchestrator.config.conditions import awaits_review
from orchestrator.db.models import (
    Event,
    Project,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.events.approvals import latest_approval
from orchestrator.events.types import requested_via
from orchestrator.events.workspaces import WHY_NOT_SERVABLE
from orchestrator.factorial import AgentPins
from orchestrator.github import (
    EXIT_LABELS,
    LABEL_DONE,
    LABEL_EXIT,
    PARKING_LABELS,
)
from orchestrator.github.state import IssueState, SeededIssue, StateSource
from orchestrator.logs import note, revert_on_error
from orchestrator.predicates import EvalContext, evaluate, needs
from orchestrator.worker.action_steps import _action_step
from orchestrator.worker.agent_steps import (
    _agent_step,
    parked_recheck_due,
    recheck_parked,
    spawn_cleared,
    spawn_recheck_due,
)
from orchestrator.worker.check_steps import _check_step
from orchestrator.worker.completions import (
    COMPLETION_EVENTS,
    CompletionRefusal,
    answer_checked,
    answer_completion,
    refuse_completion,
)
from orchestrator.worker.context import ReconcileDeps
from orchestrator.worker.exits import (
    RESETS,
    _asked_since,
    _exit,
    _exit_wanted,
    _latest,
    _request_event,
    _request_via,
    _unanswered,
    pending_request,
)
from orchestrator.worker.gates import _gate_step
from orchestrator.worker.interrupts import (
    RESUME_EVENTS,
    _lock_latest_run,
    _resume_parked,
    _resume_requested,
)
from orchestrator.worker.outputs import observe_pending_outputs
from orchestrator.worker.parked import PARKING_LABELS_BY_STATUS, ParkedListings
from orchestrator.worker.previews import reconcile_previews, stop_awake_previews
from orchestrator.worker.projection import _observe_title, _project
from orchestrator.worker.redos import REDO_EVENTS, RedoRefusal, redo, refuse_redo
from orchestrator.worker.restarts import (
    RESTART_EVENTS,
    RestartRefusal,
    refuse_restart,
    restart,
)
from orchestrator.worker.retries import STEP_RETRY_EVENTS, answer_step_retry
from orchestrator.worker.templates import _resolve_slug
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    Launches,
    _apply_outcome,
    _EvaluationUnavailable,
    _load_pinned_config,
    _load_project,
    _next_attempt,
    _Plan,
    _retry_policy,
    _runs_for,
    _Tick,
)

__all__ = ["escalated_since", "pending_unblock_request", "reconcile_ticket"]

logger = logging.getLogger(__name__)

#: A re-entry request, then what answers it.
_REENTRY_EVENTS: Final = (
    EventType.REENTRY_REQUESTED,
    EventType.TICKET_REENTERED,
    EventType.REENTRY_REFUSED,
    EventType.TICKET_RESTARTED,
)


@dataclass(frozen=True, slots=True)
class _Requests:
    """The tick's pending requests that a park must still see, from one read."""

    restart: Event | None
    completion: Event | None
    retry: Event | None
    redo: Event | None

    @property
    def any(self) -> bool:
        return any(
            request is not None
            for request in (self.restart, self.completion, self.retry, self.redo)
        )


async def reconcile_ticket(
    session: AsyncSession,
    ticket: Ticket,
    deps: ReconcileDeps,
    *,
    lease_owner: str,
    lease_ttl: timedelta,
    listings: ParkedListings | None = None,
    launches: Launches | None = None,
) -> None:
    """Converge one ticket by one state transition (design.md §3.1).

    At most one transition per tick: the projection is then a single write, and the next tick
    re-observes before deciding anything else. Un-blocking counts as one — it flips `status` back
    to RUNNING and stops there, and the step does its work on the following tick.

    `lease_owner`/`lease_ttl` are the caller's own acquisition values, required rather than
    re-read from `get_settings()`, so an agent spawn's mid-flight renewal (`_start_attempt`,
    decision 41) renews the same lease this tick was handed.

    `listings` is the tick's parked cache (decision 30); None reads every issue per ticket,
    which is what every test that does not opt in exercises.
    """
    note(
        project_id=ticket.project_id,
        process_version_id=ticket.process_version_id,
        step=ticket.current_step,
    )
    if ticket.status == TicketStatus.DONE:
        return

    # Beside the transition and ahead of the parks: a preview is no state the loop owns, and an
    # interrupted ticket can still be previewed (decision 54).
    await reconcile_previews(session, ticket, deps, now=deps.now())

    project = await _load_project(session, ticket.project_id)
    note(org_id=project.organization_id)

    bindings = await deps.bindings_or_hold(session, project)
    if bindings is None:
        return
    if (until := bindings.forge.hold_until(project.github_repo)) is not None:
        logger.debug("project %s waits for its forge until %s", project.name, until)
        return
    source: StateSource = bindings.snapshot(project.github_repo, ticket.external_key)
    bot_login = bindings.bot_login
    labels = PARKING_LABELS_BY_STATUS.get(ticket.status)
    if labels is not None:
        seeded = await bindings.forge.listed_issue(
            project.github_repo, ticket.external_key, labels, listings
        )
        if seeded is not None:
            # Observed this tick by the repo's parked listing, so seeding keeps the source's
            # one-snapshot-per-tick promise; the parked path reads nothing else, except a due
            # re-check (decision 30).
            source = SeededIssue(inner=source, seed=seeded)
    issue = await source.issue()

    # One read per tick for every request a parked ticket must still see.
    latest = await _latest(
        session,
        ticket.id,
        *RESTART_EVENTS,
        *COMPLETION_EVENTS,
        *STEP_RETRY_EVENTS,
        *REDO_EVENTS,
        *_REENTRY_EVENTS,
    )
    requests = _Requests(
        restart=await _request_event(session, latest, *RESTART_EVENTS),
        completion=await _request_event(session, latest, *COMPLETION_EVENTS),
        retry=await _request_event(session, latest, *STEP_RETRY_EVENTS),
        redo=await _request_event(session, latest, *REDO_EVENTS),
    )
    exit_via: str | None = None
    exit_parked = (
        ticket.status == TicketStatus.EXITED
        and bool(issue.labels & EXIT_LABELS)
        and requests.restart is None
        and _asked_since(latest, *_REENTRY_EVENTS) is None
    )
    if exit_parked and not requests.any:
        # Off the floor and nobody has asked for it back: the label only a human removes is
        # still there, so this tick observes the title and nothing else (decision 29).
        _observe_title(ticket, issue)
        return

    if isinstance(source, SeededIssue) and issue.state == "closed":
        # A seeded "closed" is a listing's word from earlier in the tick, and the two decisions
        # that read it — exiting a blocked ticket, refusing a re-entry — are not worth taking on
        # a stale one. The ticket's own read confirms it: one call per transition, none per
        # steady tick, because the steady exited park above never reads state (decision 30).
        source = source.inner
        issue = await source.issue()

    # Loaded ahead of the exit check, which needs the terminal step's name to decide whether a
    # closed issue is a departure or a completion.
    config, agent_pins, config_sha, config_repo = await _load_pinned_config(
        session, ticket.process_version_id
    )

    tick_for = partial(
        _tick_for,
        session,
        ticket,
        deps,
        project=project,
        config=config,
        agent_pins=agent_pins,
        config_sha=config_sha,
        config_repo=config_repo,
        lease_owner=lease_owner,
        lease_ttl=lease_ttl,
        bot_login=bot_login,
        bindings=bindings,
        launches=launches,
    )

    if ticket.status != TicketStatus.EXITED:
        exit_via = await _exit_wanted(session, ticket, issue, config.terminal.step)

    resume_request = unblock_request = locked = None
    if exit_via is None and ticket.status == TicketStatus.BLOCKED:
        locked = await _lock_latest_run(session, ticket)
        resume_request, unblock_request = await _release_requests(session, ticket)
    if (
        exit_via is None
        and ticket.status == TicketStatus.BLOCKED
        and issue.labels & PARKING_LABELS
        and resume_request is None
        and unblock_request is None
        and not requests.any
    ):
        # Parked, the label only a human can remove is still there, and nobody has asked for a
        # release: keep waiting. Any parking
        # label counts — a ticket held by `factorial:interrupted` is as parked as one held by
        # `factorial:blocked`, and testing for only the latter would make the loop read an
        # interrupted
        # ticket as already released and act on it on the very next tick.
        # `leases.acquire_leases`'s `status <> 'done'` filter is what still hands this ticket to
        # the loop every tick, so that this check can run.
        logger.debug("ticket %s is blocked; awaiting a human", ticket.external_key)
        if await _recheck_if_due(
            session, ticket, deps, config, partial(tick_for, source=source, issue=issue), locked
        ):
            return
        _observe_title(ticket, issue)
        return

    if isinstance(source, SeededIssue):
        # Past both parks, every remaining path is a transition that writes, and `_project`
        # replaces the issue's whole label set from `tick.issue.labels`. A listing taken earlier
        # in the tick must not decide which of a human's labels survive: the write reads fresh.
        # Same memoised read as the closed-confirm above, so still one call per transition and
        # none per steady tick (decision 30).
        source = source.inner
        issue = await source.issue()

    tick = await tick_for(source=source, issue=issue)
    # Beside the transition, never instead of it: an output record changes no state the loop owns
    # (decision 71).
    await observe_pending_outputs(tick)

    if exit_via is not None:
        # Ahead of the release and act phases: an exit is the tick's one transition whatever
        # else the ticket was about to do (decision 29).
        await _exit(tick, via=exit_via)
        if requests.restart is not None:
            # Stopping beats starting (decision 67): the restart is answered, not carried over.
            await refuse_restart(
                tick,
                requests.restart,
                RestartRefusal.EXITED,
                "the ticket exited before the restart was answered. Restart it again to bring "
                "it back on the new process.",
            )
        if requests.completion is not None:
            await _refuse_completion_on_exit(tick, requests.completion)
        if requests.redo is not None:
            await refuse_redo(
                tick,
                requests.redo,
                RedoRefusal.EXITED,
                "the ticket exited before the redo was answered.",
            )
        _observe_title(ticket, issue)
        await _project(tick)
        return

    if requests.restart is not None:
        await restart(tick, requests.restart)
        _observe_title(ticket, issue)
        await _project(tick)
        return

    if requests.redo is not None:
        await redo(tick, requests.redo)
        if ticket.status != TicketStatus.EXITED or exit_parked:
            _observe_title(ticket, issue)
            await _project(tick)
            return
        # Refused, and someone asked the exited ticket back: the re-entry below is this tick's
        # transition, and projecting first would put back the `factorial:exit` they removed.

    if ticket.status == TicketStatus.EXITED:
        if requests.completion is not None:
            # A Force complete that raced the exit: answered here, or the park never reads it.
            await _refuse_completion_on_exit(tick, requests.completion)
        if requests.retry is not None:
            # Pressed as the ticket exited: refused `off_the_floor`, or the park never reads it.
            await answer_step_retry(tick, requests.retry)
        if exit_parked:
            _observe_title(ticket, issue)
            return
        await _reenter(tick)
        _observe_title(ticket, issue)
        await _project(tick)
        return

    if requests.retry is not None:
        # A refusal on a BLOCKED ticket is not projected, as an un-block's is not below.
        if await answer_step_retry(tick, requests.retry) or ticket.status != TicketStatus.BLOCKED:
            await _project(tick)
        _observe_title(ticket, issue)
        return

    # A refused or still-pending request falls through: a refusal is no transition.
    if requests.completion is not None and await _completion_requested(tick, requests.completion):
        _observe_title(ticket, issue)
        await _project(tick)
        return

    if ticket.status == TicketStatus.BLOCKED:
        # The label is gone: a human removed it (design.md §2.2 — human edits are observed input,
        # never commands). Which release this is depends on WHY the ticket was parked:
        #
        # * interrupted or waiting — a run a human stopped, or an idle session the loop parked, so
        #   the session resumes in the workspace the park kept, on the SAME attempt (decision 26).
        # * blocked — an escalation, so `_grant_attempt` grants one more attempt.
        #
        # Both release a parked ticket and they must not be conflated: a resume that started a new
        # attempt would throw away the agent's context and charge for a restart, and an unblock
        # that tried to resume would have no session to continue.
        # A dashboard Resume is a release too, and the label need not be gone for it: the
        # request is the gesture, observed here exactly as a removed label is. So is an un-block
        # request, which takes the label's own grant path.
        if resume_request is None or not await _resume_requested(tick, resume_request):
            if unblock_request is not None:
                outcome = await _unblock_requested(tick, unblock_request)
                if outcome in (UnblockOutcome.REFUSED_PARKED, UnblockOutcome.REFUSED_STALE):
                    # Not projected: nothing transitioned, and for a parked run `_steady_label`
                    # would put `factorial:blocked` over the park's own label.
                    _observe_title(ticket, issue)
                    return
            elif issue.labels & PARKING_LABELS:
                # Only a refused request for a run no longer parked gets here: the park holds.
                _observe_title(ticket, issue)
                return
            else:
                await _release(tick, _UNBLOCK, via="label")
        _observe_title(ticket, issue)
        await _project(tick)
        return

    # The act phase runs inside its own SAVEPOINT so a failed evaluation read
    # (`_EvaluationUnavailable`) erases exactly the half-made transition and nothing else — e.g. a
    # `_poll_run` that already marked its run SUCCEEDED before `_accept`'s postconditions raised.
    # Committing that without the acceptance verdict would fabricate state: the next tick would
    # read the SUCCEEDED row as a completed step and start a rework attempt.
    #
    # Every other exception passes through untouched: `LeaseLost` and an infrastructure
    # `SpawnError` still unwind the whole tick (the rollback-erases-the-row semantics
    # `_start_attempt` relies on), and `UnknownActionError` and the dispatch `ValueError` below
    # stay uncaught and loud.
    try:
        # A rollback erases the runs and the step this phase noted, so the log fields go with it.
        with revert_on_error():
            async with session.begin_nested():
                if ticket.current_step == config.terminal.step:
                    await _terminal(tick)
                else:
                    # A KeyError here means the pinned snapshot and `current_step` disagree — a bug,
                    # not an input error. Let it surface: the loop logs it and the ticket is retried
                    # next tick.
                    step = config.step(ticket.current_step)
                    if isinstance(step, AgentStep):
                        await _agent_step(tick, step)
                    elif isinstance(step, GateStep):
                        await _gate_step(tick, step)
                    elif isinstance(step, ActionStep):
                        await _action_step(tick, step)
                    elif isinstance(step, CheckStep):
                        await _check_step(tick, step)
                    else:
                        # `Step` (config/models.py) is a closed discriminated union; reaching
                        # here means it grew a member this dispatch was never updated for. Same
                        # closed-set discipline as `_resolve_input_refs`' `BareInputSource` guard.
                        raise ValueError(
                            f"step {step.name!r} has kind {type(step).__name__}, which "
                            "reconcile_ticket's dispatch does not handle it"
                        )
    except _EvaluationUnavailable as exc:
        # Refresh first: the SAVEPOINT rollback expires any attribute the act phase touched, and
        # an expired attribute read on an AsyncSession raises instead of lazy-loading. This
        # restores the ticket's step/status as already persisted, which is what gets projected.
        await session.refresh(ticket)
        # A log, not an event: an infrastructure failure is not a state transition, and no
        # existing event type fits without lying (POSTCONDITION_FAILED asserts the predicates
        # evaluated false; here they evaluated nothing). A new type would also write one permanent
        # row per tick for as long as the error persists. `exc` goes in the message body, not just
        # the traceback, so a repeating incident is greppable from the one-line record.
        logger.exception(
            "ticket %s step %s: evaluation failed (%s); the tick's transition is rolled back "
            "and the last persisted state is projected instead",
            ticket.external_key,
            ticket.current_step,
            exc,
        )
        # Nothing derived from the failed evaluation: intent queued by the aborted transition
        # (an `_apply_outcome` label, a comment) must not outlive the rollback that erased the
        # transition it announced.
        tick.plan = _Plan()

    _observe_title(ticket, issue)
    await _project(tick)


async def _tick_for(
    session: AsyncSession,
    ticket: Ticket,
    deps: ReconcileDeps,
    *,
    project: Project,
    config: ProcessConfig,
    agent_pins: AgentPins | None,
    config_sha: str,
    config_repo: str,
    source: StateSource,
    issue: IssueState,
    lease_owner: str,
    lease_ttl: timedelta,
    bot_login: str,
    bindings: Bindings,
    launches: Launches | None,
) -> _Tick:
    values = {
        "ticket": ticket.external_key,
        "ticket_owner": ticket.owner_at_admission,
        "repo": project.github_repo,
    }
    # `{slug}` carry-forward, read back per tick from durable `Artifact` rows: see
    # `templates._resolve_slug` for when it resolves and `templates._glob` for why matchers never
    # take it.
    slug = await _resolve_slug(session, ticket, config, values)
    if slug is not None:
        values["slug"] = slug
    return _Tick(
        session=session,
        deps=deps,
        ticket=ticket,
        project=project,
        config=config,
        agent_pins=agent_pins,
        config_sha=config_sha,
        config_repo=config_repo,
        source=source,
        issue=issue,
        now=deps.now(),
        values=values,
        ctx=EvalContext(source=source, values=values, bot_login=bot_login),
        plan=_Plan(),
        lease_owner=lease_owner,
        lease_ttl=lease_ttl,
        bindings=bindings,
        launches=launches,
    )


async def _recheck_if_due(
    session: AsyncSession,
    ticket: Ticket,
    deps: ReconcileDeps,
    config: ProcessConfig,
    tick_for: Callable[[], Awaitable[_Tick]],
    locked: StepRun | None,
) -> bool:
    """A parked session's re-check (`agent_steps.recheck_parked`), or a refused spawn's
    (`agent_steps.spawn_cleared`), when due. True when it accepted the run or released the
    ticket, and the tick projected; the ticket's other parks read nothing here, but a parked
    run of a step that awaits review reads its latest approval every tick. `locked` is the
    step's latest run as `_lock_latest_run` read it."""
    step = config.find_step(ticket.current_step)
    if not isinstance(step, AgentStep):
        return False
    now = deps.now()
    approval = None
    if (
        locked is not None
        and locked.status == StepRunStatus.INTERRUPTED
        and awaits_review(step.postconditions)
    ):
        approval = await latest_approval(session, locked.ticket_id, locked.id)
    approval_id = None if approval is None else approval.id
    parked = locked if locked and parked_recheck_due(deps, locked, now, approval_id) else None
    if parked is None and (
        # A parked session is never released by a spawn's re-check: `_release` would resume it.
        (locked is not None and locked.status == StepRunStatus.INTERRUPTED)
        or not await spawn_recheck_due(session, deps, ticket, now)
    ):
        return False
    tick = await tick_for()
    try:
        with revert_on_error():
            async with session.begin_nested():
                if parked is not None:
                    accepted = await recheck_parked(tick, ticket, parked, step, approval)
                elif accepted := await spawn_cleared(tick):
                    await _release(tick, _RECHECK, via="recheck")
    except _EvaluationUnavailable as exc:
        await session.refresh(ticket)
        logger.exception(
            "ticket %s step %s: parked re-check failed (%s); the park holds",
            ticket.external_key,
            ticket.current_step,
            exc,
        )
        return False
    if not accepted:
        return False
    if isinstance(tick.source, SeededIssue):
        # The listing's labels are older than the write `_project` makes from them.
        tick.issue = await tick.source.inner.issue()
    _observe_title(ticket, tick.issue)
    await _project(tick)
    return True


async def _completion_requested(tick: _Tick, request: Event) -> bool:
    """Answer a Force complete (`completions.answer_completion`). True when it accepted the run.
    An unreadable verdict rolls back and leaves the request pending, until
    `COMPLETION_UNREADABLE_FOR` after the first such tick, when it is refused."""
    return await answer_checked(
        tick,
        request,
        partial(answer_completion, tick, request),
        partial(
            refuse_completion,
            tick,
            request,
            CompletionRefusal.UNAVAILABLE,
            "GitHub could not be read to check the postconditions",
        ),
        what="completion request",
    )


async def _refuse_completion_on_exit(tick: _Tick, request: Event) -> None:
    await refuse_completion(
        tick, request, CompletionRefusal.EXITED, "the ticket exited before the check was answered"
    )


@dataclass(frozen=True, slots=True)
class _Gesture:
    """The words a release is announced with — the only thing un-block, re-entry and the
    worker's own release of a refused spawn differ in."""

    verb: str
    noun: str
    event: EventType
    #: Leads a granted retry's comment in place of the backoff sentence: nobody released it.
    starting: str | None = None


_UNBLOCK: Final = _Gesture("Unblocked", "Unblocking", EventType.UNBLOCKED)
_REENTRY: Final = _Gesture("Re-entered", "Re-entering", EventType.TICKET_REENTERED)
_RECHECK: Final = _Gesture(
    "Unblocked", "Unblocking", EventType.UNBLOCKED, starting="The runtime can run a session now"
)


async def _release(tick: _Tick, gesture: _Gesture, **details: object) -> bool:
    """A human released a parked ticket: resume the interrupted run if that is what is parked,
    otherwise grant one attempt. One function for un-block and re-entry so there is
    exactly one grant path (decision 29).

    Returns whether an attempt was granted — the one outcome a caller cannot recompute.
    Resumed and refused are both readable from `ticket.status` at the call site.
    """
    granted = (
        False if await _resume_parked(tick) else await _grant_attempt(tick, gesture, **details)
    )
    if tick.ticket.status != TicketStatus.EXITED:
        # Back on the floor, so it has not left: `closed_at` is the fact the read models filter
        # on, and it stays set only while the ticket is still exited.
        tick.ticket.closed_at = None
    return granted


#: An un-block request, then what answers it.
_UNBLOCK_EVENTS: Final = (
    EventType.UNBLOCK_REQUESTED,
    EventType.UNBLOCKED,
    EventType.UNBLOCK_REFUSED,
    *RESETS,
)


async def pending_unblock_request(session: AsyncSession, ticket_id: int) -> Event | None:
    """The newest un-block request with no answer since it. An `unblocked` answers it, a label
    removal's included, and so does an `unblock_refused`. The newest one, so the answer to it
    answers any older one too."""
    return await pending_request(session, ticket_id, *_UNBLOCK_EVENTS)


async def _release_requests(
    session: AsyncSession, ticket: Ticket
) -> tuple[Event | None, Event | None]:
    """`pending_resume_request` and `pending_unblock_request` in one read of the log, since a
    parked ticket asks both on every tick. Read after the caller's `_lock_latest_run`."""
    latest = await _latest(session, ticket.id, *RESUME_EVENTS, *_UNBLOCK_EVENTS)
    return (
        await _request_event(session, latest, *RESUME_EVENTS),
        await _request_event(session, latest, *_UNBLOCK_EVENTS),
    )


async def escalated_since(session: AsyncSession, ticket_id: int, event_id: int) -> bool:
    """Whether the ticket escalated after `event_id`. If it did, an older request was about an
    escalation that has since been released, and nobody has read this one."""
    later = await session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == ticket_id,
            Event.type == EventType.ESCALATED,
            Event.id > event_id,
        )
        .limit(1)
    )
    return later is not None


class UnblockOutcome(StrEnum):
    """What became of an un-block request. A refusal's value is its `UNBLOCK_REFUSED.reason`."""

    GRANTED = "granted"
    #: The latest run is a parked session: a Resume continues it in its kept workspace.
    REFUSED_PARKED = "parked"
    #: The ticket escalated again after the request.
    REFUSED_STALE = "stale"
    #: `_grant_attempt` refused: the pinned process has no retry policy for the step.
    REFUSED_NO_POLICY = "no_retry_policy"


async def _unblock_requested(tick: _Tick, request: Event) -> UnblockOutcome:
    """Honour an un-block request through the label's own grant path, or answer it with
    `unblock_refused`. Every outcome answers the request, so it is read once."""
    runs = await _runs_for(tick.session, tick.ticket, tick.ticket.current_step)
    if runs and StepRunStatus(runs[-1].status) == StepRunStatus.INTERRUPTED:
        outcome = UnblockOutcome.REFUSED_PARKED
    elif await escalated_since(tick.session, tick.ticket.id, request.id):
        outcome = UnblockOutcome.REFUSED_STALE
    elif await _release(
        tick, _UNBLOCK, via=requested_via(request.details).value, request_id=request.id
    ):
        return UnblockOutcome.GRANTED
    else:
        outcome = UnblockOutcome.REFUSED_NO_POLICY
    await tick.emit(
        EventType.UNBLOCK_REFUSED,
        step=request.details.get("step"),
        attempt=request.details.get("attempt"),
        request_id=request.id,
        reason=outcome.value,
    )
    return outcome


async def _reenter(tick: _Tick) -> None:
    """An exited ticket's label is gone, or someone asked for it back: bring it back, or say
    why not.

    Refused while the issue is closed — reopening is a precondition the loop observes, not a
    gesture it acts on — and every path answers the gesture exactly once: `reentry_refused` or
    `ticket_reentered` for a request, and for a label removal the steady label re-asserts
    `factorial:exit` on a refusal, so neither gesture is seen again next tick.
    """
    request = await _unanswered(tick.session, tick.ticket.id, *_REENTRY_EVENTS)
    via = "label" if request is None else await _request_via(tick.session, request)
    step_name = tick.ticket.current_step
    # Read before the refusal, not after: every emit below carries the pair the ticket page keys
    # its timeline block on, and a refusal that omitted them would file under an invented
    # "unknown" block.
    runs = await _runs_for(tick.session, tick.ticket, step_name)
    attempt = runs[-1].attempt if runs else None
    if tick.issue.state == "closed":
        await tick.emit(
            EventType.REENTRY_REFUSED,
            step=step_name,
            attempt=attempt,
            via=via,
            reason="issue_closed",
        )
        tick.plan.comments.append(
            f"Re-entry refused: the issue is closed. Reopen it, then remove the `{LABEL_EXIT}` "
            "label or press Re-enter on the dashboard."
        )
        return

    if step_name == tick.config.terminal.step:
        # Exited in the one-tick window between entering the terminal step and `_terminal`
        # running. Nothing to grant; the next tick finishes the ticket.
        tick.ticket.status = TicketStatus.RUNNING
        tick.ticket.closed_at = None
        await tick.emit(
            EventType.TICKET_REENTERED,
            step=step_name,
            attempt=None,
            next_attempt=None,
            due_at=None,
            via=via,
            resumed=False,
        )
        tick.plan.comments.append(f"Re-entered at `{step_name}`; finishing.")
        return

    if await _release(tick, _REENTRY, via=via, resumed=False):
        return  # `_grant_attempt` emitted `ticket_reentered` with the attempt it grants
    if tick.ticket.status == TicketStatus.EXITED:
        # `_grant_attempt` refused (no retry policy pinned); its comment says so and the steady
        # label re-asserts `factorial:exit`. The request still needs an answer or it pends for ever.
        await tick.emit(
            EventType.REENTRY_REFUSED,
            step=step_name,
            attempt=attempt,
            via=via,
            reason="no_retry_policy",
        )
        return
    # Resumed, or re-parked as interrupted because the resume failed: either way the ticket is
    # back on the floor, on the same attempt.
    await tick.emit(
        EventType.TICKET_REENTERED,
        step=step_name,
        attempt=attempt,
        next_attempt=attempt,
        due_at=None,
        via=via,
        resumed=tick.ticket.status == TicketStatus.RUNNING,
    )


async def _grant_attempt(tick: _Tick, gesture: _Gesture, **details: object) -> bool:
    """Grant one more attempt for a human's release gesture.

    The invariant: un-blocking must never resolve to silence. Every call produces work, a
    re-block, or visible truth on the ticket — never a ticket left RUNNING forever with no event,
    no spawn, and nothing for a human to read.

    `current_step` is never touched, so the ticket resumes exactly where it was blocked, and no
    `step_run` row is created, deleted or mutated here — every branch only reads the latest one.
    The one attempt falls out of dispatch logic that already exists, and its failure escalates
    again because `_ordinal` already counts past the budget, in three cases:

    * An agent, action or check step's FAILED branch starts a new attempt only when a retry
      policy exists AND the backoff has elapsed. With NO retry policy configured at all (both
      `step.retry` and `defaults.retry` unset — this parses fine) that branch declines silently
      forever, leaving the ticket RUNNING with the label gone. So this function refuses outright
      in that case: the parked status stays as it was, one comment explains why, and `_project`'s
      `_steady_label` re-asserts its label (`factorial:blocked` or `factorial:exit`) on this
      same tick.
    * With a retry policy but unmet preconditions, `_start_attempt` declines silently every tick.
      Un-blocking still proceeds (an attempt will spawn once they clear), but the acknowledgment
      names the unmet preconditions, evaluated through the same `evaluate()`/`tick.ctx` pair
      `_start_attempt` uses. Naming the predicate is the fix; this does not try to predict whether
      it will ever hold.
    * A gate-blocked ticket always proceeds: `GateStep` has no retry-policy or precondition
      concept to wedge against, and `_gate_step` already treats a REJECTED latest run as "open a
      new gate attempt" against currently observed PR state.

    Every branch that proceeds queues exactly one acknowledgment comment and emits `gesture.event`
    — once per human gesture, not once per tick, because a re-blocked ticket stops reaching this
    function via `reconcile_ticket`'s stays-blocked check. That is the tick's one transition; the
    spawn or gate reopen happens on the following tick.

    The event names BOTH attempts, as `retry_scheduled` does: `attempt` is the blocked run's and
    `next_attempt` the one this gesture grants, which is what the ticket page groups the row under.
    `due_at` is set only where the wait is genuinely a clock — neither the gate branch nor an unmet
    precondition.
    """
    step = tick.config.step(tick.ticket.current_step)
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = runs[-1] if runs else None
    # The attempt this un-block GRANTS — the acknowledgment comment's number and the event's
    # `next_attempt` are the same one. Through `_next_attempt` rather than a local sum, so it
    # cannot drift from what the starters and `_open_gate` will actually number the row.
    next_attempt = await _next_attempt(tick.session, tick.ticket.id, step.name)
    # When the granted attempt's wait is a clock, the moment it runs out. Null on every other
    # path — see the emit at the end.
    due_at: datetime | None = None

    if isinstance(step, GateStep):
        tick.plan.comments.append(f"{gesture.verb}: reopening `{step.name}` for a fresh review.")
    else:
        retry = _retry_policy(tick.config, step)
        if (
            latest is not None
            and StepRunStatus(latest.status) in _FAILED_STATUSES
            and retry is None
        ):
            # Refuse outright — see this function's docstring. The remedy the comment names must
            # respect config pinning: this ticket keeps the process version stamped at admission,
            # so an edit to its process file reaches it only through a restart.
            tick.plan.comments.append(
                f"{gesture.noun} `{step.name}` refused: this ticket's process has no retry rule "
                f"(neither `{step.name}.retry` nor `defaults.retry`), so there is no new attempt "
                "to give it. Add a `retry:` block to the process file in `.factorial/processes/`, "
                "sync it, then restart the ticket on the latest version: it starts again from "
                "its first step."
            )
            return False

        # Lead with the true cause: a skimming operator stops at the first clause, so the
        # unmet-preconditions case must not open with "retrying … shortly" (the wait there is
        # precondition-bound, not backoff-bound).
        comment = f"{gesture.verb}: retrying `{step.name}` (attempt {next_attempt}) shortly."
        if gesture.starting is not None:
            comment = f"{gesture.starting}: starting `{step.name}` (attempt {next_attempt})."
        if retry is not None and latest is not None and latest.ended_at is not None:
            # Measured from the FAILED RUN'S END, never from this gesture: every non-gate kind's
            # FAILED branch gates its retry on `_retry_ready`, which reads exactly this sum
            # through `_retry_due`. A human who un-blocks two minutes after the escalation still
            # waits out the remaining eight.
            due_at = latest.ended_at + retry.backoff
        if isinstance(step, AgentStep) and step.preconditions is not None:
            # Caught locally rather than routed through `_observed`: falling through would
            # re-assert the BLOCKED label over the human's own un-block gesture, after which
            # `reconcile_ticket`'s stays-blocked check waits forever. This evaluate only decorates
            # the acknowledgment, so when it cannot answer the un-block still proceeds on the
            # observed gesture and `_start_attempt` re-evaluates for real before anything spawns.
            try:
                result = await evaluate(step.preconditions, tick.ctx)
            except Exception:
                logger.exception(
                    "ticket %s step %s: precondition evaluation failed during un-block; "
                    "proceeding without naming preconditions",
                    tick.ticket.external_key,
                    step.name,
                )
                result = None
            if result is not None and not result.value:
                # Name it, don't predict it — see this function's docstring.
                waiting_for = "; ".join(needs(result))
                comment = (
                    f"{gesture.verb}, but `{step.name}` can't start yet: it is still waiting for "
                    f"{waiting_for}. Attempt {next_attempt} starts when it can."
                )
                # The wait stopped being the clock's, so the countdown goes with the wording:
                # `_start_attempt` declines on the predicate every tick however long ago the
                # backoff cleared, and a timer that runs out on nothing happening is the same
                # misdirection the sentence above exists to avoid.
                due_at = None
        tick.plan.comments.append(comment)

    await tick.emit(
        gesture.event,
        # Named so the type checker does not read the `**details` splat as supplying it.
        run=None,
        step=step.name,
        attempt=latest.attempt if latest is not None else None,
        next_attempt=next_attempt,
        due_at=None if due_at is None else due_at.isoformat(),
        **details,
    )
    tick.ticket.status = TicketStatus.RUNNING
    return True


async def _terminal(tick: _Tick) -> None:
    """Final projection (design.md §4): `factorial:done`, a comment, and the ticket closed."""
    actions = tick.config.terminal.actions
    _apply_outcome(tick, actions, extra={"step": tick.config.terminal.step})
    if tick.plan.label is None:
        tick.plan.label = LABEL_DONE
    await stop_awake_previews(tick.session, tick.ticket, tick.deps, reason=WHY_NOT_SERVABLE["done"])
    tick.ticket.status = TicketStatus.DONE
    # Never leased again, so never asked whether a refused spawn holds it.
    tick.deps.spawn_rechecks.pop(tick.ticket.id, None)
    tick.ticket.closed_at = tick.now
    tick.plan.close_issue = actions.close_ticket
    await tick.emit(
        EventType.TICKET_DONE,
        step=tick.config.terminal.step,
        close_ticket=actions.close_ticket,
    )


