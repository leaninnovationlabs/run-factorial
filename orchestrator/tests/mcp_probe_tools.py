"""Tools that exist only in tests: one per scope, and one that refuses as a service does.

They exercise the registry, the scopes, `current_actor` and the error wrapper before any real tool
exists, and keep those tests independent of what the real tools need seeded.
"""

from __future__ import annotations

import pytest
from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel

from orchestrator.access import Permission
from orchestrator.api.services.errors import Conflict
from orchestrator.mcp_server import registry
from orchestrator.mcp_server.auth import current_actor
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import respond
from orchestrator.mcp_server.tokens import READ_SCOPE, WRITE_SCOPE

REFUSAL = "acme/app#7 is already exited"


class Caller(BaseModel):
    by: str
    via: str
    mcp_token_id: int | None


def _caller(*, write: bool) -> Caller:
    actor = current_actor(write=write)
    return Caller(by=actor.by, via=actor.via.value, mcp_token_id=actor.mcp_token_id)


@tool(READ_SCOPE, title="Probe read", output=Caller, open_world=False)
async def factorial_probe_read() -> ToolResult:
    """Who is asking."""
    return respond(_caller(write=False), "read")


@tool(WRITE_SCOPE, title="Probe write", output=Caller, open_world=False)
async def factorial_probe_write() -> ToolResult:
    """Who is asking, through the write lock."""
    return respond(_caller(write=True), "wrote")


@tool(READ_SCOPE, title="Probe refuse", output=Caller, open_world=False)
async def factorial_probe_refuse(tc: ToolContext = Depends(tool_context)) -> ToolResult:
    """Refuses the way a service refuses."""
    raise Conflict(REFUSAL, hint="factorial_ticket_reenter", candidates=("running", "blocked"))


PROBE_TOOLS: list[ToolSpec] = [factorial_probe_read, factorial_probe_write, factorial_probe_refuse]


def allow_probe_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write tool must name its permission to register at all (`registry.register`)."""
    monkeypatch.setattr(
        registry,
        "WRITE_TOOLS",
        {**registry.WRITE_TOOLS, "factorial_probe_write": Permission.TICKETS_OPERATE},
    )
