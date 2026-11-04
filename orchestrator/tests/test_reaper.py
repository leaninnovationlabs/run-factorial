"""The one keep rule (decision 54), as the reaper and a preview read it."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.workspaces import kept_workspaces
from orchestrator.settings import Settings
from orchestrator.worker.loop import _host_adapters, _reapers, _sweep_credentials
from orchestrator.worker.reaper import reap_once
from runtime.adapters.claude_cli import ClaudeCliAdapter
from runtime.adapters.codex_cli import CodexCliAdapter
from tests.reconcile_world import (
    SERVICES,
    PreviewAdapter,
    World,
    _build_world,
    advance_to_gate,
    event_rows,
    previewed_process,
    run_tick,
    step_runs,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

RETENTION = timedelta(days=7)


class _Reaper:
    def __init__(self) -> None:
        self.kept: set[str] = set()

    async def reap(self, keep: Collection[str], *, retain: timedelta | None) -> tuple[str, ...]:
        self.kept = set(keep)
        return ()

    async def team(self, session: object) -> list[_Reaper]:
        return [self]


async def _kept(
    world: World, *, retention: timedelta = RETENTION, now: datetime | None = None
) -> set[str]:
    reaper = _Reaper()
    await reap_once(
        world.sessions,
        reaper.team,
        now=now or world.clock.now,
        retention=retention,
        retain=timedelta(days=30),
    )
    return reaper.kept


def _unique() -> str:
    # The test database is shared, and every fake adapter names its first run `run-1`.
    return f"run-{uuid4().hex}"


async def _ended(
    world: World,
    *,
    ticket: TicketStatus,
    ago: timedelta | None,
    status: StepRunStatus = StepRunStatus.SUCCEEDED,
) -> str:
    """The world's one run, in `status` and ended `ago` (None: never ended), on a ticket now in
    `ticket` state."""
    await run_tick(world)
    (run,) = await step_runs(world)
    run_id = _unique()
    async with world.sessions() as session:
        await session.execute(
            update(StepRun)
            .where(StepRun.id == run.id)
            .values(
                status=status,
                ended_at=None if ago is None else world.clock.now - ago,
                runtime_run_id=run_id,
            )
        )
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(status=ticket)
        )
        await session.commit()
    return run_id


async def _later_attempt(
    world: World, *, status: StepRunStatus, ago: timedelta | None, **spawned: Any
) -> str:
    """Another `plan` attempt after the world's latest, with its own `agent_spawned`."""
    run_id = _unique()
    attempt = len(await step_runs(world)) + 1
    async with world.sessions() as session:
        run = StepRun(
            ticket_id=world.ticket_id,
            step_name="plan",
            kind=StepKind.AGENT,
            attempt=attempt,
            status=status,
            runtime_run_id=run_id,
            ended_at=None if ago is None else world.clock.now - ago,
        )
        session.add(run)
        await session.flush()
        await append(
            session,
            type=EventType.AGENT_SPAWNED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step="plan",
            attempt=attempt,
            run_id=run_id,
            **spawned,
        )
        await session.commit()
    return run_id


async def _previewed_at_gate(maker: async_sessionmaker[AsyncSession], **result: Any) -> World:
    """`plan` spawned public on a preview host, succeeded, and waiting at `spec_gate`."""
    world = await _build_world(
        maker, config=previewed_process(**SERVICES), adapter_cls=PreviewAdapter
    )
    await advance_to_gate(world, **result)
    return world


async def _reworked(maker: async_sessionmaker[AsyncSession]) -> World:
    """`_previewed_at_gate`, its run kept no session, rejected: `plan`'s second attempt runs
    fresh in a workspace of its own."""
    world = await _previewed_at_gate(maker, session_id=None)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects
    world.adapter.restart()
    await run_tick(world)  # the second attempt spawns
    return world


@pytest.mark.parametrize("status", [StepRunStatus.RUNNING, StepRunStatus.IDLE])
async def test_a_live_run_keeps_its_workspace_even_on_an_exited_ticket(
    world: World, status: StepRunStatus
) -> None:
    run_id = await _ended(world, ticket=TicketStatus.EXITED, ago=None, status=status)

    assert run_id in await _kept(world)


@pytest.mark.parametrize("status", [StepRunStatus.INTERRUPTED, StepRunStatus.SUCCEEDED])
async def test_an_exited_ticket_releases_its_workspaces_at_once(
    world: World, status: StepRunStatus
) -> None:
    """An exit can be undone (decision 29), but its workspace is not kept for that."""
    run_id = await _ended(world, ticket=TicketStatus.EXITED, ago=timedelta(hours=1), status=status)

    assert run_id not in await _kept(world)


async def test_a_done_ticket_keeps_its_workspaces_for_the_retention_then_releases_them(
    world: World,
) -> None:
    """So a merged change can still be opened."""
    run_id = await _ended(world, ticket=TicketStatus.DONE, ago=timedelta(hours=1))

    assert run_id in await _kept(world)
    assert run_id not in await _kept(world, now=world.clock.now + RETENTION)


async def test_an_older_attempt_stays_kept_after_its_gate_decides_and_a_newer_attempt_exists(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _reworked(maker)

    async with world.sessions() as session:
        kept = await kept_workspaces(session, world.clock.now, RETENTION, ticket_id=world.ticket_id)

    # Scoped to the ticket: every world names its runs `run-1`, `run-2`.
    assert {"run-1", "run-2"} <= kept.keep


async def test_a_parked_run_on_a_blocked_ticket_is_released_after_the_retention(
    world: World,
) -> None:
    """A human answering a park after the retention gets a fresh start, not the session."""
    run_id = await _ended(
        world, ticket=TicketStatus.BLOCKED, ago=timedelta(days=1), status=StepRunStatus.INTERRUPTED
    )

    assert run_id in await _kept(world)
    assert run_id not in await _kept(world, now=world.clock.now + RETENTION)


async def test_a_workspace_adopted_by_a_later_run_stays_kept_while_the_adopter_is_recent(
    world: World,
) -> None:
    owner = await _ended(world, ticket=TicketStatus.WAITING_GATE, ago=timedelta(days=10))
    adopter = await _later_attempt(
        world, status=StepRunStatus.SUCCEEDED, ago=timedelta(days=1), workspace_run_id=owner
    )

    assert {owner, adopter} <= await _kept(world)


async def test_the_retention_setting_is_honoured(world: World) -> None:
    run_id = await _ended(world, ticket=TicketStatus.WAITING_GATE, ago=timedelta(days=2))

    assert run_id in await _kept(world, retention=timedelta(days=3))
    assert run_id not in await _kept(world, retention=timedelta(days=1))


# --------------------------------------------------------------------------- previews


@pytest.fixture
def _no_deployment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test database is shared and a World commits: another test's awake preview must not
    fill this deployment's limit."""
    monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", 10**6)


async def _first_plan_run(world: World) -> StepRun:
    return next(run for run in await step_runs(world) if run.step_name == "plan")


async def _start_preview(world: World, run: StepRun) -> None:
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.PREVIEW_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            requested_by="reviewer@example.com",
        )
        await session.commit()
    await run_tick(world)


def _preview_starts(world: World) -> list[str]:
    assert isinstance(world.adapter, PreviewAdapter)
    return [run_id for run_id, *_ in world.adapter.preview_starts]


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_public_kept_run_can_be_previewed_after_its_review_decided(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _reworked(maker)

    await _start_preview(world, await _first_plan_run(world))

    assert _preview_starts(world) == ["run-1"]


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_preview_is_refused_on_a_workspace_a_later_run_continues_in(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _previewed_at_gate(maker)
    first = await _first_plan_run(world)
    await _later_attempt(
        world,
        status=StepRunStatus.SUCCEEDED,
        ago=timedelta(minutes=1),
        public=True,
        workspace_run_id=first.runtime_run_id,
    )

    await _start_preview(world, first)

    assert _preview_starts(world) == []
    (stopped,) = [e.details for e in await event_rows(world) if e.type == "preview_stopped"]
    assert "A later attempt continues in this workspace" in stopped["reason"]


async def test_reapers_include_the_host_root_without_any_e2b_config(tmp_path: Path) -> None:
    """A deployment without E2B used to run no reaper at all, so a host run's kept workspace
    would never be removed."""
    runtime = {
        "claude_cli": {"local_root": tmp_path / "claude"},
        "codex_cli": {"local_root": tmp_path / "codex"},
        "e2b": {"state_root": None, "deployment": ""},
    }
    settings = Settings(_env_file=None, runtime=runtime)  # pyright: ignore[reportCallIssue]

    reapers = _reapers(settings, datetime.now)
    assert reapers is not None
    built = await reapers(cast(AsyncSession, None))  # a host reaper reads no session

    assert [type(reaper) for reaper in built] == [ClaudeCliAdapter, CodexCliAdapter]


async def test_the_worker_sweeps_finished_runs_credentials_at_startup(tmp_path: Path) -> None:
    """A codex run that ended before every end deleted its login still holds it on the host."""
    run_dir = tmp_path / "codex" / "runs" / "gh-1-plan-agent-0001"
    (run_dir / "config").mkdir(parents=True)
    (run_dir / "config" / "auth.json").write_text("{}")
    state = {
        "run_id": run_dir.name,
        "status": "succeeded",
        "agent": "plan-agent",
        "ticket": "GH-1",
        "repo": "acme/widgets",
        "branch": "spec/1-{slug}",
        "worktree": str(run_dir / "worktree"),
        "started_at": "2026-09-01T12:00:00Z",
        "deadline": "2026-09-01T13:00:00Z",
        "model": "gpt-5.1-codex",
    }
    (run_dir / "state.json").write_text(json.dumps(state))
    runtime = {
        "claude_cli": {"local_root": tmp_path / "codex"},
        "codex_cli": {"local_root": tmp_path / "codex"},
    }
    settings = Settings(_env_file=None, runtime=runtime)  # pyright: ignore[reportCallIssue]

    assert _sweep_credentials(_host_adapters(settings)) == 1
    assert not (run_dir / "config" / "auth.json").exists()
