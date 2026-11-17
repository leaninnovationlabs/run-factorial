"""The Factorial-managed key's limit: a start that would run more sandboxes than an organization or
the pool may hold waits on its step, says why once, and spends nothing.

Every failure here is silent. A start past the limit runs on the deployment's bill. A held start
that spends an attempt, blocks or labels the ticket fails work that was only waiting its turn,
and a held start announced every tick buries the timeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import (
    Organization,
    Project,
    Runtime,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append, factory_overview, sandbox_waits
from orchestrator.secrets import store
from orchestrator.settings import ManagedSandbox
from orchestrator.worker import retries as retries_module
from orchestrator.worker.context import ProjectGitHub
from orchestrator.worker.loop import _reconcile_leased
from orchestrator.worker.reconcile import reconcile_ticket
from orchestrator.worker.runtime_adapters import RuntimeAdapters
from orchestrator.worker.sandbox_limit import managed_running
from runtime.contract import IdleReason, RunHandle, RunSpec, RunStatus, SpawnError
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    World,
    _build_world,
    _ensure_leased,
    _process_on_runtime,
    _runtime_slug,
    event_rows,
    run_tick,
    step_runs,
    ticket_row,
)
from tests.test_reconcile_runtimes import (
    CODEX_AGENT,
    CODEX_BUILD,
    MANAGED_KEY,
    _on_e2b,
    _Sandboxed,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def worlds(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[list[World]]:
    """Every world a test builds. Its live runs end with the test: the database is shared, and
    a managed run left running would count against the pool in every later test."""
    built: list[World] = []
    yield built
    async with maker() as session:
        await session.execute(
            update(StepRun)
            .where(
                StepRun.ticket_id.in_([world.ticket_id for world in built]),
                StepRun.status.in_([StepRunStatus.RUNNING, StepRunStatus.IDLE]),
            )
            .values(status=StepRunStatus.CANCELLED)
        )
        await session.commit()


async def _organization(maker: async_sessionmaker[AsyncSession]) -> tuple[int, str]:
    slug = f"org-{uuid4().hex[:10]}"
    async with maker() as session:
        row = Organization(slug=slug, name=slug, updated_by="test")
        session.add(row)
        await session.commit()
        return row.id, slug


def _managed(**limits: Any) -> ManagedSandbox:
    return ManagedSandbox(
        api_key=SecretStr(MANAGED_KEY), templates={"codex": CODEX_BUILD}, **limits
    )


async def _world(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    organization_id: int,
    managed: ManagedSandbox,
    *,
    own_key: str | None = None,
    adapter: _Sandboxed | None = None,
) -> World:
    """A ticket of `organization_id` on a codex runtime in a sandbox, on the managed key unless
    `own_key` names the E2B key its row stores."""
    slug = _runtime_slug()
    adapter = adapter or _Sandboxed()
    world = await _build_world(
        maker,
        resolved_agent=CODEX_AGENT,
        config=_process_on_runtime(f"proc-{slug}", slug),
        runtimes={slug: ("codex_api", adapter)},
    )
    world.adapter = adapter
    world.deps = replace(world.deps, managed_sandbox=managed)
    worlds.append(world)
    await _on_e2b(world, slug, permit=True, managed=own_key is None)
    async with world.sessions() as session:
        await session.execute(
            update(Project)
            .where(Project.id == world.project_id)
            .values(organization_id=organization_id)
        )
        runtime_id = (
            await session.execute(
                update(Runtime)
                .where(Runtime.slug == slug)
                .values(organization_id=organization_id)
                .returning(Runtime.id)
            )
        ).scalar_one()
        if own_key is not None:
            await store.put_for_runtime(session, runtime_id, "E2B_API_KEY", own_key)
        await session.commit()
    world.runtime_slug = slug  # type: ignore[attr-defined]
    return world


@dataclass
class _Gate:
    """Spawns that return only once the test opens it, so every ticket of a pass holds its
    transaction open at once, as `loop.tick` reconciles them."""

    open: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set whenever a ticket reaches its spawn or finishes.
    moved: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: int = 0


class _Gated(_Sandboxed):
    def __init__(self, gate: _Gate) -> None:
        super().__init__()
        self.gate = gate

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        self.gate.waiting += 1
        self.gate.moved.set()
        await self.gate.open.wait()
        return await super().spawn(run_spec)


def _one_deps(worlds: Sequence[World]) -> None:
    """Every world reconciled with one `ReconcileDeps`, as one worker process does."""
    githubs = {world.project_id: world.project_github for world in worlds}
    adapters = {world.runtime_slug: world.adapter for world in worlds}  # type: ignore[attr-defined]

    async def github_for(_session: AsyncSession, project: Project) -> ProjectGitHub | None:
        return githubs[project.id]

    deps = replace(
        worlds[0].deps,
        resolve_github=github_for,
        runtime_adapters=RuntimeAdapters(build=lambda row, kind: adapters[row.slug]),
    )
    for world in worlds:
        world.deps = deps


async def _gated_worlds(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    gate: _Gate,
    organizations: Sequence[int],
    managed: ManagedSandbox,
) -> list[World]:
    built = [
        await _world(maker, worlds, organization, managed, adapter=_Gated(gate))
        for organization in organizations
    ]
    _one_deps(built)
    return built


async def _one_pass(worlds: Sequence[World], gate: _Gate) -> None:
    """What `loop.tick` does with the tickets it leased: all reconciled at once through
    `_reconcile_leased`, each in its own transaction. The gate opens once every ticket has
    either reached its spawn or finished."""
    for world in worlds:
        async with world.sessions() as session:
            ticket = await session.get(Ticket, world.ticket_id)
            assert ticket is not None
            await _ensure_leased(session, ticket)
    gate.waiting = 0
    tasks = [
        asyncio.create_task(
            _reconcile_leased(
                world.ticket_id,
                world.deps,
                scope=world.sessions,
                lease_owner=_LEASE_OWNER,
                lease_ttl=_LEASE_TTL,
                listings=None,
                quarantine_after=100,
            )
        )
        for world in worlds
    ]
    for task in tasks:
        task.add_done_callback(lambda _: gate.moved.set())
    async with asyncio.timeout(10):
        while gate.waiting + sum(task.done() for task in tasks) < len(tasks):
            gate.moved.clear()
            await gate.moved.wait()
    gate.open.set()
    await asyncio.gather(*tasks)
    gate.open.clear()


async def _live(worlds: Sequence[World]) -> list[World]:
    return [
        world
        for world in worlds
        if [run.status for run in await step_runs(world)] == [StepRunStatus.RUNNING]
    ]


async def _press(world: World, run: StepRun) -> None:
    """What the dashboard's Retry records."""
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


async def _waits(world: World) -> list[dict[str, Any]]:
    return [
        dict(row.details)
        for row in await event_rows(world)
        if row.type == EventType.SANDBOX_LIMIT_WAITING
    ]


async def _sandbox_wait(maker: async_sessionmaker[AsyncSession], world: World) -> Any:
    async with maker() as session:
        return (await sandbox_waits(session, ticket_id=world.ticket_id)).get(world.ticket_id)


async def _runtime_id(maker: async_sessionmaker[AsyncSession], world: World) -> int:
    async with maker() as session:
        return (
            await session.execute(
                select(Runtime.id).where(Runtime.slug == world.runtime_slug)  # type: ignore[attr-defined]
            )
        ).scalar_one()


async def _set_status(world: World, status: StepRunStatus) -> None:
    async with world.sessions() as session:
        await session.execute(
            update(StepRun).where(StepRun.ticket_id == world.ticket_id).values(status=status)
        )


async def test_at_the_organization_limit_nothing_starts_and_no_attempt_is_spent(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    first = await _world(maker, worlds, org, managed)
    second = await _world(maker, worlds, org, managed)
    await run_tick(first)
    assert [run.status for run in await step_runs(first)] == [StepRunStatus.RUNNING]

    await run_tick(second)

    assert await step_runs(second) == []
    assert second.adapter.spawn_attempts == []
    ticket = await ticket_row(second)
    assert (ticket.status, ticket.current_step) == (TicketStatus.RUNNING, "plan")
    assert second.github.issue.labels == {"factorial:plan"}, "its step's label, and no other"
    assert second.projector.comments == []
    assert await _waits(second) == [
        {
            "step": "plan",
            "attempt": 1,
            "scope": "organization",
            "limit": 1,
            "running": 1,
            "runtime": second.runtime_slug,  # type: ignore[attr-defined]
        }
    ]


async def test_the_wait_is_announced_once_per_entry(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    first = await _world(maker, worlds, org, managed)
    second = await _world(maker, worlds, org, managed)
    await run_tick(first)

    await run_tick(second)
    await run_tick(second)
    # A worker restart forgets what it said; the log does not.
    second.deps = replace(second.deps, sandbox_limit_seen={})
    await run_tick(second)

    assert len(await _waits(second)) == 1


class _RolledBack(Exception):
    pass


async def test_a_wait_said_in_a_tick_that_rolled_back_is_said_again(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    first = await _world(maker, worlds, org, managed)
    second = await _world(maker, worlds, org, managed)
    await run_tick(first)
    # A tick that fails after the hold: its transaction, the wait's event included, rolls back.
    with pytest.raises(_RolledBack):
        async with second.sessions() as session:
            ticket = await session.get(Ticket, second.ticket_id)
            assert ticket is not None
            await _ensure_leased(session, ticket)
            await reconcile_ticket(
                session, ticket, second.deps, lease_owner=_LEASE_OWNER, lease_ttl=_LEASE_TTL
            )
            raise _RolledBack
    second.deps.sandbox_starts.release(second.ticket_id)
    assert await _waits(second) == []

    await run_tick(second)
    await run_tick(second)

    assert len(await _waits(second)) == 1


async def test_a_held_retry_announces_once_with_its_own_attempt(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """The ticket page reads the newest wait: one naming a started attempt would look as if the
    ticket were not waiting."""
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    failing = await _world(maker, worlds, org, managed)
    holder = await _world(maker, worlds, org, managed)
    taker = await _world(maker, worlds, org, managed)
    await run_tick(holder)
    await run_tick(failing)
    holder.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(holder)
    await run_tick(failing)
    failing.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(failing)
    await run_tick(taker)
    failing.clock.advance(timedelta(minutes=11))

    await run_tick(failing)
    await run_tick(failing)
    failing.deps = replace(failing.deps, sandbox_limit_seen={})
    await run_tick(failing)

    assert [wait["attempt"] for wait in await _waits(failing)] == [1, 2]
    assert [run.attempt for run in await step_runs(failing)] == [1]
    assert (await _sandbox_wait(maker, failing)) is not None


async def test_a_freed_slot_starts_the_waiting_ticket(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    first = await _world(maker, worlds, org, managed)
    second = await _world(maker, worlds, org, managed)
    await run_tick(first)
    await run_tick(second)
    assert await step_runs(second) == []

    first.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(first)
    await run_tick(second)

    (started,) = await step_runs(second)
    assert (started.attempt, started.status) == (1, StepRunStatus.RUNNING)


async def _waits_for_a_person(world: World) -> None:
    world.adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Ship it?")
    await run_tick(world)
    assert [run.status for run in await step_runs(world)] == [StepRunStatus.IDLE]


async def test_a_session_waiting_for_a_person_holds_no_place(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """Lanes wait on people often: counted, two waiting tickets would hold every other ticket of
    an organization at the default limit."""
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    waiting = await _world(maker, worlds, org, managed)
    other = await _world(maker, worlds, org, managed)
    await run_tick(waiting)
    await _waits_for_a_person(waiting)

    await run_tick(other)

    assert await _live([other]) == [other]
    assert await _waits(other) == []


async def test_a_session_working_again_counts_and_holds_new_starts(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """Its answer is not asked about the limit, so the organization can run over it for a while;
    a new start waits until the active runs fall below it."""
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    answered = await _world(maker, worlds, org, managed)
    started = await _world(maker, worlds, org, managed)
    held = await _world(maker, worlds, org, managed)
    await run_tick(answered)
    await _waits_for_a_person(answered)
    await run_tick(started)

    answered.adapter.go_active()
    await run_tick(answered)
    await run_tick(held)

    assert await _live([answered, started]) == [answered, started]
    assert await step_runs(held) == []
    (wait,) = await _waits(held)
    assert (wait["scope"], wait["limit"], wait["running"]) == ("organization", 1, 2)


async def test_the_pool_limit_stops_another_organization(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org_a, slug_a = await _organization(maker)
    org_b, slug_b = await _organization(maker)
    async with maker() as session:
        # The database is shared: other tests' live managed runs are in the pool too.
        pool = await managed_running(session) + 1
    managed = _managed(org_limits={slug_a: 5, slug_b: 5}, pool_limit=pool)
    first = await _world(maker, worlds, org_a, managed)
    other = await _world(maker, worlds, org_b, managed)
    await run_tick(first)

    await run_tick(other)

    assert await step_runs(other) == []
    (wait,) = await _waits(other)
    assert (wait["scope"], wait["limit"], wait["running"]) == ("pool", pool, pool)


async def test_a_limit_of_zero_starts_nothing_and_says_so(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    world = await _world(maker, worlds, org, _managed(org_limits={slug: 0}, pool_limit=1000))

    await run_tick(world)

    assert await step_runs(world) == []
    (wait,) = await _waits(world)
    assert (wait["scope"], wait["limit"], wait["running"]) == ("organization", 0, 0)


async def test_an_own_key_runtime_is_never_counted_or_gated(
    maker: async_sessionmaker[AsyncSession], worlds: list[World], master_key: None
) -> None:
    """Its key may even be the managed key's value: what the row says decides, never the key."""
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    own = await _world(maker, worlds, org, managed, own_key=MANAGED_KEY)
    on_managed = await _world(maker, worlds, org, managed)
    own_again = await _world(maker, worlds, org, managed, own_key=MANAGED_KEY)

    await run_tick(own)
    await run_tick(on_managed)
    await run_tick(own_again)

    for world in (own, on_managed, own_again):
        assert [run.status for run in await step_runs(world)] == [StepRunStatus.RUNNING]
        assert await _waits(world) == []


async def test_a_persons_retry_at_the_limit_is_refused_with_its_reason(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    failed = await _world(maker, worlds, org, managed)
    other = await _world(maker, worlds, org, managed)
    await run_tick(failed)
    failed.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(failed)
    (attempt_1,) = await step_runs(failed)
    await run_tick(other)
    await _press(failed, attempt_1)

    await run_tick(failed)

    assert [run.id for run in await step_runs(failed)] == [attempt_1.id]
    assert (await ticket_row(failed)).status == TicketStatus.RUNNING
    (refused,) = [
        row.details for row in await event_rows(failed) if row.type == "step_retry_refused"
    ]
    assert refused["reason"] == "sandbox_limit"


async def test_one_pass_starts_exactly_the_limit_however_many_tickets_are_startable(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """A pass reconciles its tickets at once, and a start's row is invisible to the others until
    its transaction commits after the spawn: counting committed rows alone starts every one."""
    gate = _Gate()
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 2}, pool_limit=1000)
    tickets = await _gated_worlds(maker, worlds, gate, [org] * 4, managed)

    await _one_pass(tickets, gate)

    started = await _live(tickets)
    assert len(started) == 2
    for world in tickets:
        assert len(await _waits(world)) == (0 if world in started else 1)


async def test_the_pool_limit_is_exact_across_organizations_in_one_pass(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    gate = _Gate()
    org_a, slug_a = await _organization(maker)
    org_b, slug_b = await _organization(maker)
    async with maker() as session:
        pool = await managed_running(session) + 2
    managed = _managed(org_limits={slug_a: 5, slug_b: 5}, pool_limit=pool)
    tickets = await _gated_worlds(maker, worlds, gate, [org_a, org_a, org_b, org_b], managed)

    await _one_pass(tickets, gate)

    started = await _live(tickets)
    assert len(started) == 2
    held = [wait for world in tickets if world not in started for wait in await _waits(world)]
    assert [wait["scope"] for wait in held] == ["pool", "pool"]


async def test_a_start_that_rolls_back_frees_its_place(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    gate = _Gate()
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    failing, waiting = await _gated_worlds(maker, worlds, gate, [org, org], managed)
    # An infrastructure fault: the tick rolls back and leaves no row.
    failing.adapter.fail_spawn(SpawnError("the substrate is down"))

    await _one_pass([failing], gate)
    await _one_pass([waiting], gate)

    assert await step_runs(failing) == []
    assert await _live([waiting]) == [waiting]


async def test_a_slot_freed_after_a_pass_starts_a_waiting_ticket(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    gate = _Gate()
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    tickets = await _gated_worlds(maker, worlds, gate, [org, org], managed)
    await _one_pass(tickets, gate)
    (started,) = await _live(tickets)
    (waiting,) = [world for world in tickets if world is not started]

    started.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await _one_pass([started], gate)
    await _one_pass([waiting], gate)

    assert await _live([waiting]) == [waiting]


async def test_a_retry_held_by_its_start_is_refused_for_the_limit(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit can be met between the Retry's own check and its start. That is still the
    limit, not a lost race, and the refusal is the only word on it."""
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    failed = await _world(maker, worlds, org, managed)
    other = await _world(maker, worlds, org, managed)
    await run_tick(failed)
    failed.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(failed)
    (attempt_1,) = await step_runs(failed)
    await run_tick(other)
    await _press(failed, attempt_1)

    async def passes(*_: object) -> None:
        return None

    monkeypatch.setattr(retries_module, "_held_by_limit", passes)
    await run_tick(failed)

    assert [run.id for run in await step_runs(failed)] == [attempt_1.id]
    (refused,) = [
        row.details for row in await event_rows(failed) if row.type == "step_retry_refused"
    ]
    assert refused["reason"] == "sandbox_limit"
    assert await _waits(failed) == []


async def test_a_ticket_is_not_waiting_without_a_held_start(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    world = await _world(maker, worlds, org, _managed(org_limits={slug: 1}, pool_limit=1000))

    assert await _sandbox_wait(maker, world) is None
    await run_tick(world)
    assert await _sandbox_wait(maker, world) is None


async def test_a_held_ticket_reads_its_wait_for_each_scope_until_its_run_exists(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 1}, pool_limit=1000)
    first = await _world(maker, worlds, org, managed)
    second = await _world(maker, worlds, org, managed)
    await run_tick(first)
    await run_tick(second)

    wait = await _sandbox_wait(maker, second)
    assert wait is not None
    assert (wait.scope, wait.limit, wait.running) == ("organization", 1, 1)
    assert (wait.runtime, wait.runtime_id) == (
        second.runtime_slug,  # type: ignore[attr-defined]
        await _runtime_id(maker, second),
    )
    async with maker() as session:
        board = {row.ticket_id: row for row in await factory_overview(session, 7, org)}
    assert board[second.ticket_id].sandbox_wait == wait
    assert board[first.ticket_id].sandbox_wait is None

    await _set_status(first, StepRunStatus.CANCELLED)
    await run_tick(second)
    assert await _sandbox_wait(maker, second) is None


async def test_a_pool_wait_and_a_limit_of_zero_read_with_their_scope(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org_a, slug_a = await _organization(maker)
    org_b, slug_b = await _organization(maker)
    async with maker() as session:
        pool = await managed_running(session) + 1
    managed = _managed(org_limits={slug_a: 5, slug_b: 0}, pool_limit=pool)
    running = await _world(maker, worlds, org_a, managed)
    full = await _world(maker, worlds, org_a, managed)
    zero = await _world(maker, worlds, org_b, managed)
    await run_tick(running)
    await run_tick(full)
    await run_tick(zero)

    held = await _sandbox_wait(maker, full)
    none = await _sandbox_wait(maker, zero)
    assert held is not None and none is not None
    assert (held.scope, held.limit) == ("pool", pool)
    assert (none.scope, none.limit) == ("organization", 0)


async def test_a_ticket_that_left_the_held_step_is_not_waiting(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    org, slug = await _organization(maker)
    managed = _managed(org_limits={slug: 0}, pool_limit=1000)
    world = await _world(maker, worlds, org, managed)
    await run_tick(world)
    assert await _sandbox_wait(maker, world) is not None

    async with world.sessions() as session:
        await append(
            session,
            type=EventType.STEP_ENTERED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step="plan",
        )
    assert await _sandbox_wait(maker, world) is None
    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(current_step="spec_gate")
        )
    assert await _sandbox_wait(maker, world) is None
