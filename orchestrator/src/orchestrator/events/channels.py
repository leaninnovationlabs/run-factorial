"""Postgres LISTEN/NOTIFY channel names — Postgres is the queue (tech-stack decision 4).

The name lives here once because the API's `NOTIFY` and the worker's `LISTEN` must match, and a
typo degrades silently to "wakes stopped shortening latency" — polling is authoritative, so
nothing fails.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CHAT_ITEMS_CHANNEL",
    "CHAT_QUEUE_CHANNEL",
    "WAKE_CHANNEL",
    "WAKE_LISTENER",
    "wake_admission",
    "wake_fleet",
    "wake_inspect",
    "wake_ticket",
]

#: Payload is a small JSON object, a hint about where to look and never state — Postgres caps it
#: at 8000 bytes. `{"ticket_id", "requested_at"}` asks for that one ticket (`wake_ticket`);
#: `{"admit": project_id, "issue"}` asks whether that issue enters now (`wake_admission`);
#: `{"inspect": step_run_id}` asks for the inspect pass (`wake_inspect`); anything else wakes the
#: whole fleet (`wake_fleet`). Every sender lives here, so the shape the worker parses has one
#: author.
WAKE_CHANNEL: Final[str] = "factorial_wake"

#: The worker's LISTEN connection's `application_name`, so `pg_stat_activity` can say whether
#: anything is listening.
WAKE_LISTENER: Final[str] = "factorial-wake-listener"

#: Wakes the assistant's process to claim a queued turn or to see a cancel. The payload carries
#: nothing: the process re-reads `chat_turn`, and its own tick is the guarantee.
CHAT_QUEUE_CHANNEL: Final[str] = "factorial_chat_queue"

#: Wakes the API's chat streams. The payload is a conversation id; each stream re-reads its
#: conversation's items past the last `rev` it sent (`assistant.items.notify_items`).
CHAT_ITEMS_CHANNEL: Final[str] = "factorial_chat_items"


async def wake_ticket(session: AsyncSession, ticket_id: int) -> datetime:
    """Ask the worker to reconcile one ticket now. Returns the request's database time.

    The NOTIFY rides the caller's transaction: Postgres sends it at COMMIT and drops it on
    rollback, so a request the caller goes on to refuse wakes nothing.
    """
    requested_at = (await session.execute(select(func.statement_timestamp()))).scalar_one()
    await _notify(session, {"ticket_id": ticket_id, "requested_at": requested_at.isoformat()})
    return requested_at


async def wake_fleet(
    session: AsyncSession, *, project_id: int | None = None, repo: str | None = None
) -> datetime:
    """Ask the worker for a fleet pass now — the webhook's wake, and the dashboard's project
    refresh. On the caller's transaction, like `wake_ticket`. The project and repo are a hint the
    worker does not act on: a fleet pass admits and reconciles every project either way."""
    requested_at = (await session.execute(select(func.statement_timestamp()))).scalar_one()
    hint = {"project_id": project_id, "repo": repo}
    await _notify(session, {key: value for key, value in hint.items() if value is not None})
    return requested_at


async def wake_admission(session: AsyncSession, *, project_id: int, issue: str) -> None:
    """Ask the worker to judge one issue that is not a ticket yet, beside the fleet pass and
    outside its gap. On the caller's transaction, like `wake_ticket`."""
    await _notify(session, {"admit": project_id, "issue": issue})


async def wake_inspect(session: AsyncSession, step_run_id: int) -> None:
    """Ask the worker for an inspect pass now (`worker.inspect`), which no ticket's pass runs: it
    reaches done tickets too. On the caller's transaction, like `wake_ticket`."""
    await _notify(session, {"inspect": step_run_id})


async def _notify(session: AsyncSession, payload: dict[str, object]) -> None:
    # pg_notify() rather than NOTIFY: NOTIFY takes an identifier, not a bind parameter.
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": WAKE_CHANNEL, "payload": json.dumps(payload)},
    )
