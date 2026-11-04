"""A service's refusal reaches the MCP client as its own words (design.md §2.1)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.mcp_server.registry import register
from tests.api_harness import build_mcp_app, mcp_client, mcp_settings, seed_mcp_token
from tests.mcp_probe_tools import PROBE_TOOLS, REFUSAL, allow_probe_write

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_a_service_error_is_not_masked(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_probe_write(monkeypatch)
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)
    register(app.state.mcp_server, PROBE_TOOLS)

    async with mcp_client(app, token) as client:
        with pytest.raises(ToolError) as refused:
            await client.call_tool("factorial_probe_refuse", {})

    message = str(refused.value)
    assert "Error calling tool" not in message
    assert REFUSAL in message
    assert "Next: factorial_ticket_reenter" in message
    assert "Valid: running, blocked" in message
