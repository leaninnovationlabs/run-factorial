"""Live pull-request lookup for a ticket waiting on a human gate.

The timeline records whichever MR the gate opened on; the worker re-resolves the pick every
tick (`worker/templates._find_pr`). This read gives the dashboard the same live answer for
links and banners without waiting for a new `gate_waiting` event.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import GateStep, ProcessConfig
from orchestrator.db.models import (
    ProcessVersion,
    Project,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.github import GitHubStateSource, StateSource
from orchestrator.github.state import PullRequestState
from orchestrator.gitlab.observer import GitLabStateSource
from orchestrator.gitlab.project_client import client_for_project
from orchestrator.worker.templates import _find_pr, _glob, _resolve_slug

__all__ = ["observed_gate_pull_request"]


async def observed_gate_pull_request(
    session: AsyncSession,
    ticket: Ticket,
    project: Project,
    *,
    github_client: object | None,
) -> PullRequestState | None:
    if ticket.status != TicketStatus.WAITING_GATE or not ticket.external_key.isdigit():
        return None
    content = (
        await session.execute(
            select(ProcessVersion.content).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    config = ProcessConfig.model_validate(content)
    step = config.step(ticket.current_step)
    if not isinstance(step, GateStep):
        return None

    source_row = await session.get(TicketSource, ticket.ticket_source_id)
    kind = source_row.kind if source_row is not None else TicketSourceKind.GITHUB
    issue_number = int(ticket.external_key)

    state: StateSource
    if kind == TicketSourceKind.GITLAB:
        client = await client_for_project(session, project)
        if client is None:
            return None
        state = GitLabStateSource(client, project.github_repo, issue_number)
    else:
        if github_client is None:
            return None
        state = GitHubStateSource(github_client, project.github_repo, issue_number)  # type: ignore[arg-type]

    values = {
        "ticket": ticket.external_key,
        "ticket_owner": ticket.owner_at_admission,
        "repo": project.github_repo,
    }
    slug = await _resolve_slug(session, ticket, config, values)
    if slug is not None:
        values["slug"] = slug

    head = _glob(step.gate.pr.head, values)
    base = _glob(step.gate.pr.base, values)
    return _find_pr(await state.pull_requests(), head, base)
