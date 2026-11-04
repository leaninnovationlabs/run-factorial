"""Force complete (decision 78): a person asks the loop to check a step's postconditions now, and
the loop accepts the step when they hold or refuses with every failing leaf. Having read those
failures, the person may continue with warning, and the step is accepted as forced.

Every failure here is silent. A request a parked ticket never reads waits for ever behind the
park; an accepted run whose retry still fires spawns a second agent over finished work; a refusal
that lists one leaf of an `all` lets a later override pass leaves nobody saw; a check that stops
a session mid-turn throws away the turn a person is watching; and a force over a rework that has
not moved reopens a gate on the commit it rejected.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github import BLOCKED_LABELS, LABEL_INTERRUPTED
from orchestrator.worker.completions import COMPLETION_UNREADABLE_FOR
from runtime.contract import AgentRuntimeError, IdleReason, RunStatus
from tests.reconcile_world import (
    BOT_LOGIN,
    World,
    _build_world,
    _escalate_the_plan_step,
    _fail_observation,
    _process,
    _process_with_check,
    close_issue,
    event_rows,
    exited_via_label,
    interactive_world,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _three_postconditions(name: str) -> ProcessConfig:
    """`_process()` with plan's postconditions extended by a third leaf, so a short-circuit
    `all` would hide two of them."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["postconditions"]["all"].append('ticket_label("factorial:spec-ready")')
    return ProcessConfig.model_validate(cfg)


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def _ask(
    world: World, run: StepRun, *, allow_unmet: bool = False, seen: Sequence[str] = ()
) -> int:
    async with world.sessions() as session:
        row = await append(
            session,
            type=EventType.COMPLETION_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            allow_unmet=allow_unmet,
            seen_failures=list(seen),
            requested_by="op@example.com",
            requested_via="dashboard",
        )
        await session.commit()
        return row.id


async def _answers(world: World, request_id: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        (r.type, dict(r.details))
        for r in await event_rows(world)
        if r.details.get("request_id") == request_id
    ]


async def _failed_once(world: World) -> StepRun:
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="gave up")
    await run_tick(world)  # retry scheduled, ticket RUNNING
    (failed,) = await step_runs(world)
    return failed


async def test_check_now_accepts_a_failed_run_whose_postconditions_hold_and_cancels_its_retry(
    world: World,
) -> None:
    failed = await _failed_once(world)
    world.github.deliver_spec(world.issue_number)
    request_id = await _ask(world, failed)

    await run_tick(world)

    (run,) = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert (run.status, run.failure_reason) == (StepRunStatus.SUCCEEDED, None)
    assert (await ticket_row(world)).current_step == "spec_gate"
    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.STEP_ACCEPTED_ON_REQUEST and details["from_status"] == "failed"
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    assert len(world.adapter.spawns) == 1  # the ladder never fires on an accepted run


async def test_check_now_refuses_with_every_failure_and_changes_nothing(world: World) -> None:
    failed = await _failed_once(world)
    request_id = await _ask(world, failed)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED
    assert details["reason"] == "unmet" and details["overridable"] is True
    # The full evaluation: a short-circuit `all` would list `branch_exists` alone.
    assert len(details["failures"]) == 2
    assert "branch_exists" in details["failures"][0] and "pr_open" in details["failures"][1]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert (await ticket_row(world)).current_step == "plan"
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    assert len(world.adapter.spawns) == 2


async def test_check_now_releases_an_escalated_ticket_whose_postconditions_hold(
    world: World,
) -> None:
    await _escalate_the_plan_step(world)  # BLOCKED under factorial:blocked
    attempt_2 = (await step_runs(world))[-1]
    world.github.deliver_spec(world.issue_number)

    await _ask(world, attempt_2)
    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.status, ticket.current_step) == (TicketStatus.RUNNING, "spec_gate")
    assert not world.github.issue.labels & BLOCKED_LABELS
    assert len(world.adapter.spawns) == 2


async def test_check_now_accepts_a_parked_session_without_resuming_or_minting(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    (run,) = await step_runs(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            requested_via="dashboard",
        )
        await session.commit()
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    world.github.deliver_spec(world.issue_number)

    await _ask(world, run)
    await run_tick(world)

    assert adapter.resumed == [] and adapter.finish_calls == ["run-1"] and len(world.minted) == 1
    (completed,) = [
        r.details for r in await event_rows(world) if r.type == EventType.AGENT_COMPLETED
    ]
    assert completed["accepted_on_request"] == "op@example.com"
    assert LABEL_INTERRUPTED not in world.github.issue.labels
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_check_now_stops_an_idle_session_once_and_accepts_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="?")
    await run_tick(world)  # IDLE, unmet
    world.github.deliver_spec(world.issue_number)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    await _ask(world, run)
    await run_tick(world)

    assert adapter.interrupted == ["run-1"] and adapter.finish_calls == ["run-1"]
    assert (await step_runs(world))[0].status == StepRunStatus.SUCCEEDED


async def test_check_now_refuses_an_agent_mid_turn_and_touches_nothing(world: World) -> None:
    run = await running_run(world)
    world.github.deliver_spec(world.issue_number)
    request_id = await _ask(world, run)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED
    assert (details["reason"], details["overridable"]) == ("mid_turn", False)
    assert world.adapter.interrupted == []
    assert (await step_runs(world))[0].status == StepRunStatus.RUNNING


async def test_check_now_on_a_superseded_attempt_is_refused_as_stale(world: World) -> None:
    failed = await _failed_once(world)
    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)
    assert len(world.adapter.spawns) == 2
    request_id = await _ask(world, failed)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED
    assert (details["reason"], details["overridable"]) == ("stale", False)


async def test_an_unreadable_verdict_stays_pending_then_refuses(world: World) -> None:
    failed = await _failed_once(world)
    _fail_observation(world, "branches", "pull_requests")
    request_id = await _ask(world, failed)

    await run_tick(world)
    assert await _answers(world, request_id) == []

    world.clock.advance(COMPLETION_UNREADABLE_FOR)
    await run_tick(world)
    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED
    assert (details["reason"], details["overridable"]) == ("unavailable", False)


async def test_an_exit_answers_a_pending_check_now(world: World) -> None:
    failed = await _failed_once(world)
    request_id = await _ask(world, failed)
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED and details["reason"] == "exited"


async def _parked(maker: async_sessionmaker[AsyncSession]) -> tuple[World, Any, StepRun]:
    """An interactive world whose run a person interrupted: INTERRUPTED, ticket BLOCKED."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    (run,) = await step_runs(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            requested_via="dashboard",
        )
        await session.commit()
    await run_tick(world)
    return world, adapter, run


async def test_check_now_on_a_parked_run_whose_runtime_is_gone_is_refused_not_raised(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Raised, the tick fails on every pass until decision 31 quarantines the ticket, because a
    person pressed a button."""
    world, _, run = await _parked(maker)
    async with world.sessions() as session:
        await session.execute(update(StepRun).where(StepRun.id == run.id).values(runtime_id=None))
        await session.commit()
    world.github.deliver_spec(world.issue_number)
    request_id = await _ask(world, run)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED
    assert (details["reason"], details["overridable"]) == ("unavailable", False)
    ticket = await ticket_row(world)
    assert (ticket.status, ticket.quarantined_at) == (TicketStatus.BLOCKED, None)
    assert (await step_runs(world))[0].status == StepRunStatus.INTERRUPTED


async def test_an_idle_session_whose_result_is_lost_after_the_stop_is_parked_as_a_persons_stop(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Left IDLE over a stopped session, the same tick's poll would resume it with a fresh
    token: the opposite of what the person asked for."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="?")
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    (run,) = await step_runs(world)
    adapter.result_error = AgentRuntimeError("the session's log is gone")
    request_id = await _ask(world, run)

    await run_tick(world)
    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED and details["reason"] == "unavailable"
    assert (await step_runs(world))[0].status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert adapter.interrupted == ["run-1"]
    assert adapter.resumed == [] and len(adapter.spawns) == 1


async def test_check_now_on_a_session_that_ended_by_itself_is_refused_as_stale(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="?")
    await run_tick(world)
    (run,) = await step_runs(world)
    adapter.complete(RunStatus.FAILED, failure_reason="the CLI exited")
    request_id = await _ask(world, run)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED and details["reason"] == "stale"


async def test_a_check_now_racing_an_exit_is_answered_while_the_ticket_stays_exited(
    world: World,
) -> None:
    """The exited park reads nothing else, so the dialog would wait on it for ever."""
    await exited_via_label(world)
    (run,) = await step_runs(world)
    request_id = await _ask(world, run)

    await run_tick(world)

    ((kind, details),) = await _answers(world, request_id)
    assert kind == EventType.COMPLETION_REFUSED and details["reason"] == "exited"
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert EventType.TICKET_REENTERED not in [r.type for r in await event_rows(world)]


# ------------------------------------------------------------------- continue with warning


async def test_continue_with_warning_forces_the_step_on_the_record(world: World) -> None:
    failed = await _failed_once(world)
    first = await _ask(world, failed)
    await run_tick(world)
    ((_, refused),) = await _answers(world, first)
    seen = refused["failures"]
    assert refused["reason"] == "unmet" and refused["overridable"] is True

    forced = await _ask(world, failed, allow_unmet=True, seen=seen)
    await run_tick(world)

    ((kind, details),) = await _answers(world, forced)
    assert kind == EventType.STEP_FORCED and details["failures"] == seen
    run = next(r for r in await step_runs(world) if r.id == failed.id)
    assert (run.status, run.forced_by) == (StepRunStatus.SUCCEEDED, "op@example.com")
    (comment,) = [c for c in _comments(world) if "forced complete by op@example.com" in c]
    # What was missing, in words: the rule code is the event's.
    assert "still missing:\n- " in comment and "= false" not in comment
    await run_tick(world)  # the gate opens with no pull request
    await run_tick(world)  # and waits
    waiting = [r.details for r in await event_rows(world) if r.type == EventType.NEXT_STEP_WAITING]
    assert len(waiting) == 1
    assert (waiting[0]["step"], waiting[0]["from_step"]) == ("spec_gate", "plan")
    assert waiting[0]["forced_by"] == "op@example.com"


async def test_a_force_over_a_failure_the_person_did_not_see_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_three_postconditions("proc-three"))
    failed = await _failed_once(world)
    first = await _ask(world, failed)
    await run_tick(world)
    ((_, refused),) = await _answers(world, first)
    assert [f.split("(")[0] for f in refused["failures"]] == [
        "branch_exists",
        "pr_open",
        "ticket_label",
    ]

    forced = await _ask(world, failed, allow_unmet=True, seen=refused["failures"][:1])
    await run_tick(world)

    ((kind, details),) = await _answers(world, forced)
    assert (kind, details["reason"], details["overridable"]) == (
        EventType.COMPLETION_REFUSED,
        "changed",
        True,
    )
    run = next(r for r in await step_runs(world) if r.id == failed.id)
    assert (run.status, run.forced_by) == (StepRunStatus.FAILED, None)


async def test_an_unmoved_rejection_is_never_forced(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Forced, the rework would reopen its gate on the very commit the gate rejected."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # accept -> spec_gate
    await run_tick(world)  # the gate opens
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects -> plan
    adapter.restart()
    await run_tick(world)  # the rework spawns (attempt 2)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    rework = (await step_runs(world))[-1]
    assert (rework.step_name, rework.status) == ("plan", StepRunStatus.IDLE)
    unmoved = "the pull request #101 still ends at sha-1, the commit the gate rejected"

    check = await _ask(world, rework)
    await run_tick(world)
    ((_, checked),) = await _answers(world, check)
    assert (checked["reason"], checked["overridable"]) == ("unmoved", False)
    assert checked["failures"] == [unmoved]
    # A request the dashboard would never send, since it offers no override here.
    request = await _ask(world, rework, allow_unmet=True, seen=checked["failures"])
    await run_tick(world)

    ((_, details),) = await _answers(world, request)
    assert (details["reason"], details["overridable"]) == ("unmoved", False)
    assert adapter.interrupted == []
    assert (await step_runs(world))[-1].status == StepRunStatus.IDLE


async def test_a_waiting_check_step_can_be_forced_past_its_conditions(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_check("proc-force-check"))
    await run_tick(world)
    (waiting,) = await step_runs(world)
    assert waiting.status == StepRunStatus.RUNNING
    first = await _ask(world, waiting)
    await run_tick(world)
    ((_, refused),) = await _answers(world, first)
    assert refused["reason"] == "unmet"

    forced = await _ask(world, waiting, allow_unmet=True, seen=refused["failures"])
    await run_tick(world)

    ((kind, _),) = await _answers(world, forced)
    assert kind == EventType.STEP_FORCED
    (run,) = await step_runs(world)
    assert (run.status, run.forced_by) == (StepRunStatus.SUCCEEDED, "op@example.com")
    assert (await ticket_row(world)).current_step == "done"
