"""Runtime-served previews in the loop (decision 54): the spawn that keeps a workspace, and the
pass that starts, watches and stops a preview beside the ticket's own transition."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.workspaces import WHY_NOT_SERVABLE, kept_workspaces
from runtime.contract import (
    PREVIEW_IDLE,
    PREVIEW_RENEWAL,
    AdapterUnavailable,
    PreviewPhase,
    PreviewState,
    PreviewUnavailable,
    PreviewUrl,
    UnknownRunError,
)
from tests.reconcile_world import (
    SERVICES,
    FakeAdapter,
    PreviewAdapter,
    World,
    _build_world,
    advance_to_gate,
    event_rows,
    previewed_process,
    run_tick,
    step_runs,
    store_secret,
    ticket_row,
)
from tests.test_reconcile_restart import ask_restart, new_head

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture(autouse=True)
def _no_deployment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test database is shared and a World commits, so another test's awake preview would
    count toward this deployment's limit. The limit's own test sets it back."""
    monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", 10**6)


async def _world(
    maker: async_sessionmaker[AsyncSession],
    adapter: type[FakeAdapter] = PreviewAdapter,
    **preview: Any,
) -> World:
    return await _build_world(
        maker, config=previewed_process(**(preview or SERVICES)), adapter_cls=adapter
    )


async def _spawned(world: World) -> dict[str, Any]:
    (event,) = [e for e in await event_rows(world) if e.type == EventType.AGENT_SPAWNED]
    return event.details


async def test_a_step_with_preview_services_keeps_its_workspace_on_a_preview_host(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(maker)

    await run_tick(world)

    assert world.adapter.spawns[0].public is True
    details = await _spawned(world)
    assert details["public"] is True
    assert details["workspace_run_id"] == "run-1"


async def test_a_kept_workspace_is_spawned_with_its_preview_ports(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(maker)

    await run_tick(world)

    assert world.adapter.spawns[0].public_ports == (3000,)


async def test_a_host_adapter_is_never_asked_to_keep_a_workspace(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(maker, adapter=FakeAdapter)

    await run_tick(world)

    assert world.adapter.spawns[0].public is False
    assert world.adapter.spawns[0].public_ports == ()
    assert (await _spawned(world))["public"] is False


async def test_a_producer_published_preview_keeps_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(maker, enabled=True)

    await run_tick(world)

    assert world.adapter.spawns[0].public is False
    assert (await _spawned(world))["public"] is False


# --------------------------------------------------------------------------- the preview pass


async def _at_gate(maker: async_sessionmaker[AsyncSession], **preview: Any) -> World:
    """A kept `plan` run that succeeded, and the ticket waiting at `spec_gate`."""
    world = await _world(maker, **preview)
    await advance_to_gate(world)
    return world


def _adapter(world: World) -> PreviewAdapter:
    assert isinstance(world.adapter, PreviewAdapter)
    return world.adapter


async def _press(world: World, kind: EventType) -> None:
    plan = next(run for run in await step_runs(world) if run.step_name == "plan")
    async with world.sessions() as session:
        await append(
            session,
            type=kind,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=plan.id,
            step="plan",
            attempt=plan.attempt,
            requested_by="reviewer@example.com",
        )
        await session.commit()


async def _preview_events(world: World) -> list[str]:
    return [e.type for e in await event_rows(world) if e.type.startswith("preview_")]


async def _details(world: World, kind: str) -> list[dict[str, Any]]:
    return [e.details for e in await event_rows(world) if e.type == kind]


def _ready(world: World) -> None:
    adapter = _adapter(world)
    adapter.preview = PreviewState(
        phase=PreviewPhase.READY,
        token=adapter.preview.token,
        urls=(PreviewUrl(service="web", url="https://3000-sbx.e2b.app"),),
        head_sha="0123abc",
        uncommitted=True,
    )


async def test_a_start_is_honoured_once_and_its_urls_published_when_ready(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)

    await run_tick(world)
    await run_tick(world)
    _ready(world)
    await run_tick(world)

    adapter = _adapter(world)
    assert len(adapter.preview_starts) == 1, "the token keeps a second pass from restarting"
    run_id, spec, _, _ = adapter.preview_starts[0]
    assert run_id == "run-1"
    assert spec.services[0].name == "web" and spec.services[0].port == 3000
    assert await _preview_events(world) == [
        "preview_requested",
        "preview_url_updated",
        "preview_started",
    ]
    (started,) = await _details(world, "preview_started")
    assert started["uncommitted"] is True and started["head_sha"] == "0123abc"
    assert (await ticket_row(world)).current_step == "spec_gate", "a preview is no transition"


async def test_a_failed_start_is_answered_with_its_reason(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    _adapter(world).preview_error = PreviewUnavailable("run-1's sandbox is gone")

    await run_tick(world)

    (failed,) = await _details(world, "preview_failed")
    assert "sandbox is gone" in failed["reason"]


async def test_a_failed_preview_is_logged_by_its_reasons_first_line_alone(
    maker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """The deployment's log gets a line to find a failure by. The sandbox's log text after it,
    which a service run with the preview's secrets may have printed, stays in the event."""
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    adapter = _adapter(world)
    adapter.preview = PreviewState(
        phase=PreviewPhase.FAILED,
        token=adapter.preview.token,
        detail="service web did not answer / on port 3000 within 0:02:00:\nADMIN_PASSWORD=hunter2",
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.worker.previews"):
        await run_tick(world)

    (failed,) = await _details(world, "preview_failed")
    assert "hunter2" in failed["reason"]
    plan = next(run for run in await step_runs(world) if run.step_name == "plan")
    key = (await ticket_row(world)).external_key
    [message] = [r.getMessage() for r in caplog.records if r.name == "orchestrator.worker.previews"]
    assert message == (
        f"ticket {key} run {plan.id}: the preview failed: service web did not answer / on port "
        "3000 within 0:02:00:"
    )


async def test_preview_env_secrets_are_rendered_from_the_project(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world = await _at_gate(
        maker, **SERVICES, env={"ADMIN_PASSWORD": "{secrets.PREVIEW_PASSWORD}"}
    )
    await store_secret(world, "PREVIEW_PASSWORD", "hunter2-preview")
    await _press(world, EventType.PREVIEW_REQUESTED)

    await run_tick(world)

    assert _adapter(world).preview_starts[0][2] == {"ADMIN_PASSWORD": "hunter2-preview"}


async def test_a_missing_preview_secret_fails_the_start_by_name(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(
        maker, **SERVICES, env={"ADMIN_PASSWORD": "{secrets.PREVIEW_NOT_STORED}"}
    )
    await _press(world, EventType.PREVIEW_REQUESTED)

    await run_tick(world)

    (failed,) = await _details(world, "preview_failed")
    assert "PREVIEW_NOT_STORED" in failed["reason"]
    assert _adapter(world).preview_starts == []


async def test_a_stop_parks_the_preview_and_withdraws_its_urls(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    await _press(world, EventType.PREVIEW_STOP_REQUESTED)

    await run_tick(world)

    assert _adapter(world).preview_stops == ["run-1"]
    assert (await _preview_events(world))[-2:] == ["preview_url_updated", "preview_stopped"]
    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == "stopped by reviewer@example.com"


async def test_a_preview_the_adapter_stopped_is_recorded_as_stopped(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    adapter = _adapter(world)
    adapter.preview = PreviewState(
        phase=PreviewPhase.STOPPED,
        token=adapter.preview.token,
        detail="stopped after its stopAfter",
    )

    await run_tick(world)

    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == "stopped after its stopAfter"


async def test_a_ticket_that_finishes_parks_its_awake_preview_and_keeps_the_workspace(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """No pass reaches a done ticket, so nothing else would park it before E2B's own timeout
    kills the sandbox."""
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    world.github.approve("reviewer-1")

    for _ in range(3):
        await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.DONE

    assert _adapter(world).preview_stops == ["run-1"]
    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == WHY_NOT_SERVABLE["done"]
    async with world.sessions() as session:
        kept = await kept_workspaces(
            session, world.clock.now, timedelta(days=7), ticket_id=world.ticket_id
        )
    assert "run-1" in kept.keep


async def test_a_finishing_ticket_whose_preview_run_is_unknown_still_finishes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run whose state is gone has nothing left to park."""
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    world.github.approve("reviewer-1")

    async def unknown(run_id: str) -> None:
        raise UnknownRunError(run_id)

    _adapter(world).preview_stop = unknown
    for _ in range(3):
        await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.DONE
    assert len(await _details(world, "preview_stopped")) == 1


async def test_a_start_past_the_deployment_limit_is_refused(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _at_gate(maker)
    monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", 0)
    await _press(world, EventType.PREVIEW_REQUESTED)

    await run_tick(world)

    (failed,) = await _details(world, "preview_failed")
    assert "already running" in failed["reason"]
    assert _adapter(world).preview_starts == []


async def test_a_start_beside_a_live_run_is_not_held_to_the_deployment_limit(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run keeps its sandbox awake anyway, and the limit does not count its preview."""
    world = await _world(maker)
    await run_tick(world)  # plan spawns, and runs on
    monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", 0)
    await _press(world, EventType.PREVIEW_REQUESTED)

    await run_tick(world)

    assert await _details(world, "preview_failed") == []
    assert len(_adapter(world).preview_starts) == 1


async def test_a_transient_substrate_error_leaves_a_serving_preview_watched(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Answering FAILED would stop the loop watching a preview the sandbox still serves: it would
    never be parked at stopAfter, and E2B's own timeout would destroy the kept sandbox."""
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    adapter = _adapter(world)
    adapter.preview_status_error = AdapterUnavailable("E2B answered 503")

    await run_tick(world)

    assert await _details(world, "preview_failed") == []
    assert (await _preview_events(world))[-1] == "preview_started"
    adapter.preview_status_error = None
    adapter.preview = PreviewState(
        phase=PreviewPhase.STOPPED,
        token=adapter.preview.token,
        detail="stopped after its stopAfter",
    )
    await run_tick(world)
    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == "stopped after its stopAfter"


# --------------------------------------------------------------------------- keep-until and renewal


async def _serving(world: World) -> None:
    """plan's preview started by a person and serving."""
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)


def _stopped(world: World, detail: str) -> None:
    adapter = _adapter(world)
    adapter.preview = PreviewState(
        phase=PreviewPhase.STOPPED, token=adapter.preview.token, detail=detail
    )


async def _loop_requests(world: World) -> list[dict[str, Any]]:
    return [d for d in await _details(world, "preview_requested") if d.get("renewal")]


async def test_the_pass_hands_the_adapter_its_keep_until(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker, **SERVICES, stopAfter="10m")
    _adapter(world).preview = PreviewState(
        phase=PreviewPhase.READY,
        token="t",
        urls=(PreviewUrl(service="web", url="https://3000-x.e2b.test"),),
    )
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)

    plan = next(run for run in await step_runs(world) if run.step_name == "plan")
    (request,) = [e for e in await event_rows(world) if e.type == EventType.PREVIEW_REQUESTED]
    assert plan.ended_at is not None
    expected = max(plan.ended_at, request.created_at) + timedelta(minutes=10)
    assert _adapter(world).keep_untils[-1] == expected


async def test_a_renewal_restarts_the_preview_once_however_many_passes_see_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _serving(world)
    _stopped(world, PREVIEW_RENEWAL)
    for _ in range(3):
        await run_tick(world)

    renewals = await _loop_requests(world)
    assert len(renewals) == 1 and renewals[0]["requested_by"] == "factorial"
    assert len(_adapter(world).preview_starts) == 2, "the loop's request is answered as a Start"


async def test_a_renewal_whose_park_failed_is_recorded_and_never_restarted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The park killed the sandbox: a start would only fail on a workspace that is gone."""
    world = await _at_gate(maker)
    await _serving(world)
    killed = f"{PREVIEW_RENEWAL}\nthe sandbox could not be kept, so it was killed: 502"
    _stopped(world, killed)
    await run_tick(world)
    await run_tick(world)

    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == killed
    assert await _loop_requests(world) == []
    assert len(_adapter(world).preview_starts) == 1


async def test_a_renewal_seen_after_its_idle_close_was_due_closes_it_for_idle(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody kept it, and the substrate would have closed it for idle had it read it first. Left
    as a renewal, it would read as starting again for ever."""
    world = await _world(maker)
    # The database's clock stamps a person's start: near it, the workspace is still kept.
    world.clock.now = datetime.now(UTC)
    await advance_to_gate(world)
    await _serving(world)
    plan = next(run for run in await step_runs(world) if run.step_name == "plan")
    (request,) = [e for e in await event_rows(world) if e.type == EventType.PREVIEW_REQUESTED]
    assert plan.ended_at is not None
    world.clock.now = max(plan.ended_at, request.created_at) + timedelta(minutes=31)
    _stopped(world, PREVIEW_RENEWAL)
    await run_tick(world)
    await run_tick(world)

    assert [d["reason"] for d in await _details(world, "preview_stopped")] == ["renewal", "idle"]
    assert await _loop_requests(world) == []
    assert len(_adapter(world).preview_starts) == 1


async def test_an_idle_close_is_recorded_as_idle(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _at_gate(maker)
    await _serving(world)
    _stopped(world, PREVIEW_IDLE)
    await run_tick(world)
    await run_tick(world)

    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == "idle"
    assert await _loop_requests(world) == []


async def test_a_restart_parks_an_awake_preview(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing from before a restart is served: the restart parks the preview itself rather than
    waiting for the keep rule to drop the run on a later pass."""
    world = await _at_gate(maker)
    await _press(world, EventType.PREVIEW_REQUESTED)
    await run_tick(world)
    _ready(world)
    await run_tick(world)
    await ask_restart(world, await new_head(world))

    await run_tick(world)

    assert _adapter(world).preview_stops == ["run-1"]
    (stopped,) = await _details(world, "preview_stopped")
    assert stopped["reason"] == WHY_NOT_SERVABLE["restarted"]
