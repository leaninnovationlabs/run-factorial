"""Tests for the codex_cli event seam — `--json` JSONL lines into typed events.

The stream is upstream-experimental, so the vocabulary lives behind one module
(`events.py`): a schema drift re-pins that module and nothing else. Every shape asserted
here was observed from a real `codex exec --json` run against codex-cli 0.146.0.
"""

from __future__ import annotations

import json

from runtime.adapters.codex_cli.events import (
    AgentMessage,
    CommandExecution,
    ErrorEvent,
    ThreadStarted,
    TurnCompleted,
    TurnStarted,
    UnknownItem,
    parse_event,
)


def test_thread_started_carries_the_resume_handle() -> None:
    """`thread_id` is what `codex exec resume` addresses — the one field that must outlive
    the stream."""
    line = json.dumps({"type": "thread.started", "thread_id": "01a04f4c-bd65-7091"})
    assert parse_event(line) == ThreadStarted(thread_id="01a04f4c-bd65-7091")


def test_a_thread_started_without_an_id_is_not_an_event() -> None:
    """A resume handle that is absent must read as absent, never as an empty-string id an
    interrupt would then try to resume with."""
    assert parse_event(json.dumps({"type": "thread.started"})) is None
    assert parse_event(json.dumps({"type": "thread.started", "thread_id": ""})) is None


def test_turn_started_is_the_turn_count_signal() -> None:
    assert parse_event(json.dumps({"type": "turn.started"})) == TurnStarted()


def test_an_agent_message_item_carries_the_summary_text() -> None:
    line = json.dumps(
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}}
    )
    assert parse_event(line) == AgentMessage(text="ok")


def test_a_command_execution_is_typed_on_start_and_on_completion() -> None:
    """Both halves observed on 0.153.2 (runs 157-plan-agent-ef368a05 / -5701ab70): the same
    item id, `in_progress` with no exit code first, then `completed` with the output. The
    supervisor ignores both; the activity view is what reads them."""
    started = json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": "/bin/bash -lc 'ls'",
                "aggregated_output": "",
                "exit_code": None,
                "status": "in_progress",
            },
        }
    )
    completed = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": "/bin/bash -lc 'ls'",
                "aggregated_output": "total 180\n",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert parse_event(started) == CommandExecution(
        item_id="item_2",
        command="/bin/bash -lc 'ls'",
        aggregated_output="",
        exit_code=None,
        status="in_progress",
        completed=False,
    )
    assert parse_event(completed) == CommandExecution(
        item_id="item_2",
        command="/bin/bash -lc 'ls'",
        aggregated_output="total 180\n",
        exit_code=0,
        status="completed",
        completed=True,
    )


def test_an_unobserved_completed_item_is_typed_as_unknown_not_dropped() -> None:
    """`reasoning`, `file_change`, `mcp_tool_call`, `web_search`, `todo_list` exist in the CLI
    and were never observed, so nothing pins their fields — only the type name survives, which
    is enough for a reader to say what it skipped. A started one is still nothing: it carries
    no result yet."""
    completed = json.dumps(
        {"type": "item.completed", "item": {"id": "item_1", "type": "file_change", "changes": []}}
    )
    assert parse_event(completed) == UnknownItem(item_type="file_change")
    started = json.dumps(
        {"type": "item.started", "item": {"id": "item_1", "type": "file_change", "changes": []}}
    )
    assert parse_event(started) is None
    assert parse_event(json.dumps({"type": "item.completed", "item": {"id": "x"}})) is None


def test_turn_completed_reports_gross_input_tokens_unsummed() -> None:
    """Codex's `input_tokens` already includes `cached_input_tokens` (observed: 17004 input,
    11008 cached on one real turn) — the opposite of Claude's net-of-cache reporting. Summing
    the fields, the claude_cli rule, would double-count every cached byte."""
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 17004,
                "cached_input_tokens": 11008,
                "cache_write_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_output_tokens": 0,
            },
        }
    )
    assert parse_event(line) == TurnCompleted(
        input_tokens=17004, cached_input_tokens=11008, cache_write_input_tokens=0, output_tokens=5
    )


def test_turn_completed_carries_the_cache_split_the_estimate_needs() -> None:
    """The worker prices a codex run itself (tech-stack decision 34): cached input is billed at
    a tenth of the input rate and a cache write at 1.25x on 5.6+, so both counts must survive
    the stream. `reasoning_output_tokens` is NOT carried — `output_tokens` already includes it,
    and a second copy would be double-billed by the first reader that summed the two."""
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 15373,
                "cached_input_tokens": 12160,
                "cache_write_input_tokens": 512,
                "output_tokens": 640,
                "reasoning_output_tokens": 600,
            },
        }
    )
    event = parse_event(line)
    assert event == TurnCompleted(
        input_tokens=15373,
        cached_input_tokens=12160,
        cache_write_input_tokens=512,
        output_tokens=640,
    )
    assert not hasattr(event, "reasoning_output_tokens")


def test_turn_completed_with_no_usage_reports_nothing() -> None:
    assert parse_event(json.dumps({"type": "turn.completed"})) == TurnCompleted(
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_input_tokens=None,
        output_tokens=None,
    )


def test_error_events_carry_their_message() -> None:
    """All three error shapes observed from a real failed run (2026-08-29 live smoke): codex
    exits nonzero with an EMPTY stderr — the diagnosis rides stdout as events, so dropping
    them leaves a failure reason that says nothing."""
    top = json.dumps({"type": "error", "message": "the model is not supported"})
    failed = json.dumps(
        {"type": "turn.failed", "error": {"message": "the model is not supported"}}
    )
    item = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "i", "type": "error", "message": "metadata not found"},
        }
    )
    assert parse_event(top) == ErrorEvent(message="the model is not supported", terminal=False)
    assert parse_event(failed) == ErrorEvent(message="the model is not supported", terminal=True)
    assert parse_event(item) == ErrorEvent(message="metadata not found", terminal=False)


def test_an_error_event_without_a_message_is_not_an_event() -> None:
    assert parse_event(json.dumps({"type": "error"})) is None
    assert parse_event(json.dumps({"type": "turn.failed"})) is None


def test_noise_lines_are_ignored() -> None:
    """The CLI is free to write a human line to stdout; a run must not fail because of one."""
    assert parse_event("") is None
    assert parse_event("not json") is None
    assert parse_event(json.dumps(["a", "list"])) is None
    assert parse_event(json.dumps({"type": "somefuture.event"})) is None
