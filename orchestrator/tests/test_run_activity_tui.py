"""The transcript parser on an interactive run: the session log, translated by the runtime
(`claude_cli/transcript.py`, `codex_cli/transcript.py`), reads as the same turns a headless
stream does.

The fixtures are real TUI sessions, claude 2.1.284's from the #243 probe and codex 0.159.0's from
its L1 (runtime/tests/fixtures).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orchestrator.api.activity import parse_activity
from runtime.adapters.claude_cli.transcript import Transcript
from runtime.adapters.codex_cli.transcript import RolloutTranscript

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURES = Path(__file__).parents[2] / "runtime/tests/fixtures"
ASK = FIXTURES / "claude-2.1.284/ask.session.jsonl"
CODEX = FIXTURES / "codex-0.159.0"


async def _translated(lines: list[str]) -> AsyncIterator[str]:
    transcript = Transcript("s1", turn=True)
    for line in lines:
        for event in transcript.feed(line):
            yield event
    for event in transcript.end_segment():
        yield event


async def test_a_message_split_over_lines_is_one_activity_turn() -> None:
    """The CLI writes each content block of a response on its own line. Read unmerged, every
    block would be a turn of its own."""
    lines = ASK.read_text("utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if '"tool_use"' in line)
    record = json.loads(lines[index])
    tool_use = record["message"]["content"][0]
    split = []
    for block in ({"type": "text", "text": "Asking."}, tool_use):
        record["message"]["content"] = [block]
        split.append(json.dumps(record))
    lines[index : index + 1] = split

    activity = await parse_activity(_translated(lines), run_id="r1")

    asked, answered = activity.turns
    assert asked.text == ["Asking."]
    assert [(c.tool, c.ok) for c in asked.tools] == [("AskUserQuestion", True)]
    assert answered.text == ["PONG from mock."]
    assert activity.milestones == ["session started"]
    assert activity.outcome == "success"
    assert activity.cost_usd == pytest.approx(0.000215)


async def test_an_unanswered_question_has_no_answer() -> None:
    """The log as it stands while the agent waits on the human: the call is there, its answer is
    not, and the menu's options are not an answer."""
    lines = ASK.read_text("utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if '"tool_use"' in line)

    activity = await parse_activity(_translated(lines[: index + 1]), run_id="r1")

    (turn,) = activity.turns
    (call,) = turn.tools
    assert (call.tool, call.ok) == ("AskUserQuestion", None)
    assert call.question == "Include archived rows?"
    assert call.answer is None


async def _rollout(lines: list[str]) -> AsyncIterator[str]:
    transcript = RolloutTranscript("t1", turn=True)
    for line in lines:
        for event in transcript.feed(line):
            yield event
    for event in transcript.end_segment():
        yield event


async def test_a_codex_rollout_reads_as_turns_with_its_command_and_a_failed_turns_outcome() -> None:
    lines = (CODEX / "session.rollout.jsonl").read_text("utf-8").splitlines()
    steps = json.loads((CODEX / "index.json").read_text("utf-8"))

    activity = await parse_activity(_rollout(lines[: steps["command"]["rollout"][1]]), run_id="r1")

    assert [turn.text for turn in activity.turns] == [["PONG from mock."]] * 3
    (call,) = activity.turns[2].tools
    assert (call.tool, call.target, call.ok, call.result) == (
        "command",
        "/bin/zsh -lc 'echo hi-5f9ec4'",
        True,
        "hi-5f9ec4",
    )
    assert activity.milestones == ["session started"]
    assert (activity.outcome, activity.cost_usd) == ("success", None)

    failed = await parse_activity(_rollout(lines[: steps["fail"]["rollout"][1]]), run_id="r1")
    assert failed.outcome == "error"
