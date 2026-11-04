"""A GitHub project with no connection is held: nothing is admitted, read or written, and the
loop does not count its ticks as failures, so it picks up where it was once connected."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api import deps as api_deps
from orchestrator.db.models import Event, Project, Ticket, TicketStatus
from orchestrator.github.connection import PRIVATE_KEY_SECRET
from orchestrator.secrets import store
from orchestrator.settings import Settings
from orchestrator.worker.admission import InMemoryCandidates, admit_all
from orchestrator.worker.context import production_deps
from orchestrator.worker.loop import _reconcile_leased
from runtime.contract import RunStatus
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    World,
    _build_world,
    _ensure_leased,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
    with_github,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

#: Past `worker.quarantine_after`'s default of 20, against a floor of 3.
_TICKS = 21
_QUARANTINE_AFTER = 3


async def _held_tick(world: World) -> None:
    async with world.sessions() as session:
        ticket = await session.get_one(Ticket, world.ticket_id)
        await _ensure_leased(session, ticket)
    await _reconcile_leased(
        world.ticket_id,
        world.deps,
        scope=world.sessions,
        lease_owner=_LEASE_OWNER,
        lease_ttl=_LEASE_TTL,
        listings=None,
        quarantine_after=_QUARANTINE_AFTER,
    )


async def test_an_unconnected_github_project_admits_nothing_and_writes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, admit=False)
    world.deps = with_github(world.deps, None)
    async with world.sessions() as session:
        (await session.get_one(Project, world.project_id)).active = True

    await admit_all(
        world.sessions,
        world.deps,
        lambda _client: InMemoryCandidates({world.repo: (world.github.issue,)}),
    )

    async with world.sessions() as session:
        tickets = (
            await session.execute(select(Ticket.id).where(Ticket.project_id == world.project_id))
        ).all()
        events = (
            await session.execute(select(Event.id).where(Event.project_id == world.project_id))
        ).all()
    assert tickets == []
    assert events == []
    assert world.projector.calls == []


async def test_an_unconnected_project_s_ticket_is_held_without_escalation_retry_or_quarantine(
    world: World,
) -> None:
    await run_tick(world)
    (spawned,) = await step_runs(world)
    events = await event_types(world)
    calls = list(world.projector.calls)
    world.deps = with_github(world.deps, None)
    # Past the step's timeout, so a tick that reached the run would time it out and retry.
    world.clock.advance(timedelta(hours=2))

    for _ in range(_TICKS):
        await _held_tick(world)

    ticket = await ticket_row(world)
    assert ticket.quarantined_at is None
    assert ticket.consecutive_failures == 0
    assert ticket.status == TicketStatus.RUNNING
    assert await event_types(world) == events
    assert "escalated" not in events and "step_failed" not in events
    assert world.projector.calls == calls
    assert world.adapter.cancelled == []
    (run,) = await step_runs(world)
    assert (run.id, run.attempt, run.status) == (spawned.id, spawned.attempt, spawned.status)


async def test_it_continues_once_connected(world: World) -> None:
    await run_tick(world)
    connected = world.deps
    world.deps = with_github(world.deps, None)
    for _ in range(_TICKS):
        await _held_tick(world)

    world.deps = connected
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, summary="done")
    await _held_tick(world)

    assert (await ticket_row(world)).current_step == "spec_gate"
    assert dict(world.projector.calls)["set_labels"] == frozenset({"factorial:spec-review"})


async def test_the_worker_resolves_a_project_s_own_connection_and_its_bot_login(
    world: World, master_key: None
) -> None:
    """The production wiring: no connection reads as unconnected, and a saved connection
    carries the project's own bot login."""
    deps = production_deps(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    async with world.sessions() as session:
        project = await session.get_one(Project, world.project_id)
        assert await deps.github_for(session, project) is None

        project.settings = {
            **project.settings,
            "github": {"mode": "app", "app_id": "42", "bot_login": "acme-factorial[bot]"},
        }
        await store.put(session, project.id, PRIVATE_KEY_SECRET, "-----BEGIN KEY-----")
        github = await deps.github_for(session, project)

    assert github is not None
    assert github.bot_login == "acme-factorial[bot]"
    assert github.client.connection is not None
    assert github.client.connection.app_id == "42"


async def test_a_leftover_deployment_app_in_the_environment_connects_no_project(
    world: World, master_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment may still pass a retired App's variables. Read, they would connect every
    unconnected project to that one App, in the worker and in the API."""
    for name, value in {
        "APP_ID": "7",
        "INSTALLATION_ID": "8",
        "PRIVATE_KEY": "-----BEGIN KEY-----",
        "BOT_LOGIN": "deployment[bot]",
    }.items():
        monkeypatch.setenv(f"FACTORIAL_GITHUB__{name}", value)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    deps = production_deps(settings)
    async with world.sessions() as session:
        project = await session.get_one(Project, world.project_id)
        assert await deps.github_for(session, project) is None
        assert await api_deps.project_github(project) is None
