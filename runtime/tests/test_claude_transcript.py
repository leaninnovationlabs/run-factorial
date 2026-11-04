"""The claude session log, translated into the stream the reader already reads, and the hook
signals that say whether the session is at its prompt.

The fixtures under `fixtures/claude-2.1.284/` are sessions the #243 probe captured from the real
2.1.284 TUI in tmux against a mock API: paths rewritten to the sandbox's, attachment payloads and
image data cut, the mock's one `msg_mock` id made unique per request, and each signal wrapped as
`signal.sh` writes it, its `at` taken from the matching log line. `base` is two segments: a
launch with a turn, a typed human turn and `/exit`, then `--resume` with a turn and `/exit`.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from runtime.adapters._cli.session_signals import (
    COUNT_CAP,
    QUESTION_LIMIT,
    STOP_HOOK_MISSING,
    SUMMARY_LIMIT,
    SessionSignals,
    clip,
    plus,
)
from runtime.adapters.claude_cli.session import ClaudeTui
from runtime.adapters.claude_cli.stream import StreamReader, add
from runtime.adapters.claude_cli.transcript import Transcript, claude_log_signals
from runtime.contract import IdleReason, RunUsage

FIXTURES = Path(__file__).parent / "fixtures" / "claude-2.1.284"
SID = "9289de8c-5d93-4ad6-b11c-81560f600587"
#: `base`'s first line written by the `--resume` launch.
BASE_RESUME = 31


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.session.jsonl").read_text("utf-8").splitlines()


def _signals(name: str) -> list[str]:
    return (FIXTURES / f"{name}.signals.jsonl").read_text("utf-8").splitlines()


def _translate(
    lines: list[str], *, turn: bool = True, stop_message: str | None = None, **kw: Any
) -> tuple[Transcript, list[dict[str, Any]]]:
    transcript = Transcript(SID, turn=turn, **kw)
    out = [o for line in lines for o in transcript.feed(line)]
    out += transcript.end_segment(stop_message=stop_message)
    return transcript, [json.loads(o) for o in out]


def _read(events: list[dict[str, Any]], *, prior_turns: int = 0) -> StreamReader:
    reader = StreamReader(prior_turns=prior_turns)
    for event in events:
        reader.feed(json.dumps(event))
    return reader


def _with_cost(lines: list[str], usd: float) -> list[str]:
    def edit(line: str) -> str:
        record = json.loads(line)
        if record.get("type") != "cost-state":
            return line
        record["totalCostUSD"] = usd
        return json.dumps(record)

    return [edit(line) for line in lines]


def _of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind]


def test_a_message_split_over_lines_is_one_turn() -> None:
    """The CLI writes one line per content block, all under one message id. Emitting each line
    as an event would count every block as a turn: caps fire early and transcripts triple."""
    (tool_line,) = [line for line in _lines("ask") if '"tool_use"' in line]
    record = json.loads(tool_line)
    record["requestId"] = "req_011CTest"
    tool_use = record["message"]["content"][0]
    blocks = [{"type": "thinking", "thinking": "pick one"}, {"type": "text", "text": "Asking."}]
    split = []
    for block in [*blocks, tool_use]:
        record["message"]["content"] = [block]
        split.append(json.dumps(record))
    answer = next(line for line in _lines("ask") if '"toolUseResult"' in line)

    _, events = _translate([*split, answer])

    (assistant,) = _of(events, "assistant")
    assert [b["type"] for b in assistant["message"]["content"]] == ["thinking", "text", "tool_use"]
    assert assistant["request_id"] == "req_011CTest"
    assert _read(events).turns == 1


def test_claude_live_usage_counts_each_message_once_and_never_the_result() -> None:
    """The live figure is summed off the messages. A message split over lines, reopened after
    another line, or read again by a restarted worker counts once, at its latest usage, and the
    result line that repeats the segment adds nothing: each would silently inflate spend."""
    lines = _lines("ask")
    (tool_line,) = [line for line in lines if '"tool_use"' in line]
    record = json.loads(tool_line)
    tool_use = record["message"]["content"][0]
    split = []
    for block, output in [({"type": "thinking", "thinking": "pick one"}, 3), (tool_use, 20)]:
        record["message"]["content"] = [block]
        record["message"]["usage"]["output_tokens"] = output
        split.append(json.dumps(record))
    at = lines.index(tool_line)
    answered = next(n for n, line in enumerate(lines) if '"toolUseResult"' in line)
    edited = [*lines[:at], *split, *lines[at + 1 : answered + 1], split[-1], *lines[answered + 1 :]]

    _, events = _translate(edited)
    reader = _read(events)

    ids = [event["message"]["id"] for event in _of(events, "assistant")]
    assert ids == ["msg_01ask0001", "msg_01ask0001", "msg_01ask0002"]
    (result,) = _of(events, "result")
    assert (result["usage"]["input_tokens"], result["usage"]["output_tokens"]) == (40, 35)
    so_far = RunUsage(input_tokens=20, output_tokens=25)
    assert reader.live_usage == so_far
    for event in events:
        reader.feed(json.dumps(event))
    assert reader.live_usage == so_far

    # A `--resume` writes only its own messages: the second segment adds one on the first's.
    base = _lines("base")
    first = _read(_translate(base[:BASE_RESUME])[1]).live_usage
    second = _read(_translate(base[BASE_RESUME:])[1]).live_usage
    assert plus(first, second) == RunUsage(input_tokens=30, output_tokens=15)


def test_a_claude_end_with_no_cost_reports_the_live_figure() -> None:
    """A killed CLI writes no cost, so the end is priced at list from its tokens. Without its
    cache writes the end would price them as plain input, below the live figure it replaces."""

    def cached(line: str) -> str:
        record = json.loads(line)
        if record.get("type") == "assistant":
            record["message"]["usage"].update(
                cache_creation_input_tokens=7, cache_read_input_tokens=3
            )
        return json.dumps(record)

    lines = [cached(line) for line in _lines("ask") if '"cost-state"' not in line]
    transcript, events = _translate(lines)
    reader = _read(events)
    counts = ClaudeTui().tally(reader, transcript=transcript, stop_summary=None).counts

    live = reader.live_usage
    assert counts["cost_usd"] is None and live is not None
    ended = tuple(counts.get(name) for name in RunUsage.model_fields)
    assert ended == tuple(live.model_dump().values()) == (40, 6, 14, 25)


def test_a_forged_live_usage_never_goes_negative_or_past_a_bigint() -> None:
    """The log is the agent's to write. A count `step_run` cannot hold fails every tick."""
    reader = StreamReader()
    for n, (fresh, out) in enumerate([(-50, 2**62), (7, 2**62)]):
        usage = {"input_tokens": fresh, "output_tokens": out}
        reader.feed(json.dumps({"type": "assistant", "message": {"id": f"m{n}", "usage": usage}}))

    assert reader.live_usage == RunUsage(input_tokens=7, output_tokens=COUNT_CAP)


def test_synthetic_messages_are_never_turns_and_an_api_error_fails_the_segment() -> None:
    _, events = _translate(_lines("auth-401"))
    reader = _read(events)

    assert _of(events, "assistant") == []
    assert reader.turns == 0
    (result,) = _of(events, "result")
    assert result["subtype"] == "error_during_execution"
    assert result["is_error"] is True
    assert "401 API key is invalid" in (reader.error() or "")


@pytest.mark.parametrize(
    ("turn", "origins"), [(True, ["orchestrator", "human"]), (False, ["human", "human"])]
)
def test_the_first_user_line_of_a_segment_is_the_orchestrators_and_later_ones_are_human(
    turn: bool, origins: list[str]
) -> None:
    """The CLI writes the launch prompt as `origin.kind: human`, exactly like a typed turn; only
    its position tells them apart. A segment launched without a turn has no orchestrator line."""
    _, events = _translate(_lines("base")[:BASE_RESUME], turn=turn)

    users = [e for e in _of(events, "user") if "factorial_origin" in e]
    assert [u["factorial_origin"] for u in users] == origins
    assert users[0]["message"]["content"][0]["text"].startswith("Reply with exactly the word PONG")


def test_cost_is_a_per_segment_delta_of_the_cumulative_cost_state() -> None:
    """cost-state is the session's running total, written at exit. Reporting it whole for each
    segment would bill a resumed session for every earlier segment again."""
    lines = _lines("base")
    first, second = lines[:BASE_RESUME], lines[BASE_RESUME:]

    one, events_one = _translate(_with_cost(first, 0.10))
    two, events_two = _translate(_with_cost(second, 0.25), cost_base=one.cost_seen)
    (result_one,), (result_two,) = _of(events_one, "result"), _of(events_two, "result")

    assert _read(events_one).cost_usd == Decimal("0.1")
    assert _read(events_two).cost_usd == Decimal("0.15")
    assert add(_read(events_one).cost_usd, _read(events_two).cost_usd) == Decimal("0.25")
    # Tokens are deltas of the same running total: modelUsage counted 50, then 70, inputs.
    assert result_one["usage"]["input_tokens"] == 50
    assert result_two["usage"]["input_tokens"] == 20

    _, rework = _translate(_with_cost(second, 0.30), cost_base=two.cost_seen)
    assert _read(rework).cost_usd == Decimal("0.05")

    killed = [line for line in second if '"cost-state"' not in line]
    _, events = _translate(killed, cost_base=one.cost_seen)
    (result,) = _of(events, "result")
    assert "total_cost_usd" not in result
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5


def test_an_attempt_translates_only_its_own_slice() -> None:
    lines = _lines("base")
    earlier, _ = _translate(lines[:BASE_RESUME])

    _, events = _translate(lines[BASE_RESUME:], cost_base=earlier.cost_seen)
    reader = _read(events)

    texts = [json.dumps(e) for e in events]
    assert not any("PONG. Do not" in t or "ACK" in t for t in texts)
    assert reader.turns == 1
    assert reader.cost_usd is not None
    assert float(reader.cost_usd) == pytest.approx(0.000245 - 0.000175)


def test_a_segment_cut_before_the_log_caught_up_reports_the_stop_hooks_message() -> None:
    """Stop can fire before the log holds the turn's last line; a hangup then leaves no text."""
    lines = _lines("base")[:BASE_RESUME]
    cut = [line for line in lines if '"assistant"' not in line]

    _, events = _translate(cut, stop_message="PONG from the hook.")

    assert _read(events).summary == "PONG from the hook."
    assert _read(_translate(lines, stop_message="stale")[1]).summary == "PONG from mock."


def test_askuserquestion_answers_are_extracted_field_by_field() -> None:
    lines = _lines("ask")
    index = next(i for i, line in enumerate(lines) if '"toolUseResult"' in line)
    record = json.loads(lines[index])
    record["toolUseResult"]["stdout"] = "ghs_secret"
    record["toolUseResult"]["answers"]["Second?"] = {"label": "ghs_nested"}
    lines[index] = json.dumps(record)

    _, events = _translate(lines)

    (answered,) = [e for e in _of(events, "user") if "tool_use_result" in e]
    assert answered["tool_use_result"] == {"answers": {"Include archived rows?": "Active only"}}
    assert answered["message"]["content"][0]["type"] == "tool_result"
    assert not any("ghs_" in json.dumps(e) for e in events)


T0 = datetime(2026, 10, 1, 21, 47, 20, tzinfo=UTC)


def _hook(event: str, at: datetime | str = T0, **fields: Any) -> str:
    stamp = at if isinstance(at, str) else at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"at": stamp, "hook": {"hook_event_name": event, **fields}})


ASK = json.loads(_signals("ask")[2])["hook"]


def test_signals_drive_idle_and_active() -> None:
    later = T0 + timedelta(seconds=30)
    signals = SessionSignals()
    signals.start_segment(turn=True)

    signals.feed(_hook("SessionStart"), observed_at=T0)
    assert signals.idle_reason is None
    signals.feed(_hook("UserPromptSubmit", prompt="the task"), observed_at=T0)
    assert signals.last_input_at is None  # the orchestrator's own launch prompt

    signals.feed(_hook("PreToolUse", T0 + timedelta(seconds=1), **_ask()), observed_at=later)
    assert signals.idle_reason is IdleReason.QUESTION
    assert signals.idle_since == T0 + timedelta(seconds=1)
    assert signals.question == "Include archived rows? [Active only | Active and archived]"

    answered = T0 + timedelta(seconds=5)
    signals.feed(_hook("PostToolUse", answered, **_ask()), observed_at=later)
    assert (signals.idle_reason, signals.question) == (None, None)
    assert signals.last_input_at == answered

    signals.feed(
        _hook("Stop", T0 + timedelta(seconds=6), last_assistant_message="Done."), observed_at=later
    )
    signals.feed(
        _hook("Stop", T0 + timedelta(seconds=9), last_assistant_message="Done."), observed_at=later
    )
    assert signals.idle_reason is IdleReason.TURN_END
    assert signals.idle_since == T0 + timedelta(seconds=6)  # a repeat keeps the earlier since
    assert signals.summary == "Done."

    typed = T0 + timedelta(seconds=10)
    signals.feed(_hook("UserPromptSubmit", typed, prompt="more"), observed_at=later)
    assert (signals.idle_reason, signals.last_input_at) == (None, typed)

    # A forged future `at` cannot date an idle spell past the moment it was seen.
    future = T0 + timedelta(hours=1)
    signals.feed(
        _hook("StopFailure", future, error="authentication_failed", last_assistant_message="401"),
        observed_at=later,
    )
    assert (signals.idle_reason, signals.idle_since) == (IdleReason.TURN_ERROR, later)
    assert signals.turn_error == "authentication_failed: 401"

    signals.feed(_hook("SessionEnd", "not a time", reason="other"), observed_at=later)
    assert signals.ended

    resumed = SessionSignals()
    resumed.start_segment(turn=False)
    resumed.feed(_hook("SessionStart", T0, source="resume"), observed_at=later)
    assert (resumed.idle_reason, resumed.idle_since) == (IdleReason.TURN_END, T0)
    resumed.feed(_hook("UserPromptSubmit", typed, prompt="hi"), observed_at=later)
    assert resumed.last_input_at == typed  # no turn was launched, so the first one is a human's
    # An auto-compaction starts a session mid-turn; the agent is still working.
    resumed.feed(_hook("SessionStart", typed, source="compact"), observed_at=later)
    resumed.feed(_hook("SessionStart", typed), observed_at=later)
    assert resumed.idle_reason is None

    with pytest.raises(ValueError):
        resumed.feed(_hook("Stop"), observed_at=datetime(2026, 10, 1))


def test_a_turnless_launch_after_a_failed_turn_idles_on_that_failure() -> None:
    """A rollover over a failed turn comes back at the same prompt: still the failure, so the
    worker sees no new turn end."""
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("StopFailure", error="rate_limit"), observed_at=T0)
    signals.start_segment(turn=False)
    signals.feed(_hook("SessionStart", T0, source="resume"), observed_at=T0)
    assert signals.idle_reason is IdleReason.TURN_ERROR


def _ask() -> dict[str, Any]:
    return {k: ASK[k] for k in ("tool_name", "tool_input", "tool_use_id")}


_LONG = "Context. " * 60 + "Ship it?"


@pytest.mark.parametrize(
    ("message", "reason", "question"),
    [
        ("Opened PR #9.", IdleReason.TURN_END, None),
        (
            "I found two schemas.\n\nWhich one should I migrate first?",
            IdleReason.QUESTION,
            "Which one should I migrate first?",
        ),
        ("**Postgres or SQLite?**\n\n", IdleReason.QUESTION, "**Postgres or SQLite?**"),
        ("What next?\n\n```\nls\n```", IdleReason.TURN_END, None),
        (
            "Added the query:\n\n```sql\nSELECT * FROM t WHERE id = ?\n```",
            IdleReason.TURN_END,
            None,
        ),
        ("Which one?\n\n**", IdleReason.QUESTION, "Which one?"),
        (_LONG, IdleReason.QUESTION, ...),
    ],
)
def test_a_turn_that_ends_on_a_question_idles_on_it(
    message: str, reason: IdleReason, question: str | None
) -> None:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("UserPromptSubmit", prompt="go"), observed_at=T0)
    signals.feed(_hook("Stop", last_assistant_message=message), observed_at=T0)
    if question is ...:
        q = signals.question
        assert q is not None and len(q) == QUESTION_LIMIT
        assert q.startswith("…") and q.endswith("Ship it?")
        question = q
    assert (signals.idle_reason, signals.question) == (reason, question)
    assert signals.summary == clip(message, SUMMARY_LIMIT)


def test_an_answer_to_an_earlier_question_tool_call_does_not_answer_one_asked_in_words() -> None:
    later = T0 + timedelta(minutes=1)
    answer = next(line for line in _lines("ask") if '"toolUseResult"' in line)
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("PreToolUse", **_ask()), observed_at=later)
    signals.feed(_hook("PostToolUse", **_ask()), observed_at=later)
    signals.feed(_hook("Stop", last_assistant_message="Shall I open the PR?"), observed_at=later)
    claude_log_signals(signals, answer, observed_at=later)
    assert signals.idle_reason is IdleReason.QUESTION


def test_a_new_turn_clears_the_last_turns_closing_words() -> None:
    signals = SessionSignals()
    signals.start_segment(turn=True)
    signals.feed(_hook("Stop", last_assistant_message="Done."), observed_at=T0)
    signals.feed(_hook("UserPromptSubmit", prompt="more"), observed_at=T0)
    assert signals.summary is None

    kept = SessionSignals()
    kept.start_segment(turn=True)
    kept.feed(_hook("Stop", last_assistant_message="Done."), observed_at=T0)
    kept.start_segment(turn=False)
    assert kept.summary == "Done."


def test_the_log_ends_a_turn_or_answers_a_question_when_a_hook_did_not_fire() -> None:
    """An agent can delete its hooks between launches. The log still shows the turn end."""
    later = T0 + timedelta(minutes=1)
    turn_end = next(line for line in _lines("base") if '"turn_duration"' in line)
    answer = next(line for line in _lines("ask") if '"toolUseResult"' in line)

    # A fast turn: its Stop and the next typed prompt are read before its own turn_duration.
    hooked = SessionSignals()
    hooked.start_segment(turn=True)
    hooked.feed(_hook("Stop", T0, last_assistant_message="x"), observed_at=later)
    hooked.feed(_hook("UserPromptSubmit", T0, prompt="next"), observed_at=later)
    claude_log_signals(hooked, turn_end, observed_at=later)
    assert (hooked.idle_reason, hooked.warnings) == (None, ())

    unhooked = SessionSignals()
    unhooked.start_segment(turn=True)
    unhooked.feed(_hook("UserPromptSubmit", prompt="the task"), observed_at=T0)
    claude_log_signals(unhooked, turn_end, observed_at=later)
    assert unhooked.idle_reason is IdleReason.TURN_END
    assert unhooked.idle_since == datetime(2026, 10, 1, 21, 47, 20, 237000, tzinfo=UTC)
    unhooked.settle_turn_ends()  # the next read's signals carried no Stop either
    assert unhooked.warnings == (STOP_HOOK_MISSING,)
    # The lost Stop is credited: the next turns, hooked again, are not taken for hookless ones.
    unhooked.feed(_hook("UserPromptSubmit", prompt="two"), observed_at=later)
    unhooked.feed(_hook("Stop", last_assistant_message="x"), observed_at=later)
    unhooked.feed(_hook("UserPromptSubmit", prompt="three"), observed_at=later)
    claude_log_signals(unhooked, turn_end, observed_at=later)
    assert unhooked.idle_reason is None

    asked = SessionSignals()
    asked.start_segment(turn=True)
    asked.feed(_hook("PreToolUse", **_ask()), observed_at=T0)
    claude_log_signals(asked, answer, observed_at=later)
    assert asked.idle_reason is None
    assert asked.last_input_at == datetime(2026, 10, 1, 21, 47, 28, 113000, tzinfo=UTC)


@pytest.mark.parametrize("name", ["base", "ask", "image", "image-paste"])
def test_replay_after_a_restart_neither_repeats_nor_drops_a_line(name: str) -> None:
    """A restarted worker rebuilds the translator from the raw mirror and skips what the
    translated mirror already holds, wherever the restart fell — mid-message included."""
    lines = _lines(name)[:BASE_RESUME]
    one_pass = Transcript(SID, turn=True)
    whole = [o for line in lines for o in one_pass.feed(line)] + one_pass.end_segment()

    for cut in range(len(lines) + 1):
        before = Transcript(SID, turn=True)
        held = [o for line in lines[:cut] for o in before.feed(line)]
        after = Transcript(SID, turn=True, skip=len(held))
        replayed = [o for line in lines[:cut] for o in after.feed(line)]
        rest = [o for line in lines[cut:] for o in after.feed(line)] + after.end_segment()
        assert replayed == []
        assert held + rest == whole, cut


@pytest.mark.parametrize(
    ("name", "start", "stop"),
    [
        ("base", 0, BASE_RESUME),
        ("base", BASE_RESUME, None),
        ("ask", 0, None),
        ("image", 0, None),
        ("image-paste", 0, None),
        ("auth-401", 0, None),
    ],
)
def test_every_captured_2_1_284_session_translates_with_nothing_the_reader_needs_missing(
    name: str, start: int, stop: int | None
) -> None:
    transcript, events = _translate(_lines(name)[start:stop])
    reader = _read(events)

    assert transcript.unknown_types == {}
    assert transcript.missing() == ()
    assert reader.session_id == SID
    if name == "auth-401":
        assert reader.missing() == ("no assistant event counted as a turn",)
    else:
        assert reader.missing() == ()
        assert reader.error() is None
    assert "FIXTURETRUNCATED" not in json.dumps(events)  # no image data reaches the stream

    transcript.feed(json.dumps({"type": "never-seen-before"}))
    assert transcript.unknown_types == {"never-seen-before": 1}


@pytest.mark.parametrize("kind", ["api-request", "api-request-blob", "api-request-shape"])
def test_2_1_310s_api_request_records_add_nothing_and_are_known(kind: str) -> None:
    """2.1.310 records each request it sends: the system prompt and tools (`-shape`), the
    message bodies by hash (`-blob`), and the parameters. Each turn is already its own line."""
    transcript = Transcript(SID, turn=True)

    emitted = transcript.feed(json.dumps({"type": kind, "sessionId": SID}))

    assert emitted == Transcript(SID, turn=True).feed("")  # the segment's start, nothing more
    assert transcript.unknown_types == {}


SURROGATE = "\\ud800"


@pytest.mark.parametrize(
    ("log", "signal"),
    [
        pytest.param(
            '{"type":"cost-state","totalCostUSD":' + "9" * 5000 + "}",
            '{"at":"x","hook":{"hook_event_name":"Stop","n":' + "9" * 5000 + "}}",
            id="an-integer-too-long-to-parse",
        ),
        pytest.param(
            json.dumps(
                {
                    "type": "cost-state",
                    "totalCostUSD": 0.1,
                    "modelUsage": {"m": {"inputTokens": 10**30}},
                }
            ),
            _hook("Stop"),
            id="a-count-past-a-bigint",
        ),
        pytest.param(
            '{"type":"user","message":' + "[" * 100_000 + "]" * 100_000 + "}",
            '{"hook":' + "[" * 100_000 + "]" * 100_000 + "}",
            id="deep-nesting",
        ),
        *(
            pytest.param(
                '{"type":"cost-state","totalCostUSD":' + value + "}",
                '{"at":' + value + ',"hook":{"hook_event_name":"Stop"}}',
                id=value,
            )
            for value in ("NaN", "Infinity", "1e400")
        ),
        pytest.param(
            '{"type":"user","message":{"role":"user","content":"' + SURROGATE + '"}}',
            '{"at":"x","hook":{"hook_event_name":"Stop","last_assistant_message":"'
            + SURROGATE
            + '"}}',
            id="a-lone-surrogate",
        ),
        pytest.param(
            '{"type":"assistant","message":{"id":"msg_1","model":"m","content":'
            '[{"type":"text","text":"' + SURROGATE + '"}],"usage":{"output_tokens":1}}}',
            _hook("Stop"),
            id="a-lone-surrogate-in-assistant-text",
        ),
        pytest.param(
            '{"type":["x"]}', '{"hook":{"hook_event_name":["x"]}}', id="a-list-for-a-type"
        ),
    ],
)
def test_no_agent_written_line_can_break_the_reader(log: str, signal: str) -> None:
    """The log and the signals are the agent's to write. A line that raised would stop the turn
    cap and the cost with it; one that wrote a value nothing downstream can hold fails later."""
    transcript = Transcript(SID, turn=True)
    out = transcript.feed(log) + transcript.end_segment()
    signals = SessionSignals()
    signals.start_segment(turn=False)
    signals.feed(signal, observed_at=T0)
    claude_log_signals(signals, log, observed_at=T0)

    reader = StreamReader()
    for line in out:
        line.encode("utf-8")
        reader.feed(line)
    # What a consumer decodes, not only the line, must be UTF-8: the summary reaches a state file.
    TypeAdapter(str).dump_json(reader.summary)
    result = json.loads(out[-1])
    assert math.isfinite(result.get("total_cost_usd", 0))
    assert all(0 <= count < 2**63 for count in result["usage"].values())
    signals.model_dump_json()
