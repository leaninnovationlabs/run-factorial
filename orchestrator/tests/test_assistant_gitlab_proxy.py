"""The assistant's GitLab read proxy (design.md §7.18): the assistant's process holds no master
key, so it reads a GitLab project through the API, which adds the project's stored token here.
GET only, only under the project's own GitLab repo and intake repo, and only for the API bearer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import gitlab_proxy_http_dependency
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import Organization, Project, TicketSource, TicketSourceKind
from orchestrator.gitlab.project_client import TOKEN_SECRET as GITLAB_TOKEN
from orchestrator.secrets import store
from orchestrator.settings import GitLabSettings
from tests.api_harness import (
    TOKEN,
    build_app,
    client_for,
    make_settings,
    seed_connected,
    seed_project,
)
from tests.members import ensure_member

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("master_key"),
]

BEARER = {"Authorization": f"Bearer {TOKEN}"}
GITLAB = "https://gitlab.invalid"


class GitLabStub:
    """GitLab, as the proxy's HTTP client sees it: every request kept, one canned answer."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.response


def _app(session: AsyncSession, gitlab: GitLabStub) -> FastAPI:
    app = build_app(make_settings(), session)

    def open_client(settings: GitLabSettings) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=settings.api_url, transport=httpx.MockTransport(gitlab))

    app.dependency_overrides[gitlab_proxy_http_dependency] = lambda: open_client
    return app


async def _gitlab_project(
    session: AsyncSession,
    *,
    token: str | None = "glpat-ours",
    intake: str | None = None,
    organization_id: int | None = None,
) -> Project:
    project = await seed_project(
        session, f"group/gl-{uuid4().hex[:8]}", organization_id=organization_id
    )
    project.settings = {"gitlab": {"base_url": GITLAB, "bot_login": "factorial-bot"}}
    session.add(
        TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITLAB,
            external_ref=intake or project.github_repo,
        )
    )
    await session.flush()
    if token is not None:
        await store.put(session, project.id, GITLAB_TOKEN, token)
    return project


def _encoded(repo: str) -> str:
    return repo.replace("/", "%2F")


def _proxy(project: Project, tail: str) -> str:
    return f"/api/assistant/gitlab/projects/{project.id}/{tail}"


def _own(project: Project, tail: str = "") -> str:
    return _proxy(project, f"projects/{_encoded(project.github_repo)}{tail}")


async def test_the_proxy_answers_only_the_api_bearer(async_session: AsyncSession) -> None:
    project = await _gitlab_project(async_session)
    await ensure_member(async_session, "operator@example.com")
    cookie = mint_session(
        "operator@example.com", verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=3600
    )
    gitlab = GitLabStub(httpx.Response(200, json={}))
    async with client_for(_app(async_session, gitlab)) as client:
        nothing = await client.get(_own(project))
        client.cookies.set(SESSION_COOKIE, cookie)
        signed_in = await client.get(_own(project))
        client.cookies.clear()
        wrong = await client.get(_own(project), headers={"Authorization": "Bearer not-the-token"})

    assert (nothing.status_code, signed_in.status_code, wrong.status_code) == (401, 401, 401)
    assert gitlab.seen == []


async def test_the_proxy_forwards_a_get_for_the_project_s_repos_with_its_stored_token(
    async_session: AsyncSession,
) -> None:
    project = await _gitlab_project(async_session, intake="group/intake")
    gitlab = GitLabStub(
        httpx.Response(
            200,
            json=[{"iid": 7}],
            headers={
                "Content-Type": "application/json",
                "X-Next-Page": "3",
                "X-Page": "2",
                "X-Per-Page": "100",
                "X-Total-Pages": "4",
                "Link": f'<{GITLAB}/api/v4/projects/1/issues?page=3>; rel="next"',
                "Set-Cookie": "_gitlab_session=abc",
            },
        )
    )

    async with client_for(_app(async_session, gitlab)) as client:
        file = await client.get(
            _own(project, "/repository/files/.factorial%2Fprocess.yaml"),
            params={"ref": "main"},
            headers=BEARER,
        )
        intake = await client.get(
            _proxy(project, "projects/group%2Fintake/issues"),
            params={"state": "opened", "page": "2", "per_page": "100"},
            headers=BEARER,
        )

    assert (file.status_code, intake.status_code) == (200, 200), intake.text
    assert intake.json() == [{"iid": 7}]
    assert intake.headers["content-type"] == "application/json"
    assert (intake.headers["x-next-page"], intake.headers["x-total-pages"]) == ("3", "4")
    # Link names the GitLab host, which stays in the API.
    assert "link" not in intake.headers and "set-cookie" not in intake.headers
    sent_file, sent_intake = gitlab.seen
    assert str(sent_file.url) == (
        f"{GITLAB}/api/v4/projects/{_encoded(project.github_repo)}"
        "/repository/files/.factorial%2Fprocess.yaml?ref=main"
    )
    assert str(sent_intake.url) == (
        f"{GITLAB}/api/v4/projects/group%2Fintake/issues?state=opened&page=2&per_page=100"
    )
    for sent in gitlab.seen:
        assert sent.method == "GET"
        assert sent.headers["private-token"] == "glpat-ours"
        assert "authorization" not in sent.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_the_proxy_refuses_every_method_but_get(
    async_session: AsyncSession, method: str
) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(httpx.Response(200, json={}))
    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.request(method, _own(project, "/issues"), headers=BEARER)
    assert response.status_code == 405
    assert gitlab.seen == []


async def _other_organization(session: AsyncSession) -> Organization:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Other", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization


async def test_the_proxy_reads_only_the_project_s_own_repos(async_session: AsyncSession) -> None:
    project = await _gitlab_project(async_session)
    other = await _organization_s_project(async_session)
    own = f"projects/{_encoded(project.github_repo)}"
    gitlab = GitLabStub(httpx.Response(200, json={}))
    tails = {
        "another organization's project": f"projects/{_encoded(other.github_repo)}",
        "a slash the path decoder would split": f"projects/{project.github_repo}",
        "a double-encoded slash": f"projects/{project.github_repo.replace('/', '%252F')}",
        "a numeric project id": "projects/1",
        "outside every project": "user",
        "the projects listing": "projects",
        "an encoded climb": f"{own}/%2E%2E/%2E%2E/{_encoded(other.github_repo)}",
        "a climb inside a segment": f"{own}/repository/files/..%2F..%2Fsecrets",
        "a dot segment": f"{own}/%2E/issues",
        "an empty segment": f"{own}//issues",
        "a trailing slash": f"{own}/",
        "a double-encoded climb": f"{own}/%252E%252E/issues",
        "a backslash climb": f"{own}/repository/files/..%5C..%5Cx",
    }

    async with client_for(_app(async_session, gitlab)) as client:
        answers = {
            why: (await client.get(_proxy(project, tail), headers=BEARER)).status_code
            for why, tail in tails.items()
        }

    assert answers == dict.fromkeys(tails, 403)
    assert gitlab.seen == []


#: What a Maintainer token reads under a project that a chat tool never needs: CI/CD variable
#: values, job traces and artifacts, and the project's own tokens.
SECRET_TAILS = (
    "variables",
    "variables/DEPLOY_KEY",
    "pipeline_schedules/1/variables",
    "jobs/1/trace",
    "jobs/artifacts/main/download",
    "pipelines/1/variables",
    "hooks",
    "deploy_tokens",
    "access_tokens",
    "triggers",
    "deploy_keys",
    "runners",
)


#: Every read the chat tools' GitLab readers make (`gitlab/contents.py`, `gitlab/observer.py`,
#: `gitlab/issues.py`).
TOOL_TAILS = (
    "",
    "/repository/commits/factorial",
    "/repository/commits/" + "a" * 40,
    "/repository/commits/" + "a" * 40 + "/statuses",
    "/repository/files/.factorial%2Fprocesses%2Fprocess-x.yaml",
    "/repository/blobs/" + "b" * 40,
    "/repository/tree",
    "/repository/compare",
    "/repository/branches",
    "/merge_requests",
    "/merge_requests/5/notes",
    "/merge_requests/5/approvals",
    "/merge_requests/5/reviews",
    "/issues",
    "/issues/9",
    "/issues/9/notes",
    "/members/all",
)


@pytest.mark.parametrize("tail", TOOL_TAILS)
async def test_the_proxy_reads_what_a_chat_tool_reads(
    async_session: AsyncSession, tail: str
) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(httpx.Response(200, json=[]))

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project, tail), headers=BEARER)

    assert response.status_code == 200, response.text
    assert len(gitlab.seen) == 1


@pytest.mark.parametrize("tail", SECRET_TAILS)
async def test_the_proxy_reads_nothing_else_under_the_project(
    async_session: AsyncSession, tail: str
) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(httpx.Response(200, json=[{"key": "DEPLOY_KEY", "value": "s3cret"}]))

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project, f"/{tail}"), headers=BEARER)

    assert response.status_code == 403
    assert gitlab.seen == []


async def test_the_bare_project_answer_drops_its_tokens(async_session: AsyncSession) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(
        httpx.Response(
            200,
            json={
                "id": 1,
                "default_branch": "main",
                "runners_token": "GR1348941-runner",
                "service_desk_token": "sd-token",
                "namespace": {"id": 2, "invite_token": "inv"},
            },
        )
    )

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project), headers=BEARER)

    assert response.status_code == 200
    assert response.json() == {"id": 1, "default_branch": "main", "namespace": {"id": 2}}
    assert "GR1348941" not in response.text


async def _organization_s_project(session: AsyncSession) -> Project:
    return await _gitlab_project(
        session,
        token="glpat-theirs",
        organization_id=(await _other_organization(session)).id,
    )


async def test_each_project_s_proxy_uses_that_project_s_token_alone(
    async_session: AsyncSession,
) -> None:
    ours = await _gitlab_project(async_session)
    theirs = await _organization_s_project(async_session)
    gitlab = GitLabStub(httpx.Response(200, json={}))

    async with client_for(_app(async_session, gitlab)) as client:
        await client.get(_own(ours), headers=BEARER)
        await client.get(_own(theirs), headers=BEARER)

    assert [sent.headers["private-token"] for sent in gitlab.seen] == [
        "glpat-ours",
        "glpat-theirs",
    ]


async def test_a_project_with_no_gitlab_token_has_no_proxy(async_session: AsyncSession) -> None:
    unconfigured = await _gitlab_project(async_session, token=None)
    # Moved from GitLab to GitHub: its GitLab settings and token are still stored.
    on_github = await seed_connected(async_session, f"group/gh-{uuid4().hex[:8]}")
    on_github.settings = {**on_github.settings, "gitlab": {"base_url": GITLAB}}
    async_session.add(
        TicketSource(
            project_id=on_github.id,
            kind=TicketSourceKind.GITHUB,
            external_ref=on_github.github_repo,
        )
    )
    await store.put(async_session, on_github.id, GITLAB_TOKEN, "glpat-left-over")
    gitlab = GitLabStub(httpx.Response(200, json={}))

    async with client_for(_app(async_session, gitlab)) as client:
        no_token = await client.get(_own(unconfigured), headers=BEARER)
        github = await client.get(_own(on_github), headers=BEARER)
        missing = await client.get(
            f"/api/assistant/gitlab/projects/999999999/projects/{_encoded('a/b')}", headers=BEARER
        )

    assert (no_token.status_code, github.status_code, missing.status_code) == (404, 404, 404)
    assert gitlab.seen == []


async def test_the_proxy_never_returns_or_logs_the_token(
    async_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(
        httpx.Response(
            401,
            content=b'{"message": "401 Unauthorized", "echo": "glpat-ours"}',
            headers={
                "Content-Type": "application/json",
                "PRIVATE-TOKEN": "glpat-ours",
                "WWW-Authenticate": 'Bearer realm="glpat-ours"',
            },
        )
    )

    with caplog.at_level("DEBUG"):
        async with client_for(_app(async_session, gitlab)) as client:
            response = await client.get(_own(project), headers=BEARER)

    assert response.status_code == 401
    assert "glpat-ours" not in response.text
    assert "glpat-ours" not in str(response.headers)
    assert not {name.lower() for name in response.headers} & {"private-token", "www-authenticate"}
    assert all("glpat-ours" not in record.getMessage() for record in caplog.records)


async def test_the_proxy_follows_no_redirect(async_session: AsyncSession) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(httpx.Response(302, headers={"Location": "https://elsewhere.invalid/"}))

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project), headers=BEARER)

    assert response.status_code == 302
    assert "location" not in response.headers
    assert len(gitlab.seen) == 1


async def test_the_proxy_s_own_client_follows_no_redirect() -> None:
    open_client = gitlab_proxy_http_dependency()
    client = open_client(GitLabSettings(base_url=GITLAB))
    assert client.follow_redirects is False
    assert str(client.base_url) == f"{GITLAB}/api/v4/"


async def test_the_proxy_drops_query_parameters_that_carry_a_credential(
    async_session: AsyncSession,
) -> None:
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(httpx.Response(200, json=[]))

    async with client_for(_app(async_session, gitlab)) as client:
        await client.get(
            _own(project, "/issues"),
            params={
                "private_token": "x",
                "Access_Token": "y",
                "job_token": "z",
                "sudo": "root",
                "page": "2",
            },
            headers=BEARER,
        )

    [sent] = gitlab.seen
    assert sent.url.query == b"page=2"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, text="<html>internal admin page</html>"),
        httpx.Response(500, json={"AccessKeyId": "internal"}),
        httpx.Response(200, text="AccessKeyId internal", headers={"Content-Type": "text/plain"}),
    ],
)
async def test_the_proxy_relays_only_a_json_success_body(
    async_session: AsyncSession, answer: httpx.Response
) -> None:
    """A host that is not GitLab must not have its pages read back through the API."""
    project = await _gitlab_project(async_session)
    gitlab = GitLabStub(answer)

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project), headers=BEARER)

    assert response.status_code == (502 if answer.is_success else answer.status_code)
    assert "internal" not in response.text
    assert len(gitlab.seen) == 1


async def test_a_project_naming_a_host_inside_the_deployment_has_no_proxy(
    async_session: AsyncSession,
) -> None:
    """Stored before the write-time check, or past it: the client is never built."""
    project = await _gitlab_project(async_session)
    project.settings = {"gitlab": {"base_url": "http://169.254.170.2/v2/metadata?"}}
    await async_session.flush()
    gitlab = GitLabStub(httpx.Response(200, json={}))

    async with client_for(_app(async_session, gitlab)) as client:
        response = await client.get(_own(project), headers=BEARER)

    assert response.status_code == 404
    assert gitlab.seen == []

