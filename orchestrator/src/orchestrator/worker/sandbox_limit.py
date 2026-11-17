"""The Factorial-managed key's limit: how many active runs on it an organization, and the whole
pool, may have at one time. A start past either waits on its step; nothing fails and no attempt is
spent.

Only RUNNING runs count. A session waiting at its prompt for a person (IDLE) holds no place,
because lanes wait on people often and two such tickets would hold every other ticket of an
organization. Only new attempts are asked: a resume, a preview and an inspection wake a sandbox
that already exists. So a resumed session can take an organization over its limit for a while,
and the sandboxes alive in E2B, active and idle together, can exceed it; E2B's project limit and
spending limit are the hard stops.

The limit is exact inside one worker process, which is all decision 40 runs per database. A pass
reconciles its tickets at once, each in a transaction that commits only after its spawn returns,
so the committed count alone would let every ticket of a burst start: each start that passed is
reserved in `SandboxStarts` until its transaction ends, and the next check counts it. No database
lock is taken, and nothing is held across a spawn. A second worker, in decision 40's accepted
gap, counts only what the first committed and can overshoot; E2B's own project limit is the hard
stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    Event,
    Organization,
    Runtime,
    StepRun,
    StepRunStatus,
    Ticket,
    current_runs,
)
from orchestrator.events import EventType
from orchestrator.settings import ManagedSandbox

if TYPE_CHECKING:
    from orchestrator.worker.tick_context import _Tick

#: IDLE is left out on purpose: a session waiting for a person must not hold a start back.
_ACTIVE: Final = (StepRunStatus.RUNNING,)


@dataclass(frozen=True, slots=True)
class LimitReached:
    scope: Literal["organization", "pool"]
    limit: int
    running: int


@dataclass(slots=True)
class SandboxStarts:
    """The managed starts that passed the limit in this process and whose ticket's transaction
    has not ended: `ticket.id -> organization.id`. Released by `worker.loop._reconcile_leased`."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    in_flight: dict[int, int] = field(default_factory=dict)

    async def reserve(
        self,
        session: AsyncSession,
        ticket_id: int,
        organization: Organization,
        managed: ManagedSandbox,
    ) -> LimitReached | None:
        """`sandbox_limit` with the other tickets' starts in flight counted. A start under both
        limits is reserved; one held drops any reservation of its own."""
        async with self.lock:
            others = {t: o for t, o in self.in_flight.items() if t != ticket_id}
            reached = await sandbox_limit(session, organization, managed, in_flight=others)
            if reached is None:
                self.in_flight[ticket_id] = organization.id
            else:
                self.in_flight.pop(ticket_id, None)
            return reached

    def release(self, ticket_id: int) -> None:
        self.in_flight.pop(ticket_id, None)


async def managed_running(session: AsyncSession, organization_id: int | None = None) -> int:
    """Active runs on managed runtimes, of `organization_id` or of every organization."""
    query = (
        select(func.count(StepRun.id))
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .join(Runtime, Runtime.id == StepRun.runtime_id)
        .where(
            current_runs(Ticket.generation),
            StepRun.status.in_(_ACTIVE),
            Runtime.details["sandbox_key"].astext == "managed",
        )
    )
    if organization_id is not None:
        query = query.where(Runtime.organization_id == organization_id)
    return await session.scalar(query) or 0


async def sandbox_limit(
    session: AsyncSession,
    organization: Organization,
    managed: ManagedSandbox,
    *,
    in_flight: Mapping[int, int] | None = None,
) -> LimitReached | None:
    """The limit a new managed sandbox of `organization` would pass, or None. `in_flight` is
    starts not committed yet (`SandboxStarts`). The organization's limit comes first: it is the
    one a person can act on."""
    pending = in_flight or {}
    limit = managed.limit_for(organization.slug)
    running = await managed_running(session, organization.id) + sum(
        1 for owner in pending.values() if owner == organization.id
    )
    if running >= limit:
        return LimitReached("organization", limit, running)
    pool = await managed_running(session) + len(pending)
    if pool >= managed.pool_limit:
        return LimitReached("pool", managed.pool_limit, pool)
    return None


async def note_sandbox_limit_waiting(
    tick: _Tick, step_name: str, reached: LimitReached, *, attempt: int, runtime: str
) -> None:
    """Say once per entry of `step_name` that its start waits on `reached`, and again only when
    the scope, the limit or the held attempt changes: a count that moves is not news. A held
    retry names its own attempt so the ticket's newest event never names an attempt that has run."""
    entered_id = (
        await tick.session.scalar(
            select(Event.id)
            .where(
                Event.ticket_id == tick.ticket.id,
                Event.type == EventType.STEP_ENTERED,
                Event.details["step"].astext == step_name,
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
        or 0
    )
    said = (entered_id, step_name, reached.scope, reached.limit, attempt)
    if tick.deps.sandbox_limit_seen.get(tick.ticket.id) == said:
        return
    # After a worker restart empties the memo, the log keeps it once per entry. The memo is set
    # only from the log, so a tick that rolls back its event leaves nothing remembered.
    last = await tick.session.scalar(
        select(Event.details)
        .where(
            Event.ticket_id == tick.ticket.id,
            Event.type == EventType.SANDBOX_LIMIT_WAITING,
            Event.details["step"].astext == step_name,
            Event.id > entered_id,
        )
        .order_by(Event.id.desc())
        .limit(1)
    )
    if last is not None and (last.get("scope"), last.get("limit"), last.get("attempt")) == (
        reached.scope,
        reached.limit,
        attempt,
    ):
        tick.deps.sandbox_limit_seen[tick.ticket.id] = said
        return
    await tick.emit(
        EventType.SANDBOX_LIMIT_WAITING,
        step=step_name,
        attempt=attempt,
        scope=reached.scope,
        limit=reached.limit,
        running=reached.running,
        runtime=runtime,
    )
