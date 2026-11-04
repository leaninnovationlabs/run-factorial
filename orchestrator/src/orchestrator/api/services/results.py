"""What a request service reports to the route or tool that called it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["RequestResult"]


@dataclass(frozen=True)
class RequestResult:
    """`requested`: appended now. `pending`: an earlier request is still unanswered, and nothing
    was appended. `already`: the ticket is in the asked-for state. `requeued`: the one gesture the
    API performs itself. `landed`: the worker pass it woke finished inside the wait."""

    ticket_id: int
    action: str
    status: Literal["requested", "pending", "already", "requeued"]
    landed: bool
