"""The transcript parser on a codex stream: the same turns, tool calls and milestones the UI
already renders, derived from codex's `--json` lines instead of Claude's stream-json.

The fixtures under `fixtures/codex-activity/` are two real 0.153.2 runs (plan agent, ticket
157) with their prose and command output trimmed — every line shape is what the CLI wrote.
`ef368a05` opened its PR; `5701ab70` wrote the docs and could not commit. Both streams END the
same way, with `turn.completed`: the failure lives in the agent's own words and in the step's
verdict, not in the stream, and the parser must not pretend otherwise.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orchestrator.api.activity import parse_activity

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURES = Path(__file__).parent / "fixtures" / "codex-activity"
SUCCEEDED = "codex-157-plan-agent-ef368a05"
FAILED_AT_COMMIT = "codex-157-plan-agent-5701ab70"

METADATA_WARNING = "error: Model metadata for `gpt-5.3-codex` not found."


async def _fixture(name: str) -> AsyncIterator[str]:
    for line in (FIXTURES / f"{name}.jsonl").read_text("utf-8").splitlines():
        yield line


async def _stream(*lines: str) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _item(kind: str, event: str = "item.completed", **fields: object) -> str:
    return json.dumps({"type": event, "item": {"id": "item_9", "type": kind, **fields}})


async def test_a_succeeded_codex_run_reads_as_one_turn_of_text_and_commands() -> None:
    activity = await parse_activity(_fixture(SUCCEEDED), run_id=SUCCEEDED)

    (turn,) = activity.turns
    assert turn.index == 1
    assert len(turn.text) == 4
    assert "ll inspect the repo" in turn.text[0]
    assert turn.text[-1].startswith("Produced `specs/157-version-endpoint/spec.md`")
    assert turn.thinking == []
    assert [(c.tool, c.ok) for c in turn.tools] == [("command", True)] * 3
    assert turn.tools[0].target is not None
    assert turn.tools[0].target.startswith("/bin/bash -lc 'rg --files")
    assert turn.tools[-1].result is not None
    assert "pull/158" in turn.tools[-1].result
    assert activity.outcome == "success"
    assert activity.cost_usd is None
    assert activity.truncated is False


async def test_a_codex_error_item_is_a_milestone_and_the_session_start_is_too() -> None:
    activity = await parse_activity(_fixture(SUCCEEDED))

    assert activity.milestones[0] == "session started"
    assert activity.milestones[1].startswith(METADATA_WARNING)
    assert len(activity.milestones) == 2


async def test_an_error_item_does_not_decide_the_outcome() -> None:
    """Every 0.153.2 run opens with the metadata warning before `turn.started`, so a live
    run's stream cut there — or anywhere before the turn ends — has no outcome yet. A run the
    supervisor stops after that warning must not read `error` for ever either."""
    lines = (FIXTURES / f"{SUCCEEDED}.jsonl").read_text("utf-8").splitlines()

    after_warning = await parse_activity(_stream(*lines[:2]))
    assert after_warning.milestones[1].startswith(METADATA_WARNING)
    assert after_warning.outcome is None

    mid_command = await parse_activity(_stream(*lines[:5]))
    assert mid_command.outcome is None
    assert mid_command.turns[0].tools[-1].ok is None

    top_level = await parse_activity(
        _stream(
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "error", "message": "stream error; retrying"}),
        )
    )
    assert top_level.milestones == ["session started", "error: stream error; retrying"]
    assert top_level.outcome is None


async def test_the_run_that_failed_at_commit_says_so_in_its_own_words() -> None:
    """Its stream is shaped exactly like the succeeded one — same warning, same
    `turn.completed` — so the outcome is codex's (the turn completed) and the failure is the
    last thing the agent said, which the view must carry whole enough to read."""
    activity = await parse_activity(_fixture(FAILED_AT_COMMIT))

    (turn,) = activity.turns
    assert activity.milestones[1].startswith(METADATA_WARNING)
    assert "could **not** open the PR" in turn.text[-1]
    assert [(c.tool, c.ok) for c in turn.tools] == [("command", True)]
    assert activity.outcome == "success"


async def test_a_stream_that_dies_after_an_error_has_outcome_error() -> None:
    """The keyless-run shape (cli.py's EVENT_ERROR pin): the diagnosis rides stdout and the
    process exits with no turn ever completing."""
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.failed", "error": {"message": "the model is not supported"}}),
        )
    )

    assert activity.outcome == "error"
    assert activity.milestones == ["session started", "error: the model is not supported"]


async def test_a_failing_command_is_marked_not_ok_and_keeps_its_output() -> None:
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "turn.started"}),
            _item(
                "command_execution",
                command="/bin/bash -lc 'npm test'",
                aggregated_output="FAIL tests/x.test.ts\n" + "y" * 1000,
                exit_code=1,
                status="completed",
            ),
        )
    )

    (call,) = activity.turns[0].tools
    assert call.ok is False
    assert call.target == "/bin/bash -lc 'npm test'"
    assert call.result is not None
    assert call.result.startswith("FAIL tests/x.test.ts")
    assert len(call.result) <= 400


async def test_a_command_still_running_has_no_verdict_yet() -> None:
    """`item.started` is the live run's outstanding call; its `item.completed` fills the same
    slot rather than adding a second row."""
    started = _item(
        "command_execution",
        event="item.started",
        command="git push",
        aggregated_output="",
        exit_code=None,
        status="in_progress",
    )
    live = await parse_activity(_stream(json.dumps({"type": "turn.started"}), started))
    (call,) = live.turns[0].tools
    assert (call.tool, call.target, call.ok) == ("command", "git push", None)

    done = await parse_activity(
        _stream(
            json.dumps({"type": "turn.started"}),
            started,
            _item(
                "command_execution",
                command="git push",
                aggregated_output="ok",
                exit_code=0,
                status="completed",
            ),
        )
    )
    (call,) = done.turns[0].tools
    assert (call.ok, call.result) == (True, "ok")


async def test_an_unobserved_item_type_is_skipped_not_fatal() -> None:
    """`reasoning`, `file_change`, `mcp_tool_call`, `web_search`, `todo_list` exist in the CLI
    and were not observed; only observed shapes are pinned, the rest must not break the view."""
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "turn.started"}),
            _item("file_change", changes=[{"path": "a.ts", "kind": "add"}]),
            _item("reasoning", text="hmm"),
            _item("agent_message", text="done"),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        )
    )

    (turn,) = activity.turns
    assert turn.text == ["done"]
    assert turn.tools == []
    assert activity.outcome == "success"


async def test_a_turn_that_completes_without_a_message_is_not_a_success() -> None:
    """Mirrors the supervisor: exit 0 with no agent message is a run that never said what it
    did (`results.py::_classify`)."""
    activity = await parse_activity(
        _stream(json.dumps({"type": "turn.started"}), json.dumps({"type": "turn.completed"}))
    )

    assert activity.outcome == "finished"


async def test_the_turn_limit_applies_to_codex_turns() -> None:
    lines = []
    for i in range(5):
        lines.append(json.dumps({"type": "turn.started"}))
        lines.append(_item("agent_message", text=f"turn {i}"))
        lines.append(json.dumps({"type": "turn.completed"}))
    activity = await parse_activity(_stream(*lines), limit=2)

    assert [t.index for t in activity.turns] == [1, 2]
    assert activity.truncated is True


async def test_a_path_inside_the_codex_worktree_renders_relative() -> None:
    """Cut in place, not split off: a command's words around the path are the point."""
    run_id = "157-plan-agent-ef368a05"
    inside = f"cat /Users/x/.factorial/codex/runs/{run_id}/worktree/specs/157/spec.md"
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "turn.started"}),
            _item(
                "command_execution",
                command=inside,
                aggregated_output="",
                exit_code=0,
                status="completed",
            ),
        ),
        run_id=run_id,
    )

    assert activity.turns[0].tools[0].target == "cat specs/157/spec.md"


async def test_a_claude_stream_still_parses_identically() -> None:
    """The dialect is detected per line; a Claude stream must not be touched by the codex
    grouper. Claude's top-level `error`-free vocabulary and its `message` object are the tell."""
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Reading the suite."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Bash",
                                "input": {"command": "npm test"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "ok",
                                "is_error": False,
                            }
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5}),
        )
    )

    (turn,) = activity.turns
    assert turn.text == ["Reading the suite."]
    assert [(c.tool, c.target, c.ok, c.result) for c in turn.tools] == [
        ("Bash", "npm test", True, "ok")
    ]
    assert activity.milestones == ["session started"]
    assert activity.outcome == "success"
    assert activity.cost_usd == pytest.approx(0.5)
