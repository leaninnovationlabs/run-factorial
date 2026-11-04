"""The factory board (design.md §5's read models, across an organization's projects at once),
and the refresh that wakes the whole fleet.

Cross-project on purpose: these are the questions about the factory rather than about a repo, so
they hang off the organization (`/orgs/{org}`, tech-stack decision 68), not `/projects/{id}`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.deps import OrganizationDep, SessionDep, organization_dependency
from orchestrator.api.schemas import (
    FleetRefreshOut,
    OverviewProjectOut,
    OverviewResponse,
    OverviewTicketOut,
)
from orchestrator.db.models import Project
from orchestrator.events import factory_overview, wake_fleet

router = APIRouter(tags=["overview"])

#: How long a finished ticket stays on the board. Done is the biggest lane on a healthy board, so
#: dropping closed tickets empties it — and keeping all of them swamps it within a month.
DONE_WINDOW_DAYS = 7


@router.get("/overview", response_model=OverviewResponse)
@needs(Permission.ORG_READ)
async def get_overview(
    session: SessionDep,
    organization: OrganizationDep,
    done_days: int = Query(DONE_WINDOW_DAYS, ge=0, le=365),
) -> OverviewResponse:
    projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.organization_id == organization.id)
                .order_by(Project.id)
            )
        )
        .scalars()
        .all()
    )
    tickets = await factory_overview(session, done_days, organization.id)
    return OverviewResponse(
        projects=[OverviewProjectOut(id=p.id, name=p.name, active=p.active) for p in projects],
        tickets=[OverviewTicketOut.model_validate(t) for t in tickets],
        done_days=done_days,
    )


@router.post(
    "/refresh",
    response_model=FleetRefreshOut,
    status_code=status.HTTP_202_ACCEPTED,
    # Resolved only so an organization that does not exist answers 404, as every route here does.
    dependencies=[Depends(organization_dependency)],
)
@needs(Permission.TICKETS_OPERATE)
async def refresh_fleet(session: SessionDep) -> FleetRefreshOut:
    """Ask the worker for a fleet pass now: admission, then every ticket (decision 4).

    The webhook's wake, sent from the dashboard, so an issue opened since the last tick is
    admitted without waiting out the interval. Fleet-wide, every organization's tickets included,
    because the tick is: a wake is only a signal (decision 4), so it leaks nothing. It records no
    event, like a ticket refresh, and it does not wait: a fleet pass leaves no mark to wait for,
    so the page's own polling shows what it did.
    """
    requested_at = await wake_fleet(session)
    await session.commit()
    return FleetRefreshOut(requested_at=requested_at)
