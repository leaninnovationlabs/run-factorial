"""The exit and re-entry routes: requests recorded for the loop, and the states they refuse."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, TicketStatus
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


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


async def test_exit_is_a_request_the_loop_honours(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """202, not 200: nothing has left when this returns. The route appends the request and the
    worker acts on its next tick, the same honesty the interrupt route keeps."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/exit", headers=auth)

    assert response.status_code == 202
    # No worker behind this API, so the pass the request woke never lands.
    assert response.json() == {"status": "requested", "landed": False}
    [event] = await _events(async_session, ticket.id)
    assert event.type == "exit_requested"
    assert event.step_run_id is None
    # `step`/`attempt` carried for the ticket page: `segmentsFrom` keys a timeline block on
    # them, and without them the request files under an invented "unknown" block.
    assert event.details == {
        "requested_by": "api-token",
        "requested_via": "dashboard",
        "step": "develop",
        "attempt": None,
    }


async def test_close_and_exit_asks_the_loop_to_close_the_issue_too(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/exit", headers=auth, json={"close_issue": True}
        )

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.type == "exit_requested"
    assert event.details["close_issue"] is True


@pytest.mark.parametrize("status", [TicketStatus.DONE, TicketStatus.EXITED])
async def test_exit_is_refused_where_the_loop_could_not_honour_it(
    async_session: AsyncSession, auth: dict[str, str], status: str
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, status=status)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/exit", headers=auth)

    assert response.status_code == 409
    assert status in response.json()["detail"]
    assert await _events(async_session, ticket.id) == []


async def test_exit_is_refused_while_the_ticket_is_quarantined(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The loop does not lease a quarantined ticket, so the request would sit unread for ever —
    the same reason `requeue` writes the row itself instead of recording a request."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    ticket.quarantined_at = datetime.now(UTC)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/exit", headers=auth)

    assert response.status_code == 409
    assert response.json()["code"] == "quarantined"
    assert await _events(async_session, ticket.id) == []


async def test_reenter_is_refused_while_the_ticket_is_quarantined(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, status=TicketStatus.EXITED)
    ticket.quarantined_at = datetime.now(UTC)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/reenter", headers=auth)

    assert response.status_code == 409
    assert response.json()["code"] == "quarantined"
    assert await _events(async_session, ticket.id) == []


async def test_reenter_records_the_request(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, status=TicketStatus.EXITED)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/reenter", headers=auth)

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.type == "reentry_requested"
    assert event.details == {
        "requested_by": "api-token",
        "requested_via": "dashboard",
        "step": "develop",
        "attempt": None,
    }


async def test_reenter_is_refused_unless_the_ticket_is_exited(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/reenter", headers=auth)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This ticket hasn't exited, so there is nothing to re-enter.",
        "code": "not_exited",
    }


async def test_both_routes_404_an_unknown_ticket(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        assert (await client.post("/api/tickets/99999999/exit", headers=auth)).status_code == 404
        assert (
            await client.post("/api/tickets/99999999/reenter", headers=auth)
        ).status_code == 404
