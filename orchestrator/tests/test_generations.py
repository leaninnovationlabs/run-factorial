"""Generations (tech-stack.md decision 67): after a restart the loop decides with the new
generation's runs alone, while attempt numbers keep counting and history keeps every run.

Each test fakes the restart's effect on the ticket row (`start_generation`) rather than running a
restart, so a failure here names the read that leaked, not the restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import request_retry
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.services.tickets import _latest_attempt
from orchestrator.db.models import (
    ProcessVersion,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.read_models import factory_overview, latest_run
from orchestrator.events.terminals import is_current_run
from orchestrator.events.workspaces import kept_workspaces
from orchestrator.factorial.registry import lane_head
from orchestrator.worker.gates import _answered_review_ids
from orchestrator.worker.tick_context import _enter_step, _load_pinned_config
from runtime.contract import Artifact, ArtifactKind, RunStatus
from tests.reconcile_world import (
    World,
    _build_world,
    _escalate_the_plan_step,
    event_rows,
    retry_waiting,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def start_generation(world: World, step: str) -> None:
    """What a restart does to the ticket row, without the rest of the restart: the next
    generation, entered at `step` with the restart's own reason."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        config, _, _ = await _load_pinned_config(session, ticket.process_version_id)
        ticket.generation += 1
        await _enter_step(
            session, ticket, config, step=step, from_step=ticket.current_step, reason="restart"
        )
        ticket.current_step = step
        ticket.status = TicketStatus.RUNNING
        await session.commit()


async def bump_generation(world: World) -> None:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        ticket.generation += 1
        await session.commit()


async def seed_run(
    world: World,
    *,
    step: str = "plan",
    attempt: int = 1,
    status: StepRunStatus = StepRunStatus.SUCCEEDED,
    generation: int = 1,
    kind: StepKind = StepKind.AGENT,
    runtime_run_id: str | None = None,
    ended_at: datetime | None = None,
) -> int:
    async with world.sessions() as session:
        run = StepRun(
            ticket_id=world.ticket_id,
            step_name=step,
            kind=kind,
            attempt=attempt,
            status=status,
            generation=generation,
            runtime_run_id=runtime_run_id,
            ended_at=ended_at,
        )
        session.add(run)
        await session.commit()
        return run.id


async def test_a_new_generation_starts_its_step_fresh(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The old generation's latest plan run SUCCEEDED, which the loop would read as a gate's
    rework and hand the delivered branch back. A new generation must spawn a fresh attempt with
    `{slug}` literal again — and the attempt number keeps counting, so the unique key holds."""
    world = await _build_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
    )
    await run_tick(world)  # accept plan -> spec_gate; the slug is on record
    await run_tick(world)  # open the gate

    await start_generation(world, "plan")
    world.adapter.restart()
    await run_tick(world)

    plan = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert [(r.attempt, r.generation) for r in plan] == [(1, 1), (2, 2)]
    assert plan[-1].entered_by == "restart"
    assert world.adapter.spawns[-1].workspace.branch == "spec/12-{slug}"


async def test_a_new_generation_gets_the_whole_retry_budget(world: World) -> None:
    """Generation 1 spent both attempts and escalated. Generation 2 starts at once — no backoff
    is owed to the old failure — and its first failure is retried, not escalated."""
    await _escalate_the_plan_step(world)
    await start_generation(world, "plan")
    world.adapter.restart()

    await run_tick(world)
    assert [(r.attempt, r.generation) for r in await step_runs(world)] == [
        (1, 1),
        (2, 1),
        (3, 2),
    ]

    world.adapter.complete(RunStatus.FAILED, failure_reason="still no")
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    scheduled = [e.details for e in await event_rows(world) if e.type == "retry_scheduled"]
    assert [d["attempt"] for d in scheduled] == [1, 3]


async def test_review_ids_answered_before_a_restart_stay_answered(world: World) -> None:
    """GitHub keeps a CHANGES_REQUESTED review as the reviewer's latest (factorial#132). One the
    first generation already answered must not reject the second generation's gate."""
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
    await bump_generation(world)

    async with world.sessions() as session:
        assert await _answered_review_ids(session, world.ticket_id, "spec_gate") == {7}


async def test_current_state_reads_forget_an_earlier_generation(world: World) -> None:
    """Right after a restart the ticket has run nothing at its step in the new generation, and
    every current-state read must say so — not show the old run as the ticket's latest."""
    old = await seed_run(world, attempt=1, status=StepRunStatus.FAILED)
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

    latest, attempt, latest_attempt, current = await reads()
    assert (attempt, latest_attempt, current) == (2, 2, True)
    assert latest is not None and latest != old

    await bump_generation(world)

    assert await reads() == (None, None, None, False)

    await seed_run(world, attempt=3, status=StepRunStatus.RUNNING, generation=2)
    _, attempt, latest_attempt, current = await reads()
    assert (attempt, latest_attempt, current) == (1, 3, False), "the board counts from the restart"


async def test_a_retry_request_for_an_earlier_generation_is_refused(world: World) -> None:
    failed = await retry_waiting(world)
    await bump_generation(world)
    async with world.sessions() as session:
        run = await session.get(StepRun, failed.id)
        assert run is not None
        with pytest.raises(Conflict):
            await request_retry(session, run, Actor.dashboard("ops@example.com"))


async def test_an_earlier_generations_workspace_is_no_longer_kept(world: World) -> None:
    """The reaper is the only thing that releases an interrupted or ended run's workspace, and it
    releases what the keep rule stops naming. After a restart, nothing from before it is kept."""
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

    await bump_generation(world)

    assert not await kept()


async def test_the_lane_head_is_the_latest_snapshot_and_none_once_retired(world: World) -> None:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        pinned = await session.get(ProcessVersion, ticket.process_version_id)
        assert pinned is not None
        newer = ProcessVersion(
            project_id=world.project_id,
            process_key=pinned.process_key,
            content=pinned.content,
            source_commit_sha="b" * 40,
            activated_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        session.add(newer)
        await session.commit()
        head = await lane_head(session, world.project_id, pinned.process_key)
        assert head is not None and head.id == newer.id

        for row in (pinned, newer):
            row.retired_at = datetime.now(UTC)
        await session.commit()
        assert await lane_head(session, world.project_id, pinned.process_key) is None
