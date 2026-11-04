"""The preview's read side: URLs as a list, and a status derived from lifecycle events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.previews import (
    LOOP_REQUESTER,
    PreviewLink,
    awake_previews,
    preview_keep_until,
    preview_lifecycle,
    preview_links,
    publish_preview_links,
)
from orchestrator.events.workspaces import KeptRun, KeptWorkspaces
from tests.api_harness import make_settings, seed_project, seed_ticket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _run(session: AsyncSession, status: StepRunStatus = StepRunStatus.SUCCEEDED) -> StepRun:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=status,
        runtime_run_id="run-1",
    )
    session.add(run)
    await session.flush()
    return run


async def _lifecycle(session: AsyncSession, run: StepRun, kind: EventType) -> None:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    await append(
        session,
        type=kind,
        project_id=ticket.project_id,
        ticket_id=run.ticket_id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
    )


async def test_a_run_publishes_one_url_per_service_and_withdraws_them_together(
    async_session: AsyncSession,
) -> None:
    run = await _run(async_session)
    settings = make_settings()
    links: list[PreviewLink] = [
        ("web", "https://3000-sbx.e2b.app"),
        ("api", "https://8000-sbx.e2b.app"),
    ]

    assert await publish_preview_links(
        async_session, run, links, settings=settings, published_via="worker"
    )
    assert await preview_links(async_session, run) == links
    assert not await publish_preview_links(
        async_session, run, links, settings=settings, published_via="worker"
    )
    assert await publish_preview_links(
        async_session, run, [], settings=settings, published_via="worker"
    )
    assert await preview_links(async_session, run) == []


@pytest.mark.parametrize(
    ("events", "status"),
    [
        ([], "none"),
        ([EventType.PREVIEW_REQUESTED], "starting"),
        ([EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED], "ready"),
        (
            [
                EventType.PREVIEW_REQUESTED,
                EventType.PREVIEW_STARTED,
                EventType.PREVIEW_STOP_REQUESTED,
            ],
            "stopping",
        ),
        ([EventType.PREVIEW_REQUESTED, EventType.PREVIEW_FAILED], "failed"),
        (
            [EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED, EventType.PREVIEW_STOPPED],
            "stopped",
        ),
    ],
)
async def test_the_preview_status_is_the_latest_lifecycle_event(
    async_session: AsyncSession, events: list[EventType], status: str
) -> None:
    run = await _run(async_session)
    for kind in events:
        await _lifecycle(async_session, run, kind)

    found, _ = await preview_lifecycle(async_session, run)

    assert found == status
    assert (run.id in await awake_previews(async_session)) is (status in ("starting", "ready"))


async def test_a_done_tickets_preview_is_not_awake(async_session: AsyncSession) -> None:
    """No pass reaches a done ticket to record its preview's end, so it must not count."""
    run = await _run(async_session)
    await _lifecycle(async_session, run, EventType.PREVIEW_REQUESTED)
    ticket = await async_session.get(Ticket, run.ticket_id)
    assert ticket is not None
    ticket.status = TicketStatus.DONE
    await async_session.flush()

    assert run.id not in await awake_previews(async_session)


async def _at(
    session: AsyncSession, run: StepRun, kind: EventType, at: datetime, **details: Any
) -> None:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    session.add(
        Event(
            project_id=ticket.project_id,
            ticket_id=run.ticket_id,
            step_run_id=run.id,
            type=kind,
            details={"step": run.step_name, "attempt": run.attempt, **details},
            created_at=at,
        )
    )
    await session.flush()


async def test_the_idle_clock_counts_from_the_latest_start_end_or_keep(
    async_session: AsyncSession,
) -> None:
    run = await _run(async_session)
    run.ended_at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    await _at(
        async_session,
        run,
        EventType.PREVIEW_REQUESTED,
        datetime(2026, 10, 9, 11, 0, tzinfo=UTC),
        requested_by="operator@example.com",
    )
    await _at(
        async_session,
        run,
        EventType.PREVIEW_KEPT,
        datetime(2026, 10, 9, 12, 20, tzinfo=UTC),
        requested_by="operator@example.com",
    )
    await _at(
        async_session,
        run,
        EventType.PREVIEW_REQUESTED,
        datetime(2026, 10, 9, 12, 40, tzinfo=UTC),
        requested_by=LOOP_REQUESTER,
        renewal=True,
    )

    keep = await preview_keep_until(
        async_session,
        run,
        timedelta(minutes=30),
        now=datetime(2026, 10, 9, 12, 45, tzinfo=UTC),
    )

    assert keep == datetime(2026, 10, 9, 12, 50, tzinfo=UTC), "the keep, not the loop's renewal"


async def test_a_carried_previews_idle_clock_falls_back_to_the_runs_end(
    async_session: AsyncSession,
) -> None:
    """A rework adopts a ready preview whose start events are on the earlier run."""
    run = await _run(async_session)
    run.ended_at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    keep = await preview_keep_until(
        async_session,
        run,
        timedelta(minutes=30),
        now=datetime(2026, 10, 9, 12, 45, tzinfo=UTC),
    )

    assert keep == datetime(2026, 10, 9, 12, 30, tzinfo=UTC)


async def test_a_live_runs_preview_never_closes_for_idle(async_session: AsyncSession) -> None:
    run = await _run(async_session, StepRunStatus.RUNNING)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    await _at(
        async_session,
        run,
        EventType.PREVIEW_REQUESTED,
        datetime(2026, 10, 9, 8, 0, tzinfo=UTC),
        requested_by="operator@example.com",
    )

    keep = await preview_keep_until(async_session, run, timedelta(minutes=30), now=now)

    assert keep == now + timedelta(minutes=30)


async def test_a_preview_beside_a_live_run_is_not_counted_against_the_cap(
    async_session: AsyncSession,
) -> None:
    ended = await _run(async_session)
    live = StepRun(
        ticket_id=ended.ticket_id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=2,
        status=StepRunStatus.RUNNING,
        runtime_run_id="run-2",
    )
    async_session.add(live)
    await async_session.flush()
    for run in (live, ended):
        await _lifecycle(async_session, run, EventType.PREVIEW_STARTED)

    awake = await awake_previews(async_session)

    assert ended.id in awake
    assert live.id not in awake


async def test_a_live_public_runs_preview_can_start() -> None:
    kept = KeptWorkspaces(
        runs={
            1: KeptRun(
                step_run_id=1,
                runtime_run_id="run-1",
                workspace="ws-1",
                status=StepRunStatus.RUNNING,
                ticket_status=TicketStatus.RUNNING,
                public=True,
            )
        }
    )

    assert kept.unservable(1) is None
