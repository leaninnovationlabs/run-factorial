"""Project reads shared by the dashboard routes and the MCP tools.

Moved out of `api/routes/admin.py` because a tool calls them; what only a route needs stays there.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.registry import display_of
from orchestrator.api.bindings import Binding, process_bindings
from orchestrator.api.readiness import (
    blockers,
    core_items,
    default_or_note,
    github_standing,
    named_runtimes,
    parse_snapshots,
    probe_keys,
    process_summaries,
    runtime_items,
    secret_gap,
)
from orchestrator.api.schemas import (
    ForgeDisplayOut,
    ProjectDetail,
    SecretDeliveryOut,
    TicketSourceOut,
)
from orchestrator.api.services.refs import ticket_source
from orchestrator.db.models import ProcessVersion, Project, Ticket, TicketStatus
from orchestrator.events.read_models import blocked_clause
from orchestrator.factorial import RepoConfigSource
from orchestrator.factorial.registry import adopters, latest_process_versions
from orchestrator.github.connection import CONNECTION_SECRETS
from orchestrator.secrets import store


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    """One project as both lists draw it. The dashboard's row reads `project` and `runtime`; the
    MCP tool reads the lanes and the counts."""

    project: Project
    default_process: str | None
    #: Available lanes in key order; retired ones are absent (`latest_process_versions`).
    lanes: tuple[str, ...]
    #: The default lane's binding, else the first lane's; None when no lane names a runtime.
    runtime: Binding | None
    open_tickets: int
    #: Counted with `blocked_clause`, so it matches `select_tickets(state="blocked")`.
    blocked_tickets: int


async def list_projects(session: AsyncSession, organization_id: int) -> list[ProjectSummary]:
    """The organization's projects in id order, in four statements however many projects there
    are.

    One runtime per row, from the default lane — the same choice `readiness._process_summary`
    makes for the checklist. A project running several lanes binds one runtime per lane, and a
    list column that named all of them would be a count rather than something to read.
    """
    projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.organization_id == organization_id)
                .order_by(Project.id)
            )
        )
        .scalars()
        .all()
    )
    bound: dict[int, list[Binding]] = {}
    for binding in await process_bindings(session):
        bound.setdefault(binding.project_id, []).append(binding)
    lanes: dict[int, list[str]] = {}
    lane_rows = await session.execute(
        select(ProcessVersion.project_id, ProcessVersion.process_key)
        .where(ProcessVersion.retired_at.is_(None))
        .distinct()
        .order_by(ProcessVersion.project_id, ProcessVersion.process_key)
    )
    for project_id, process_key in lane_rows.all():
        lanes.setdefault(project_id, []).append(process_key)
    count_rows = await session.execute(
        select(
            Ticket.project_id,
            func.count(Ticket.id).filter(Ticket.closed_at.is_(None)),
            func.count(Ticket.id).filter(blocked_clause()),
        ).group_by(Ticket.project_id)
    )
    counts = {int(r[0]): (int(r[1]), int(r[2])) for r in count_rows.all()}

    summaries: list[ProjectSummary] = []
    for project in projects:
        default, _ = default_or_note(project)
        bindings = bound.get(project.id, [])
        open_tickets, blocked_tickets = counts.get(project.id, (0, 0))
        summaries.append(
            ProjectSummary(
                project=project,
                default_process=default,
                lanes=tuple(lanes.get(project.id, [])),
                runtime=next(
                    (b for b in bindings if b.process_key == default),
                    bindings[0] if bindings else None,
                ),
                open_tickets=open_tickets,
                blocked_tickets=blocked_tickets,
            )
        )
    return summaries


async def project_detail(
    session: AsyncSession,
    project: Project,
    *,
    config_source: RepoConfigSource | None,
    webhook_reachable: bool = True,
) -> ProjectDetail:
    """One project with everything that needs no live GitHub state.

    `secret_gap` still reads the pinned agent manifests, which is a GitHub read. It is memoized
    on the immutable sha it reads at, so it costs round trips once per synced commit and nothing
    afterwards. Its failures degrade to `warnings`, so an outage never bricks this read.
    """
    source = await ticket_source(session, project.id)
    default, default_note = default_or_note(project)
    versions = list((await latest_process_versions(session, project.id)).values())
    configs, parse_notes = parse_snapshots(versions)
    runtimes = await named_runtimes(session, project.organization_id, configs)
    gap = await secret_gap(session, config_source, project, configs, runtimes)
    runtime_rows = await runtime_items(session, configs, runtimes, project.settings)
    github = await github_standing(session, project, source, webhook_reachable=webhook_reachable)

    counts = (
        await session.execute(
            select(
                func.count(Ticket.id),
                func.count(Ticket.id).filter(Ticket.closed_at.is_(None)),
                func.count(Ticket.id).filter(Ticket.status == TicketStatus.EXITED),
            ).where(Ticket.project_id == project.id)
        )
    ).one()

    return ProjectDetail(
        id=project.id,
        organization=project.organization_slug,
        name=project.name,
        github_repo=project.github_repo,
        description=project.description,
        settings=project.settings,
        active=project.active,
        created_at=project.created_at,
        forge=ForgeDisplayOut(
            **display_of(project, source.kind if source else None).payload()
        ),
        ticket_source=TicketSourceOut.model_validate(source) if source else None,
        processes=process_summaries(
            versions, default=default, adopted=await adopters(session, project.id)
        ),
        default_process=default,
        ticket_count=int(counts[0]),
        open_ticket_count=int(counts[1]),
        exited_ticket_count=int(counts[2]),
        secret_names=[
            n for n in await store.list_names(session, project.id) if n not in CONNECTION_SECRETS
        ],
        declared_secrets=gap.declared,
        runtime_provided_secrets=gap.runtime_provided,
        secret_deliveries=[
            SecretDeliveryOut(name=name, mode=d.mode, host=d.host, header=d.header)
            for name, d in (await store.deliveries(session, project.id)).items()
            if name not in CONNECTION_SECRETS
        ],
        blockers=blockers(source, versions, gap.missing, default, github),
        warnings=parse_notes
        + gap.notes
        + ([default_note] if default_note else [])
        + [r.detail for r in runtime_rows if r.detail],
        readiness=[
            *core_items(source, versions, gap.missing, default, github),
            *runtime_rows,
        ],
        probe_keys=probe_keys(configs),
    )
