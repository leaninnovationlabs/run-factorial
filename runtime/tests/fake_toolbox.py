"""A stand-in toolbox: one tool, `factorial_echo`, whose result is its input as JSON text, so a
real `claude` can call it in-process with no orchestrator behind it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from runtime.assistant.contract import ToolDefinition, ToolOutcome

ECHO = ToolDefinition(
    name="factorial_echo",
    title="Echo",
    description="Return the text it is given.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    read_only=True,
    destructive=False,
)


class FakeToolbox:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ECHO,)

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        return ToolOutcome(ok=self.ok, text=json.dumps(dict(arguments)))
