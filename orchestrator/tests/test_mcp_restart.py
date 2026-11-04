"""The restart on the operator mount (#321): the write tool records the dashboard's request, and
factorial_ticket_get says when a ticket is behind its lane."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import Event, McpAccess, Project
from tests.api_harness import (
    build_mcp_app,
    mcp_client,
    mcp_settings,
    seed_mcp_token,
)
from tests.test_routes_restart import HEAD_SHA, PINNED_SHA, behind, on_lane_p

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content


async def _events(maker: async_sessionmaker[AsyncSession], ticket_id: int) -> list[Event]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(Event).where(Event.ticket_id == ticket_id).order_by(Event.id)
                )
            ).all()
        )


async def test_the_restart_tool_records_the_request_as_its_token(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        row, token = await seed_mcp_token(session, access=McpAccess.WRITE)
        ticket, head = await behind(session)
        repo = (await session.get_one(Project, ticket.project_id)).github_repo
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        out = _data(
            await client.call_tool("factorial_ticket_restart", {"ticket": f"id:{ticket.id}"})
        )

    assert (out["ref"], out["action"], out["status"]) == (f"{repo}#12", "restart", "requested")
    [event] = await _events(maker, ticket.id)
    assert event.type == "restart_requested"
    assert event.details == {
        "requested_by": "operator@example.com",
        "requested_via": "mcp",
        "mcp_token_id": row.id,
        "target_process_version_id": head.id,
        "target_sha": HEAD_SHA,
        "allow_current_pin": False,
        "allow_merged": False,
        "step": "plan",
        "attempt": None,
    }


async def test_a_ticket_on_the_lane_head_needs_allow_current_pin(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """D7: the service refuses it for every caller, so a client cannot restart from scratch on
    the same config by accident."""
    async with maker() as session:
        _, token = await seed_mcp_token(session, access=McpAccess.WRITE)
        ticket = await on_lane_p(session)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        with pytest.raises(ToolError, match="already runs aaaaaaa") as refused:
            await client.call_tool("factorial_ticket_restart", {"ticket": f"id:{ticket.id}"})
        anyway = _data(
            await client.call_tool(
                "factorial_ticket_restart",
                {"ticket": f"id:{ticket.id}", "allow_current_pin": True},
            )
        )

    assert "the current version of the p process" in str(refused.value)
    assert anyway["status"] == "requested"
    [event] = await _events(maker, ticket.id)
    assert event.details["target_process_version_id"] == ticket.process_version_id
    assert event.details["target_sha"] == PINNED_SHA
    assert event.details["allow_current_pin"] is True


async def test_ticket_get_says_when_a_ticket_is_behind_its_lane(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        ticket, head = await behind(session)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        got = _data(await client.call_tool("factorial_ticket_get", {"ticket": f"id:{ticket.id}"}))

    assert got["generation"] == 1
    assert got["pin"] == {"process_version_id": ticket.process_version_id, "sha": PINNED_SHA}
    assert got["lane_head"] == {"process_version_id": head.id, "sha": HEAD_SHA}
