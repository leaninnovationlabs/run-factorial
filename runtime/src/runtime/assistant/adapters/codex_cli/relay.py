"""The relay between an assistant turn's Codex child and Bedrock's `/openai/v1`.

Codex 0.164.0 offers an MCP server's tools as one `namespace` tool, and runs a call of one only
when the call names that namespace: any other is answered `unsupported call` and nothing runs.
Bedrock accepts the `namespace` tool and answers with a call that names the tool alone
(recorded 2026-11-10 with a Grok 4.6 model), so on Bedrock the model could name the assistant's
tools and run none. Codex has no setting for either side of that.

So for one turn the relay's address is the provider address Codex is given. A request goes on to
Bedrock with each namespace opened into plain function tools, the shape Bedrock was recorded to
serve, and a call in the reply gets its namespace back. All else passes as it came, both ways.

The relay holds no credential. Any process on the host can reach a loopback port, so the bearer
is the one a request carries, and it goes to the one host the relay was made for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

import httpx

__all__ = ["LOOPBACK", "ResponsesRelay", "open_request", "restore_line"]

log = logging.getLogger(__name__)

LOOPBACK: Final = "127.0.0.1"
TOOL_NAMESPACE: Final = "namespace"
ITEM_FUNCTION_CALL: Final = "function_call"
#: How Codex named an MCP tool before it sent namespaces: `mcp__server__tool`.
SEPARATOR: Final = "__"

_HEAD_LIMIT: Final = 64 * 1024
#: A request carries the whole thread, and Codex compacts a thread long before this.
_BODY_LIMIT: Final = 64 * 1024 * 1024
#: Codex ends a stream silent for five minutes, so the relay waits on Bedrock no longer.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
#: A hop's own headers. The relay sends a body by its length and takes a reply unencoded, as
#: the recorded requests did, because it reads the reply line by line.
_REQUEST_OWN: Final = frozenset(
    {
        b"host",
        b"content-length",
        b"connection",
        b"keep-alive",
        b"accept-encoding",
        b"te",
        b"trailer",
        b"upgrade",
        b"proxy-authorization",
        b"proxy-connection",
    }
)
_RESPONSE_OWN: Final = frozenset(
    {
        b"content-length",
        b"content-encoding",
        b"transfer-encoding",
        b"connection",
        b"keep-alive",
        b"trailer",
        b"upgrade",
    }
)


def open_request(raw: bytes) -> tuple[bytes, dict[str, tuple[str, str]]]:
    """A request body as Bedrock gets it, and for each opened tool the name it was sent under
    with the namespace and name Codex knows it by. A body with nothing to open, or one the relay
    cannot read, is returned as it came."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return raw, {}
    if not isinstance(body, dict):
        return raw, {}
    names = _open_tools(body)
    handed_back = _bare_calls(body, names)
    if not names and not handed_back:
        return raw, {}
    return json.dumps(body, separators=(",", ":")).encode(), names


def _open_tools(body: dict[str, Any]) -> dict[str, tuple[str, str]]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return {}
    taken = {
        tool["name"]
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("type") != TOOL_NAMESPACE
        and isinstance(tool.get("name"), str)
    }
    names: dict[str, tuple[str, str]] = {}
    opened: list[Any] = []
    for tool in tools:
        members = _members(tool)
        if members is None:
            opened.append(tool)
            continue
        for member in members:
            name = member["name"]
            # Two tools of one name would leave a call naming either.
            flat = f"{tool['name']}{SEPARATOR}{name}" if name in taken else name
            taken.add(flat)
            names[flat] = (tool["name"], name)
            opened.append({**member, "name": flat})
    if names:
        body["tools"] = opened
    return names


def _members(tool: object) -> list[dict[str, Any]] | None:
    """A namespace's tools. None for a tool of another kind, and for a namespace in a shape the
    relay does not know, which passes as it came."""
    if not (isinstance(tool, dict) and tool.get("type") == TOOL_NAMESPACE):
        return None
    members = tool.get("tools")
    if not isinstance(tool.get("name"), str) or not isinstance(members, list):
        return None
    named = [m for m in members if isinstance(m, dict) and isinstance(m.get("name"), str)]
    return named if len(named) == len(members) else None


def _bare_calls(body: dict[str, Any], names: Mapping[str, tuple[str, str]]) -> bool:
    """Each call Codex hands back, under the name it was sent to Bedrock by and with no
    namespace: the call as Bedrock gave it."""
    sent = {owner: flat for flat, owner in names.items()}
    items = body.get("input")
    changed = False
    for item in items if isinstance(items, list) else []:
        if not (isinstance(item, dict) and item.get("type") == ITEM_FUNCTION_CALL):
            continue
        namespace, name = item.get("namespace"), item.get("name")
        if not isinstance(namespace, str):
            continue
        del item["namespace"]
        if isinstance(name, str):
            item["name"] = sent.get((namespace, name), name)
        changed = True
    return changed


def restore_line(line: bytes, names: Mapping[str, tuple[str, str]]) -> bytes:
    """One line of a reply as Codex gets it. An output item comes in an event's `item` and in a
    finished response's `output`. A line with no call to change is returned as it came."""
    if not line.startswith(b"data:") or b'"function_call"' not in line:
        return line
    payload = line[5:]
    try:
        event = json.loads(payload)
    except (ValueError, RecursionError):
        return line
    if not isinstance(event, dict):
        return line
    response = event.get("response")
    output = response.get("output") if isinstance(response, dict) else None
    items = [event.get("item"), *(output if isinstance(output, list) else [])]
    if not [item for item in items if _restore(item, names)]:
        return line
    lead = payload[: len(payload) - len(payload.lstrip())]
    tail = payload[len(payload.rstrip()) :]
    return b"data:" + lead + json.dumps(event, separators=(",", ":")).encode() + tail


def _restore(item: object, names: Mapping[str, tuple[str, str]]) -> bool:
    if not (isinstance(item, dict) and item.get("type") == ITEM_FUNCTION_CALL):
        return False
    name = item.get("name")
    if "namespace" in item or not isinstance(name, str) or name not in names:
        return False
    item["namespace"], item["name"] = names[name]
    return True


class _Refused(Exception):
    def __init__(self, status: int, why: str) -> None:
        super().__init__(why)
        self.status = status


class ResponsesRelay:
    """One turn's relay. `upstream` is the provider address Codex would have been given."""

    def __init__(
        self, upstream: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._upstream = httpx.URL(upstream)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._server: asyncio.Server | None = None
        self._serving: set[asyncio.Task[None]] = set()

    @property
    def base_url(self) -> str:
        """What Codex is given in the upstream's place: the same path, on this relay."""
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{LOOPBACK}:{port}{self._upstream.path}"

    async def start(self) -> None:
        self._client = httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT)
        self._server = await asyncio.start_server(self._serve, LOOPBACK, 0, limit=_HEAD_LIMIT)

    async def stop(self) -> None:
        """Close the port and everything still open through it, to Codex and to Bedrock."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        serving = list(self._serving)
        for task in serving:
            task.cancel()
        await asyncio.gather(*serving, return_exceptions=True)
        await server.wait_closed()
        if self._client is not None:
            await self._client.aclose()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._serving.add(task)
        try:
            await self._answer(reader, writer)
        except ConnectionError:
            # Codex went away with the reply unsent: the turn was cancelled, or it gave up.
            pass
        finally:
            self._serving.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self._client is not None
        try:
            method, target, headers, body = await _read_request(reader)
            url = self._upstream.copy_with(raw_path=target.encode("ascii"))
        except (_Refused, httpx.InvalidURL) as refused:
            await _reply(writer, getattr(refused, "status", 400), str(refused))
            return
        names: dict[str, tuple[str, str]] = {}
        if method == "POST":
            body, names = open_request(body)
        request = self._client.build_request(
            method, url, headers=[*headers, (b"accept-encoding", b"identity")], content=body
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            log.warning("the Bedrock relay could not reach %s: %r", url.host, exc)
            await _reply(writer, 502, f"the relay could not reach {url.host}: {exc}")
            return
        try:
            await _pass_reply(writer, response, names)
        except httpx.HTTPError as exc:
            # The status has gone out: an unfinished reply is what tells Codex to ask again.
            log.warning("the reply from %s broke off: %r", url.host, exc)
        finally:
            await response.aclose()


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, list[tuple[bytes, bytes]], bytes]:
    """One request: its method, its target, the headers that go on, and its whole body."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise _Refused(400, "not an HTTP request") from exc
    request_line, *lines = head[:-4].split(b"\r\n")
    parts = request_line.split(b" ")
    # The target is a path: the host a request goes to is never the request's to name.
    if (
        len(parts) != 3
        or not parts[2].startswith(b"HTTP/1.")
        or not parts[1].startswith(b"/")
        or not request_line.isascii()
    ):
        raise _Refused(400, "not an HTTP/1.1 request for a path")
    headers: list[tuple[bytes, bytes]] = []
    length = 0
    for line in lines:
        name, colon, value = line.partition(b":")
        name, value = name.strip().lower(), value.strip()
        if not colon or not name:
            raise _Refused(400, "not an HTTP header")
        if name == b"transfer-encoding":
            raise _Refused(411, "the relay reads a body by its content-length")
        if name == b"content-length":
            if not value.isdigit():
                raise _Refused(400, "not a content-length")
            length = int(value)
        if name not in _REQUEST_OWN:
            headers.append((name, value))
    if length > _BODY_LIMIT:
        raise _Refused(413, f"a body over {_BODY_LIMIT} bytes")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise _Refused(400, "the body ended before its content-length") from exc
    return parts[0].decode("ascii"), parts[1].decode("ascii"), headers, body


async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    """The relay's own answer, in the shape of a provider's refusal."""
    body = json.dumps({"error": {"message": message, "type": "relay_error"}}).encode()
    head = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    )
    writer.write(head.encode("ascii") + body)
    await writer.drain()


async def _pass_reply(
    writer: asyncio.StreamWriter, response: httpx.Response, names: Mapping[str, tuple[str, str]]
) -> None:
    """Bedrock's reply to Codex as it arrives, each whole line restored."""
    head = [f"HTTP/1.1 {response.status_code} {response.reason_phrase}".encode("latin-1")]
    head += [
        name + b": " + value
        for name, value in response.headers.raw
        if name.lower() not in _RESPONSE_OWN
    ]
    head += [b"transfer-encoding: chunked", b"connection: close", b"", b""]
    writer.write(b"\r\n".join(head))
    held = b""
    async for piece in response.aiter_bytes():
        if names:
            *lines, held = (held + piece).split(b"\n")
            piece = b"".join(restore_line(line, names) + b"\n" for line in lines)
        await _send(writer, piece)
    await _send(writer, restore_line(held, names))
    writer.write(b"0\r\n\r\n")
    await writer.drain()


async def _send(writer: asyncio.StreamWriter, piece: bytes) -> None:
    if piece:
        writer.write(b"%x\r\n%b\r\n" % (len(piece), piece))
        await writer.drain()
