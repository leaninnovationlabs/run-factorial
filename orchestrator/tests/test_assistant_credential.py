"""`POST /api/orgs/{org}/assistant/credential`: the API opens an organization's assistant
credential for the assistant's process, which holds no master key (design.md §7.18), and only
for the API bearer. The process's own client of it, `assistant.credential.api_credential`, is
driven against the real route."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.credential import SettingsChanged, api_credential
from orchestrator.db.models import Organization
from orchestrator.settings import APP_PREFIX, AssistantProcessSettings
from tests.api_harness import TOKEN, build_app, client_for, make_settings
from tests.log_probe import probing
from tests.members import ensure_member

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("master_key"),
]

BEARER = {"Authorization": f"Bearer {TOKEN}"}
API_KEY = {"backend": "claude_sdk", "auth_mode": "api_key"}


def _path(organization: Organization) -> str:
    return f"/api/orgs/{organization.slug}/assistant/credential"


async def _organization(
    session: AsyncSession,
    *,
    credential: str | None = "sk-ant-ours",
    **settings: object,
) -> Organization:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Acme", updated_by="test")
    session.add(organization)
    await session.flush()
    saved = AssistantSettings.model_validate({"enabled": True, **settings})
    await svc.save_assistant_settings(session, organization.id, saved, by="test")
    if credential is not None:
        await svc.set_assistant_credential(
            session, organization.id, saved.backend, saved.auth_mode, credential, by="test"
        )
    return organization


@pytest.fixture
def app(async_session: AsyncSession) -> Iterator[FastAPI]:
    yield build_app(make_settings(), async_session)


def _served(app: FastAPI) -> httpx.ASGITransport:
    """The API as the image serves it, under the prefix the assistant's path carries."""
    return httpx.ASGITransport(app=Starlette(routes=[Mount(APP_PREFIX, app=app)]))


async def test_the_bearer_gets_its_organization_s_credential(
    async_session: AsyncSession, app: FastAPI
) -> None:
    ours = await _organization(async_session)
    await _organization(async_session, credential="sk-ant-theirs")

    with probing("orchestrator") as logged:
        async with client_for(app) as client:
            response = await client.post(_path(ours), json=API_KEY, headers=BEARER)

    assert response.status_code == 200, response.text
    assert response.json() == {"credential": "sk-ant-ours"}
    assert response.headers["cache-control"] == "no-store"
    assert "sk-ant-ours" not in str(logged.lines)


async def test_only_the_api_bearer_gets_a_credential(
    async_session: AsyncSession, app: FastAPI
) -> None:
    ours = await _organization(async_session)
    # A member's session, so the guard admits it and the route itself must refuse it.
    await ensure_member(async_session, "operator@example.com", organization_id=ours.id)
    cookie = mint_session(
        "operator@example.com", verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=3600
    )
    async with client_for(app) as client:
        nothing = await client.post(_path(ours), json=API_KEY)
        client.cookies.set(SESSION_COOKIE, cookie)
        signed_in = await client.post(
            _path(ours), json=API_KEY, headers={"Sec-Fetch-Site": "same-origin"}
        )
        client.cookies.clear()
        wrong = await client.post(
            _path(ours), json=API_KEY, headers={"Authorization": "Bearer not-the-token"}
        )

    assert (nothing.status_code, signed_in.status_code, wrong.status_code) == (401, 401, 401)
    assert "sk-ant-ours" not in nothing.text + signed_in.text + wrong.text


@pytest.mark.parametrize(
    ("stored", "settings", "asked", "code"),
    [
        ("sk-ant-ours", {"enabled": False}, API_KEY, "not_configured"),
        (None, {}, API_KEY, "not_configured"),
        # The turn read its settings before a switch the route reads after: the credential
        # stored for the other mode is never handed out for this one.
        ("tok-sub", {"auth_mode": "subscription"}, API_KEY, "settings_changed"),
    ],
)
async def test_a_turn_with_no_credential_to_run_on_is_refused(
    async_session: AsyncSession,
    app: FastAPI,
    stored: str | None,
    settings: dict[str, object],
    asked: dict[str, str],
    code: str,
) -> None:
    ours = await _organization(async_session, credential=stored, **settings)

    async with client_for(app) as client:
        response = await client.post(_path(ours), json=asked, headers=BEARER)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert stored is None or stored not in response.text


async def test_the_assistant_s_client_reads_the_route(
    async_session: AsyncSession, app: FastAPI
) -> None:
    ours = await _organization(async_session)
    off = await _organization(async_session, enabled=False)
    settings = make_settings().model_copy(
        update={"assistant": AssistantProcessSettings(api_url="http://api.test")}
    )
    fetch = api_credential(lambda: settings, transport=_served(app))

    credential = await fetch(ours.slug, AssistantSettings(enabled=True))
    assert credential is not None and credential.get_secret_value() == "sk-ant-ours"
    assert await fetch(off.slug, AssistantSettings(enabled=True)) is None


async def test_the_assistant_s_client_tells_settings_changed_from_no_credential(
    async_session: AsyncSession, app: FastAPI
) -> None:
    """A switch mid-turn is not a missing credential: the turn must not tell the person to set
    one up."""
    switched = await _organization(async_session, credential="tok-sub", auth_mode="subscription")
    settings = make_settings().model_copy(
        update={"assistant": AssistantProcessSettings(api_url="http://api.test")}
    )
    fetch = api_credential(lambda: settings, transport=_served(app))

    with pytest.raises(SettingsChanged):
        await fetch(switched.slug, AssistantSettings(enabled=True))


async def test_the_assistant_s_client_raises_when_the_api_refuses_it(app: FastAPI) -> None:
    """A wrong API token is an outage, not a missing credential: the turn must not tell the
    person to set one."""
    settings = make_settings(api_token="not-the-token").model_copy(
        update={"assistant": AssistantProcessSettings(api_url="http://api.test")}
    )
    fetch = api_credential(lambda: settings, transport=_served(app))

    with pytest.raises(httpx.HTTPStatusError, match="401"):
        await fetch("default", AssistantSettings(enabled=True))
