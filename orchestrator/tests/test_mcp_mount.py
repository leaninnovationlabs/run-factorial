"""No credential crosses between `/api` and `/mcp/` (design.md §7.16)."""

from __future__ import annotations

import inspect
import secrets
from datetime import UTC, datetime

import pytest

from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import Project
from orchestrator.mcp_server.context import McpDeps
from orchestrator.mcp_server.tokens import LIFETIME, mint_mcp_jwt
from tests.api_harness import TOKEN, build_mcp_app, client_for, mcp_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

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
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.mark.parametrize("credential", ["none", "api_bearer", "session_cookie"])
async def test_mcp_refuses_dashboard_credentials(credential: str) -> None:
    now = datetime.now(UTC)
    headers = dict(_MCP_HEADERS)
    cookies: dict[str, str] = {}
    if credential == "api_bearer":
        headers["Authorization"] = f"Bearer {TOKEN}"
    elif credential == "session_cookie":
        cookies[SESSION_COOKIE] = mint_session(
            "operator@example.com", verified=True, api_token=TOKEN, now=now, max_age=3600
        )
    # No maker: a refusal must happen before any row is read. Inside the lifespan, so a request
    # the guard let through would answer, not crash on an unstarted session manager.
    app = build_mcp_app(mcp_settings(), None)
    async with app.router.lifespan_context(app), client_for(app) as client:
        client.cookies.update(cookies)
        response = await client.post("/mcp/", json=_INITIALIZE, headers=headers)
    assert response.status_code == 401


async def test_api_refuses_an_mcp_token() -> None:
    now = datetime.now(UTC)
    token = mint_mcp_jwt(
        api_token=TOKEN, jti=secrets.token_urlsafe(32), issued_at=now, expires_at=now + LIFETIME
    )
    async with client_for(build_mcp_app(mcp_settings(), None)) as client:
        response = await client.get(
            "/api/orgs/default/projects", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401


def test_every_default_port_takes_a_project_alone() -> None:
    """`ToolContext` calls each port with the project a tool acts on, and nothing else; the API's
    request-scoped dependencies take a session too, so one of those as a default fails at call
    time."""
    deps = McpDeps()
    for port in (
        deps.config_source,
        deps.commit_writer,
        deps.issue_writer,
        deps.github,
        deps.repo_reader,
    ):
        inspect.signature(port).bind(Project())
