"""The `exec --json` reader: lines in, run facts out, and no I/O.

Shared by the host supervisor and the E2B harness, so the two substrates cannot read the same
stream two ways. It consumes the TYPED events of `events.py`, never raw JSON.
"""

from __future__ import annotations

from typing import Final

from runtime.adapters._cli.session_signals import COUNT_CAP, usage
from runtime.adapters._cli.session_state import add
from runtime.adapters.codex_cli.cli import (
    EVENT_THREAD_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_STARTED,
    EVENT_USAGE,
)
from runtime.adapters.codex_cli.events import (
    AgentMessage,
    ErrorEvent,
    ThreadStarted,
    TurnCompleted,
    TurnStarted,
    parse_event,
)
from runtime.contract import RunUsage

#: `StreamReader.missing()`'s words for a segment with no turn in it.
NO_TURN: Final = f"no {EVENT_TURN_STARTED} event"
NO_USAGE: Final = f"no {EVENT_USAGE} on any {EVENT_TURN_COMPLETED} event"


class StreamReader:
    """One run segment's view of the stream.

    `prior_turns` seeds the count with the segments before a resume, so a run cannot escape the
    manifest's cap by being stopped and started.
    """

    def __init__(self, *, prior_turns: int = 0) -> None:
        self._prior_turns = prior_turns
        self.segment_turns = 0
        self.session_id: str | None = None
        #: The last agent message wins: it is the run's own account of what it did.
        self.summary: str | None = None
        #: The last stream-reported error. The diagnosis: stderr holds nothing (0.146.0) or
        #: retry tracing (0.153.2), never the reason.
        self.last_error: str | None = None
        self.input_tokens: int | None = None
        self.cached_input_tokens: int | None = None
        self.cache_write_input_tokens: int | None = None
        self.output_tokens: int | None = None

    def feed(self, line: str) -> bool:
        """Read one line; True when it started a new turn."""
        match parse_event(line):
            case ThreadStarted(thread_id=thread_id):
                if self.session_id is None:
                    self.session_id = thread_id
            case TurnStarted():
                self.segment_turns += 1
                return True
            case AgentMessage(text=text):
                self.summary = text
            case ErrorEvent(message=message):
                self.last_error = message
            case TurnCompleted() as turn:
                # Per-turn figures, summed; `input_tokens` is already gross of cache (cli.py's
                # USAGE_INPUT pin), so a plain sum is the run's true total.
                self.input_tokens = add(self.input_tokens, _count(turn.input_tokens))
                self.cached_input_tokens = add(
                    self.cached_input_tokens, _count(turn.cached_input_tokens)
                )
                self.cache_write_input_tokens = add(
                    self.cache_write_input_tokens, _count(turn.cache_write_input_tokens)
                )
                self.output_tokens = add(self.output_tokens, _count(turn.output_tokens))
            case _:
                pass
        return False

    @property
    def turns(self) -> int:
        return self._prior_turns + self.segment_turns

    @property
    def live_usage(self) -> RunUsage | None:
        """The segment's tokens so far: its turns' usage, each counted as the turn completes."""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return usage(
            self.input_tokens or 0,
            self.cached_input_tokens or 0,
            self.cache_write_input_tokens or 0,
            self.output_tokens or 0,
        )

    def over_cap(self, cap: int | None) -> bool:
        return cap is not None and self.turns > cap

    def missing(self) -> tuple[str, ...]:
        """What the adapter reads off the stream and this segment never carried."""
        found: list[str] = []
        if self.session_id is None:
            found.append(f"no {EVENT_THREAD_STARTED} event")
        if not self.segment_turns:
            found.append(NO_TURN)
        if self.input_tokens is None or self.output_tokens is None:
            found.append(NO_USAGE)
        return tuple(found)


def _count(value: int | None) -> int | None:
    """A turn's count, from a stream the agent can write, held to [0, COUNT_CAP]."""
    return None if value is None else min(max(value, 0), COUNT_CAP)
