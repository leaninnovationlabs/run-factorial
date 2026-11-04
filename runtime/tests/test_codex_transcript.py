"""The codex TUI's rollout, translated into the `exec --json` stream the reader already reads,
and what it says about turns to the hook signals.

The fixtures under `fixtures/codex-0.159.0/` are one thread's rollout and signals file, which
`test_codex_tui_local.py` captured from the real 0.159.0 TUI in tmux against the mock.
`index.json` names each step's line range in the `session` files. Their steps fall into four
launches: the spawn (a turn with an image, a typed turn, a command, a 401, an Esc, a command,
`/quit`), a turnless resume and a typed turn, a resume with an image and the pointer, and a
turnless resume whose second turn left a command running when the server was killed. The `ask`
files are the same thread's two later launches, each a turnless resume and a typed turn that
calls `request_user_input`: `refused` with the ask tool off, and `self_answered` with it on,
where codex answered its own picker with nothing two minutes on; `ask.index.json` names them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from runtime.adapters._cli.session_reader import SessionReader
from runtime.adapters._cli.session_signals import STOP_HOOK_MISSING, SessionSignals
from runtime.adapters.codex_cli.events import CommandExecution, parse_event
from runtime.adapters.codex_cli.stream import NO_USAGE, StreamReader
from runtime.adapters.codex_cli.transcript import RolloutTranscript, codex_log_signals
from runtime.contract import IdleReason

FIXTURES = Path(__file__).parent / "fixtures" / "codex-0.159.0"
ROLLOUT = (FIXTURES / "session.rollout.jsonl").read_text("utf-8").splitlines()
SIGNALS = (FIXTURES / "session.signals.jsonl").read_text("utf-8").splitlines()
INDEX: dict[str, dict[str, list[int]]] = json.loads((FIXTURES / "index.json").read_text("utf-8"))
THREAD = "01a0fd77-250b-7800-82c6-e6f8b2077805"
#: Read after every line in the fixture was written.
READ_AT = datetime(2026, 10, 2, 17, 0, tzinfo=UTC)

#: Each launch's steps, and whether it carried the orchestrator's turn.
LAUNCHES: dict[str, tuple[tuple[str, ...], bool]] = {
    "spawn": (("base", "human", "command", "fail", "abort", "agent_edit", "quit"), True),
    "resume": (("resume",), False),
    "resume_image": (("resume_image",), True),
    "resume_compressed": (("resume_compressed", "hangup"), False),
}


def _step(name: str, file: str = "rollout") -> list[str]:
    start, end = INDEX[name][file]
    return (ROLLOUT if file == "rollout" else SIGNALS)[start:end]


def _launch(name: str, file: str = "rollout") -> list[str]:
    steps, _ = LAUNCHES[name]
    return [line for step in steps for line in _step(step, file)]


def _translate(
    lines: list[str], *, turn: bool = True, skip: int = 0
) -> tuple[RolloutTranscript, list[str]]:
    transcript = RolloutTranscript(THREAD, turn=turn, skip=skip)
    out = [o for line in lines for o in transcript.feed(line)]
    return transcript, out + transcript.end_segment()


def _read(lines: list[str]) -> StreamReader:
    reader = StreamReader()
    for line in lines:
        reader.feed(line)
    return reader


def _poll(signals: SessionSignals, hooks: list[str], rollout: list[str]) -> None:
    """One supervisor read, in the reader's order: the signals, then the log."""
    for line in hooks:
        signals.feed(line, observed_at=READ_AT, session_id=THREAD)
    signals.settle_turn_ends()
    for line in rollout:
        codex_log_signals(signals, line, observed_at=READ_AT)


def _where(lines: list[str], record: str, kind: str | None = None) -> int:
    return next(
        i
        for i, line in enumerate(lines)
        if json.loads(line)["type"] == record
        and (kind is None or json.loads(line)["payload"].get("type") == kind)
    )


@pytest.mark.parametrize(
    ("launch", "turns", "tokens"),
    [
        # The command turn's two responses: its last running total, not the first, nor the
        # thread's total, which a resume's turn would multiply.
        ("spawn", 6, (6000, 42)),
        ("resume", 1, (1000, 7)),
        ("resume_image", 1, (1000, 7)),
        ("resume_compressed", 2, (3000, 21)),
    ],
)
def test_every_launch_translates_into_the_exec_stream_its_reader_reads(
    launch: str, turns: int, tokens: tuple[int, int]
) -> None:
    transcript, out = _translate(_launch(launch), turn=LAUNCHES[launch][1])
    reader = _read(out)

    assert [json.loads(line)["type"] for line in out].count("thread.started") == 1
    assert reader.session_id == THREAD
    assert (reader.segment_turns, (reader.input_tokens, reader.output_tokens)) == (turns, tokens)
    assert reader.summary == "PONG from mock."
    assert reader.missing() == ()
    assert transcript.missing() == ()
    assert (transcript.unknown_types, transcript.unreadable) == ({}, 0)
    assert transcript.cost_seen is None
    assert "local_image" not in "".join(out)

    transcript.feed(json.dumps({"type": "never-seen-before"}))
    transcript.feed(json.dumps({"type": 5}))
    assert transcript.unknown_types == {"never-seen-before": 1, "<not a string>": 1}


@pytest.mark.parametrize(
    ("launch", "messages"),
    [
        (
            "spawn",
            [
                ("orchestrator", "Read /RUN/turns/0.md and do what it says."),
                ("human", "Reply with one word. HUMAN-TURN"),
                ("human", "RUNME echo hi-5f9ec4"),
                ("human", "FAILME now"),
                ("human", "SLOWME now"),
                ("human", "RUNME printf '# l1-agent-edit' >> /CODEX_HOME/config.toml"),
            ],
        ),
        ("resume", [("human", "Reply with one word. AFTER-RESUME")]),
        ("resume_image", [("orchestrator", "Read /RUN/turns/1.md and do what it says.")]),
        (
            "resume_compressed",
            [
                ("human", "Reply with one word. AFTER-COMPRESSION"),
                ("human", "RUNME sleep 7059068"),
            ],
        ),
    ],
)
def test_the_first_user_message_of_a_launch_with_a_turn_is_the_orchestrators(
    launch: str, messages: list[tuple[str, str]]
) -> None:
    """The TUI logs the pointer the orchestrator launched it with exactly as it logs a typed
    turn, so only its position tells them apart. An attached image stays out of the text."""
    _, out = _translate(_launch(launch), turn=LAUNCHES[launch][1])
    users = [
        (event["factorial_origin"], event["item"]["text"])
        for event in map(json.loads, out)
        if event["type"] == "item.completed" and event["item"]["type"] == "user_message"
    ]
    assert users == messages


def test_a_command_is_a_completed_command_execution_item() -> None:
    _, out = _translate(_step("command"))
    commands = [e for e in map(parse_event, out) if isinstance(e, CommandExecution)]
    assert commands == [
        CommandExecution(
            item_id="call_d31dc7964b6b4e2fae8af798db3c99b8",
            command="/bin/zsh -lc 'echo hi-5f9ec4'",
            aggregated_output="hi-5f9ec4\n",
            exit_code=0,
            status="completed",
            completed=True,
        )
    ]


def test_a_failed_and_an_aborted_turn_idle_the_session_with_no_lost_hook_warning() -> None:
    """Codex fires no hook for either, so the rollout is the only word on them; a warning for
    a hook that never exists would fail every smoke and finish that met one."""
    signals = SessionSignals()
    signals.start_segment(turn=True)
    idles: list[tuple[str, IdleReason | None]] = []
    for step in LAUNCHES["spawn"][0]:
        _poll(signals, _step(step, "signals"), _step(step))
        idles.append((step, signals.idle_reason))
        if step == "fail":
            assert signals.turn_error is not None
            assert "401 Unauthorized" in signals.turn_error
        if step == "abort":
            assert signals.turn_error is None
    assert idles == [
        ("base", IdleReason.TURN_END),
        ("human", IdleReason.TURN_END),
        ("command", IdleReason.TURN_END),
        ("fail", IdleReason.TURN_ERROR),
        ("abort", IdleReason.TURN_END),
        ("agent_edit", IdleReason.TURN_END),
        ("quit", IdleReason.TURN_END),
    ]
    assert signals.warnings == ()

    transcript, out = _translate(_step("fail"))
    reader = _read(out)
    assert reader.last_error is not None and "401 Unauthorized" in reader.last_error
    assert json.loads(out[-1])["type"] == "turn.failed"
    assert transcript.missing() == ()
    assert RolloutTranscript(THREAD, turn=False).missing() == ()
    aborted, _ = _translate(_step("abort"), turn=False)
    assert aborted.missing() == ()


def test_a_lost_stop_hook_is_credited_from_task_complete_with_the_warning() -> None:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    hooks = [line for line in _step("base", "signals") if '"Stop"' not in line]
    _poll(signals, hooks, _step("base"))
    assert (signals.idle_reason, signals.warnings) == (IdleReason.TURN_END, ())

    _poll(signals, _step("human", "signals"), _step("human"))
    assert (signals.idle_reason, signals.warnings) == (IdleReason.TURN_END, (STOP_HOOK_MISSING,))


def test_a_turn_that_ends_normally_after_a_failed_one_clears_the_failure_without_its_hook() -> None:
    """Left set, the session's verdict would call a run whose last turn succeeded a failure."""
    signals = SessionSignals()
    signals.start_segment(turn=True)
    _poll(signals, _step("fail", "signals"), _step("fail"))
    assert signals.turn_error is not None
    hooks = [line for line in _step("agent_edit", "signals") if '"Stop"' not in line]
    _poll(signals, hooks, _step("agent_edit"))
    assert (signals.idle_reason, signals.turn_error) == (IdleReason.TURN_END, None)


def test_a_hookless_turn_end_read_with_the_next_prompt_leaves_the_session_working() -> None:
    """One read can take the next turn's prompt hook and the rollout's end of the turn before
    it. With no hook for that end, idling on it would show a working session as waiting."""
    signals = SessionSignals()
    signals.start_segment(turn=True)
    for step in ("base", "human", "command"):
        _poll(signals, _step(step, "signals"), _step(step))
    abort = _step("abort")
    ended = _where(abort, "event_msg", "turn_aborted")

    _poll(
        signals,
        _step("fail", "signals") + _step("abort", "signals"),
        _step("fail") + abort[:ended],
    )
    assert signals.idle_reason is None
    assert signals.turn_error is not None and "401 Unauthorized" in signals.turn_error

    _poll(signals, _step("agent_edit", "signals")[:1], abort[ended:])
    assert signals.idle_reason is None

    _poll(signals, _step("agent_edit", "signals")[1:], _step("agent_edit"))
    assert (signals.idle_reason, signals.turn_error, signals.warnings) == (
        IdleReason.TURN_END,
        None,
        (),
    )


def test_a_turn_cut_off_by_a_hangup_reports_the_tokens_it_had() -> None:
    lines = _launch("resume_compressed")
    last_usage = max(i for i, line in enumerate(lines) if '"token_usage_record"' in line)
    hung = lines[: last_usage + 1]

    transcript, out = _translate(hung, turn=False)
    reader = _read(out)

    assert (reader.segment_turns, reader.input_tokens, reader.output_tokens) == (2, 3000, 21)
    assert json.loads(out[-1]) == {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 2000,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 14,
        },
    }
    assert transcript.missing() == ("a task_started with no task_complete",)


def test_missing_names_a_completed_turn_with_no_usage() -> None:
    lines = [line for line in _step("base") if '"token_usage_record"' not in line]
    transcript, out = _translate(lines)
    assert transcript.missing() == ("a turn with no token_usage_record line",)
    assert _read(out).missing() == (NO_USAGE,)


@pytest.mark.parametrize("launch", list(LAUNCHES))
def test_a_replay_with_skip_reproduces_the_mirror_exactly(launch: str) -> None:
    """A restarted worker rebuilds the translator from the raw mirror and skips what the
    translated mirror already holds, wherever the restart fell, mid-turn included."""
    lines = _launch(launch)
    turn = LAUNCHES[launch][1]
    _, whole = _translate(lines, turn=turn)

    for cut in range(len(lines) + 1):
        before = RolloutTranscript(THREAD, turn=turn)
        held = [o for line in lines[:cut] for o in before.feed(line)]
        after = RolloutTranscript(THREAD, turn=turn, skip=len(held))
        assert [o for line in lines[:cut] for o in after.feed(line)] == []
        rest = [o for line in lines[cut:] for o in after.feed(line)] + after.end_segment()
        assert held + rest == whole, cut


ARGS = {
    "questions": [
        {
            "id": "db",
            "header": "DB",
            "question": "Which database?",
            "options": [
                {"label": "Postgres", "description": ""},
                {"label": "SQLite", "description": ""},
            ],
        }
    ]
}
ASKED = "Which database? [Postgres | SQLite]"
#: What codex 0.159.0 returns for the call in Default mode with the ask tool off.
REFUSED = "request_user_input is unavailable in Default mode"
CALL_AT = datetime(2026, 10, 2, 16, 34, tzinfo=UTC)
OUTPUT_AT = datetime(2026, 10, 2, 16, 35, tzinfo=UTC)


def _response(payload: dict[str, Any], at: datetime) -> str:
    return json.dumps({"timestamp": at.isoformat(), "type": "response_item", "payload": payload})


def _ask_call(call_id: str = "call_1", arguments: str = json.dumps(ARGS)) -> str:
    payload = {
        "type": "function_call",
        "name": "request_user_input",
        "arguments": arguments,
        "call_id": call_id,
    }
    return _response(payload, CALL_AT)


def _ask_output(output: str, call_id: str = "call_1") -> str:
    payload = {"type": "function_call_output", "call_id": call_id, "output": output}
    return _response(payload, OUTPUT_AT)


def _stop(message: str) -> str:
    hook = {"session_id": THREAD, "hook_event_name": "Stop", "last_assistant_message": message}
    return json.dumps({"at": "2026-10-02T16:36:00Z", "hook": hook})


def _asking() -> SessionSignals:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    return signals


def test_a_refused_question_call_never_idles_but_the_turns_stop_asks_it() -> None:
    """Codex refuses the tool in Default mode, and the agent's closing words need not repeat
    the question: read as a plain turn end, the run would finish on a question nobody saw."""
    signals = _asking()
    codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=False)
    assert signals.idle_reason is None
    refused = _ask_output(REFUSED)
    codex_log_signals(signals, refused, observed_at=READ_AT, ask_tool=False)
    assert signals.idle_reason is None

    signals.feed(_stop("Done."), observed_at=READ_AT, session_id=THREAD)
    assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, ASKED)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param('{"answers":{"db":{"answers":["Postgres"]}}}', id="answered"),
        pytest.param('{"answers":{}}', id="self-answered"),
        pytest.param("request_user_input was cancelled", id="error"),
    ],
)
def test_with_the_ask_tool_a_question_call_idles_until_answered(output: str) -> None:
    """Codex answers its own picker with nothing after two minutes: the expiry is what lets
    the worker park first, and an empty answer must not read as a person's."""
    signals = _asking()
    codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=True)
    assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, ASKED)
    assert signals.question_id == "call_1"
    assert signals.question_expires_at == CALL_AT + timedelta(seconds=120)

    codex_log_signals(signals, _ask_output(output, "call_2"), observed_at=READ_AT, ask_tool=True)
    assert (signals.idle_reason, signals.question_id) == (IdleReason.QUESTION, "call_1")

    codex_log_signals(signals, _ask_output(output), observed_at=READ_AT, ask_tool=True)
    assert signals.idle_reason is None
    assert signals.question_expires_at is None
    answered = "Postgres" in output
    assert signals.last_input_at == (OUTPUT_AT if answered else None)

    signals.feed(_stop("Done."), observed_at=READ_AT, session_id=THREAD)
    if answered:
        assert signals.idle_reason is IdleReason.TURN_END
    else:
        assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, ASKED)


ASK_ROLLOUT = (FIXTURES / "ask.rollout.jsonl").read_text("utf-8").splitlines()
ASK_SIGNALS = (FIXTURES / "ask.signals.jsonl").read_text("utf-8").splitlines()
ASK_INDEX: dict[str, dict[str, list[int]]] = json.loads(
    (FIXTURES / "ask.index.json").read_text("utf-8")
)
ASK_THREAD = "01a16743-eaad-7163-8cb9-f00341068a54"
ASK_READ_AT = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
#: What the mock's question is, as the signals show it.
CAPTURED_ASK = "Which database? [Postgres | SQLite]"


def _ask_step(name: str) -> tuple[list[str], list[str]]:
    rollout, signals = ASK_INDEX[name]["rollout"], ASK_INDEX[name]["signals"]
    return ASK_SIGNALS[signals[0] : signals[1]], ASK_ROLLOUT[rollout[0] : rollout[1]]


def _ask_read(signals: SessionSignals, hooks: list[str], rollout: list[str], ask: bool) -> None:
    """One supervisor read of the question-box thread: the signals, then the log."""
    for line in hooks:
        signals.feed(line, observed_at=ASK_READ_AT, session_id=ASK_THREAD)
    signals.settle_turn_ends()
    for line in rollout:
        codex_log_signals(signals, line, observed_at=ASK_READ_AT, ask_tool=ask)


def test_the_captured_question_calls_signal_as_the_rules_say() -> None:
    """Codex refuses the call within the turn, and answers its own picker with nothing two
    minutes on (`test_codex_tui_local.py`). A read takes the signals before the log, so the
    turn's Stop is often read before the call or the output it follows; either order must end
    the turn on the question."""
    hooks, rollout = _ask_step("refused")
    signals = SessionSignals()
    signals.start_segment(turn=False)
    _ask_read(signals, hooks, rollout, ask=False)
    assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, CAPTURED_ASK)

    hooks, rollout = _ask_step("self_answered")
    call = _where(rollout, "response_item", "function_call")
    output = _where(rollout, "response_item", "function_call_output")
    stop = next(i for i, line in enumerate(hooks) if '"Stop"' in line)
    called_at = datetime.fromisoformat(json.loads(rollout[call])["timestamp"])
    for stop_first in (False, True):
        signals = SessionSignals()
        signals.start_segment(turn=False)
        _ask_read(signals, hooks[:stop], rollout[: call + 1], ask=True)
        assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, CAPTURED_ASK)
        assert signals.question_expires_at == called_at + timedelta(seconds=120)
        if stop_first:
            _ask_read(signals, hooks[stop:], rollout[call + 1 :], ask=True)
        else:
            _ask_read(signals, [], rollout[call + 1 : output + 1], ask=True)
            assert (signals.idle_reason, signals.question_expires_at) == (None, None)
            _ask_read(signals, hooks[stop:], rollout[output + 1 :], ask=True)
        assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, CAPTURED_ASK)
        assert signals.question_expires_at is None
        assert signals.at_plain_prompt


@pytest.mark.parametrize("call_read_first", [False, True], ids=["one-read", "call-read-first"])
@pytest.mark.parametrize(
    ("ask_tool", "output", "asked"),
    [
        pytest.param(False, REFUSED, True, id="refused"),
        pytest.param(True, '{"answers":{}}', True, id="self-answered"),
        pytest.param(True, '{"answers":{"db":{"answers":["Postgres"]}}}', False, id="answered"),
    ],
)
def test_a_question_call_read_after_its_turns_stop_still_decides_how_the_turn_ended(
    ask_tool: bool, output: str, asked: bool, call_read_first: bool
) -> None:
    """codex logs the call and its output just before the turn's Stop, and a read takes the
    signals before the log: settled by the Stop alone, the turn would end silent on a question
    nobody saw, or idle on one a person had answered."""
    signals = _asking()
    if call_read_first:
        codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=ask_tool)
    signals.feed(_stop("Done."), observed_at=READ_AT, session_id=THREAD)
    if not call_read_first:
        codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=ask_tool)
    codex_log_signals(signals, _ask_output(output), observed_at=READ_AT, ask_tool=ask_tool)

    assert signals.question_expires_at is None
    if asked:
        assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, ASKED)
        assert signals.at_plain_prompt
    else:
        assert signals.idle_reason is IdleReason.TURN_END
        assert signals.last_input_at == OUTPUT_AT


@pytest.mark.parametrize(
    ("ask_tool", "output"),
    [
        pytest.param(False, REFUSED, id="refused"),
        pytest.param(True, '{"answers":{}}', id="self-answered"),
        pytest.param(True, '{"answers":{"db":{"answers":["Postgres"]}}}', id="answered"),
    ],
)
def test_a_question_call_read_after_the_next_prompt_leaves_that_turn_running(
    ask_tool: bool, output: str
) -> None:
    """The call belongs to the turn that already ended: idling on it would show a working
    agent as waiting, and a finish could land on it mid-turn."""
    signals = _asking()
    prompt = {"session_id": THREAD, "hook_event_name": "UserPromptSubmit", "prompt": "Go on."}
    for line in (_stop("Done."), json.dumps({"at": "2026-10-02T16:36:30Z", "hook": prompt})):
        signals.feed(line, observed_at=READ_AT, session_id=THREAD)
    for line in (_ask_call(), _ask_output(output)):
        codex_log_signals(signals, line, observed_at=READ_AT, ask_tool=ask_tool)
    assert signals.idle_reason is None

    signals.feed(_stop("Done."), observed_at=READ_AT, session_id=THREAD)
    assert signals.idle_reason is IdleReason.TURN_END


ANSWERED = '{"answers":{"db":{"answers":["Postgres"]}}}'


def test_a_stop_read_before_an_answered_calls_output_keeps_its_closing_question() -> None:
    """The agent asks again in its closing words once the picker is answered: read either way
    round, the turn must end on that question, or the run finishes past it."""
    closing = "Done. Should I also add an index?"
    states = []
    for stop_first in (False, True):
        signals = _asking()
        codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=True)
        if stop_first:
            signals.feed(_stop(closing), observed_at=READ_AT, session_id=THREAD)
        codex_log_signals(signals, _ask_output(ANSWERED), observed_at=READ_AT, ask_tool=True)
        if not stop_first:
            signals.feed(_stop(closing), observed_at=READ_AT, session_id=THREAD)
        states.append((signals.idle_reason, signals.question))
    assert states == [(IdleReason.QUESTION, closing)] * 2


@pytest.mark.parametrize(
    ("ask_tool", "output"),
    [pytest.param(False, REFUSED, id="refused"), pytest.param(True, '{"answers":{}}', id="on")],
)
def test_a_log_two_turns_behind_pins_no_old_question_on_the_latest_turn(
    ask_tool: bool, output: str
) -> None:
    """One read can take two whole turns' hooks: a call read then is the first turn's, and
    idling the second turn's end on it would park a run on a question already behind it."""
    signals = _asking()
    prompt = {"session_id": THREAD, "hook_event_name": "UserPromptSubmit", "prompt": "Go on."}
    for line in (
        _stop("Done."),
        json.dumps({"at": "2026-10-02T16:36:30Z", "hook": prompt}),
        _stop("Done."),
    ):
        signals.feed(line, observed_at=READ_AT, session_id=THREAD)
    for line in (_ask_call(), _ask_output(output)):
        codex_log_signals(signals, line, observed_at=READ_AT, ask_tool=ask_tool)
    assert (signals.idle_reason, signals.question) == (IdleReason.TURN_END, None)


def test_a_turn_aborted_under_a_live_picker_leaves_the_session_at_its_plain_prompt() -> None:
    """Esc takes the picker with the turn: left on the question, a met finish would hang the
    session up rather than quit it, and the worker would park early on a question gone."""
    signals = _asking()
    codex_log_signals(signals, _ask_call(), observed_at=READ_AT, ask_tool=True)
    aborted = _msg('{"type":"turn_aborted","reason":"interrupted"}')
    codex_log_signals(signals, aborted, observed_at=READ_AT, ask_tool=True)
    assert signals.idle_reason is IdleReason.TURN_END
    assert (signals.question_expires_at, signals.question_id) == (None, None)
    assert signals.at_plain_prompt


def test_a_question_call_with_unreadable_arguments_still_idles_on_a_question() -> None:
    signals = _asking()
    codex_log_signals(signals, _ask_call(arguments="{"), observed_at=READ_AT, ask_tool=True)
    assert (signals.idle_reason, signals.question) == (IdleReason.QUESTION, None)


SURROGATE = "\\ud800"


def _msg(payload: str) -> str:
    return '{"timestamp":"2026-10-02T16:34:00Z","type":"event_msg","payload":' + payload + "}"


def _item(item: str) -> str:
    return _msg('{"type":"item_completed","item":' + item + "}")


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param(
            '{"type":"token_usage_record","payload":{"turn_token_usage":{"input_tokens":'
            + "9" * 5000
            + "}}}",
            id="an-integer-too-long-to-parse",
        ),
        pytest.param(
            json.dumps(
                {
                    "type": "token_usage_record",
                    "payload": {
                        "turn_token_usage": {
                            "input_tokens": 10**30,
                            "cached_input_tokens": -5,
                            "cache_write_input_tokens": True,
                            "output_tokens": "7",
                        }
                    },
                }
            ),
            id="counts-past-a-bigint-negative-and-not-integers",
        ),
        pytest.param(
            '{"type":"token_usage_record","payload":{"turn_token_usage":[1]}}',
            id="a-list-for-a-usage",
        ),
        pytest.param(_msg("[" * 100_000 + "]" * 100_000), id="deep-nesting"),
        pytest.param(
            _item('{"type":"UserMessage","content":[{"type":"text","text":"' + SURROGATE + '"}]}'),
            id="a-lone-surrogate-in-user-text",
        ),
        pytest.param(
            _item('{"type":"AgentMessage","content":[{"type":"Text","text":"' + SURROGATE + '"}]}'),
            id="a-lone-surrogate-in-agent-text",
        ),
        pytest.param(
            _item(
                json.dumps(
                    {
                        "type": "CommandExecution",
                        "id": 7,
                        "command": {"a": 1},
                        "aggregated_output": 5,
                        "exit_code": "0",
                        "status": ["x"],
                    }
                )
            ),
            id="a-command-of-wrong-types",
        ),
        pytest.param(
            _item(
                json.dumps(
                    {
                        "type": "CommandExecution",
                        "command": [1, "x"],
                        "exit_code": 2**70,
                        "aggregated_output": "\udc80",
                    }
                )
            ),
            id="a-command-argv-of-mixed-types",
        ),
        pytest.param(_msg('["x"]'), id="a-list-for-a-payload"),
        pytest.param(_msg('{"type":"item_completed","item":"x"}'), id="a-string-for-an-item"),
        pytest.param(_item('{"type":"UserMessage","content":{"a":1}}'), id="content-not-a-list"),
        pytest.param(
            '{"timestamp":5,"type":"event_msg","payload":{"type":"task_complete","error":[1]}}',
            id="a-list-for-an-error",
        ),
        pytest.param(
            _msg('{"type":"task_complete","error":{"message":5}}'), id="an-error-of-wrong-type"
        ),
        pytest.param(
            _msg('{"type":"error","message":"' + SURROGATE + '"}'), id="a-lone-surrogate-in-error"
        ),
        pytest.param('{"type":["x"]}', id="a-list-for-a-type"),
        pytest.param(
            _item(
                json.dumps(
                    {"type": "AgentMessage", "content": [{"type": "Text", "text": "x" * (1 << 20)}]}
                )
            ),
            id="a-1-mib-text",
        ),
    ],
)
def test_no_agent_written_rollout_line_can_break_the_reader(line: str) -> None:
    """The rollout is the agent's to write. A line that raised would stop the turn cap with it;
    one that wrote a value nothing downstream can hold fails later."""
    started = ROLLOUT[_where(ROLLOUT, "event_msg", "task_started")]
    transcript = RolloutTranscript(THREAD, turn=True)
    out = transcript.feed(started) + transcript.feed(line) + transcript.end_segment()
    signals = SessionSignals()
    signals.start_segment(turn=True)
    codex_log_signals(signals, line, observed_at=READ_AT)

    reader = StreamReader()
    for each in out:
        each.encode("utf-8")
        reader.feed(each)
        event = json.loads(each)
        assert all(0 <= count < 2**63 for count in event.get("usage", {}).values())
        code = event.get("item", {}).get("exit_code")
        assert code is None or -(2**31) <= code < 2**31
    TypeAdapter(str | None).dump_json(reader.summary)
    signals.model_dump_json()


def test_a_compressed_rollout_with_no_plain_file_reads_as_absent_not_shrunk() -> None:
    """With `local_thread_store_compression` on, a week-old rollout is `.jsonl.zst` until a
    resume restores it byte for byte: a shrink would fail a run for its retention's edge."""
    reader = cast(Any, SessionReader())
    reader._state = SimpleNamespace(log_read=4096, signals_offset=0)
    polled = SimpleNamespace(signals=b"", signals_size=0, log=b"", log_size=None)
    assert reader._shrunk(polled) == ()
