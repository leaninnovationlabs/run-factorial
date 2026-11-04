"""Inspect shells (decision 57): Open shell as a request the API records, and the
deployment-wide pass that answers it and watches every open shell, done tickets included."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import (
    Event,
    Runtime,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.previews import awake_workspaces
from orchestrator.events.terminals import SWITCHED_OFF
from orchestrator.secrets.store import put_for_runtime
from orchestrator.settings import Settings, TerminalSettings
from orchestrator.worker.context import SessionPolicy
from orchestrator.worker.inspect import INSPECT_LIMIT, ViewClosed, reconcile_inspect_shells
from orchestrator.worker.runtime_adapters import RuntimeRefused
from runtime.contract import (
    InspectLoginRefused,
    InspectNoSession,
    InspectPhase,
    InspectState,
    InspectUnavailable,
    RuntimeAdapter,
    TerminalTarget,
)
from tests.api_harness import build_app, client_for, make_settings
from tests.reconcile_world import (
    SERVICES,
    FakeAdapter,
    InspectAdapter,
    World,
    _build_world,
    advance_to_gate,
    previewed_process,
    run_tick,
    store_secret,
    world_runtime_id,
)
from tests.terminal_rows import record, seed_run

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

ON = SessionPolicy(terminal_enabled=True)
SHELL = TerminalTarget(substrate="fake", socket="/fake/inspect.sock", session="inspect")
MARKER = "agent-wrote-this-7f3c"


@pytest.fixture(autouse=True)
def _no_deployment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test database is shared and a World commits, so another test's awake preview or open
    shell would count toward this deployment's cap. The cap's own tests set it."""
    monkeypatch.setattr("orchestrator.worker.inspect.MAX_AWAKE_WORKSPACES", 10**6)
    monkeypatch.setattr("orchestrator.api.routes.runs.MAX_AWAKE_WORKSPACES", 10**6)


@pytest.fixture(autouse=True)
def _a_stored_credential(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run has a model credential unless the test asks for the real store (`master_key`),
    where what a runtime holds is the point."""
    if "master_key" in request.fixturenames:
        return

    async def held(session: AsyncSession, run: StepRun) -> dict[str, SecretStr]:
        return {"ANTHROPIC_API_KEY": SecretStr("sk-held")}

    monkeypatch.setattr("orchestrator.worker.inspect.view_credentials", held)


# --------------------------------------------------------------------------- the pass


async def _world(
    maker: async_sessionmaker[AsyncSession], *, done: bool = True
) -> tuple[World, InspectAdapter]:
    """A world whose ticket is done unless asked otherwise. The pass reaches a done ticket, and
    nothing leases one, so these tickets stay out of other tests' lease batches in the shared
    database."""
    world = await _build_world(maker, adapter_cls=InspectAdapter, sessions=ON)
    if done:
        await _done(world)
    adapter = world.adapter
    assert isinstance(adapter, InspectAdapter)
    adapter.clock = world.clock
    return world, adapter


async def _done(world: World) -> None:
    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(status=TicketStatus.DONE)
        )


async def _kept_run(
    world: World,
    *,
    status: StepRunStatus = StepRunStatus.SUCCEEDED,
    ended_ago: timedelta = timedelta(0),
    workspace_run_id: str | None = None,
    step: str = "plan",
) -> StepRun:
    """An agent run of the world's ticket, spawned in its own workspace unless one is named."""
    runtime_id = await world_runtime_id(world)
    async with world.sessions() as session:
        attempt = (
            await session.execute(
                select(func.count()).where(
                    StepRun.ticket_id == world.ticket_id, StepRun.step_name == step
                )
            )
        ).scalar_one() + 1
        live = status in (StepRunStatus.RUNNING, StepRunStatus.IDLE)
        run = StepRun(
            ticket_id=world.ticket_id,
            step_name=step,
            kind=StepKind.AGENT,
            attempt=attempt,
            status=status,
            runtime_id=runtime_id,
            runtime_run_id=f"run-{step}-{attempt}",
            started_at=world.clock() - ended_ago,
            ended_at=None if live else world.clock() - ended_ago,
        )
        session.add(run)
        await session.flush()
        await record(
            session,
            run,
            EventType.AGENT_SPAWNED,
            **({"workspace_run_id": workspace_run_id} if workspace_run_id else {}),
        )
        return run


async def _ticket(world: World) -> Ticket:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        return ticket


async def _request(world: World, run: StepRun, by: str = "dev@example.com") -> int:
    async with world.sessions() as session:
        event = await append(
            session,
            type=EventType.INSPECT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            requested_by=by,
        )
        return event.id


def _scoped(world: World, adapter: RuntimeAdapter | None = None) -> Any:
    """The world's own adapter for its own runs. The pass is deployment-wide and the database is
    shared: another test's leftover request must never reach this test's fake."""

    async def adapter_for_run(session: AsyncSession, run: StepRun) -> RuntimeAdapter:
        if run.ticket_id != world.ticket_id:
            raise RuntimeRefused("another test's run")
        return adapter if adapter is not None else await world.deps.adapter_for_run(session, run)

    return adapter_for_run


async def _pass(
    world: World, *, policy: SessionPolicy = ON, adapter: RuntimeAdapter | None = None
) -> None:
    async with world.sessions() as session:
        await reconcile_inspect_shells(
            session, adapter_for_run=_scoped(world, adapter), policy=policy, now=world.clock()
        )


async def _shell_events(world: World, run: StepRun) -> list[tuple[str, dict[str, Any]]]:
    async with world.sessions() as session:
        rows = (
            await session.scalars(
                select(Event)
                .where(Event.step_run_id == run.id, Event.type.like("inspect_%"))
                .order_by(Event.id)
            )
        ).all()
        return [(row.type, row.details) for row in rows]


async def _opened(world: World, run: StepRun) -> int:
    request_id = await _request(world, run)
    await _pass(world)
    assert (await _shell_events(world, run))[-1][0] == EventType.INSPECT_STARTED
    return request_id


async def test_a_done_tickets_kept_run_opens_a_shell(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Silent and costly: no ticket's pass reaches a done ticket, and decision 54 keeps its
    workspace so a merged change can still be opened. A pass that rode the ticket's would leave
    this request starting for ever."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    assert (await _ticket(world)).status == TicketStatus.DONE
    request_id = await _request(world, run)

    await _pass(world)

    assert adapter.inspect_starts == [(run.runtime_run_id, str(request_id), INSPECT_LIMIT)]
    events = await _shell_events(world, run)
    assert [kind for kind, _ in events] == ["inspect_requested", "inspect_started"]
    started = events[1][1]
    assert started["terminal"] == SHELL.model_dump()
    assert started["request_id"] == request_id
    assert started["requested_by"] == "dev@example.com"


async def test_a_view_on_a_runtime_gets_only_the_credential_its_type_names(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    await store_secret(world, "ANTHROPIC_API_KEY", "sk-project-copy")
    async with world.sessions() as session:
        runtime = Runtime(
            organization_id=await default_organization_id(session),
            slug=f"view-{world.ticket_id}",
            name="view",
            type="claude_api",
            details={},
        )
        session.add(runtime)
        await session.flush()
        await session.execute(
            update(StepRun).where(StepRun.id == run.id).values(runtime_id=runtime.id)
        )
        await put_for_runtime(session, runtime.id, "ANTHROPIC_API_KEY", "sk-runtime")
        await put_for_runtime(session, runtime.id, "E2B_API_KEY", "e2b_key")
        await put_for_runtime(session, runtime.id, "GH_TOKEN", "ghp_held")
        await put_for_runtime(session, runtime.id, "GITHUB_TOKEN", "ghp_stray")
    await _request(world, run)

    await _pass(world, adapter=adapter)

    [credentials] = adapter.inspect_credentials
    assert {name: value.get_secret_value() for name, value in credentials.items()} == {
        "ANTHROPIC_API_KEY": "sk-runtime"
    }


@pytest.mark.parametrize(
    "case", ["switched off", "quarantined", "live", "continued", "released", "not an inspect host"]
)
async def test_the_pass_refuses_a_request_nothing_can_honour(
    maker: async_sessionmaker[AsyncSession], case: str
) -> None:
    world, adapter = await _world(maker)
    run = await _kept_run(
        world,
        status=StepRunStatus.RUNNING if case == "live" else StepRunStatus.SUCCEEDED,
        ended_ago=ON.retention + timedelta(days=1) if case == "released" else timedelta(0),
    )
    if case == "continued":
        await _kept_run(world, status=StepRunStatus.RUNNING, workspace_run_id=run.runtime_run_id)
    if case == "quarantined":
        async with world.sessions() as session:
            await session.execute(
                update(Ticket)
                .where(Ticket.id == world.ticket_id)
                .values(quarantined_at=datetime.now(UTC))
            )
    request_id = await _request(world, run)

    await _pass(
        world,
        policy=SessionPolicy() if case == "switched off" else ON,
        adapter=FakeAdapter() if case == "not an inspect host" else None,
    )

    assert adapter.inspect_starts == []
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked" and details["failed"] is True
    assert details["request_id"] == request_id
    expected = {
        "switched off": ViewClosed.TERMINAL_OFF,
        "quarantined": ViewClosed.QUARANTINED,
        "live": ViewClosed.LIVE,
        "continued": ViewClosed.CONTINUED,
        "released": ViewClosed.RELEASED,
        "not an inspect host": ViewClosed.UNSUPPORTED,
    }[case]
    assert details["reason"] == expected


async def test_a_request_is_answered_once_across_passes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The request's id is the token: a shell still starting on the next pass is not started
    again, and one that opened is recorded once."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    adapter.opens_as = InspectPhase.STARTING
    request_id = await _request(world, run)

    await _pass(world)
    await _pass(world)
    assert len(adapter.inspect_starts) == 1
    assert [kind for kind, _ in await _shell_events(world, run)] == ["inspect_requested"]

    assert run.runtime_run_id is not None
    adapter.shells[run.runtime_run_id] = InspectState(
        phase=InspectPhase.OPEN, token=str(request_id), target=SHELL, started_at=world.clock()
    )
    await _pass(world)
    await _pass(world)

    assert len(adapter.inspect_starts) == 1
    assert [kind for kind, _ in await _shell_events(world, run)] == [
        "inspect_requested",
        "inspect_started",
    ]


async def test_previews_and_shells_share_the_awake_cap(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An awake preview and an open shell each hold a workspace awake: with the cap at exactly
    those two, a third workspace's shell is refused. Not done: a done ticket serves no preview."""
    world, adapter = await _world(maker, done=False)
    try:
        previewed, shelled, wanted = (
            await _kept_run(world, step="plan"),
            await _kept_run(world, step="build"),
            await _kept_run(world, step="review"),
        )
        async with world.sessions() as session:
            for kind in (EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED):
                await record(session, previewed, kind)
        await _opened(world, shelled)
        async with world.sessions() as session:
            others = len(await awake_workspaces(session) - {previewed.id, shelled.id})
        monkeypatch.setattr("orchestrator.worker.inspect.MAX_AWAKE_WORKSPACES", others + 2)
        await _request(world, wanted)

        await _pass(world)

        kind, details = (await _shell_events(world, wanted))[-1]
        assert kind == "inspect_parked" and details["failed"] is True
        assert details["reason"] == ViewClosed.TOO_MANY_AWAKE
        assert details["limit"] == others + 2
        assert [run_id for run_id, _, _ in adapter.inspect_starts] == [shelled.runtime_run_id]
    finally:
        await _done(world)


async def test_an_open_shell_counts_against_a_preview_start(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _build_world(
        maker, config=previewed_process(**SERVICES), adapter_cls=InspectAdapter, sessions=ON
    )
    try:
        await advance_to_gate(world)
        shelled = await _kept_run(world, step="build")
        async with world.sessions() as session:
            await record(session, shelled, EventType.INSPECT_STARTED, terminal=SHELL.model_dump())
            plan = (
                await session.scalars(
                    select(StepRun).where(
                        StepRun.ticket_id == world.ticket_id, StepRun.step_name == "plan"
                    )
                )
            ).one()
            await record(session, plan, EventType.PREVIEW_REQUESTED, requested_by="reviewer")
            others = len(await awake_workspaces(session) - {plan.id})
        monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", others)

        await run_tick(world)

        adapter = world.adapter
        assert isinstance(adapter, InspectAdapter)
        assert adapter.preview_starts == []
        async with world.sessions() as session:
            failed = (
                await session.scalars(
                    select(Event).where(
                        Event.step_run_id == plan.id, Event.type == EventType.PREVIEW_FAILED
                    )
                )
            ).one()
        assert "already running" in failed.details["reason"]
    finally:
        await _done(world)


async def _lease(world: World, owner: str | None) -> None:
    async with world.sessions() as session:
        await session.execute(
            update(Ticket)
            .where(Ticket.id == world.ticket_id)
            .values(
                lease_owner=owner,
                lease_expires_at=None if owner is None else func.now() + timedelta(minutes=5),
            )
        )


async def test_a_held_lease_defers_a_start_and_a_stop(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rework adopts the workspace under the ticket's lease, kills the shell, and records the
    adoption only once its spawn returns: a shell started meanwhile would open beside the run
    taking it over, and a stop would pause the sandbox that run is starting in."""
    world, adapter = await _world(maker, done=False)
    try:
        run = await _kept_run(world)
        assert run.runtime_run_id is not None
        await _lease(world, "another-pass")
        await _request(world, run)

        await _pass(world)

        assert adapter.inspect_starts == []
        assert [kind for kind, _ in await _shell_events(world, run)] == ["inspect_requested"]

        await _lease(world, None)
        await _pass(world)

        assert len(adapter.inspect_starts) == 1
        assert (await _shell_events(world, run))[-1][0] == "inspect_started"

        await _lease(world, "another-pass")
        adapter.shells[run.runtime_run_id] = InspectState(phase=InspectPhase.STOPPED)
        await _pass(world)

        assert adapter.inspect_stops == []
        assert (await _shell_events(world, run))[-1][0] == "inspect_started"

        await _lease(world, None)
        await _pass(world)

        assert adapter.inspect_stops == [run.runtime_run_id]
        assert (await _shell_events(world, run))[-1][0] == "inspect_parked"
    finally:
        await _done(world)


async def test_an_idle_session_with_no_clients_parks(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    request_id = await _opened(world, run)
    assert run.runtime_run_id is not None
    open_shell = adapter.shells[run.runtime_run_id]

    adapter.shells[run.runtime_run_id] = open_shell.model_copy(
        update={"clients": 1, "last_activity": world.clock()}
    )
    world.clock.advance(ON.idle_park_after * 2)
    await _pass(world)
    assert (await _shell_events(world, run))[-1][0] == "inspect_started", "a viewer keeps it"

    adapter.shells[run.runtime_run_id] = open_shell.model_copy(
        update={"clients": 0, "last_activity": world.clock()}
    )
    world.clock.advance(ON.idle_park_after - timedelta(seconds=1))
    await _pass(world)
    assert (await _shell_events(world, run))[-1][0] == "inspect_started"

    world.clock.advance(timedelta(seconds=1))
    await _pass(world)

    assert adapter.inspect_stops == [run.runtime_run_id]
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked" and details["reason"] == ViewClosed.UNWATCHED
    assert details["after_seconds"] == ON.idle_park_after.total_seconds()
    assert details["request_id"] == request_id and "failed" not in details


async def _close_request(world: World, run: StepRun, by: str = "operator@example.com") -> None:
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INSPECT_CLOSE_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            requested_by=by,
        )


async def test_a_close_request_parks_an_open_view_and_frees_its_slot(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Silent and costly: a view someone closed that kept its slot would block the next Open
    session for ten minutes, and a viewer still attached would keep it from idling."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    request_id = await _opened(world, run)
    assert run.runtime_run_id is not None
    adapter.shells[run.runtime_run_id] = adapter.shells[run.runtime_run_id].model_copy(
        update={"clients": 2, "last_activity": world.clock()}
    )
    async with world.sessions() as session:
        assert run.id in await awake_workspaces(session)

    await _close_request(world, run)
    await _pass(world)

    assert adapter.inspect_stops == [run.runtime_run_id]
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked" and details["reason"] == ViewClosed.CLOSED
    assert details["closed_by"] == "operator@example.com"
    assert details["request_id"] == request_id and "failed" not in details
    async with world.sessions() as session:
        assert run.id not in await awake_workspaces(session)


async def test_a_close_request_while_a_view_starts_ends_it_unopened(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    adapter.opens_as = InspectPhase.STARTING
    request_id = await _request(world, run)
    await _pass(world)

    await _close_request(world, run)
    await _pass(world)

    assert adapter.inspect_stops == [run.runtime_run_id]
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked" and details["reason"] == ViewClosed.CLOSED
    assert details["closed_by"] == "operator@example.com"
    assert details["request_id"] == request_id and "failed" not in details


async def test_a_close_request_does_not_end_a_view_opened_after_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A close is answered by the park that follows it: the next Open session starts fresh."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    await _opened(world, run)
    await _close_request(world, run)
    await _pass(world)
    assert (await _shell_events(world, run))[-1][0] == "inspect_parked"
    stops = list(adapter.inspect_stops)

    await _opened(world, run)
    await _pass(world)

    assert (await _shell_events(world, run))[-1][0] == "inspect_started"
    assert adapter.inspect_stops == stops


async def test_a_shell_parks_at_its_limit(maker: async_sessionmaker[AsyncSession]) -> None:
    """A viewer typing keeps it from idling, never past the limit: E2B's session cap parks the
    sandbox under it."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    await _opened(world, run)
    assert run.runtime_run_id is not None
    open_shell = adapter.shells[run.runtime_run_id]

    world.clock.advance(INSPECT_LIMIT - timedelta(seconds=1))
    adapter.shells[run.runtime_run_id] = open_shell.model_copy(
        update={"clients": 1, "last_activity": world.clock()}
    )
    await _pass(world)
    assert adapter.inspect_stops == []

    world.clock.advance(timedelta(seconds=1))
    await _pass(world)

    assert adapter.inspect_stops == [run.runtime_run_id]
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked" and details["reason"] == ViewClosed.LIMIT
    assert details["limit_seconds"] == INSPECT_LIMIT.total_seconds()


async def test_a_later_attempt_in_the_workspace_parks_the_shell(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The rework that resumed in the workspace ended the shell before it wrote a credential;
    a stop now would park the sandbox the new attempt works in."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    await _opened(world, run)
    await _kept_run(world, status=StepRunStatus.RUNNING, workspace_run_id=run.runtime_run_id)

    await _pass(world)

    kind, details = (await _shell_events(world, run))[-1]
    assert kind == "inspect_parked"
    assert details["reason"] == ViewClosed.CONTINUED
    assert adapter.inspect_stops == []


async def test_a_sandboxs_own_words_never_reach_an_event(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`InspectState.detail`, and an `InspectUnavailable`'s message, are read from a machine the
    agent controlled."""
    world, adapter = await _world(maker)
    failing, exiting = await _kept_run(world, step="plan"), await _kept_run(world, step="build")
    refused = await _kept_run(world, step="review")
    adapter.opens_as, adapter.detail = InspectPhase.FAILED, MARKER
    await _request(world, failing)
    await _pass(world)
    adapter.inspect_error = InspectUnavailable(f"sandbox said {MARKER}")
    await _request(world, refused)
    await _pass(world)
    adapter.opens_as, adapter.detail, adapter.inspect_error = InspectPhase.OPEN, None, None
    await _opened(world, exiting)
    assert exiting.runtime_run_id is not None
    adapter.shells[exiting.runtime_run_id] = InspectState(
        phase=InspectPhase.STOPPED, token="x", detail=MARKER
    )

    await _pass(world)

    assert (await _shell_events(world, failing))[-1][1]["failed"] is True
    assert (await _shell_events(world, refused))[-1][1]["failed"] is True
    assert (await _shell_events(world, exiting))[-1][0] == "inspect_parked"
    async with world.sessions() as session:
        details = (
            await session.scalars(select(Event.details).where(Event.ticket_id == world.ticket_id))
        ).all()
    assert all(MARKER not in json.dumps(row) for row in details)


# --------------------------------------------------------------------------- the API


def _settings(*, enabled: bool = True) -> Settings:
    return make_settings().model_copy(update={"terminal": TerminalSettings(enabled=enabled)})


async def _requests(session: AsyncSession, run: StepRun) -> int:
    return (
        await session.execute(
            select(func.count()).where(
                Event.step_run_id == run.id, Event.type == EventType.INSPECT_REQUESTED
            )
        )
    ).scalar_one()


@pytest.mark.parametrize("case", ["quarantined", "switched off", "live", "released"])
async def test_inspect_is_refused_when_quarantined_or_switched_off_or_live(
    async_session: AsyncSession, auth: dict[str, str], case: str
) -> None:
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.RUNNING if case == "live" else StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.DONE,
        quarantined=case == "quarantined",
    )
    if case == "released":
        run.ended_at = datetime.now(UTC) - timedelta(days=30)
    await async_session.commit()
    app = build_app(_settings(enabled=case != "switched off"), async_session)

    async with client_for(app) as client:
        response = await client.post(f"/api/step-runs/{run.id}/inspect", headers=auth)
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert response.status_code == 409
    assert await _requests(async_session, run) == 0
    assert read["inspect"]["startable"] is False and read["inspect"]["unavailable"]


async def test_a_second_open_shell_request_while_pending_records_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.DONE,
    )
    await async_session.commit()
    url = f"/api/step-runs/{run.id}"

    async with client_for(build_app(_settings(), async_session)) as client:
        before = (await client.get(f"{url}/terminal", headers=auth)).json()
        first = await client.post(f"{url}/inspect", headers=auth)
        second = await client.post(f"{url}/inspect", headers=auth)

    assert before["inspect"] == {
        "status": "none",
        "reason": None,
        "startable": True,
        "unavailable": None,
        "reason_details": {},
    }
    assert before["kept_until"] is not None
    assert first.status_code == second.status_code == 202
    assert second.json()["inspect"]["status"] == "starting"
    assert await _requests(async_session, run) == 1


async def _close_requests(session: AsyncSession, run: StepRun) -> list[Event]:
    return list(
        (
            await session.scalars(
                select(Event).where(
                    Event.step_run_id == run.id, Event.type == EventType.INSPECT_CLOSE_REQUESTED
                )
            )
        ).all()
    )


async def test_closing_a_view_records_who_asked_and_closing_none_records_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.DONE,
    )
    await async_session.commit()
    url = f"/api/step-runs/{run.id}"

    async with client_for(build_app(_settings(), async_session)) as client:
        none = await client.post(f"{url}/inspect/close", headers=auth)
        assert none.status_code == 202 and none.json()["inspect"]["status"] == "none"
        assert await _close_requests(async_session, run) == []

        await client.post(f"{url}/inspect", headers=auth)
        closed = await client.post(f"{url}/inspect/close", headers=auth)

    assert closed.status_code == 202
    [request] = await _close_requests(async_session, run)
    assert request.details["requested_by"]
    assert (request.details["step"], request.details["attempt"]) == (run.step_name, run.attempt)


async def test_closing_a_view_is_refused_while_the_terminal_is_switched_off(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.DONE,
    )
    await async_session.commit()

    async with client_for(build_app(_settings(enabled=False), async_session)) as client:
        response = await client.post(f"/api/step-runs/{run.id}/inspect/close", headers=auth)

    assert response.status_code == 409 and response.json()["detail"] == SWITCHED_OFF
    assert await _close_requests(async_session, run) == []


async def test_a_refused_login_parks_the_view_with_its_fixed_sentence(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The one adapter refusal that reaches an event, and only as the sentence the contract
    fixes: the substrate's own text stays in the log."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    adapter.inspect_error = InspectLoginRefused()
    await _request(world, run)

    await _pass(world)

    kind, details = (await _shell_events(world, run))[-1]
    assert kind == EventType.INSPECT_PARKED
    assert details["reason"] == InspectLoginRefused.MESSAGE
    assert details["failed"] is True


async def test_a_run_that_never_began_a_session_parks_the_view_with_its_fixed_sentence(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run kept from before every run was a session is still offered Open session; the park
    must say why it cannot open, not read as an outage."""
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    adapter.inspect_error = InspectNoSession()
    await _request(world, run)

    await _pass(world)

    kind, details = (await _shell_events(world, run))[-1]
    assert kind == EventType.INSPECT_PARKED
    assert details["reason"] == InspectNoSession.MESSAGE
    assert details["failed"] is True


async def test_an_awake_preview_does_not_refuse_the_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A view and a preview share the sandbox; only the cap of awake sandboxes counts both."""
    world, adapter = await _world(maker, done=False)
    try:
        run = await _kept_run(world)
        async with world.sessions() as session:
            for kind in (EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED):
                await record(session, run, kind)

        await _opened(world, run)

        assert len(adapter.inspect_starts) == 1
    finally:
        await _done(world)


async def test_a_runtime_holding_no_model_credential_is_told_to_store_one(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world, adapter = await _world(maker)
    run = await _kept_run(world)
    async with world.sessions() as session:
        runtime = Runtime(
            organization_id=await default_organization_id(session),
            slug=f"bare-{world.ticket_id}",
            name="bare",
            type="claude_api",
            details={},
        )
        session.add(runtime)
        await session.flush()
        await session.execute(
            update(StepRun).where(StepRun.id == run.id).values(runtime_id=runtime.id)
        )
    await _request(world, run)

    await _pass(world, adapter=adapter)

    assert adapter.inspect_starts == []
    kind, details = (await _shell_events(world, run))[-1]
    assert kind == EventType.INSPECT_PARKED
    assert details["reason"] == ViewClosed.NO_CREDENTIAL


async def test_a_closed_session_view_reaches_the_terminal_tab_with_what_words_it(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The park stores a code; the tab words it, so it needs the code's parameters too, and none
    of the park's other details."""
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.RUNNING,
    )
    await record(async_session, run, EventType.INSPECT_REQUESTED)
    await record(async_session, run, EventType.INSPECT_STARTED)
    await record(
        async_session,
        run,
        EventType.INSPECT_PARKED,
        reason=ViewClosed.CLOSED,
        closed_by="a@x.dev",
        failed=False,
    )
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["inspect"]["reason"] == "closed"
    assert read["inspect"]["reason_details"] == {"closed_by": "a@x.dev"}


async def test_the_terminal_answer_names_where_the_browser_dials_the_gateway(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The bundle holds no terminal origin, so the dashboard dials what this says."""
    run = await seed_run(async_session, target=None, status=StepRunStatus.RUNNING)
    await async_session.commit()
    settings = _settings().model_copy(
        update={"terminal": TerminalSettings(enabled=True, public_url="https://t.example")}
    )

    async with client_for(build_app(settings, async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["gateway_origin"] == "https://t.example"


async def test_an_awake_preview_does_not_stop_open_session_being_offered(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.RUNNING,
    )
    for kind in (EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED):
        await record(async_session, run, kind)
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["inspect"]["startable"] is True
    assert read["inspect"]["unavailable"] is None


async def test_terminal_names_the_attempt_that_continued_the_workspace(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    first = await seed_run(
        async_session,
        target=None,
        status=StepRunStatus.SUCCEEDED,
        ticket_status=TicketStatus.RUNNING,
    )
    later: list[StepRun] = []
    for attempt in (2, 3):
        run = StepRun(
            ticket_id=first.ticket_id,
            step_name="develop",
            kind=StepKind.AGENT,
            attempt=attempt,
            status=StepRunStatus.SUCCEEDED,
            runtime_run_id=f"run-terminal-{attempt}",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
        )
        async_session.add(run)
        await async_session.flush()
        await record(async_session, run, EventType.AGENT_SPAWNED, workspace_run_id="run-terminal")
        later.append(run)
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{first.id}/terminal", headers=auth)).json()
        last = (await client.get(f"/api/step-runs/{later[-1].id}/terminal", headers=auth)).json()

    assert read["continued_by"] == later[-1].id
    assert read["continued_by_attempt"] == 3
    assert read["settled"] is True
    assert last["continued_by"] is None and last["continued_by_attempt"] is None


async def test_terminal_is_unsettled_while_a_live_run_is_not_attachable(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(async_session, target=None, status=StepRunStatus.RUNNING)
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["attachable"] is False
    assert read["settled"] is False


async def test_a_live_run_on_a_quarantined_ticket_is_settled(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(async_session, target=None, quarantined=True)
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["attachable"] is False
    assert read["settled"] is True


async def test_an_ended_kept_run_is_settled(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await seed_run(
        async_session, target=None, status=StepRunStatus.SUCCEEDED, ticket_status=TicketStatus.DONE
    )
    await async_session.commit()

    async with client_for(build_app(_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)).json()

    assert read["settled"] is True
    assert read["continued_by"] is None
