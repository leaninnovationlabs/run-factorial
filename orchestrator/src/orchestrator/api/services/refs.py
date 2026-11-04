"""Turn the refs a caller types into rows. A project is named by its name, by the repo it reads
issues from (`db.models.intake_repo`), or by `id:N`. A ticket is named by `owner/repo#N`, by `#N`
with a project, or by `id:N`. `id:N` is factorial's own id, the one a dashboard page names.

Every ref resolves within the caller's organization (design.md §2.11): an MCP token's, or an
assistant conversation's. A row in another organization is not found, never refused, so a caller
cannot learn that a name, a repo or an id exists elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.errors import Invalid, NotFound
from orchestrator.db.models import (
    Project,
    StepRun,
    Ticket,
    TicketSource,
    intake_column,
    intake_repo,
)
from orchestrator.events.read_models import latest_run

__all__ = [
    "IssueRef",
    "current_run",
    "resolve_issue",
    "resolve_project",
    "resolve_ticket",
    "ticket_source",
]

_REPO_ISSUE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>[0-9]+)$")
_NUMBER = re.compile(r"^#?(?P<number>[0-9]+)$")
# Explicit, so an all-digit issue number or project name is never read as a row id. Eighteen
# digits always fit the bigint key, so a longer id is a miss rather than a database error.
_ROW_ID = re.compile(r"^id:(?P<id>[0-9]{1,18})$")
_FORMS = "owner/repo#N, #N with a project, or id:N"


@dataclass(frozen=True)
class IssueRef:
    """An issue a caller named, admitted or not: the project that reads it and where it lives."""

    project: Project
    repo: str
    number: int


async def ticket_source(session: AsyncSession, project_id: int) -> TicketSource | None:
    """The project's one ticket source: `ticket_source.project_id` is unique."""
    return (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project_id))
    ).scalar_one_or_none()


async def resolve_project(session: AsyncSession, organization_id: int, ref: str) -> Project:
    """A project of the organization, by its exact name, by `id:N`, or by the repo it reads issues
    from (case-insensitive).

    A repo that two projects read from is refused instead of guessed. Either guess would act on a
    project the caller did not mean.
    """
    named = (
        await session.execute(
            select(Project).where(Project.organization_id == organization_id, Project.name == ref)
        )
    ).scalar_one_or_none()
    if named is not None:
        return named
    by_id = _ROW_ID.match(ref)
    if by_id:
        found = (
            await session.execute(
                select(Project).where(
                    Project.organization_id == organization_id, Project.id == int(by_id["id"])
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
    elif "/" in ref:
        matches = list(
            (
                await session.execute(
                    select(Project)
                    .outerjoin(TicketSource, TicketSource.project_id == Project.id)
                    .where(
                        Project.organization_id == organization_id,
                        func.lower(intake_column()) == ref.lower(),
                    )
                    .order_by(Project.name)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise Invalid(
                f"{ref} is the intake repository of {len(matches)} projects.",
                hint="Name the project instead.",
                candidates=[project.name for project in matches],
            )
    names = list(
        (
            await session.execute(
                select(Project.name)
                .where(Project.organization_id == organization_id)
                .order_by(Project.name)
            )
        )
        .scalars()
        .all()
    )
    raise NotFound(
        f"No project has ID {by_id['id']}."
        if by_id
        else f"No project is named {ref} or reads issues from it.",
        # A bare number is most likely the id a dashboard page shows, but read as one it could
        # act on a project the caller did not mean, so it is refused with the id's form.
        tool_hint=f"Pass id:{ref} for the project with ID {ref}."
        if _ROW_ID.match(f"id:{ref}")
        else "Call factorial_projects_list to list them.",
        candidates=names[:20],
    )


async def _same_project(
    session: AsyncSession, organization_id: int, owner: Project, project: str | None, ref: str
) -> None:
    if (
        project is not None
        and (await resolve_project(session, organization_id, project)).id != owner.id
    ):
        raise Invalid(
            f"{ref} is in project {owner.name}, not {project}.",
            tool_hint="Drop the project argument or correct it.",
        )


async def _ticket_by_id(
    session: AsyncSession, organization_id: int, ticket_id: int, project: str | None
) -> Ticket:
    row = (
        await session.execute(
            select(Ticket)
            .join(Project, Project.id == Ticket.project_id)
            .where(Ticket.id == ticket_id, Project.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"No ticket has ID {ticket_id}.")
    owner = (
        await session.execute(select(Project).where(Project.id == row.project_id))
    ).scalar_one()
    await _same_project(session, organization_id, owner, project, f"id:{ticket_id}")
    return row


async def resolve_issue(
    session: AsyncSession, organization_id: int, ticket: str, project: str | None
) -> IssueRef:
    """The issue a ref names, admitted or not. `#N` needs `project`. `owner/repo#N` takes the
    project from the repo, or with `project` checks that project reads issues from that repo:
    a repo several projects read from needs `project` to say which."""
    ref = ticket.strip()
    if by_id := _ROW_ID.match(ref):
        row = await _ticket_by_id(session, organization_id, int(by_id["id"]), project)
        if not row.external_key.isdigit():
            raise Invalid(f"Ticket {ref} has no issue number (key {row.external_key!r}).")
        owner = (
            await session.execute(select(Project).where(Project.id == row.project_id))
        ).scalar_one()
        return IssueRef(
            owner, intake_repo(owner, await ticket_source(session, owner.id)), int(row.external_key)
        )
    if repo_issue := _REPO_ISSUE.match(ref):
        if project is None:
            owner = await resolve_project(session, organization_id, repo_issue["repo"])
            repo = intake_repo(owner, await ticket_source(session, owner.id))
        else:
            owner = await resolve_project(session, organization_id, project)
            repo = intake_repo(owner, await ticket_source(session, owner.id))
            if repo.lower() != repo_issue["repo"].lower():
                raise Invalid(
                    f"{ref} is not in project {owner.name}, which reads issues from {repo}.",
                    tool_hint="Drop the project argument or correct it.",
                )
        return IssueRef(owner, repo, int(repo_issue["number"]))
    if number := _NUMBER.match(ref):
        if project is None:
            raise Invalid(f"{ref} needs a project.", tool_hint=f"Pass project, or use {_FORMS}.")
        owner = await resolve_project(session, organization_id, project)
        return IssueRef(
            owner, intake_repo(owner, await ticket_source(session, owner.id)), int(number["number"])
        )
    raise Invalid(f"Factorial can't read the ticket ref {ticket!r}.", tool_hint=f"Use {_FORMS}.")


async def resolve_ticket(
    session: AsyncSession, organization_id: int, ticket: str, project: str | None
) -> Ticket:
    """The admitted ticket a ref names. The project's single ticket source makes
    `(project_id, external_key)` unique, because `(ticket_source_id, external_key)` is."""
    ref = ticket.strip()
    if by_id := _ROW_ID.match(ref):
        return await _ticket_by_id(session, organization_id, int(by_id["id"]), project)
    issue = await resolve_issue(session, organization_id, ref, project)
    row = (
        await session.execute(
            select(Ticket).where(
                Ticket.project_id == issue.project.id,
                Ticket.external_key == str(issue.number),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(
            f"{issue.repo}#{issue.number} is not a ticket.",
            tool_hint="Call factorial_ticket_get to see why it is not admitted yet.",
        )
    return row


async def current_run(session: AsyncSession, ticket: Ticket) -> StepRun | None:
    """The run the ticket page shows: `latest_run`'s rule, the highest attempt at the step."""
    current = select(latest_run(StepRun.id)).where(Ticket.id == ticket.id).scalar_subquery()
    return await session.scalar(select(StepRun).where(StepRun.id == current))
