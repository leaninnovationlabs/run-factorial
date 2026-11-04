"""Stdio MCP server Codex spawns for one assistant turn.

Forwards tool list and tool calls to the assistant process over
`FACTORIAL_ASSISTANT_TOOL_SOCKET` (see `runtime.assistant.adapters.codex_cli.tool_socket`).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastmcp import FastMCP

from runtime.assistant.adapters.codex_cli.config import ENV_TOOL_SOCKET

__all__ = ["main"]

mcp = FastMCP("factorial")


async def _rpc(request: dict[str, Any]) -> dict[str, Any]:
    path = os.environ.get(ENV_TOOL_SOCKET, "")
    if not path:
        return {"error": f"{ENV_TOOL_SOCKET} is not set"}
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
        if not line:
            return {"error": "tool socket closed"}
        return json.loads(line)
    finally:
        writer.close()
        await writer.wait_closed()


def _register_tool(name: str, description: str, _input_schema: dict[str, Any]) -> None:
    async def handler(**arguments: Any) -> str:
        response = await _rpc({"id": name, "op": "call", "name": name, "arguments": arguments})
        if "error" in response:
            return f"Error: {response['error']}"
        return str(response.get("text", ""))

    mcp.tool(name=name, description=description or name)(handler)


async def _register_all() -> None:
    response = await _rpc({"id": 0, "op": "definitions"})
    if "error" in response:
        raise RuntimeError(response["error"])
    for item in response.get("tools", []):
        if not isinstance(item, dict) or "name" not in item:
            continue
        schema = item.get("input_schema")
        _register_tool(
            str(item["name"]),
            str(item.get("description") or item.get("title") or item["name"]),
            schema if isinstance(schema, dict) else {},
        )


async def _run() -> None:
    await _register_all()
    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
