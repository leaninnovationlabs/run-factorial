"""Invariants every MCP tool keeps, whichever module registers it (design.md §7.16)."""

from __future__ import annotations

import re

import pytest

from orchestrator.mcp_server.app import build_server
from orchestrator.mcp_server.tokens import READ_SCOPE, WRITE_SCOPE
from tests.api_harness import (
    authed_client_for,
    build_mcp_app,
    make_settings,
    mcp_deps_for,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_NAME = re.compile(r"^factorial_[a-z]+(_[a-z]+)+$")

# Pinned, not derived: a write tool tagged `factorial:read` would pass every per-tool check below
# and hand a read-only token a write.
WRITE_TOOLS = {
    "factorial_ticket_create",
    "factorial_ticket_exit",
    "factorial_ticket_reenter",
    "factorial_ticket_restart",
    "factorial_ticket_interrupt",
    "factorial_ticket_resume",
    "factorial_ticket_unblock",
    "factorial_ticket_retry",
    "factorial_ticket_requeue",
    "factorial_config_commit",
}
READ_TOOLS = {
    "factorial_projects_list",
    "factorial_project_get",
    "factorial_tickets_list",
    "factorial_ticket_get",
    "factorial_metrics_get",
    "factorial_dashboard_get",
    "factorial_vocabulary_get",
    "factorial_runs_list",
    "factorial_costs_get",
    "factorial_config_read",
    "factorial_config_validate",
}


async def test_every_tool_is_scoped_and_annotated() -> None:
    server = build_server(mcp_deps_for(make_settings(), None))
    tools = await server.local_provider.list_tools()
    assert {item.name for item in tools} == READ_TOOLS | WRITE_TOOLS
    for item in tools:
        scopes = item.tags & {READ_SCOPE, WRITE_SCOPE}
        assert item.auth is not None, item.name
        assert len(scopes) == 1, item.name
        (scope,) = scopes
        assert scope == (WRITE_SCOPE if item.name in WRITE_TOOLS else READ_SCOPE), item.name
        assert item.annotations is not None, item.name
        assert item.annotations.read_only_hint == (scope == READ_SCOPE), item.name
        assert _NAME.match(item.name), item.name
        assert item.description and len(item.description) < 600, item.name


async def test_dashboard_catalogue_lists_write_tools_without_an_mcp_token() -> None:
    app = build_mcp_app(make_settings(), None)
    async with authed_client_for(app) as client:
        response = await client.get("/api/mcp/tools")
    assert response.status_code == 200
    scopes = {tool["name"]: tool["scope"] for tool in response.json()["tools"]}
    assert {name for name, scope in scopes.items() if scope == WRITE_SCOPE} == WRITE_TOOLS
