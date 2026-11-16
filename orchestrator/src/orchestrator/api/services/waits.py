"""A run's idle spell or park, read from the log.

The terminal panel and `factorial_ticket_get` both show what a waiting session asked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.schemas import TerminalIdleOut, TerminalParkedOut
from orchestrator.api.services.outputs import pinned_review_step
from orchestrator.db.models import Event, StepRun, StepRunStatus, Ticket
from orchestrator.events import EventType, ResumedVia
from orchestrator.events.read_models import park_reason
from orchestrator.events.terminals import (
    last_terminal_input,
    question_parks_now,
    waits_for_a_person,
)
from orchestrator.events.types import aware_time
from orchestrator.predicates import Unmet
from runtime.contract import ParkReason

__all__ = ["session_waits"]


#: The events a run's idle spells and parks are read from, newest first.
_WAITS: Final = (
    EventType.AGENT_IDLE,
    EventType.AGENT_ACTIVE,
    EventType.AGENT_RESUMED,
    EventType.AGENT_FINISHING,
    EventType.AGENT_PARKED,
    EventType.AGENT_INTERRUPTED,
)
_WAITS_READ: Final = 200


async def session_waits(
    session: AsyncSession, ticket: Ticket, run: StepRun, idle_park_after: timedelta
) -> tuple[TerminalIdleOut | None, TerminalParkedOut | None]:
    """An IDLE run's spell, or a parked one's park, from the log. `ticket` is the run's."""
    if run.status not in (StepRunStatus.IDLE, StepRunStatus.INTERRUPTED):
        return None, None
    rows = (
        await session.scalars(
            select(Event)
            .where(
                # The ticket filter is for `event_ticket_idx`; the run is what selects.
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type.in_(_WAITS),
            )
            .order_by(Event.id.desc())
            .limit(_WAITS_READ)
        )
    ).all()
    # A rollover's park and resume continue the stretch they interrupt.
    stretch = [row for row in rows if not _rollover(row)]
    if run.status == StepRunStatus.IDLE:
        spell = _spell(stretch)
        if spell is None:
            return None, None
        since = aware_time(spell.details.get("idle_since")) or spell.created_at
        typed = await last_terminal_input(session, run)
        # When the worker parks (`sessions._waited_out`), less the input only the session reports.
        anchors = (since, _stretch_start(stretch, run), typed)
        parks_at = max(at for at in anchors if at is not None) + idle_park_after
        if question_parks_now(spell.details.get("question_expires_at"), typed, since):
            parks_at = since
        reason = str(spell.details.get("reason") or "idle")
        # A stop that does not park ends at the deadline, and its step retries. The pinned config
        # is read only when the log does not decide.
        parks, _ = await waits_for_a_person(session, run, reason, typed, review=False)
        parks = parks or await pinned_review_step(session, ticket, run) is not None
        return (
            TerminalIdleOut(
                reason=reason,
                since=since,
                question=_text(spell.details.get("question")),
                parks_at=parks_at,
                at_deadline="park" if parks else "retry",
            ),
            None,
        )
    # The latest park is the one the run is parked on, a rollover whose resume failed included.
    park = next(
        (row for row in rows if row.type in (EventType.AGENT_PARKED, EventType.AGENT_INTERRUPTED)),
        None,
    )
    if park is None:
        return None, None
    why = park_reason(park.type, park.details)
    # A ceiling can park a session at its question as well as mid-turn.
    asked = why in (ParkReason.IDLE, ParkReason.CEILING)
    before = [row for row in stretch if row.id < park.id]
    spell = _spell(before) if asked else None
    # A finish sent since the spell went idle means the park is a finish nobody honoured: what was
    # unmet or asked then no longer says why it stopped.
    if spell is not None and any(
        row.type == EventType.AGENT_FINISHING and row.id > spell.id for row in before
    ):
        spell = None
    unmet = Unmet()
    if why == ParkReason.EXITED:
        unmet = Unmet.read(park.details)
    elif spell is not None:
        unmet = Unmet.read(spell.details)
    return None, TerminalParkedOut(
        reason=why,
        question=_text(spell.details.get("question")) if spell is not None else None,
        turn_error=_text(spell.details.get("turn_error")) if spell is not None else None,
        unmet=list(unmet.rules) or None,
        needs=list(unmet.words) or None,
        at=park.created_at,
    )


def _rollover(row: Event) -> bool:
    if row.type == EventType.AGENT_RESUMED:
        return row.details.get("via") == ResumedVia.ROLLOVER
    return row.type == EventType.AGENT_PARKED and row.details.get("reason") == ParkReason.ROLLOVER


def _spell(newest_first: list[Event]) -> Event | None:
    """The `agent_idle` of the stretch the rows end in, unless that stretch has none."""
    for row in newest_first:
        if row.type == EventType.AGENT_IDLE:
            return row
        if row.type in (EventType.AGENT_ACTIVE, EventType.AGENT_RESUMED):
            return None
    return None


def _stretch_start(newest_first: list[Event], run: StepRun) -> datetime | None:
    """When the active stretch the rows end in began, as `sessions._active_since` reads it."""
    for row in newest_first:
        if row.type == EventType.AGENT_ACTIVE:
            return aware_time(row.details.get("active_at")) or row.created_at
        if row.type == EventType.AGENT_RESUMED:
            return aware_time(row.details.get("resumed_at")) or row.created_at
    return run.started_at


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
