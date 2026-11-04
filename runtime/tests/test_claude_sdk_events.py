"""The SDK messages are built with the pinned SDK's own constructors, in the shapes `claude`
2.1.284 was observed to send: one `AssistantMessage` per content block, delivered while that
block is still open in the stream."""

from decimal import Decimal
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from runtime.assistant.adapters.claude_sdk.events import EventMapper, ToolsLeaked
from runtime.assistant.contract import (
    TextDelta,
    TextDone,
    ToolCall,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)

CHAT_TOOL = "mcp__factorial__factorial_ticket_get"


def _init(tools: list[str]) -> SystemMessage:
    return SystemMessage(subtype="init", data={"type": "system", "subtype": "init", "tools": tools})


def _stream(event: dict[str, Any]) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=event)


def _start(index: int, kind: str) -> StreamEvent:
    return _stream({"type": "content_block_start", "index": index, "content_block": {"type": kind}})


def _delta(index: int, text: str) -> StreamEvent:
    return _stream(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def _message_start(message_id: str) -> StreamEvent:
    return _stream({"type": "message_start", "message": {"id": message_id}})


def _assistant(*blocks: Any, error: Any = None, message_id: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks), model="claude-opus-5-5", error=error, message_id=message_id
    )


def _result(subtype: str = "success", **overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": subtype,
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": subtype != "success",
        "num_turns": 1,
        "session_id": "5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a",
        "stop_reason": "end_turn",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 300,
            "output_tokens": 40,
        },
    }
    return ResultMessage(**(fields | overrides))


def _mapper() -> EventMapper:
    mapper = EventMapper(["factorial"])
    assert mapper.map(_init([CHAT_TOOL])) == []
    return mapper


def _run(mapper: EventMapper, *messages: Any) -> list[Any]:
    return [event for message in messages for event in mapper.map(message)]


def test_init_with_only_chat_tools_passes():
    assert EventMapper(["factorial"]).map(_init([CHAT_TOOL, "mcp__factorial__x"])) == []


def test_init_listing_a_builtin_tool_raises_tools_leaked():
    with pytest.raises(ToolsLeaked) as raised:
        EventMapper(["factorial"]).map(_init(["Bash", "mcp__factorial__x"]))
    assert raised.value.names == ("Bash",)


def test_init_that_names_no_tools_fails_closed():
    with pytest.raises(ToolsLeaked):
        EventMapper(["factorial"]).map(SystemMessage(subtype="init", data={"subtype": "init"}))


def test_model_output_before_the_init_check_fails_closed():
    with pytest.raises(ToolsLeaked):
        EventMapper(["factorial"]).map(_assistant(TextBlock(text="hi")))


def test_text_deltas_and_the_final_block_share_a_block_key():
    events = _run(
        _mapper(),
        _message_start("m1"),
        _start(0, "text"),
        _delta(0, "hel"),
        _delta(0, "lo"),
        _assistant(TextBlock(text="hello"), message_id="m1"),
    )
    assert events == [
        TextDelta(block="1:0", text="hel"),
        TextDelta(block="1:0", text="lo"),
        TextDone(block="1:0", text="hello"),
    ]


def test_a_text_block_after_thinking_keeps_its_stream_index():
    events = _run(
        _mapper(),
        _message_start("m1"),
        _start(0, "thinking"),
        _assistant(ThinkingBlock(thinking="hmm", signature="sig"), message_id="m1"),
        _start(1, "text"),
        _delta(1, "answer"),
        _assistant(TextBlock(text="answer"), message_id="m1"),
        _message_start("m2"),
        _start(0, "text"),
        _assistant(TextBlock(text="next"), message_id="m2"),
    )
    assert events == [
        TextDelta(block="1:1", text="answer"),
        TextDone(block="1:1", text="answer"),
        TextDone(block="2:0", text="next"),
    ]


def test_a_message_the_stream_never_announced_gets_a_key_of_its_own():
    events = _run(
        _mapper(),
        _message_start("m1"),
        _start(0, "text"),
        _assistant(TextBlock(text="streamed"), message_id="m1"),
        _assistant(TextBlock(text="synthetic"), message_id="x"),
    )
    assert events == [
        TextDone(block="1:0", text="streamed"),
        TextDone(block="2:0", text="synthetic"),
    ]


def test_tool_use_drops_the_server_prefix():
    events = _run(_mapper(), _assistant(ToolUseBlock(id="t1", name=CHAT_TOOL, input={"ref": "12"})))
    assert events == [ToolCall(call_id="t1", name="factorial_ticket_get", input={"ref": "12"})]


def test_tool_result_is_summarised_and_capped_at_2000_chars():
    long = UserMessage(
        content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "x" * 5000}])]
    )
    as_json = UserMessage(
        content=[ToolResultBlock(tool_use_id="t2", content='{"ok": 1}', is_error=None)]
    )
    failed = UserMessage(content=[ToolResultBlock(tool_use_id="t3", content="no", is_error=True)])
    events = _run(_mapper(), long, as_json, failed)
    assert events == [
        ToolResult(call_id="t1", ok=True, summary="x" * 2000),
        ToolResult(call_id="t2", ok=True, summary='{"ok": 1}', structured={"ok": 1}),
        ToolResult(call_id="t3", ok=False, summary="no"),
    ]


def test_result_success_gives_usage_then_end():
    assert _run(_mapper(), _result()) == [
        Usage(
            input_tokens=420,
            cached_input_tokens=300,
            output_tokens=40,
            cost_usd=Decimal("0.0123"),
            cost_reported=False,
            session_id="5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a",
        ),
        TurnDone(stop=TurnStop.END),
    ]


def test_result_error_max_turns_is_steps_and_budget_is_budget():
    assert _run(_mapper(), _result("error_max_turns"))[-1] == TurnDone(stop=TurnStop.STEPS)
    assert _run(_mapper(), _result("error_max_budget_usd"))[-1] == TurnDone(stop=TurnStop.BUDGET)


def test_an_interrupted_turn_is_cancelled():
    result = _result("error_during_execution", terminal_reason="aborted_streaming")
    assert _run(_mapper(), result)[-1] == TurnDone(stop=TurnStop.CANCELLED)


def test_any_other_error_result_is_infrastructure():
    events = _run(_mapper(), _result("error_during_execution", errors=["boom"]))
    assert events[-1] == TurnFailed(reason=TurnFailure.INFRASTRUCTURE, message="boom")


def test_assistant_error_authentication_failed_is_auth():
    synthetic = _assistant(TextBlock(text="Invalid API key"), error="authentication_failed")
    events = _run(_mapper(), synthetic, _result(is_error=True, stop_reason="stop_sequence"))
    assert [e.kind for e in events] == ["usage", "turn_failed"]
    assert events[-1] == TurnFailed(reason=TurnFailure.AUTH, message="Invalid API key")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("billing_error", TurnFailure.AUTH),
        ("rate_limit", TurnFailure.RATE_LIMIT),
        ("server_error", TurnFailure.INFRASTRUCTURE),
    ],
)
def test_assistant_errors_map_to_their_failure(error, reason):
    synthetic = _assistant(TextBlock(text="API Error"), error=error)
    events = _run(_mapper(), synthetic, _result(is_error=True))
    assert events[-1] == TurnFailed(reason=reason, message="API Error")


def test_a_refusal_is_refusal_whatever_error_the_cli_attached():
    synthetic = _assistant(TextBlock(text="safeguards flagged this"), error="invalid_request")
    result = _result(is_error=True, stop_reason="refusal", terminal_reason="api_error")
    events = _run(_mapper(), synthetic, result)
    assert events[-1] == TurnFailed(reason=TurnFailure.REFUSAL, message="safeguards flagged this")


def test_a_rejected_credential_fails_at_the_first_retry():
    retry = SystemMessage(
        subtype="api_retry",
        data={"subtype": "api_retry", "attempt": 1, "error": "authentication_failed"},
    )
    events = _run(_mapper(), retry)
    assert [(e.kind, e.reason) for e in events] == [("turn_failed", TurnFailure.AUTH)]


def test_a_rate_limited_retry_is_left_to_the_cli():
    retry = SystemMessage(subtype="api_retry", data={"subtype": "api_retry", "error": "rate_limit"})
    assert _run(_mapper(), retry) == []


def test_thinking_is_not_shown():
    assert _run(_mapper(), _assistant(ThinkingBlock(thinking="secret", signature="s"))) == []
