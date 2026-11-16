"""`BedrockResponsesBackend`: a chat turn as model calls on Bedrock Runtime's
`/openai/v1/responses`, with no CLI and no child process (tech-stack decision 63).

The loop is here. One step is one model call. A reply that calls tools has each run by the
turn's `ChatToolbox`, in this process and in order, and the next request carries the reply's own
items and each call's output. A reply that calls nothing ends the turn. Bedrock marks the text
before a call `final_answer` all the same, so the mark decides nothing.

A turn keeps no session. It opens with the conversation's transcript (`ChatTurnSpec.history`)
as text in the person's message, the one form of an earlier turn Bedrock was recorded taking.

The caller mints the bearer and hands it over as the turn's credential; it goes to Bedrock in
the `Authorization` header and nowhere else.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import httpx

from runtime.assistant.adapters.bedrock_responses.wire import (
    PINNED_API,
    Reply,
    call_output,
    endpoint,
    function_tool,
    message,
    read_line,
    refusal,
    request_body,
)
from runtime.assistant.contract import (
    BackendStateStore,
    ChatEvent,
    ChatToolbox,
    ChatTurnSpec,
    ToolCall,
    ToolOutcome,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)

__all__ = ["PINNED_API", "BedrockResponsesBackend"]

SUMMARY_CHARS: Final = 2000
#: The read is the quiet between two events: a model may think that long before its first.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class _Stopped(Exception):
    """The turn was cancelled while a request was in flight."""


@dataclass
class _Live:
    stop: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Tally:
    """The turn's tokens, over its model calls."""

    reported: bool = False
    tokens: tuple[int, int, int] = (0, 0, 0)

    def add(self, usage: tuple[int, int, int] | None) -> None:
        if usage is not None:
            self.reported = True
            self.tokens = (
                self.tokens[0] + usage[0],
                self.tokens[1] + usage[1],
                self.tokens[2] + usage[2],
            )

    def events(self) -> list[ChatEvent]:
        if not self.reported:
            return []
        return [
            Usage(
                input_tokens=self.tokens[0],
                cached_input_tokens=self.tokens[1],
                output_tokens=self.tokens[2],
                # Bedrock reports tokens and no cost.
                cost_usd=None,
                cost_reported=False,
                session_id=None,
            )
        ]


class BedrockResponsesBackend:
    kind: ClassVar[str] = "bedrock_responses"
    pinned_version: ClassVar[str] = PINNED_API
    supports_partial_text: ClassVar[bool] = True
    supports_resume: ClassVar[bool] = False
    supports_cancel: ClassVar[bool] = True
    supports_effort: ClassVar[bool] = False

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._live: dict[str, _Live] = {}

    async def preflight(self) -> None:
        """Nothing on the host to check: there is no binary, and the bearer is each turn's."""

    async def turn(
        self, spec: ChatTurnSpec, state: BackendStateStore, tools: ChatToolbox
    ) -> AsyncIterator[ChatEvent]:
        del state
        try:
            url = endpoint(spec.bedrock_region)
        except ValueError as exc:
            yield TurnFailed(reason=TurnFailure.CONFIG, message=str(exc))
            return
        live = self._live[spec.turn_id] = _Live()
        items = [message("developer", spec.system_prompt), message("user", _opening(spec))]
        offered = [function_tool(definition) for definition in tools.definitions()]
        headers = {
            "authorization": f"Bearer {spec.credential.get_secret_value()}",
            "accept": "text/event-stream",
        }
        tally = _Tally()
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT) as client:
                for step in range(spec.limits.max_steps):
                    request = client.build_request(
                        "POST", url, headers=headers, json=request_body(spec.model, items, offered)
                    )
                    reply = Reply()
                    async with aclosing(_model_call(client, request, live.stop, reply)) as said:
                        async for event in said:
                            yield event
                    tally.add(reply.usage)
                    if reply.failure is not None:
                        for event in tally.events():
                            yield event
                        yield reply.failure
                        return
                    if not reply.calls:
                        for event in tally.events():
                            yield event
                        yield TurnDone(stop=TurnStop.END)
                        return
                    if step + 1 == spec.limits.max_steps:
                        # The model would never read these calls' results, and a tool may
                        # record a proposal: they stay unrun.
                        break
                    items += reply.items
                    for call in reply.calls:
                        async with aclosing(_answer(tools, call, items)) as answered:
                            async for event in answered:
                                yield event
            for event in tally.events():
                yield event
            yield TurnDone(stop=TurnStop.STEPS)
        except _Stopped:
            for event in tally.events():
                yield event
            yield TurnDone(stop=TurnStop.CANCELLED)
        finally:
            self._live.pop(spec.turn_id, None)

    async def cancel(self, turn_id: str) -> None:
        live = self._live.get(turn_id)
        if live is not None:
            live.stop.set()


def _opening(spec: ChatTurnSpec) -> str:
    if not spec.history:
        return spec.message
    lines = "\n".join(f"{item.role}: {item.text}" for item in spec.history)
    return f"Earlier in this conversation:\n{lines}\n\n{spec.message}"


async def _model_call(
    client: httpx.AsyncClient, request: httpx.Request, stop: asyncio.Event, reply: Reply
) -> AsyncGenerator[ChatEvent]:
    """One request: its text as it arrives, and the rest of the answer in `reply`."""
    try:
        response = await _unless_stopped(client.send(request, stream=True), stop)
    except httpx.HTTPError as exc:
        reply.failure = _unreachable(exc)
        return
    try:
        if response.status_code != 200:
            body = await _unless_stopped(response.aread(), stop)
            reply.failure = refusal(response.status_code, body.decode(errors="replace"))
            return
        lines = response.aiter_lines()
        while not reply.completed and reply.failure is None:
            line = await _unless_stopped(_next(lines), stop)
            if line is None:
                break
            for event in read_line(line, reply):
                yield event
    except httpx.HTTPError as exc:
        reply.failure = _unreachable(exc)
    finally:
        await response.aclose()
    if not reply.completed and reply.failure is None:
        reply.failure = TurnFailed(
            reason=TurnFailure.INFRASTRUCTURE,
            message="Bedrock's stream ended before the reply was complete",
        )


async def _next(lines: AsyncIterator[str]) -> str | None:
    try:
        return await anext(lines)
    except StopAsyncIteration:
        return None


async def _unless_stopped[T](work: Awaitable[T], stop: asyncio.Event) -> T:
    """`work`'s result, or `_Stopped` as soon as the turn is cancelled: a cancel must not wait
    on a model that has gone quiet."""
    task = asyncio.ensure_future(work)
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
    if stop.is_set():
        if not task.cancelled():
            # Read, so a failure nobody will handle is not reported as never retrieved.
            task.exception()
        raise _Stopped
    return task.result()


def _unreachable(exc: httpx.HTTPError) -> TurnFailed:
    # The exception's own text can quote the request; its kind says enough.
    return TurnFailed(
        reason=TurnFailure.INFRASTRUCTURE, message=f"could not reach Bedrock: {type(exc).__name__}"
    )


async def _answer(
    tools: ChatToolbox, call: dict[str, Any], items: list[dict[str, Any]]
) -> AsyncGenerator[ChatEvent]:
    """Run one call and put its output after the items the next request carries."""
    call_id, name = str(call.get("call_id", "")), str(call.get("name", ""))
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except (TypeError, ValueError):
        arguments = None
    if isinstance(arguments, dict):
        yield ToolCall(call_id=call_id, name=name, input=arguments)
        outcome = await tools.call(name, arguments)
    else:
        yield ToolCall(call_id=call_id, name=name, input={})
        outcome = ToolOutcome(
            ok=False, text=f"invalid arguments for {name}: not a JSON object. Send one."
        )
    yield ToolResult(call_id=call_id, ok=outcome.ok, summary=outcome.text[:SUMMARY_CHARS])
    items.append(call_output(call_id, outcome.text))
