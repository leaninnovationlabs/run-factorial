"""The requests a person makes of one ticket: exit, re-enter, requeue (decision 29).

Transport-neutral: the dashboard's routes and the MCP server's tools both call these, so a
refusal reads the same on either surface and the loop sees one event shape. Exit and re-entry
append a request, commit it, wake the worker for the ticket and wait for that pass
(`api.wakes`); the worker does the transition. Requeue writes the ticket itself, and its
docstring says why.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.results import RequestResult
from orchestrator.api.wakes import wake_and_wait
from orchestrator.db.models import StepRun, Ticket, TicketStatus, current_runs
from orchestrator.events import EventType, append

__all__ = [
    "nothing_to",
    "refuse_while_quarantined",
    "request_exit",
    "request_reentry",
    "request_requeue",
]


def refuse_while_quarantined(ticket: Ticket, verb: str) -> None:
    """Refuse a request the loop could not read: it does not lease a quarantined ticket.

    Quarantine leaves `status` and the `step_run` row exactly where they stood, so every gesture
    stays reachable on a ticket nothing will ever pick up again; a gesture that asks the loop to
    act refuses here rather than wait for a pass that never comes.
    """
    if ticket.quarantined_at is not None:
        raise Conflict(
            f"This ticket is stopped after repeated errors. Requeue it before you {verb} it.",
            code="quarantined",
            tool_hint="factorial_ticket_requeue",
        )


def nothing_to(ticket: Ticket, verb: str) -> Conflict:
    """The refusal of a request a done or exited ticket gives nothing to act on."""
    if ticket.status == TicketStatus.EXITED:
        return Conflict(
            f"This ticket has exited, so there is nothing to {verb}.", code="ticket_exited"
        )
    return Conflict(f"This ticket is done, so there is nothing to {verb}.", code="ticket_done")


async def _latest_attempt(session: AsyncSession, ticket: Ticket) -> int | None:
    """The highest current attempt at the ticket's current step, or None when nothing current
    has run there yet.

    Carried on the request events so the ticket page files them under the block they are
    about; without it `segmentsFrom` has no key and invents an "unknown" one.
    """
    return (
        await session.execute(
            select(func.max(StepRun.attempt)).where(
                StepRun.ticket_id == ticket.id,
                current_runs(ticket.generation),
                StepRun.step_name == ticket.current_step,
            )
        )
    ).scalar_one_or_none()


async def request_exit(
    session: AsyncSession, ticket: Ticket, actor: Actor, *, close_issue: bool = False
) -> RequestResult:
    """Ask the loop to take this ticket off the factory floor (decision 29).

    The worker cancels live work, parks the ticket as `exited` and projects `factorial:exit` on
    the pass the request wakes; `landed` says whether that pass came in time. With `close_issue`
    it closes the issue as well (decision 79). Refused for a ticket that is done or already
    exited: accepting would record a request the loop can never honour. Refused while
    quarantined too.
    """
    if ticket.status == TicketStatus.EXITED:
        raise Conflict("This ticket has already exited.", code="ticket_exited")
    if ticket.status == TicketStatus.DONE:
        raise nothing_to(ticket, "exit")
    refuse_while_quarantined(ticket, "exit")
    await append(
        session,
        type=EventType.EXIT_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        # Named although None: pyright checks the unpacked fields against every parameter the
        # call leaves unmatched.
        step_run_id=None,
        **actor.fields(),
        step=ticket.current_step,
        attempt=await _latest_attempt(session, ticket),
        **({"close_issue": True} if close_issue else {}),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="exit", status="requested", landed=landed)


async def request_requeue(session: AsyncSession, ticket: Ticket, actor: Actor) -> RequestResult:
    """Put a quarantined ticket back into the loop.

    Unlike exit and re-entry, this writes the ticket directly instead of appending a request
    for the loop to honour on its next pass: `acquire_leases` excludes a quarantined ticket, so
    the loop would never lease it to read the request, and it would sit unread for ever.
    Clearing `quarantined_at`/`consecutive_failures` is not advancing the state machine — it is
    an operational flag that gates leasing, not a step, status, or transition. It is still
    evented: the timeline is the audit trail, and a quarantine with no answer in the log reads
    as a ticket nobody ever recovered.
    """
    if ticket.quarantined_at is None:
        raise Conflict(
            "This ticket isn't stopped after repeated errors, so there is nothing to requeue.",
            code="not_quarantined",
        )
    await append(
        session,
        type=EventType.TICKET_REQUEUED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=None,
        **actor.fields(),
        # Read before the clear: the count `ticket_quarantined` fired on lives nowhere else once
        # the column is zeroed.
        failures=ticket.consecutive_failures,
    )
    ticket.quarantined_at = None
    ticket.consecutive_failures = 0
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="requeue", status="requeued", landed=landed)


async def request_reentry(session: AsyncSession, ticket: Ticket, actor: Actor) -> RequestResult:
    """Ask the loop to bring an exited ticket back. The loop refuses visibly while the issue is
    closed (`reentry_refused`), so that is not checked here: this answers from Postgres.

    Quarantine is refused, though, for the reason `request_exit` gives: an exited ticket is
    still leased and so can be quarantined, and once it is, nothing reads the request.
    """
    if ticket.status != TicketStatus.EXITED:
        raise Conflict(
            "This ticket hasn't exited, so there is nothing to re-enter.", code="not_exited"
        )
    refuse_while_quarantined(ticket, "re-enter")
    await append(
        session,
        type=EventType.REENTRY_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=None,
        **actor.fields(),
        step=ticket.current_step,
        attempt=await _latest_attempt(session, ticket),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="reenter", status="requested", landed=landed)
