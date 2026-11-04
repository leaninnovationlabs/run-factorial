"""Ticket leases — exactly one reconciler per ticket (design.md §3.1, data-model.md `ticket`).

`ticket.lease_owner` / `lease_expires_at` exist so N loop processes can run (tech-stack decision
3). The whole value of the columns is in *how* they are claimed: a single
`UPDATE ... WHERE (free or expired) ... RETURNING` over a `FOR UPDATE SKIP LOCKED` subquery.
A select-then-update would hand the same ticket to two runners in the window between the two
statements, which is precisely the race the lease is for.

Every write here but `release_all` is also guarded by
`lease_owner = :owner AND lease_expires_at > now()`: a runner whose lease expired mid-tick has
already lost the ticket to whoever reclaimed it, and must not write. `now()` is the *database*
clock throughout, so runners with skewed clocks still agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement, TextClause
from sqlalchemy.sql.expression import text as sql_text

from orchestrator.db.models import Ticket, TicketStatus

__all__ = [
    "EXITED_PASS_INTERVAL",
    "acquire_lease",
    "acquire_leases",
    "awaiting_pass",
    "release",
    "release_all",
    "renew",
]


def _expires_in(ttl: timedelta) -> TextClause:
    """`now() + ttl`, computed by Postgres — see the module docstring on clock skew."""
    return sql_text("now() + make_interval(secs => :lease_ttl_seconds)").bindparams(
        lease_ttl_seconds=ttl.total_seconds()
    )


def _claim(owner: str, ttl: timedelta) -> dict[str, object]:
    # The claim statement's own time: `now()` is when the caller's transaction began.
    return {
        "lease_owner": owner,
        "lease_expires_at": _expires_in(ttl),
        "last_leased_at": func.statement_timestamp(),
    }


def _handed_out() -> tuple[ColumnElement[bool], ...]:
    """A ticket the loop gives passes to at all, lease aside."""
    return (Ticket.status != TicketStatus.DONE, Ticket.quarantined_at.is_(None))


def _leasable() -> tuple[ColumnElement[bool], ...]:
    return (
        or_(Ticket.lease_owner.is_(None), Ticket.lease_expires_at < func.now()),
        *_handed_out(),
    )


#: How often the fleet pass leases a ticket that exited longer ago than this. Every closed issue
#: comes to rest as EXITED, so passing all of them every tick grew each pass with history. A
#: settled one only waits for its label to come off or for the dashboard, and both of those wake
#: it by ticket (`acquire_lease`), which this does not throttle (decision 29): the label through
#: the GitHub or GitLab webhook's issue delivery. Without a webhook, it waits up to this long.
EXITED_PASS_INTERVAL: Final = timedelta(minutes=10)


def _due_in_fleet_pass() -> ColumnElement[bool]:
    quiet = sql_text("now() - make_interval(secs => :exited_pass_seconds)").bindparams(
        exited_pass_seconds=EXITED_PASS_INTERVAL.total_seconds()
    )
    return or_(
        Ticket.status != TicketStatus.EXITED,
        Ticket.closed_at.is_(None),
        Ticket.closed_at > quiet,
        Ticket.last_leased_at.is_(None),
        Ticket.last_leased_at < quiet,
    )


def _unanswered(requested_at: datetime) -> ColumnElement[bool]:
    """No pass has claimed the ticket since a refresh requested at `requested_at`."""
    return or_(Ticket.last_leased_at.is_(None), Ticket.last_leased_at <= requested_at)


async def acquire_leases(
    session: AsyncSession, owner: str, limit: int, ttl: timedelta
) -> Sequence[Ticket]:
    """Claim up to `limit` reconcilable tickets for `owner`, atomically.

    `SKIP LOCKED` is what keeps N runners from fighting: a row another transaction is claiming
    right now is skipped rather than waited on, so a slow claim never serialises the fleet.
    `status <> 'done'` is the only status filter — a blocked or exited ticket is still leased and
    still reconciled, because unblocking is observed input like anything else; a long-exited one
    only every `EXITED_PASS_INTERVAL`. Quarantine, not
    status, is therefore what removes a ticket from the loop (decision 31): a ticket the loop
    cannot finish thinking about must stop being handed out at all, and a status alone would not
    do it — `reconcile_ticket`'s parked check needs the parking label ON the issue, and writing
    that label is exactly what fails in the archived-repo case quarantine exists for.
    """
    candidates = (
        select(Ticket.id)
        .where(*_leasable(), _due_in_fleet_pass())
        .order_by(Ticket.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Ticket)
        .where(Ticket.id.in_(candidates))
        .values(**_claim(owner, ttl))
        .returning(Ticket)
        # The rows are being read back in this same statement; there is no in-session state to
        # synchronise, and asking SQLAlchemy to try would only add a second query.
        .execution_options(synchronize_session=False)
    )
    return list((await session.execute(stmt)).scalars().all())


async def acquire_lease(
    session: AsyncSession,
    ticket_id: int,
    owner: str,
    ttl: timedelta,
    *,
    requested_at: datetime,
) -> Ticket | None:
    """Claim one ticket for a refresh requested at `requested_at` (database time).

    None when `acquire_leases` would not hand the ticket out, and also when a pass has claimed it
    since the request: that pass already answers it.
    """
    candidate = (
        select(Ticket.id)
        .where(Ticket.id == ticket_id, *_leasable(), _unanswered(requested_at))
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Ticket)
        .where(Ticket.id == candidate)
        .values(**_claim(owner, ttl))
        .returning(Ticket)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def awaiting_pass(session: AsyncSession, ticket_id: int, requested_at: datetime) -> bool:
    """Whether a refresh requested at `requested_at` still wants a pass: the loop still hands the
    ticket out and no pass has claimed it since. `acquire_lease` declining a ticket for which this
    is true means only that a lease is in the way."""
    stmt = select(Ticket.id).where(
        Ticket.id == ticket_id, *_handed_out(), _unanswered(requested_at)
    )
    return (await session.execute(stmt)).first() is not None


async def renew(session: AsyncSession, ticket_id: int, owner: str, ttl: timedelta) -> bool:
    """Extend a lease the caller still holds. False (and no write) if it does not."""
    stmt = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.lease_owner == owner,
            Ticket.lease_expires_at > func.now(),
        )
        .values(lease_expires_at=_expires_in(ttl))
        .returning(Ticket.id)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).first() is not None


async def release(session: AsyncSession, ticket_id: int, owner: str) -> bool:
    """Drop a lease the caller still holds, so the next tick can pick the ticket up at once.

    A no-op for a non-holder — including the caller's *own* expired lease, which someone else may
    already have reclaimed; clearing it then would evict the new holder.
    """
    stmt = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.lease_owner == owner,
            Ticket.lease_expires_at > func.now(),
        )
        .values(lease_owner=None, lease_expires_at=None)
        .returning(Ticket.id)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).first() is not None


async def release_all(session: AsyncSession) -> int:
    """Drop every ticket lease; returns how many there were.

    Only for a worker holding the singleton lock, at startup (decision 40): no other runner should
    exist then (`singleton`'s accepted gap aside), so every lease is a dead process's, and
    `acquire_leases` would leave its ticket out until it expired.
    """
    stmt = (
        update(Ticket)
        .where(Ticket.lease_owner.is_not(None))
        .values(lease_owner=None, lease_expires_at=None)
        .returning(Ticket.id)
        .execution_options(synchronize_session=False)
    )
    return len((await session.execute(stmt)).all())
