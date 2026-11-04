"""The pure `stream-json` reader both Claude substrates share."""

from __future__ import annotations

import json

from runtime.adapters.claude_cli.stream import StreamReader


def _assistant(request_id: str, parent: str | None = None) -> str:
    return json.dumps({"type": "assistant", "request_id": request_id, "parent_tool_use_id": parent})


def test_a_turn_is_a_distinct_main_loop_request() -> None:
    """One API request streams several assistant events, and a subagent's are not turns."""
    reader = StreamReader()
    counted = [reader.feed(line) for line in (_assistant("a"), _assistant("a"), _assistant("b"))]
    reader.feed(_assistant("c", parent="toolu_1"))

    assert counted == [True, False, True]
    assert reader.turns == 2


def test_prior_turns_seed_the_count_and_the_cap() -> None:
    reader = StreamReader(prior_turns=3)
    reader.feed(_assistant("a"))

    assert reader.turns == 4
    assert reader.over_cap(3)
    assert not reader.over_cap(4)


def test_the_first_session_id_wins_and_noise_is_ignored() -> None:
    reader = StreamReader()
    lines = ("not json", "", json.dumps({"session_id": "s1"}), json.dumps({"session_id": "s2"}))
    for line in lines:
        reader.feed(line)

    assert reader.session_id == "s1"


def test_an_error_result_names_its_subtype_and_text() -> None:
    reader = StreamReader()
    reader.feed(
        json.dumps({"type": "result", "is_error": True, "subtype": "error_max", "result": "boom"})
    )

    assert reader.error() == "the agent reported error_max: boom"
