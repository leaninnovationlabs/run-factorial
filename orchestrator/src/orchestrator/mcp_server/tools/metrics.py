"""Read tools over delivery: velocity and cost per project, and the closed config vocabulary."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.schemas import VocabularyOut
from orchestrator.api.services.refs import resolve_project
from orchestrator.api.services.vocabulary import config_vocabulary
from orchestrator.db.models import Project
from orchestrator.events.read_models import DeliveryMetrics, StepTime, delivery_metrics
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import respond
from orchestrator.mcp_server.tokens import READ_SCOPE
from orchestrator.mcp_server.tools.params import OptionalProjectArg


class WeekOut(BaseModel):
    week_start: date
    done: int
    exited: int


class StepTimeOut(BaseModel):
    process: str
    step: str
    kind: str
    runs: int
    median_minutes: float


class WindowSpendOut(BaseModel):
    cost_usd: float
    cost_estimated: bool
    input_tokens: int
    output_tokens: int


class ProjectMetricsOut(BaseModel):
    project: str
    weeks: list[WeekOut]
    done: int
    exited: int
    per_step: list[StepTimeOut]
    gate_wait: list[StepTimeOut]
    cost_per_done_ticket_usd: float | None = None
    done_tokens: int
    #: Whether any run behind the done tickets' cost was priced at list rather than measured.
    done_cost_estimated: bool
    spend: WindowSpendOut


class MetricsOut(BaseModel):
    since: datetime
    until: datetime
    projects: list[ProjectMetricsOut]


def _step(row: StepTime) -> StepTimeOut:
    return StepTimeOut(
        process=row.process_key,
        step=row.step,
        kind=row.kind,
        runs=row.runs,
        median_minutes=round(row.median_seconds / 60, 2),
    )


def _project_out(project: Project, metrics: DeliveryMetrics) -> ProjectMetricsOut:
    return ProjectMetricsOut(
        project=project.name,
        weeks=[
            WeekOut(week_start=week.week_start, done=week.done, exited=week.exited)
            for week in metrics.weeks
        ],
        done=metrics.done,
        exited=metrics.exited,
        per_step=[_step(row) for row in metrics.step_times],
        gate_wait=[_step(row) for row in metrics.gate_waits],
        cost_per_done_ticket_usd=(
            float(metrics.cost_per_done_usd) if metrics.cost_per_done_usd is not None else None
        ),
        done_tokens=metrics.done_tokens,
        done_cost_estimated=metrics.cost_estimated,
        spend=WindowSpendOut(
            cost_usd=float(metrics.spend.cost_usd),
            cost_estimated=metrics.spend.cost_estimated,
            input_tokens=metrics.spend.input_tokens,
            output_tokens=metrics.spend.output_tokens,
        ),
    )


async def _projects(session: AsyncSession, organization_id: int, ref: str | None) -> list[Project]:
    if ref is not None:
        return [await resolve_project(session, organization_id, ref)]
    return list(
        (
            await session.scalars(
                select(Project)
                .where(Project.organization_id == organization_id)
                .order_by(Project.name)
            )
        ).all()
    )


@tool(READ_SCOPE, title="Delivery metrics", output=MetricsOut, open_world=False)
async def factorial_metrics_get(
    project: OptionalProjectArg = None,
    window_days: Annotated[int, Field(ge=7, le=365, description="Days back from now.")] = 28,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Velocity and cost per project over the last window_days: tickets done and exited per UTC
    week (a ticket that exited and came back still counts), the median run time of each step,
    the median gate wait from opening to decision, the lifetime cost and tokens of the tickets
    done in the window, and the spend of the runs that ended in it or, still live, started in it.
    Times are active time, without a run's paused time. Omit project for every project."""
    until = datetime.now(UTC)
    since = until - timedelta(days=window_days)
    rows = [
        _project_out(row, await delivery_metrics(tc.session, row.id, since=since))
        for row in await _projects(tc.session, tc.organization_id, project)
    ]
    done = sum(row.done for row in rows)
    exited = sum(row.exited for row in rows)
    return respond(
        MetricsOut(since=since, until=until, projects=rows),
        f"{len(rows)} project(s) over {window_days} days: {done} done, {exited} exited.",
    )


@tool(READ_SCOPE, title="Config vocabulary", output=VocabularyOut, open_world=False)
async def factorial_vocabulary_get() -> ToolResult:
    """The closed library process config may reference: predicates with their argument counts,
    actions, placeholders, input sources and the all/any/not compositions. Config can only name
    these; a new one is a code change."""
    out = config_vocabulary()
    return respond(
        out,
        f"{len(out.predicates)} predicates, {len(out.actions)} actions, "
        f"{len(out.placeholders)} placeholders.",
    )


TOOLS: list[ToolSpec] = [factorial_metrics_get, factorial_vocabulary_get]
