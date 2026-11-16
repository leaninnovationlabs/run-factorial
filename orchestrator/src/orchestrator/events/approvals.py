"""A person's approval of a step run in the dashboard, as `request_approval()` reads it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event
from orchestrator.events.read_models import latest_run_event
from orchestrator.events.types import EventType

__all__ = ["Approval", "latest_approval"]


@dataclass(frozen=True)
class Approval:
    #: The `step_approved` event's id: what a parked re-check remembers it has read.
    id: int
    head_sha: str
    approved_by: str
    at: datetime


async def latest_approval(session: AsyncSession, ticket_id: int, run_id: int) -> Approval | None:
    """The newest `step_approved` of the run, None when there is none or it names no commit."""
    row = (
        await session.execute(
            latest_run_event(
                ticket_id,
                run_id,
                (EventType.STEP_APPROVED,),
                Event.id,
                Event.details,
                Event.created_at,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    event_id, details, at = row
    head_sha, approved_by = details.get("head_sha"), details.get("approved_by")
    if not isinstance(head_sha, str) or not head_sha or not isinstance(approved_by, str):
        return None
    return Approval(id=event_id, head_sha=head_sha, approved_by=approved_by, at=at)
