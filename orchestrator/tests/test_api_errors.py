"""A service refusal over HTTP: `detail` in the shape `HTTPException` gives it, with the refusal's
`code` and `hint` beside it when it has them, so the dashboard can branch on one and show the
other."""

from __future__ import annotations

import json

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.errors import service_error_handler
from orchestrator.api.services.errors import Conflict, NotFound, render
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


async def test_a_refusal_carries_its_code_and_hint_beside_its_detail() -> None:
    refusal = Conflict("This ticket is done.", code="ticket_done", hint="Open a new issue.")

    response = await service_error_handler(_request(), refusal)

    assert response.status_code == 409
    assert json.loads(bytes(response.body)) == {
        "detail": "This ticket is done.",
        "code": "ticket_done",
        "hint": "Open a new issue.",
    }


async def test_a_refusal_without_them_answers_as_httpexception_does() -> None:
    """Candidates are the MCP client's: the dashboard never names a thing by a free string."""
    refusal = NotFound("ticket not found", candidates=("12", "13"))

    response = await service_error_handler(_request(), refusal)

    assert response.status_code == 404
    assert json.loads(bytes(response.body)) == {"detail": "ticket not found"}


async def test_a_tool_callers_next_step_reaches_mcp_and_never_the_dashboard() -> None:
    """The assistant needs the tool to call; a person needs the button. One refusal carries both,
    and each surface reads its own."""
    refusal = Conflict(
        "This ticket is stopped after repeated errors. Requeue it before you refresh it.",
        code="quarantined",
        hint="Requeue it from the ticket page.",
        tool_hint="factorial_ticket_requeue",
    )

    response = await service_error_handler(_request(), refusal)

    assert "factorial_ticket_requeue" not in bytes(response.body).decode()
    assert json.loads(bytes(response.body))["hint"] == "Requeue it from the ticket page."
    assert render(refusal).splitlines()[1] == "Next: factorial_ticket_requeue"


@pytest.mark.postgres
async def test_a_route_refusal_reaches_the_dashboard_with_its_code_and_hint(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/unblock", headers=auth)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This ticket isn't blocked, so there is nothing to unblock.",
        "code": "not_blocked",
        "hint": "It is running, so nothing needs to be unblocked.",
    }
