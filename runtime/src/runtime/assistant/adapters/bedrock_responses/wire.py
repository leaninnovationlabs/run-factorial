"""Bedrock Runtime's `/openai/v1/responses`, as this adapter speaks it.

Every request field and stream event named here was recorded on 2026-11-10 with a Grok 4.6
model (`runtime/tests/fixtures/bedrock-responses-2026-11-10`). The failure events were not
recorded: they are as OpenAI's reference gives them, and say so.
A change in what Bedrock sends re-pins this module, and the loop in `backend.py` stays.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from runtime.assistant.contract import (
    ChatEvent,
    TextDelta,
    TextDone,
    ToolDefinition,
    TurnFailed,
    TurnFailure,
)

__all__ = [
    "PINNED_API",
    "Reply",
    "call_output",
    "endpoint",
    "function_tool",
    "message",
    "read_line",
    "refusal",
    "request_body",
]

PINNED_API: Final = "openai/v1 responses, recorded 2026-11-10"

#: Two letters, one or more words and a number, as `us-east-1` and `ap-southeast-2` are.
_REGION: Final = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+")

#: Beside the tools, what Codex 0.164.0 sends and Bedrock answers.
_TOOL_FIELDS: Final[Mapping[str, Any]] = {
    "tool_choice": "auto",
    "parallel_tool_calls": True,
    "reasoning": {"summary": "auto"},
    "include": ["reasoning.encrypted_content"],
}

EVENT_TEXT_DELTA: Final = "response.output_text.delta"
EVENT_ITEM_DONE: Final = "response.output_item.done"
EVENT_COMPLETED: Final = "response.completed"
#: Not recorded.
EVENT_FAILED: Final = "response.failed"
EVENT_INCOMPLETE: Final = "response.incomplete"
EVENT_ERROR: Final = "error"
ITEM_MESSAGE: Final = "message"
ITEM_FUNCTION_CALL: Final = "function_call"
PART_TEXT: Final = "output_text"
#: Not recorded: the code OpenAI gives a throttled response, and Bedrock gives a 429.
CODE_RATE_LIMIT: Final = "rate_limit_exceeded"
#: Not recorded.
INCOMPLETE_FILTERED: Final = "content_filter"


def endpoint(region: str | None) -> str:
    """Where a region's requests go. The region becomes part of a host name, and the bearer goes
    to that host, so anything but a region's own shape is refused."""
    chosen = (region or "").strip()
    if not _REGION.fullmatch(chosen):
        raise ValueError(f"{region!r} is not an AWS region ID, such as us-east-1")
    return f"https://bedrock-runtime.{chosen}.amazonaws.com/openai/v1/responses"


def message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def function_tool(definition: ToolDefinition) -> dict[str, Any]:
    """A plain function tool, under the tool's own name. Bedrock answers a `namespace` tool with
    a call that names no namespace, so nothing is gained by one."""
    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "strict": False,
        "parameters": dict(definition.input_schema),
    }


def call_output(call_id: str, text: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def request_body(
    model: str, items: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "model": model,
        "stream": True,
        "store": False,
        "input": list(items),
        "tools": list(tools),
        **_TOOL_FIELDS,
    }


@dataclass
class Reply:
    """One model call's answer, as its stream gave it."""

    #: Every output item, as sent: the next request hands them back unchanged.
    items: list[dict[str, Any]] = field(default_factory=list)
    #: Input, cached input and output tokens. Input is gross of cache.
    usage: tuple[int, int, int] | None = None
    failure: TurnFailed | None = None
    completed: bool = False

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item.get("type") == ITEM_FUNCTION_CALL]


def read_line(line: str, reply: Reply) -> list[ChatEvent]:
    """One line of the stream into `reply`, and the text events it carries. The stream is `data:`
    lines alone, each a JSON event, and ends with `data: [DONE]`."""
    if not line.startswith("data:"):
        return []
    try:
        event = json.loads(line[5:])
    except ValueError:
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == EVENT_TEXT_DELTA:
        block, delta = event.get("item_id"), event.get("delta")
        if isinstance(block, str) and isinstance(delta, str) and delta:
            return [TextDelta(block=block, text=delta)]
        return []
    if kind == EVENT_ITEM_DONE:
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        reply.items.append(item)
        return _said(item, len(reply.items))
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    if kind == EVENT_COMPLETED:
        reply.completed = True
        reply.usage = _usage(response.get("usage"))
        output = response.get("output")
        if reply.items or not isinstance(output, list):
            return []
        # No item event came: the completed response still lists what was said.
        reply.items = [item for item in output if isinstance(item, dict)]
        return [done for n, item in enumerate(reply.items, 1) for done in _said(item, n)]
    if kind == EVENT_FAILED:
        reply.failure = _failed(response.get("error"))
    elif kind == EVENT_INCOMPLETE:
        details = response.get("incomplete_details")
        why = details.get("reason") if isinstance(details, dict) else None
        reply.failure = TurnFailed(
            reason=(
                TurnFailure.REFUSAL if why == INCOMPLETE_FILTERED else TurnFailure.INFRASTRUCTURE
            ),
            message=f"the reply was cut off: {why or 'no reason given'}",
        )
    elif kind == EVENT_ERROR:
        reply.failure = _failed(event)
    return []


def _said(item: Mapping[str, Any], position: int) -> list[ChatEvent]:
    if item.get("type") != ITEM_MESSAGE:
        return []
    content = item.get("content")
    text = "".join(
        part["text"]
        for part in (content if isinstance(content, list) else [])
        if isinstance(part, dict)
        and part.get("type") == PART_TEXT
        and isinstance(part.get("text"), str)
    )
    if not text:
        return []
    block = item.get("id")
    return [TextDone(block=block if isinstance(block, str) else f"item-{position}", text=text)]


def _usage(usage: object) -> tuple[int, int, int] | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return _int(usage.get("input_tokens")), _int(cached), _int(usage.get("output_tokens"))


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _failed(error: object) -> TurnFailed:
    code = error.get("code") if isinstance(error, dict) else None
    said = error.get("message") if isinstance(error, dict) else None
    words = [part for part in (code, said) if isinstance(part, str) and part]
    return TurnFailed(
        reason=(
            TurnFailure.RATE_LIMIT if code == CODE_RATE_LIMIT else TurnFailure.INFRASTRUCTURE
        ),
        message=": ".join(words) or "the response failed and gave no reason",
    )


def refusal(status: int, body: str) -> TurnFailed:
    """A request Bedrock did not answer with a stream. The body is `{"error": {"message"}}`, as
    recorded for a 403 and a 429."""
    said = body.strip()[:400]
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        said = error["message"]
    if status in (401, 403):
        reason = TurnFailure.AUTH
    elif status == 429:
        reason = TurnFailure.RATE_LIMIT
    elif status in (400, 404, 422):
        reason = TurnFailure.CONFIG
    else:
        reason = TurnFailure.INFRASTRUCTURE
    return TurnFailed(reason=reason, message=f"Bedrock answered {status}: {said}")
