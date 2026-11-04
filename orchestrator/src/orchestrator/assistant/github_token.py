"""The assistant's read-only GitHub and GitLab access (design.md §7.18): the repos it covers, the
cache the process keeps it in, and the clients and ports its tools read with.

The API hands this process one entry per App and owner, and per token or GitLab project, on
loopback for the API token (`api_fetch`). An App's entry is a read-only token the API minted
with that App's key over its projects' repos under that owner. A token project's entry holds no
token: its base URL is the API's GET-only read proxy for the project (`routes/assistant.py`),
which this process calls with the API token and which adds the stored token server-side. A
GitLab project's entry is the same, on the API's GitLab read proxy, and also names the project
and its bot login: a GitLab token opens only with the master key, which this process does not
hold. So this process holds nothing that can write.
There is one cache per organization (`OrganizationTokens`), over that
organization's repos alone, so a turn's GitHub reads never reach another tenant's repos
(design.md §2.11). At each turn's start `ReadTokenCache.refresh` asks again when the
organization's repos changed or when any entry has less than the turn's wall clock plus `MARGIN`
left, so a token never expires mid-turn. A failed fetch keeps the entries there are, which a
running turn may be reading with; with no entry for a repo, a GitHub-reading tool answers
`NO_TOKEN`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Generator, Hashable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final, Literal

import httpx
from githubkit import GitHub, TokenAuthStrategy
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import ApiGitHub, RepoReader
from orchestrator.api.services.errors import Unavailable
from orchestrator.db.models import Project, TicketSource, TicketSourceKind
from orchestrator.factorial import RepoConfigSource
from orchestrator.github.client import GitHubClient
from orchestrator.github.connection import project_bot_login
from orchestrator.github.contents import RepoContents
from orchestrator.github.observer import GitHubStateSource
from orchestrator.gitlab.client import GitLabClient
from orchestrator.mcp_server.context import McpDeps, Sessions
from orchestrator.settings import APP_PREFIX, GitLabSettings, Settings

__all__ = [
    "GITLAB_PROXY_PATH",
    "MARGIN",
    "NO_GITLAB",
    "NO_TOKEN",
    "PROXY_PATH",
    "TOKEN_PATH",
    "Fetch",
    "OrganizationTokens",
    "ProxyGitLabClient",
    "ReadEntry",
    "ReadTokenCache",
    "ReadTokenClient",
    "api_fetch",
    "factory_repos",
    "factory_repos_from_rows",
    "tool_deps",
]

log = logging.getLogger(__name__)

MARGIN: Final = timedelta(minutes=5)
#: Formatted with the organization's slug. The prefix too: loopback passes no load balancer.
TOKEN_PATH: Final = f"{APP_PREFIX}/api/orgs/{{org}}/assistant/github-token"
#: The read proxy's base for one token project, formatted with its id.
PROXY_PATH: Final = f"{APP_PREFIX}/api/assistant/github/projects/{{project_id}}"
#: The GitLab read proxy's base for one GitLab project, formatted with its id.
GITLAB_PROXY_PATH: Final = f"{APP_PREFIX}/api/assistant/gitlab/projects/{{project_id}}"
FETCH_TIMEOUT: Final = 10.0
NO_TOKEN: Final = (
    "The assistant has no GitHub token, because the API could not mint one. Try again later."
)
NO_GITLAB: Final = (
    "The assistant cannot read this GitLab project: it has no GitLab connection, or the API "
    "could not be reached. Try again later."
)


@dataclass(frozen=True, slots=True)
class ReadEntry:
    """How the assistant reads `repos` (lower-cased): at `base_url` with `token`, or with the
    API token when `token` is empty. A GitLab entry is always the latter, for `project_id`."""

    repos: frozenset[str]
    token: SecretStr
    expires_at: datetime
    base_url: str
    forge: Literal["github", "gitlab"] = "github"
    project_id: int | None = None
    bot_login: str = ""


Fetch = Callable[[], Awaitable[Sequence[ReadEntry]]]


def factory_repos_from_rows(
    rows: list[tuple[str, str | None, str | None]],
) -> frozenset[str]:
    repos: set[str] = set()
    for github_repo, kind, external_ref in rows:
        repos.add(github_repo)
        if kind in (TicketSourceKind.GITHUB, TicketSourceKind.GITLAB) and external_ref:
            repos.add(external_ref)
    return frozenset(repos)


async def factory_repos(session: AsyncSession, organization_id: int) -> frozenset[str]:
    """Every repo a chat tool in the organization may read: each of its projects' own, and its
    GitHub or GitLab intake repo."""
    rows = await session.execute(
        select(Project.github_repo, TicketSource.kind, TicketSource.external_ref)
        .outerjoin(TicketSource, TicketSource.project_id == Project.id)
        .where(Project.organization_id == organization_id)
    )
    return factory_repos_from_rows(
        [(str(github_repo), kind, external_ref) for github_repo, kind, external_ref in rows]
    )


class ReadTokenCache:
    """One per organization. `refresh` runs at each turn's start; the tools read `entry_for`."""

    def __init__(
        self, fetch: Fetch, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._entries: tuple[ReadEntry, ...] = ()
        self._wanted: frozenset[str] = frozenset()

    @property
    def held(self) -> bool:
        now = self._clock()
        return any(entry.expires_at > now for entry in self._entries)

    def entry_for(self, repo: str) -> ReadEntry | None:
        wanted, now = repo.lower(), self._clock()
        for entry in self._entries:
            if entry.forge == "github" and entry.expires_at > now and wanted in entry.repos:
                return entry
        return None

    def gitlab_entry(self, project_id: int) -> ReadEntry | None:
        now = self._clock()
        for entry in self._entries:
            if (entry.forge, entry.project_id) == ("gitlab", project_id) and entry.expires_at > now:
                return entry
        return None

    async def refresh(self, wanted: frozenset[str], *, wall_clock: timedelta) -> None:
        if not wanted:
            return
        async with self._lock:
            entries = self._entries
            if (
                entries
                and wanted == self._wanted
                and min(entry.expires_at for entry in entries) - self._clock()
                > wall_clock + MARGIN
            ):
                return
            try:
                fetched = await self._fetch()
            except Exception:
                log.warning("the assistant could not get read-only GitHub access", exc_info=True)
                return
            self._entries, self._wanted = tuple(fetched), wanted


class OrganizationTokens:
    """One `ReadTokenCache` per organization, made on its first turn. Keyed by id; the slug only
    builds the fetch, and a slug never changes."""

    def __init__(self, fetch_for: Callable[[str], Fetch]) -> None:
        self._fetch_for = fetch_for
        self._caches: dict[int, ReadTokenCache] = {}

    def for_organization(self, organization_id: int, slug: str) -> ReadTokenCache:
        cache = self._caches.get(organization_id)
        if cache is None:
            cache = self._caches[organization_id] = ReadTokenCache(self._fetch_for(slug))
        return cache


def _expiry(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def api_fetch(
    settings: Callable[[], Settings],
    *,
    org: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Fetch:
    """Ask the API for `org`'s entries, with the API token this process holds. Never through a
    proxy: the API is on this machine."""

    async def fetch() -> list[ReadEntry]:
        current = settings()
        api_url = current.assistant.api_url.rstrip("/")
        async with httpx.AsyncClient(
            base_url=api_url, timeout=FETCH_TIMEOUT, trust_env=False, transport=transport
        ) as client:
            response = await client.post(
                TOKEN_PATH.format(org=org),
                headers={"Authorization": f"Bearer {current.api_token.get_secret_value()}"},
            )
        if response.is_error:
            raise httpx.HTTPStatusError(
                f"the API refused the read-only token: {response.status_code} "
                f"{response.text[:500]}",
                request=response.request,
                response=response,
            )
        entries: list[ReadEntry] = []
        for item in response.json():
            base_url = str(item["base_url"]).rstrip("/")
            gitlab = item.get("forge") == "gitlab"
            if gitlab and item["token"]:
                log.warning("the API handed a GitLab entry a token; it is dropped")
                continue
            # A tokenless entry is read with the API token, which must reach this API alone.
            if not item["token"] and not base_url.startswith(f"{api_url}/"):
                log.warning("the API named a read proxy that is not its own: %s", base_url)
                continue
            entries.append(
                ReadEntry(
                    repos=frozenset(str(repo).lower() for repo in item["repos"]),
                    token=SecretStr(item["token"]),
                    expires_at=_expiry(item["expires_at"]),
                    base_url=base_url,
                    forge="gitlab" if gitlab else "github",
                    project_id=item.get("project_id"),
                    bot_login=str(item.get("bot_login") or ""),
                )
            )
        return entries

    return fetch


class _BearerAuth(httpx.Auth):
    """Read per request, so a rotated API token reaches the next one."""

    def __init__(self, token: Callable[[], str]) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {self._token()}"
        yield request


class _ApiBearer(TokenAuthStrategy):
    """githubkit sends a token as `token <value>`; the API's `require_bearer` wants `Bearer`."""

    def get_auth_flow(self, github: Any) -> httpx.Auth:
        return _BearerAuth(lambda: self.token)


class ReadTokenClient(GitHubClient):
    """A `GitHubClient` on the cached entries, for a process with no GitHub credential. Built on
    no connection, so `installation_access` and `mint_run_token` refuse."""

    def __init__(self, cache: ReadTokenCache, api_token: Callable[[], str]) -> None:
        super().__init__(None)
        self._cache = cache
        self._api_token = api_token
        #: An entry's base URL and repos → (credential, client): rebuilt when its token changes.
        self._readers: dict[tuple[str, frozenset[str]], tuple[str, GitHub[TokenAuthStrategy]]] = {}

    def _backoff_key(self, repo: str) -> Hashable:
        """Each entry is its own credential: an App's token on one owner, or one project's
        proxy, so one project's spent limit never refuses another's reads."""
        entry = self._cache.entry_for(repo)
        return None if entry is None else (entry.base_url, entry.repos)

    async def api(self, repo: str) -> GitHub[TokenAuthStrategy]:
        entry = self._cache.entry_for(repo)
        if entry is None:
            raise Unavailable(NO_TOKEN)
        key = (entry.base_url, entry.repos)
        self._check_backoff(key)
        token = entry.token.get_secret_value()
        credential = token or self._api_token()
        cached = self._readers.get(key)
        if cached is None or cached[0] != credential:
            reader = (
                self._github(token, base_url=entry.base_url, backoff=key)
                if token
                else self._github(
                    _ApiBearer(credential), base_url=entry.base_url, trust_env=False, backoff=key
                )
            )
            cached = self._readers[key] = (credential, reader)
        return cached[1]


class ProxyGitLabClient(GitLabClient):
    """A `GitLabClient` on one GitLab entry: the API's read proxy for that project, called with
    the API token. It holds no GitLab credential, and sends the API token to the API alone."""

    def __init__(
        self,
        entry: ReadEntry,
        api_token: Callable[[], str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            GitLabSettings(base_url=entry.base_url, bot_login=entry.bot_login, api_prefix="")
        )
        self._api_token = api_token
        self._transport = transport

    async def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.api_url,
                auth=_BearerAuth(self._api_token),
                timeout=30.0,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._http


def tool_deps(
    settings: Callable[[], Settings],
    sessions: Sessions,
    cache: ReadTokenCache,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> McpDeps:
    """The ports the assistant's tools run on: GitHub and GitLab reads on the cached entries,
    and no writer, so nothing in this process can write to either. `transport` is the GitLab
    clients', for a test."""

    def api_token() -> str:
        return settings().api_token.get_secret_value()

    client = ReadTokenClient(cache, api_token)
    contents = RepoContents(client)
    #: Project id -> its client, rebuilt when its entry names another proxy or bot login.
    gitlab_clients: dict[int, ProxyGitLabClient] = {}

    async def github(project: Project) -> ApiGitHub | None:
        if not cache.held:
            return None
        return ApiGitHub(client=client, contents=contents, bot_login=project_bot_login(project))

    async def config_source(_: Project) -> RepoConfigSource | None:
        return RepoConfigSource(contents) if cache.held else None

    async def repo_reader(_: Project) -> RepoReader | None:
        return partial(GitHubStateSource, client) if cache.held else None

    async def gitlab(project: Project) -> GitLabClient | None:
        entry = cache.gitlab_entry(project.id)
        if entry is None:
            return None
        cached = gitlab_clients.get(project.id)
        if cached is None or (cached.settings.base_url, cached.settings.bot_login) != (
            entry.base_url,
            entry.bot_login,
        ):
            cached = gitlab_clients[project.id] = ProxyGitLabClient(entry, api_token, transport)
        return cached

    async def nothing(_: Project) -> None:
        return None

    return McpDeps(
        settings=settings,
        sessions=sessions,
        issue_writer=nothing,
        commit_writer=nothing,
        github=github,
        config_source=config_source,
        repo_reader=repo_reader,
        github_missing=NO_TOKEN,
        gitlab=gitlab,
        gitlab_missing=NO_GITLAB,
    )
