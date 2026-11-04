"""Minting an MCP token (spec §3.3, test §8 #6). The token lives a year, so the sign-in behind
it must be recent, a session must win over a bearer sent beside it, and the request must come
from the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import McpToken
from orchestrator.mcp_server.tokens import jti_digest, read_mcp_jwt
from orchestrator.settings import AuthSettings, Settings
from tests.api_harness import TOKEN, build_app, client_for, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

DASH = "http://api.test"
EMAIL = "p@leaninnovationlabs.com"


def _settings() -> Settings:
    return make_settings(auth=AuthSettings(external_url=DASH))


def _session_cookie(settings: Settings, *, signed_in_ago: timedelta) -> str:
    return mint_session(
        EMAIL,
        verified=True, api_token=TOKEN,
        now=datetime.now(UTC) - signed_in_ago,
        max_age=settings.auth.session_max_age,
    )


async def _mint(
    session: AsyncSession, *, signed_in_ago: timedelta | None, origin: str | None, label: str
) -> Response:
    settings = _settings()
    # The bearer rides along as the dashboard sends it whenever `sf.apiToken` is stored.
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if origin is not None:
        headers["Origin"] = origin
    async with client_for(build_app(settings, session), base_url=DASH) as client:
        if signed_in_ago is not None:
            cookie = _session_cookie(settings, signed_in_ago=signed_in_ago)
            client.cookies.set(SESSION_COOKIE, cookie)
        return await client.post(
            "/api/orgs/default/mcp/tokens",
            json={"label": label, "access": "write"},
            headers=headers,
        )


async def _rows(session: AsyncSession, label: str) -> list[McpToken]:
    return list((await session.execute(select(McpToken).where(McpToken.label == label))).scalars())


async def test_a_stale_session_cannot_mint_even_beside_the_bearer(
    async_session: AsyncSession,
) -> None:
    """Bearer-first would mint as `api_token` and skip the freshness check altogether. The
    bearer alone mints as `api-token`, which names no one."""
    stale = f"stale-{uuid4().hex[:8]}"
    refused = await _mint(
        async_session, signed_in_ago=timedelta(hours=25), origin=DASH, label=stale
    )
    assert refused.status_code == 403
    assert "Sign in again" in refused.json()["detail"]
    assert await _rows(async_session, stale) == []

    fresh = f"fresh-{uuid4().hex[:8]}"
    minted = await _mint(async_session, signed_in_ago=timedelta(hours=1), origin=DASH, label=fresh)
    assert minted.status_code == 201
    assert minted.headers["cache-control"] == "no-store"
    [row] = await _rows(async_session, fresh)
    assert (row.minted_via, row.created_by) == ("session", EMAIL)
    claims = read_mcp_jwt(minted.json()["token"], api_token=TOKEN, now=datetime.now(UTC))
    assert claims is not None
    assert jti_digest(claims[0]) == row.jti_sha256

    bearer = f"bearer-{uuid4().hex[:8]}"
    minted = await _mint(async_session, signed_in_ago=None, origin=DASH, label=bearer)
    assert minted.status_code == 201
    [row] = await _rows(async_session, bearer)
    assert (row.minted_via, row.created_by) == ("api_token", "api-token")
    assert row.principal == "api-token"


async def test_minting_without_an_origin_is_refused(async_session: AsyncSession) -> None:
    """A cross-site form post carries the cookie; only `Origin` says which page sent it."""
    label = f"no-origin-{uuid4().hex[:8]}"
    response = await _mint(
        async_session, signed_in_ago=timedelta(hours=1), origin=None, label=label
    )
    assert response.status_code == 403
    assert await _rows(async_session, label) == []


async def test_a_session_signed_in_the_future_is_not_fresh(async_session: AsyncSession) -> None:
    """`issued > now` would otherwise pass any `now - issued < 24h` check."""
    label = f"future-{uuid4().hex[:8]}"
    response = await _mint(
        async_session, signed_in_ago=-timedelta(hours=2), origin=DASH, label=label
    )
    assert response.status_code == 403
    assert await _rows(async_session, label) == []


async def _chat_token(session: AsyncSession) -> McpToken:
    row = McpToken(
        organization_id=await default_organization_id(session),
        jti_sha256=uuid4().hex,
        audience="aud",
        label="chat",
        created_by=EMAIL,
        minted_via="chat",
        access="chat",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_chat_token_is_not_on_the_access_page(async_session: AsyncSession) -> None:
    chat = await _chat_token(async_session)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with client_for(build_app(_settings(), async_session), base_url=DASH) as client:
        response = await client.get("/api/orgs/default/mcp/tokens", headers=headers)
    assert response.status_code == 200
    assert chat.id not in [token["id"] for token in response.json()["tokens"]]


async def test_a_chat_token_cannot_be_revoked_from_the_access_page(
    async_session: AsyncSession,
) -> None:
    chat = await _chat_token(async_session)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": DASH}
    async with client_for(build_app(_settings(), async_session), base_url=DASH) as client:
        response = await client.post(f"/api/mcp/tokens/{chat.id}/revoke", headers=headers)
    assert response.status_code == 404
    await async_session.refresh(chat)
    assert chat.revoked_at is None
