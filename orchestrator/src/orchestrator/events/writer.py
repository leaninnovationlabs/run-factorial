"""The only way to write the event log.

The table is append-only (design.md §2.7): this module inserts, and nothing anywhere may UPDATE
or DELETE an `event` row. The dashboard, audit trail and cost report are all derived from the
log, so rewriting history rewrites all three at once. A correction is a new event.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event
from orchestrator.events.types import EventType

__all__ = ["UnsafeEventDetails", "append"]


class UnsafeEventDetails(ValueError):
    """A `details` key looks like it carries a credential."""


#: Word-parts that mark a key as credential-shaped. `details` lands verbatim in the audit trail
#: and on the dashboard, and secret values are never logged (design.md §7.1) — so the guard sits
#: at the single write path rather than at every call site.
_SECRETISH: Final[frozenset[str]] = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwd",
        "pwd",
        "key",
        "keys",
        "credential",
        "credentials",
        "authorization",
        "apikey",
    }
)

#: Names that split into a `_SECRETISH` part but carry no credential — `external_key` is the
#: issue number, `input_tokens` is a usage count, `mcp_token_id` is an `mcp_token` row id. Kept
#: explicit so the guard can stay blunt.
_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "external_key",
        "idempotency_key",
        "enc_key_id",
        "key_id",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "secret_names",
        "mcp_token_id",
    }
)

_WORDS = re.compile(r"[^a-z0-9]+")


def _is_secretish(key: str) -> bool:
    lowered = key.lower()
    if lowered in _ALLOWED:
        return False
    return any(part in _SECRETISH for part in _WORDS.split(lowered) if part)


def _reject_credentials(value: Any, path: str = "details") -> None:
    """Walk the payload; a credential-shaped key anywhere in it is fatal."""
    if isinstance(value, Mapping):
        items: list[tuple[Any, Any]] = list(value.items())
        for key, nested in items:
            name = str(key)
            if _is_secretish(name):
                raise UnsafeEventDetails(
                    f"{path}.{name}: event details must never carry credentials. "
                    "Log a reference or a name instead."
                )
            _reject_credentials(nested, f"{path}.{name}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        elements: list[Any] = list(value)
        for index, item in enumerate(elements):
            _reject_credentials(item, f"{path}[{index}]")


async def append(
    session: AsyncSession,
    *,
    # Shadows the builtin: the column is `event.type`, and a synonym would make every call site
    # translate between two names for one field.
    type: EventType,
    project_id: int,
    ticket_id: int | None = None,
    step_run_id: int | None = None,
    **details: Any,
) -> Event:
    """Append one event and flush, so the caller has the identity `id` before the commit.

    Committing is the caller's unit of work: an event and the state change it describes belong in
    one transaction.
    """
    _reject_credentials(details)

    event = Event(
        project_id=project_id,
        ticket_id=ticket_id,
        step_run_id=step_run_id,
        type=str(type),
        details=dict(details),
    )
    session.add(event)
    await session.flush()
    return event
