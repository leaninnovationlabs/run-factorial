"""The assistant's side of its read-only GitHub access: the cache a turn refreshes, the client its
tools read with, and ports with no writer (design.md §7.18). No database, no network."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from githubkit.exception import RateLimitExceeded
from pydantic import SecretStr

from orchestrator.api.services.errors import Unavailable
from orchestrator.assistant.github_token import (
    MARGIN,
    NO_TOKEN,
    ReadEntry,
    ReadTokenCache,
    ReadTokenClient,
    api_fetch,
    factory_repos_from_rows,
    tool_deps,
)
from orchestrator.db.models import Project, TicketSourceKind
from orchestrator.github import GitHubStateSource
from orchestrator.github.client import GITHUB_API_URL, RateLimited
from orchestrator.settings import AssistantProcessSettings
from tests.api_harness import TOKEN, make_settings
from tests.assistant_fakes import FakeClock

WALL = timedelta(minutes=5)
REPOS = frozenset({"acme/api"})
PROXY = "http://127.0.0.1:8000/factorial/api/assistant/github/projects/7"


class Fetches:
    def __init__(
        self, clock: FakeClock, *, fails: bool = False, lifetime: timedelta = timedelta(hours=1)
    ) -> None:
        self.clock, self.fails, self.lifetime = clock, fails, lifetime
        self.count = 0

    async def __call__(self) -> list[ReadEntry]:
        self.count += 1
        await asyncio.sleep(0)
        if self.fails:
            raise RuntimeError("the API is down")
        return [
            ReadEntry(
                repos=REPOS,
                token=SecretStr(f"ghs_{self.count}"),
                expires_at=self.clock() + self.lifetime,
                base_url=GITHUB_API_URL,
            )
        ]


def _cache(clock: FakeClock, fetches: Fetches) -> ReadTokenCache:
    return ReadTokenCache(fetches, clock=clock)


def _token(cache: ReadTokenCache) -> str | None:
    entry = cache.entry_for("acme/api")
    return None if entry is None else entry.token.get_secret_value()


async def test_a_fresh_token_is_reused_until_the_margin() -> None:
    clock = FakeClock()
    fetches = Fetches(clock)
    cache = _cache(clock, fetches)

    await cache.refresh(REPOS, wall_clock=WALL)
    await cache.refresh(REPOS, wall_clock=WALL)
    assert (fetches.count, _token(cache)) == (1, "ghs_1")

    clock.advance(seconds=(timedelta(hours=1) - WALL - MARGIN).total_seconds() + 1)
    await cache.refresh(REPOS, wall_clock=WALL)
    assert (fetches.count, _token(cache)) == (2, "ghs_2")


async def test_a_new_project_fetches_again() -> None:
    clock = FakeClock()
    fetches = Fetches(clock)
    cache = _cache(clock, fetches)
    await cache.refresh(REPOS, wall_clock=WALL)
    await cache.refresh(REPOS | {"acme/new"}, wall_clock=WALL)
    assert fetches.count == 2


async def test_no_project_fetches_nothing() -> None:
    clock = FakeClock()
    fetches = Fetches(clock)
    cache = _cache(clock, fetches)
    await cache.refresh(frozenset(), wall_clock=WALL)
    assert (fetches.count, cache.held) == (0, False)


async def test_a_failed_fetch_keeps_the_token_a_running_turn_reads() -> None:
    clock = FakeClock()
    fetches = Fetches(clock)
    cache = _cache(clock, fetches)
    await cache.refresh(REPOS, wall_clock=WALL)
    fetches.fails = True
    await cache.refresh(REPOS | {"acme/new"}, wall_clock=WALL)
    assert _token(cache) == "ghs_1"


async def test_an_expired_token_is_no_token() -> None:
    clock = FakeClock()
    cache = _cache(clock, Fetches(clock))
    await cache.refresh(REPOS, wall_clock=WALL)
    clock.advance(hours=1)
    assert (_token(cache), cache.held) == (None, False)


async def test_concurrent_refreshes_fetch_once() -> None:
    clock = FakeClock()
    fetches = Fetches(clock)
    cache = _cache(clock, fetches)
    await asyncio.gather(*(cache.refresh(REPOS, wall_clock=WALL) for _ in range(3)))
    assert (fetches.count, _token(cache)) == (1, "ghs_1")


async def test_the_client_reads_on_the_cached_token_and_never_mints() -> None:
    clock = FakeClock()
    cache = _cache(clock, Fetches(clock))
    client = ReadTokenClient(cache, lambda: TOKEN)
    with pytest.raises(Unavailable, match="no GitHub token"):
        await client.api("acme/api")
    await cache.refresh(REPOS, wall_clock=WALL)
    with pytest.raises(Unavailable, match="no GitHub token"):
        await client.api("acme/elsewhere")
    with pytest.raises(RuntimeError, match="no GitHub connection"):
        await client.mint_run_token("acme/api", {"contents": "read"})


async def test_the_client_reads_each_repo_at_its_entry_s_base_url() -> None:
    """An App project's repo is read on api.github.com with its minted token; a token project's
    through the API's proxy, with the API token as a bearer and no GitHub token at all."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    clock = FakeClock()

    async def fetch() -> list[ReadEntry]:
        until = clock() + timedelta(hours=1)
        return [
            ReadEntry(frozenset({"acme/app"}), SecretStr("ghs_minted"), until, GITHUB_API_URL),
            ReadEntry(frozenset({"acme/token"}), SecretStr(""), until, PROXY),
        ]

    cache = ReadTokenCache(fetch, clock=clock)
    await cache.refresh(frozenset({"acme/app", "acme/token"}), wall_clock=WALL)
    client = ReadTokenClient(cache, lambda: TOKEN)
    client._transport = httpx.MockTransport(answer)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    await (await client.api("acme/app")).rest.repos.async_get("acme", "app")
    await (await client.api("ACME/Token")).rest.repos.async_get("acme", "token")

    app_read, token_read = seen
    assert str(app_read.url) == f"{GITHUB_API_URL}/repos/acme/app"
    assert app_read.headers["authorization"] == "token ghs_minted"
    assert str(token_read.url) == f"{PROXY}/repos/acme/token"
    assert token_read.headers["authorization"] == f"Bearer {TOKEN}"


async def test_one_project_s_spent_limit_never_refuses_another_project_s_reads() -> None:
    """The process holds one read client for every project; one backoff on it refused every
    project's reads until the spent one's limit reset."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repos/acme/spent"):
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 3600),
                },
            )
        return httpx.Response(200, json={})

    clock = FakeClock()

    async def fetch() -> list[ReadEntry]:
        until = clock() + timedelta(hours=1)
        return [
            ReadEntry(frozenset({"acme/spent"}), SecretStr("ghs_one"), until, GITHUB_API_URL),
            ReadEntry(frozenset({"other/fresh"}), SecretStr("ghs_two"), until, GITHUB_API_URL),
        ]

    cache = ReadTokenCache(fetch, clock=clock)
    await cache.refresh(frozenset({"acme/spent", "other/fresh"}), wall_clock=WALL)
    client = ReadTokenClient(cache, lambda: TOKEN)
    client._transport = httpx.MockTransport(answer)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    with pytest.raises(RateLimitExceeded):
        await (await client.api("acme/spent")).rest.repos.async_get("acme", "spent")

    with pytest.raises(RateLimited):
        await client.api("acme/spent")
    await (await client.api("other/fresh")).rest.repos.async_get("other", "fresh")


async def test_the_ports_have_no_writer_and_no_reader_without_a_token() -> None:
    clock = FakeClock()
    cache = _cache(clock, Fetches(clock))
    settings = make_settings()
    project = Project(name="p", github_repo="acme/api")
    deps = tool_deps(lambda: settings, sessions=None, cache=cache)  # type: ignore[arg-type]
    assert await deps.issue_writer(project) is None
    assert await deps.commit_writer(project) is None
    assert await deps.github(project) is None
    assert await deps.config_source(project) is None
    assert await deps.repo_reader(project) is None
    assert deps.github_missing == NO_TOKEN
    await cache.refresh(REPOS, wall_clock=WALL)
    github = await deps.github(project)
    assert github is not None and isinstance(github.client, ReadTokenClient)
    assert await deps.config_source(project) is not None
    reader = await deps.repo_reader(project)
    assert reader is not None
    assert isinstance(reader("acme/api", 7), GitHubStateSource)
    assert await deps.issue_writer(project) is None
    assert await deps.commit_writer(project) is None


async def test_the_fetch_asks_the_api_on_loopback_with_the_api_token() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        until = datetime(2026, 10, 13, 13, 0, tzinfo=UTC).isoformat()
        return httpx.Response(
            200,
            json=[
                {
                    "token": "ghs_api",
                    "expires_at": until,
                    "repos": ["Acme/API"],
                    "base_url": GITHUB_API_URL,
                },
                {"token": "", "expires_at": until, "repos": ["acme/tok"], "base_url": PROXY},
            ],
        )

    settings = make_settings()
    assert AssistantProcessSettings().api_url == "http://127.0.0.1:8000"
    entries = await api_fetch(
        lambda: settings, org="acme", transport=httpx.MockTransport(answer)
    )()

    [request] = seen
    assert str(request.url) == "http://127.0.0.1:8000/factorial/api/orgs/acme/assistant/github-token"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert [(e.token.get_secret_value(), e.repos, e.base_url) for e in entries] == [
        ("ghs_api", frozenset({"acme/api"}), GITHUB_API_URL),
        ("", frozenset({"acme/tok"}), PROXY),
    ]


async def test_a_tokenless_entry_elsewhere_than_the_api_is_dropped() -> None:
    """A tokenless entry is read with the API token, so it may name this API alone."""

    def answer(_: httpx.Request) -> httpx.Response:
        body = [
            {
                "token": "",
                "expires_at": "2026-10-13T13:00:00+00:00",
                "repos": ["acme/api"],
                "base_url": GITHUB_API_URL,
            }
        ]
        return httpx.Response(200, json=body)

    settings = make_settings()
    fetch = api_fetch(lambda: settings, org="acme", transport=httpx.MockTransport(answer))
    assert list(await fetch()) == []


async def test_a_refused_fetch_raises() -> None:
    settings = make_settings()
    fetch = api_fetch(
        lambda: settings,
        org="default",
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": "no"})),
    )
    with pytest.raises(httpx.HTTPStatusError, match=r"503.*no"):
        await fetch()


async def test_an_offset_less_expiry_reads_as_utc() -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        body = [
            {
                "token": "ghs_api",
                "expires_at": "2026-10-13T13:00:00",
                "repos": ["acme/api"],
                "base_url": GITHUB_API_URL,
            }
        ]
        return httpx.Response(200, json=body)

    settings = make_settings()
    [entry] = await api_fetch(
        lambda: settings, org="default", transport=httpx.MockTransport(answer)
    )()
    assert entry.expires_at == datetime(2026, 10, 13, 13, 0, tzinfo=UTC)


def test_factory_repos_count_gitlab_projects_too() -> None:
    """A GitLab project read through the API is one a turn refreshes for: an organization with
    GitLab projects alone must still fetch its entries."""
    repos = factory_repos_from_rows(
        [
            ("acme/api", TicketSourceKind.GITHUB, "acme/intake"),
            ("group/app", TicketSourceKind.GITLAB, "group/issues"),
            ("acme/other", None, None),
            ("acme/jira", TicketSourceKind.JIRA, "ENG"),
        ]
    )
    assert repos == frozenset(
        {"acme/api", "acme/intake", "group/app", "group/issues", "acme/other", "acme/jira"}
    )


GITLAB_PROXY = "http://127.0.0.1:8000/factorial/api/assistant/gitlab/projects/9"


def _gitlab_entry(clock: FakeClock, *, base_url: str = GITLAB_PROXY) -> ReadEntry:
    return ReadEntry(
        frozenset({"acme/api"}),
        SecretStr(""),
        clock() + timedelta(hours=1),
        base_url,
        forge="gitlab",
        project_id=9,
        bot_login="factorial-bot",
    )


async def test_the_fetch_reads_a_gitlab_entry_and_drops_one_elsewhere_than_the_api() -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        until = "2026-10-13T13:00:00+00:00"
        gitlab = {"forge": "gitlab", "bot_login": "factorial-bot", "token": "", "expires_at": until}
        return httpx.Response(
            200,
            json=[
                gitlab | {"repos": ["Group/App"], "project_id": 9, "base_url": GITLAB_PROXY},
                gitlab
                | {"repos": ["group/x"], "project_id": 10, "base_url": "https://gitlab.invalid"},
                # A token would be a GitLab credential, which this process never holds.
                gitlab
                | {"repos": ["group/y"], "project_id": 11, "token": "glpat-x", "base_url": PROXY},
            ],
        )

    settings = make_settings()
    entries = await api_fetch(
        lambda: settings, org="acme", transport=httpx.MockTransport(answer)
    )()

    [entry] = entries
    assert (entry.forge, entry.project_id, entry.bot_login, entry.repos, entry.base_url) == (
        "gitlab",
        9,
        "factorial-bot",
        frozenset({"group/app"}),
        GITLAB_PROXY,
    )


async def test_a_gitlab_entry_never_answers_a_github_read() -> None:
    """GitHub and GitLab can name the same path; a GitHub read must not reach the GitLab proxy."""
    clock = FakeClock()

    async def fetch() -> list[ReadEntry]:
        return [_gitlab_entry(clock)]

    cache = ReadTokenCache(fetch, clock=clock)
    await cache.refresh(REPOS, wall_clock=WALL)
    assert cache.entry_for("acme/api") is None
    assert cache.gitlab_entry(9) == _gitlab_entry(clock)
    assert cache.gitlab_entry(10) is None


async def test_the_gitlab_port_reads_through_the_api_proxy_with_the_api_token() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"default_branch": "main"})

    clock = FakeClock()

    async def fetch() -> list[ReadEntry]:
        return [_gitlab_entry(clock)]

    cache = ReadTokenCache(fetch, clock=clock)
    settings = make_settings()
    deps = tool_deps(
        lambda: settings,
        sessions=None,  # type: ignore[arg-type]
        cache=cache,
        transport=httpx.MockTransport(answer),
    )
    project = Project(id=9, name="p", github_repo="group/app")
    assert await deps.gitlab(project) is None

    await cache.refresh(REPOS, wall_clock=WALL)
    gitlab = await deps.gitlab(project)
    assert gitlab is not None
    assert await deps.gitlab(Project(id=10, name="q", github_repo="group/q")) is None
    assert gitlab.settings.bot_login == "factorial-bot"
    assert await gitlab.get("/projects/group%2Fapp") == {"default_branch": "main"}

    [request] = seen
    assert str(request.url) == f"{GITLAB_PROXY}/projects/group%2Fapp"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "private-token" not in request.headers
