"""The project dashboard: one project-scoped read composed from the read models (tech-stack
decision 61).

One request rather than a route per section, for the reason decision 27 gives the factory board:
the page is one screen. The lists are cut at the constants below, so the answer is bounded by them
and never by how many runs a project has. The MCP twin (`mcp_server/tools/dashboard.py`) calls
this same function, so the two can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.schemas import (
    DashboardLifetimeOut,
    DashboardLimitsOut,
    ExtremesOut,
    LeadTimeOut,
    OpenGateOut,
    OutcomeCountsOut,
    OutcomeSpendOut,
    PerDoneOut,
    PeriodFiguresOut,
    PeriodTicketsOut,
    ProjectDashboardOut,
    ProjectSpendOut,
    RunCountsOut,
    Span,
    SpendFoldOut,
    StepSegmentOut,
    StepTimeOut,
    TicketSpendOut,
    TicketStepCostsOut,
    TimeSplitOut,
    UnmergedSpendOut,
    WeekOut,
    WindowSpendOut,
)
from orchestrator.events.read_models import (
    P90_MIN_RUNS,
    PeriodFigures,
    done_ticket_cost,
    first_admission,
    open_gates,
    open_ticket_count,
    period_figures,
    project_spend,
    run_extremes,
    spend_by_outcome,
    spend_by_ticket,
    step_medians,
    ticket_step_costs,
    time_split,
    unmerged_spend,
    week_summaries,
)

__all__ = ["WindowBounds", "project_dashboard", "window_bounds"]

#: Whole UTC weeks before the current one (`window_bounds`). `all` has no cut: the sections read
#: it as `since=None`, and the window starts at the project's first admission.
SPANS: Final[dict[str, int | None]] = {"4w": 4, "12w": 12, "all": None}
#: Where the lists are cut. Served back as `limits`, so the page can say "top 12 of 17".
TOP_TICKETS: Final = 5
TOP_STEP_COSTS: Final = 5
OPEN_GATES: Final = 5


@dataclass(frozen=True)
class WindowBounds:
    """Where the window starts, and where the period of the same length before it starts."""

    since: datetime
    previous_since: datetime | None
    #: The span reached back before the first ticket, so `since` is the first admission.
    clipped: bool


def window_bounds(span: Span, as_of: datetime, first_admitted_at: datetime | None) -> WindowBounds:
    """`SPANS[span]` whole UTC weeks back from Monday 00:00 of `as_of`'s week, so the current week
    is the partial one; `all` starts at the first admission. A window is clipped to the first
    admission, and a project with no tickets gets `since = as_of`.

    The previous period, `[since - (as_of - since), since)`, exists only when it lies wholly
    inside the project's life: a change against a part-period would read as growth.
    """
    if first_admitted_at is None:
        return WindowBounds(since=as_of, previous_since=None, clipped=False)
    weeks = SPANS[span]
    if weeks is None:
        return WindowBounds(since=first_admitted_at, previous_since=None, clipped=False)
    today = as_of.astimezone(UTC)
    week0 = datetime(today.year, today.month, today.day, tzinfo=UTC) - timedelta(
        days=today.weekday()
    )
    since = week0 - timedelta(weeks=weeks)
    if since < first_admitted_at:
        return WindowBounds(since=first_admitted_at, previous_since=None, clipped=True)
    previous = since - (as_of - since)
    return WindowBounds(
        since=since,
        previous_since=previous if previous >= first_admitted_at else None,
        clipped=False,
    )


def _period_out(figures: PeriodFigures) -> PeriodFiguresOut:
    return PeriodFiguresOut(
        spend=WindowSpendOut.model_validate(figures.spend),
        runs=RunCountsOut.model_validate(figures.runs),
        tickets=PeriodTicketsOut(
            done=figures.tickets_done,
            exited=figures.tickets_exited,
            admitted=figures.tickets_admitted,
        ),
        per_done=PerDoneOut.model_validate(figures.per_done),
        velocity_per_week=figures.velocity_per_week,
        lead_time=LeadTimeOut(
            tickets=figures.lead_time_tickets,
            median_seconds=figures.lead_time_median_seconds,
        ),
        forced_steps=figures.forced_steps,
    )


async def project_dashboard(
    session: AsyncSession, project_id: int, *, span: Span
) -> ProjectDashboardOut:
    """Every figure the page draws. The read models do the arithmetic and the cuts; this sets
    the window and the cuts and shapes the answer.

    An unknown project answers an empty dashboard rather than a 404, as the board and the cost
    report it sits beside do: the page already fetched the project, and every aggregate here is
    well defined over no rows.
    """
    as_of = datetime.now(UTC)
    bounds = window_bounds(span, as_of, await first_admission(session, project_id))
    since = None if span == "all" else bounds.since

    spend = await project_spend(session, project_id)
    per_done = await done_ticket_cost(session, project_id, since=None)
    outcomes = await spend_by_outcome(session, project_id, since=since)
    # Under a span its ticket counts are the tickets that ran in it; the hero needs every ticket.
    lifetime_outcomes = (
        outcomes if since is None else await spend_by_outcome(session, project_id, since=None)
    )
    tickets = await spend_by_ticket(session, project_id, since=since, limit=TOP_TICKETS)
    detailed = tickets.top[:TOP_STEP_COSTS]
    segments = await ticket_step_costs(session, [t.ticket_id for t in detailed], since=since)
    unmerged = await unmerged_spend(session, project_id, since=since)
    extremes = await run_extremes(session, project_id, since=since)
    weeks = await week_summaries(session, project_id, since=bounds.since, until=as_of)
    step_times, gate_waits = await step_medians(session, project_id, since=bounds.since)
    period = await period_figures(session, project_id, since=bounds.since, until=as_of)
    previous = (
        None
        if bounds.previous_since is None
        else await period_figures(
            session, project_id, since=bounds.previous_since, until=bounds.since
        )
    )
    split = await time_split(session, project_id, since=bounds.since, until=as_of)
    waiting = [
        OpenGateOut.model_validate(gate)
        for gate in await open_gates(session, project_id, limit=OPEN_GATES)
    ]
    open_now = await open_ticket_count(session, project_id)

    counts = {row.outcome: row.tickets for row in lifetime_outcomes}
    by_ticket: dict[int, list[StepSegmentOut]] = {t.ticket_id: [] for t in detailed}
    for segment in segments:
        by_ticket[segment.ticket_id].append(StepSegmentOut.model_validate(segment))

    return ProjectDashboardOut(
        project_id=project_id,
        span=span,
        since=bounds.since,
        as_of=as_of,
        clipped=bounds.clipped,
        previous_since=bounds.previous_since,
        lifetime=DashboardLifetimeOut(
            spend=ProjectSpendOut.model_validate(spend),
            tickets=OutcomeCountsOut(
                done=counts.get("done", 0),
                exited=counts.get("exited", 0),
                open=counts.get("open", 0),
                total=sum(counts.values()),
            ),
            per_done=PerDoneOut.model_validate(per_done),
            runs=RunCountsOut(
                total=spend.runs,
                priced=spend.priced_runs,
                estimated=spend.estimated_runs,
                subscription=spend.subscription_runs,
                cached_input_tokens=spend.cached_input_tokens,
            ),
        ),
        period=_period_out(period),
        previous=None if previous is None else _period_out(previous),
        open_now=open_now,
        time_split=TimeSplitOut.model_validate(split),
        open_gates=waiting,
        by_outcome=[OutcomeSpendOut.model_validate(row) for row in outcomes],
        by_ticket=[TicketSpendOut.model_validate(t) for t in tickets.top],
        by_ticket_rest=None if tickets.rest is None else SpendFoldOut.model_validate(tickets.rest),
        unrun_tickets=tickets.unrun,
        step_costs=[
            TicketStepCostsOut(
                ticket_id=t.ticket_id,
                external_key=t.external_key,
                title=t.title,
                cost_usd=t.cost_usd,
                cost_estimated=t.cost_estimated,
                segments=by_ticket[t.ticket_id],
            )
            for t in detailed
        ],
        steps=[StepTimeOut.model_validate(row) for row in step_times],
        gates=[StepTimeOut.model_validate(row) for row in gate_waits],
        unmerged=UnmergedSpendOut.model_validate(unmerged),
        weeks=[WeekOut.model_validate(week) for week in weeks],
        extremes=ExtremesOut.model_validate(extremes).model_copy(
            update={"longest_open_wait": waiting[0] if waiting else None}
        ),
        limits=DashboardLimitsOut(
            by_ticket=TOP_TICKETS,
            step_costs=TOP_STEP_COSTS,
            open_gates=OPEN_GATES,
            p90_min_runs=P90_MIN_RUNS,
        ),
    )
