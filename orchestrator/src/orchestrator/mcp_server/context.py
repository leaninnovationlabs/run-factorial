"""What a tool reaches: one database session per call, the settings, and the host ports.

The ports are the API's own (`api/deps.py`), each resolved for the project a tool acts on, so a
project's client and its installation tokens are shared with the routes. A project whose host is
not connected is refused here, once. A tool reaches GitHub or GitLab only through `deps`, never
through `api/deps.py` itself, so a test can stub it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token, get_context
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.bindings import Bindings
from orchestrator.adapters.registry import (
    display_of,
    host_registries,
    resolve_bindings,
    tracker_kind_for,
)
from orchestrator.api.deps import (
    GITHUB_NOT_CONNECTED,
    ApiGitHub,
    ConfigCommitWriter,
    RepoReader,
    github_config_source,
    github_repo_reader,
    project_config_commit_writer,
    project_github,
    project_gitlab,
    project_issue_writer,
)
from orchestrator.api.services.errors import Unavailable
from orchestrator.db.models import Project
from orchestrator.db.session import session_scope
from orchestrator.factorial import RepoConfigSource
from orchestrator.github.issues import IssueWriter
from orchestrator.gitlab.client import GitLabClient
from orchestrator.settings import Settings, get_settings

if TYPE_CHECKING:
    from orchestrator.mcp_server.chat.scope import ChatScope

__all__ = [
    "DEPS_KEY",
    "GITLAB_MISSING",
    "McpDeps",
    "Sessions",
    "ToolContext",
    "deps_lifespan",
    "tool_context",
]

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Where `build_server` leaves the deps in FastMCP's lifespan context.
DEPS_KEY = "deps"
GITLAB_MISSING = (
    "GitLab is not configured for this project. Set the GitLab URL and store GITLAB_TOKEN."
)


@dataclass(frozen=True)
class McpDeps:
    settings: Callable[[], Settings] = get_settings
    sessions: Sessions = session_scope
    #: Opens an issue where the project's tickets live, on either host. Writes only ever go
    #: through this port and `commit_writer`, never `bindings`: the assistant leaves both empty,
    #: while its read ports still answer.
    issue_writer: Callable[[Project], Awaitable[IssueWriter | None]] = project_issue_writer
    #: The GitHub port. A GitLab project's tools read through `gitlab` (`tools/config.py`).
    config_source: Callable[[Project], Awaitable[RepoConfigSource | None]] = github_config_source
    commit_writer: Callable[[Project], Awaitable[ConfigCommitWriter | None]] = (
        project_config_commit_writer
    )
    #: The project's client and bot login.
    github: Callable[[Project], Awaitable[ApiGitHub | None]] = project_github
    #: The observer a worker tick builds: the restart preview reads through it.
    repo_reader: Callable[[Project], Awaitable[RepoReader | None]] = github_repo_reader
    #: What a GitHub-reading tool says when its port is `None`.
    github_missing: str = GITHUB_NOT_CONNECTED
    #: A GitLab project's client, which opens its stored token.
    gitlab: Callable[[Project], Awaitable[GitLabClient | None]] = project_gitlab
    gitlab_missing: str = GITLAB_MISSING


@dataclass(frozen=True)
class ToolContext:
    session: AsyncSession
    settings: Settings
    deps: McpDeps
    #: The organization every ref resolves in and every list is drawn from: the MCP token's on
    #: `/mcp/`, the conversation's in an assistant turn (design.md §2.11).
    organization_id: int
    #: The assistant turn a call belongs to; None on `/mcp/`.
    turn: ChatScope | None = None

    async def commit_writer(self, project: Project) -> ConfigCommitWriter:
        writer = await self.deps.commit_writer(project)
        if writer is None:
            raise Unavailable(await self.unbound(project))
        return writer

    async def issue_writer(self, project: Project) -> IssueWriter:
        writer = await self.deps.issue_writer(project)
        if writer is None:
            raise Unavailable(await self.unbound(project))
        return writer

    async def bindings(self, project: Project) -> Bindings | None:
        """The project's tracker and forge over this context's read ports, for reading only."""
        from orchestrator.worker.context import ProjectGitHub

        async def github(_session: AsyncSession, found: Project) -> ProjectGitHub | None:
            api = await self.deps.github(found)
            if api is None:
                return None
            return ProjectGitHub.over(api.client, api.contents, bot_login=api.bot_login)

        async def gitlab(_session: AsyncSession, found: Project) -> GitLabClient | None:
            return await self.deps.gitlab(found)

        trackers, forges = host_registries(github, gitlab=gitlab)
        return await resolve_bindings(self.session, project, trackers=trackers, forges=forges)

    async def unbound(self, project: Project) -> str:
        """What a tool says when the project's host is not connected."""
        host = display_of(project, await tracker_kind_for(self.session, project))
        return self.deps.github_missing if host.github_app else self.deps.gitlab_missing

    async def repo_reader(self, project: Project) -> RepoReader:
        reader = await self.deps.repo_reader(project)
        if reader is None:
            raise Unavailable(self.deps.github_missing)
        return reader


def deps_lifespan(
    deps: McpDeps,
) -> Callable[[Any], AbstractAsyncContextManager[dict[str, Any]]]:
    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[dict[str, Any]]:
        yield {DEPS_KEY: deps}

    return lifespan


@asynccontextmanager
async def tool_context() -> AsyncIterator[ToolContext]:
    """A tool's `Depends`: commits when the tool returns, rolls back when it raises. The
    organization is the token row's, read when the request was verified."""
    deps: McpDeps = get_context().lifespan_context[DEPS_KEY]
    organization_id = getattr(get_access_token(), "organization_id", None)
    if not isinstance(organization_id, int):
        raise AuthorizationError("This request carries no Factorial MCP token.")
    async with deps.sessions() as session:
        yield ToolContext(
            session=session, settings=deps.settings(), deps=deps, organization_id=organization_id
        )
