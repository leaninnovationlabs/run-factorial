"""Who may attach to a run's terminal (decision 57, design.md §7.14): one rule, shared by the
gateway's handshake and its recheck, `GET /step-runs/{id}/terminal`, and the inspect pass's
sentences.

The target comes only from the log the worker wrote: the gateway never builds an adapter, which
could create, pause or kill a sandbox (decision 25).

Also what a person has done with a run's session, which the worker's idle deadline and the
drawer both read: whether it parks for them or ends and retries (decision 56).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from orchestrator.db.models import (
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
    current_runs,
)
from orchestrator.events.read_models import latest_run_event
from orchestrator.events.types import EventType, ResumedVia
from orchestrator.events.workspaces import LIVE_STATUSES, kept_workspaces
from orchestrator.terminal.protocol import CloseCode
from runtime.contract import IdleReason, TerminalTarget

__all__ = [
    "Admission",
    "AdmissionKind",
    "Refusal",
    "TerminalRun",
    "ask_this_stretch",
    "is_current_run",
    "last_terminal_input",
    "person_engaged",
    "question_parks_now",
    "stretch_starts",
    "terminal_admission",
    "typed_since",
    "waits_for_a_person",
]

log = logging.getLogger(__name__)

AdmissionKind = Literal["session", "inspect"]

SWITCHED_OFF: Final = "The terminal is switched off on this Factorial install."
PARKED: Final = "This session is paused. Press Resume session, or open the session read-only."
NO_TARGET: Final = "This run has no terminal."
READ_ONLY_VIEW: Final = "An open session is read-only."
READ_ONLY_MOVED: Final = "The ticket has moved past this step, so this session is read-only."
#: `step_run.id` is a bigint: past it the query would fail, not miss.
_MAX_ID: Final = 2**63 - 1


@dataclass(frozen=True, slots=True)
class Refusal:
    code: CloseCode
    #: Shown to the person as it stands, and short enough for a close frame's reason.
    sentence: str


@dataclass(frozen=True, slots=True)
class TerminalRun:
    """What the gateway needs of a run besides its target: where its audit events go, and
    which runtime row holds the key its sandbox answers to."""

    step_run_id: int
    project_id: int
    ticket_id: int
    step: str
    attempt: int
    status: StepRunStatus
    runtime_id: int | None


@dataclass(frozen=True, slots=True)
class Admission:
    """`kind` and `target` are both set exactly when `refusal` is None. `writable`: a person may
    type, which only the ticket's current run allows."""

    run: TerminalRun | None
    kind: AdmissionKind | None = None
    target: TerminalTarget | None = None
    refusal: Refusal | None = None
    writable: bool = False

    @property
    def read_only(self) -> str | None:
        """Why an admitted terminal cannot be typed into; None when it can, or when none is
        admitted."""
        if self.kind is None or self.writable:
            return None
        return READ_ONLY_VIEW if self.kind == "inspect" else READ_ONLY_MOVED

    @property
    def status(self) -> str | None:
        return self.run.status.value if self.run is not None else None


def _refuse(run: TerminalRun | None, code: CloseCode, sentence: str) -> Admission:
    return Admission(run=run, refusal=Refusal(code, sentence))


async def terminal_admission(
    session: AsyncSession,
    step_run_id: int,
    *,
    now: datetime,
    retention: timedelta,
    enabled: bool,
) -> Admission:
    """In order: the switch; an agent run; quarantine; a live session (writable only
    when it is the latest attempt at its ticket's current step); an open session; a parked
    session (4409); otherwise nothing to attach to (4404)."""
    if not enabled:
        return _refuse(None, CloseCode.FORBIDDEN, SWITCHED_OFF)
    if not 0 < step_run_id <= _MAX_ID:
        return _refuse(None, CloseCode.NO_TERMINAL, "This run doesn't exist.")
    row = (
        await session.execute(
            select(
                StepRun,
                Ticket.project_id,
                Ticket.quarantined_at,
                Ticket.current_step,
                Ticket.status,
            )
            .join(Ticket, Ticket.id == StepRun.ticket_id)
            .where(StepRun.id == step_run_id)
        )
    ).first()
    if row is None or row[0].kind != StepKind.AGENT:
        return _refuse(None, CloseCode.NO_TERMINAL, "Only an agent's run has a terminal.")
    step_run, project_id, quarantined_at, current_step, ticket_status = row
    run = TerminalRun(
        step_run_id=step_run.id,
        project_id=project_id,
        ticket_id=step_run.ticket_id,
        step=step_run.step_name,
        attempt=step_run.attempt,
        status=StepRunStatus(step_run.status),
        runtime_id=step_run.runtime_id,
    )
    if quarantined_at is not None:
        return _refuse(
            run,
            CloseCode.FORBIDDEN,
            "This ticket is stopped after repeated errors. Requeue it before you open its "
            "terminal.",
        )

    if run.status in LIVE_STATUSES:
        # The latest spawn or resume decides: one that recorded no target leaves none, rather
        # than an earlier one that may name a session the resume replaced.
        details = await _latest_details(
            session, run, (EventType.AGENT_SPAWNED, EventType.AGENT_RESUMED)
        )
        target = _target(details, run)
        if target is None:
            return _refuse(run, CloseCode.NO_TERMINAL, NO_TARGET)
        writable = ticket_status != TicketStatus.DONE and await is_current_run(
            session,
            ticket_id=run.ticket_id,
            step=run.step,
            attempt=run.attempt,
            current_step=current_step,
        )
        return Admission(run=run, kind="session", target=target, writable=writable)

    details = await _latest_details(
        session, run, (EventType.INSPECT_STARTED, EventType.INSPECT_PARKED)
    )
    if details is not None and details[0] == EventType.INSPECT_STARTED:
        target = _target(details, run)
        if target is None:
            return _refuse(run, CloseCode.NO_TERMINAL, NO_TARGET)
        return Admission(run=run, kind="inspect", target=target)

    if run.status == StepRunStatus.INTERRUPTED:
        return _refuse(run, CloseCode.PARKED, PARKED)
    kept = await kept_workspaces(session, now, retention, ticket_id=run.ticket_id)
    if step_run_id in kept.runs:
        return _refuse(
            run,
            CloseCode.NO_TERMINAL,
            "This run has ended. Open the session read-only to look at its workspace.",
        )
    return _refuse(run, CloseCode.NO_TERMINAL, "This run has ended and its files were cleaned up.")


async def is_current_run(
    session: AsyncSession, *, ticket_id: int, step: str, attempt: int, current_step: str
) -> bool:
    """The run is the latest attempt at its ticket's current step, and current: the one its
    ticket is on."""
    if step != current_step:
        return False
    current = await session.scalar(
        select(StepRun.id)
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .where(
            StepRun.ticket_id == ticket_id,
            StepRun.step_name == step,
            StepRun.attempt == attempt,
            current_runs(Ticket.generation),
        )
    )
    if current is None:
        return False
    later = await session.scalar(
        select(StepRun.id)
        .where(
            StepRun.ticket_id == ticket_id,
            StepRun.step_name == step,
            StepRun.attempt > attempt,
        )
        .limit(1)
    )
    return later is None


async def last_terminal_input(session: AsyncSession, run: StepRun) -> datetime | None:
    """When the gateway last recorded a writer's typing into the run's terminal: a human
    answering, which holds off the worker's finish and idle park (decision 56). Watchers write
    no `terminal_input`, so they hold off neither."""
    return await session.scalar(
        latest_run_event(run.ticket_id, run.id, [EventType.TERMINAL_INPUT], Event.created_at)
    )


def _resumed_not_rolled_over(event: type[Event]) -> ColumnElement[bool]:
    return and_(
        event.type == EventType.AGENT_RESUMED,
        event.details["via"].astext.is_distinct_from(ResumedVia.ROLLOVER),
    )


def stretch_starts(event: type[Event]) -> ColumnElement[bool]:
    """The events an active stretch starts at. Not a rollover's resume: it continues the stretch
    with a fresh token, or a busy session that rolls over every 55 minutes would never time
    out."""
    return or_(event.type == EventType.AGENT_ACTIVE, _resumed_not_rolled_over(event))


def typed_since(typed: datetime | None, since: datetime) -> bool:
    """Has a writer typed into the terminal since `since`, by the gateway's latest
    `terminal_input`? Only the gateway's word counts where typing holds off a park: the
    session's own `last_input_at` is agent-writable."""
    return typed is not None and typed >= since


def question_parks_now(expires_at: object, typed: datetime | None, since: datetime) -> bool:
    """A question the CLI would answer itself at `expires_at` (codex's question box) parks at
    the first tick that sees it, unless a writer typed since it was asked."""
    return expires_at is not None and not typed_since(typed, since)


async def waits_for_a_person(
    session: AsyncSession,
    run: StepRun,
    reason: str | None,
    typed: datetime | None,
    *,
    review: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """Does an unmet stop park for a person rather than end and retry (decision 56)? It does
    when the step awaits review (`review`, `awaits_review` of its postconditions), when the
    agent asked, or its turn failed, this spell (`reason`) or earlier in the stretch, or when a
    person worked with the session. `typed` is the run's `last_terminal_input`.

    Also `ask_this_stretch` when it was read: None when `review` or the spell's own reason
    decided."""
    if review or reason in (IdleReason.QUESTION, IdleReason.TURN_ERROR):
        return True, None
    ask = await ask_this_stretch(session, run)
    if ask is not None:
        return True, ask
    # Typing is engagement on its own: `person_engaged` would find the same `terminal_input`.
    return typed is not None or await person_engaged(session, run), None


async def person_engaged(session: AsyncSession, run: StepRun) -> bool:
    """Has a person worked with this run's session: typed into it through the gateway, or
    released one of its parks? Either makes an unmet stop wait for them rather than retry
    (decision 56). Never the session's own report of input, which its agent can forge, nor a
    rollover's resume, which the loop grants itself. A retry is a new run, and starts
    unengaged."""
    found = await session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            or_(Event.type == EventType.TERMINAL_INPUT, _resumed_not_rolled_over(Event)),
        )
        .limit(1)
    )
    return found is not None


async def ask_this_stretch(session: AsyncSession, run: StepRun) -> dict[str, Any] | None:
    """The latest `agent_idle` of the run's active stretch that asked a question or ended on a
    failed turn. A rollover does not start a stretch, so a question asked before one survives
    the session's return to a plain prompt."""
    stretch = aliased(Event)
    boundary = (
        select(stretch.id)
        .where(
            stretch.ticket_id == run.ticket_id,
            stretch.step_run_id == run.id,
            stretch_starts(stretch),
        )
        .order_by(stretch.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    details = await session.scalar(
        select(Event.details)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            Event.type == EventType.AGENT_IDLE,
            Event.details["reason"].astext.in_([IdleReason.QUESTION, IdleReason.TURN_ERROR]),
            Event.id > func.coalesce(boundary, 0),
        )
        .order_by(Event.id.desc())
        .limit(1)
    )
    return dict(details) if details is not None else None


async def _latest_details(
    session: AsyncSession, run: TerminalRun, types: tuple[EventType, ...]
) -> tuple[str, dict[str, Any]] | None:
    row = (
        await session.execute(
            latest_run_event(run.ticket_id, run.step_run_id, types, Event.type, Event.details)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1] or {}


def _target(details: tuple[str, dict[str, Any]] | None, run: TerminalRun) -> TerminalTarget | None:
    if details is None or details[1].get("terminal") is None:
        return None
    try:
        return TerminalTarget.model_validate(details[1]["terminal"])
    except ValidationError:
        log.warning("step run %s: its recorded terminal does not parse", run.step_run_id)
        return None
