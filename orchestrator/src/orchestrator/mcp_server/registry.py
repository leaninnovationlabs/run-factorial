"""How a tool function becomes an MCP tool: its scope, its annotations, its errors.

`register` wraps every tool itself because FastMCP masks any exception that is not its own into
"Error calling tool" before middleware sees it (`FastMCP.call_tool`); a `ServiceError` must reach
the client as its message, its next step and its valid values.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.base import ToolResult
from githubkit.exception import RateLimitExceeded
from pydantic import BaseModel

from orchestrator.access import GATED_READ_TOOLS, WRITE_TOOLS, Permission
from orchestrator.access.decision import Forbidden
from orchestrator.api.errors import backoff_wait
from orchestrator.api.services.errors import ServiceError, render
from orchestrator.backoff import ConnectionBackoff
from orchestrator.mcp_server.auth import McpAccessToken
from orchestrator.mcp_server.tokens import CHAT_SCOPE, READ_SCOPE, WRITE_SCOPE

__all__ = ["CatalogEntry", "ToolFn", "ToolSpec", "catalogue", "register", "render", "tool"]

ToolFn = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    fn: ToolFn
    scope: str
    title: str
    output: type[BaseModel]
    destructive: bool
    idempotent: bool
    open_world: bool

    @property
    def name(self) -> str:
        return self.fn.__name__


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    title: str
    description: str
    scope: str
    destructive: bool


def tool(
    scope: str,
    *,
    title: str,
    output: type[BaseModel],
    destructive: bool = False,
    idempotent: bool = True,
    open_world: bool = True,
) -> Callable[[ToolFn], ToolSpec]:
    if scope not in (READ_SCOPE, WRITE_SCOPE, CHAT_SCOPE):
        raise ValueError(f"unknown MCP scope {scope!r}")

    def record(fn: ToolFn) -> ToolSpec:
        return ToolSpec(fn, scope, title, output, destructive, idempotent, open_world)

    return record


def _answer_service_errors(fn: ToolFn) -> ToolFn:
    @functools.wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> ToolResult:
        try:
            return await fn(*args, **kwargs)
        except ServiceError as exc:
            # A refusal is an answer, not a fault: below the pinned WARNING level.
            raise ToolError(render(exc), log_level=logging.INFO) from exc
        except (ConnectionBackoff, RateLimitExceeded) as exc:
            raise ToolError(backoff_wait(exc)[0], log_level=logging.INFO) from exc

    return wrapped


def _require_permission(fn: ToolFn, permission: Permission) -> ToolFn:
    """A write tool, or a gated read, checks what the token's holder may do, beside the token's
    own scope: a member's token does not outrank their role (design.md §7.19)."""

    @functools.wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> ToolResult:
        token = get_access_token()
        if not isinstance(token, McpAccessToken):
            raise ToolError("This request carries no Factorial MCP token.", log_level=logging.INFO)
        if permission not in token.permissions:
            raise ToolError(str(Forbidden(permission)), log_level=logging.INFO)
        return await fn(*args, **kwargs)

    return wrapped


def register(server: FastMCP, specs: Iterable[ToolSpec]) -> None:
    for spec in specs:
        if spec.scope == WRITE_SCOPE and spec.name not in WRITE_TOOLS:
            # Fail closed at start: a write tool with no entry would need only `org:read`.
            raise ValueError(f"write tool {spec.name} names no permission in access.WRITE_TOOLS")
        body = _answer_service_errors(spec.fn)
        needed = WRITE_TOOLS.get(spec.name) or GATED_READ_TOOLS.get(spec.name)
        if needed is not None:
            body = _require_permission(body, needed)
        server.tool(
            body,
            name=spec.name,
            title=spec.title,
            description=inspect.getdoc(spec.fn),
            tags={spec.scope},
            auth=require_scopes(spec.scope),
            # Explicit: a `ToolResult` return annotation suppresses schema inference.
            output_schema=spec.output.model_json_schema(),
            annotations={
                "read_only_hint": spec.scope == READ_SCOPE,
                "destructive_hint": spec.destructive,
                "idempotent_hint": spec.idempotent,
                "open_world_hint": spec.open_world,
            },
        )


async def catalogue(server: FastMCP) -> list[CatalogEntry]:
    """Every tool, whoever asks. `server.list_tools()` filters by the current MCP token, so
    outside an MCP request (the dashboard's `/api/mcp/tools`) it would list nothing."""
    entries: list[CatalogEntry] = []
    for item in await server.local_provider.list_tools():
        scope = WRITE_SCOPE if WRITE_SCOPE in item.tags else READ_SCOPE
        annotations = item.annotations
        entries.append(
            CatalogEntry(
                name=item.name,
                title=item.title or item.name,
                description=item.description or "",
                scope=scope,
                destructive=bool(annotations and annotations.destructive_hint),
            )
        )
    return sorted(entries, key=lambda entry: entry.name)
