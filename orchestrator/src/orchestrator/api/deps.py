"""Request-scoped FastAPI dependencies: the session, the settings, the GitHub readers and
writers the routes use, and the runtime adapter a transcript reads through.

Every GitHub reader and writer is the project's own: `github_for` resolves its connection
(`github/pool.py`), and a project with none has no GitHub.

Each is an indirection so tests can override these symbols without reaching into
`orchestrator.db.session`, `orchestrator.settings` or GitHub.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi import Path as PathParam
from githubkit.exception import RequestFailed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session

from orchestrator.api.runtimes_probe import TIMEOUT_SECONDS as PROBE_TIMEOUT_SECONDS
from orchestrator.api.services.github_connection import github_repos
from orchestrator.db.models import (
    Organization,
    Project,
    Runtime,
    StepRun,
    Ticket,
    TicketSource,
    TicketSourceKind,
)
from orchestrator.db.session import session_scope
from orchestrator.factorial import CONFIG_BRANCH, CONFIG_DIRS, RepoConfigSource
from orchestrator.factorial.runtimes import SLUG_RE
from orchestrator.github import (
    ConfigBranchRules,
    GitHubClient,
    GitHubStateSource,
    InstallationAccess,
    IssueState,
    RepoContents,
    StateSource,
    commit_config_files,
    config_branch_rules,
    create_repo_label,
    repo_branch_protection,
    repo_issue,
    repo_labels,
    seed_config_branch,
)
from orchestrator.github.client import GITHUB_API_URL, ReadToken, split_repo
from orchestrator.github.connection import GitHubConnection, project_bot_login
from orchestrator.github.issues import GitHubIssueWriter, IssueWriter
from orchestrator.github.pool import pool
from orchestrator.github.read_token import read_token_for
from orchestrator.gitlab.client import GitLabClient, tls_verify
from orchestrator.settings import GitLabSettings, Settings, get_settings
from orchestrator.worker.context import adapter_options
from orchestrator.worker.runtime_adapters import (
    RuntimeAdapters,
    RuntimeRefused,
    registry_build,
    spawned_kind,
)
from runtime.adapters.codex_cli.login import LoginChild, start_device_login
from runtime.contract import AdapterUnavailable, RuntimeAdapter

__all__ = [
    "GITHUB_NOT_CONNECTED",
    "AccessReader",
    "AccessReaderDep",
    "AccountSessions",
    "AccountSessionsDep",
    "ApiGitHub",
    "ConfigBranchReader",
    "ConfigBranchReaderDep",
    "ConfigBranchSeeder",
    "ConfigBranchSeederDep",
    "ConfigCommitWriter",
    "ConfigCommitWriterDep",
    "ConfigSourceDep",
    "ConfigSourceOptionalDep",
    "GitHubFor",
    "GitHubForDep",
    "GitHubProxyHttpDep",
    "GitLabProxyHttp",
    "GitLabProxyHttpDep",
    "IssueReader",
    "IssueReaderDep",
    "LabelReader",
    "LabelReaderDep",
    "LabelWriter",
    "LabelWriterDep",
    "LoginRunner",
    "LoginRunnerDep",
    "LoginSessions",
    "LoginSessionsDep",
    "OrganizationDep",
    "PageBlobs",
    "PageBlobsDep",
    "PageBlobsFor",
    "ProbeHttpDep",
    "ProtectionReader",
    "ProtectionReaderDep",
    "ReadTokenMinter",
    "ReadTokenMinterDep",
    "RepoReader",
    "RepoReaderDep",
    "RuntimeAdapterDep",
    "SessionDep",
    "SettingsDep",
    "WorkflowsReader",
    "WorkflowsReaderDep",
    "access_reader_dependency",
    "access_reader_for",
    "account_sessions_dependency",
    "config_branch_reader_dependency",
    "config_branch_seeder_dependency",
    "config_commit_writer_dependency",
    "config_source_dependency",
    "config_source_optional_dependency",
    "get_db_session",
    "github_config_commit_writer",
    "github_config_source",
    "github_for",
    "github_for_dependency",
    "github_issue_writer",
    "github_proxy_http_dependency",
    "github_repo_reader",
    "gitlab_proxy_http_dependency",
    "issue_reader_dependency",
    "label_reader_dependency",
    "label_writer_dependency",
    "login_runner_dependency",
    "login_sessions_dependency",
    "organization_dependency",
    "organization_path",
    "page_blobs_dependency",
    "probe_http_dependency",
    "project_github",
    "project_gitlab",
    "protection_reader_dependency",
    "read_token_minter_dependency",
    "repo_reader_dependency",
    "runtime_adapter_dependency",
    "settings_dependency",
    "workflows_reader_dependency",
]

log = logging.getLogger(__name__)

PROXY_TIMEOUT_SECONDS: Final = 30.0

#: What a route says when it needs the project's GitHub and the project has no connection.
GITHUB_NOT_CONNECTED: Final = (
    "This project has no GitHub connection. Connect it with an App or a token on Ticket source."
)
GITLAB_NOT_CONFIGURED: Final = (
    "GitLab is not configured for this project. Set settings.gitlab.base_url and save GITLAB_TOKEN."
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """One unit of work per request."""
    async with session_scope() as session:
        yield session


def settings_dependency() -> Settings:
    """`get_settings` is `lru_cache`d, so this is a lookup, not a re-parse of the environment."""
    return get_settings()


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]

#: A session for work that outlives the request that started it. The device-login watcher
#: stores a credential after the client may have gone, so it cannot ride a request's session.
LoginSessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def login_sessions_dependency() -> LoginSessions:
    return session_scope


LoginSessionsDep = Annotated[LoginSessions, Depends(login_sessions_dependency)]

#: A unit of work opened only when an access decision needs one: the SSO callback with multi-org
#: on, and the access guard for a person's request (`api.access`). A request's own session would
#: be a connection on every request, the many that need no decision included.
AccountSessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def account_sessions_dependency() -> AccountSessions:
    return session_scope


AccountSessionsDep = Annotated[AccountSessions, Depends(account_sessions_dependency)]
SettingsDep = Annotated[Settings, Depends(settings_dependency)]


def organization_path(
    org: Annotated[str, PathParam(description="The organization's slug.", pattern=SLUG_RE.pattern)],
) -> None:
    """Declares `{org}` on the router mounted at `/orgs/{org}`, so it is validated and documented
    once rather than on every route beneath it."""


async def organization_dependency(request: Request, session: SessionDep) -> Organization:
    """The organization a collection route acts in: the path's `{org}` slug (tech-stack decision
    68). An unknown slug is a 404, never a fall back to the default."""
    slug = request.path_params["org"]
    organization = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if organization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Organization {slug!r} not found.")
    return organization


OrganizationDep = Annotated[Organization, Depends(organization_dependency)]


@dataclass(frozen=True, slots=True)
class ApiGitHub:
    """One project's GitHub, as the API's routes and the MCP tools reach it."""

    client: GitHubClient
    contents: RepoContents
    #: The comment predicates' author fence for this project's pending issues.
    bot_login: str


async def github_for(session: AsyncSession, project: Project) -> ApiGitHub | None:
    """The project's own connection; None without one."""
    client = await pool().for_project(session, project)
    if client is None:
        return None
    return ApiGitHub(
        client=client, contents=pool().contents(client), bot_login=project_bot_login(project)
    )


#: `project -> its GitHub, or None`. Every per-project GitHub dependency resolves through this
#: one, so a test stands in a single answer for all of them.
GitHubFor = Callable[[Project], Awaitable[ApiGitHub | None]]


def github_for_dependency(session: SessionDep) -> GitHubFor:
    """Once per project per request: a readiness read resolves it for six probes, and each
    resolution decrypts the project's credential."""
    found: dict[int, ApiGitHub | None] = {}

    async def resolve(project: Project) -> ApiGitHub | None:
        if project.id not in found:
            found[project.id] = await github_for(session, project)
        return found[project.id]

    return resolve


GitHubForDep = Annotated[GitHubFor, Depends(github_for_dependency)]


def _session_of(project: Project) -> AsyncSession:
    session = async_object_session(project)
    if session is None:
        raise RuntimeError(f"project {project.id} is not attached to a session")
    return session


async def project_github(project: Project) -> ApiGitHub | None:
    """`github_for` in the session that loaded `project`: the shape of the MCP ports
    (`McpDeps`), which hold no session of their own."""
    return await github_for(_session_of(project), project)


async def project_gitlab(project: Project) -> GitLabClient | None:
    """The project's GitLab client in the session that loaded it; None unconfigured."""
    from orchestrator.gitlab.project_client import client_for_project

    return await client_for_project(_session_of(project), project)


async def github_config_source(project: Project) -> RepoConfigSource | None:
    """The project's GitHub config reader. The MCP tools pick the forge themselves
    (`forge_config_source`)."""
    github = await project_github(project)
    return None if github is None else RepoConfigSource(github.contents)


async def github_config_commit_writer(project: Project) -> ConfigCommitWriter | None:
    github = await project_github(project)
    return None if github is None else partial(commit_config_files, github.client)


async def github_issue_writer(project: Project) -> IssueWriter | None:
    """`None` without a connection, and `create_ticket` then refuses."""
    github = await project_github(project)
    return None if github is None else GitHubIssueWriter(github.client)


async def github_repo_reader(project: Project) -> RepoReader | None:
    github = await project_github(project)
    return None if github is None else partial(GitHubStateSource, github.client)


#: `(client, one owner's repos) -> a read-only token over the ones its installation holds`
#: (design.md §7.18). A callable rather than the mint, so a stand-in needs no githubkit call
#: chain.
ReadTokenMinter = Callable[[GitHubClient, Collection[str]], Awaitable[ReadToken]]


def read_token_minter_dependency() -> ReadTokenMinter:
    return read_token_for


ReadTokenMinterDep = Annotated[ReadTokenMinter, Depends(read_token_minter_dependency)]


async def github_proxy_http_dependency() -> AsyncIterator[httpx.AsyncClient]:
    """The client the assistant's read proxy calls GitHub through, so a test answers with a
    mock."""
    async with httpx.AsyncClient(
        base_url=GITHUB_API_URL, timeout=PROXY_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        yield client


GitHubProxyHttpDep = Annotated[httpx.AsyncClient, Depends(github_proxy_http_dependency)]

#: `(a project's GitLab settings) -> the client the assistant's GitLab read proxy calls it with`.
GitLabProxyHttp = Callable[[GitLabSettings], httpx.AsyncClient]


def gitlab_proxy_http_dependency() -> GitLabProxyHttp:
    """A factory rather than a client: each project names its own host and CA."""

    def open_client(gitlab: GitLabSettings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=gitlab.api_url,
            timeout=PROXY_TIMEOUT_SECONDS,
            follow_redirects=False,
            verify=tls_verify(gitlab),
        )

    return open_client


GitLabProxyHttpDep = Annotated[GitLabProxyHttp, Depends(gitlab_proxy_http_dependency)]


async def _github_of(
    session: AsyncSession, project_id: int, github_for: GitHubFor
) -> ApiGitHub | None:
    project = await session.get(Project, project_id)
    return None if project is None else await github_for(project)


async def _ticket_github(
    session: AsyncSession, ticket_id: int, github_for: GitHubFor
) -> ApiGitHub | None:
    project = (
        await session.execute(
            select(Project)
            .join(Ticket, Ticket.project_id == Project.id)
            .where(Ticket.id == ticket_id)
        )
    ).scalar_one_or_none()
    return None if project is None else await github_for(project)


async def _ticket_source_kind(session: AsyncSession, project_id: int) -> str | None:
    row = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project_id))
    ).scalar_one_or_none()
    return None if row is None else str(row.kind)


async def _config_source_for_request(
    session: AsyncSession, project_id: int, github_for: GitHubFor
) -> RepoConfigSource | None:
    """GitLab ticket sources read `.factorial/` with the project token, GitHub ones with the
    project's connection."""
    from orchestrator.gitlab.project_client import config_source_for

    project = await session.get(Project, project_id)
    if project is None:
        return None
    github = await github_for(project)
    return await config_source_for(
        session, project, github=None if github is None else RepoConfigSource(github.contents)
    )


async def config_source_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> RepoConfigSource:
    """Fail closed when the forge this project reads config from is not configured.

    Without credentials the source would raise from deep inside the forge client, indistinguishable
    from "that repo has no `.factorial/`". A 503 naming the missing configuration is the honest
    answer.
    A GitLab ticket source does not require a GitHub connection.
    """
    source = await _config_source_for_request(session, project_id, github_for)
    if source is None:
        kind = await _ticket_source_kind(session, project_id)
        detail = GITLAB_NOT_CONFIGURED if kind == TicketSourceKind.GITLAB else GITHUB_NOT_CONNECTED
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    return source


ConfigSourceDep = Annotated[RepoConfigSource, Depends(config_source_dependency)]


async def config_source_optional_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> RepoConfigSource | None:
    """The fail-open counterpart of `config_source_dependency`, for best-effort readiness probes.

    A probe is decoration on a request that is otherwise pure database, so an unconfigured forge is
    `None` here and the probe degrades to "not checked" rather than refusing the read.
    """
    return await _config_source_for_request(session, project_id, github_for)


ConfigSourceOptionalDep = Annotated[
    RepoConfigSource | None, Depends(config_source_optional_dependency)
]

#: `repo -> label names defined on it`. A callable rather than a client so a stand-in needs no
#: githubkit call chain.
LabelReader = Callable[[str], Awaitable[frozenset[str]]]


async def _gitlab_for_project(
    session: AsyncSession, project_id: int
) -> tuple[bool, GitLabClient | None]:
    """`(is_gitlab, client)`. The client is None when GitLab is the ticket source but has no
    URL or token. A GitHub source is `(False, None)` and the caller uses the project's GitHub."""
    from orchestrator.gitlab.project_client import client_for_project

    project = await session.get(Project, project_id)
    if project is None:
        return False, None
    row = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
    ).scalar_one_or_none()
    if row is None or row.kind != TicketSourceKind.GITLAB:
        return False, None
    client: GitLabClient | None = await client_for_project(session, project)
    return True, client


async def label_reader_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> LabelReader | None:
    """`None` when this project's forge is not configured — same fail-open contract as the
    optional source. A GitLab ticket source reads labels with the project token."""
    from orchestrator.gitlab.readiness import repo_labels as gitlab_labels

    gitlab, client = await _gitlab_for_project(session, project_id)
    if gitlab:
        return partial(gitlab_labels, client) if client is not None else None
    github = await _github_of(session, project_id, github_for)
    return None if github is None else partial(repo_labels, github.client)


LabelReaderDep = Annotated[LabelReader | None, Depends(label_reader_dependency)]

#: `repo -> workflow file names under .github/workflows on the default branch`. Same
#: callable-not-client shape as `LabelReader`; consumed only by the `ci_workflows` readiness
#: row, which is why a repo with no such directory answers `()` rather than raising.
WorkflowsReader = Callable[[str], Awaitable[tuple[str, ...]]]


async def workflows_reader_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> WorkflowsReader | None:
    """`None` when this project's forge is not configured — same fail-open contract as the
    optional source. A GitLab ticket source looks for the project's CI config file."""
    from orchestrator.gitlab.readiness import repo_ci_files

    gitlab, client = await _gitlab_for_project(session, project_id)
    if gitlab:
        return partial(repo_ci_files, client) if client is not None else None
    github = await _github_of(session, project_id, github_for)
    if github is None:
        return None
    contents = github.contents

    async def read(repo: str) -> tuple[str, ...]:
        sha = await contents.resolve_ref(repo, await contents.default_branch(repo))
        entries = await contents.list_dir(repo, ".github/workflows", sha)
        # Actions reads only .yml/.yaml files at the top of the directory; so does this.
        return tuple(
            sorted(
                entry.name
                for entry in entries
                if entry.type == "file" and entry.name.endswith((".yml", ".yaml"))
            )
        )

    return read


WorkflowsReaderDep = Annotated[WorkflowsReader | None, Depends(workflows_reader_dependency)]

#: `(repo, blob sha) -> bytes`: a page's content (`api/routes/pages.py`, decision 58).
PageBlobs = Callable[[str, str], Awaitable[bytes]]
#: `project -> its PageBlobs, or None without a connection` (the page route answers 503).
PageBlobsFor = Callable[[Project], Awaitable[PageBlobs | None]]


def page_blobs_dependency(github_for: GitHubForDep) -> PageBlobsFor:
    """Resolved by the route after it has checked the link, so an unsigned request reads no
    connection."""

    async def blobs_for(project: Project) -> PageBlobs | None:
        github = await github_for(project)
        return None if github is None else github.contents.read_blob

    return blobs_for


PageBlobsDep = Annotated[PageBlobsFor, Depends(page_blobs_dependency)]


class ProtectionProfile(Protocol):
    """What readiness reads of a default branch's protection, on either forge."""

    @property
    def branch(self) -> str: ...
    @property
    def protected(self) -> bool: ...
    @property
    def requires_codeowners(self) -> bool: ...
    def shortfalls(self) -> tuple[str, ...]: ...


#: `repo -> its default branch's protection`. Same callable-not-client shape as `LabelReader`.
ProtectionReader = Callable[[str], Awaitable[ProtectionProfile]]


async def protection_reader_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> ProtectionReader | None:
    """`None` when this project's forge is not configured. Configured is not the same as
    *permitted*: reading GitHub protection needs `administration: read`, which the installation
    may not hold, and that comes back as a 403 from the reader rather than a `None` here. Both
    degrade to "not checked" — the checklist must never report an unreadable branch as an
    unprotected one. A GitLab ticket source reads protected branches with the project token."""
    from orchestrator.gitlab.readiness import repo_branch_protection as gitlab_protection

    gitlab, client = await _gitlab_for_project(session, project_id)
    if gitlab:
        return partial(gitlab_protection, client) if client is not None else None
    github = await _github_of(session, project_id, github_for)
    return None if github is None else partial(repo_branch_protection, github.client)


ProtectionReaderDep = Annotated[ProtectionReader | None, Depends(protection_reader_dependency)]

#: `(repo, name, colour, description) -> None`. The one repo-side write the checklist offers.
LabelWriter = Callable[[str, str, str, str], Awaitable[None]]


async def label_writer_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> LabelWriter | None:
    """`None` when this project's forge is not configured; the route then refuses rather than
    pretending. A GitLab ticket source creates labels with the project token."""
    from orchestrator.gitlab.readiness import create_repo_label as gitlab_create_label

    gitlab, client = await _gitlab_for_project(session, project_id)
    if gitlab:
        return partial(gitlab_create_label, client) if client is not None else None
    github = await _github_of(session, project_id, github_for)
    return None if github is None else partial(create_repo_label, github.client)


LabelWriterDep = Annotated[LabelWriter | None, Depends(label_writer_dependency)]

#: `(repo, branch, base_sha, files, message, record) -> the new commit's sha`, fast-forwarded onto
#: `branch`; `record` is awaited with the sha before the ref moves. Raises `ConfigBranchMoved`.
#: Paths arrive from the operator, so containment is the route's job: every submitted path must
#: be a member of the proposal `assemble()` recomputes in the same request (tech-stack decision
#: 33, design.md §7.7).
ConfigCommitWriter = Callable[
    [str, str, str, Mapping[str, str], str, Callable[[str], Awaitable[None]]], Awaitable[str]
]


async def config_commit_writer_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> ConfigCommitWriter | None:
    """The forge that receives the dashboard's config commit.

    A GitLab ticket source commits with the project token. `None` means that forge is not
    configured; the route refuses rather than pretending the write happened.
    """
    from orchestrator.gitlab.files_commit import commit_config_files as gitlab_commit
    from orchestrator.gitlab.project_client import client_for_project

    project = await session.get(Project, project_id)
    if project is not None:
        row = (
            await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
        ).scalar_one_or_none()
        if row is not None and row.kind == TicketSourceKind.GITLAB:
            client = await client_for_project(session, project)
            if client is None:
                return None
            return partial(gitlab_commit, client)
    github = None if project is None else await github_for(project)
    return None if github is None else partial(commit_config_files, github.client)


ConfigCommitWriterDep = Annotated[
    ConfigCommitWriter | None, Depends(config_commit_writer_dependency)
]

#: `(repo, record, empty) -> the sha of the orphan commit a new config branch points at`;
#: `record` is awaited with that sha before the branch exists. Raises `ConfigBranchExists`.
ConfigBranchSeeder = Callable[[str, Callable[[str], Awaitable[None]], bool], Awaitable[str]]


async def config_branch_seeder_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> ConfigBranchSeeder | None:
    """`None` when the forge this project writes config to is not configured."""
    from orchestrator.gitlab.files_commit import seed_config_branch as gitlab_seed
    from orchestrator.gitlab.project_client import client_for_project

    project = await session.get(Project, project_id)
    if project is not None:
        row = (
            await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
        ).scalar_one_or_none()
        if row is not None and row.kind == TicketSourceKind.GITLAB:
            client = await client_for_project(session, project)
            if client is None:
                return None

            async def seed(repo: str, record: Callable[[str], Awaitable[None]], empty: bool) -> str:
                return await gitlab_seed(
                    client, repo, CONFIG_BRANCH, CONFIG_DIRS, record, empty=empty
                )

            return seed
    github = None if project is None else await github_for(project)
    if github is None:
        return None
    client = github.client

    async def seed(repo: str, record: Callable[[str], Awaitable[None]], empty: bool) -> str:
        return await seed_config_branch(
            client, repo, CONFIG_BRANCH, CONFIG_DIRS, record, empty=empty
        )

    return seed


ConfigBranchSeederDep = Annotated[
    ConfigBranchSeeder | None, Depends(config_branch_seeder_dependency)
]

#: `repo -> the config branch's rules, or None when the branch does not exist`.
ConfigBranchReader = Callable[[str], Awaitable[ConfigBranchRules | None]]


async def config_branch_reader_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> ConfigBranchReader | None:
    """`None` when the forge this project reads config from is not configured."""
    from orchestrator.gitlab.files_commit import config_branch_rules as gitlab_rules
    from orchestrator.gitlab.project_client import client_for_project

    project = await session.get(Project, project_id)
    if project is not None:
        row = (
            await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
        ).scalar_one_or_none()
        if row is not None and row.kind == TicketSourceKind.GITLAB:
            client = await client_for_project(session, project)
            if client is None:
                return None

            async def read(repo: str) -> ConfigBranchRules | None:
                return await gitlab_rules(client, repo, CONFIG_BRANCH)

            return read
    github = None if project is None else await github_for(project)
    if github is None:
        return None
    client = github.client

    async def read(repo: str) -> ConfigBranchRules | None:
        return await config_branch_rules(client, repo, CONFIG_BRANCH)

    return read


ConfigBranchReaderDep = Annotated[
    ConfigBranchReader | None, Depends(config_branch_reader_dependency)
]

@dataclass(frozen=True, slots=True)
class AccessReader:
    """What the `app_access` row reads of one project's reach: an App's installation per repo,
    or what GitHub answers the stored token for each repo. Two seams, one per mode, so token
    mode never reaches an App-only endpoint."""

    mode: Literal["app", "token"]
    #: The project's own repo first, then its GitHub intake repo when that is another.
    repos: tuple[str, ...]
    #: App: the installation on the repo's owner, and whether the repo is inside it.
    installation: Callable[[str], Awaitable[InstallationAccess]]
    #: Token: GitHub's HTTP status for `GET /repos/{owner}/{repo}`.
    repo_status: Callable[[str], Awaitable[int]]


def access_reader_for(client: GitHubClient, repos: Sequence[str]) -> AccessReader:
    async def repo_status(repo: str) -> int:
        owner, name = split_repo(repo)
        api = await client.api(repo)
        try:
            return (await api.rest.repos.async_get(owner, name)).status_code
        except RequestFailed as exc:
            return exc.response.status_code

    connection = client.connection
    return AccessReader(
        mode=connection.mode if connection is not None else "app",
        repos=tuple(repos),
        installation=client.installation_access,
        repo_status=repo_status,
    )


async def access_reader_dependency(
    project_id: Annotated[int, PathParam(ge=1)],
    session: SessionDep,
    github_for: GitHubForDep,
) -> AccessReader | None:
    """`None` without a connection — which is itself the answer the row reports."""
    project = await session.get(Project, project_id)
    github = None if project is None else await github_for(project)
    if project is None or github is None:
        return None
    return access_reader_for(github.client, await github_repos(session, project))


AccessReaderDep = Annotated[AccessReader | None, Depends(access_reader_dependency)]

#: `(repo, issue number) -> that issue as GitHub has it`. A callable rather than a client, for the
#: same reason as `LabelReader`: a stand-in needs no githubkit call chain.
IssueReader = Callable[[str, int], Awaitable[IssueState]]


async def issue_reader_dependency(
    ticket_id: Annotated[int, PathParam()], session: SessionDep, github_for: GitHubForDep
) -> IssueReader | None:
    """The GitHub issue reader for the ticket's project. The route resolves an explicit GitLab
    source per project."""
    github = await _ticket_github(session, ticket_id, github_for)
    return None if github is None else partial(repo_issue, github.client)


IssueReaderDep = Annotated[IssueReader | None, Depends(issue_reader_dependency)]

#: `(repo, issue number) -> the observer one worker tick builds`: the issue, the repo's branches
#: and its pull requests, each read once. The worker's own `state_source` shape, so a stand-in is
#: a `SnapshotState`.
RepoReader = Callable[[str, int], StateSource]


async def repo_reader_dependency(
    ticket_id: Annotated[int, PathParam()], session: SessionDep, github_for: GitHubForDep
) -> RepoReader:
    """Fail closed, as the issue read does: the restart preview's read is the request, and a
    preview with nothing to close or delete would read as a restart that loses nothing."""
    github = await _ticket_github(session, ticket_id, github_for)
    if github is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GITHUB_NOT_CONNECTED
        )
    return partial(GitHubStateSource, github.client)


RepoReaderDep = Annotated[RepoReader, Depends(repo_reader_dependency)]


@lru_cache(maxsize=1)
def _runtime_adapters() -> RuntimeAdapters:
    """The per-runtime adapter cache, built exactly as `worker.context.production_deps` builds
    the worker's, and process-wide for the same reason: an adapter holds supervisors, so one
    instance per runtime row, never one per request."""
    return RuntimeAdapters(registry_build(partial(adapter_options, get_settings())))


async def runtime_adapter_dependency(
    session: SessionDep, step_run_id: int | None = None
) -> RuntimeAdapter | None:
    """The substrate, for the one read the database cannot answer: a run's transcript
    (`runs.get_run_activity`), by THAT run's runtime and the kind its `agent_spawned` event
    recorded.

    `None` when there is no answer — no run, a run on no runtime, a runtime row since deleted,
    or one the cache refuses (logged) — so an API that is otherwise pure database still serves
    every other route; the route answers 503 itself.

    **This is the read path taking a dependency on the substrate**, which nothing else in the API
    does. A wedged substrate can hang these routes, and only these: every other route never
    touches the adapter.
    """
    if step_run_id is None:
        return None
    runtime = (
        await session.execute(
            select(Runtime)
            .join(StepRun, StepRun.runtime_id == Runtime.id)
            .where(StepRun.id == step_run_id)
        )
    ).scalar_one_or_none()
    if runtime is None:
        return None
    try:
        return _runtime_adapters().for_runtime(runtime, await spawned_kind(session, step_run_id))
    except (RuntimeRefused, AdapterUnavailable) as exc:
        log.warning("no adapter reads run %s's logs: %s", step_run_id, exc)
        return None


RuntimeAdapterDep = Annotated[RuntimeAdapter | None, Depends(runtime_adapter_dependency)]


#: `(config home) -> a started login child`. The child runs with `CODEX_HOME=<home>`, so the
#: login it completes lands as `<home>/auth.json` and nowhere near the operator's own `~/.codex`.
LoginRunner = Callable[[Path], Awaitable[LoginChild]]


def login_runner_dependency(settings: SettingsDep) -> LoginRunner:
    """The seam the login route starts a CLI through — the only reason it is a dependency is so
    tests can hand in a fake that never spawns anything."""
    return partial(start_device_login, settings.runtime.codex_cli.codex_bin)


LoginRunnerDep = Annotated[LoginRunner, Depends(login_runner_dependency)]


async def probe_http_dependency() -> AsyncIterator[httpx.AsyncClient]:
    """The client `test-credential` calls a vendor through, so a test answers with a mock."""
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
        yield client


ProbeHttpDep = Annotated[httpx.AsyncClient, Depends(probe_http_dependency)]


#: `(connection) -> a client for it`: the seam saving a GitHub connection looks the App up
#: through, so a test answers with a client that reaches no GitHub.
ConnectionClientFactory = Callable[[GitHubConnection], GitHubClient]


def connection_client_dependency() -> ConnectionClientFactory:
    return GitHubClient


ConnectionClientDep = Annotated[ConnectionClientFactory, Depends(connection_client_dependency)]
