"""Tracker and forge factories. Kind checks belong here, not in the worker or API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.bindings import Bindings, ForgePort, TrackerPort
from orchestrator.adapters.display import HostDisplay, display_for
from orchestrator.adapters.split import forge_kind_of
from orchestrator.config.scaffold.catalog import Forge
from orchestrator.db.models import Project, TicketSource, TicketSourceKind

if TYPE_CHECKING:
    from orchestrator.github.host import GitHubBundle
    from orchestrator.gitlab.client import GitLabClient

__all__ = [
    "ForgeFactory",
    "GitHubResolver",
    "GitLabResolver",
    "HostFailure",
    "TrackerFactory",
    "display_for_settings",
    "display_of",
    "forge_kind_for",
    "forge_of",
    "host_failure",
    "host_registries",
    "resolve_bindings",
    "tracker_kind_for",
]

TrackerFactory = Callable[[AsyncSession, Project], Awaitable[TrackerPort | None]]
ForgeFactory = Callable[[AsyncSession, Project], Awaitable[ForgePort | None]]
#: `(session, project) -> the project's GitHub bundle, or None` (`github.host.GitHubBundle`).
GitHubResolver = Callable[[AsyncSession, Project], Awaitable["GitHubBundle | None"]]
#: `(session, project) -> the project's own GitLab client, or None`.
GitLabResolver = Callable[[AsyncSession, Project], Awaitable["GitLabClient | None"]]


async def tracker_kind_for(session: AsyncSession, project: Project) -> str:
    row = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
    ).scalar_one_or_none()
    return row.kind if row is not None else "github"


def forge_kind_for(project: Project, tracker_kind: str | None) -> str | None:
    settings = project.settings if isinstance(project.settings, dict) else None
    return forge_kind_of(tracker_kind, settings)


def forge_of(source: TicketSource | None) -> Forge:
    """The host a project's template catalog and config files belong to, from its ticket source
    (tech-stack decision 88). Only GitLab is a second host; every other source, including none,
    is GitHub."""
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        return "gitlab"
    return "github"


def display_of(project: Project, tracker_kind: str | None) -> HostDisplay:
    return display_for_settings(tracker_kind, project.settings)


def display_for_settings(tracker_kind: str | None, settings: object) -> HostDisplay:
    """`display_of` for a caller holding the settings without the project row."""
    bag = settings if isinstance(settings, dict) else None
    kind = forge_kind_of(tracker_kind, bag) or "github"
    origin = None
    # A switch back to GitHub leaves `settings.gitlab` in place, so only a GitLab forge reads it.
    gitlab = (bag or {}).get("gitlab") if kind == "gitlab" else None
    if isinstance(gitlab, dict):
        raw = gitlab.get("base_url")
        if isinstance(raw, str) and raw.strip():
            origin = raw.strip()
    return display_for(kind, origin=origin)


@dataclass(frozen=True, slots=True)
class HostFailure:
    """An HTTP error from a tracker or forge, whichever host raised it."""

    status: int | None


def host_failure(exc: BaseException) -> HostFailure | None:
    """The host's HTTP error behind `exc`, or None when `exc` is not one."""
    from githubkit.exception import RequestFailed

    from orchestrator.gitlab.client import GitLabError

    if isinstance(exc, RequestFailed):
        return HostFailure(exc.response.status_code)
    if isinstance(exc, GitLabError):
        return HostFailure(exc.status_code)
    return None


def host_registries(
    github: GitHubResolver,
    *,
    gitlab: GitLabResolver | None = None,
) -> tuple[dict[str, TrackerFactory], dict[str, ForgeFactory]]:
    """The tracker and forge factories for the hosts Factorial ships, by kind.

    `github`, and `gitlab` when given, are the caller's own resolvers, so a worker tick's memo,
    an API request's injected dependency and an MCP context's ports all hold. Each host's
    connection is resolved at most once per registry: a same-host project's tracker and forge
    share it rather than decrypting it twice.
    """
    from orchestrator.github.host import GitHubForge, GitHubTracker
    from orchestrator.gitlab.host import GitLabForge, GitLabTracker, gitlab_client

    github_once = _once_per_project(github)
    gitlab_once = _once_per_project(gitlab if gitlab is not None else gitlab_client)

    async def github_tracker(session: AsyncSession, project: Project) -> TrackerPort | None:
        bundle = await github_once(session, project)
        return None if bundle is None else GitHubTracker(bundle)

    async def github_forge(session: AsyncSession, project: Project) -> ForgePort | None:
        bundle = await github_once(session, project)
        return None if bundle is None else GitHubForge(bundle)

    async def gitlab_tracker(session: AsyncSession, project: Project) -> TrackerPort | None:
        client = await gitlab_once(session, project)
        return None if client is None else GitLabTracker(client)

    async def gitlab_forge(session: AsyncSession, project: Project) -> ForgePort | None:
        client = await gitlab_once(session, project)
        return None if client is None else GitLabForge(client)

    return (
        {"github": github_tracker, "gitlab": gitlab_tracker},
        {"github": github_forge, "gitlab": gitlab_forge},
    )


def _once_per_project[T](
    resolve: Callable[[AsyncSession, Project], Awaitable[T]],
) -> Callable[[AsyncSession, Project], Awaitable[T]]:
    found: dict[int, T] = {}

    async def once(session: AsyncSession, project: Project) -> T:
        if project.id not in found:
            found[project.id] = await resolve(session, project)
        return found[project.id]

    return once


async def resolve_bindings(
    session: AsyncSession,
    project: Project,
    *,
    trackers: Mapping[str, TrackerFactory],
    forges: Mapping[str, ForgeFactory],
) -> Bindings | None:
    """One tracker and one forge, or None when a kind has no factory or either side cannot be
    built. Nothing falls back to another kind's host: a project whose own connection is missing
    is held, never read through someone else's."""
    tracker_kind = await tracker_kind_for(session, project)
    host_kind = forge_kind_for(project, tracker_kind)
    tracker_factory = trackers.get(tracker_kind)
    forge_factory = forges.get(host_kind) if host_kind is not None else None
    if tracker_factory is None or forge_factory is None:
        return None
    tracker = await tracker_factory(session, project)
    if tracker is None:
        return None
    forge = await forge_factory(session, project)
    return None if forge is None else Bindings(tracker, forge)
