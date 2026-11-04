"""When a GitLab project may fall back to the deployment's GitLab credentials. The project picks
its host and repo, so a deployment-wide token paired with them reads whatever that token can."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project
from orchestrator.gitlab.ca_file import extra_ca_pem
from orchestrator.gitlab.project_client import (
    TOKEN_SECRET,
    _GitLabPool,  # pyright: ignore[reportPrivateUsage]
    client_for_project,
    settings_for_project,
    webhook_secret_for,
)
from orchestrator.secrets import store
from orchestrator.settings import GitLabSettings, get_settings
from tests.api_harness import seed_project

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("master_key"),
]

DEPLOYMENT = "https://gitlab.corp.invalid"


def _deployment(monkeypatch: pytest.MonkeyPatch, *, multi_org: bool) -> Iterator[None]:
    monkeypatch.setenv("FACTORIAL_GITLAB__BASE_URL", DEPLOYMENT)
    monkeypatch.setenv("FACTORIAL_GITLAB__TOKEN", "glpat-deployment")
    monkeypatch.setenv("FACTORIAL_GITLAB__WEBHOOK_SECRET", "deployment-hook")
    monkeypatch.setenv("FACTORIAL_MULTI_ORG__ENABLED", "true" if multi_org else "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def single_tenant(monkeypatch: pytest.MonkeyPatch, master_key: None) -> Iterator[None]:
    yield from _deployment(monkeypatch, multi_org=False)


@pytest.fixture
def multi_org(monkeypatch: pytest.MonkeyPatch, master_key: None) -> Iterator[None]:
    yield from _deployment(monkeypatch, multi_org=True)


async def _project(session: AsyncSession, gitlab: dict[str, str]) -> Project:
    project = await seed_project(session, f"group/gl-{uuid4().hex[:8]}")
    project.settings = {"gitlab": gitlab}
    await session.flush()
    return project


@pytest.mark.usefixtures("single_tenant")
async def test_the_deployment_token_serves_only_a_project_on_the_deployment_s_host(
    async_session: AsyncSession,
) -> None:
    own_host = await _project(async_session, {})
    same_origin = await _project(async_session, {"base_url": f"{DEPLOYMENT}/"})
    elsewhere = await _project(async_session, {"base_url": "https://gitlab.example.com"})

    for project in (own_host, same_origin):
        resolved = await settings_for_project(async_session, project)
        assert resolved is not None
        assert resolved.token.get_secret_value() == "glpat-deployment"
        assert await webhook_secret_for(async_session, project) == "deployment-hook"
    assert await settings_for_project(async_session, elsewhere) is None
    assert await webhook_secret_for(async_session, elsewhere) == ""


@pytest.mark.usefixtures("multi_org")
async def test_under_multi_org_no_project_gets_the_deployment_s_credentials(
    async_session: AsyncSession,
) -> None:
    project = await _project(async_session, {})
    assert await settings_for_project(async_session, project) is None
    assert await webhook_secret_for(async_session, project) == ""


@pytest.mark.usefixtures("multi_org")
async def test_under_multi_org_a_project_s_ca_bundle_path_is_never_read(
    async_session: AsyncSession,
) -> None:
    project = await _project(async_session, {"ca_bundle": "/etc/hosts"})
    await store.put(async_session, project.id, TOKEN_SECRET, "glpat-own")
    resolved = await settings_for_project(async_session, project)
    assert resolved is not None
    assert resolved.ca_bundle is None
    assert extra_ca_pem(resolved) == ""


@pytest.mark.usefixtures("single_tenant")
async def test_a_single_organization_project_keeps_its_ca_bundle_path(
    async_session: AsyncSession, tmp_path: Path
) -> None:
    """Projects saved before paths were refused hold one, and an on-premises GitLab behind a
    private CA cannot be reached without it. Only open signup is reason to refuse a path."""
    ca = tmp_path / "org-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    project = await _project(async_session, {"ca_bundle": str(ca)})
    resolved = await settings_for_project(async_session, project)
    assert resolved is not None
    assert resolved.ca_bundle == ca
    assert extra_ca_pem(resolved).startswith("-----BEGIN CERTIFICATE-----")


@pytest.mark.usefixtures("single_tenant")
async def test_a_project_s_gitlab_client_is_reused_until_its_credentials_change(
    async_session: AsyncSession,
) -> None:
    """A client per call opened a connection pool, and with a CA an SSL context, for every
    ticket each tick and every API read, and none was closed."""
    project = await _project(async_session, {})

    first = await client_for_project(async_session, project)
    assert first is not None
    assert await client_for_project(async_session, project) is first

    await store.put(async_session, project.id, TOKEN_SECRET, "glpat-two")
    rotated = await client_for_project(async_session, project)
    assert rotated is not None and rotated is not first
    assert rotated.settings.token.get_secret_value() == "glpat-two"


async def test_a_replaced_gitlab_client_closes_once_its_request_in_flight_ends() -> None:
    """The pool dropped a replaced client without closing it, and its connections stayed open
    for the life of the worker."""
    pool = _GitLabPool()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    old = await pool.client(1, GitLabSettings(base_url=DEPLOYMENT, token=SecretStr("one")))
    old_http = old._http = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        base_url=DEPLOYMENT, transport=httpx.MockTransport(slow)
    )
    in_flight = asyncio.create_task(old.get("/projects"))
    await asyncio.wait_for(entered.wait(), 5)

    new = await pool.client(1, GitLabSettings(base_url=DEPLOYMENT, token=SecretStr("two")))
    assert new is not old
    assert not old_http.is_closed, "closed under the request in flight"
    release.set()
    assert await in_flight == {"ok": True}
    assert old_http.is_closed

    idle_http = new._http = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(slow)
    )
    await pool.forget(1)
    assert idle_http.is_closed
