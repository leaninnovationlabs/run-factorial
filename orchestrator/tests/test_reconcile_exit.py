"""Exiting a ticket: the three ways in, and what the loop does to the work in flight.

The behaviours here are the ones that would regress silently. An exit that left an agent
running would look like it worked — the label lands either way — while a container kept
spending on a ticket nobody wants; an exit that cancelled an interrupted run would destroy a
workspace a human asked to keep; and a closed issue that did not exit is the ledger item this
state exists to settle (decision 29).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github import LABEL_EXIT, LABEL_INTERRUPTED
from orchestrator.worker.exits import _pending
from orchestrator.worker.runtime_adapters import RuntimeRefused
from runtime.contract import AgentRuntimeError
from tests.reconcile_world import (
    FakeAdapter,
    World,
    _build_world,
    _escalate_the_plan_step,
    _process_with_check,
    _process_without_gate_reject,
    _process_without_retry,
    add_label,
    advance_to_gate,
    close_issue,
    event_rows,
    event_types,
    exited_via_label,
    remove_label,
    request,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def reopen_issue(world: World) -> None:
    world.github.issue = replace(world.github.issue, state="open")


def comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def exited_events(world: World) -> list[dict[str, object]]:
    return [dict(e.details) for e in await event_rows(world) if e.type == EventType.TICKET_EXITED]


async def test_adding_the_label_takes_a_running_ticket_off_the_floor(world: World) -> None:
    """The whole point: the substrate is told to stop, the row says why, and the ticket leaves
    every open view because `closed_at` is set — the fact the read models filter on."""
    run = await running_run(world)
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.EXITED
    assert ticket.closed_at is not None
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.CANCELLED
    assert row.failure_reason == "cancelled: ticket exited"
    assert world.adapter.cancelled == [run.runtime_run_id]
    assert world.adapter.interrupted == [], "exit is a departure, not a pause"

    [exited] = await exited_events(world)
    assert exited["via"] == "label"
    assert exited["attempt"] == 1
    assert exited["cancelled"] is True

    labels = set(world.github.issue.labels)
    assert LABEL_EXIT in labels
    assert "factorial:plan" in labels, "the step label must survive beside the status label"
    [comment] = comments(world)
    assert "Left the process at `plan`" in comment
    assert "attempt 1" in comment


async def test_a_dashboard_request_exits_on_the_next_tick(world: World) -> None:
    await running_run(world)
    await request(world, EventType.EXIT_REQUESTED)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [exited] = await exited_events(world)
    assert exited["via"] == "dashboard"


async def test_closing_the_issue_exits_the_ticket(world: World) -> None:
    """Closing is observed input the loop acts on — the one place a human's GitHub gesture
    other than a label removal moves the machine (decision 29)."""
    await running_run(world)
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [exited] = await exited_events(world)
    assert exited["via"] == "issue_closed"
    [comment] = comments(world)
    assert "the issue was closed" in comment
    assert "reopen the issue" in comment


async def test_a_blocked_ticket_under_a_closed_issue_finally_leaves(world: World) -> None:
    """The ledger item: a parked ticket whose issue was closed stayed not-done for ever and was
    polled every tick, because the reconciler acted on labels only."""
    await _escalate_the_plan_step(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert world.adapter.cancelled == [], "nothing was running; nothing to cancel"


async def test_an_exit_files_under_the_attempt_it_leaves_behind(world: World) -> None:
    """The event's `attempt` is what the ticket page keys its block on. A blocked ticket's
    latest run is FAILED, not cancelled, so keying on the cancelled row alone would open an
    attempt-less block beside the one the reader is looking at."""
    await _escalate_the_plan_step(world)
    close_issue(world)

    await run_tick(world)

    [exited] = await exited_events(world)
    assert exited["attempt"] == 2, "the exhausted attempt, not null"
    assert exited["cancelled"] is False


async def _approve_and_advance_to_the_terminal_step(world: World) -> None:
    """Gate 2's own drive (`test_observed_approval_merges_and_advances`): approve, merge, and
    land on the terminal step with its tick still to run."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"


async def test_a_closing_issue_at_the_terminal_step_finishes_rather_than_exits(
    world: World,
) -> None:
    """The build agent writes `Closes #N`, so merging the spec PR closes the issue while the
    ticket still has its terminal tick to run. Exiting there would replace a completion with a
    departure in every cost and throughput read model."""
    await _approve_and_advance_to_the_terminal_step(world)
    close_issue(world)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.DONE
    assert not await exited_events(world)


async def test_the_label_still_exits_a_ticket_at_the_terminal_step(world: World) -> None:
    """The carve-out is for the side effect of merging, not for a human's gesture."""
    await _approve_and_advance_to_the_terminal_step(world)
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [exited] = await exited_events(world)
    assert exited["via"] == "label"


async def test_a_dashboard_press_still_exits_a_ticket_at_the_terminal_step(
    world: World,
) -> None:
    await _approve_and_advance_to_the_terminal_step(world)
    await request(world, EventType.EXIT_REQUESTED)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_a_gate_merged_between_polls_advances_instead_of_exiting(world: World) -> None:
    """Gate 2 is the designed path to main: a human merges the spec PR, and its `Closes #N`
    closes the issue before the next poll. The tick owes that gate its evaluation, not an exit;
    exiting here would turn every completion into a departure."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    world.github.merge_externally()
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status != TicketStatus.EXITED
    assert EventType.TICKET_EXITED not in await event_types(world)
    assert EventType.GATE_APPROVED in await event_types(world)


async def test_a_blocked_gate_under_a_closed_issue_still_exits(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The carve-out is on the WAITING status, not on gate steps: a dead-end rejection parks the
    ticket as blocked at the gate, and a closed issue there is the ledger's own case."""
    world = await _build_world(maker, config=_process_without_gate_reject("gate-dead-end"))
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected with nowhere to loop back to -> BLOCKED at the gate
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [exited] = await exited_events(world)
    assert exited["via"] == "issue_closed"


async def test_exit_cancels_a_waiting_gate_row_without_a_substrate(world: World) -> None:
    """A gate's WAITING row is the loop's own wait; it is marked cancelled so no in-flight view
    lists an exited ticket's gate, and re-entry opens a fresh one."""
    await advance_to_gate(world)
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    gate = [r for r in await step_runs(world) if r.step_name == "spec_gate"][-1]
    assert gate.status == StepRunStatus.CANCELLED
    assert world.adapter.cancelled == []
    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_exit_cancels_a_running_check_row_and_re_entry_restarts_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`kind: check` is the fourth arm, and its RUNNING row is the loop's own wait rather than a
    container: the exit marks it cancelled without reaching an adapter, and re-entry restarts it
    through the same failed-statuses path a timeout takes."""
    world = await _build_world(maker, config=_process_with_check("chk"))
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING

    add_label(world, LABEL_EXIT)
    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.CANCELLED
    assert world.adapter.cancelled == [], "a check has no substrate to stop"
    assert (await ticket_row(world)).status == TicketStatus.EXITED

    remove_label(world, LABEL_EXIT)
    await run_tick(world)  # re-enter grants attempt 2
    world.clock.advance(timedelta(minutes=11))  # past the pinned backoff
    await run_tick(world)

    runs = await step_runs(world)
    assert [r.status for r in runs] == [StepRunStatus.CANCELLED, StepRunStatus.RUNNING]
    assert [r.attempt for r in runs] == [1, 2]


class _UnstoppableAdapter(FakeAdapter):
    """A substrate that cannot stop a run — its record is gone, or the stop itself fails."""

    async def cancel(self, run_id: str) -> None:
        raise AgentRuntimeError("no such run")


class _RefusedAdapter(FakeAdapter):
    """A run whose runtime the worker refuses to reach — `adapter_for_run`'s `RuntimeRefused`,
    whose own message tells the operator to exit the ticket."""

    async def cancel(self, run_id: str) -> None:
        raise RuntimeRefused("no runtime row reaches this run")


async def test_a_refused_runtime_does_not_stop_the_exit(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, adapter_cls=_RefusedAdapter)
    await running_run(world)
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.CANCELLED


async def test_an_unstoppable_run_does_not_stop_the_exit(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exit is the lever an operator pulls precisely when the substrate is misbehaving. A
    `cancel` that raises used to unwind the whole tick, so the ticket stayed RUNNING under
    `factorial:exit` and every following tick raised again — no event, no comment, for ever."""
    world = await _build_world(maker, adapter_cls=_UnstoppableAdapter)
    run = await running_run(world)
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.CANCELLED
    [exited] = await exited_events(world)
    assert exited["cancelled"] is True
    [comment] = comments(world)
    assert "couldn't be stopped" in comment
    assert str(run.runtime_run_id) in comment


async def test_exit_leaves_an_interrupted_run_and_its_workspace_alone(world: World) -> None:
    """Exit cancels LIVE work. An interrupted run is parked work: its workspace was kept on a
    human's say-so, and it resumes on re-entry exactly as it would have from blocked."""
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
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.INTERRUPTED

    add_label(world, LABEL_EXIT)
    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.INTERRUPTED
    assert world.adapter.cancelled == []
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert LABEL_INTERRUPTED not in world.github.issue.labels, "exited replaces interrupted"


async def test_an_exited_ticket_is_parked_while_the_label_stays(world: World) -> None:
    """Steady state costs nothing: no events, no GitHub writes, however many ticks pass."""
    await running_run(world)
    add_label(world, LABEL_EXIT)
    await run_tick(world)
    events_before = len(await event_rows(world))
    writes_before = len(world.projector.calls)

    await run_tick(world)
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert len(await event_rows(world)) == events_before
    assert len(world.projector.calls) == writes_before


async def test_a_done_ticket_ignores_the_label(world: World) -> None:
    """Done is terminal. The label on a finished issue is a human's note, not a gesture."""
    await running_run(world)
    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(status=TicketStatus.DONE)
        )
        await session.commit()
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.DONE
    assert EventType.TICKET_EXITED not in await event_types(world)


async def test_a_request_is_pending_only_until_something_answers_it(world: World) -> None:
    """By event id, not by count. `ticket_exited` is also written for a label or a closed
    issue, so a lifetime count of answers outruns the requests: after one label exit, a
    dashboard press would read as already answered and be swallowed for ever."""

    async def exited(via: str) -> None:
        async with world.sessions() as session:
            await append(
                session,
                type=EventType.TICKET_EXITED,
                project_id=world.project_id,
                ticket_id=world.ticket_id,
                via=via,
            )
            await session.commit()

    async def pending() -> bool:
        async with world.sessions() as session:
            return await _pending(
                session, world.ticket_id, EventType.EXIT_REQUESTED, EventType.TICKET_EXITED
            )

    await exited("label")
    assert not await pending()
    await request(world, EventType.EXIT_REQUESTED)
    assert await pending(), "one unrequested exit in the past must not swallow this press"
    await exited("dashboard")
    assert not await pending()


async def reentered_events(world: World) -> list[dict[str, object]]:
    return [
        dict(e.details) for e in await event_rows(world) if e.type == EventType.TICKET_REENTERED
    ]


async def refused_events(world: World) -> list[dict[str, object]]:
    return [
        dict(e.details) for e in await event_rows(world) if e.type == EventType.REENTRY_REFUSED
    ]


async def test_removing_the_label_brings_the_ticket_back(world: World) -> None:
    """Re-entry is un-block with different words: the same fresh attempt is granted, the ticket
    is running again, and `closed_at` clears so it leaves the Done lane."""
    await exited_via_label(world)
    remove_label(world, LABEL_EXIT)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.closed_at is None
    [reentered] = await reentered_events(world)
    assert reentered["via"] == "label"
    assert reentered["attempt"] == 1  # the cancelled run
    assert reentered["next_attempt"] == 2  # the one this gesture grants
    assert reentered["resumed"] is False
    assert LABEL_EXIT not in world.github.issue.labels
    assert "factorial:plan" in world.github.issue.labels
    assert any(c.startswith("Re-entered") for c in comments(world))


async def test_re_entry_starts_a_fresh_attempt_once_the_backoff_clears(world: World) -> None:
    """The granted attempt is gated on `_retry_due` like any retry: the cancelled run's
    `ended_at` plus the pinned backoff (10m in the world's process)."""
    await exited_via_label(world)
    remove_label(world, LABEL_EXIT)
    await run_tick(world)  # re-enter

    await run_tick(world)  # inside the backoff: nothing spawns
    assert len(await step_runs(world)) == 1

    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)

    first, second = await step_runs(world)
    assert first.status == StepRunStatus.CANCELLED
    assert second.attempt == 2
    assert second.status == StepRunStatus.RUNNING
    assert len(world.adapter.spawns) == 2


async def test_a_dashboard_request_brings_the_ticket_back_and_clears_the_label(
    world: World,
) -> None:
    """The label is a projection: after a dashboard re-entry the loop removes it itself."""
    await running_run(world)
    await request(world, EventType.EXIT_REQUESTED)
    await run_tick(world)
    assert LABEL_EXIT in world.github.issue.labels
    await request(world, EventType.REENTRY_REQUESTED)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    [reentered] = await reentered_events(world)
    assert reentered["via"] == "dashboard"
    assert LABEL_EXIT not in world.github.issue.labels


async def test_re_entry_is_refused_once_while_the_issue_is_closed(world: World) -> None:
    """Reopening is a precondition the loop observes, not a gesture it acts on. The refusal
    answers the gesture exactly once: the label is re-asserted, so the next tick sees a parked
    ticket rather than a second removal."""
    await running_run(world)
    close_issue(world)
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    remove_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [refused] = await refused_events(world)
    assert refused["reason"] == "issue_closed"
    assert LABEL_EXIT in world.github.issue.labels, "the steady label is re-asserted"
    assert sum("Re-entry refused" in c for c in comments(world)) == 1

    await run_tick(world)
    await run_tick(world)
    assert len(await refused_events(world)) == 1
    assert sum("Re-entry refused" in c for c in comments(world)) == 1


async def test_re_entry_after_reopening_proceeds(world: World) -> None:
    await running_run(world)
    close_issue(world)
    await run_tick(world)
    reopen_issue(world)
    remove_label(world, LABEL_EXIT)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert len(await reentered_events(world)) == 1


async def test_re_entry_resumes_an_interrupted_run_in_its_workspace(world: World) -> None:
    """Parked work stays parked through an exit and releases the way it would have: the same
    attempt continues, so `next_attempt == attempt` and the event says so."""
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
    add_label(world, LABEL_EXIT)
    await run_tick(world)
    remove_label(world, LABEL_EXIT)

    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING
    assert [rid for rid, _turn in world.adapter.resumed] == [row.runtime_run_id]
    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.closed_at is None
    [reentered] = await reentered_events(world)
    assert reentered["resumed"] is True
    assert reentered["attempt"] == reentered["next_attempt"] == 1


async def test_re_entry_reopens_a_cancelled_gate(world: World) -> None:
    """A cancelled gate row is a closed round; re-entry opens a new one against the PR as it is
    now, approvals given while the ticket was away included."""
    await advance_to_gate(world)
    add_label(world, LABEL_EXIT)
    await run_tick(world)
    remove_label(world, LABEL_EXIT)

    await run_tick(world)  # re-enter
    await run_tick(world)  # reopen the gate

    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [g.status for g in gates] == [StepRunStatus.CANCELLED, StepRunStatus.WAITING]
    assert gates[-1].attempt == 2
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE


async def test_re_entry_without_a_retry_policy_is_refused_and_answers_the_request(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Un-block's own refusal, with re-entry's words. The dashboard request must still be
    answered, or it pends for ever and re-enters the ticket the moment it is next exited."""
    world = await _build_world(maker, config=_process_without_retry("no-retry"))
    await running_run(world)
    await request(world, EventType.EXIT_REQUESTED)
    await run_tick(world)
    await request(world, EventType.REENTRY_REQUESTED)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [refused] = await refused_events(world)
    assert refused["reason"] == "no_retry_policy"
    assert any("Re-entering `plan` refused" in c for c in comments(world))
    assert any("restart the ticket on the latest version" in c for c in comments(world))

    await run_tick(world)
    assert len(await refused_events(world)) == 1


async def test_un_block_still_says_un_blocked(world: World) -> None:
    """The extraction must not change un-block's vocabulary: its tests and its readers pin the
    lead word."""
    await _escalate_the_plan_step(world)
    remove_label(world, "factorial:blocked")

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert EventType.UNBLOCKED in await event_types(world)
    assert any(c.startswith("Unblocked") for c in comments(world))
    assert not await reentered_events(world)
