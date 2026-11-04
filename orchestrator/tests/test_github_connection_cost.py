"""What resolving a project's GitHub connection costs the worker: one resolution per project per
tick, shared by admission and every ticket, and a connection saved between ticks is the one the
next tick uses."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Collection
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project, Ticket, TicketStatus
from orchestrator.github.connection import PRIVATE_KEY_SECRET, WEBHOOK_SECRET
from orchestrator.secrets import store
from orchestrator.settings import Settings
from orchestrator.worker.admission import InMemoryCandidates
from orchestrator.worker.context import (
    ProjectGitHub,
    ReconcileDeps,
    github_once_per_tick,
    production_deps,
)
from orchestrator.worker.loop import tick
from tests.reconcile_world import World

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_HOLD = "connection-cost-bystanders"
_TICKETS = 3


@pytest.fixture
async def held(world: World) -> AsyncIterator[World]:
    """Every other project's tickets leased away, so `tick` reconciles this world's alone."""
    async with world.sessions() as session:
        await session.execute(
            update(Ticket)
            .where(
                Ticket.project_id != world.project_id,
                or_(Ticket.lease_owner.is_(None), Ticket.lease_expires_at < func.now()),
            )
            .values(lease_owner=_HOLD, lease_expires_at=text("now() + interval '1 hour'"))
        )
    try:
        yield world
    finally:
        async with world.sessions() as session:
            await session.execute(
                update(Ticket)
                .where(Ticket.lease_owner == _HOLD)
                .values(lease_owner=None, lease_expires_at=None)
            )


async def test_one_tick_resolves_a_project_s_connection_once_for_all_its_tickets(
    held: World,
) -> None:
    world = held
    async with world.sessions() as session:
        first = await session.get_one(Ticket, world.ticket_id)
        (await session.get_one(Project, world.project_id)).active = True
        for number in range(1, _TICKETS):
            session.add(
                Ticket(
                    project_id=world.project_id,
                    ticket_source_id=first.ticket_source_id,
                    external_key=str(100 + number),
                    process_version_id=first.process_version_id,
                    owner_at_admission="operator",
                    current_step=first.current_step,
                    status=TicketStatus.RUNNING,
                )
            )
    resolutions: Counter[int] = Counter()

    async def unconnected(_session: AsyncSession, project: Project) -> ProjectGitHub | None:
        resolutions[project.id] += 1
        return None

    deps = replace(world.deps, resolve_github=unconnected)
    for _ in range(2):
        reconciled = await tick(
            deps,
            sessions=world.sessions,
            owner=f"cost-{uuid4().hex[:8]}",
            limit=50,
            ttl=timedelta(seconds=60),
            candidates=lambda _client: InMemoryCandidates({}),
        )
        assert reconciled == _TICKETS

    # Admission and three tickets each tick, against one resolution; the second tick reads again.
    assert resolutions[world.project_id] == 2


@pytest.mark.usefixtures("master_key")
async def test_a_tick_decrypts_a_connection_once_and_the_next_tick_reads_a_rotated_key(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = production_deps(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    async with world.sessions() as session:
        project = await session.get_one(Project, world.project_id)
        project.settings = {
            **project.settings,
            "github": {"mode": "app", "app_id": "42", "bot_login": "acme[bot]"},
        }
        await store.put(session, project.id, PRIVATE_KEY_SECRET, "old-key")
        await store.put(session, project.id, WEBHOOK_SECRET, "whsec")

    read: list[frozenset[str]] = []
    get_many = store.get_many

    async def counted(
        session: AsyncSession, project_id: int, names: Collection[str]
    ) -> dict[str, SecretStr]:
        if project_id == world.project_id:
            read.append(frozenset(names))
        return await get_many(session, project_id, names)

    monkeypatch.setattr(store, "get_many", counted)

    async def key_seen() -> list[str]:
        keys = []
        for _ in range(_TICKETS):
            async with world.sessions() as session:
                project = await session.get_one(Project, world.project_id)
                github = await deps.github_for(session, project)
            assert github is not None and github.client.connection is not None
            assert github.client.connection.private_key is not None
            keys.append(github.client.connection.private_key.get_secret_value())
        return keys

    with github_once_per_tick():
        before = await key_seen()
    async with world.sessions() as session:
        await store.put(session, world.project_id, PRIVATE_KEY_SECRET, "new-key")
    with github_once_per_tick():
        after = await key_seen()

    assert before == ["old-key"] * _TICKETS
    assert after == ["new-key"] * _TICKETS
    assert read == [frozenset({PRIVATE_KEY_SECRET})] * 2


async def test_a_task_started_in_a_tick_resolves_afresh_once_the_tick_is_over() -> None:
    """A run's supervisor is started inside the tick and outlives it, holding the tick's
    context: past the tick, it must read the connection as it is now."""
    resolutions = 0

    async def resolve(_session: AsyncSession, _project: Project) -> ProjectGitHub | None:
        nonlocal resolutions
        resolutions += 1
        return None

    deps = ReconcileDeps(resolve_github=resolve)
    project = Project(id=1)
    later = asyncio.Event()

    async def supervisor() -> None:
        await deps.github_for(None, project)  # type: ignore[arg-type]
        await later.wait()
        await deps.github_for(None, project)  # type: ignore[arg-type]

    with github_once_per_tick():
        await deps.github_for(None, project)  # type: ignore[arg-type]
        running = asyncio.create_task(supervisor())
        await asyncio.sleep(0)
    later.set()
    await running

    assert resolutions == 2
