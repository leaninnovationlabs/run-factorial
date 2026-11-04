"""`/mcp/` refuses a chat-access row, a history of the mount the assistant once had, and that
mount is gone (design.md §7.16). The access page never lists one: `test_mcp_tokens.py`."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import McpAccess
from tests.api_harness import build_mcp_app, client_for, mcp_settings, seed_mcp_token

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}


async def _status(app: FastAPI, path: str, token: str) -> int:
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.post(path, json=_INITIALIZE, headers=headers)
    return response.status_code


async def test_a_chat_access_row_is_refused_at_the_operator_mount(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        _, chat = await seed_mcp_token(session, access=McpAccess.CHAT)
        _, read = await seed_mcp_token(session, access=McpAccess.READ)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    assert await _status(app, "/mcp/", chat) == 401
    # The control: a read token is good there.
    assert await _status(app, "/mcp/", read) == 200


async def test_the_chat_mount_is_gone(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        _, read = await seed_mcp_token(session, access=McpAccess.READ)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    assert await _status(app, "/mcp/chat/", read) == 404
