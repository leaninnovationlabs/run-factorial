"""The `--json` JSONL stream, one line into one typed event.

The one module that reads the stream's vocabulary (its names live in cli.py's pinned block):
upstream calls the schema experimental, so a drift re-pins this seam and the reader in
stream.py never changes. Anything unrecognised — a future event type, a human line — is
`None`, because the raw line is already in the log for a human to read and a run must not
fail over noise. A completed item of a type nobody has observed is `UnknownItem`, its name
and nothing else: both readers skip it, and a re-pin that adds its fields has a place to go.

Two readers: the supervisor (`stream.py`, live, one line at a time) and the orchestrator's
activity view (`orchestrator/api/activity.py`, off `stdout.jsonl` after the fact). Both see the
same typed events, so the stream's shape is pinned in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from runtime.adapters.codex_cli.cli import (
    EVENT_ERROR,
    EVENT_ITEM,
    EVENT_ITEM_COMPLETED,
    EVENT_ITEM_STARTED,
    EVENT_THREAD_ID,
    EVENT_THREAD_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
    EVENT_TYPE,
    EVENT_USAGE,
    ITEM_AGENT_MESSAGE,
    ITEM_AGGREGATED_OUTPUT,
    ITEM_COMMAND,
    ITEM_COMMAND_EXECUTION,
    ITEM_ERROR,
    ITEM_EXIT_CODE,
    ITEM_ID,
    ITEM_STATUS,
    ITEM_TEXT,
    ITEM_TYPE,
    KEY_MESSAGE,
    USAGE_CACHE_WRITE_INPUT,
    USAGE_CACHED_INPUT,
    USAGE_INPUT,
    USAGE_OUTPUT,
)

__all__ = [
    "AgentMessage",
    "CodexEvent",
    "CommandExecution",
    "ErrorEvent",
    "ThreadStarted",
    "TurnCompleted",
    "TurnStarted",
    "UnknownItem",
    "parse_event",
    "parse_payload",
]


@dataclass(frozen=True, slots=True)
class ThreadStarted:
    """The resume handle — what `codex resume` addresses, and the one field that must
    outlive the stream."""

    thread_id: str


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """The turn-count signal: no `--max-turns` flag exists, so the supervisor counts these."""


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """The agent's own message — the last one seen is the run's summary."""

    text: str


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """One shell command the agent ran, as `item.started` (`completed=False`: no exit code
    yet) and again as `item.completed` under the same `item_id`. The supervisor does not read
    these; the activity view does."""

    item_id: str
    command: str
    aggregated_output: str
    exit_code: int | None
    status: str
    completed: bool


@dataclass(frozen=True, slots=True)
class UnknownItem:
    """A completed item of a type cli.py does not pin — nothing but its name is trusted."""

    item_type: str


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Per-turn token usage. `input_tokens` is gross of cache (see cli.py's USAGE_INPUT pin);
    no cost figure exists anywhere in the stream, so the cache split is what the orchestrator
    prices from. `output_tokens` already includes reasoning."""

    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """A stream-reported failure. The last one seen is the run's diagnosis — codex exits
    nonzero with an empty stderr (cli.py's EVENT_ERROR pin), so this is the only detail.

    `terminal` is `turn.failed` alone. An `error` item is a warning the turn goes on after
    (every 0.153.2 run opens with the gpt-5.3-codex metadata line before `turn.started`), and
    a top-level `error` is followed by `turn.failed` when it is fatal."""

    message: str
    terminal: bool


CodexEvent = (
    ThreadStarted
    | TurnStarted
    | AgentMessage
    | CommandExecution
    | UnknownItem
    | TurnCompleted
    | ErrorEvent
)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_event(line: str) -> CodexEvent | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parse_payload(payload)


def parse_payload(payload: Any) -> CodexEvent | None:
    """`parse_event` for a line something else already decoded."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get(EVENT_TYPE)
    if kind == EVENT_THREAD_STARTED:
        thread_id = payload.get(EVENT_THREAD_ID)
        if isinstance(thread_id, str) and thread_id:
            return ThreadStarted(thread_id=thread_id)
        # An absent handle must read as absent, never as an empty id a resume would address.
        return None
    if kind == EVENT_TURN_STARTED:
        return TurnStarted()
    if kind in (EVENT_ITEM_STARTED, EVENT_ITEM_COMPLETED):
        item = payload.get(EVENT_ITEM)
        if not isinstance(item, dict):
            return None
        return _parse_item(item, completed=kind == EVENT_ITEM_COMPLETED)
    if kind == EVENT_ERROR:
        message = payload.get(KEY_MESSAGE)
        if isinstance(message, str) and message:
            return ErrorEvent(message=message, terminal=False)
        return None
    if kind == EVENT_TURN_FAILED:
        error = payload.get(EVENT_ERROR)
        message = error.get(KEY_MESSAGE) if isinstance(error, dict) else None
        if isinstance(message, str) and message:
            return ErrorEvent(message=message, terminal=True)
        return None
    if kind == EVENT_TURN_COMPLETED:
        usage = payload.get(EVENT_USAGE)
        usage = usage if isinstance(usage, dict) else {}
        return TurnCompleted(
            input_tokens=_int(usage.get(USAGE_INPUT)),
            cached_input_tokens=_int(usage.get(USAGE_CACHED_INPUT)),
            cache_write_input_tokens=_int(usage.get(USAGE_CACHE_WRITE_INPUT)),
            output_tokens=_int(usage.get(USAGE_OUTPUT)),
        )
    return None


def _parse_item(item: dict[str, Any], *, completed: bool) -> CodexEvent | None:
    item_type = item.get(ITEM_TYPE)
    if item_type == ITEM_COMMAND_EXECUTION:
        return CommandExecution(
            item_id=_str(item.get(ITEM_ID)),
            command=_str(item.get(ITEM_COMMAND)),
            aggregated_output=_str(item.get(ITEM_AGGREGATED_OUTPUT)),
            exit_code=_int(item.get(ITEM_EXIT_CODE)),
            status=_str(item.get(ITEM_STATUS)),
            completed=completed,
        )
    # A started message or error carries nothing yet; only its completion is read.
    if not completed:
        return None
    if item_type == ITEM_AGENT_MESSAGE:
        return AgentMessage(text=str(item.get(ITEM_TEXT, "")))
    if item_type == ITEM_ERROR:
        message = item.get(KEY_MESSAGE)
        if isinstance(message, str) and message:
            return ErrorEvent(message=message, terminal=False)
        return None
    if isinstance(item_type, str) and item_type:
        return UnknownItem(item_type=item_type)
    return None
