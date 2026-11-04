"""SDK messages to `ChatEvent`s, and the init check (tech-stack.md decision 63).

Observed against `claude` 2.1.284 and 2.1.310 under the pinned SDK, with partial messages on:

- Each `init` lists the tools the CLI loaded. Anything outside the turn's own server raises
  `ToolsLeaked`, and so does model output before any init: containment that was not checked
  is treated as containment that failed.
- The CLI sends one `AssistantMessage` per content block, while that block is still open in
  the stream, so a `TextDone` takes the open block's stream index, the key its `TextDelta`s
  carry. A message holding several blocks falls back to their positions, and one the stream
  never announced gets a message number of its own, so its text can never replace another's.
- A failed API call ends in a synthetic `AssistantMessage` with `error` set, then the
  `ResultMessage`; the failure is reported once, at the result, after `Usage`. A refusal is a
  refusal whatever `error` the CLI attached to it.
- The CLI retries a rejected credential ten times with backoff, about three minutes, so the
  first `api_retry` that names one fails the turn instead.
- `total_cost_usd` is the SDK's estimate from list prices, never the bill.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from decimal import Decimal
from typing import Any, Final

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from runtime.assistant.contract import (
    ChatEvent,
    TextDelta,
    TextDone,
    ToolCall,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)

__all__ = ["SUMMARY_CHARS", "EventMapper", "ToolsLeaked"]

SUMMARY_CHARS: Final = 2000

_ASSISTANT_ERRORS: Final = {
    "authentication_failed": TurnFailure.AUTH,
    "billing_error": TurnFailure.AUTH,
    "rate_limit": TurnFailure.RATE_LIMIT,
}
_CREDENTIAL_ERRORS: Final = frozenset({"authentication_failed", "billing_error"})
_LIMIT_STOPS: Final = {"error_max_turns": TurnStop.STEPS, "error_max_budget_usd": TurnStop.BUDGET}
_ABORTED: Final = frozenset({"aborted_streaming", "aborted_tools"})


class ToolsLeaked(Exception):
    """The CLI loaded a tool outside the turn's own server, or never said which tools it loaded."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)
        super().__init__(", ".join(self.names) or "the CLI did not list the tools it loaded")


class EventMapper:
    """One turn's mapping. `servers` are the in-process server names; their tools are
    `mcp__<server>__*`."""

    def __init__(self, servers: Collection[str]) -> None:
        self._prefixes = tuple(f"mcp__{server}__" for server in servers)
        self._checked = False
        self._message = 0
        self._message_id: str | None = None
        self._last_block: int | None = None
        self._error: tuple[TurnFailure, str] | None = None

    @property
    def checked(self) -> bool:
        """Whether this turn's init message arrived and passed."""
        return self._checked

    def map(self, message: Message) -> list[ChatEvent]:
        if isinstance(message, SystemMessage):
            return self._system(message)
        if not self._checked and isinstance(message, StreamEvent | AssistantMessage):
            raise ToolsLeaked(())
        if isinstance(message, StreamEvent):
            return self._stream(message.event)
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, ResultMessage):
            return self._result(message)
        return []

    def _system(self, message: SystemMessage) -> list[ChatEvent]:
        if message.subtype == "init":
            tools = message.data.get("tools")
            if not isinstance(tools, list):
                raise ToolsLeaked(())
            leaked = [str(t) for t in tools if not str(t).startswith(self._prefixes)]
            if leaked:
                raise ToolsLeaked(leaked)
            self._checked = True
        elif message.subtype == "api_retry" and message.data.get("error") in _CREDENTIAL_ERRORS:
            return [
                TurnFailed(
                    reason=TurnFailure.AUTH,
                    message=f"the API rejected the credential ({message.data['error']})",
                )
            ]
        return []

    def _stream(self, event: dict[str, Any]) -> list[ChatEvent]:
        kind = event.get("type")
        if kind == "message_start":
            self._next_message((event.get("message") or {}).get("id"))
        elif kind == "content_block_start":
            self._last_block = event.get("index")
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                block = f"{self._message}:{event.get('index')}"
                return [TextDelta(block=block, text=delta.get("text", ""))]
        return []

    def _assistant(self, message: AssistantMessage) -> list[ChatEvent]:
        if message.error is not None:
            text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
            reason = _ASSISTANT_ERRORS.get(message.error, TurnFailure.INFRASTRUCTURE)
            self._error = (reason, text or message.error)
            return []
        if message.message_id is None or message.message_id != self._message_id:
            self._next_message(message.message_id)
        single = len(message.content) == 1 and self._last_block is not None
        events: list[ChatEvent] = []
        for position, block in enumerate(message.content):
            if isinstance(block, TextBlock):
                index = self._last_block if single else position
                events.append(TextDone(block=f"{self._message}:{index}", text=block.text))
            elif isinstance(block, ToolUseBlock):
                events.append(
                    ToolCall(call_id=block.id, name=self._tool_name(block.name), input=block.input)
                )
        return events

    def _user(self, message: UserMessage) -> list[ChatEvent]:
        if isinstance(message.content, str):
            return []
        return [
            _tool_result(block) for block in message.content if isinstance(block, ToolResultBlock)
        ]

    def _result(self, message: ResultMessage) -> list[ChatEvent]:
        usage = message.usage or {}
        fresh, written, read = (
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        events: list[ChatEvent] = [
            Usage(
                input_tokens=fresh + written + read,
                cached_input_tokens=read,
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=None
                if message.total_cost_usd is None
                else Decimal(str(message.total_cost_usd)),
                cost_reported=False,
                session_id=message.session_id,
            )
        ]
        detail = self._error[1] if self._error else ""
        if message.stop_reason == "refusal":
            events.append(TurnFailed(reason=TurnFailure.REFUSAL, message=detail or "refusal"))
        elif message.terminal_reason in _ABORTED:
            events.append(TurnDone(stop=TurnStop.CANCELLED))
        elif self._error is not None:
            events.append(TurnFailed(reason=self._error[0], message=detail))
        elif message.subtype == "success" and not message.is_error:
            events.append(TurnDone(stop=TurnStop.END))
        elif message.subtype in _LIMIT_STOPS:
            events.append(TurnDone(stop=_LIMIT_STOPS[message.subtype]))
        else:
            errors = "; ".join(message.errors or ())
            events.append(
                TurnFailed(
                    reason=TurnFailure.INFRASTRUCTURE,
                    message=errors or message.result or message.subtype,
                )
            )
        return events

    def _next_message(self, message_id: str | None) -> None:
        self._message += 1
        self._message_id = message_id

    def _tool_name(self, name: str) -> str:
        for prefix in self._prefixes:
            if name.startswith(prefix):
                return name.removeprefix(prefix)
        return name


def _tool_result(block: ToolResultBlock) -> ToolResult:
    content = block.content
    if isinstance(content, str):
        text = content
    else:
        text = "".join(
            str(part.get("text", "")) for part in content or () if part.get("type") == "text"
        )
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return ToolResult(
        call_id=block.tool_use_id,
        ok=not block.is_error,
        summary=text[:SUMMARY_CHARS],
        structured=parsed if isinstance(parsed, dict) else None,
    )
