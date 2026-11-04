"""Redoing a step from a clean start, with every step after it (decision 76).

Dashboard only: a redo closes pull requests and deletes branches, so neither an MCP caller nor an
assistant's proposal, which a person may approve without reading it, may ask for one. The request
appends `redo_requested`, commits, and waits for the pass it wakes (`api.wakes`). The worker
checks everything again from what it observes when it answers (`worker.redos`), and refuses a
request whose ticket moved on meanwhile; the checks here only stop a request the loop could never
honour from being recorded.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import dashboard_only
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.outputs import pinned_config
from orchestrator.api.services.results import RequestResult
from orchestrator.api.services.tickets import (
    _latest_attempt,
    nothing_to,
    refuse_while_quarantined,
)
from orchestrator.api.wakes import wake_and_wait
from orchestrator.db.models import Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.worker.redos import pending_redo_request, redo_target

__all__ = ["request_redo"]


async def request_redo(
    session: AsyncSession, ticket: Ticket, actor: Actor, *, target_step: str, note: str | None
) -> RequestResult:
    """Ask the loop to redo `target_step`, the step `redo_target` names for the ticket now.

    The request records the step, attempt and generation the ticket is at, which the worker
    compares; at a waiting gate the attempt is the gate run's. `note` is kept on this event
    alone, as data for the fresh agent. While one redo is unanswered another is refused: folded
    in, its note would be lost.
    """
    dashboard_only(
        actor, action="redo a step", hint="Press Redo on the ticket page in the dashboard."
    )
    # Locked and re-read: a second press waits for the first one's commit and finds it pending.
    await session.execute(
        select(Ticket)
        .where(Ticket.id == ticket.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket.status in (TicketStatus.DONE, TicketStatus.EXITED):
        raise nothing_to(ticket, "redo")
    refuse_while_quarantined(ticket, "redo")
    target = redo_target(await pinned_config(session, ticket), ticket.current_step, ticket.status)
    if target is None:
        raise Conflict(
            f"There is nothing to redo at {ticket.current_step}.",
            hint="Redo is offered on the step the ticket is on, and on the step a waiting "
            "review gate reviews.",
        )
    if target != target_step:
        raise Conflict(f"{target_step} can't be redone now. A redo here redoes {target}.")
    if await pending_redo_request(session, ticket.id) is not None:
        raise Conflict(
            "A redo of this ticket is already being answered.",
            hint="Wait for its answer on the timeline, then press again.",
        )
    await append(
        session,
        type=EventType.REDO_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=None,
        **actor.fields(),
        target_step=target_step,
        has_note=bool(note),
        # The one place the text is kept: the worker reads it from here for the fresh agent.
        **({"note": note} if note else {}),
        step=ticket.current_step,
        attempt=await _latest_attempt(session, ticket),
        generation=ticket.generation,
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="redo", status="requested", landed=landed)
