"""Write tools for one ticket's controls: each records the request the dashboard's button records.

The worker honours a request on the pass it wakes (`api.wakes`). The tool reads the ticket again
after that wait, so `ticket_status` is what the pass did when `landed` is true.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import (
    request_interrupt,
    request_resume,
    request_retry,
    request_unblock,
)
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.refs import current_run, resolve_ticket
from orchestrator.api.services.restarts import request_restart
from orchestrator.api.services.results import RequestResult
from orchestrator.api.services.tickets import request_exit, request_reentry, request_requeue
from orchestrator.db.models import Project, StepRun, Ticket, TicketSource, intake_column
from orchestrator.events.read_models import BlockedOn, blocked_on, ticket_row
from orchestrator.mcp_server.auth import current_actor
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import respond
from orchestrator.mcp_server.tokens import WRITE_SCOPE
from orchestrator.mcp_server.tools.params import TicketArg, TicketProjectArg

__all__ = [
    "TOOLS",
    "RequestResultOut",
    "require_run",
    "run_ticket_exit",
    "run_ticket_interrupt",
    "run_ticket_reenter",
    "run_ticket_requeue",
    "run_ticket_restart",
    "run_ticket_resume",
    "run_ticket_retry",
    "run_ticket_unblock",
    "ticket_ref",
]


class RequestResultOut(BaseModel):
    ref: str
    action: str
    status: Literal["requested", "pending", "already", "requeued"] = Field(
        description="requested: recorded now. pending: an earlier identical request is still "
        "unanswered, so nothing was recorded. already: the ticket is in that state. requeued: "
        "quarantine cleared."
    )
    landed: bool = Field(
        description="The worker's pass finished within the wait. When false, read the ticket "
        "again with factorial_ticket_get; do not repeat the request."
    )
    ticket_status: str
    blocked_on: BlockedOn | None = None


async def ticket_ref(session: AsyncSession, ticket: Ticket) -> str:
    repo = (
        await session.execute(
            select(intake_column())
            .select_from(TicketSource)
            .join(Project, Project.id == TicketSource.project_id)
            .where(TicketSource.id == ticket.ticket_source_id)
        )
    ).scalar_one()
    return f"{repo}#{ticket.external_key}"


async def require_run(session: AsyncSession, ticket: Ticket, ref: str, verb: str) -> StepRun:
    run = await current_run(session, ticket)
    if run is None:
        raise Conflict(
            f"{ref} has no run at {ticket.current_step!r}, so there is nothing to {verb}."
        )
    return run


async def _answer(
    session: AsyncSession, ticket: Ticket, ref: str, result: RequestResult
) -> ToolResult:
    # A fresh read: the woken pass committed after the service read `ticket`.
    row = await ticket_row(session, ticket.id)
    waiting = blocked_on(row)
    out = RequestResultOut(
        ref=ref,
        action=result.action,
        status=result.status,
        landed=result.landed,
        ticket_status=row.status,
        blocked_on=waiting,
    )
    if result.landed:
        held = f", blocked on {waiting}" if waiting is not None else ""
        outcome = f"landed; the ticket is {row.status}{held}"
    else:
        outcome = "not landed yet; read it again with factorial_ticket_get, do not repeat"
    return respond(out, f"{ref}: {result.action} {result.status}; {outcome}")


@tool(WRITE_SCOPE, title="Exit a ticket", output=RequestResultOut, destructive=True)
async def factorial_ticket_exit(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Take a ticket off the factory floor: cancels any running agent run (its work is not kept)
    and marks the ticket exited. factorial_ticket_reenter brings it back with a fresh attempt.
    Refused for a done, exited or quarantined ticket."""
    return await run_ticket_exit(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_exit(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    return await _answer(tc.session, row, ref, await request_exit(tc.session, row, actor))


@tool(WRITE_SCOPE, title="Re-enter an exited ticket", output=RequestResultOut)
async def factorial_ticket_reenter(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Bring an exited ticket back for a fresh attempt at the step it left. The worker refuses
    while the issue is closed; a quarantined ticket needs factorial_ticket_requeue first."""
    return await run_ticket_reenter(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_reenter(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    return await _answer(tc.session, row, ref, await request_reentry(tc.session, row, actor))



@tool(
    WRITE_SCOPE,
    title="Restart a ticket on its lane's current process",
    output=RequestResultOut,
    destructive=True,
)
async def factorial_ticket_restart(
    ticket: TicketArg,
    project: TicketProjectArg = None,
    process_version_id: Annotated[
        int | None,
        Field(
            description="The lane head to restart on (factorial_ticket_get's lane_head); "
            "refused once the lane has moved. Omit for the head as it is."
        ),
    ] = None,
    allow_current_pin: Annotated[
        bool,
        Field(description="Restart a ticket that is already on the lane head, from scratch."),
    ] = False,
    allow_merged: Annotated[
        bool,
        Field(
            description="Go ahead although a ticket PR is already merged into a branch it does "
            "not own; a restart may repeat that work. Only after the person says so."
        ),
    ] = False,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Restart a ticket from scratch on its lane's current process. The worker cancels the run at
    the current step, abandons a parked session, closes the ticket's open pull requests and
    deletes its branches, then starts the new process's entry step. Nothing earlier is reused.
    Refused for a done or quarantined ticket, a retired lane, or a process_version_id that is no
    longer the lane head. A ticket already on the head needs allow_current_pin=true, and one with
    a PR merged outside its branches needs allow_merged=true. Refused while the issue is closed."""
    return await run_ticket_restart(
        tc,
        current_actor(write=True),
        ticket=ticket,
        project=project,
        process_version_id=process_version_id,
        allow_current_pin=allow_current_pin,
        allow_merged=allow_merged,
    )


async def run_ticket_restart(
    tc: ToolContext,
    actor: Actor,
    *,
    ticket: str,
    project: str | None,
    process_version_id: int | None,
    allow_current_pin: bool,
    allow_merged: bool,
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    result = await request_restart(
        tc.session,
        row,
        actor,
        process_version_id=process_version_id,
        allow_current_pin=allow_current_pin,
        allow_merged=allow_merged,
    )
    return await _answer(tc.session, row, ref, result)


@tool(WRITE_SCOPE, title="Interrupt a ticket's agent", output=RequestResultOut)
async def factorial_ticket_interrupt(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Stop the agent running the ticket's current step and park the ticket, keeping the agent's
    workspace so factorial_ticket_resume can continue it. Only an agent run that is running or
    idle can be interrupted."""
    return await run_ticket_interrupt(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_interrupt(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    run = await require_run(tc.session, row, ref, "interrupt")
    return await _answer(tc.session, row, ref, await request_interrupt(tc.session, run, actor))


@tool(WRITE_SCOPE, title="Resume a parked session", output=RequestResultOut)
async def factorial_ticket_resume(
    ticket: TicketArg,
    project: TicketProjectArg = None,
    answer: Annotated[
        str | None,
        Field(
            max_length=8000,
            description="The session's next turn, as if typed into its terminal. Optional.",
        ),
    ] = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Continue an agent session parked at the ticket's current step (interrupted or idle), in
    the workspace it kept. It never lifts a wall-clock ceiling park: a person resumes that from
    the dashboard."""
    return await run_ticket_resume(
        tc, current_actor(write=True), ticket=ticket, project=project, answer=answer
    )


async def run_ticket_resume(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None, answer: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    run = await require_run(tc.session, row, ref, "resume")
    result = await request_resume(tc.session, run, actor, answer=answer)
    return await _answer(tc.session, row, ref, result)


@tool(WRITE_SCOPE, title="Unblock an escalated ticket", output=RequestResultOut)
async def factorial_ticket_unblock(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Release an escalation, as removing `factorial:blocked` on GitHub would. The loop grants
    the step one fresh attempt after its retry backoff (a paid agent run), or reopens a
    rejected gate for review. Only for a ticket blocked on an escalation. A parked session
    takes factorial_ticket_resume instead."""
    return await run_ticket_unblock(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_unblock(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    return await _answer(tc.session, row, ref, await request_unblock(tc.session, row, actor))


@tool(WRITE_SCOPE, title="Retry a failed run now", output=RequestResultOut)
async def factorial_ticket_retry(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Start the retry of the failed run at the ticket's current step now, instead of after its
    backoff. Starts a new, paid agent run. Refused unless the run is waiting out that backoff."""
    return await run_ticket_retry(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_retry(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    run = await require_run(tc.session, row, ref, "retry")
    return await _answer(tc.session, row, ref, await request_retry(tc.session, run, actor))


@tool(WRITE_SCOPE, title="Requeue a quarantined ticket", output=RequestResultOut, open_world=False)
async def factorial_ticket_requeue(
    ticket: TicketArg, project: TicketProjectArg = None, tc: ToolContext = Depends(tool_context)
) -> ToolResult:
    """Put a quarantined ticket (the worker stopped leasing it after repeated failures) back into
    the loop. Refused for a ticket that is not quarantined."""
    return await run_ticket_requeue(tc, current_actor(write=True), ticket=ticket, project=project)


async def run_ticket_requeue(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    ref = await ticket_ref(tc.session, row)
    return await _answer(tc.session, row, ref, await request_requeue(tc.session, row, actor))


TOOLS: list[ToolSpec] = [
    factorial_ticket_exit,
    factorial_ticket_reenter,
    factorial_ticket_restart,
    factorial_ticket_interrupt,
    factorial_ticket_resume,
    factorial_ticket_unblock,
    factorial_ticket_retry,
    factorial_ticket_requeue,
]
