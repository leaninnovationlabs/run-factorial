"""Adopting a `factorial` head the sync guard cannot account for (tech-stack decision 53).

A direct push leaves a head no event names and no human merged. An operator reads what it
changed against the last known commit and vouches for exactly that head; the event the
adoption records is what the guard then accepts. Dashboard only: over MCP a model that read
injected text could vouch for an agent's push.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from githubkit.exception import RequestFailed
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.config import SyncedConfig, sync_config
from orchestrator.api.services.errors import (
    Conflict,
    Forbidden,
    Invalid,
    ServiceError,
    UpstreamFailed,
)
from orchestrator.api.services.refs import ticket_source
from orchestrator.db.models import Project, TicketSourceKind
from orchestrator.events import EventType, append
from orchestrator.events.types import RequestedVia
from orchestrator.factorial import CONFIG_BRANCH, RepoConfigSource
from orchestrator.factorial.registry import (
    WALK_LIMIT,
    WalkLimitReached,
    is_known_commit,
    known_base,
)
from orchestrator.github import Comparison, ContentsNotFound
from orchestrator.github.client import refusal_reason
from orchestrator.gitlab.urls import gitlab_commit_url, gitlab_compare_url

__all__ = ["Adopted", "UnknownHead", "adopt_head", "preview_unknown_head"]

log = logging.getLogger(__name__)

_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class UnknownHead:
    head_sha: str
    known_base: str | None
    comparison: Comparison
    compare_url: str


@dataclass(frozen=True, slots=True)
class Adopted:
    sha: str
    #: False when the head was already known: nothing was recorded, and it was synced again.
    adopted: bool
    sync: SyncedConfig | None
    sync_error: str | None


async def _head(source: RepoConfigSource, project: Project) -> str:
    try:
        return await source.resolve_ref(project.github_repo, None)
    except ContentsNotFound as exc:
        raise Conflict(str(exc)) from exc


_EMPTY_COMPARISON = Comparison(status="ahead", total_commits=0, commits=(), files=())


async def _base_of(
    session: AsyncSession, source: RepoConfigSource, project: Project, head: str
) -> str | None:
    """The newest known commit beneath `head`, or None when its chain holds none down to its
    root. A person may then vouch for the tip: the branch can predate the factory, or another
    install, whose events this database never saw, may have seeded it (issue #422)."""
    try:
        return await known_base(session, source, project, head, limit_raises=True)
    except WalkLimitReached:
        raise Conflict(
            f"The `{CONFIG_BRANCH}` head {head[:7]} sits on more than {WALK_LIMIT} commits that "
            "nobody adopted, so what it would carry cannot be shown. Reset or re-seed the branch."
        ) from None


async def _compare(source: RepoConfigSource, project: Project, base: str, head: str) -> Comparison:
    try:
        return await source.compare(project.github_repo, base, head)
    except ContentsNotFound as exc:
        raise Conflict(
            f"GitHub cannot compare {base[:7]} with {head[:7]}. Reset the branch or try again."
        ) from exc
    except RequestFailed as exc:
        raise UpstreamFailed(
            f"GitHub did not answer the comparison ({refusal_reason(exc)}). Try again."
        ) from exc


async def preview_unknown_head(
    session: AsyncSession, source: RepoConfigSource, project: Project
) -> UnknownHead:
    """What adopting the head would vouch for. Writes nothing."""
    head = await _head(source, project)
    if await is_known_commit(session, source, project, head):
        raise Conflict(
            f"The `{CONFIG_BRANCH}` head {head[:7]} is already known, so there is nothing to adopt."
        )
    base = await _base_of(session, source, project, head)
    if base is None:
        return UnknownHead(
            head_sha=head,
            known_base=None,
            comparison=_EMPTY_COMPARISON,
            compare_url=await _commit_url(session, project, head),
        )
    comparison = await _compare(source, project, base, head)
    if comparison.status != "ahead":
        # The ruleset forbids force-push, so the last known commit is always an ancestor.
        raise Conflict(
            f"{head[:7]} is not ahead of {base[:7]} ({comparison.status}). Reset the branch."
        )
    return UnknownHead(
        head_sha=head,
        known_base=base,
        comparison=comparison,
        compare_url=await _compare_url(session, project, base, head),
    )


async def adopt_head(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    *,
    sha: str,
    actor: Actor,
) -> Adopted:
    """Record that `actor` vouched for the `factorial` head `sha`, then sync it.

    The event is committed before the sync and the sync runs in a savepoint, so a head that does
    not parse stays adopted and the builder can commit the fix on top of it.
    """
    if actor.via is RequestedVia.MCP:
        raise Forbidden(
            "Only a person can adopt a commit on the factorial branch.",
            hint="Press Review and adopt on the project's Process config page in the dashboard.",
        )
    if not _FULL_SHA.match(sha):
        raise Invalid("Name the head by its full 40-character SHA.")
    head = await _head(source, project)
    if head != sha:
        raise Conflict(
            f"The `{CONFIG_BRANCH}` branch moved to {head[:7]} since you reviewed {sha[:7]}. "
            "Review the new head."
        )
    adopted = False
    if not await is_known_commit(session, source, project, sha):
        base = await _base_of(session, source, project, sha)
        commits = 0
        if base is not None:
            comparison = await _compare(source, project, base, sha)
            if comparison.status != "ahead":
                raise Conflict(
                    f"{sha[:7]} is not ahead of {base[:7]} ({comparison.status}). Reset the branch."
                )
            commits = comparison.total_commits
        await append(
            session,
            type=EventType.CONFIG_HEAD_ADOPTED,
            project_id=project.id,
            # Named although None: pyright checks the unpacked fields against every parameter
            # the call leaves unmatched.
            ticket_id=None,
            step_run_id=None,
            branch=CONFIG_BRANCH,
            sha=sha,
            known_base=base,
            commits=commits,
            **actor.fields(),
        )
        await session.commit()
        adopted = True
    try:
        async with session.begin_nested():
            synced = await sync_config(session, source, project, sha=sha)
    except Exception as exc:  # the adoption is recorded: a 500 would say it did not happen
        log.warning("adopted %s on %s did not sync: %s", sha[:7], project.github_repo, exc)
        error = (
            exc.message
            if isinstance(exc, ServiceError)
            else "The sync failed on the server. The server log has the cause."
        )
        return Adopted(sha=sha, adopted=adopted, sync=None, sync_error=error)
    return Adopted(sha=sha, adopted=adopted, sync=synced, sync_error=None)


async def _compare_url(session: AsyncSession, project: Project, base: str, head: str) -> str:
    source = await ticket_source(session, project.id)
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        return gitlab_compare_url(project.settings, project.github_repo, base, head)
    return f"https://github.com/{project.github_repo}/compare/{base}...{head}"


async def _commit_url(session: AsyncSession, project: Project, sha: str) -> str:
    source = await ticket_source(session, project.id)
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        return gitlab_commit_url(project.settings, project.github_repo, sha)
    return f"https://github.com/{project.github_repo}/commit/{sha}"
