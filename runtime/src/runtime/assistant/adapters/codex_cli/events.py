"""Codex `--json` events to `ChatEvent`s for one assistant turn."""

from __future__ import annotations

from typing import Final

from runtime.adapters.codex_cli.events import (
    AgentMessage,
    CodexEvent,
    CommandExecution,
    ErrorEvent,
    ThreadStarted,
    TurnCompleted,
    TurnStarted,
    UnknownItem,
)
from runtime.assistant.contract import (
    ChatEvent,
    TextDone,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)

__all__ = ["CommandLeaked", "EventMapper"]

_ASSISTANT_BLOCK: Final = "assistant"


class CommandLeaked(Exception):
    """Codex ran a shell command; the assistant backend allows MCP tools only."""


class EventMapper:
    """Maps one turn's Codex stream. Counts `turn.started` against `max_steps`."""

    def __init__(self, *, max_steps: int) -> None:
        self._max_steps = max_steps
        self._turns = 0
        self._thread_id: str | None = None
        self._failed: TurnFailed | None = None

    def map(self, event: CodexEvent) -> list[ChatEvent]:
        if isinstance(event, ThreadStarted):
            self._thread_id = event.thread_id
            return []
        if isinstance(event, TurnStarted):
            self._turns += 1
            if self._turns > self._max_steps:
                return [TurnDone(stop=TurnStop.STEPS)]
            return []
        if isinstance(event, CommandExecution):
            if event.completed and event.command.strip():
                raise CommandLeaked(event.command)
            return []
        if isinstance(event, UnknownItem) and event.item_type == "mcp_tool_call":
            return []
        if isinstance(event, AgentMessage):
            text = event.text.strip()
            if not text:
                return []
            return [TextDone(block=_ASSISTANT_BLOCK, text=text)]
        if isinstance(event, ErrorEvent):
            if not event.terminal:
                return []
            self._failed = TurnFailed(
                reason=TurnFailure.INFRASTRUCTURE,
                message=event.message,
            )
            return [self._failed]
        if isinstance(event, TurnCompleted):
            usage = _usage(event, self._thread_id)
            out: list[ChatEvent] = [usage]
            if self._failed is not None:
                out.append(self._failed)
            else:
                out.append(TurnDone(stop=TurnStop.END))
            return out
        return []


def _usage(event: TurnCompleted, session_id: str | None) -> Usage:
    inp = event.input_tokens or 0
    cached = event.cached_input_tokens or 0
    out = event.output_tokens or 0
    return Usage(
        input_tokens=inp,
        cached_input_tokens=cached,
        output_tokens=out,
        cost_usd=None,
        cost_reported=False,
        session_id=session_id,
    )
