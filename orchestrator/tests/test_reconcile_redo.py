"""Redo (decision 76): a person throws away one step and every step after it, and the loop starts
that step again from a clean start on the pinned config.

What would regress silently: a redo that left a session parked would let a Resume continue work
the person threw away; one that adopted the old session would hand the "fresh" agent everything
it was asked to forget; one that closed a pull request a kept step still needs would break the
ticket further back than the person asked; and one that left a request it overtook unanswered
would leave the dashboard reading `pending` for ever."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.previews import change_request_outcome
from orchestrator.github import LABEL_EXIT, LABEL_INTERRUPTED
from orchestrator.github.projector import step_label
from orchestrator.github.state import PullRequestState
from orchestrator.worker.completions import completion_outcome, pending_completion_request
from orchestrator.worker.retries import pending_step_retry
from runtime.contract import AgentRuntimeError, RunInput, RunStatus
from tests.reconcile_world import (
    FakeAdapter,
    World,
    _build_world,
    add_label,
    advance_to_gate,
    change_request_inputs,
    event_rows,
    exited_via_label,
    remove_label,
    request_changes,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def ask_redo(
    world: World, target: str, *, step: str, attempt: int | None, note: str | None = None
) -> int:
    """What the API records. Written directly so this module tests the LOOP's half."""
    note_field: dict[str, Any] = {} if note is None else {"note": note}
    async with world.sessions() as session:
        row = await append(
            session,
            type=EventType.REDO_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            requested_by="ops@example.com",
            requested_via="dashboard",
            target_step=target,
            has_note=note is not None,
            **note_field,
            step=step,
            attempt=attempt,
            generation=1,
        )
        await session.commit()
        return row.id


async def answers(world: World) -> list[tuple[str, dict[str, Any]]]:
    return [
        (e.type, dict(e.details))
        for e in await event_rows(world)
        if e.type in (EventType.STEP_REDONE, EventType.REDO_REFUSED)
    ]


class _UnstoppableAdapter(FakeAdapter):
    async def cancel(self, run_id: str) -> None:
        raise AgentRuntimeError("no such run")


async def _outcome(world: World, request_id: int) -> tuple[str, str | None]:
    async with world.sessions() as session:
        return await change_request_outcome(session, world.ticket_id, request_id)


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def test_a_redo_of_the_running_step_cancels_it_and_starts_it_fresh_with_a_full_budget(
    world: World,
) -> None:
    run = await running_run(world)
    await ask_redo(world, "plan", step="plan", attempt=1)

    await run_tick(world)

    assert world.adapter.cancelled == [run.runtime_run_id]
    (old,) = await step_runs(world)
    assert old.status == StepRunStatus.CANCELLED and old.superseded_at is not None
    [(kind, d)] = await answers(world)
    assert kind == EventType.STEP_REDONE
    assert d["superseded_runs"] == [{"step": "plan", "attempt": 1}]
    assert d["cancelled"] == {"step": "plan", "attempt": 1}
    assert d["requested_by"] == "ops@example.com"

    world.adapter.restart()
    await run_tick(world)

    fresh = (await step_runs(world))[-1]
    assert (fresh.attempt, fresh.entered_by) == (2, "redo")
    assert world.adapter.spawns[-1].resume_from is None

    world.adapter.complete(RunStatus.FAILED, failure_reason="no")
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.RUNNING  # retried, not escalated


async def test_a_redo_from_a_waiting_gate_discards_the_reviewed_step_and_reopens_the_gate(
    world: World,
) -> None:
    await advance_to_gate(world)
    world.github.pulls.append(
        PullRequestState(
            number=102,
            head_ref="spec/12-old",
            base_ref="main",
            head_sha="sha-old",
            state="closed",
            merged=True,
            url="https://github.test/pr/102",
        )
    )
    await ask_redo(world, "plan", step="spec_gate", attempt=1)

    await run_tick(world)

    calls = world.contract_github.calls
    assert ("close_pull_request", 101) in calls and ("delete_branch", "spec/12-widget") in calls
    assert ("close_pull_request", 102) not in calls
    [(_, d)] = await answers(world)
    assert d["merged_prs_left"] == [102]
    ticket = await ticket_row(world)
    assert (ticket.current_step, ticket.status) == ("plan", TicketStatus.RUNNING)
    assert step_label("plan") in world.github.issue.labels
    assert all(r.superseded_at is not None for r in await step_runs(world))

    world.adapter.restart()
    await run_tick(world)
    assert world.adapter.spawns[-1].resume_from is None


async def test_a_redo_at_a_gate_answers_its_pending_change_request(world: World) -> None:
    """Section F case 5: unanswered, the request reads `pending` for ever."""
    await advance_to_gate(world)
    request_id = await request_changes(world)
    await ask_redo(world, "plan", step="spec_gate", attempt=1)

    await run_tick(world)

    assert await _outcome(world, request_id) == ("refused", "redone")
    world.adapter.restart()
    await run_tick(world)
    assert change_request_inputs(world.adapter.spawns[-1]) == []


async def test_a_redo_ends_a_parked_session_so_nothing_resumes_it(world: World) -> None:
    run = await running_run(world)
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
    assert LABEL_INTERRUPTED in world.github.issue.labels

    await ask_redo(world, "plan", step="plan", attempt=1)
    await run_tick(world)

    assert world.adapter.finish_calls == [run.runtime_run_id]
    assert LABEL_INTERRUPTED not in world.github.issue.labels
    [(kind, d)] = await answers(world)
    assert kind == EventType.STEP_REDONE and d["ended_parked"] == {"step": "plan", "attempt": 1}

    world.adapter.restart()
    await run_tick(world)
    assert world.adapter.resumed == []


async def test_a_redo_that_cannot_stop_the_run_is_refused_and_changes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, adapter_cls=_UnstoppableAdapter)
    await running_run(world)
    await ask_redo(world, "plan", step="plan", attempt=1)

    await run_tick(world)

    [(kind, d)] = await answers(world)
    assert (kind, d["reason"]) == (EventType.REDO_REFUSED, "cancel_failed")
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING and row.superseded_at is None
    assert world.contract_github.calls == []


async def test_a_redo_the_ticket_moved_past_is_refused(world: World) -> None:
    await advance_to_gate(world)  # plan delivered and accepted; the ticket waits at spec_gate
    await ask_redo(world, "plan", step="plan", attempt=1)

    await run_tick(world)

    [(kind, d)] = await answers(world)
    assert (kind, d["reason"]) == (EventType.REDO_REFUSED, "stale")
    assert all(r.superseded_at is None for r in await step_runs(world))
    assert world.contract_github.calls == []


@pytest.mark.parametrize("asked", [EventType.STEP_RETRY_REQUESTED, EventType.COMPLETION_REQUESTED])
async def test_a_retry_pending_when_a_redo_lands_is_refused_and_the_redone_step_starts(
    world: World, asked: EventType
) -> None:
    """Section F case 1. Plan attempt 1 fails and waits out its backoff; a Retry or a Force
    complete is pressed on it, then the redo lands before the loop reads either."""
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)
    (failed,) = await step_runs(world)
    async with world.sessions() as session:
        request = await append(
            session,
            type=asked,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=failed.id,
            step=failed.step_name,
            attempt=failed.attempt,
            next_attempt=failed.attempt + 1,
            allow_unmet=False,
            seen_failures=[],
            requested_by="operator@example.com",
            requested_via="dashboard",
        )
        await session.commit()
        request_id = request.id
    await ask_redo(world, "plan", step="plan", attempt=1)
    world.adapter.restart()

    await run_tick(world)
    await run_tick(world)

    async with world.sessions() as session:
        assert await pending_step_retry(session, world.ticket_id) is None
        assert await pending_completion_request(session, world.ticket_id) is None
        if asked == EventType.COMPLETION_REQUESTED:
            answer = await completion_outcome(session, world.ticket_id, request_id)
            assert (answer.outcome, answer.reason) == ("refused", "redone")
    kinds = [e.type for e in await event_rows(world)]
    assert EventType.STEP_RETRY_REFUSED not in kinds  # the redo alone answers a retry
    fresh = (await step_runs(world))[-1]
    assert (fresh.step_name, fresh.attempt, fresh.entered_by) == ("plan", 2, "redo")


async def test_an_exit_on_the_same_tick_wins_and_refuses_the_redo(world: World) -> None:
    await running_run(world)
    await ask_redo(world, "plan", step="plan", attempt=1)
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [(kind, d)] = await answers(world)
    assert (kind, d["reason"]) == (EventType.REDO_REFUSED, "exited")
    assert all(r.superseded_at is None for r in await step_runs(world))


async def test_a_redo_asked_of_an_exited_ticket_is_answered(world: World) -> None:
    await exited_via_label(world)
    await ask_redo(world, "plan", step="plan", attempt=1)

    await run_tick(world)

    [(kind, d)] = await answers(world)
    assert (kind, d["reason"]) == (EventType.REDO_REFUSED, "exited")
    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_a_refused_redo_does_not_undo_a_re_entry_asked_on_the_same_tick(
    world: World,
) -> None:
    """Projecting the refusal first would put back the `factorial:exit` a person removed."""
    await exited_via_label(world)
    await ask_redo(world, "plan", step="plan", attempt=1)
    remove_label(world, LABEL_EXIT)

    await run_tick(world)

    [(kind, d)] = await answers(world)
    assert (kind, d["reason"]) == (EventType.REDO_REFUSED, "exited")
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert LABEL_EXIT not in world.github.issue.labels


async def test_the_redo_note_reaches_the_fresh_attempt_as_an_input_and_nowhere_else(
    world: World,
) -> None:
    await running_run(world)
    await ask_redo(world, "plan", step="plan", attempt=1, note="use the v2 API")

    await run_tick(world)
    world.adapter.restart()
    await run_tick(world)

    assert RunInput(name="redo_note", value="use the v2 API") in world.adapter.spawns[-1].inputs
    assert all("v2 API" not in comment for comment in _comments(world))
    [(_, d)] = await answers(world)
    assert "note" not in d and d["has_note"] is True
