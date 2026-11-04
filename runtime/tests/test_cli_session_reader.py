"""The session reader and the signals model every CLI's session is read through (decision 56):
a translator, a log's signal rules and a reader's turnless words the reader does not know, the
neutral `note_*` methods each CLI's log rules call, and the hook filter by session."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fake_e2b import FakeSandboxClient, Script, TuiScript
from pydantic import SecretStr

from runtime.adapters._cli import session_signals
from runtime.adapters._cli.session_reader import SessionReader, TornPoll
from runtime.adapters._cli.session_signals import STOP_HOOK_MISSING, CostState, SessionSignals
from runtime.adapters.claude_cli import cli as claude_cli
from runtime.adapters.claude_cli.stream import NO_TURN
from runtime.adapters.e2b import ClaudeE2bAdapter
from runtime.adapters.e2b.adapter import _read_state, _write_state
from runtime.adapters.e2b.layout import SESSION_POLL, session_log
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    ParkReason,
    ResumeFrom,
    RunSpec,
    RunStatus,
    RunUsage,
    Workspace,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(minutes=1)
TURNLESS = "the stub saw no turn"


def _hook(event: str, **fields: Any) -> str:
    return json.dumps({"at": "2026-10-02T12:00:00Z", "hook": {"hook_event_name": event, **fields}})


# --------------------------------------------------------------------------- the reader


@dataclass
class _State:
    run_id: str = "run-1"
    status: RunStatus = RunStatus.RUNNING
    stream_warnings: tuple[str, ...] = ()
    offset: int = 0
    signals_offset: int = 0
    log_read: int = 0
    cost_seen: CostState | None = None
    usage_base: RunUsage | None = None
    live_usage: RunUsage | None = None
    signals: SessionSignals = field(default_factory=SessionSignals)
    session_turns: int = 0
    segment_turn: bool = True
    finishing: bool = False
    stopping: RunStatus | None = None
    park_reason: ParkReason | None = None


@dataclass(frozen=True)
class _Polled:
    signals: bytes
    signals_size: int
    log: bytes
    log_size: int | None


class _Translator:
    """Knows nothing of claude: each log line becomes one `out <line>`."""

    def __init__(self) -> None:
        self.cost_seen: CostState | None = None
        self.unknown_types: dict[str, int] = {}
        self.unreadable = 0
        self.emitted = 0
        self.torn = False
        self.lacks: tuple[str, ...] = ()

    def feed(self, line: str) -> list[str]:
        if self.torn:
            raise OSError("the translator broke mid-poll")
        self.emitted += 1
        return [f"out {line}"]

    def end_segment(self, *, stop_message: str | None = None) -> list[str]:
        return []

    def missing(self) -> tuple[str, ...]:
        return self.lacks


class _Turns:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, line: str) -> bool:
        self.lines.append(line)
        return line.endswith("done")

    @property
    def turns(self) -> int:
        return sum(line.endswith("done") for line in self.lines)

    @property
    def live_usage(self) -> RunUsage | None:
        return None


def _stub_log_signals(signals: SessionSignals, line: str, /, *, observed_at: datetime) -> None:
    if line == "done":
        signals.note_turn_end(observed_at)


class _Reader(SessionReader[_State, _Turns]):
    _signals_mirror = "signals.jsonl"
    _session_mirror = "session.jsonl"
    _translated_mirror = "stdout.jsonl"
    _cli_label = "stub CLI"
    _turnless = frozenset({TURNLESS})

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._parked_reasons = {ParkReason.EXITED: "the stub was quit"}
        self._state = _State()
        self._state.signals.start_segment(turn=True)
        self.translator = _Translator()
        self._transcript = self.translator
        self._log_signals = _stub_log_signals
        self._reader = _Turns()
        self._override = None
        self._limit = self._read_limit = 1 << 10
        self._max_line = 1 << 12
        self.signals_file = b""
        self.log_file = b""
        self.saves = 0

    async def _read(self) -> _Polled:
        state = self._state
        return _Polled(
            signals=self.signals_file[state.signals_offset :][: self._limit],
            signals_size=len(self.signals_file),
            log=self.log_file[state.log_read :][: self._limit],
            log_size=len(self.log_file),
        )

    async def _stop(self, outcome: RunStatus, reason: str) -> None:
        self._override = (outcome, reason)

    def _observed(self) -> datetime:
        return LATER

    def _save(self) -> None:
        self.saves += 1


async def test_the_session_reader_supervises_a_translator_it_does_not_know(
    tmp_path: Path,
) -> None:
    reader = _Reader(tmp_path)
    prompt = _hook("UserPromptSubmit", prompt="the task") + "\n"
    reader.signals_file = (prompt + '{"at":"2026-10-02T12:00:01Z","ho').encode()
    reader.log_file = b"working\ndone\npart"

    assert await reader._poll() is False
    state = reader._state
    assert (tmp_path / "signals.jsonl").read_bytes() == prompt.encode()
    assert (tmp_path / "session.jsonl").read_bytes() == b"working\ndone\n"
    translated = b"out working\nout done\n"
    assert (tmp_path / "stdout.jsonl").read_bytes() == translated
    assert (state.signals_offset, state.log_read, state.offset) == (
        len(prompt),
        len("working\ndone\n"),
        len(translated),
    )
    assert reader._reader.lines == ["out working", "out done"]
    assert state.session_turns == 1
    # The log rule the reader was given idled the session: no Stop hook ever fired.
    assert state.status is RunStatus.IDLE
    assert state.signals.idle_reason is IdleReason.TURN_END
    assert reader.saves == 1

    reader.log_file += b"\n"
    reader.translator.torn = True
    with pytest.raises(TornPoll):
        await reader._poll()


def test_turnless_warnings_are_the_readers_own(tmp_path: Path) -> None:
    reader = _Reader(tmp_path)
    reader._state.segment_turn = False
    warnings = reader._session_warnings(
        RunStatus.SUCCEEDED, 0, stream=(TURNLESS, NO_TURN), error=None
    )
    assert TURNLESS not in warnings
    assert NO_TURN in warnings


def test_a_clean_exit_nobody_asked_for_parks_as_exited(tmp_path: Path) -> None:
    """A TUI never exits by itself: a clean exit with no finish or stop in flight is a
    person's quit, which a resume can undo."""
    reader = _Reader(tmp_path)
    status, reason = reader._session_verdict(None, 0)
    assert status is RunStatus.INTERRUPTED
    assert reason == reader._parked_reasons[ParkReason.EXITED]
    assert reader._state.park_reason is ParkReason.EXITED


@pytest.mark.parametrize(
    ("finishing", "error", "turn_error", "exit_code", "expected"),
    [
        (True, None, None, 0, RunStatus.SUCCEEDED),
        (False, "boom", None, 0, RunStatus.FAILED),
        (False, None, "boom", 0, RunStatus.FAILED),
        (False, None, None, 1, RunStatus.FAILED),
        (False, None, None, None, RunStatus.FAILED),
    ],
)
def test_a_finish_a_failed_turn_or_a_crash_is_judged_as_before(
    tmp_path: Path,
    finishing: bool,
    error: str | None,
    turn_error: str | None,
    exit_code: int | None,
    expected: RunStatus,
) -> None:
    reader = _Reader(tmp_path)
    reader._state.finishing = finishing
    reader._state.signals.turn_error = turn_error
    status, _ = reader._session_verdict(error, exit_code)
    assert status is expected
    assert reader._state.park_reason is None


def test_a_failed_turn_or_a_crash_says_so_first_and_keeps_the_cli_words_below(
    tmp_path: Path,
) -> None:
    """The timeline shows a reason's first paragraph; a refused key's own words stay in the
    reason below it, where the Runtime page looks for them."""
    reader = _Reader(tmp_path)
    reader._state.signals.turn_error = "API Error: 401 invalid x-api-key"
    assert reader._session_verdict(None, 0)[1] == (
        "The session ended on a failed turn.\n\nAPI Error: 401 invalid x-api-key"
    )
    reader._state.signals.turn_error = None
    assert reader._session_verdict(None, 1)[1] == "The stub CLI stopped unexpectedly (exit 1)."
    assert reader._session_verdict(None, None)[1] == (
        "The stub CLI stopped unexpectedly (no exit code)."
    )


def test_a_quit_proves_the_transcripts_missing_lines_absent_as_a_finished_exit_does(
    tmp_path: Path,
) -> None:
    reader = _Reader(tmp_path)
    reader.translator.lacks = ("no cost-state line",)
    status, _ = reader._session_verdict(None, 0)
    warnings = reader._session_warnings(status, 0, stream=(NO_TURN,), error=None)
    assert "no cost-state line" in warnings
    assert NO_TURN in warnings


# --------------------------------------------------------------------------- the signals


def test_a_hook_line_from_another_session_is_dropped_when_the_session_is_named() -> None:
    other = _hook("Stop", session_id="title-thread", last_assistant_message="a title")
    named = SessionSignals()
    named.start_segment(turn=True)
    named.feed(other, observed_at=LATER, session_id="the-thread")
    assert (named.idle_reason, named.summary) == (None, None)
    named.feed(_hook("Stop", session_id="the-thread"), observed_at=LATER, session_id="the-thread")
    assert named.idle_reason is IdleReason.TURN_END

    unnamed = SessionSignals()
    unnamed.start_segment(turn=True)
    unnamed.feed(other, observed_at=LATER)
    assert (unnamed.idle_reason, unnamed.summary) == (IdleReason.TURN_END, "a title")


def test_a_turn_error_noted_from_the_log_idles_turn_error_without_the_missing_hook_warning() -> (
    None
):
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("UserPromptSubmit", prompt="the task"), observed_at=LATER)
    signals.note_turn_error(T0, "unexpected status 401 Unauthorized")
    assert (signals.idle_reason, signals.idle_since) == (IdleReason.TURN_ERROR, T0)
    assert signals.turn_error == "unexpected status 401 Unauthorized"
    assert signals.warnings == ()
    assert signals.hook_turn_ends == signals.log_turn_ends == 1


def test_an_aborted_turn_noted_from_the_log_idles_turn_end_and_credits_the_hook_count() -> None:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("UserPromptSubmit", prompt="the task"), observed_at=LATER)
    signals.note_turn_aborted(T0)
    assert (signals.idle_reason, signals.warnings) == (IdleReason.TURN_END, ())
    assert signals.hook_turn_ends == signals.log_turn_ends == 1
    # The next turn's Stop is that turn's own: its log line finds nothing missing.
    signals.feed(_hook("UserPromptSubmit", prompt="next"), observed_at=LATER)
    signals.feed(_hook("Stop"), observed_at=LATER)
    signals.feed(_hook("UserPromptSubmit", prompt="third"), observed_at=LATER)
    signals.note_turn_end(LATER)
    assert (signals.idle_reason, signals.warnings) == (None, ())


def test_a_turn_end_noted_with_no_stop_hook_warns_once_and_credits_it() -> None:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    for _ in range(2):
        signals.feed(_hook("UserPromptSubmit", prompt="a turn"), observed_at=LATER)
        signals.note_turn_end(T0)
        assert signals.idle_reason is IdleReason.TURN_END
        signals.settle_turn_ends()
    assert signals.warnings == (STOP_HOOK_MISSING,)
    assert signals.hook_turn_ends == signals.log_turn_ends == 2


def test_claudes_pinned_hook_names_are_the_ones_session_signals_reads() -> None:
    prefixes = ("HOOK_", "ASK_", "LAUNCH_", "TOOL_")
    # Claude pins none: its hooks count whatever session they name.
    shared = [n for n in vars(session_signals) if n.startswith(prefixes) and n != "HOOK_SESSION_ID"]
    assert shared
    for name in shared:
        assert getattr(claude_cli, name) == getattr(session_signals, name), name


# --------------------------------------------------------------------------- the Stop hook's grace
# The Stop hook's line lands before the log's turn end, but a poll can read the signals file
# before the line and the log after the turn end.

PROMPT = _hook("UserPromptSubmit", prompt="the task") + "\n"
STOP = _hook("Stop", last_assistant_message="Done.") + "\n"


def _restart(reader: _Reader) -> None:
    """A worker restart: the signals come back from the state file alone."""
    state = reader._state
    state.signals = SessionSignals.model_validate_json(state.signals.model_dump_json())


@pytest.mark.parametrize("restart", [False, True], ids=["live", "restarted"])
async def test_a_stop_hook_read_a_poll_after_its_turn_end_idles_at_once_and_warns_nothing(
    tmp_path: Path, restart: bool
) -> None:
    reader = _Reader(tmp_path)
    reader.signals_file = PROMPT.encode()
    reader.log_file = b"working\ndone\n"
    await reader._poll()
    assert reader._state.status is RunStatus.IDLE
    if restart:
        _restart(reader)

    reader.signals_file += STOP.encode()
    await reader._poll()
    await reader._poll()
    reader._end_segment()
    signals = reader._state.signals
    assert (signals.idle_reason, signals.summary) == (IdleReason.TURN_END, "Done.")
    assert signals.warnings == ()
    assert signals.hook_turn_ends == signals.log_turn_ends == 1


@pytest.mark.parametrize("restart", [False, True], ids=["live", "restarted"])
async def test_a_stop_hook_that_never_comes_warns_after_the_next_poll(
    tmp_path: Path, restart: bool
) -> None:
    reader = _Reader(tmp_path)
    reader.signals_file = PROMPT.encode()
    reader.log_file = b"done\n"
    await reader._poll()
    assert reader._state.status is RunStatus.IDLE
    assert reader._state.signals.warnings == ()
    if restart:
        _restart(reader)

    await reader._poll()
    assert reader._state.signals.warnings == (STOP_HOOK_MISSING,)


async def test_a_lost_stop_hook_is_not_paid_by_the_next_turns(tmp_path: Path) -> None:
    """Hooks fire in order, so a prompt read while a Stop is owed proves that Stop lost."""
    reader = _Reader(tmp_path)
    reader.signals_file = PROMPT.encode()
    reader.log_file = b"done\n"
    await reader._poll()
    reader.signals_file += (_hook("UserPromptSubmit", prompt="two") + "\n" + STOP).encode()
    reader.log_file += b"done\n"
    await reader._poll()
    reader._end_segment()
    signals = reader._state.signals
    assert signals.warnings == (STOP_HOOK_MISSING,)
    assert signals.hook_turn_ends == signals.log_turn_ends == 2


async def test_a_stop_hook_missing_when_the_segment_ends_warns(tmp_path: Path) -> None:
    reader = _Reader(tmp_path)
    reader.signals_file = PROMPT.encode()
    reader.log_file = b"done\n"
    await reader._poll(last=True)
    reader._end_segment()
    assert reader._state.signals.warnings == (STOP_HOOK_MISSING,)


async def test_the_stop_hooks_grace_lasts_until_a_signals_backlog_is_read(tmp_path: Path) -> None:
    reader = _Reader(tmp_path)
    reader._limit = reader._read_limit = len(PROMPT)
    filler = json.dumps({"pad": "x" * (len(PROMPT) - 12)}) + "\n"
    assert len(filler) == len(PROMPT)
    reader.signals_file = (PROMPT + filler + STOP).encode()
    reader.log_file = b"done\n"
    while await reader._poll():
        pass
    signals = reader._state.signals
    assert signals.summary == "Done."
    assert signals.warnings == ()


# --------------------------------------------------------------------------- E2B


def _spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "secrets": {"GH_TOKEN": SecretStr("ghs_x"), "ANTHROPIC_API_KEY": SecretStr("sk-ant-x")},
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 40,
    }
    return RunSpec.model_validate(fields | overrides)


def _adapter(tmp_path: Path, client: FakeSandboxClient) -> ClaudeE2bAdapter:
    return ClaudeE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "state",
        deployment="test",
        client=client,
        now=lambda: T0,
        poll_interval=0,
    )


async def _polled(client: FakeSandboxClient) -> None:
    start = len(client.commands_by_name(SESSION_POLL))
    for _ in range(10_000):
        if len(client.commands_by_name(SESSION_POLL)) > start:
            return
        await asyncio.sleep(0)
    raise AssertionError("the supervisor did not poll")


async def test_an_e2b_session_reads_the_log_path_its_state_records(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    first = _adapter(tmp_path, client)
    run_id = (await first.spawn(_spec())).run_id
    await _polled(client)
    for task in first._supervisors.values():
        task.cancel()
    run_dir = tmp_path / "state" / "runs" / run_id
    state = _read_state(run_dir, run_id)
    state.session_log = "/home/agent/.codex/sessions/rollout-x.jsonl"
    _write_state(run_dir, state)

    second = _adapter(tmp_path, client)
    try:
        await second.status(run_id)
        await _polled(client)
        assert client.commands_by_name(SESSION_POLL)[-1].envs["LOG"] == state.session_log
    finally:
        await second.cancel(run_id)
        await second.drain()


async def test_a_rework_reads_and_keeps_the_log_path_its_earlier_run_recorded(
    tmp_path: Path,
) -> None:
    turn_end = TuiScript(
        steps=[
            (
                [_hook("UserPromptSubmit", prompt="the task"), _hook("Stop")],
                ['{"type":"system","subtype":"turn_duration"}'],
            )
        ]
    )
    client = FakeSandboxClient(scripts=[Script(tui=turn_end), Script(tui=TuiScript())])
    adapter = _adapter(tmp_path, client)
    first = (await adapter.spawn(_spec())).run_id
    await _polled(client)
    await _polled(client)
    await adapter.finish(first)
    first_dir = tmp_path / "state" / "runs" / first
    prev = _read_state(first_dir, first)
    assert prev.kept and prev.session_id is not None
    recorded = "/home/agent/.codex/sessions/rollout-x.jsonl"
    [sandbox] = client.sandboxes.values()
    sandbox.logs[recorded] = sandbox.logs.pop(session_log(prev.session_id))
    prev.session_log = recorded
    _write_state(first_dir, prev)

    before = len(client.commands_by_name(SESSION_POLL))
    resume_from = ResumeFrom(run_id=first, session_id=prev.session_id)
    run_id = (await adapter.spawn(_spec(resume_from=resume_from))).run_id
    try:
        await _polled(client)
        assert _read_state(tmp_path / "state" / "runs" / run_id, run_id).session_log == recorded
        polls = client.commands_by_name(SESSION_POLL)[before:]
        assert {poll.envs["LOG"] for poll in polls} == {recorded}
    finally:
        await adapter.cancel(run_id)
        await adapter.drain()
