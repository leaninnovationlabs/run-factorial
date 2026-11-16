"""The relay between an assistant turn's Codex child and Bedrock.

Requests are in the shape Codex 0.164.0 sent a provider with the factorial bridge attached,
cut to a few tools. Replies are the streams recorded from Bedrock
(`fixtures/bedrock-responses-2026-11-10`), where the model calls `factorial_projects_list`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.assistant.adapters.codex_cli.relay import ResponsesRelay, open_request, restore_line

FIXTURES = Path(__file__).parent / "fixtures" / "bedrock-responses-2026-11-10"
UPSTREAM = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
BEARER = "Bearer bedrock-api-key-test"
NAMESPACE = "mcp__factorial"
TOOL = "factorial_projects_list"
NAMES = {TOOL: (NAMESPACE, TOOL)}
#: A refusal Bedrock gave a probe on 2026-11-10, body for body.
UNVERIFIED = (
    b'{"error":{"message":"Your account is currently being verified.",'
    b'"type":"permission_denied_error","param":null,"code":"access_denied"}}'
)


def _function(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": f"What {name} does.",
        "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }


def _message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def _codex_request(
    *later: dict[str, Any], bridge: tuple[str, ...] = (TOOL, "factorial_ticket_get")
) -> dict[str, Any]:
    tools: list[dict[str, Any]] = [
        _function("list_mcp_resources"),
        _function("request_user_input"),
        {
            "type": "namespace",
            "name": NAMESPACE,
            "description": "Tools of the factorial server.",
            "tools": [_function(name) for name in bridge],
        },
        _function("get_goal"),
    ]
    return {
        "model": "us.xai.grok-4.6",
        "stream": True,
        "store": False,
        "input": [_message("developer", "You are a coding agent."), _message("user", "hi"), *later],
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "thread-1",
    }


#: The two items Codex hands back once it has run a namespaced call.
CALL_BACK = {
    "type": "function_call",
    "id": "fc_27b18c66ffbe508f9a14c2b74fd3ed1e",
    "name": TOOL,
    "namespace": NAMESPACE,
    "arguments": "{}",
    "call_id": "call_35a71a6283f1530ba7100ee08320766a",
}
RESULT_BACK = {
    "type": "function_call_output",
    "id": "fco_01a1c7ee-d3e5-77f0-8d17-ff38742421c6",
    "call_id": "call_35a71a6283f1530ba7100ee08320766a",
    "output": [
        {"type": "input_text", "text": "Wall time: 0.0048 seconds\nOutput:"},
        {"type": "input_text", "text": '{"projects": []}'},
    ],
}


def _raw(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


def test_a_namespace_goes_to_bedrock_as_its_plain_function_tools() -> None:
    """Bedrock answers a `namespace` tool with a call that names the tool alone. Plain function
    tools are what it was recorded to serve, all 25 of the assistant's among them."""
    request = _codex_request()

    sent, names = open_request(_raw(request))

    assert json.loads(sent) == request | {
        "tools": [
            _function("list_mcp_resources"),
            _function("request_user_input"),
            _function(TOOL),
            _function("factorial_ticket_get"),
            _function("get_goal"),
        ]
    }
    assert names == {
        TOOL: (NAMESPACE, TOOL),
        "factorial_ticket_get": (NAMESPACE, "factorial_ticket_get"),
    }


def test_a_call_codex_hands_back_goes_to_bedrock_as_bedrock_gave_it() -> None:
    """Bedrock gave the call no `namespace`, and the relay added one for Codex. Codex hands the
    call back with it on the next request."""
    sent, _ = open_request(_raw(_codex_request(CALL_BACK, RESULT_BACK)))

    *_, call, result = json.loads(sent)["input"]
    assert call == {key: value for key, value in CALL_BACK.items() if key != "namespace"}
    assert result == RESULT_BACK


def test_a_name_another_tool_has_is_sent_joined_to_its_namespace() -> None:
    """Two tools of one name would leave Bedrock's call naming either. The bridge's tool gives
    way, so Codex's own tool keeps the name its call is run by."""
    back = CALL_BACK | {"name": "get_goal"}

    sent, names = open_request(_raw(_codex_request(back, bridge=("get_goal",))))

    body = json.loads(sent)
    assert [tool["name"] for tool in body["tools"]] == [
        "list_mcp_resources",
        "request_user_input",
        "mcp__factorial__get_goal",
        "get_goal",
    ]
    assert names == {"mcp__factorial__get_goal": (NAMESPACE, "get_goal")}
    assert body["input"][-1]["name"] == "mcp__factorial__get_goal"


def test_a_request_with_no_namespace_goes_as_it_came() -> None:
    request = _codex_request()
    request["tools"] = [tool for tool in request["tools"] if tool["type"] == "function"]
    raw = json.dumps(request, indent=1).encode()

    assert open_request(raw) == (raw, {})


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'{"tools": "none"}'])
def test_a_body_the_relay_cannot_read_goes_as_it_came(raw: bytes) -> None:
    assert open_request(raw) == (raw, {})


def _items(line: bytes) -> list[dict[str, Any]]:
    """Every output item one line of the stream carries."""
    if not line.startswith(b"data: {"):
        return []
    event = json.loads(line[5:])
    items = [event["item"]] if "item" in event else []
    return items + list((event.get("response") or {}).get("output") or [])


def test_each_call_in_the_recorded_reply_gets_its_namespace_back() -> None:
    """Codex 0.164.0 runs a call of an MCP tool only when the call names the tool's namespace,
    and answers any other `unsupported call` without running it. The recorded reply carries the
    call three times: as it opens, as it closes and in the finished response."""
    lines = (FIXTURES / "tool-call.sse").read_bytes().split(b"\n")

    restored = [restore_line(line, NAMES) for line in lines]

    calls = [
        item
        for line in restored
        for item in _items(line)
        if item["type"] == "function_call"
    ]
    assert [(call["namespace"], call["name"]) for call in calls] == [(NAMESPACE, TOOL)] * 3
    changed = [(was, now) for was, now in zip(lines, restored, strict=True) if was != now]
    assert len(changed) == 3
    for was, now in changed:
        for item in (event := json.loads(now[5:])).get("response", {}).get("output", []):
            item.pop("namespace", None)
        event.get("item", {}).pop("namespace", None)
        assert event == json.loads(was[5:])


def _event(item: dict[str, Any]) -> bytes:
    done = {"type": "response.output_item.done", "output_index": 0, "item": item}
    return b"data: " + json.dumps(done, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "item",
    [
        {"type": "function_call", "name": "get_goal", "arguments": "{}", "call_id": "c1"},
        {
            "type": "function_call",
            "name": TOOL,
            "namespace": "mcp__other",
            "arguments": "{}",
            "call_id": "c1",
        },
        {"type": "message", "name": TOOL, "content": []},
    ],
    ids=["a tool of codex's own", "a call that names a namespace", "not a call"],
)
def test_only_a_bare_call_of_a_namespaced_tool_is_changed(item: dict[str, Any]) -> None:
    line = _event(item)

    assert restore_line(line, NAMES) == line


def test_a_line_that_is_not_an_event_passes() -> None:
    for line in (b"", b"data: [DONE]", b'data: {"type": "function_call"', b": keep-alive"):
        assert restore_line(line, NAMES) == line


class _Bedrock:
    """The endpoint, stood in: each request gets the next reply, and is kept."""

    def __init__(self, *replies: httpx.Response) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._replies.pop(0)


class _Pieces(httpx.AsyncByteStream):
    """A reply in pieces that end mid-line, as a socket delivers one."""

    def __init__(self, whole: bytes, size: int = 97) -> None:
        self._whole, self._size = whole, size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._whole), self._size):
            yield self._whole[start : start + self._size]


class _Stalled(httpx.AsyncByteStream):
    """A reply that sends its opening and then nothing, until it is closed."""

    def __init__(self, opening: bytes) -> None:
        self._opening = opening
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._opening
        await self.closed.wait()

    async def aclose(self) -> None:
        self.closed.set()


def _stream(stream: httpx.AsyncByteStream) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-amzn-requestid": "request-1"},
        stream=stream,
    )


@contextlib.asynccontextmanager
async def _relay(bedrock: Any) -> AsyncIterator[ResponsesRelay]:
    relay = ResponsesRelay(UPSTREAM, transport=httpx.MockTransport(bedrock))
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


def _codex() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, headers={"authorization": BEARER})


async def test_a_turn_s_request_reaches_bedrock_and_its_call_comes_back_namespaced() -> None:
    recorded = (FIXTURES / "tool-call.sse").read_bytes()
    bedrock = _Bedrock(_stream(_Pieces(recorded)))

    async with _relay(bedrock) as relay, _codex() as codex:
        reply = await codex.post(f"{relay.base_url}/responses", json=_codex_request())

    [request] = bedrock.requests
    assert str(request.url) == f"{UPSTREAM}/responses"
    assert request.headers["authorization"] == BEARER
    assert request.headers["accept-encoding"] == "identity"
    assert {tool["type"] for tool in json.loads(request.content)["tools"]} == {"function"}
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "text/event-stream"
    assert reply.headers["x-amzn-requestid"] == "request-1"
    assert reply.content == b"\n".join(
        restore_line(line, NAMES) for line in recorded.split(b"\n")
    )
    assert b'"namespace":"mcp__factorial"' in reply.content


async def test_the_reply_reaches_codex_while_bedrock_is_still_sending() -> None:
    """Codex ends a stream that is silent for five minutes. A relay that waited for the whole
    reply would be silent for as long as the model takes."""
    recorded = (FIXTURES / "tool-call.sse").read_bytes()
    opening = recorded[: recorded.index(b"\n\n") + 2]
    stalled = _Stalled(opening)
    bedrock = _Bedrock(_stream(stalled))

    async with _relay(bedrock) as relay, _codex() as codex:
        request = codex.stream("POST", f"{relay.base_url}/responses", json=_codex_request())
        got = b""
        async with asyncio.timeout(5), request as reply:
            async for piece in reply.aiter_bytes():
                got += piece
                if len(got) >= len(opening):
                    break
        assert not stalled.closed.is_set()

    assert got == opening
    assert stalled.closed.is_set()


async def test_a_refusal_reaches_codex_as_bedrock_gave_it() -> None:
    refused = httpx.Response(403, headers={"content-type": "application/json"}, content=UNVERIFIED)

    async with _relay(_Bedrock(refused)) as relay, _codex() as codex:
        reply = await codex.post(f"{relay.base_url}/responses", json=_codex_request())

    assert (reply.status_code, reply.content) == (403, UNVERIFIED)
    assert reply.headers["content-type"] == "application/json"


async def test_bedrock_out_of_reach_is_an_answer_codex_tries_again_on() -> None:
    """Codex sends a request again on a 5xx. A connection the relay only dropped would look to
    Codex like a broken stream with no word of why."""

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _relay(unreachable) as relay, _codex() as codex:
        reply = await codex.post(f"{relay.base_url}/responses", json=_codex_request())

    assert reply.status_code == 502
    assert "connection refused" in reply.json()["error"]["message"]


async def _raw_request(relay: ResponsesRelay, head: bytes) -> bytes:
    port = httpx.URL(relay.base_url).port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    async with asyncio.timeout(5):
        answer = await reader.read()
    writer.close()
    return answer


async def test_a_request_never_chooses_the_host_its_bearer_goes_to() -> None:
    """Any process on the host can reach the relay's port. The bearer a request carries goes to
    the region's Bedrock endpoint and nowhere a request names."""
    bedrock = _Bedrock(httpx.Response(404), httpx.Response(404))

    async with _relay(bedrock) as relay:
        slashes = await _raw_request(
            relay, b"GET //other.example/openai/v1/models HTTP/1.1\r\nhost: other.example\r\n\r\n"
        )
        absolute = await _raw_request(
            relay, b"GET http://other.example/openai/v1/models HTTP/1.1\r\n\r\n"
        )

    assert slashes.startswith(b"HTTP/1.1 404 ")
    assert absolute.startswith(b"HTTP/1.1 400 ")
    [request] = bedrock.requests
    assert request.url.host == "bedrock-runtime.us-east-1.amazonaws.com"
    assert request.headers["host"] == "bedrock-runtime.us-east-1.amazonaws.com"


async def test_what_is_not_http_is_refused_and_the_relay_keeps_serving() -> None:
    bedrock = _Bedrock(httpx.Response(200, content=b"ok"))

    async with _relay(bedrock) as relay, _codex() as codex:
        garbage = await _raw_request(relay, b"\x16\x03\x01 not http\r\n\r\n")
        chunked = await _raw_request(
            relay,
            b"POST /openai/v1/responses HTTP/1.1\r\ntransfer-encoding: chunked\r\n\r\n0\r\n\r\n",
        )
        reply = await codex.post(f"{relay.base_url}/responses", content=b"plain")

    assert garbage.startswith(b"HTTP/1.1 400 ")
    assert chunked.startswith(b"HTTP/1.1 411 ")
    assert (reply.status_code, reply.content) == (200, b"ok")
    assert bedrock.requests[0].content == b"plain"


async def test_the_relay_is_gone_when_it_is_stopped() -> None:
    async with _relay(_Bedrock()) as relay:
        port = httpx.URL(relay.base_url).port

    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
