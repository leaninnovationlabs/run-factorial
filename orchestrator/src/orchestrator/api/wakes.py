"""The wake a dashboard or MCP request sends, and the wait for the pass that answers it
(decisions 4, 49).

The API never reconciles and never admits. It asks the worker for a pass and waits for that
pass, so the caller's next read shows what the pass did rather than the state from before it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Ticket, TicketStatus
from orchestrator.events import wake_ticket

__all__ = [
    "WAIT_SECONDS",
    "await_admission",
    "await_pass",
    "wake_and_wait",
    "wake_for_request",
]

#: How long a request holds its answer for the worker's pass. Under any proxy timeout; past it
#: the page's own polling picks the pass up.
WAIT_SECONDS: float = 20.0
_POLL_SECONDS: float = 0.5


async def wake_for_request(session: AsyncSession, ticket: Ticket) -> datetime | None:
    """Wake the worker for a request the caller has already committed (`events.wake_ticket`).

    Returns the wake's time, or None when nothing was woken. After the commit, because the wake's
    time is what a pass must claim after, and a pass that claimed before the request committed
    might not see it. Never for a failing ticket: a pass there counts toward quarantine (decision
    31), so the request waits for the interval, as `POST /tickets/{id}/refresh` refuses outright.
    """
    if ticket.consecutive_failures:
        return None
    requested_at = await wake_ticket(session, ticket.id)
    await session.commit()
    return requested_at


async def wake_and_wait(session: AsyncSession, ticket: Ticket) -> bool:
    """`wake_for_request`, then `await_pass`: whether the pass the request woke has landed."""
    requested_at = await wake_for_request(session, ticket)
    return requested_at is not None and await await_pass(session, ticket.id, requested_at)


async def _poll_until[T](
    session: AsyncSession, probe: Callable[[], Awaitable[T | None]], wait_seconds: float
) -> T | None:
    """`probe` every `_POLL_SECONDS` until it answers (anything but None) or `wait_seconds` pass."""
    clock = asyncio.get_running_loop()
    give_up_at = clock.time() + wait_seconds
    while True:
        answer = await probe()
        # Ends the read's transaction, so a waiting request holds no connection between polls.
        await session.commit()
        if answer is not None:
            return answer
        if clock.time() >= give_up_at:
            return None
        await asyncio.sleep(_POLL_SECONDS)


async def await_pass(session: AsyncSession, ticket_id: int, requested_at: datetime) -> bool:
    """Poll until a pass that claimed the ticket after `requested_at` has finished with it."""
    first_claim: datetime | None = None

    async def landed() -> bool | None:
        nonlocal first_claim
        row = (
            await session.execute(
                select(
                    Ticket.last_leased_at, Ticket.lease_owner, Ticket.status, Ticket.quarantined_at
                ).where(Ticket.id == ticket_id)
            )
        ).one()
        claimed_at = row.last_leased_at
        answered = claimed_at is not None and claimed_at > requested_at
        # Done or quarantined is committed before the pass releases, and nothing leases the
        # ticket again: a pass that claimed after the request is the one that got it there.
        if row.status == TicketStatus.DONE or row.quarantined_at is not None:
            return answered
        if answered:
            if row.lease_owner is None:
                return True
            # A claim newer than the first one seen after the request means that one was
            # released: the fleet tick claims every ticket up front, so it can take the ticket
            # back before any poll sees it free.
            if first_claim is None:
                first_claim = claimed_at
            elif claimed_at is not None and claimed_at > first_claim:
                return True
        return None

    return await _poll_until(session, landed, WAIT_SECONDS) is True


async def await_admission(session: AsyncSession, source_id: int, key: str) -> Ticket | None:
    """Poll until the worker admits issue `key` from ticket source `source_id`, or give up after
    `WAIT_SECONDS`. `(ticket_source_id, external_key)` is unique, so the row is the answer."""

    async def admitted() -> Ticket | None:
        return await session.scalar(
            select(Ticket).where(Ticket.ticket_source_id == source_id, Ticket.external_key == key)
        )

    return await _poll_until(session, admitted, WAIT_SECONDS)
