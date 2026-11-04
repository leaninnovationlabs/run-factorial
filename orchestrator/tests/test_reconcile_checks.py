"""Check steps: completion on observed conditions, the attempt-free wait, the timeout
ladder, idempotence, and rework re-entry — the fourth dispatch arm, evaluated, never spawned.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    StepKind,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.github import (
    BranchState,
    PullRequestState,
)
from orchestrator.worker import check_steps as check_steps_module
from orchestrator.worker.reconcile import reconcile_ticket
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    _build_world,
    _fail_observation,
    _process_with_check,
    _remove_blocked_label,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _impl_pr(*, number: int = 33, head_sha: str = "beefbeef" * 5) -> PullRequestState:
    return PullRequestState(
        number=number,
        head_ref="impl/12-widget",
        base_ref="spec/12-widget",
        head_sha=head_sha,
        state="open",
        merged=False,
        url=f"https://github.test/pr/{number}",
    )


def _deliver_green_impl(world) -> None:
    sha = "beefbeef" * 5
    world.github.branches.append(BranchState(name="impl/12-widget", sha=sha))
    world.github.pulls.append(_impl_pr(head_sha=sha))
    world.github.mark_ci_green(sha, name="verify")


async def test_check_step_completes_when_conditions_already_hold(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Conditions that hold on the entry tick complete the step on that same tick — start and
    completion are one transition, as on an action step — and `{pr_url}` renders from the
    step's own `pr:` matcher even though the step pushed nothing."""
    world = await _build_world(maker, config=_process_with_check("proc-check"))
    _deliver_green_impl(world)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    assert ticket.status == TicketStatus.RUNNING

    (run,) = await step_runs(world)
    assert run.kind == StepKind.CHECK
    assert run.status == StepRunStatus.SUCCEEDED
    assert run.attempt == 1
    # Nothing ran: `runtime_kind` stays NULL, like an action's (0007_check_step_kind).
    assert run.runtime_kind is None

    assert await event_types(world) == ["step_started", "step_completed", "step_entered"]

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:code-review"})
    assert "https://github.test/pr/33" in calls["comment"]


async def test_check_step_waits_attempt_free_until_conditions_hold(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """While the conditions are false and the deadline has not passed, ticks consume nothing:
    no new rows, no failure events — the same wait-not-fail semantics agent preconditions
    have. The SAME attempt then completes once CI goes green."""
    world = await _build_world(maker, config=_process_with_check("proc-check-wait"))

    await run_tick(world)
    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert run.attempt == 1
    assert await event_types(world) == ["step_started"]

    _deliver_green_impl(world)
    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert run.attempt == 1
    ticket = await ticket_row(world)
    assert ticket.current_step == "done"


async def test_check_step_times_out_retries_then_escalates_naming_the_condition(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Conditions that never hold end at the deadline, not silently: the attempt is TIMEOUT,
    the failing leaves are the failure reason, and exhaustion walks the same retry/escalate
    ladder every other kind uses — with the escalation comment naming the predicate."""
    world = await _build_world(
        maker, config=_process_with_check("proc-check-timeout"), assignees=("bot-user",)
    )

    await run_tick(world)  # attempt 1 opens
    world.clock.advance(timedelta(minutes=31))  # past the step's 30m timeout
    await run_tick(world)  # attempt 1 times out -> retry scheduled

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.TIMEOUT
    types = await event_types(world)
    assert "postcondition_failed" in types
    assert "check_failed" in types
    assert "retry_scheduled" in types
    failed = next(row for row in await event_rows(world) if row.type == "check_failed")
    assert failed.details.get("reason") == "timeout"
    assert "ci_green" in (failed.details.get("failure_reason") or "")

    world.clock.advance(timedelta(minutes=11))  # past the 10m backoff
    await run_tick(world)  # attempt 2 opens
    runs = await step_runs(world)
    assert [run.attempt for run in runs] == [1, 2]
    assert runs[-1].status == StepRunStatus.RUNNING

    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)  # attempt 2 times out -> escalate (2 == maxAttempts)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "escalated" in await event_types(world)
    calls = dict(world.projector.calls)
    assert "factorial:blocked" in calls["set_labels"]
    assert calls["set_assignees"] == ("operator",)
    assert "ci_green" in calls["comment"]


async def test_a_timed_out_check_escalates_with_its_summary_in_words(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`{failure_summary}` is the plain sentence behind `{failure_reason}`'s rule code: the
    comment a person reads on the issue names what is missing, never the predicate. The template
    ends the sentence, so the summary's own period is not doubled."""
    content = _process_with_check("proc-check-summary").model_dump(mode="json", by_alias=True)
    content["defaults"]["onFailure"]["escalate"]["comment"] = (
        "Factorial stopped at {step}: {failure_summary}."
    )
    world = await _build_world(
        maker, config=ProcessConfig.model_validate(content), assignees=("bot-user",)
    )

    await run_tick(world)  # attempt 1 opens
    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)  # attempt 1 times out -> retry scheduled
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)  # attempt 2 opens
    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)  # attempt 2 times out -> escalate

    summary = "Still waiting after 30 minutes for passing CI on a branch starting with impl/12-."
    escalated = next(row for row in await event_rows(world) if row.type == "escalated")
    assert escalated.details["failure_summary"] == summary
    assert "ci_green" in escalated.details["failure_reason"]
    assert dict(world.projector.calls)["comment"] == f"Factorial stopped at validate_ci: {summary}"


async def test_unblocking_a_check_step_books_the_attempt_it_grants(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The fourth arm reaches `_grant_attempt` like the others, and books the same way.

    `_grant_attempt` dispatches on whatever step the ticket was blocked on, so a check step gets the
    `next_attempt` + `due_at` pair `UNBLOCKED` carries for every non-gate kind. The due time is
    real here for the same reason it is on an agent step — `_check_step`'s FAILED branch gates
    its restart on `_retry_due`, which reads the timed-out run's own `ended_at + backoff` — and
    a check step has no `preconditions`, so nothing can null it out.
    """
    world = await _build_world(
        maker, config=_process_with_check("proc-check-unblock"), assignees=("bot-user",)
    )

    await run_tick(world)  # attempt 1 opens
    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)  # attempt 1 times out -> retry scheduled
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)  # attempt 2 opens
    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)  # attempt 2 times out -> escalate
    timed_out_at = world.clock.now  # `_fail_run` stamped `ended_at` with this
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED

    # Time passes before the human notices. The due time must not move with it: measured from the
    # failed run's end, not from the gesture, which is what `_retry_due` will read back.
    world.clock.advance(timedelta(minutes=4))
    _remove_blocked_label(world)
    await run_tick(world)

    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.details["attempt"] == 2
    assert unblocked.details["next_attempt"] == 3
    assert unblocked.details["due_at"] == (timed_out_at + timedelta(minutes=10)).isoformat()

    # Redeemed: the attempt the next tick opens is the one the un-block booked, so both share one
    # block on the ticket page.
    world.clock.advance(timedelta(minutes=7))
    await run_tick(world)
    runs = await step_runs(world)
    assert [run.attempt for run in runs] == [1, 2, 3]
    assert runs[-1].status == StepRunStatus.RUNNING


async def test_check_start_is_idempotent_under_concurrency(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same `UNIQUE (ticket_id, step_name, attempt)` that arbitrates agent and action
    starts arbitrates a check's — the loser's SAVEPOINT rolls back and only one row and one
    `step_started` survive.

    Both runners choose attempt 1 before either inserts. One that read the runs before the other
    committed and the next attempt after it would insert attempt 2, a key the constraint never
    sees; left to the scheduler that made this test flake on CI."""
    world = await _build_world(maker, config=_process_with_check("proc-check-idempotent"))
    both_chose = asyncio.Barrier(2)
    next_attempt = check_steps_module._next_attempt

    async def choose_then_wait(*args: Any, **kwargs: Any) -> int:
        attempt = await next_attempt(*args, **kwargs)
        await both_chose.wait()
        return attempt

    monkeypatch.setattr(check_steps_module, "_next_attempt", choose_then_wait)

    async def one() -> None:
        async with world.sessions() as session:
            ticket = await session.get(Ticket, world.ticket_id)
            assert ticket is not None
            await reconcile_ticket(
                session, ticket, world.deps, lease_owner=_LEASE_OWNER, lease_ttl=_LEASE_TTL
            )

    async with asyncio.timeout(30):
        await asyncio.gather(one(), one())

    runs = await step_runs(world)
    assert len(runs) == 1
    assert (await event_types(world)).count("step_started") == 1


async def test_unreadable_conditions_still_time_out(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A persistently erroring conditions read must not disable the timeout. The 2026-08-21
    incident shape — every check-runs read 403s — parks an agent step visibly RUNNING to be
    re-asked; a check step has no adapter timeout as a second net, so its own deadline must
    still fire: the entry tick anchors the row despite the failed first read, pre-deadline
    ticks say nothing, and the deadline ends the wait with "unreadable", not silence."""
    world = await _build_world(maker, config=_process_with_check("proc-check-unreadable"))
    # A branch the glob matches, so evaluation reaches the failing check-runs read at all —
    # with no matching branch, ci_green is False without ever asking.
    sha = "beefbeef" * 5
    world.github.branches.append(BranchState(name="impl/12-widget", sha=sha))
    world.github.pulls.append(_impl_pr(head_sha=sha))
    _fail_observation(world, "check_runs")

    await run_tick(world)  # entry: the wait is anchored despite the unreadable verdict
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert await event_types(world) == ["step_started"]

    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)  # pre-deadline: an unreadable verdict still says nothing
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING

    world.clock.advance(timedelta(minutes=26))  # past the step's 30m timeout
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.TIMEOUT
    assert "conditions unreadable" in (run.failure_reason or "")
    failed = next(row for row in await event_rows(world) if row.type == "check_failed")
    assert failed.details["failure_summary"] == (
        "Couldn't read GitHub for 30 minutes to check this step."
    )
    types = await event_types(world)
    assert "retry_scheduled" in types
    # No POSTCONDITION_FAILED: that event asserts the predicates evaluated false, and here
    # they evaluated nothing.
    assert "postcondition_failed" not in types


async def test_reentered_check_step_starts_a_fresh_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A ticket sitting on a check step it already completed — a gate's `rerunStep` edge —
    gets a new attempt with a new deadline, exactly as the other kinds treat rework; the old
    SUCCEEDED row is history, never re-read as current."""
    world = await _build_world(maker, config=_process_with_check("proc-check-rework"))
    _deliver_green_impl(world)
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"

    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(current_step="validate_ci")
        )

    world.clock.advance(timedelta(minutes=1))
    await run_tick(world)

    runs = await step_runs(world)
    assert [run.attempt for run in runs] == [1, 2]
    # Conditions still hold, so the fresh attempt completes on its own entry tick.
    assert runs[-1].status == StepRunStatus.SUCCEEDED
