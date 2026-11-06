"""Restarting a ticket on its lane's current snapshot (tech-stack.md decision 67).

What would regress silently: a restart that left a branch behind would hand the fresh generation
a postcondition that already holds; one that read an old run would resume work the operator threw
away; and one that answered nothing would leave a re-entry request to fire on the next exit."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import ProcessVersion, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.factorial.registry import lane_head
from orchestrator.github import LABEL_BLOCKED, LABEL_EXIT, LABEL_INTERRUPTED
from orchestrator.github.projector import step_label
from orchestrator.github.state import BranchState
from runtime.contract import AgentRuntimeError
from tests.reconcile_world import (
    FakeAdapter,
    World,
    _build_world,
    _escalate_the_plan_step,
    _process,
    add_label,
    advance_to_gate,
    close_issue,
    event_rows,
    exited_via_label,
    request,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _renamed(name: str) -> ProcessConfig:
    """The world's process with `plan` renamed `outline`: a snapshot whose entry step the old one
    does not have, so projecting it with the old config would raise."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["entry"]["step"] = "outline"
    cfg["steps"][0]["name"] = "outline"
    cfg["steps"][1]["gate"]["onReject"]["rerunStep"] = "outline"
    return ProcessConfig.model_validate(cfg)


async def new_head(world: World, *, rename: bool = False, sha: str = "b" * 40) -> int:
    """Sync a newer snapshot of the ticket's lane at `sha`; its id."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        pinned = await session.get(ProcessVersion, ticket.process_version_id)
        assert pinned is not None
        content = (
            _renamed(pinned.process_key).model_dump(mode="json", by_alias=True)
            if rename
            else pinned.content
        )
        row = ProcessVersion(
            project_id=world.project_id,
            source_repo=world.repo,
            process_key=pinned.process_key,
            content=content,
            source_commit_sha=sha,
            agent_pins=pinned.agent_pins,
            activated_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        session.add(row)
        await session.commit()
        return row.id


async def _pinned(world: World) -> ProcessVersion:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        pinned = await session.get(ProcessVersion, ticket.process_version_id)
        assert pinned is not None
        return pinned


async def ask_restart(
    world: World, target: int, *, allow_current_pin: bool = False, allow_merged: bool = False
) -> None:
    """What the API records. Written directly so this module tests the LOOP's half."""
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.RESTART_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            target_process_version_id=target,
            target_sha="b" * 40,
            allow_current_pin=allow_current_pin,
            allow_merged=allow_merged,
            requested_by="ops@example.com",
            requested_via="dashboard",
        )
        await session.commit()


async def answers(world: World) -> list[tuple[str, dict[str, Any]]]:
    return [
        (e.type, dict(e.details))
        for e in await event_rows(world)
        if e.type in (EventType.TICKET_RESTARTED, EventType.RESTART_REFUSED)
    ]


async def test_a_restart_moves_a_running_ticket_onto_the_new_snapshot_from_its_entry(
    world: World,
) -> None:
    run = await running_run(world)
    ticket_process = (await _pinned(world)).process_key
    world.github.issue = replace(world.github.issue, assignees=("teammate",))
    target = await new_head(world, rename=True)
    await ask_restart(world, target)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.process_version_id, ticket.generation, ticket.current_step) == (
        target,
        2,
        "outline",
    )
    assert (ticket.status, ticket.owner_at_admission) == (TicketStatus.RUNNING, "teammate")
    assert world.adapter.cancelled == [run.runtime_run_id]
    (old,) = await step_runs(world)
    assert old.status == StepRunStatus.CANCELLED
    assert step_label("outline") in world.github.issue.labels
    [(kind, details)] = await answers(world)
    assert kind == EventType.TICKET_RESTARTED
    assert details["cancelled"] == {"step": "plan", "attempt": 1}
    assert (details["generation"], details["entry_step"]) == (2, "outline")
    assert (details["process"], details["requested_by"]) == (ticket_process, "ops@example.com")
    kinds = [e.type for e in await event_rows(world)]
    last_entry = max(i for i, kind in enumerate(kinds) if kind == "step_entered")
    assert kinds.index(EventType.TICKET_RESTARTED) < last_entry, "the restart files first"

    world.adapter.restart()
    await run_tick(world)

    fresh = (await step_runs(world))[-1]
    assert (fresh.step_name, fresh.attempt, fresh.generation, fresh.entered_by) == (
        "outline",
        1,
        2,
        "restart",
    )


async def test_a_restart_removes_the_tickets_github_work_and_nothing_else(
    world: World,
) -> None:
    """Every branch of the ticket goes — the delivered one and one an interrupted run pushed
    without recording it — with its open PR closed. Another ticket's branch stays."""
    await advance_to_gate(world)
    world.github.branches += [
        BranchState(name="spec/12-half", sha="sha-h"),
        BranchState(name="spec/13-other", sha="sha-o"),
    ]
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    calls = world.contract_github.calls
    assert ("close_pull_request", 101) in calls
    assert sorted(name for kind, name in calls if kind == "delete_branch") == [
        "spec/12-half",
        "spec/12-widget",
    ]
    assert [b.name for b in world.github.branches] == ["spec/13-other"]
    gate = [r for r in await step_runs(world) if r.step_name == "spec_gate"][-1]
    assert gate.status == StepRunStatus.CANCELLED

    world.adapter.restart()
    await run_tick(world)
    assert world.adapter.spawns[-1].workspace.branch == "spec/12-{slug}"


async def test_a_restart_abandons_an_interrupted_session_and_starts_fresh(world: World) -> None:
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

    await ask_restart(world, await new_head(world))
    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert kind == EventType.TICKET_RESTARTED
    assert details["abandoned"] == {"step": "plan", "attempt": 1}
    assert LABEL_INTERRUPTED not in world.github.issue.labels

    world.adapter.restart()
    await run_tick(world)
    assert world.adapter.resumed == []
    assert [(r.attempt, r.generation) for r in await step_runs(world)] == [(1, 1), (2, 2)]


async def test_a_restart_brings_an_exited_ticket_back_without_its_exit_label(
    world: World,
) -> None:
    await exited_via_label(world)
    await ask_restart(world, await new_head(world))

    await run_tick(world)
    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.status, ticket.generation, ticket.closed_at) == (TicketStatus.RUNNING, 2, None)
    assert LABEL_EXIT not in world.github.issue.labels


async def test_a_restart_answers_a_re_entry_request_it_overtook(world: World) -> None:
    """Without the answer, the re-entry request would wait for the next exit and undo it."""
    await exited_via_label(world)
    await request(world, EventType.REENTRY_REQUESTED)
    await ask_restart(world, await new_head(world))
    await run_tick(world)  # the restart wins: it is checked before re-entry

    add_label(world, LABEL_EXIT)
    await run_tick(world)  # exits again
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_an_exit_on_the_same_tick_wins_and_refuses_the_restart(world: World) -> None:
    await running_run(world)
    await ask_restart(world, await new_head(world))
    add_label(world, LABEL_EXIT)

    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.status, ticket.generation) == (TicketStatus.EXITED, 1)
    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "exited")


async def test_a_restart_is_refused_while_the_issue_is_closed(world: World) -> None:
    await running_run(world)
    close_issue(world)
    await run_tick(world)  # the closed issue exits the ticket
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "issue_closed")
    assert (await ticket_row(world)).generation == 1


async def test_work_merged_outside_the_ticket_refuses_the_restart(world: World) -> None:
    await advance_to_gate(world)
    world.github.merge_externally()
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    refused = [d for k, d in await answers(world) if k == EventType.RESTART_REFUSED]
    assert [d["reason"] for d in refused] == ["merged_outside"]
    assert not [c for c in world.contract_github.calls if c[0] == "delete_branch"]


async def test_a_restart_that_accepts_the_merged_work_goes_ahead(world: World) -> None:
    """Whether merged work still matters is a person's call: the check sees a merge, not what
    `main` holds now. Said so, the restart removes the ticket's branches and leaves the merged
    pull request as it is."""
    await advance_to_gate(world)
    world.github.merge_externally()
    await ask_restart(world, await new_head(world), allow_merged=True)

    await run_tick(world)

    assert (await ticket_row(world)).generation == 2
    assert ("delete_branch", "spec/12-widget") in world.contract_github.calls
    assert not [c for c in world.contract_github.calls if c[0] == "close_pull_request"]


async def test_a_retired_target_refuses_the_restart(world: World) -> None:
    await running_run(world)
    target = await new_head(world)
    async with world.sessions() as session:
        row = await session.get(ProcessVersion, target)
        assert row is not None
        row.retired_at = datetime.now(UTC)
        await session.commit()
    await ask_restart(world, target)

    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "lane_retired")


async def test_a_target_from_another_repository_refuses_the_restart(world: World) -> None:
    """Asked for before the project's repository changed, landed after: the target's config was
    read from a repository the project has left, so restarting onto it is a retired lane."""
    await running_run(world)
    target = await new_head(world)
    async with world.sessions() as session:
        row = await session.get(ProcessVersion, target)
        assert row is not None
        row.source_repo = "acme/elsewhere"
        await session.commit()
    await ask_restart(world, target)

    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "lane_retired")


class _UnstoppableAdapter(FakeAdapter):
    async def cancel(self, run_id: str) -> None:
        raise AgentRuntimeError("no such run")


async def test_an_unstoppable_run_refuses_the_restart_and_keeps_running(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exit leaves regardless; a restart would start a second agent beside one that may be live."""
    world = await _build_world(maker, adapter_cls=_UnstoppableAdapter)
    await running_run(world)
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "cancel_failed")
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING
    assert (await ticket_row(world)).generation == 1


# ----------------------------------------------------------------- the plan's Review Focus cases


async def test_a_gate_approved_on_the_same_poll_never_merges(world: World) -> None:
    """The restart is the tick's one transition, ahead of dispatch: an approval that lands in the
    same poll is never acted on, and the pull request it approved is closed with the rest."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    assert "merge_pr" not in world.projector.kinds()
    assert ("close_pull_request", 101) in world.contract_github.calls
    assert (await ticket_row(world)).generation == 2


async def test_the_loop_applies_the_snapshot_the_request_named(world: World) -> None:
    """A sync between the press and the pass must not move the ticket onto a snapshot nobody
    saw. The ticket is behind again, so the dashboard offers the button again."""
    await running_run(world)
    seen = await new_head(world)
    await ask_restart(world, seen)
    newer = await new_head(world, sha="c" * 40)

    await run_tick(world)

    assert (await ticket_row(world)).process_version_id == seen
    async with world.sessions() as session:
        head = await lane_head(session, world.project_id, (await _pinned(world)).process_key)
    assert head is not None and head.id == newer


async def test_a_restart_keeps_the_owner_when_the_issue_has_no_assignee(world: World) -> None:
    await running_run(world)
    world.github.issue = replace(world.github.issue, assignees=())
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.generation, ticket.owner_at_admission) == (2, "operator")


async def test_a_second_restart_starts_a_third_generation(world: World) -> None:
    await running_run(world)
    await ask_restart(world, await new_head(world))
    await run_tick(world)
    world.adapter.restart()
    await run_tick(world)  # generation 2's first plan attempt spawns

    await ask_restart(world, await new_head(world, sha="c" * 40))
    await run_tick(world)
    world.adapter.restart()
    await run_tick(world)

    assert [(r.attempt, r.generation) for r in await step_runs(world)] == [
        (1, 1),
        (2, 2),
        (3, 3),
    ]


async def test_a_refused_restart_leaves_a_parked_session_labelled_as_parked(world: World) -> None:
    """A refusal changes nothing, so the issue keeps saying what holds the ticket: an
    interrupted session, never an escalation it is not."""
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
    target = await new_head(world)
    async with world.sessions() as session:
        row = await session.get(ProcessVersion, target)
        assert row is not None
        row.retired_at = datetime.now(UTC)
        await session.commit()
    await ask_restart(world, target)

    await run_tick(world)

    [(kind, details)] = await answers(world)
    assert (kind, details["reason"]) == (EventType.RESTART_REFUSED, "lane_retired")
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert LABEL_BLOCKED not in world.github.issue.labels


async def test_a_cleanup_that_fails_midway_rolls_back_and_the_next_tick_finishes(
    world: World,
) -> None:
    """GitHub writes are not transactional: what the failed tick closed stays closed, the ticket
    row rolls back with the tick, and the request stays pending for the next one."""
    await advance_to_gate(world)
    await ask_restart(world, await new_head(world))
    world.contract_github.fail_delete_branch = RuntimeError("GitHub said 500")

    with pytest.raises(RuntimeError, match="GitHub said 500"):
        await run_tick(world)

    assert (await ticket_row(world)).generation == 1
    assert await answers(world) == []
    assert [b.name for b in world.github.branches] == ["spec/12-widget"]

    world.contract_github.fail_delete_branch = None
    await run_tick(world)

    assert (await ticket_row(world)).generation == 2
    assert world.github.branches == []
    [(kind, _)] = await answers(world)
    assert kind == EventType.TICKET_RESTARTED


async def test_a_restart_releases_an_escalated_ticket(world: World) -> None:
    await _escalate_the_plan_step(world)
    assert LABEL_BLOCKED in world.github.issue.labels
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.status, ticket.generation) == (TicketStatus.RUNNING, 2)
    assert LABEL_BLOCKED not in world.github.issue.labels


async def test_a_restart_brings_back_a_ticket_exited_by_request(world: World) -> None:
    await running_run(world)
    await request(world, EventType.EXIT_REQUESTED)
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    await ask_restart(world, await new_head(world))

    await run_tick(world)
    await run_tick(world)

    ticket = await ticket_row(world)
    assert (ticket.status, ticket.generation, ticket.closed_at) == (TicketStatus.RUNNING, 2, None)
    assert LABEL_EXIT not in world.github.issue.labels
