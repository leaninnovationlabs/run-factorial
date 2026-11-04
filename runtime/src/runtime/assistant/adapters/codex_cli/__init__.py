"""In-app assistant on the pinned Codex CLI (tech-stack.md decision 63)."""

from __future__ import annotations

from typing import Any

from runtime.assistant.adapters.codex_cli.backend import CodexCliBackend

__all__ = ["CodexCliBackend"]


def __getattr__(name: str) -> Any:
    if name == "CodexCliBackend":
        return CodexCliBackend
    raise AttributeError(name)
