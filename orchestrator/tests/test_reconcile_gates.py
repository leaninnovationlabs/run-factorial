"""Human gates: rejection outcomes answering the human, waiting/approval/merge, approval
freshness, and changes-requested rejection.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    Event,
    StepRunStatus,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.events.previews import change_request_outcome
from orchestrator.github import (
    CHANGE_REQUEST_MARKER,
    BranchState,
    MergeRefused,
    PullRequestState,
)
from orchestrator.github.state import RepoPermission, ReviewVerdict
from orchestrator.worker.gates import ChangesRefusal
from runtime.contract import (
    Artifact,
    ArtifactKind,
    RunStatus,
)
from tests.reconcile_world import (
    BOT_LOGIN,
    World,
    _build_world,
    _escalate_the_plan_step,
    _process,
    _process_with_gate,
    _process_with_open_approvers,
    _process_with_spec_merge_gate,
    _process_without_gate_reject,
    _process_without_gate_reject_and_escalate_label,
    _process_without_gate_reject_or_escalate,
    _remove_blocked_label,
    advance_to_gate,
    change_request_inputs,
    entered_events,
    event_rows,
    event_types,
    request_changes,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# -------------------------------------------------------- the gate rejection answers the human


async def test_gate_rejection_without_onreject_applies_the_declared_escalate_outcome(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_reject`'s no-onReject arm no longer stops at label-only: a DECLARED
    `defaults.onFailure.escalate` is now applied through `_apply_outcome` — the identical
    machinery `_retry_or_escalate` already uses for an agent step's own escalation — so a comment
    lands on the ticket, not just a label and a dashboard event. `{step}`/`{attempt}`/
    `{failure_reason}` are gate-shaped: the gate's own name and attempt row, and the same reason
    string the ESCALATED event already carried."""
    world = await _build_world(
        maker, config=_process_without_gate_reject(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected, no onReject configured -> escalate applies, then blocks

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED

    (gate,) = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert gate.status == StepRunStatus.REJECTED
    assert gate.attempt == 1

    calls = dict(world.projector.calls)
    # `_status_step` keeps the gate's own step label alongside `factorial:blocked` — the escalate
    # outcome's `ticketLabel` composes with that, it does not replace it (`_desired_labels`).
    assert calls["set_labels"] == frozenset({"factorial:spec_gate", "factorial:blocked"})
    assert calls["set_assignees"] == ("operator",)  # {ticket_owner}, expanded
    assert calls["comment"] == (
        "ADLC spec_gate failed after 1 attempt(s): gate rejected and no onReject is configured."
    )
    assert "escalated" in await event_types(world)


async def test_gate_rejection_without_a_declared_escalate_stays_label_only(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The absence case: with no `defaults.onFailure` declared at all, the no-onReject arm must
    invent nothing — a label and an ESCALATED event, no comment and no assignment."""
    world = await _build_world(
        maker, config=_process_without_gate_reject_or_escalate(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    world.projector.calls.clear()  # isolate this tick's writes from plan's earlier onSuccess
    await run_tick(world)  # rejected, no onReject and no declared escalate -> label-only blocks

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec_gate", "factorial:blocked"})
    assert "set_assignees" not in calls  # nothing declared an `assign` -> nothing to replace with
    assert "comment" not in calls  # nothing declared a `comment` -> never invented
    assert "escalated" in await event_types(world)


async def test_same_tick_reopen_and_reject_still_projects_blocked_without_a_declared_label(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Live-proven: `_reject` can run in the SAME TICK as
    `_open_gate` — exactly `test_gate_unblocked_without_a_real_fix_re_blocks_in_the_same_
    follow_up_tick`'s own drive (a REJECTED gate's next attempt reopens and is immediately
    re-evaluated against a fresh change request in that one follow-up tick, no separate tick for
    `advance_to_gate` to hand it a clean WAITING state first). `_open_gate` applies
    `gate.waiting`'s own `ticketLabel` ("factorial:spec-review") THIS tick, BEFORE `_reject` ever
    runs. A first draft guarded the BLOCKED fallback with `if tick.plan.label is None`
    — copied verbatim from `_retry_or_escalate`, which never faces this same-tick ordering — so
    it saw that non-None waiting label already sitting there and never corrected it to
    `factorial:blocked` when the escalate itself declares no label of its own
    (`_process_without_gate_reject_and_escalate_label`: comment and assign declared, `ticketLabel`
    removed). Left unfixed, the ticket went BLOCKED internally while GitHub kept showing the
    WAITING label forever: `_grant_attempt`'s own `LABEL_BLOCKED in issue.labels` check
    read "already unblocked" on every following tick, reopened the gate, which re-rejected,
    which never projected the label either — an unbounded livelock, proven six ticks deep against
    this exact fixture. The fix forces `factorial:blocked` whenever the escalate outcome does not
    itself declare a label, regardless of what `_open_gate` already wrote this same tick — this
    test proves the label lands correctly on the SAME tick the bug used to eat it, and that the
    tick after is genuinely quiet rather than looping."""
    world = await _build_world(
        maker,
        config=_process_without_gate_reject_and_escalate_label(f"gate-reject-{uuid4().hex[:8]}"),
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected, no onReject configured -> blocks (attempt 1)

    _remove_blocked_label(world)
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED
    world.github.request_changes("reviewer-1")  # a new review: the first one is answered
    world.projector.calls.clear()
    await run_tick(world)  # _gate_step opens attempt 2, then re-rejects it -- SAME TICK

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [r.attempt for r in gates] == [1, 2]
    assert gates[-1].status == StepRunStatus.REJECTED

    # THE bug this proves fixed: without the fix, `set_labels` here is `_open_gate`'s own
    # `gate.waiting` label ("factorial:spec-review") ALONE — never `factorial:blocked` — because
    # `_open_gate` wrote `tick.plan.label` moments before `_reject` ran, this SAME tick.
    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec_gate", "factorial:blocked"})

    # The tick after is QUIET: `factorial:blocked` is now genuinely projected, so the top-level
    # `status == BLOCKED and LABEL_BLOCKED in issue.labels` check holds and returns immediately
    # — no UNBLOCKED, no new step_run, no livelock.
    before_events = await event_types(world)
    before_run_count = len(await step_runs(world))
    await run_tick(world)
    assert await event_types(world) == before_events
    assert len(await step_runs(world)) == before_run_count


async def test_same_tick_reopen_and_reject_projects_blocked_with_no_escalate_at_all(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The same same-tick proof, on `_process_without_gate_reject_or_escalate` — no declared
    `defaults.onFailure` at all, the reviewer's own reproduction fixture (six ticks deep). Not a
    duplicate of the test above: the fix's guard is an `or` of two arms
    (`on_failure is None`, `on_failure.escalate.ticket_label is None`) and this is the one that
    exercises the FIRST arm rather than the second."""
    world = await _build_world(
        maker, config=_process_without_gate_reject_or_escalate(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected, no onReject and no declared escalate -> blocks (attempt 1)

    _remove_blocked_label(world)
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED
    world.github.request_changes("reviewer-1")  # a new review: the first one is answered
    world.projector.calls.clear()
    await run_tick(world)  # _gate_step opens attempt 2, then re-rejects it -- SAME TICK

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [r.attempt for r in gates] == [1, 2]
    assert gates[-1].status == StepRunStatus.REJECTED

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec_gate", "factorial:blocked"})

    before_events = await event_types(world)
    before_run_count = len(await step_runs(world))
    await run_tick(world)
    assert await event_types(world) == before_events
    assert len(await step_runs(world)) == before_run_count


async def test_sandbox_lane_gate_two_rejection_posts_the_escalate_comment(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """End to end on the sandbox lane's own shape (`_process_with_spec_merge_gate`):
    `spec_merge_gate` is the SECOND gate, and it carries NO `onReject` — exactly
    the sandbox lane's process file's real `spec_merge_gate`. A rejection there
    once left the human with a label and a dashboard event and no comment (the sandbox process
    file's own documented rough edge); this proves the escalate outcome now reaches them there
    too, not only on the smaller synthetic fixtures above."""
    world = await _build_world(
        maker, config=_process_with_spec_merge_gate("proc"), bot_login=BOT_LOGIN
    )

    # plan -> spec_gate (approved + merged) -> build
    await run_tick(world)  # plan attempt 1 spawns
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
    )
    await run_tick(world)  # accept plan -> advance to spec_gate
    await run_tick(world)  # open the gate
    world.github.approve("reviewer-1")
    await run_tick(world)  # gate approves, merges the spec PR, advances to "build"

    # build -> test -> code_gate
    await run_tick(world)  # build attempt 1 spawns
    world.github.branches.append(BranchState(name="impl/12-widget", sha="sha-impl-1"))
    world.github.pulls.append(
        PullRequestState(
            number=202,
            head_ref="impl/12-widget",
            base_ref="main",
            head_sha="sha-impl-1",
            state="open",
            merged=False,
            url="https://github.test/pr/202",
        )
    )
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "impl/12-widget"}),),
    )
    await run_tick(world)  # accept build attempt 1 -> advance to "test"

    world.adapter.restart()
    await run_tick(world)  # test attempt 1 spawns
    world.github.mark_ci_green("sha-impl-1")
    world.github.add_pr_comment(202, BOT_LOGIN, "## Test Verdict\nPASS")
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # accept test attempt 1 -> advance to "code_gate"

    await run_tick(world)  # open code_gate (attempt 1)
    world.github.approve("reviewer-1")
    await run_tick(world)  # code_gate approves WITHOUT merging -> advances to "spec_merge_gate"

    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_merge_gate"

    world.projector.calls.clear()
    # `reviewer-2` (spec_merge_gate's own approver) has not approved, so `reviewer-1`'s standing
    # approval never auto-satisfies this second gate — it genuinely waits.
    await run_tick(world)  # open spec_merge_gate (attempt 1)

    (gate,) = [r for r in await step_runs(world) if r.step_name == "spec_merge_gate"]
    assert gate.status == StepRunStatus.WAITING

    # REJECTION: `reviewer-2`, spec_merge_gate's own approver, requests changes on the impl PR —
    # the same path the smaller fixtures above exercise, on the sandbox lane's real second gate.
    world.github.request_changes("reviewer-2")
    await run_tick(world)  # spec_merge_gate rejects; no onReject -> the escalate arm

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert ticket.current_step == "spec_merge_gate"  # no rerunStep to move to

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec_merge_gate", "factorial:blocked"})
    # No `set_assignees` call here is itself expected, not a gap: `spec_gate.onApprove` already
    # assigned `{ticket_owner}` ("operator") earlier in this run, and the escalate outcome's own
    # `assign: ["{ticket_owner}"]` resolves to the SAME login — `_project` only writes what
    # actually differs from the observed assignees, so the outcome still fired (`_apply_outcome`
    # ran), it just had nothing new to project. `set_assignees == ("operator",)` is asserted
    # directly against a fixture built to show a real change (the test above this one).
    assert calls["comment"] == (
        "ADLC spec_merge_gate failed after 1 attempt(s): "
        "gate rejected and no onReject is configured."
    )
    assert "escalated" in await event_types(world)


async def test_projection_after_unblock_carries_the_step_label_not_blocked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_steady_label` only ever returns `LABEL_BLOCKED` while `ticket.status == BLOCKED`
    (`_grant_attempt` flips that before `_project` runs), and `_desired_labels`' `blocked=True`
    branch is reached only through that same status check — so the projection this tick already
    carries `factorial:<step>`, never re-adds `factorial:blocked`, and does not fight the human's
    removal."""
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)

    # The human strips every factory label while fixing things, not just the blocked one — the
    # projection must recompute the whole desired set (`factorial:plan` included) and never
    # reintroduce `factorial:blocked` now that the ticket is RUNNING again.
    world.github.issue = replace(world.github.issue, labels=frozenset())
    world.projector.calls.clear()
    await run_tick(world)

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:plan"})
    assert "factorial:blocked" not in calls["set_labels"]


# --------------------------------------------------------------------------- gates


async def test_gate_waiting_is_projected_and_emitted_once(world: World) -> None:
    await advance_to_gate(world)
    world.projector.calls.clear()
    baseline = await event_types(world)

    for _ in range(3):
        await run_tick(world)

    assert baseline.count("gate_waiting") == 1
    assert await event_types(world) == baseline
    assert world.projector.calls == []
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE


async def test_gate_waiting_names_the_pull_request_it_is_waiting_on(world: World) -> None:
    """The event a reader is sent to when a ticket stops says WHICH pull request to go and review.

    The gate already resolves its pick to render `{pr_url}` into the waiting comment; carrying the
    same number on the event is what lets the ticket page link the row without the UI guessing a
    PR from some earlier event that happened to mention one.
    """
    await advance_to_gate(world)

    (waiting,) = [row for row in await event_rows(world) if row.type == "gate_waiting"]
    assert waiting.details["pr_number"] == 101


async def test_gate_waiting_carries_no_pr_number_when_nothing_matches(world: World) -> None:
    """A null, not a missing key or an invented number: the gate opens on the step it was sent to
    whether or not a pull request has appeared yet, and a row with nothing to point at must show
    no link rather than the wrong one."""
    await run_tick(world)  # plan spawns
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # plan is accepted, the ticket advances to the gate
    world.github.pulls.clear()  # ... and the PR is gone before the gate opens
    await run_tick(world)

    (waiting,) = [row for row in await event_rows(world) if row.type == "gate_waiting"]
    assert waiting.details["pr_number"] is None


async def test_gate_escalation_without_onreject_names_the_rejected_pull_request(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The other way a gate holds a ticket: rejected with no rework edge, so the ticket blocks on
    the gate itself. `_reject` has the pick in hand, so the escalation names it too."""
    world = await _build_world(
        maker, config=_process_without_gate_reject(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)

    (escalated,) = [row for row in await event_rows(world) if row.type == "escalated"]
    assert escalated.details["pr_number"] == 101


async def test_observed_approval_merges_and_advances(world: World) -> None:
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    world.projector.calls.clear()
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.APPROVED
    assert gate.decided_by == "reviewer-1"
    assert ("merge_pr", 101) in world.projector.calls
    types = await event_types(world)
    assert types[-3:] == ["gate_approved", "step_completed", "step_entered"]

    # ... and the terminal step closes the ticket out.
    world.projector.calls.clear()
    await run_tick(world)
    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.DONE
    assert ticket.closed_at is not None
    assert (await event_types(world))[-1] == "ticket_done"
    assert ("comment", "ADLC complete — merged to main.") in world.projector.calls
    assert world.github.issue.labels == frozenset({"factorial:done"})
    # `terminal.actions.closeTicket` closes the issue, and only after the label and comment land.
    assert ("close_issue", world.github.issue.number) in world.projector.calls
    assert world.projector.calls[-1][0] == "close_issue"
    assert world.github.issue.state == "closed"

    # A closed ticket converges: nothing left to write, so no further calls or events.
    world.projector.calls.clear()
    before = await event_types(world)
    await run_tick(world)
    assert world.projector.calls == []
    assert await event_types(world) == before


async def test_gate_rejection_loops_back_with_a_new_attempt(world: World) -> None:
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "plan"
    assert "gate_rejected" in await event_types(world)
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.REJECTED

    # Rework is a NEW attempt of the step that was sent back, not a re-read of the old run.
    world.adapter.restart()
    await run_tick(world)
    assert [(r.step_name, r.attempt) for r in await step_runs(world)] == [
        ("plan", 1),
        ("spec_gate", 1),
        ("plan", 2),
    ]


async def test_gate_rejection_records_the_step_it_sends_work_back_to(world: World) -> None:
    """A rejection is the one move that runs the graph BACKWARD. `reason` is what lets a reader
    tell it from an ordinary advance, which is why the entry carries one at all."""
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)

    assert (await entered_events(world))[-1] == {
        "step": "plan",
        "from_step": "spec_gate",
        "reason": "reject_rerun",
        "attempt": 2,
        "kind": "agent",
    }


async def test_entering_the_terminal_step_carries_no_attempt(world: World) -> None:
    """The terminal step has no `Step` in the config and never gets a `step_run`, and
    `ticket_done` carries no attempt either. A number on the entry would file the two under
    different `step#attempt` keys and draw the terminal node as two blocks, the first empty."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    await run_tick(world)  # approve, then advance onto `done`

    assert (await ticket_row(world)).current_step == "done"
    assert (await entered_events(world))[-1] == {
        "step": "done",
        "from_step": "spec_gate",
        "reason": "next",
        "attempt": None,
        "kind": "terminal",
    }

    await run_tick(world)  # terminal
    done = next(row for row in await event_rows(world) if row.type == "ticket_done")
    # Neither names an attempt, so both resolve to the same block on the ticket page.
    assert "attempt" not in done.details


# ---------------------------------------------------------- gate-approval freshness at the gate


async def test_gate_approved_and_merged_before_the_poll_still_advances(world: World) -> None:
    """A human approves AND merges the gate's PR between two polls, so the first tick that looks
    again already sees state="closed", merged=True, the approval frozen in review history.
    `pr_approved` (rightly, for config) skips merged PRs; the closed-arm requires NOT merged; the
    changes-requested arm requires open — before the resolve-first fix the tick fell through
    every arm and the ticket sat WAITING_GATE forever, eventless. The gate must record the
    observed decision and advance WITHOUT attempting a second merge."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    world.github.merge_externally()
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.APPROVED
    assert gate.decided_by == "reviewer-1"
    # The merge already happened out-of-band; a merged PR must never be re-merged.
    assert "merge_pr" not in world.projector.kinds()
    approved = next(row for row in await event_rows(world) if row.type == "gate_approved")
    assert approved.details["merged_sha"] is None

    # ... and downstream continues: the terminal step closes the ticket out.
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.DONE


async def test_gate_merged_without_approval_keeps_waiting(world: World) -> None:
    """The honest outcome for a merge with NO qualifying approval: keep WAITING, visibly.

    The gate's mandate is a named approver's approval — the merge itself is not one, and
    blessing it would convert any review bypass on `main` into a silent auto-advance for exactly
    the merges that skipped review. Nor is it a rejection: the closed-arm reads
    closed-WITHOUT-merge as the reviewer's dismissal, which a merge is not. So the ticket stays
    WAITING_GATE, deliberately, and the wait is recoverable: a retroactive approval on the merged
    PR is honored on the next poll, without a re-merge, exactly like the race test above.

    Bite note: against the pre-fix code only the RECOVERY half fails — the waiting half was also
    the wedge's own (silent) behavior, so the first three ticks pass either way."""
    await advance_to_gate(world)
    world.github.merge_externally()  # no approval at all
    baseline = await event_types(world)
    world.projector.calls.clear()

    for _ in range(3):
        await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.WAITING_GATE
    assert ticket.current_step == "spec_gate"
    assert await event_types(world) == baseline
    assert world.projector.calls == []

    # A human sorts it out by approving retroactively -> the gate advances, still merge-free.
    world.github.approve("reviewer-1")
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"
    assert "merge_pr" not in world.projector.kinds()


async def test_gate_reads_approval_off_its_own_pr_not_any_glob_match(world: World) -> None:
    """The pre-existing residual the resolve-first shape closes: two OPEN PRs match the gate's
    glob, the older approved, the newer not. The old existential `pr_approved` read said
    "approved" off #101 while `_approve` merged `_find_pr`'s pick — #102, the newer, UNAPPROVED
    one: a human review skipped. Verdict and action must be read off the same, single PR: the
    gate waits until the pick itself is approved, then merges the pick."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")  # lands on #101, the only PR so far
    world.github.pulls.append(
        PullRequestState(
            number=102,
            head_ref="spec/12-widget-v2",
            base_ref="main",
            head_sha="sha-2",
            state="open",
            merged=False,
            url="https://github.test/pr/102",
        )
    )
    world.projector.calls.clear()

    await run_tick(world)

    # `_find_pr` prefers open-then-newest -> #102; it carries no approval -> keep waiting.
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    assert "merge_pr" not in world.projector.kinds()

    # Approving the pick itself is what advances — and what merges is the pick, #102.
    world.github.approve("reviewer-1")
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"
    assert ("merge_pr", 102) in world.projector.calls
    assert ("merge_pr", 101) not in world.projector.calls


async def test_gate_advances_when_only_the_pick_is_approved(world: World) -> None:
    """The inverse pick corner: the approval sits on the NEWER open PR — `_find_pr`'s pick —
    while the older open #101 has none. The gate advances and what merges is the pick."""
    await advance_to_gate(world)
    world.github.pulls.append(
        PullRequestState(
            number=102,
            head_ref="spec/12-widget-v2",
            base_ref="main",
            head_sha="sha-2",
            state="open",
            merged=False,
            url="https://github.test/pr/102",
        )
    )
    world.github.verdicts[102] = ReviewVerdict(
        approvals=("reviewer-1",), commits={"reviewer-1": "sha-2"}
    )
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.decided_by == "reviewer-1"
    assert ("merge_pr", 102) in world.projector.calls
    assert ("merge_pr", 101) not in world.projector.calls


async def test_gate_waits_on_the_7_3_shape_and_merges_nothing(world: World) -> None:
    """The frozen-approval reproduction pinned at GATE level, not just in the predicate: PR #101
    merged with its approval frozen, PR #100 open and unapproved — the gate waits and nothing
    merges. `_find_pr`'s open-preference is now the load-bearing protector of that fix: arm 1
    accepts a merged pick (the approve-and-merge race fix), so the frozen #101 approval would
    advance the gate if #101 could ever BE the pick while an open PR exists. The open PR
    deliberately carries the LOWER number: newest-preference alone would pick the merged #101
    and advance on frozen evidence, so this test bites on the open-preference key itself, not on
    numbering luck (the whole suite passed under a newest-only key before this test)."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    world.github.merge_externally()  # 101: merged, approval frozen in history
    world.github.pulls.append(
        PullRequestState(
            number=100,
            head_ref="spec/12-rework",
            base_ref="main",
            head_sha="sha-2",
            state="open",
            merged=False,
            url="https://github.test/pr/100",
        )
    )
    baseline = await event_types(world)
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.WAITING_GATE
    assert ticket.current_step == "spec_gate"
    assert await event_types(world) == baseline  # no gate_approved, no step_completed
    assert "merge_pr" not in world.projector.kinds()


async def test_gate_ignores_an_approval_of_an_earlier_commit(world: World) -> None:
    """A commit lands after the approval and the base branch does not dismiss stale approvals,
    so GitHub still lists the review as the approver's latest APPROVED one. It approved none of
    the new code: the gate waits, and advances only on an approval of the head it merges."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")  # on sha-1
    world.github.push(101, "sha-2")
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_gate"
    assert ticket.status == TicketStatus.WAITING_GATE
    assert "merge_pr" not in world.projector.kinds()

    world.github.approve("reviewer-1")  # on sha-2
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "done"
    assert world.projector.merge_shas[101] == ["sha-2"]


async def test_a_refused_merge_keeps_the_gate_waiting_and_says_why_once(world: World) -> None:
    """GitHub declining the merge is a state of the pull request, not a failed tick: raising
    would fail every poll until the ticket is quarantined (decision 31). The gate keeps waiting,
    retries the merge each tick, and announces each distinct refusal once."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    required = "Required status check \"verify\" is expected."
    world.projector.refuse_merge = MergeRefused(405, required)
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_gate"
    assert ticket.status == TicketStatus.WAITING_GATE
    (gate,) = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert gate.status == StepRunStatus.WAITING
    (refused,) = [row for row in await event_rows(world) if row.type == "gate_merge_refused"]
    assert refused.details["pr_number"] == 101
    assert refused.details["head_sha"] == "sha-1"
    assert refused.details["approved_by"] == "reviewer-1"
    assert refused.details["status"] == 405
    assert refused.details["reason"] == required
    comments = [body for kind, body in world.projector.calls if kind == "comment"]
    assert len(comments) == 1
    assert required in comments[0]

    world.projector.calls.clear()
    await run_tick(world)  # the same refusal again: tried, and not announced twice
    assert len(world.projector.merge_shas[101]) == 2
    assert "comment" not in world.projector.kinds()
    assert [r.type for r in await event_rows(world)].count("gate_merge_refused") == 1

    behind = "Head branch is out of date."
    world.projector.refuse_merge = MergeRefused(405, behind)
    await run_tick(world)  # a different refusal is news
    assert [r.type for r in await event_rows(world)].count("gate_merge_refused") == 2

    world.projector.refuse_merge = None
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"
    (gate,) = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert gate.status == StepRunStatus.APPROVED
    assert gate.decided_by == "reviewer-1"


# ----------------------------------------------------------------- changes-requested rejection


async def test_gate_rejects_on_an_approvers_changes_requested_review(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`pr_changes_requested` is now read by the gate: a "Request changes" review from
    a configured approver rejects, the same observable effects (label/assign/rerunStep) an
    explicit closed-PR rejection already produces — and the PR need not even be closed."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")  # the sample's sole spec_gate approver
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "plan"  # rerunStep fired
    assert ticket.status == TicketStatus.RUNNING
    assert "gate_rejected" in await event_types(world)
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.REJECTED

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec-rework"})
    assert calls["set_assignees"] == ("operator",)


async def test_gate_ignores_a_non_approvers_changes_requested_review(world: World) -> None:
    """The identical trust model `pr_approved` already applies to approvals: a login outside
    `approvers` cannot reject any more than it could approve."""
    await advance_to_gate(world)
    world.github.request_changes("someone-else")  # not in `approvers`
    await run_tick(world)

    assert "gate_rejected" not in await event_types(world)
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_approval_wins_over_a_simultaneous_changes_requested_review(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Precedence, decided and pinned here: when the SAME pull request carries one approver's
    approval and a DIFFERENT approver's outstanding changes-requested review, the gate still
    approves — the approval check runs first, and neither rejection branch is even reached once
    it succeeds."""
    world = await _build_world(maker, config=_process_with_open_approvers("proc"))
    await advance_to_gate(world)
    world.github.approve("teammate")
    world.github.request_changes("teammate-2")
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    assert "gate_rejected" not in await event_types(world)
    assert ("merge_pr", 101) in world.projector.calls


# ------------------------------------------------------------------ bots never decide a gate


async def test_the_apps_own_approval_never_passes_an_open_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Agents act as the App and hold `pull_requests: write`, so they can approve a pull request
    a person opened. An empty `approvers` must not turn that into a passed gate."""
    world = await _build_world(
        maker, config=_process_with_open_approvers("proc"), bot_login=BOT_LOGIN
    )
    await advance_to_gate(world)
    world.github.approve(BOT_LOGIN, bot=True)
    world.projector.calls.clear()
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    assert "gate_approved" not in await event_types(world)
    assert "merge_pr" not in world.projector.kinds()


async def test_the_apps_own_approval_never_counts_even_when_named(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_gate("proc", approvers=[BOT_LOGIN]), bot_login=BOT_LOGIN
    )
    await advance_to_gate(world)
    world.github.approve(BOT_LOGIN, bot=True)
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "spec_gate"
    assert "gate_approved" not in await event_types(world)


async def test_another_bots_approval_counts_only_when_named(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    bot = "dependabot[bot]"
    unnamed = await _build_world(
        maker, config=_process_with_open_approvers("proc"), bot_login=BOT_LOGIN
    )
    await advance_to_gate(unnamed)
    unnamed.github.approve(bot, bot=True)
    await run_tick(unnamed)
    assert (await ticket_row(unnamed)).current_step == "spec_gate"

    named = await _build_world(
        maker, config=_process_with_gate("proc", approvers=[bot]), bot_login=BOT_LOGIN
    )
    await advance_to_gate(named)
    named.github.approve(bot, bot=True)
    await run_tick(named)
    assert (await ticket_row(named)).current_step == "done"
    gate = next(run for run in await step_runs(named) if run.step_name == "spec_gate")
    assert gate.decided_by == bot


async def test_a_bots_change_request_never_rejects_an_open_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_open_approvers("proc"), bot_login=BOT_LOGIN
    )
    await advance_to_gate(world)
    world.github.request_changes("dependabot[bot]", bot=True)
    await run_tick(world)

    assert "gate_rejected" not in await event_types(world)
    assert (await ticket_row(world)).current_step == "spec_gate"


# ------------------------------------------------------------- anyone with write access decides


async def _permission_world(
    maker: async_sessionmaker[AsyncSession],
    permissions: dict[str, RepoPermission],
    *,
    approvers: tuple[str, ...] = (),
) -> World:
    """`spec_gate` with `approverPermission: write`, GitHub answering `permissions`, waiting."""
    world = await _build_world(
        maker,
        config=_process_with_gate(
            f"perm-{uuid4().hex[:8]}", approvers=list(approvers), approverPermission="write"
        ),
        bot_login=BOT_LOGIN,
    )
    world.github.permissions.update(permissions)
    await advance_to_gate(world)
    world.projector.calls.clear()
    world.github.permission_reads.clear()
    return world


@pytest.mark.parametrize("level", ["write", "maintain", "admin"])
async def test_a_writers_approval_passes_a_permission_gate(
    maker: async_sessionmaker[AsyncSession], level: RepoPermission
) -> None:
    world = await _permission_world(maker, {"teammate": level})
    world.github.approve("teammate")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "done"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.decided_by == "teammate"
    assert ("merge_pr", 101) in world.projector.calls


@pytest.mark.parametrize("login", ["reader", "triager", "stranger"])
async def test_a_read_only_reviewers_approval_decides_nothing(
    maker: async_sessionmaker[AsyncSession], login: str
) -> None:
    """With the key set and nobody named, nobody qualifies here: the gate waits. `stranger`
    has no entry, which GitHub reports as no access."""
    world = await _permission_world(maker, {"reader": "read", "triager": "triage"})
    world.github.approve(login)
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    assert "gate_approved" not in await event_types(world)
    assert "merge_pr" not in world.projector.kinds()


async def test_a_bot_with_write_never_passes_a_permission_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"ci-helper[bot]": "write"})
    world.github.approve("ci-helper[bot]", bot=True)
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "spec_gate"
    assert world.github.permission_reads == []


async def test_a_named_approver_needs_no_lookup(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {}, approvers=("reviewer-1",))
    world.github.approve("reviewer-1")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "done"
    assert world.github.permission_reads == []


async def test_a_named_gate_with_a_permission_also_takes_a_writer(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate": "write"}, approvers=("reviewer-1",))
    world.github.approve("teammate")
    await run_tick(world)

    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.decided_by == "teammate"


async def test_a_permission_gate_with_no_reviews_costs_no_lookups(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate": "write"})
    await run_tick(world)

    assert world.github.permission_reads == []
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE


async def test_a_writers_change_request_sends_the_work_back(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate": "write"})
    world.github.request_changes("teammate")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "plan"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.REJECTED


async def test_a_read_only_change_request_sends_nothing_back(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate-2": "read"})
    world.github.request_changes("teammate-2")
    await run_tick(world)

    assert "gate_rejected" not in await event_types(world)
    assert (await ticket_row(world)).current_step == "spec_gate"



async def test_gate_approved_records_how_the_reviewer_qualified(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A role can change later, so the event keeps why that login counted when it did."""
    world = await _permission_world(maker, {"teammate": "admin"}, approvers=("reviewer-1",))
    world.github.approve("teammate")
    await run_tick(world)
    (approved,) = [row for row in await event_rows(world) if row.type == "gate_approved"]
    assert approved.details["qualified_as"] == "permission"
    assert approved.details["permission"] == "admin"

    named = await _permission_world(maker, {}, approvers=("reviewer-1",))
    named.github.approve("reviewer-1")
    await run_tick(named)
    (approved,) = [row for row in await event_rows(named) if row.type == "gate_approved"]
    assert approved.details["qualified_as"] == "named"
    assert approved.details["permission"] is None


async def test_gate_rejected_records_how_the_reviewer_qualified(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate": "write"})
    world.github.request_changes("teammate")
    await run_tick(world)
    (rejected,) = [row for row in await event_rows(world) if row.type == "gate_rejected"]
    assert rejected.details["qualified_as"] == "permission"
    assert rejected.details["permission"] == "write"


async def test_a_rejection_no_review_decided_records_no_qualification(world: World) -> None:
    await advance_to_gate(world)
    await request_changes(world)
    await run_tick(world)
    (rejected,) = [row for row in await event_rows(world) if row.type == "gate_rejected"]
    assert rejected.details["qualified_as"] is None
    assert rejected.details["permission"] is None


# -------------------------------------------------------------- a review that does not count


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def _ignored_rows(world: World) -> list[dict[str, object]]:
    return [row.details for row in await event_rows(world) if row.type == "gate_review_ignored"]


async def test_an_approval_that_does_not_count_is_announced_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Without this the reviewer sees no reaction at all: the gate just keeps waiting."""
    world = await _permission_world(maker, {"teammate-2": "read"})
    world.github.approve("teammate-2")
    for _ in range(3):
        await run_tick(world)

    (ignored,) = await _ignored_rows(world)
    assert ignored["reviewer"] == "teammate-2"
    assert ignored["verdict"] == "approved"
    assert ignored["reason"] == "below_permission"
    assert ignored["permission"] == "read"
    assert ignored["required"] == "write"
    assert ignored["pr_number"] == 101
    (comment,) = _comments(world)
    assert "`teammate-2`" in comment
    assert "does not count at `spec_gate`" in comment
    assert "read access" in comment
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE


async def test_a_change_request_from_someone_not_named_is_announced(world: World) -> None:
    await advance_to_gate(world)
    world.github.request_changes("someone-else")
    await run_tick(world)

    (ignored,) = await _ignored_rows(world)
    assert ignored["verdict"] == "changes_requested"
    assert ignored["reason"] == "not_named"
    assert ignored["permission"] is None
    assert any("request for changes by `someone-else`" in body for body in _comments(world))


async def test_the_apps_own_approval_is_announced(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_open_approvers("proc"), bot_login=BOT_LOGIN
    )
    await advance_to_gate(world)
    world.github.approve(BOT_LOGIN, bot=True)
    await run_tick(world)

    (ignored,) = await _ignored_rows(world)
    assert ignored["reason"] == "app"


async def test_nothing_is_announced_when_the_gate_decides_in_the_same_poll(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _permission_world(maker, {"teammate": "write", "teammate-2": "read"})
    world.github.approve("teammate-2")
    world.github.approve("teammate")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "done"
    assert await _ignored_rows(world) == []
    assert not any("does not count" in body for body in _comments(world))


async def test_a_review_id_past_32_bits_is_announced_once_and_the_gate_keeps_polling(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub review ids passed 2**31: reading the announced ids back as a 32-bit integer failed
    every later poll of the ticket."""
    world = await _permission_world(maker, {"teammate-2": "read"})
    world.github.approve("teammate-2")
    world.github.verdicts[101] = replace(
        world.github.verdicts[101], review_ids={"teammate-2": 5_684_826_022}
    )
    for _ in range(3):
        await run_tick(world)

    (ignored,) = await _ignored_rows(world)
    assert ignored["review_id"] == 5_684_826_022
    assert len(_comments(world)) == 1
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE


async def test_a_review_with_no_id_is_never_announced(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing could tell the next poll it had been announced, so it would repeat every poll."""
    world = await _permission_world(maker, {"teammate-2": "read"})
    world.github.approve("teammate-2")
    world.github.verdicts[101] = replace(world.github.verdicts[101], review_ids={})
    await run_tick(world)
    await run_tick(world)

    assert await _ignored_rows(world) == []
    assert _comments(world) == []


async def test_an_ignored_review_is_not_announced_again_when_the_gate_reopens(
    world: World,
) -> None:
    """A CHANGES_REQUESTED review stays its author's latest after the rework pushes, so the
    reopened gate weighs it again. It was said once already."""
    await advance_to_gate(world)
    world.github.request_changes("someone-else")
    await run_tick(world)  # ignored, and announced
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # gate attempt 1 rejects on reviewer-1's request
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    await _redeliver(world)
    world.adapter.restart()
    await run_tick(world)

    assert await _gate_statuses(world) == [StepRunStatus.REJECTED, StepRunStatus.WAITING]
    assert len(await _ignored_rows(world)) == 1

async def _redeliver(world: World) -> None:
    """The running plan attempt pushes a new commit to the same PR and succeeds; the gate
    reopens."""
    world.github.pulls = [replace(pr, head_sha="sha-2") for pr in world.github.pulls]
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(
            Artifact(
                kind=ArtifactKind.BRANCH,
                locator={"branch": f"spec/{world.issue_number}-widget"},
            ),
        ),
    )
    await run_tick(world)  # accept the plan attempt -> spec_gate
    await run_tick(world)  # the gate reopens


async def _gate_statuses(world: World) -> list[str]:
    return [run.status for run in await step_runs(world) if run.step_name == "spec_gate"]


async def test_a_change_request_rejects_once_and_the_rework_waits_for_a_new_review(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """factorial#132. GitHub keeps a CHANGES_REQUESTED review as the reviewer's latest after new
    commits (dismiss-stale clears approvals only), so reading it as a fresh verdict rejected
    every rework again, with no review in between and no cap on rework attempts."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # gate attempt 1 rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    await _redeliver(world)
    world.adapter.restart()
    await run_tick(world)

    assert await _gate_statuses(world) == [StepRunStatus.REJECTED, StepRunStatus.WAITING]
    assert [run.attempt for run in await step_runs(world) if run.step_name == "plan"] == [1, 2]


async def test_every_standing_change_request_is_answered_by_one_rejection(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The rework was sent back for every change request standing when the gate rejected, not
    just the one its reason names. Recording only that one would let the other reject attempt
    2, and the first reject attempt 3."""
    world = await _build_world(maker, config=_process_with_open_approvers("proc"))
    await advance_to_gate(world)
    world.github.request_changes("teammate")
    world.github.request_changes("teammate-2")
    await run_tick(world)  # gate attempt 1 rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    await _redeliver(world)
    world.adapter.restart()
    await run_tick(world)

    assert await _gate_statuses(world) == [StepRunStatus.REJECTED, StepRunStatus.WAITING]


async def test_a_new_change_request_on_the_rework_rejects_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # gate attempt 1 rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    await _redeliver(world)
    world.github.request_changes("reviewer-1")  # a second review, of the reworked spec
    await run_tick(world)

    assert await _gate_statuses(world) == [StepRunStatus.REJECTED, StepRunStatus.REJECTED]
    assert (await ticket_row(world)).current_step == "plan"


async def test_a_change_request_left_during_the_rework_rejects_the_reopened_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A review already acted on is what cannot reject again, not a review older than the
    rework: a reviewer who asks for more while the agent works is heard when the gate reopens."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # gate attempt 1 rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    world.github.request_changes("reviewer-1")  # left while attempt 2 runs
    await _redeliver(world)

    assert await _gate_statuses(world) == [StepRunStatus.REJECTED, StepRunStatus.REJECTED]


# ------------------------------------------------- a rework moves the pull request it was sent for


async def test_a_rejection_records_the_commit_it_rejected(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The rework's acceptance compares the pull request's head against it."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)

    (rejected,) = [row for row in await event_rows(world) if row.type == "gate_rejected"]
    assert rejected.details["head_sha"] == "sha-1"


async def _rework_ends(world: World, *, head: str, branch_head: str | None = None) -> None:
    """The gate rejects PR #101 at sha-1, and the rework attempt reports success with the PR's
    head at `head` and its branch at `branch_head`, or at `head` when that is None."""
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    world.github.push(101, head)
    world.github.branches = [
        replace(branch, sha=branch_head or head) for branch in world.github.branches
    ]
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)


async def _postcondition_failures(world: World) -> list[list[str]]:
    return [
        row.details["failures"]
        for row in await event_rows(world)
        if row.type == EventType.POSTCONDITION_FAILED
    ]


async def test_a_rework_that_leaves_the_rejected_commit_in_place_is_not_accepted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Its branch and open PR held before it began, so they cannot tell a rework that answered
    the reviewer from one that pushed nothing."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    await _rework_ends(world, head="sha-1")

    assert await _postcondition_failures(world) == [
        ["the pull request #101 still ends at sha-1, the commit the gate rejected"]
    ]
    *_, rework = await step_runs(world)
    assert (rework.step_name, rework.status) == ("plan", StepRunStatus.FAILED)


async def test_a_rework_that_moves_the_rejected_pull_request_is_accepted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    await advance_to_gate(world)
    await _rework_ends(world, head="sha-2")

    assert await _postcondition_failures(world) == []
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_a_rejection_recorded_without_its_commit_accepts_the_rework_as_before(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rejection recorded before the gate kept the commit has nothing to compare against."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects
    async with world.sessions() as session:
        (rejected,) = await session.scalars(
            select(Event).where(
                Event.ticket_id == world.ticket_id, Event.type == EventType.GATE_REJECTED
            )
        )
        rejected.details = {k: v for k, v in rejected.details.items() if k != "head_sha"}
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)

    assert await _postcondition_failures(world) == []
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_a_push_the_pull_request_listing_has_not_shown_yet_still_counts(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub moves a branch on push and the pull request's head later. A headless run is read
    once, as it exits, and must not lose an attempt to that lag."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    await _rework_ends(world, head="sha-1", branch_head="sha-2")

    assert await _postcondition_failures(world) == []
    assert (await ticket_row(world)).current_step == "spec_gate"


def _process_reworking_another_branch(name: str) -> ProcessConfig:
    raw = _process(name).model_dump(mode="json", by_alias=True)
    raw["steps"][0]["workspace"]["branch"] = "impl/{ticket}-{slug}"
    return ProcessConfig.model_validate(raw)


async def test_a_rework_that_pushes_another_branch_than_the_rejected_one_is_not_held(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stacked lane can send a rejection back to the step that pushes the branch below the
    gate's: that PR moves only after the step is accepted, so holding the step would hold it for
    ever."""
    world = await _build_world(maker, config=_process_reworking_another_branch("proc"))
    await advance_to_gate(world)
    await _rework_ends(world, head="sha-1")

    assert await _postcondition_failures(world) == []
    assert (await ticket_row(world)).current_step == "spec_gate"


# ----------------------------------------------------------------- dashboard change requests


def _process_with_plan_preview(name: str, *, max_attempts: int = 2) -> ProcessConfig:
    raw = _process(name).model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {"reviewGate": "spec_gate"}
    raw["defaults"]["retry"]["maxAttempts"] = max_attempts
    return ProcessConfig.model_validate(raw)


async def _outcome(world: World, request_id: int) -> tuple[str, str | None]:
    async with world.sessions() as session:
        return await change_request_outcome(session, world.ticket_id, request_id)


async def test_a_dashboard_change_request_reruns_the_previewed_step_with_its_feedback(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_plan_preview("proc"))
    await advance_to_gate(world)
    request_id = await request_changes(world, "Make the button blue.\n\nAnd bigger.")
    await run_tick(world)

    assert await _outcome(world, request_id) == ("accepted", None)
    assert (await ticket_row(world)).current_step == "plan"
    world.adapter.restart()
    await run_tick(world)

    runs = [(r.step_name, r.attempt, r.status) for r in await step_runs(world)]
    assert runs == [
        ("plan", 1, StepRunStatus.SUCCEEDED),
        ("spec_gate", 1, StepRunStatus.REJECTED),
        ("plan", 2, StepRunStatus.RUNNING),
    ]
    assert change_request_inputs(world.adapter.spawns[-1]) == [
        "Make the button blue.\n\nAnd bigger."
    ]


async def test_a_dashboard_change_request_needs_no_preview_on_the_reviewed_step(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process("proc"))
    await advance_to_gate(world)
    request_id = await request_changes(world, "Rename the endpoint.")
    await run_tick(world)

    assert await _outcome(world, request_id) == ("accepted", None)
    assert (await ticket_row(world)).current_step == "plan"


def _process_without_rejection(name: str) -> ProcessConfig:
    raw = _process(name).model_dump(mode="json", by_alias=True)
    del raw["steps"][1]["gate"]["onReject"]
    return ProcessConfig.model_validate(raw)


async def test_a_dashboard_change_request_at_a_gate_with_no_rejection_route_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_without_rejection("proc"))
    await advance_to_gate(world)
    request_id = await request_changes(world)
    await run_tick(world)

    assert await _outcome(world, request_id) == ("refused", ChangesRefusal.NOT_SENT_BACK)
    (refused,) = [row for row in await event_rows(world) if row.type == "step_changes_refused"]
    assert (refused.details["step"], refused.details["reviewed_step"]) == ("spec_gate", "plan")
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_a_dashboard_rejection_also_answers_a_writers_standing_change_request(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The dashboard rejection records every standing GitHub change request it answers; on a
    permission gate that includes a writer's, found through the same lookup."""
    config = _process_with_gate(
        "proc",
        base=_process_with_plan_preview("proc"),
        approvers=[],
        approverPermission="write",
    )
    world = await _build_world(maker, config=config)
    world.github.permissions["teammate"] = "write"
    await advance_to_gate(world)
    world.github.request_changes("teammate")
    await request_changes(world)
    await run_tick(world)

    (rejected,) = [row for row in await event_rows(world) if row.type == "gate_rejected"]
    assert rejected.details["review_ids"] == [world.github.verdicts[101].review_ids["teammate"]]


async def test_the_feedback_reaches_a_retry_of_the_rework_too(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The request stays outstanding until the gate decides again, so a rework attempt that
    fails and retries is still working on it. Three attempts, because the rework is the second."""
    world = await _build_world(maker, config=_process_with_plan_preview("proc", max_attempts=3))
    await advance_to_gate(world)
    await request_changes(world, "Rename the widget.")
    await run_tick(world)  # the gate rejects
    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 2 fails -> retry scheduled
    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 3 spawns

    assert [change_request_inputs(spec) for spec in world.adapter.spawns[1:]] == [
        ["Rename the widget."],
        ["Rename the widget."],
    ]


async def test_a_change_request_is_honoured_before_a_same_poll_approval(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_plan_preview("proc"))
    await advance_to_gate(world)
    await request_changes(world)
    world.github.approve("reviewer-1")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "plan"
    assert not [call for call in world.projector.calls if call[0] == "merge_pr"]


#: Markdown, a hard line break (two trailing spaces) and a mention: each must arrive as typed.
_FEEDBACK = (
    "Make the toggle remember its choice.\n\n- cards misalign below 600px  \n- `@teammate-3` agrees"
)


async def test_an_honoured_change_request_is_posted_to_the_pull_request_as_written(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The pull request keeps the reviewer's words, and whose they were, for whoever reviews it
    or audits it later."""
    world = await _build_world(maker, config=_process_with_plan_preview("proc"))
    await advance_to_gate(world)
    await request_changes(world, _FEEDBACK)
    await run_tick(world)
    await run_tick(world)  # nothing new: the request is answered and posted once

    (posted,) = [body for number, body in world.projector.comments if number == 101]
    assert posted.endswith(f"\n\n{_FEEDBACK}")
    assert "reviewer@example.com" in posted
    assert "`spec_gate`" in posted
    assert "attempt 1 of `plan`" in posted
    assert posted.startswith(CHANGE_REQUEST_MARKER)


async def test_a_refused_change_request_posts_nothing_to_the_pull_request(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_plan_preview("proc"))
    await advance_to_gate(world)
    await request_changes(world, _FEEDBACK)
    world.github.approve("reviewer-1")
    world.github.merge_externally()
    await run_tick(world)

    assert [number for number, _ in world.projector.comments if number == 101] == []


async def test_a_change_request_after_the_merge_is_refused_and_undoes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_plan_preview("proc"))
    await advance_to_gate(world)
    request_id = await request_changes(world)
    world.github.approve("reviewer-1")
    world.github.merge_externally()
    await run_tick(world)

    assert await _outcome(world, request_id) == ("refused", ChangesRefusal.ALREADY_MERGED)
    assert (await ticket_row(world)).current_step == "done"
    assert [r.step_name for r in await step_runs(world)] == ["plan", "spec_gate"]


# ---------------------------------------- a ticket pinned before `preview.page` was retired

_UNMOVED_101 = "the pull request #101 still ends at sha-1, the commit the gate rejected"


def _process_pinned_with_a_page(name: str) -> ProcessConfig:
    raw = _process(name).model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {
        "reviewGate": "spec_gate",
        "page": {"branch": "pages/{ticket}-{slug}", "path": "summary.html"},
    }
    return ProcessConfig.model_validate(raw)


async def _paged_rework_spawns(world: World) -> None:
    """plan attempt 1 delivers PR #101 at sha-1 beside a page on its old page branch, and the
    reviewer sends it back from the dashboard; plan attempt 2 spawns."""
    await run_tick(world)
    world.github.push_page(
        f"pages/{world.issue_number}-widget", "summary.html", commit="c1", blob="b-c1"
    )
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # accept + advance
    await run_tick(world)  # open the gate
    await request_changes(world, "The summary leaves out the risks.")
    await run_tick(world)  # the gate rejects
    world.adapter.restart()
    await run_tick(world)


async def test_a_dashboard_rejection_records_no_page(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_pinned_with_a_page("proc"))
    await _paged_rework_spawns(world)

    (rejected,) = [row for row in await event_rows(world) if row.type == "gate_rejected"]
    assert "page_branch" not in rejected.details
    assert "page_sha" not in rejected.details


async def test_a_rework_that_moves_only_an_old_page_branch_has_not_answered_its_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The summary is a declared file on the step's own branch now, so changing it moves the
    pull request like any other answer; a page branch moving answers nothing."""
    world = await _build_world(maker, config=_process_pinned_with_a_page("proc"))
    await _paged_rework_spawns(world)
    world.github.push_page(
        f"pages/{world.issue_number}-widget", "summary.html", commit="c2", blob="b-c2"
    )
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)

    assert await _postcondition_failures(world) == [[_UNMOVED_101]]
