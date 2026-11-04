"""The requeue route: the operator's way back for a quarantined ticket."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, Ticket
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _reload(session: AsyncSession, ticket_id: int) -> Ticket:
    return (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()


async def _events(session: AsyncSession, ticket_id: int) -> list[Event]:
    return list(
        (
            await session.execute(
                select(Event).where(Event.ticket_id == ticket_id).order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )


async def _quarantine(session: AsyncSession, ticket: Ticket, *, failures: int = 5) -> None:
    ticket.quarantined_at = datetime.now(UTC)
    ticket.consecutive_failures = failures
    await session.flush()


async def test_requeue_clears_both_columns(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """202, and this time the row already reflects it: unlike exit/reenter, requeue is not a
    request the loop honours later — the loop never leases a quarantined ticket to read one."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    await _quarantine(async_session, ticket)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/requeue", headers=auth)

    assert response.status_code == 202
    assert response.json() == {"status": "requeued", "landed": False}
    reloaded = await _reload(async_session, ticket.id)
    assert reloaded.quarantined_at is None
    assert reloaded.consecutive_failures == 0


async def test_requeue_records_the_recovery(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """`ticket_quarantined` is in the log and its answer has to be too: the timeline is the audit
    trail, and quarantine → requeue → quarantine would otherwise read as two failures with an
    unexplained gap and no record that a human acted. `failures` is carried because the count
    this clears survives nowhere else."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    await _quarantine(async_session, ticket, failures=7)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/requeue", headers=auth)

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.type == "ticket_requeued"
    assert event.details == {
        "requested_by": "api-token",
        "requested_via": "dashboard",
        "failures": 7,
    }


async def test_requeue_is_refused_unless_the_ticket_is_quarantined(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/requeue", headers=auth)

    assert response.status_code == 409
    assert response.json()["code"] == "not_quarantined"
    reloaded = await _reload(async_session, ticket.id)
    assert reloaded.quarantined_at is None
    assert reloaded.consecutive_failures == 0
    assert await _events(async_session, ticket.id) == [], "a refusal is not a recovery"


async def test_requeue_404s_an_unknown_ticket(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post("/api/tickets/99999999/requeue", headers=auth)

    assert response.status_code == 404
