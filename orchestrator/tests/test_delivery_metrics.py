"""The velocity read model behind `factorial_metrics_get`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus, TicketStatus
from orchestrator.events.read_models import delivery_metrics
from orchestrator.events.types import EventType
from tests.api_harness import seed_project, seed_ticket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _event(project_id: int, ticket_id: int, kind: EventType, at: datetime) -> Event:
    # `append` stamps the transaction's now(); a closure in an earlier week needs its own time.
    return Event(project_id=project_id, ticket_id=ticket_id, type=kind, details={}, created_at=at)


async def test_a_ticket_that_exited_and_came_back_still_counts_as_exited(
    async_session: AsyncSession,
) -> None:
    """Re-entry clears `closed_at`, so a count over `closed_at` would lose every ticket that left
    in the window and was later brought back."""
    now = datetime.now(UTC)
    project = await seed_project(async_session)
    returned = await seed_ticket(async_session, project)
    finished = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    finished.closed_at = now - timedelta(days=2)
    earlier = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    earlier.closed_at = now - timedelta(days=40)
    async_session.add_all(
        [
            _event(project.id, returned.id, EventType.TICKET_EXITED, now - timedelta(days=9)),
            _event(project.id, returned.id, EventType.TICKET_REENTERED, now - timedelta(days=8)),
            _event(project.id, finished.id, EventType.TICKET_DONE, now - timedelta(days=2)),
            _event(project.id, earlier.id, EventType.TICKET_DONE, now - timedelta(days=40)),
        ]
    )
    await async_session.flush()

    metrics = await delivery_metrics(async_session, project.id, since=now - timedelta(days=28))

    assert (metrics.done, metrics.exited) == (1, 1)
    assert sum(week.exited for week in metrics.weeks) == 1
    # 28 days always spans five Mondays, oldest first.
    assert [week.week_start.weekday() for week in metrics.weeks] == [0] * 5
    assert metrics.weeks == sorted(metrics.weeks, key=lambda week: week.week_start)


async def test_cost_per_done_ticket_counts_the_runs_before_the_window(
    async_session: AsyncSession,
) -> None:
    """A done ticket is priced whole; only the window's spend, beside it, stops at `since`."""
    now = datetime.now(UTC)
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    ticket.closed_at = now - timedelta(days=1)
    async_session.add_all(
        [
            StepRun(
                ticket_id=ticket.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                cost_usd=Decimal("2.00"),
                input_tokens=100,
                output_tokens=20,
                started_at=now - timedelta(days=60, minutes=30),
                ended_at=now - timedelta(days=60),
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                cost_usd=Decimal("1.00"),
                input_tokens=10,
                output_tokens=2,
                started_at=now - timedelta(days=1, minutes=10),
                ended_at=now - timedelta(days=1),
            ),
            _event(project.id, ticket.id, EventType.TICKET_DONE, now - timedelta(days=1)),
        ]
    )
    await async_session.flush()

    metrics = await delivery_metrics(async_session, project.id, since=now - timedelta(days=28))

    assert metrics.done_cost_usd == Decimal("3.00")
    assert metrics.cost_per_done_usd == Decimal("3.00")
    assert metrics.done_tokens == 132
    assert metrics.spend.cost_usd == Decimal("1.00")
    assert [(row.step, row.runs, row.median_seconds) for row in metrics.step_times] == [
        ("develop", 1, 600.0)
    ]
