"""The redo route (decision 76): a request recorded for the loop, naming its target and carrying
what the worker compares, the states it refuses, and the surfaces that may not ask."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Forbidden
from orchestrator.api.services.redos import request_redo
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events.types import RequestedVia
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket
from tests.reconcile_world import _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

KEY = "12"


async def on_lane_p(session: AsyncSession) -> Ticket:
    """A running ticket at `plan`, issue #12, pinned to a parseable snapshot of lane `p`."""
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, step="plan", external_key=KEY)
    pinned = await session.get_one(ProcessVersion, ticket.process_version_id)
    pinned.content = _process("p").model_dump(mode="json", by_alias=True)
    await session.flush()
    return ticket


async def _done(session: AsyncSession) -> Ticket:
    ticket = await on_lane_p(session)
    ticket.status = TicketStatus.DONE
    ticket.current_step = "done"
    await session.flush()
    return ticket


async def _exited(session: AsyncSession) -> Ticket:
    ticket = await on_lane_p(session)
    ticket.status = TicketStatus.EXITED
    await session.flush()
    return ticket


async def _quarantined(session: AsyncSession) -> Ticket:
    ticket = await on_lane_p(session)
    ticket.quarantined_at = datetime.now(UTC)
    await session.flush()
    return ticket


async def _gate_blocked(session: AsyncSession) -> Ticket:
    ticket = await on_lane_p(session)
    ticket.current_step = "spec_gate"
    ticket.status = TicketStatus.BLOCKED
    await session.flush()
    return ticket


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


def _add_run(session: AsyncSession, ticket: Ticket, attempt: int, status: str) -> None:
    session.add(
        StepRun(
            ticket_id=ticket.id,
            step_name=ticket.current_step,
            kind=StepKind.AGENT,
            attempt=attempt,
            status=status,
        )
    )


async def test_redo_is_a_request_naming_its_target(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The request carries the step, attempt and generation the worker compares, so a ticket
    that moved on meanwhile is refused as stale rather than redone."""
    ticket = await on_lane_p(async_session)
    _add_run(async_session, ticket, 2, StepRunStatus.RUNNING)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/redo",
            json={"target_step": "plan", "note": " v2 "},
            headers=auth,
        )

    assert response.status_code == 202
    assert response.json() == {"status": "requested", "landed": False}
    [event] = await _events(async_session, ticket.id)
    assert (event.type, event.step_run_id) == ("redo_requested", None)
    assert event.details == {
        "requested_by": "api-token",
        "requested_via": "dashboard",
        "target_step": "plan",
        "note": "v2",
        "has_note": True,
        "step": "plan",
        "attempt": 2,
        "generation": 1,
    }


async def test_a_second_redo_while_one_is_unanswered_is_refused(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """Refused, not folded in: the second press's note would otherwise be dropped unseen."""
    ticket = await on_lane_p(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        first = await client.post(
            f"/api/tickets/{ticket.id}/redo", json={"target_step": "plan"}, headers=auth
        )
        second = await client.post(
            f"/api/tickets/{ticket.id}/redo",
            json={"target_step": "plan", "note": "again"},
            headers=auth,
        )

    assert (first.status_code, second.status_code) == (202, 409)
    assert "already" in second.json()["detail"]
    [event] = await _events(async_session, ticket.id)
    assert "note" not in event.details
    assert event.details["has_note"] is False


@pytest.mark.parametrize(
    ("seed", "body", "says"),
    [
        (_done, {"target_step": "plan"}, "is done"),
        (_exited, {"target_step": "plan"}, "has exited"),
        (_quarantined, {"target_step": "plan"}, "stopped after repeated errors"),
        (on_lane_p, {"target_step": "spec_gate"}, "can't be redone"),
        (_gate_blocked, {"target_step": "plan"}, "nothing to redo"),
    ],
    ids=["done", "exited", "quarantined", "not_the_target", "blocked_gate"],
)
async def test_redo_is_refused_where_it_cannot_be_honoured(
    async_session: AsyncSession,
    auth: dict[str, str],
    seed: Callable[[AsyncSession], Awaitable[Ticket]],
    body: dict[str, Any],
    says: str,
) -> None:
    ticket = await seed(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/redo", json=body, headers=auth)

    assert response.status_code == 409
    assert says in response.json()["detail"]
    assert await _events(async_session, ticket.id) == []


@pytest.mark.parametrize(
    "actor",
    [Actor(by="a", via=RequestedVia.MCP), Actor.chat("a", proposal_id=1)],
    ids=["mcp", "chat"],
)
async def test_redo_is_refused_to_every_surface_but_the_dashboard(
    async_session: AsyncSession, actor: Actor
) -> None:
    ticket = await on_lane_p(async_session)

    with pytest.raises(Forbidden):
        await request_redo(async_session, ticket, actor, target_step="plan", note=None)
    assert await _events(async_session, ticket.id) == []


@pytest.mark.parametrize(
    ("seed", "target"),
    [(on_lane_p, "plan"), (_quarantined, None), (_gate_blocked, None)],
    ids=["current_step", "quarantined", "blocked_gate"],
)
async def test_the_ticket_detail_names_the_step_a_redo_would_take(
    async_session: AsyncSession,
    auth: dict[str, str],
    seed: Callable[[AsyncSession], Awaitable[Ticket]],
    target: str | None,
) -> None:
    ticket = await seed(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}", headers=auth)

    assert response.status_code == 200
    assert response.json()["redo_target"] == target


async def test_the_ticket_detail_words_what_its_step_waits_on(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """From the pinned snapshot, expanded for this ticket: the page names the step's
    preconditions in words instead of rule code with `{ticket}` still in it."""
    ticket = await on_lane_p(async_session)
    pinned = await async_session.get_one(ProcessVersion, ticket.process_version_id)
    content = dict(pinned.content)
    content["steps"] = [dict(step) for step in content["steps"]]
    content["steps"][0]["preconditions"] = {
        "all": ["ticket_has_assignee()", 'pr_merged("spec/{ticket}-*", "main")']
    }
    pinned.content = content
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}", headers=auth)

    assert response.json()["waiting_on"] == [
        "an assignee on the issue",
        f"the pull request from a branch starting with spec/{KEY}- merged into main",
    ]
