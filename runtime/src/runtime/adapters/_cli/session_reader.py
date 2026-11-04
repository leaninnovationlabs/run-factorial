"""How a session's supervisor reads a TUI's hook signals and session log, on E2B and on a host
alike, for every CLI (decision 56). Each substrate supplies only its read of the two files and
its stop; each CLI supplies its log's translator, its log's rules for `SessionSignals`, and the
words its reader uses for a segment with no turn.

A read takes whole lines from the offsets in the run's state, mirrors them beside the run before
the state that counts them is written, feeds the signals to `SessionSignals`, whose facts are
the run's RUNNING or IDLE, and translates the log into the mirror `logs()` serves. Both files are
the agent's to write, so they are data: nothing in them is executed, and only what the
translators extract reaches the mirror or the state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from runtime.adapters._cli import mirror
from runtime.adapters._cli.mirror import LOG_SHRANK, SIGNALS_SHRANK
from runtime.adapters._cli.session_signals import CostState, SessionSignals, plus
from runtime.contract import AgentRuntimeError, ParkReason, RunStatus, RunUsage

__all__ = [
    "LogSignals",
    "Polled",
    "SessionReader",
    "SessionTranscript",
    "TornPoll",
    "login_refused",
    "turn_cap_reason",
]


class TornPoll(AgentRuntimeError):
    """A poll failed after it began writing the mirrors. The supervisor ends, and the run is
    supervised again by one that truncates them back to the state."""


class Polled(Protocol):
    """One read of both files. `log_size` is None while the CLI has not created its log."""

    @property
    def signals(self) -> bytes: ...

    @property
    def signals_size(self) -> int: ...

    @property
    def log(self) -> bytes: ...

    @property
    def log_size(self) -> int | None: ...


class SessionRun(Protocol):
    """The session fields of a run's state, as each adapter's run state holds them."""

    run_id: str
    status: RunStatus
    stream_warnings: tuple[str, ...]
    offset: int
    signals_offset: int
    log_read: int
    cost_seen: CostState | None
    usage_base: RunUsage | None
    live_usage: RunUsage | None
    signals: SessionSignals
    session_turns: int
    segment_turn: bool
    finishing: bool
    stopping: RunStatus | None
    park_reason: ParkReason | None


class SessionTranscript(Protocol):
    """One segment's translator, from the CLI's session log into the stream its reader counts."""

    def feed(self, line: str) -> list[str]: ...

    def end_segment(self, *, stop_message: str | None = None) -> list[str]:
        """Flush and close the segment. `stop_message` is the last `Stop` hook's closing words,
        for a log cut off before its own."""
        ...

    def missing(self) -> tuple[str, ...]:
        """What the segment's log never carried; meaningful after the CLI's own clean exit."""
        ...

    @property
    def cost_seen(self) -> CostState | None: ...

    @property
    def unknown_types(self) -> dict[str, int]: ...

    @property
    def unreadable(self) -> int: ...

    @property
    def emitted(self) -> int: ...


class LogSignals(Protocol):
    """A CLI's rules for what one session-log line tells `signals` about its turns."""

    def __call__(self, signals: SessionSignals, line: str, /, *, observed_at: datetime) -> None: ...


class TurnReader(Protocol):
    def feed(self, line: str) -> bool: ...

    @property
    def turns(self) -> int: ...

    @property
    def live_usage(self) -> RunUsage | None:
        """The current segment's tokens so far."""
        ...


def login_refused(label: str) -> str:
    """A sign-in screen at launch: the stored login was refused, and no retry signs it in."""
    return (
        f"The {label} refused the runtime's stored login. Store a fresh one on the Runtime "
        "page."
    )


def turn_cap_reason(cap: int | None) -> str:
    return f"Stopped after {cap} turns, the most the agent's limits.max_turns allows."


class SessionReader[State: SessionRun, Counter: TurnReader]:
    """Mixed into a session's supervisor, which sets every attribute below."""

    _run_dir: Path
    _state: State
    _transcript: SessionTranscript
    #: Set per instance: a function held on the class would bind as a method.
    _log_signals: LogSignals
    #: The reader's own words for a segment with no turn in it.
    _turnless: frozenset[str]
    #: The substrate's words for each park, as its `_requested` gives them.
    _parked_reasons: Mapping[ParkReason, str]
    #: The session a hook line must name to count; None takes every hook line.
    _hook_session: str | None = None
    _reader: Counter
    _override: tuple[RunStatus, str] | None
    #: The raw mirrors' names beside the run, and the translated one's.
    _signals_mirror: str
    _session_mirror: str
    _translated_mirror: str
    #: The next read's size, where it starts again, and the longest line it grows to take.
    _limit: int
    _read_limit: int
    _max_line: int
    #: What the CLI is named in a verdict.
    _cli_label: str

    async def _read(self) -> Polled:
        """Both files, from the state's offsets, at most `_limit` bytes of each."""
        raise NotImplementedError

    async def _stop(self, outcome: RunStatus, reason: str) -> None:
        raise NotImplementedError

    def _observed(self) -> datetime:
        raise NotImplementedError

    def _save(self) -> None:
        """Write the state file."""
        raise NotImplementedError

    async def _poll(self, *, last: bool = False) -> bool:
        """One read of both files; True when more is waiting to be read at once."""
        polled = await self._read()
        observed = self._observed()
        signals_full = len(polled.signals) >= self._limit
        log_full = len(polled.log) >= self._limit
        signals = mirror.whole(polled.signals, last=last and not signals_full)
        logged = b""
        if polled.log_size is not None:
            logged = mirror.whole(polled.log, last=last and not log_full)
        overlong = self._grow(
            signals_stuck=signals_full and not signals,
            log_stuck=log_full and not logged,
            took=bool(signals or logged),
        )
        try:
            self._take(polled, signals, logged, observed, settle=not signals_full)
        except Exception as exc:
            run = self._state.run_id
            raise TornPoll(f"recording a poll of session {run} failed: {exc}") from exc
        if overlong is not None and self._override is None:
            await self._stop(RunStatus.FAILED, f"{overlong} carried a line over 16 MiB")
        return (signals_full or log_full) and overlong is None

    def _grow(self, *, signals_stuck: bool, log_stuck: bool, took: bool) -> str | None:
        """Size the next read. Returns the file whose line no read may take whole, if any."""
        if not (signals_stuck or log_stuck):
            if took:
                self._limit = self._read_limit
            return None
        if self._limit < self._max_line:
            self._limit *= 2  # one line longer than a read: read more at once
            return None
        return "the session log" if log_stuck else "the signals file"

    def _take(
        self, polled: Polled, signals: bytes, logged: bytes, observed: datetime, *, settle: bool
    ) -> None:
        """`settle` when this read took the signals file to its end: a hook owed is not in a
        backlog still to be read."""
        state = self._state
        before = (state.status, state.stream_warnings, state.signals.warnings)
        shrunk = self._shrunk(polled)
        state.stream_warnings = tuple(dict.fromkeys((*state.stream_warnings, *shrunk)))
        # The mirrors first, then the state: a restart truncates every mirror back to the
        # offsets the state holds.
        mirror.append(self._run_dir / self._signals_mirror, signals)
        mirror.append(self._run_dir / self._session_mirror, logged)
        for line in mirror.lines(signals):
            state.signals.feed(line, observed_at=observed, session_id=self._hook_session)
        if settle:
            state.signals.settle_turn_ends()
        translated: list[str] = []
        for line in mirror.lines(logged):
            translated += self._transcript.feed(line)
            self._log_signals(state.signals, line, observed_at=observed)
        self._emit(translated)
        state.signals_offset += len(signals)
        state.log_read += len(logged)
        state.cost_seen = self._transcript.cost_seen
        state.session_turns = self._reader.turns
        state.live_usage = plus(state.usage_base, self._reader.live_usage)
        if state.finishing and state.signals.idle_reason is None:
            # A finish is sent only to an idle session, and `/exit` fires no prompt hook: this is
            # a human's new turn, which voids a finish that failed to land.
            state.finishing = False
        state.status = self._status()
        if (
            signals
            or logged
            or (state.status, state.stream_warnings, state.signals.warnings) != before
        ):
            self._save()

    def _emit(self, lines: list[str]) -> None:
        """Translated lines into the mirror `logs()` serves, and into the reader."""
        if not lines:
            return
        block = "".join(f"{line}\n" for line in lines).encode()
        mirror.append(self._run_dir / self._translated_mirror, block)
        self._state.offset += len(block)
        for line in lines:
            self._reader.feed(line)

    def _status(self) -> RunStatus:
        state = self._state
        # A finish leaves the status to the hooks: read RUNNING, the worker would take it for a
        # human's turn and lose the finishing spell it re-sends the finish from.
        ending = state.stopping is not None or self._override is not None
        if state.signals.idle_reason is not None and not ending:
            return RunStatus.IDLE
        return RunStatus.RUNNING

    def _shrunk(self, polled: Polled) -> tuple[str, ...]:
        found: list[str] = []
        if polled.log_size is not None and polled.log_size < self._state.log_read:
            found.append(LOG_SHRANK)
        if polled.signals_size < self._state.signals_offset:
            found.append(SIGNALS_SHRANK)
        return tuple(found)

    def _end_segment(self) -> None:
        """Close the translated segment with its `result` line, at the CLI's end."""
        self._state.signals.settle_turn_ends()
        self._emit(self._transcript.end_segment(stop_message=self._state.signals.summary))
        self._state.cost_seen = self._transcript.cost_seen
        # The flush can carry a last message: the next segment's base must hold it.
        self._state.live_usage = plus(self._state.usage_base, self._reader.live_usage)

    @property
    def _stopped(self) -> bool:
        """A run we stopped was cut off mid-stream, so what it lacks says nothing about the CLI."""
        return self._state.stopping is not None or self._override is not None

    def _session_verdict(
        self, error: str | None, exit_code: int | None
    ) -> tuple[RunStatus, str | None]:
        """The verdict on a session nobody stopped."""
        if self._state.finishing:
            # The worker asked only once the postconditions held; how the CLI left is no verdict.
            return RunStatus.SUCCEEDED, None
        if error:
            return RunStatus.FAILED, error
        if error := self._state.signals.turn_error:
            return RunStatus.FAILED, f"The session ended on a failed turn.\n\n{error}"
        if exit_code != 0:
            ended = "no exit code" if exit_code is None else f"exit {exit_code}"
            return RunStatus.FAILED, f"The {self._cli_label} stopped unexpectedly ({ended})."
        # A TUI never exits on its own, so a clean exit nobody asked for is a person's quit.
        self._state.park_reason = ParkReason.EXITED
        return RunStatus.INTERRUPTED, self._parked_reasons[ParkReason.EXITED]

    def _session_warnings(
        self,
        status: RunStatus,
        exit_code: int | None,
        *,
        stream: tuple[str, ...],
        error: str | None,
    ) -> tuple[str, ...]:
        state = self._state
        found = state.signals.warnings
        # Only the CLI's own clean exit proves a line absent: a hangup cuts the log short.
        clean = status is RunStatus.SUCCEEDED or state.park_reason is ParkReason.EXITED
        if clean and exit_code == 0 and not self._stopped:
            if not state.segment_turn and state.signals.prompts == 0:
                # Back at its prompt with no turn, and nobody typed: nothing was asked.
                stream = tuple(warning for warning in stream if warning not in self._turnless)
            found = (*found, *self._transcript.missing(), *stream)
        failed = state.signals.turn_error or error
        if status is RunStatus.SUCCEEDED and state.finishing and failed:
            found = (*found, f"finished after its last turn failed: {failed}")
        return found
