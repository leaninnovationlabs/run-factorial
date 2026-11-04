"""The assistant's tools, called in this process (design.md §7.18): the read tools of `/mcp/`,
the chat tools and the proposal twins, as plain function calls.

The registry is built once per process. A tool's input schema comes from FastMCP's own
`FunctionTool.from_function`, so it is the schema `/mcp/` serves for the same function, and its
arguments are validated strictly against the same signature, less `tc`, before the call. A turn
gets a `TurnToolbox`: the ports, and the turn its calls belong to, which `chat_scope` checks is
still running.

A gated read (`GATED_READ_TOOLS`) runs only while the turn's owner holds its permission, as
`/mcp/` asks of a token's holder. A `ServiceError` is the tool's answer, rendered as `/mcp/`
renders it. Anything else is logged and masked, as `mask_error_details` masks it there.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, Final

from fastmcp.tools import FunctionTool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import get_cached_typeadapter
from mcp.types import TextContent
from pydantic import TypeAdapter, ValidationError

from orchestrator.access import GATED_READ_TOOLS
from orchestrator.access.decision import Forbidden, allowed, caller_named
from orchestrator.api.services.errors import ServiceError, render
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.mcp_server.chat.tools import CHAT_TOOLS
from orchestrator.mcp_server.chat.twins import TWINS
from orchestrator.mcp_server.context import McpDeps, ToolContext
from orchestrator.mcp_server.registry import ToolFn, ToolSpec
from orchestrator.mcp_server.tokens import READ_SCOPE
from orchestrator.mcp_server.tools import ALL_TOOLS
from runtime.assistant.contract import ToolDefinition, ToolOutcome

__all__ = ["CHAT_TOOLSET", "Entry", "TurnToolbox", "registry"]

log = logging.getLogger(__name__)

CHAT_TOOLSET: Final[tuple[ToolSpec, ...]] = (
    *(spec for spec in ALL_TOOLS if spec.scope == READ_SCOPE),
    *CHAT_TOOLS,
    *TWINS,
)


@dataclass(frozen=True)
class Entry:
    spec: ToolSpec
    definition: ToolDefinition
    arguments: TypeAdapter[dict[str, Any]]


def _arguments(fn: ToolFn) -> TypeAdapter[dict[str, Any]]:
    """A validator of `fn`'s own parameters, less the injected `tc`, as FastMCP builds one."""
    signature = inspect.signature(fn, eval_str=True)
    params = [param for name, param in signature.parameters.items() if name != "tc"]

    def bound(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    bound.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    bound.__annotations__ = {param.name: param.annotation for param in params} | {
        "return": dict[str, Any]
    }
    return get_cached_typeadapter(bound)


def _definition(spec: ToolSpec) -> ToolDefinition:
    return ToolDefinition(
        name=spec.name,
        title=spec.title,
        description=inspect.getdoc(spec.fn) or "",
        input_schema=FunctionTool.from_function(spec.fn, name=spec.name).parameters,
        read_only=spec.scope == READ_SCOPE,
        destructive=spec.destructive,
    )


@cache
def registry() -> Mapping[str, Entry]:
    entries: dict[str, Entry] = {}
    for spec in CHAT_TOOLSET:
        if spec.name in entries:
            raise ValueError(f"two assistant tools are named {spec.name}")
        entries[spec.name] = Entry(spec, _definition(spec), _arguments(spec.fn))
    return MappingProxyType(entries)


class TurnToolbox:
    """One turn's tools: a `ChatToolbox` over the registry, on `deps`, for `turn`."""

    def __init__(self, deps: McpDeps, turn: ChatScope) -> None:
        self._deps = deps
        self._turn = turn

    def definitions(self) -> Sequence[ToolDefinition]:
        return tuple(entry.definition for entry in registry().values())

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        entry = registry().get(name)
        if entry is None:
            return ToolOutcome(ok=False, text=f"no tool named {name!r}")
        try:
            kwargs = entry.arguments.validate_python(dict(arguments), strict=True)
        except ValidationError as exc:
            return ToolOutcome(ok=False, text=_invalid(name, exc))
        needed = GATED_READ_TOOLS.get(name)
        try:
            async with self._deps.sessions() as session:
                if needed is not None and not await allowed(
                    session, caller_named(self._turn.owner), self._turn.organization_id, needed
                ):
                    return ToolOutcome(ok=False, text=str(Forbidden(needed)))
                tc = ToolContext(
                    session=session,
                    settings=self._deps.settings(),
                    deps=self._deps,
                    organization_id=self._turn.organization_id,
                    turn=self._turn,
                )
                result = await entry.spec.fn(tc=tc, **kwargs)
        except ServiceError as exc:
            return ToolOutcome(ok=False, text=render(exc))
        except Exception:
            log.exception("assistant turn %s: %s failed", self._turn.turn_id, name)
            return ToolOutcome(ok=False, text=f"Error calling tool {name!r}")
        return ToolOutcome(ok=True, text=_text(result))


def _invalid(name: str, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )
    return f"invalid arguments for {name}: {problems}"


def _text(result: ToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))
