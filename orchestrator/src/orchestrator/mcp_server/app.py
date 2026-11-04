"""The operator MCP server (tech-stack decision 60), mounted inside the API at `/mcp/`, which
the site serves under `APP_PREFIX` (decision 74).

Stateless JSON over streamable HTTP. Every transport knob is passed here in code: fastmcp also
reads `FASTMCP_*` from the environment and `.env`, and a stray one must not change the guards.
"""

from __future__ import annotations

import logging
from importlib.metadata import version
from typing import Any, Final
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan

from orchestrator.mcp_server import teaching
from orchestrator.mcp_server.auth import FactorialTokenVerifier, RecordTokenUse
from orchestrator.mcp_server.context import McpDeps, deps_lifespan
from orchestrator.mcp_server.registry import register
from orchestrator.mcp_server.tools import ALL_TOOLS

__all__ = ["MCP_PATH", "build_mcp_app", "build_server", "http_app", "new_server"]

MCP_PATH: Final = "/mcp"

# fastmcp installs a Rich handler with propagate=False on import, and its debug lines carry tool
# arguments. Ours: the root handlers, WARNING and up.
for _name in ("fastmcp", "mcp"):
    _logger = logging.getLogger(_name)
    _logger.handlers.clear()
    _logger.propagate = True
    _logger.setLevel(logging.WARNING)


def new_server(deps: McpDeps, *, instructions: str) -> FastMCP[Any]:
    """An empty server with every knob the mount sets."""
    return FastMCP(
        "factorial",
        version=version("factorial-orchestrator"),
        instructions=instructions,
        auth=FactorialTokenVerifier(deps),
        lifespan=deps_lifespan(deps),
        middleware=[RecordTokenUse(deps)],
        on_duplicate="error",
        mask_error_details=True,
        strict_input_validation=True,
    )


def build_server(deps: McpDeps) -> FastMCP[Any]:
    server = new_server(deps, instructions=teaching.INSTRUCTIONS)
    register(server, ALL_TOOLS)
    teaching.register(server)
    return server


def http_app(server: FastMCP[Any], url: str) -> StarletteWithLifespan:
    """`allowed_hosts` is explicit because uvicorn sees the public Host header, never its own
    bind address, and "auto" would answer that with 421. It names the MCP URL's host alone, so
    `/mcp/` reached on any other name is refused."""
    host = urlsplit(url).hostname or "localhost"
    return server.http_app(
        path="/",
        stateless_http=True,
        json_response=True,
        host_origin_protection=True,
        allowed_hosts=[host],
    )


def build_mcp_app(deps: McpDeps) -> StarletteWithLifespan:
    return http_app(build_server(deps), deps.settings().mcp_url)
