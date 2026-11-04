"""A gate's pull request closed without a merge: the gate holds instead of rejecting, and the
ticket leaves only when a person says so (decision 79)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from tests.reconcile_world import (
    World,
    _build_world,
    _process_without_gate_reject,
    advance_to_gate,
    close_issue,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def _closed_rows(world: World) -> list[dict[str, object]]:
    return [dict(row.details) for row in await event_rows(world) if row.type == "gate_pr_closed"]


def _reopen_pr(world: World) -> None:
    world.github.pulls = [replace(pr, state="open") for pr in world.github.pulls]


async def test_a_closed_pull_request_holds_the_gate_instead_of_sending_the_work_back(
    world: World,
) -> None:
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    world.projector.calls.clear()

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.WAITING_GATE
    assert ticket.current_step == "spec_gate"
    assert [(r.step_name, r.status) for r in await step_runs(world)] == [
        ("plan", StepRunStatus.SUCCEEDED),
        ("spec_gate", StepRunStatus.WAITING),
    ]
    assert EventType.GATE_REJECTED not in await event_types(world)
    assert await _closed_rows(world) == [
        {"step": "spec_gate", "attempt": 1, "pr_number": 101, "head_sha": "sha-1"}
    ]
    [comment] = _comments(world)
    assert "https://github.test/pr/101 was closed without merging" in comment
    assert "reopen it" in comment
    assert "close this issue" in comment


async def test_a_held_gate_says_so_once(world: World) -> None:
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    await run_tick(world)
    before = await event_types(world)
    world.projector.calls.clear()

    await run_tick(world)
    await run_tick(world)

    assert await event_types(world) == before
    assert world.projector.calls == []


async def test_a_closed_pull_request_holds_a_gate_with_no_onreject(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Without the hold, a gate with no rework edge blocked the ticket as a failure."""
    world = await _build_world(maker, config=_process_without_gate_reject("gate-closed-hold"))
    await advance_to_gate(world)
    world.github.close_pr_unmerged()

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    assert EventType.ESCALATED not in await event_types(world)
    assert len(await _closed_rows(world)) == 1


async def test_reopening_the_pull_request_resumes_the_review(world: World) -> None:
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    await run_tick(world)
    _reopen_pr(world)
    world.github.approve("reviewer-1")

    await run_tick(world)

    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [(g.attempt, g.status) for g in gates] == [(1, StepRunStatus.APPROVED)]
    assert ("merge_pr", 101) in world.projector.calls
    assert (await ticket_row(world)).current_step == "done"


async def test_an_approval_then_a_close_holds_without_merging(world: World) -> None:
    """Closing is the later act, so it wins over the standing approval. The approve arm's
    `(state == "open" or merged)` conjunct keeps it from merging a closed pull request:
    `_approve`'s merge guard is `not merged`, which a closed one passes."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    world.github.close_pr_unmerged()
    world.projector.calls.clear()

    await run_tick(world)

    assert "merge_pr" not in world.projector.kinds()
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.WAITING
    assert len(await _closed_rows(world)) == 1


async def test_closing_the_issue_exits_a_gate_held_on_a_closed_pull_request(world: World) -> None:
    """Decision 29 skips a waiting gate because a merge closes the issue a tick before the gate
    sees it. A pull request closed without a merge is not that merge."""
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    await run_tick(world)
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    [exited] = [row.details for row in await event_rows(world) if row.type == "ticket_exited"]
    assert exited["via"] == "issue_closed"
    gate = next(run for run in await step_runs(world) if run.step_name == "spec_gate")
    assert gate.status == StepRunStatus.CANCELLED


async def test_closing_the_pull_request_and_the_issue_together_exits(world: World) -> None:
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    close_issue(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert EventType.GATE_REJECTED not in await event_types(world)


async def _request_exit(world: World, **details: object) -> None:
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.EXIT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=None,
            requested_via="dashboard",
            **details,
        )
        await session.commit()


async def test_close_and_exit_closes_the_issue_after_its_comment(world: World) -> None:
    await advance_to_gate(world)
    world.github.close_pr_unmerged()
    await run_tick(world)
    world.projector.calls.clear()
    await _request_exit(world, close_issue=True)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    kinds = world.projector.kinds()
    assert kinds[-1] == "close_issue", "the exit comment must land before the issue closes"
    assert ("close_issue", world.issue_number) in world.projector.calls
    [comment] = _comments(world)
    assert "reopen the issue, then" in comment


async def test_a_plain_exit_leaves_the_issue_open(world: World) -> None:
    await advance_to_gate(world)
    world.projector.calls.clear()
    await _request_exit(world)

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert "close_issue" not in world.projector.kinds()
