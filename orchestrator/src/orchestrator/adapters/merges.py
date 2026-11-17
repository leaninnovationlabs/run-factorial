"""A forge's refusal of a gate's merge, which both projectors raise and the gate reads."""

from __future__ import annotations

from typing import Final

__all__ = ["MergeRefused", "refusal_message"]

#: The forge's message reaches an issue comment and an event; bound it like any untrusted text.
_MAX_REFUSAL_CHARS: Final = 300


class MergeRefused(Exception):
    """The forge declined a gate's merge. A state of the pull request, not an outage: the gate
    keeps waiting and says why, rather than failing the tick (worker/gates.py)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def refusal_message(status: int | None, body: object) -> str:
    """The `message` of a refusal's parsed body, bounded, or the status when it has none."""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return f"HTTP {status}"
    return message.strip()[:_MAX_REFUSAL_CHARS]
