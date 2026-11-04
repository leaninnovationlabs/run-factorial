"""Unix-socket RPC between the assistant process and the Codex MCP stdio child.

Codex spawns the MCP server as its own subprocess; that child cannot hold the turn's
`ChatToolbox`, which lives in the assistant process. The child forwards tool list and
tool calls here; this side runs `ChatToolbox.call` in the process that owns the database
and the GitHub read token.

Socket paths stay under ``/tmp/fa-asst/`` with short names: macOS limits ``AF_UNIX`` paths
to 104 bytes, and ``work_root`` under a long ``$TMPDIR`` path must not be used here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Final

from runtime.assistant.contract import ChatToolbox

__all__ = ["ToolSocketServer", "socket_path_for_turn"]

log = logging.getLogger(__name__)

#: Must stay within macOS ``sun_path`` (104 bytes). ``/tmp/fa-asst/p12345-t99.sock`` ≈ 30.
_SOCKET_ROOT: Final = Path("/tmp/fa-asst")
_MAX_SUN_PATH: Final = 104


def socket_path_for_turn(turn_id: str) -> Path:
    digits = re.sub(r"\D", "", turn_id) or "0"
    return _SOCKET_ROOT / f"p{os.getpid()}-t{digits}.sock"


def _check_path_length(path: Path) -> None:
    encoded = os.fsencode(str(path))
    if len(encoded) >= _MAX_SUN_PATH:
        raise OSError(
            f"AF_UNIX path too long ({len(encoded)} bytes, limit {_MAX_SUN_PATH - 1}): {path!s}"
        )


class ToolSocketServer:
    """Serves one turn. Start before Codex; stop when the turn ends."""

    def __init__(self, path: Path, tools: ChatToolbox) -> None:
        self._path = path
        self._tools = tools
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        _check_path_length(self._path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._path.exists():
            self._path.unlink()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    await _reply(writer, {"error": "invalid json"})
                    continue
                response = await self._dispatch(request)
                await _reply(writer, response)
        except Exception:
            log.exception("assistant tool socket: client handler failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        op = request.get("op")
        if op == "definitions":
            tools = [
                {
                    "name": d.name,
                    "title": d.title,
                    "description": d.description,
                    "input_schema": dict(d.input_schema),
                }
                for d in self._tools.definitions()
            ]
            return {"id": req_id, "tools": tools}
        if op == "call":
            name = request.get("name")
            arguments = request.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return {"id": req_id, "error": "name and arguments required"}
            outcome = await self._tools.call(name, arguments)
            return {
                "id": req_id,
                "ok": outcome.ok,
                "text": outcome.text,
            }
        return {"id": req_id, "error": f"unknown op {op!r}"}


async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    await writer.drain()
