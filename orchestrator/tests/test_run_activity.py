"""The run transcript parser: what it reads, what it groups, and what it cannot make safe.

Tool fields are built from a closed key set, so a credential in an unread field is never looked
at rather than filtered out. `text` and `thinking` are the documented exception — free-form model
output with no fields to restrict — and the tests below pin the boundary between the two so a
future change cannot quietly move it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from orchestrator.api.activity import parse_activity

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _stream(*lines: str) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _assistant(*blocks: dict[str, object]) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def _tool(name: str, payload: dict[str, object], use_id: str = "t1") -> dict[str, object]:
    return {"type": "tool_use", "id": use_id, "name": name, "input": payload}


def _result(use_id: str, content: str, *, error: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": use_id,
                        "content": content,
                        "is_error": error,
                    }
                ]
            },
        }
    )


async def test_a_turn_carries_its_text_thinking_and_tools() -> None:
    activity = await parse_activity(
        _stream(
            _assistant(
                {"type": "thinking", "thinking": "I should look at the tests first"},
                {"type": "text", "text": "Reading the suite."},
                _tool("Bash", {"command": "npm test"}),
            )
        )
    )

    (turn,) = activity.turns
    assert turn.index == 1
    assert turn.text == ["Reading the suite."]
    assert turn.thinking == ["I should look at the tests first"]
    assert [(c.tool, c.target) for c in turn.tools] == [("Bash", "npm test")]


async def test_a_result_is_matched_to_its_call_by_id_not_position() -> None:
    """The stream interleaves, and a turn with parallel calls would mis-pair by position. The id
    is the only thing that actually identifies which call a result belongs to."""
    activity = await parse_activity(
        _stream(
            _assistant(
                _tool("Bash", {"command": "first"}, use_id="a"),
                _tool("Bash", {"command": "second"}, use_id="b"),
            ),
            # Deliberately out of order: b's result arrives before a's.
            _result("b", "second done"),
            _result("a", "first failed", error=True),
        )
    )

    (turn,) = activity.turns
    first, second = turn.tools
    assert (first.target, first.ok, first.result) == ("first", False, "first failed")
    assert (second.target, second.ok, second.result) == ("second", True, "second done")


async def test_an_outstanding_call_has_no_verdict_yet() -> None:
    """A live run's last call has not returned. `None` says "waiting", which is not the same as
    success and must not render as one."""
    activity = await parse_activity(_stream(_assistant(_tool("Bash", {"command": "npm test"}))))

    (call,) = activity.turns[0].tools
    assert call.ok is None
    assert call.result is None


async def test_a_credential_in_an_unread_tool_field_is_never_looked_at() -> None:
    """The property the whole derivation exists for. `input.env` is not in the key set, so the
    token is not matched by a filter and then removed — it is never read."""
    token = "ghs_livetokenshapedvalue0000000000000000"  # gitleaks:allow
    activity = await parse_activity(
        _stream(_assistant(_tool("Bash", {"command": "gh pr create", "env": {"GH_TOKEN": token}})))
    )

    (call,) = activity.turns[0].tools
    assert call.target == "gh pr create"
    assert token not in json.dumps({"tool": call.tool, "target": call.target})


async def test_thinking_tokens_never_become_milestones() -> None:
    """206 of ~330 events in a real run were `thinking_tokens` deltas. A milestone list that
    included them would be noise with a timestamp."""
    activity = await parse_activity(
        _stream(
            json.dumps({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12}),
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "system", "subtype": "vcs_state_changed"}),
        )
    )

    assert activity.milestones == ["session started", "working tree changed"]


async def test_the_terminal_result_supplies_the_outcome_and_cost() -> None:
    activity = await parse_activity(
        _stream(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.3438}))
    )

    assert activity.outcome == "success"
    assert activity.cost_usd == pytest.approx(0.3438)


async def test_a_path_inside_the_runs_workspace_renders_relative() -> None:
    run_id = "112-plan-agent-5f260d8f"
    inside = f"/Users/x/.software-factory/local/runs/{run_id}/worktree/specs/112/spec.md"
    activity = await parse_activity(
        _stream(_assistant(_tool("Edit", {"file_path": inside}))), run_id=run_id
    )

    assert activity.turns[0].tools[0].target == "specs/112/spec.md"


async def test_a_path_outside_the_workspace_stays_absolute() -> None:
    """Keyed on the run id on purpose: an agent reading elsewhere on the machine is a fact worth
    seeing, and shortening it would hide where it went."""
    activity = await parse_activity(
        _stream(_assistant(_tool("Read", {"file_path": "/Users/x/other-repo/app.ts"}))),
        run_id="112-plan-agent-5f260d8f",
    )

    assert activity.turns[0].tools[0].target == "/Users/x/other-repo/app.ts"


async def test_model_prose_is_truncated_rather_than_forwarded_whole() -> None:
    """`text` and `thinking` cannot be field-restricted the way a tool input can, so the only
    control left is length. This pins that the control exists."""
    activity = await parse_activity(_stream(_assistant({"type": "text", "text": "x" * 5000})))

    (line,) = activity.turns[0].text
    assert len(line) <= 2000
    assert line.endswith("…")


async def test_a_truncated_final_line_does_not_lose_the_turns_before_it() -> None:
    activity = await parse_activity(
        _stream(_assistant(_tool("Read", {"file_path": "a.ts"})), '{"type": "assistant", "mess')
    )

    assert len(activity.turns) == 1


async def test_the_turn_limit_is_reported_as_truncation() -> None:
    activity = await parse_activity(
        _stream(*[_assistant({"type": "text", "text": f"turn {i}"}) for i in range(10)]), limit=3
    )

    assert [t.index for t in activity.turns] == [1, 2, 3]
    assert activity.truncated is True


async def test_an_empty_assistant_message_contributes_no_turn() -> None:
    """A message carrying only a stop reason has nothing to read."""
    activity = await parse_activity(_stream(_assistant()))

    assert activity.turns == []
