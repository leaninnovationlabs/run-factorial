"""Rows the terminal tests seed: an agent run, its spawn with or without a recorded target, and
its inspect events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from runtime.contract import TerminalTarget
from tests.api_harness import seed_project, seed_ticket


async def seed_run(
    session: AsyncSession,
    *,
    target: TerminalTarget | None,
    status: StepRunStatus = StepRunStatus.RUNNING,
    kind: StepKind = StepKind.AGENT,
    ticket_status: TicketStatus = TicketStatus.RUNNING,
    quarantined: bool = False,
    ticket_step: str = "develop",
) -> StepRun:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, step=ticket_step, status=ticket_status)
    now = datetime.now(UTC)
    if quarantined:
        ticket.quarantined_at = now
    live = status in (StepRunStatus.RUNNING, StepRunStatus.IDLE)
    run = StepRun(
        ticket_id=ticket.id,
        step_name="develop",
        kind=kind,
        attempt=1,
        status=status,
        runtime_run_id="run-terminal",
        started_at=now,
        ended_at=None if live else now,
    )
    session.add(run)
    await session.flush()
    await record(
        session,
        run,
        EventType.AGENT_SPAWNED,
        **({"terminal": target.model_dump()} if target is not None else {}),
    )
    return run


async def record(session: AsyncSession, run: StepRun, type: EventType, **details: object) -> None:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    await append(
        session,
        type=type,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        **details,
    )
