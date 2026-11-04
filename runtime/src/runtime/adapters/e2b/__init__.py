"""Agent CLIs in an E2B sandbox per run (tech-stack.md decision 25)."""

from __future__ import annotations

from typing import Any, ClassVar

from runtime.adapters.e2b.adapter import E2bAdapter
from runtime.adapters.e2b.claude import ClaudeHarness
from runtime.adapters.e2b.codex import CodexHarness
from runtime.adapters.e2b.harness import Harness

__all__ = ["ClaudeE2bAdapter", "CodexE2bAdapter", "E2bAdapter"]


class ClaudeE2bAdapter(E2bAdapter):
    harness: Harness = ClaudeHarness()
    kind: ClassVar[str] = "claude_e2b"
    pinned_version: ClassVar[str] = f"claude {ClaudeHarness.pinned_version}"


class CodexE2bAdapter(E2bAdapter):
    harness: Harness = CodexHarness()
    kind: ClassVar[str] = "codex_e2b"
    pinned_version: ClassVar[str] = f"codex {CodexHarness.pinned_version}"

    def __init__(self, *, ask_tool: bool = False, **options: Any) -> None:
        super().__init__(**options)
        self.harness = CodexHarness(ask_tool=ask_tool)
