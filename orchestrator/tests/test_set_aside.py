"""Set-aside runs (decision 76): after a redo the loop decides without the runs it set aside,
while attempt numbers keep counting and answered reviews stay answered.

Each test fakes the redo's effect on the rows (`set_aside`) rather than running a redo, so a
failure here names the read that leaked, not the redo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.services.tickets import _latest_attempt
from orchestrator.db.models import StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.read_models import factory_overview, latest_run
from orchestrator.events.terminals import is_current_run
from orchestrator.events.workspaces import kept_workspaces
from orchestrator.worker.gates import _answered_review_ids
from orchestrator.worker.tick_context import _enter_step, _load_pinned_config
from runtime.contract import Artifact, ArtifactKind, RunStatus
from tests.reconcile_world import (
    World,
    _build_world,
    _escalate_the_plan_step,
    event_rows,
    run_tick,
    step_runs,
    ticket_row,
)
from tests.test_generations import seed_run

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def set_aside(world: World, *steps: str, enter: str | None = None) -> None:
    """What a redo does to the rows, without the rest of the redo: every current run of `steps`
    set aside, and the ticket entered at `enter` with the redo's own reason."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await session.execute(
            update(StepRun)
            .where(
                StepRun.ticket_id == ticket.id,
                StepRun.step_name.in_(steps),
                StepRun.generation == ticket.generation,
                StepRun.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now(UTC))
        )
        if enter is not None:
            config, _, _ = await _load_pinned_config(session, ticket.process_version_id)
            await _enter_step(
                session, ticket, config, step=enter, from_step=ticket.current_step, reason="redo"
            )
            ticket.current_step = enter
            ticket.status = TicketStatus.RUNNING
        await session.commit()


async def test_a_set_aside_step_starts_fresh(maker: async_sessionmaker[AsyncSession]) -> None:
    """The set-aside plan run SUCCEEDED, which the loop would read as a gate's rework and hand
    the delivered branch and session back. A redone step starts fresh, with `{slug}` literal
    again, and its attempt number keeps counting."""
    world = await _build_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
    )
    await run_tick(world)  # accept plan -> spec_gate; the slug is on record
    await run_tick(world)  # open the gate

    await set_aside(world, "plan", "spec_gate", enter="plan")
    world.adapter.restart()
    await run_tick(world)

    plan = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert [(r.attempt, r.superseded_at is None) for r in plan] == [(1, False), (2, True)]
    assert plan[-1].entered_by == "redo"
    assert world.adapter.spawns[-1].resume_from is None
    assert world.adapter.spawns[-1].workspace.branch == "spec/12-{slug}"


async def test_a_set_aside_step_gets_the_whole_retry_budget(world: World) -> None:
    """Both attempts failed and escalated before the redo. The redone step's first failure is
    retried, not escalated."""
    await _escalate_the_plan_step(world)
    await set_aside(world, "plan", enter="plan")
    world.adapter.restart()

    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="still no")
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    scheduled = [e.details for e in await event_rows(world) if e.type == "retry_scheduled"]
    assert [d["attempt"] for d in scheduled] == [1, 3]


async def test_current_state_reads_forget_a_set_aside_run(world: World) -> None:
    """Right after a redo the ticket has run nothing current at its step, and every
    current-state read must say so."""
    await seed_run(world, attempt=1, status=StepRunStatus.FAILED)
    await seed_run(world, attempt=2, status=StepRunStatus.FAILED)

    async def reads() -> tuple[object, ...]:
        async with world.sessions() as session:
            ticket = await session.get(Ticket, world.ticket_id)
            assert ticket is not None
            latest = await session.scalar(
                select(latest_run(StepRun.id)).select_from(Ticket).where(Ticket.id == ticket.id)
            )
            board = await factory_overview(session, 7, await default_organization_id(session))
            [row] = [r for r in board if r.ticket_id == ticket.id]
            return (
                latest,
                row.attempt,
                await _latest_attempt(session, ticket),
                await is_current_run(
                    session, ticket_id=ticket.id, step="plan", attempt=2, current_step="plan"
                ),
            )

    _, attempt, latest_attempt, current = await reads()
    assert (attempt, latest_attempt, current) == (2, 2, True)

    await set_aside(world, "plan")

    assert await reads() == (None, None, None, False)

    await seed_run(world, attempt=3, status=StepRunStatus.RUNNING)
    _, attempt, latest_attempt, current = await reads()
    assert (attempt, latest_attempt, current) == (1, 3, False), "the board counts from the redo"


async def test_review_ids_answered_before_a_redo_stay_answered(world: World) -> None:
    """GitHub keeps a CHANGES_REQUESTED review as the reviewer's latest (factorial#132). One
    answered before the redo must not reject the redone step's gate."""
    gate_id = await seed_run(
        world, step="spec_gate", kind=StepKind.HUMAN_GATE, status=StepRunStatus.REJECTED
    )
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.GATE_REJECTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=gate_id,
            review_ids=[7],
        )
        await session.commit()
    await set_aside(world, "spec_gate")

    async with world.sessions() as session:
        assert await _answered_review_ids(session, world.ticket_id, "spec_gate") == {7}


async def test_a_set_aside_runs_workspace_is_no_longer_kept(world: World) -> None:
    """The reaper releases what the keep rule stops naming. A redo's discarded work is not
    kept."""
    now = datetime.now(UTC)
    old = await seed_run(
        world,
        status=StepRunStatus.INTERRUPTED,
        runtime_run_id="run-old",
        ended_at=now - timedelta(hours=1),
    )

    async def kept() -> bool:
        async with world.sessions() as session:
            found = await kept_workspaces(
                session, now, timedelta(days=7), ticket_id=world.ticket_id
            )
            return old in found.runs

    assert await kept()

    await set_aside(world, "plan")

    assert not await kept()
