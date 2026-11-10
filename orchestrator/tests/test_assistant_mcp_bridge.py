"""The stdio MCP server Codex starts for an assistant turn: every tool the assistant process
describes, served with that tool's own schema, and each call forwarded over the turn's socket."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from mcp.types import TextContent

from orchestrator.assistant import mcp_bridge
from orchestrator.assistant.toolbox import registry
from runtime.assistant.adapters.codex_cli.config import ENV_TOOL_SOCKET
from runtime.assistant.adapters.codex_cli.tool_socket import ToolSocketServer, socket_path_for_turn
from runtime.assistant.contract import ToolDefinition, ToolOutcome


class _Toolbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def definitions(self) -> Sequence[ToolDefinition]:
        return tuple(entry.definition for entry in registry().values())

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        if arguments.get("ticket") == "acme/app#404":
            return ToolOutcome(ok=False, text="no ticket acme/app#404")
        return ToolOutcome(ok=True, text=f"{name} answered")


async def test_the_bridge_serves_each_tool_with_its_schema_and_forwards_its_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastMCP builds a function tool's schema from its signature and refuses the `**kwargs` a
    forwarding handler has, so a bridge that registered functions exited at start: the turn ran
    with no factorial tools at all."""
    toolbox = _Toolbox()
    socket = socket_path_for_turn("990001")
    host = ToolSocketServer(socket, toolbox)
    await host.start()
    monkeypatch.setenv(ENV_TOOL_SOCKET, str(socket))
    monkeypatch.setattr(mcp_bridge, "mcp", FastMCP("factorial"))
    try:
        await mcp_bridge._register_all()  # pyright: ignore[reportPrivateUsage]
        async with Client(mcp_bridge.mcp) as client:
            served = {tool.name: tool.input_schema for tool in await client.list_tools()}
            answered = await client.call_tool("factorial_ticket_get", {"ticket": "acme/app#7"})
            refused = await client.call_tool(
                "factorial_ticket_get", {"ticket": "acme/app#404"}, raise_on_error=False
            )
    finally:
        await host.stop()

    # FastMCP inlines a schema's `$defs` as it serves one, so compare a tool that has none.
    assert served.keys() == registry().keys()
    expected = registry()["factorial_ticket_get"].definition.input_schema
    assert served["factorial_ticket_get"] == expected
    assert toolbox.calls == [
        ("factorial_ticket_get", {"ticket": "acme/app#7"}),
        ("factorial_ticket_get", {"ticket": "acme/app#404"}),
    ]
    assert (answered.is_error, _text(answered)) == (False, "factorial_ticket_get answered")
    assert (refused.is_error, _text(refused)) == (True, "no ticket acme/app#404")


def _text(result: CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))
