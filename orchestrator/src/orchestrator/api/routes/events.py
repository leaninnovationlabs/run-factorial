"""The audit feed, for operators over Bruno or curl (bootstrap.md).

A keyset cursor rather than a stream (SSE and websockets are deferred): the client holds the last
`id` it saw and asks for what came after. `id` is the log's total order, which makes "exactly
once, in order, resumable after a reload" fall out of the query with no server-side state.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.deps import SessionDep
from orchestrator.api.schemas import EventFeed, EventOut
from orchestrator.db.models import Ticket
from orchestrator.events import recent_events

router = APIRouter(tags=["events"])

#: A poller asking for more than this is asking for a backfill; make it page instead.
MAX_LIMIT = 500


@router.get("/projects/{project_id}/events", response_model=EventFeed)
@needs(Permission.ORG_READ)
async def get_events(
    project_id: int,
    session: SessionDep,
    after_id: Annotated[int | None, Query(ge=0, description="return events with id > this")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    ticket: Annotated[
        str | None,
        Query(description="external key (the issue number): only this ticket's events"),
    ] = None,
) -> EventFeed:
    ticket_id: int | None = None
    if ticket is not None:
        # The client holds the issue number, never the internal id, so the filter takes the
        # external key. An unknown key is a 404 rather than an empty feed: nothing for a typo'd
        # number reads exactly like "this ticket has no events yet".
        ticket_id = (
            await session.execute(
                select(Ticket.id).where(
                    Ticket.project_id == project_id, Ticket.external_key == ticket
                )
            )
        ).scalar_one_or_none()
        if ticket_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project {project_id} has no ticket with external key {ticket!r}.",
            )
    rows = await recent_events(
        session, project_id, limit=limit, after_id=after_id, ticket_id=ticket_id
    )
    return EventFeed(
        project_id=project_id,
        ticket=ticket,
        events=[EventOut.model_validate(row) for row in rows],
        # None on an empty page: the client keeps the cursor it already had.
        next_after_id=rows[-1].id if rows else None,
    )
