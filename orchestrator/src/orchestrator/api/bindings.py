"""Which Runtime each project's process binds — one rule, read by three routes.

The projects list's Runtime column, the runtimes list's connected count and a runtime's own page
all come from `process_bindings`, so no two of them can disagree. The rule is the one readiness
applies (`factorial.registry.latest_process_versions` + `config.runtime`): a project is bound by
the `runtime:` the latest version of each AVAILABLE lane names — the version a ticket entering
that lane now would pin — and by nothing an older version, a retired lane, or an in-flight
ticket says. One row per lane, so a project running several processes binds each runtime they
name; a reader that wants one runtime per project picks the default lane's.

A slug with no Runtime row is still a binding as far as the project is concerned (the process
names it, and the projects list shows it) but binds no runtime: readiness calls that row
"no longer exists", and a count that credited it would name a runtime that is not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import ProcessVersion, Project, Runtime


@dataclass(frozen=True)
class Binding:
    project_id: int
    project_name: str
    github_repo: str
    #: Which lane names it. A project has one binding per available lane that names a runtime.
    process_key: str
    #: What the latest snapshot's `runtime:` says, whether or not a row carries it.
    slug: str
    #: The row with that slug; both None when it was deleted after the sync that accepted it.
    runtime_id: int | None
    runtime_name: str | None


async def process_bindings(
    session: AsyncSession, *, runtime_ids: Sequence[int] | None = None
) -> list[Binding]:
    """Every lane whose latest process version names a runtime, in project-id then lane order.
    One statement however many projects: DISTINCT ON picks each lane's current version and the
    outer join says whether the slug still has a row. `runtime_ids` narrows to lanes bound to
    those rows, which drops the rowless slugs by construction."""
    latest = (
        select(
            ProcessVersion.project_id,
            ProcessVersion.process_key,
            ProcessVersion.content["runtime"].astext.label("slug"),
        )
        .where(ProcessVersion.retired_at.is_(None))
        .distinct(ProcessVersion.project_id, ProcessVersion.process_key)
        # `factorial.registry.latest_process_versions`'s rule, as DISTINCT ON across projects:
        # most recently synced wins.
        .order_by(
            ProcessVersion.project_id,
            ProcessVersion.process_key,
            ProcessVersion.activated_at.desc(),
            ProcessVersion.id.desc(),
        )
        .subquery()
    )
    query = (
        select(
            Project.id,
            Project.name,
            Project.github_repo,
            latest.c.process_key,
            latest.c.slug,
            Runtime.id,
            Runtime.name,
        )
        .join(latest, latest.c.project_id == Project.id)
        .outerjoin(
            Runtime,
            (Runtime.organization_id == Project.organization_id) & (Runtime.slug == latest.c.slug),
        )
        .where(latest.c.slug.is_not(None))
        .order_by(Project.id, latest.c.process_key)
    )
    if runtime_ids is not None:
        query = query.where(Runtime.id.in_(runtime_ids))
    return [
        Binding(
            project_id=project_id,
            project_name=project_name,
            github_repo=github_repo,
            process_key=process_key,
            slug=slug,
            runtime_id=runtime_id,
            runtime_name=runtime_name,
        )
        for (
            project_id,
            project_name,
            github_repo,
            process_key,
            slug,
            runtime_id,
            runtime_name,
        ) in (await session.execute(query)).all()
    ]
