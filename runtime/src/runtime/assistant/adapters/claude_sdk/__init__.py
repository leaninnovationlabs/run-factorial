"""The assistant on the Claude Agent SDK: backend kind `claude_sdk` (tech-stack.md decision 63).

`launcher.py` is also the `factorial-assistant-cli` console script, so this package's own
import stays free of the SDK: the backend loads only when the registry asks for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runtime.assistant.adapters.claude_sdk.backend import ClaudeSdkBackend

__all__ = ["ClaudeSdkBackend"]


def __getattr__(name: str) -> Any:
    if name == "ClaudeSdkBackend":
        from runtime.assistant.adapters.claude_sdk.backend import ClaudeSdkBackend

        return ClaudeSdkBackend
    raise AttributeError(name)
