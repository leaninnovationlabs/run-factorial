"""Adapter registry — select a substrate by string, never by importing a vendor module.

The orchestrator reads `settings.runtime.kind` (tech-stack.md decision 9) and calls
`create_adapter(kind, ...)`. Factories import lazily, so nothing vendor-specific loads until the
kind that needs it is actually asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.contract import AdapterUnavailable, RuntimeAdapter

__all__ = ["create_adapter"]


def _claude_cli(**options: Any) -> RuntimeAdapter:
    from runtime.adapters.claude_cli import ClaudeCliAdapter

    return ClaudeCliAdapter(**options)


def _codex_cli(**options: Any) -> RuntimeAdapter:
    from runtime.adapters.codex_cli import CodexCliAdapter

    return CodexCliAdapter(**options)


def _claude_e2b(**options: Any) -> RuntimeAdapter:
    from runtime.adapters.e2b import ClaudeE2bAdapter

    return ClaudeE2bAdapter(**options)


def _codex_e2b(**options: Any) -> RuntimeAdapter:
    from runtime.adapters.e2b import CodexE2bAdapter

    return CodexE2bAdapter(**options)


#: `claude_managed` stays absent until it exists, so an unimplemented kind fails loudly at startup
#: instead of at first spawn.
_REGISTRY: dict[str, Callable[..., RuntimeAdapter]] = {
    "claude_cli": _claude_cli,
    "codex_cli": _codex_cli,
    "claude_e2b": _claude_e2b,
    "codex_e2b": _codex_e2b,
}


def create_adapter(kind: str, **options: Any) -> RuntimeAdapter:
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise AdapterUnavailable(
            f"unknown agent runtime {kind!r}; available: {', '.join(sorted(_REGISTRY))}"
        )
    return factory(**options)
