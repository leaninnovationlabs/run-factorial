"""The `stream-json` reader: lines in, run facts out, and no I/O.

Shared by the host supervisor and the E2B harness, so the two substrates cannot read the same
stream two ways. Everything it knows comes from the lines fed to it; the caller owns the process,
the log file and the state file.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from runtime.adapters._cli.session_signals import json_object, usage
from runtime.adapters._cli.session_state import add, merge_warnings
from runtime.adapters.claude_cli.cli import (
    EVENT_ASSISTANT,
    EVENT_PARENT_TOOL_USE,
    EVENT_REQUEST_ID,
    EVENT_RESULT,
    EVENT_SESSION_ID,
    EVENT_TYPE,
    LOG_MESSAGE,
    LOG_MESSAGE_ID,
    LOG_USAGE,
    RESULT_COST,
    RESULT_IS_ERROR,
    RESULT_NUM_TURNS,
    RESULT_SUBTYPE,
    RESULT_TEXT,
    RESULT_USAGE,
    USAGE_CACHE_CREATE,
    USAGE_CACHE_READ,
    USAGE_INPUT,
    USAGE_OUTPUT,
)
from runtime.contract import RunUsage

__all__ = ["NO_TURN", "StreamReader", "add", "merge_warnings", "parse_event"]


#: `StreamReader.missing()`'s word for a segment with no answer in it.
NO_TURN: Final = f"no {EVENT_ASSISTANT} event counted as a turn"


class StreamReader:
    """One run segment's view of the stream.

    `prior_turns` seeds the count with the segments before a resume, so a run cannot escape the
    manifest's cap by being stopped and started.
    """

    def __init__(self, *, prior_turns: int = 0) -> None:
        self._prior_turns = prior_turns
        self._requests: dict[str, None] = {}  # insertion-ordered set of API request ids
        self.session_id: str | None = None
        self.result: Mapping[str, Any] | None = None
        #: Each message's latest usage by id: a message split over events, or emitted again
        #: after a worker restart, counts once.
        self._message_usage: dict[str, tuple[int, int, int, int]] = {}

    def feed(self, line: str) -> bool:
        """Read one line; True when it counted a new turn."""
        event = parse_event(line)
        if event is None:
            return False
        if self.session_id is None:
            session_id = event.get(EVENT_SESSION_ID)
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
        kind = event.get(EVENT_TYPE)
        if kind == EVENT_RESULT:
            self.result = event
            return False
        if kind != EVENT_ASSISTANT:
            return False
        self._count(event.get(LOG_MESSAGE))
        if event.get(EVENT_PARENT_TOOL_USE) is not None:
            return False  # a subagent's message is not a main-loop turn
        before = len(self._requests)
        self._requests[str(event.get(EVENT_REQUEST_ID))] = None
        return len(self._requests) > before

    def _count(self, message: Any) -> None:
        if not isinstance(message, Mapping):
            return
        message_id, counts = message.get(LOG_MESSAGE_ID), message.get(LOG_USAGE)
        if isinstance(message_id, str) and isinstance(counts, Mapping):
            self._message_usage[message_id] = (
                _tokens(counts.get(USAGE_INPUT)),
                _tokens(counts.get(USAGE_CACHE_READ)),
                _tokens(counts.get(USAGE_CACHE_CREATE)),
                _tokens(counts.get(USAGE_OUTPUT)),
            )

    @property
    def live_usage(self) -> RunUsage | None:
        """The segment's tokens so far, from its messages: never from its `result` line, which
        repeats them."""
        if not self._message_usage:
            return None
        counts = zip(*self._message_usage.values(), strict=True)
        fresh, read, written, out = (sum(column) for column in counts)
        return usage(fresh + read + written, read, written, out)

    @property
    def turns(self) -> int:
        """Turns across every segment. The CLI's own `num_turns` counts this segment only."""
        return self._prior_turns + len(self._requests)

    def over_cap(self, cap: int | None) -> bool:
        return cap is not None and self.turns > cap

    @property
    def summary(self) -> str:
        return str(self.result.get(RESULT_TEXT, "")) if self.result else ""

    @property
    def cost_usd(self) -> Decimal | None:
        return _decimal(self.result.get(RESULT_COST)) if self.result else None

    @property
    def _usage(self) -> Mapping[str, Any]:
        usage = self.result.get(RESULT_USAGE) if self.result else None
        return usage if isinstance(usage, Mapping) else {}

    @property
    def input_tokens(self) -> int | None:
        return _total_input_tokens(self._usage)

    @property
    def cached_input_tokens(self) -> int | None:
        return _int(self._usage.get(USAGE_CACHE_READ))

    @property
    def cache_write_input_tokens(self) -> int | None:
        return _int(self._usage.get(USAGE_CACHE_CREATE))

    @property
    def output_tokens(self) -> int | None:
        return _int(self._usage.get(USAGE_OUTPUT))

    @property
    def reported_turns(self) -> int | None:
        return _int(self.result.get(RESULT_NUM_TURNS)) if self.result else None

    @property
    def final_turns(self) -> int:
        """The run's turns once its segment ended: the CLI's own `num_turns` for the segment beats
        ours, which only has to be good enough to enforce a cap while the run is in flight."""
        reported = self.reported_turns
        return self.turns if reported is None else self._prior_turns + reported

    def missing(self) -> tuple[str, ...]:
        """What the adapter reads off the stream and this segment never carried."""
        found: list[str] = []
        if self.session_id is None:
            found.append(f"no {EVENT_SESSION_ID} on any event")
        if not self._requests:
            found.append(NO_TURN)
        elif "None" in self._requests:
            found.append(f"an {EVENT_ASSISTANT} event without a {EVENT_REQUEST_ID}")
        if self.result is None:
            found.append(f"no {EVENT_RESULT} event")
            return tuple(found)
        if self.cost_usd is None:
            found.append(f"no {RESULT_COST} in the {EVENT_RESULT} event")
        if self.reported_turns is None:
            found.append(f"no {RESULT_NUM_TURNS} in the {EVENT_RESULT} event")
        if not self._usage:
            found.append(f"no {RESULT_USAGE} in the {EVENT_RESULT} event")
        elif self.input_tokens is None or self.output_tokens is None:
            found.append(f"no {USAGE_INPUT} or {USAGE_OUTPUT} in the {EVENT_RESULT} {RESULT_USAGE}")
        return tuple(found)

    def error(self) -> str | None:
        """The failure a result event reported, or None when it reported success or is absent."""
        if self.result is None or not self.result.get(RESULT_IS_ERROR):
            return None
        subtype = str(self.result.get(RESULT_SUBTYPE, "error"))
        if subtype == "success":  # an API error (a rejected token, say) keeps this subtype
            subtype = "error"
        # The result text carries the actual diagnosis ("Not logged in", a tool error), and the
        # reason is what the orchestrator's `agent_failed` event shows a human.
        detail = str(self.result.get(RESULT_TEXT, "")).strip()[:200]
        return f"the agent reported {subtype}: {detail}".rstrip(": ")


def parse_event(line: str) -> Mapping[str, Any] | None:
    """None for a line that is not a JSON object. Not fatal: the CLI is free to write a human
    line to stdout, and the raw line is already in the log for a human to read."""
    return json_object(line)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _tokens(value: Any) -> int:
    """An agent-written count, or 0."""
    return max(_int(value) or 0, 0)


def _total_input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Everything the run paid to read, cache included.

    The CLI reports `input_tokens` net of cache, and reporting that as the run's input would make
    every cost read model wrong by orders of magnitude.
    """
    parts = [
        _int(usage.get(USAGE_INPUT)),
        _int(usage.get(USAGE_CACHE_CREATE)),
        _int(usage.get(USAGE_CACHE_READ)),
    ]
    present = [part for part in parts if part is not None]
    return sum(present) if present else None
