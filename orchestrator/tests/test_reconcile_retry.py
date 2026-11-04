"""A person's Retry on a failed step starts its next attempt now and continues the failed
attempt's session where it can, or starts fresh, told what the failed one delivered.

Every failure here is silent. A continued session that is really fresh pays again for what the
failed attempt already knew, and nothing on the ticket says so. A press that grants two attempts
spends a budget nobody approved. A press on a ticket blocked for another reason that is honoured,
or never answered, wedges the ticket behind a request the loop keeps reading.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.worker.completions import COMPLETION_UNREADABLE_FOR
from runtime.contract import (
    AdapterUnavailable,
    Artifact,
    ArtifactKind,
    NoBranchHeld,
    ResumeFrom,
    RunSpec,
    RunStatus,
    SessionUnavailable,
)
from tests.reconcile_world import (
    World,
    _build_world,
    _escalate_the_plan_step,
    _fail_observation,
    _process,
    _process_with_check,
    _process_without_retry,
    event_rows,
    event_types,
    exited_via_label,
    interactive_world,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _details(world: World, event_type: EventType) -> list[dict[str, Any]]:
    return [dict(row.details) for row in await event_rows(world) if row.type == event_type]


async def _plan_runs(world: World) -> list[StepRun]:
    return [run for run in await step_runs(world) if run.step_name == "plan"]


async def _press(world: World, run: StepRun) -> None:
    """What `POST /step-runs/{id}/retry-step` records, written directly: this is the loop's
    half."""
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.STEP_RETRY_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            next_attempt=run.attempt + 1,
            requested_by="operator@example.com",
            requested_via="dashboard",
        )


def _mint_as(
    token: str, minted: list[tuple[str, Mapping[str, str]]]
) -> Callable[[str, Mapping[str, str]], Awaitable[str]]:
    async def mint(repo: str, permissions: Mapping[str, str]) -> str:
        minted.append((repo, dict(permissions)))
        return token

    return mint


def _names(spec: RunSpec) -> set[str]:
    return {item.name for item in spec.inputs}


async def _failed_once(world: World, **result: Any) -> StepRun:
    """plan attempt 1 fails; its retry waits out the 10-minute backoff."""
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up", **result)
    await run_tick(world)
    world.adapter.restart()
    (failed,) = await _plan_runs(world)
    return failed


async def _refusals(world: World) -> list[dict[str, Any]]:
    return await _details(world, EventType.STEP_RETRY_REFUSED)


async def test_a_retry_pressed_on_an_escalated_step_continues_the_failed_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    await _escalate_the_plan_step(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    _, attempt_2 = await _plan_runs(world)
    await _press(world, attempt_2)
    minted_before = len(world.minted)
    world.mint_with(_mint_as("ghs_retry", world.minted))
    adapter.restart()

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    third = adapter.spawn_attempts[-1]
    assert len(world.minted) == minted_before + 1
    assert third.secrets["GH_TOKEN"].get_secret_value() == "ghs_retry"
    assert third.resume_from == ResumeFrom(
        run_id="run-2", session_id="session-run-2", mode="retry"
    )
    briefing = {item.name: item.value for item in third.inputs}
    assert "agent gave up again" in briefing["previous_attempt"]
    assert "previous_attempt_delivered" not in briefing
    spawned = (await _details(world, EventType.AGENT_SPAWNED))[-1]
    assert spawned["resumed_from"] == {
        "step_run_id": attempt_2.id,
        "run_id": "run-2",
        "attempt": 2,
    }
    assert EventType.SESSION_RESUME_SKIPPED not in await event_types(world)

    adapter.complete(RunStatus.FAILED, failure_reason="still broken")
    await run_tick(world)
    assert (await event_types(world))[-2:] == ["agent_failed", "escalated"]


async def test_a_retry_pressed_during_the_backoff_takes_the_booked_attempt_and_the_ladder_stays_fresh(  # noqa: E501
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(
        maker, plan={"retry": {"maxAttempts": 3, "backoff": "10m"}}
    )
    first = await _failed_once(world)
    started_at = world.clock()
    await _press(world, first)

    await run_tick(world)

    assert world.clock() == started_at
    second = adapter.spawn_attempts[-1]
    assert second.resume_from is not None and second.resume_from.mode == "retry"
    assert [run.attempt for run in await _plan_runs(world)] == [1, 2]

    adapter.complete(RunStatus.FAILED, failure_reason="again")
    await run_tick(world)
    assert (await event_types(world))[-1] == "retry_scheduled"

    world.clock.advance(timedelta(minutes=11))
    adapter.restart()
    await run_tick(world)
    assert len(adapter.spawn_attempts) == 3
    assert adapter.spawn_attempts[-1].resume_from is None


async def test_a_retry_pressed_with_no_retry_policy_grants_exactly_one_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_without_retry("proc-retry-nopolicy"))
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    (first,) = await _plan_runs(world)
    await _press(world, first)
    world.adapter.restart()

    await run_tick(world)

    assert [(run.attempt, run.status) for run in await _plan_runs(world)] == [
        (1, StepRunStatus.FAILED),
        (2, StepRunStatus.RUNNING),
    ]
    world.adapter.complete(RunStatus.FAILED, failure_reason="again")
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert "retry_scheduled" not in await event_types(world)


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (NoBranchHeld("held no branch"), "no_branch"),
        (SessionUnavailable("reaped"), "not_kept"),
    ],
)
async def test_a_retry_that_cannot_continue_starts_fresh_with_the_delivered_work_and_says_why(
    maker: async_sessionmaker[AsyncSession], exc: Exception, reason: str
) -> None:
    world, adapter = await interactive_world(maker)
    first = await _failed_once(
        world,
        artifacts=(
            Artifact(
                kind=ArtifactKind.BRANCH,
                locator={"branch": f"spec/{world.issue_number}-widget"},
            ),
        ),
    )
    adapter.spawn_error_for_resume = exc
    await _press(world, first)

    await run_tick(world)

    _, refused, fresh = adapter.spawn_attempts
    assert refused.resume_from is not None
    assert fresh.resume_from is None
    (skipped,) = await _details(world, EventType.SESSION_RESUME_SKIPPED)
    assert skipped["reason"] == reason
    assert "previous_attempt_delivered" in _names(fresh)
    assert "previous_attempt_delivered" not in _names(refused)
    delivered = {item.name: item.value for item in fresh.inputs}["previous_attempt_delivered"]
    assert f"spec/{world.issue_number}-widget" in delivered


async def test_a_retry_pressed_on_a_check_step_starts_the_next_attempt_now(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_check("proc-retry-check"))
    await run_tick(world)
    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)
    (timed_out,) = await step_runs(world)
    assert timed_out.status == StepRunStatus.TIMEOUT
    await _press(world, timed_out)

    await run_tick(world)

    runs = await step_runs(world)
    assert [run.attempt for run in runs] == [1, 2]
    assert runs[-1].status == StepRunStatus.RUNNING


async def test_a_retry_request_the_ticket_moved_past_is_refused_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    first = await _failed_once(world)
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    assert len(adapter.spawns) == 2
    await _press(world, first)

    await run_tick(world)

    (refusal,) = await _refusals(world)
    assert refusal["reason"] == "stale"
    assert refusal["request_id"] is not None
    assert len(adapter.spawns) == 2
    await run_tick(world)
    assert len(await _refusals(world)) == 1


async def test_a_retry_pressed_on_a_ticket_a_refused_spawn_blocked_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    first = await _failed_once(world)
    adapter.refusal = "the build is not verified"
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert (await event_types(world))[-1] == "spawn_refused"
    await _press(world, first)
    world.projector.calls.clear()

    await run_tick(world)

    (refusal,) = await _refusals(world)
    assert refusal["reason"] == "not_escalated"
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert world.projector.calls == []
    assert "factorial:blocked" in world.github.issue.labels


async def test_a_retry_racing_an_exit_is_refused_once_while_the_ticket_stays_exited(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The exited park reads nothing else, so an unanswered press would wait there, and be
    honoured once the ticket re-entered."""
    world = await _build_world(maker)
    await exited_via_label(world)
    (run,) = await step_runs(world)
    await _press(world, run)

    await run_tick(world)
    await run_tick(world)

    (refusal,) = await _refusals(world)
    assert refusal["reason"] == "off_the_floor"
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert EventType.TICKET_REENTERED not in await event_types(world)


async def test_a_retry_whose_spawn_the_substrate_fails_is_refused_and_leaves_the_ticket_blocked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Raised out of the tick, the press would fail every pass until the ticket is
    quarantined."""
    world, adapter = await interactive_world(maker)
    await _escalate_the_plan_step(world)
    _, attempt_2 = await _plan_runs(world)
    adapter.fail_spawn(AdapterUnavailable("the substrate is down"))
    adapter.restart()
    await _press(world, attempt_2)

    await run_tick(world)
    await run_tick(world)

    (refusal,) = await _refusals(world)
    assert refusal["reason"] == "spawn_failed"
    assert "the substrate is down" in refusal["detail"]
    assert [run.attempt for run in await _plan_runs(world)] == [1, 2]
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


async def test_a_retry_whose_preconditions_stay_unreadable_is_refused_after_the_bound(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Pending, the press holds the act phase every tick, the ladder's own retry included, for
    as long as GitHub cannot be read."""
    cfg = _process("proc-retry-unreadable").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["preconditions"] = {"not": 'branch_exists("hold/{ticket}-*")'}
    world = await _build_world(maker, config=ProcessConfig.model_validate(cfg))
    await _escalate_the_plan_step(world)
    _, attempt_2 = await _plan_runs(world)
    _fail_observation(world, "branches")
    await _press(world, attempt_2)

    await run_tick(world)
    assert await _refusals(world) == []

    world.clock.advance(COMPLETION_UNREADABLE_FOR)
    await run_tick(world)
    await run_tick(world)

    (refusal,) = await _refusals(world)
    assert refusal["reason"] == "unavailable"
    assert [run.attempt for run in await _plan_runs(world)] == [1, 2]
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


async def _postconditions_unmet(world: World) -> StepRun:
    """plan attempt 1 reports success without delivering the spec, so its checks fail."""
    await run_tick(world)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)
    world.adapter.restart()
    (failed,) = await _plan_runs(world)
    assert failed.status == StepRunStatus.FAILED
    return failed


async def _no_reason(world: World) -> StepRun:
    failed = await _failed_once(world)
    async with world.sessions() as session:
        await session.execute(
            update(StepRun).where(StepRun.id == failed.id).values(failure_reason=None)
        )
    return failed


@pytest.mark.parametrize("fail", [_postconditions_unmet, _no_reason])
async def test_a_retry_briefing_says_each_failure_once_and_never_none(
    maker: async_sessionmaker[AsyncSession],
    fail: Callable[[World], Awaitable[StepRun]],
) -> None:
    world, adapter = await interactive_world(maker)
    failed = await fail(world)
    await _press(world, failed)

    await run_tick(world)

    briefing = {item.name: item.value for item in adapter.spawn_attempts[-1].inputs}
    text = briefing["previous_attempt"]
    assert "None" not in text
    if fail is _no_reason:
        assert text == "Attempt 1 ended failed."
    else:
        (unmet,) = await _details(world, EventType.POSTCONDITION_FAILED)
        assert unmet["failures"]
        for leaf in unmet["failures"]:
            assert text.count(leaf) == 1
