"""Chat-backend registry — select a backend by string, never by importing a vendor module.

Mirrors `runtime.adapters`: factories import lazily, so nothing vendor-specific loads until the
kind that needs it is actually asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from runtime.assistant.contract import ChatBackend
from runtime.contract import AdapterUnavailable

__all__ = ["TAKES_IMAGES", "create_backend"]

#: The kinds whose backend takes an image in a turn (`ChatBackend.supports_images`), so that the
#: API can refuse an image for one that does not without loading a vendor module. A test pins
#: this to the classes. `bedrock_responses` is out until an image is verified against Bedrock;
#: which sign-ins of the others are handed one is the orchestrator's to say.
TAKES_IMAGES: Final[frozenset[str]] = frozenset({"claude_sdk", "codex_cli"})


def _claude_sdk(**options: Any) -> ChatBackend:
    from runtime.assistant.adapters.claude_sdk import ClaudeSdkBackend

    return ClaudeSdkBackend(**options)


def _codex_cli(**options: Any) -> ChatBackend:
    from runtime.assistant.adapters.codex_cli.backend import CodexCliBackend

    return CodexCliBackend(**options)


def _bedrock_responses(**options: Any) -> ChatBackend:
    from runtime.assistant.adapters.bedrock_responses import BedrockResponsesBackend

    return BedrockResponsesBackend(**options)


_REGISTRY: dict[str, Callable[..., ChatBackend]] = {
    "claude_sdk": _claude_sdk,
    "codex_cli": _codex_cli,
    "bedrock_responses": _bedrock_responses,
}


def create_backend(kind: str, **options: Any) -> ChatBackend:
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise AdapterUnavailable(
            f"unknown assistant backend {kind!r}; available: {', '.join(sorted(_REGISTRY))}"
        )
    return factory(**options)
