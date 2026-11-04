"""Chat-backend registry — select a backend by string, never by importing a vendor module.

Mirrors `runtime.adapters`: factories import lazily, so nothing vendor-specific loads until the
kind that needs it is actually asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.assistant.contract import ChatBackend
from runtime.contract import AdapterUnavailable

__all__ = ["create_backend"]


def _claude_sdk(**options: Any) -> ChatBackend:
    from runtime.assistant.adapters.claude_sdk import ClaudeSdkBackend

    return ClaudeSdkBackend(**options)


def _codex_cli(**options: Any) -> ChatBackend:
    from runtime.assistant.adapters.codex_cli.backend import CodexCliBackend

    return CodexCliBackend(**options)


_REGISTRY: dict[str, Callable[..., ChatBackend]] = {
    "claude_sdk": _claude_sdk,
    "codex_cli": _codex_cli,
}


def create_backend(kind: str, **options: Any) -> ChatBackend:
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise AdapterUnavailable(
            f"unknown assistant backend {kind!r}; available: {', '.join(sorted(_REGISTRY))}"
        )
    return factory(**options)
