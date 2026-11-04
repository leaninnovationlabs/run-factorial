"""The project dashboard's one read (tech-stack decision 61).

Project-scoped like the board and the cost report beside it; the composition lives in
`api/services/dashboard.py` so the MCP twin shares it without going through HTTP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.deps import SessionDep
from orchestrator.api.schemas import ProjectDashboardOut, Span
from orchestrator.api.services.dashboard import project_dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/projects/{project_id}/dashboard", response_model=ProjectDashboardOut)
@needs(Permission.ORG_READ)
async def get_dashboard(
    project_id: int,
    session: SessionDep,
    span: Annotated[
        Span,
        Query(
            description="4w or 12w: whole UTC weeks back from this one, clipped to the first "
            "ticket; all: the project's whole history. Every card and section follows it."
        ),
    ] = "all",
) -> ProjectDashboardOut:
    """Every figure the page draws, over `span` except `lifetime` and the NOW figures
    (`open_now`, `open_gates`). An unknown `span` is a 422, as an unknown ticket status is on
    the ticket list."""
    return await project_dashboard(session, project_id, span=span)
