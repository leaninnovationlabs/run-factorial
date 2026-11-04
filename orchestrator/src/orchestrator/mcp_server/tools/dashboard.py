"""The project dashboard's figures, for an operator's MCP client (tech-stack decision 61)."""

from __future__ import annotations

from typing import Annotated

from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import Field

from orchestrator.api.schemas import ProjectDashboardOut, Span
from orchestrator.api.services.dashboard import project_dashboard
from orchestrator.api.services.refs import resolve_project
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import respond
from orchestrator.mcp_server.tokens import READ_SCOPE
from orchestrator.mcp_server.tools.params import ProjectArg

__all__ = ["TOOLS"]


@tool(READ_SCOPE, title="Project dashboard", output=ProjectDashboardOut, open_world=False)
async def factorial_dashboard_get(
    project: ProjectArg,
    span: Annotated[
        Span,
        Field(
            description="4w or 12w: whole UTC weeks back, clipped to the first ticket; all: the "
            "project's whole history. Default all."
        ),
    ] = "all",
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """The project dashboard's figures over span (4w/12w: whole UTC weeks back, clipped to
    the first ticket; all: whole history): period and previous (spend, runs, tickets
    done/exited/admitted, cost per done ticket, velocity per week, median lead time), time_split
    (agent, gate, paused, idle seconds), spend by outcome, costliest tickets with each attempt,
    step and gate median/p90/max seconds, unmerged spend, weeks, callouts. open_now and
    open_gates are now; lifetime is all history. Money is a decimal string; cost_estimated marks
    any list-priced run."""
    found = await resolve_project(tc.session, tc.organization_id, project)
    out = await project_dashboard(tc.session, found.id, span=span)
    tickets, period = out.lifetime.tickets, out.period
    return respond(
        out,
        f"project {found.name}: {tickets.done} done, {tickets.open} open, {tickets.exited} "
        f"exited; {out.lifetime.runs.total} runs, ${out.lifetime.spend.cost_usd} lifetime; "
        f"span {span}: {period.tickets.done} done, ${period.spend.cost_usd}",
    )


TOOLS: list[ToolSpec] = [factorial_dashboard_get]
