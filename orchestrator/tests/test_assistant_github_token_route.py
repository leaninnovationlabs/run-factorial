"""`POST /api/orgs/{org}/assistant/github-token` and the read proxy: the API hands the assistant
process read-only GitHub access for every repo a chat tool in the organization may read, one
entry per App and owner, and only for the API bearer (design.md §7.18). A token project's
entry is the API's GET-only read proxy, which adds the stored token here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Iterator
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    ApiGitHub,
    github_for_dependency,
    github_proxy_http_dependency,
    read_token_minter_dependency,
)
from orchestrator.api.routes import assistant as assistant_routes
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import Organization, Project, TicketSource, TicketSourceKind
from orchestrator.github import client as github_client_module
from orchestrator.github.client import (
    GITHUB_API_URL,
    GitHubClient,
    ReadToken,
    TokenScopeNotGranted,
)
from orchestrator.github.connection import GitHubConnection
from orchestrator.github.contents import RepoContents
from orchestrator.github.pool import pool
from orchestrator.gitlab.project_client import TOKEN_SECRET as GITLAB_TOKEN
from orchestrator.secrets import store
from tests.api_harness import (
    TOKEN,
    build_app,
    client_for,
    make_settings,
    seed_connected,
    seed_project,
)
from tests.members import ensure_member

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

PATH = "/api/orgs/default/assistant/github-token"
UNTIL = datetime(2026, 10, 13, 13, 0, tzinfo=UTC)
BEARER = {"Authorization": f"Bearer {TOKEN}"}
#: What `seed_connected` stores as a token project's credential.
STORED = "credential"


class FakeMint:
    """`refuses` fails every mint; `refuses_owner` only those over that owner's repos. With
    `together`, each mint waits until that many are in flight at once."""

    def __init__(
        self,
        *,
        refuses: Exception | None = None,
        refuses_owner: str | None = None,
        together: int = 0,
    ) -> None:
        self.refuses = refuses
        self.refuses_owner = refuses_owner
        self.wanted: list[frozenset[str]] = []
        self.clients: list[GitHubClient] = []
        self._together = together
        self._all_in = asyncio.Event()

    async def __call__(self, client: GitHubClient, wanted: Collection[str]) -> ReadToken:
        self.wanted.append(frozenset(wanted))
        self.clients.append(client)
        if self._together:
            if len(self.wanted) >= self._together:
                self._all_in.set()
            await asyncio.wait_for(self._all_in.wait(), timeout=5)
        if self.refuses is not None:
            raise self.refuses
        if self.refuses_owner and any(r.startswith(f"{self.refuses_owner}/") for r in wanted):
            raise TokenScopeNotGranted(f"GitHub granted {self.refuses_owner} contents: write")
        return ReadToken(SecretStr("ghs_read"), UNTIL, tuple(sorted(wanted)))


def _github(mode: Literal["app", "token"]) -> ApiGitHub:
    connection = GitHubConnection(
        mode=mode,
        app_id="7" if mode == "app" else "",
        private_key=SecretStr("key") if mode == "app" else None,
        token=SecretStr(STORED) if mode == "token" else None,
        webhook_secret=None,
        bot_login="factorial-test[bot]",
        installations={},
    )
    client = GitHubClient(connection)
    return ApiGitHub(client=client, contents=RepoContents(client), bot_login=connection.bot_login)


def _app(
    session: AsyncSession,
    mint: FakeMint,
    mode_of: Callable[[Project], Literal["app", "token"] | None] = lambda _: "app",
) -> FastAPI:
    app = build_app(make_settings(), session)
    by_mode = {"app": _github("app"), "token": _github("token")}

    async def github_for(project: Project) -> ApiGitHub | None:
        mode = mode_of(project)
        return None if mode is None else by_mode[mode]

    app.dependency_overrides[read_token_minter_dependency] = lambda: mint
    app.dependency_overrides[github_for_dependency] = lambda: github_for
    return app


async def _organization(session: AsyncSession) -> Organization:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Acme", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization


async def _seed(session: AsyncSession) -> None:
    own = await seed_project(session, "acme/route-own")
    session.add(
        TicketSource(
            project_id=own.id, kind=TicketSourceKind.GITHUB, external_ref="acme/route-intake"
        )
    )
    jira = await seed_project(session, "acme/route-jira")
    session.add(TicketSource(project_id=jira.id, kind=TicketSourceKind.JIRA, external_ref="ENG"))
    await session.flush()


async def test_the_route_mints_over_every_project_and_intake_repo(
    async_session: AsyncSession,
) -> None:
    await _seed(async_session)
    mint = FakeMint()

    async with client_for(_app(async_session, mint)) as client:
        response = await client.post(PATH, headers=BEARER)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    entries = response.json()
    assert {entry["token"] for entry in entries} == {"ghs_read"}
    assert {entry["base_url"] for entry in entries} == {GITHUB_API_URL}
    assert {datetime.fromisoformat(entry["expires_at"]) for entry in entries} == {UNTIL}
    wanted = frozenset().union(*mint.wanted)
    assert {"acme/route-own", "acme/route-intake", "acme/route-jira"} <= wanted
    assert "ENG" not in wanted
    assert {repo for entry in entries for repo in entry["repos"]} == wanted


async def test_projects_on_one_app_and_owner_share_one_token_and_owners_mint_at_once(
    async_session: AsyncSession,
) -> None:
    """A mint covers one installation, so one owner. Projects on one App under one owner get
    one token over all their repos, and the owners' mints are in flight together."""
    organization = await _organization(async_session)
    split = await seed_project(async_session, "acme/split", organization_id=organization.id)
    async_session.add(
        TicketSource(project_id=split.id, kind=TicketSourceKind.GITHUB, external_ref="other/intake")
    )
    await seed_project(async_session, "acme/plain", organization_id=organization.id)
    await async_session.flush()
    mint = FakeMint(together=2)

    async with client_for(_app(async_session, mint)) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    assert sorted(mint.wanted, key=sorted) == [
        frozenset({"acme/plain", "acme/split"}),
        frozenset({"other/intake"}),
    ]
    assert sorted(entry["repos"] for entry in response.json()) == [
        ["acme/plain", "acme/split"],
        ["other/intake"],
    ]


async def test_projects_on_different_apps_mint_apart(async_session: AsyncSession) -> None:
    organization = await _organization(async_session)
    first = await seed_project(async_session, "acme/first-app", organization_id=organization.id)
    await seed_project(async_session, "acme/second-app", organization_id=organization.id)
    mint = FakeMint()
    app = _app(async_session, mint)
    shared, other = _github("app"), _github("app")

    async def github_for(project: Project) -> ApiGitHub | None:
        return other if project.id == first.id else shared

    app.dependency_overrides[github_for_dependency] = lambda: github_for

    async with client_for(app) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    assert sorted(mint.wanted, key=sorted) == [
        frozenset({"acme/first-app"}),
        frozenset({"acme/second-app"}),
    ]
    assert {id(client) for client in mint.clients} == {id(other.client), id(shared.client)}


async def test_one_refused_group_costs_only_its_own_entries(
    async_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    organization = await _organization(async_session)
    split = await seed_project(async_session, "acme/refused", organization_id=organization.id)
    async_session.add(
        TicketSource(project_id=split.id, kind=TicketSourceKind.GITHUB, external_ref="other/kept")
    )
    await async_session.flush()
    mint = FakeMint(refuses_owner="acme")

    async with client_for(_app(async_session, mint)) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    assert [entry["repos"] for entry in response.json()] == [["other/kept"]]
    assert any(
        "assistant read-only token refused" in record.getMessage()
        and f"project {split.id}" in record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    )


async def test_a_token_project_s_entry_points_at_the_proxy(async_session: AsyncSession) -> None:
    organization = await _organization(async_session)
    by_token = await seed_project(async_session, "acme/by-token", organization_id=organization.id)
    await seed_project(async_session, "acme/by-app", organization_id=organization.id)
    mint = FakeMint()
    app = _app(
        async_session, mint, lambda project: "token" if project.id == by_token.id else "app"
    )

    async with client_for(app) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    [proxied] = [entry for entry in response.json() if entry["repos"] == ["acme/by-token"]]
    assert proxied["token"] == ""
    assert proxied["base_url"] == (
        f"http://127.0.0.1:8000/factorial/api/assistant/github/projects/{by_token.id}"
    )
    assert mint.wanted == [frozenset({"acme/by-app"})]


@pytest.mark.usefixtures("master_key")
async def test_a_gitlab_project_s_entry_is_the_gitlab_read_proxy(
    async_session: AsyncSession,
) -> None:
    """The assistant's process cannot open a GitLab token, so the API reads for it, and names
    the bot login the tools read, which that process has no other way to learn."""
    organization = await _organization(async_session)
    tag = uuid4().hex[:8]
    connected = await seed_project(
        async_session, f"group/gl-{tag}", organization_id=organization.id
    )
    unconfigured = await seed_project(
        async_session, f"group/dark-{tag}", organization_id=organization.id
    )
    for project in (connected, unconfigured):
        project.settings = {
            "gitlab": {"base_url": "https://gitlab.invalid", "bot_login": "factorial-bot"}
        }
    async_session.add_all(
        [
            TicketSource(
                project_id=connected.id,
                kind=TicketSourceKind.GITLAB,
                external_ref=f"group/intake-{tag}",
            ),
            TicketSource(
                project_id=unconfigured.id,
                kind=TicketSourceKind.GITLAB,
                external_ref=unconfigured.github_repo,
            ),
        ]
    )
    await async_session.flush()
    await store.put(async_session, connected.id, GITLAB_TOKEN, "glpat-stored")
    mint = FakeMint()

    async with client_for(_app(async_session, mint)) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    [entry] = response.json()
    assert entry["repos"] == [connected.github_repo, f"group/intake-{tag}"]
    assert (entry["forge"], entry["project_id"], entry["bot_login"], entry["token"]) == (
        "gitlab",
        connected.id,
        "factorial-bot",
        "",
    )
    assert entry["base_url"] == (
        f"http://127.0.0.1:8000/factorial/api/assistant/gitlab/projects/{connected.id}"
    )
    assert "glpat-stored" not in response.text
    assert mint.wanted == []


async def test_a_gitlab_only_refusal_does_not_blame_github(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = await _organization(async_session)
    project = await seed_project(
        async_session, f"group/gl-{uuid4().hex[:8]}", organization_id=organization.id
    )
    async_session.add(
        TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITLAB, external_ref=project.github_repo
        )
    )
    await async_session.flush()

    async def unreadable(_: AsyncSession, __: Project) -> None:
        raise RuntimeError("the stored secret could not be decrypted")

    monkeypatch.setattr(assistant_routes, "settings_for_project", unreadable)

    async with client_for(_app(async_session, FakeMint(), lambda _: None)) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        f"Factorial has no read-only access: Project {project.id}'s GitLab connection could not"
        " be read."
    )


async def test_only_the_api_bearer_gets_a_token(async_session: AsyncSession) -> None:
    mint = FakeMint()
    # A member's session, so the guard admits it and the route itself must refuse it.
    await ensure_member(async_session, "operator@example.com")
    cookie = mint_session(
        "operator@example.com", verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=3600
    )
    async with client_for(_app(async_session, mint)) as client:
        nothing = await client.post(PATH)
        client.cookies.set(SESSION_COOKIE, cookie)
        signed_in = await client.post(PATH, headers={"Sec-Fetch-Site": "same-origin"})
        client.cookies.clear()
        wrong = await client.post(PATH, headers={"Authorization": "Bearer not-the-token"})

    assert (nothing.status_code, signed_in.status_code, wrong.status_code) == (401, 401, 401)
    assert mint.wanted == []


async def test_a_project_without_github_gets_no_entry(async_session: AsyncSession) -> None:
    organization = await _organization(async_session)
    await seed_project(async_session, "acme/dark", organization_id=organization.id)
    mint = FakeMint()
    async with client_for(_app(async_session, mint, lambda _: None)) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )
    assert (response.status_code, response.json(), mint.wanted) == (200, [], [])


async def test_a_refused_mint_answers_503_with_the_reason(
    async_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await _seed(async_session)
    mint = FakeMint(refuses=TokenScopeNotGranted("GitHub granted contents: write"))
    async with client_for(_app(async_session, mint)) as client:
        response = await client.post(PATH, headers=BEARER)
    assert response.status_code == 503
    assert "contents: write" in response.json()["detail"]
    assert any(
        "assistant read-only token refused" in record.getMessage()
        and "contents: write" in record.getMessage()
        and "ghs_" not in record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    )


async def test_one_project_whose_connection_cannot_be_read_costs_only_its_own_entries(
    async_session: AsyncSession,
) -> None:
    organization = await _organization(async_session)
    broken = await seed_project(async_session, "acme/broken", organization_id=organization.id)
    await seed_project(async_session, "acme/fine", organization_id=organization.id)
    mint = FakeMint()
    app = _app(async_session, mint)
    working = app.dependency_overrides[github_for_dependency]()

    async def github_for(project: Project) -> ApiGitHub | None:
        if project.id == broken.id:
            raise RuntimeError("the stored secret could not be decrypted")
        return await working(project)

    app.dependency_overrides[github_for_dependency] = lambda: github_for

    async with client_for(app) as client:
        response = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    assert response.status_code == 200, response.text
    assert [entry["repos"] for entry in response.json()] == [["acme/fine"]]


# --- the read proxy ---------------------------------------------------------------------------


class GitHubStub:
    """GitHub, as the proxy's HTTP client sees it: every request kept, one canned answer."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.response


def _proxy_app(session: AsyncSession, github: GitHubStub) -> FastAPI:
    app = build_app(make_settings(), session)

    async def http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=GITHUB_API_URL, transport=httpx.MockTransport(github)
        ) as client:
            yield client

    app.dependency_overrides[github_proxy_http_dependency] = http
    return app


def _proxy(project: Project, tail: str) -> str:
    return f"/api/assistant/github/projects/{project.id}/{tail}"


@pytest.mark.usefixtures("master_key")
async def test_the_proxy_answers_only_the_api_bearer(async_session: AsyncSession) -> None:
    project = await seed_connected(async_session, f"acme/proxy-{uuid4().hex[:8]}")
    await ensure_member(async_session, "operator@example.com")
    cookie = mint_session(
        "operator@example.com", verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=3600
    )
    github = GitHubStub(httpx.Response(200, json=[]))
    url = _proxy(project, f"repos/{project.github_repo}/pulls")
    async with client_for(_proxy_app(async_session, github)) as client:
        nothing = await client.get(url)
        client.cookies.set(SESSION_COOKIE, cookie)
        signed_in = await client.get(url)

    assert (nothing.status_code, signed_in.status_code) == (401, 401)
    assert github.seen == []


@pytest.mark.usefixtures("master_key")
async def test_the_proxy_forwards_get_for_the_project_s_repos_with_the_stored_token(
    async_session: AsyncSession,
) -> None:
    project = await seed_connected(async_session, f"acme/proxy-{uuid4().hex[:8]}")
    github = GitHubStub(
        httpx.Response(200, json=[{"number": 7}], headers={"Content-Type": "application/json"})
    )

    async with client_for(_proxy_app(async_session, github)) as client:
        response = await client.get(
            _proxy(project, f"repos/{project.github_repo}/pulls"),
            params={"state": "open", "per_page": "100"},
            headers={**BEARER, "Accept": "application/vnd.github+json"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == [{"number": 7}]
    assert response.headers["content-type"] == "application/json"
    [sent] = github.seen
    assert sent.method == "GET"
    assert str(sent.url) == (
        f"{GITHUB_API_URL}/repos/{project.github_repo}/pulls?state=open&per_page=100"
    )
    assert sent.headers["authorization"] == f"Bearer {STORED}"
    assert sent.headers["accept"] == "application/vnd.github+json"


@pytest.mark.usefixtures("master_key")
async def test_the_proxy_refuses_other_methods_and_other_repos(
    async_session: AsyncSession,
) -> None:
    project = await seed_connected(async_session, f"acme/proxy-{uuid4().hex[:8]}")
    by_app = await seed_connected(async_session, f"acme/proxy-{uuid4().hex[:8]}", app_id="7")
    github = GitHubStub(httpx.Response(200, json={}))
    own = f"repos/{project.github_repo}"

    async with client_for(_proxy_app(async_session, github)) as client:
        posted = await client.post(_proxy(project, f"{own}/issues"), headers=BEARER, json={})
        other = await client.get(_proxy(project, f"repos/{by_app.github_repo}"), headers=BEARER)
        climbed = await client.get(
            _proxy(project, f"{own}/%2E%2E/%2E%2E/{by_app.github_repo}"), headers=BEARER
        )
        outside = await client.get(_proxy(project, "user"), headers=BEARER)
        app_mode = await client.get(_proxy(by_app, f"repos/{by_app.github_repo}"), headers=BEARER)

    assert (posted.status_code, other.status_code, climbed.status_code) == (405, 403, 403)
    assert (outside.status_code, app_mode.status_code) == (403, 404)
    assert github.seen == []


@pytest.mark.usefixtures("master_key")
async def test_the_proxy_never_returns_the_token(async_session: AsyncSession) -> None:
    project = await seed_connected(async_session, f"acme/proxy-{uuid4().hex[:8]}")
    github = GitHubStub(
        httpx.Response(
            401,
            content=f'{{"message": "Bad credentials", "echo": "Bearer {STORED}"}}'.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {STORED}",
                "WWW-Authenticate": f'Bearer realm="{STORED}"',
                "X-OAuth-Scopes": "repo",
                "GitHub-Authentication-Token-Expiration": "2026-12-01 00:00:00 UTC",
            },
        )
    )

    async with client_for(_proxy_app(async_session, github)) as client:
        response = await client.get(_proxy(project, f"repos/{project.github_repo}"), headers=BEARER)

    assert response.status_code == 401
    assert STORED not in response.text
    assert STORED not in str(response.headers)
    shown = {name.lower() for name in response.headers}
    assert not shown & {
        "authorization",
        "www-authenticate",
        "x-oauth-scopes",
        "github-authentication-token-expiration",
    }


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Every request a pooled project client sends, answered 200, and a pool that forgets its
    clients on both sides, so no other test inherits this transport."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        github_client_module, "_pooled_transport", lambda: httpx.MockTransport(answer)
    )
    pool.cache_clear()
    yield seen
    pool.cache_clear()


@pytest.mark.usefixtures("master_key")
async def test_token_mode_never_calls_app_only_endpoints(
    async_session: AsyncSession, recorded: list[httpx.Request]
) -> None:
    organization = await _organization(async_session)
    project = await seed_connected(
        async_session, f"acme/token-{uuid4().hex[:8]}", organization_id=organization.id
    )
    async_session.add(
        TicketSource(project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref="acme/in")
    )
    await async_session.flush()
    app = build_app(make_settings(), async_session)
    del app.dependency_overrides[github_for_dependency]

    async with client_for(app) as client:
        readiness = await client.get(f"/api/projects/{project.id}/readiness", headers=BEARER)
        handed = await client.post(
            f"/api/orgs/{organization.slug}/assistant/github-token", headers=BEARER
        )

    paths = [request.url.path for request in recorded]
    assert not [path for path in paths if path.startswith("/app") or "installation" in path]
    assert f"/repos/{project.github_repo}" in paths and "/repos/acme/in" in paths
    assert {request.headers["authorization"] for request in recorded} == {f"token {STORED}"}
    assert readiness.status_code == 200, readiness.text
    [access] = [row for row in readiness.json()["readiness"] if row["key"] == "app_access"]
    assert access["state"] == "ok", access
    assert [entry["token"] for entry in handed.json()] == [""]
