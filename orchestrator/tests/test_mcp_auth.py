"""The `mcp_token` row, not the JWT, decides who an MCP request is (design.md §7.16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError
from joserfc import jwt
from joserfc.jwk import OctKey
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.access.accounts import end_sessions, user_by_email
from orchestrator.api.sessions import derive_key
from orchestrator.db.models import McpAccess
from orchestrator.mcp_server.auth import FactorialTokenVerifier, McpAccessToken
from orchestrator.mcp_server.registry import register
from orchestrator.mcp_server.tokens import ALG, ISSUER, audience
from orchestrator.settings import AuthSettings
from tests.api_harness import (
    MCP_BASE,
    TOKEN,
    build_mcp_app,
    make_settings,
    mcp_client,
    mcp_deps_for,
    mcp_settings,
    seed_mcp_token,
)
from tests.mcp_probe_tools import PROBE_TOOLS, allow_probe_write

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_OTHER = "a-rotated-api-token"


async def _refused_token(case: str, session: AsyncSession) -> str:
    now = datetime.now(UTC)
    if case == "revoked":
        row, token = await seed_mcp_token(session)
        row.revoked_at, row.revoked_by = now, "operator@example.com"
    elif case == "expired":
        _, token = await seed_mcp_token(session, expires_at=now - timedelta(seconds=1))
    elif case == "missing":
        row, token = await seed_mcp_token(session)
        await session.delete(row)
    elif case == "signed_elsewhere":
        _, token = await seed_mcp_token(session, signed_with=_OTHER)
    else:
        _, token = await seed_mcp_token(session, audience_of=_OTHER)
    await session.commit()
    return token


@pytest.mark.parametrize(
    "case", ["revoked", "expired", "missing", "signed_elsewhere", "rotated_audience"]
)
async def test_verifier_refuses(case: str, maker: async_sessionmaker[AsyncSession]) -> None:
    verifier = FactorialTokenVerifier(mcp_deps_for(mcp_settings(), maker))
    async with maker() as session:
        refused = await _refused_token(case, session)
        row, accepted = await seed_mcp_token(session)
        await session.commit()

    assert await verifier.verify_token(refused) is None
    # The control: the same verifier admits an untouched row, so a refusal above is the case's.
    admitted = await verifier.verify_token(accepted)
    assert isinstance(admitted, McpAccessToken)
    assert admitted.token_id == row.id


async def test_verifier_refuses_a_creator_who_is_no_member(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # Membership, not the domain list, decides (design.md §7.19): a creator the list still admits
    # but who belongs to nothing is refused.
    auth = AuthSettings(
        external_url=MCP_BASE,
        google_client_id="x",
        google_client_secret=SecretStr("y"),
        allowed_domains=["example.com"],
    )
    verifier = FactorialTokenVerifier(mcp_deps_for(make_settings(auth=auth), maker))
    async with maker() as session:
        _, stranger = await seed_mcp_token(
            session, created_by=f"gone-{uuid4().hex[:8]}@example.com", member=False
        )
        row, member = await seed_mcp_token(session, created_by="operator@example.com")
        await session.commit()

    assert await verifier.verify_token(stranger) is None
    # The control: the same kind of row is admitted while its creator is a member.
    admitted = await verifier.verify_token(member)
    assert isinstance(admitted, McpAccessToken)
    assert admitted.token_id == row.id


async def test_ending_an_account_s_sessions_ends_the_tokens_they_minted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Sign out everywhere and a password change: a year-long token minted from a stolen session
    outlived both."""
    verifier = FactorialTokenVerifier(mcp_deps_for(mcp_settings(), maker))
    email = f"ends-{uuid4().hex[:8]}@example.com"
    async with maker() as session:
        _, minted = await seed_mcp_token(session, created_by=email)
        _, other = await seed_mcp_token(session, created_by=f"other-{uuid4().hex[:8]}@example.com")
        await session.commit()
    assert isinstance(await verifier.verify_token(minted), McpAccessToken)

    async with maker() as session:
        user = await user_by_email(session, email)
        assert user is not None
        await end_sessions(session, user)
        await session.commit()

    assert await verifier.verify_token(minted) is None
    assert isinstance(await verifier.verify_token(other), McpAccessToken)


def _claiming_write(jti_token: str) -> str:
    """The JWT `jti_token` with a `scope` claim added: what a verifier that read scopes from
    claims would obey."""
    key = OctKey.import_key(derive_key(TOKEN, b"mcp-token-v1"))
    claims = dict(jwt.decode(jti_token, key, algorithms=[ALG]).claims)
    assert claims["iss"] == ISSUER and claims["aud"] == audience(TOKEN)
    claims["scope"] = "factorial:read factorial:write"
    return jwt.encode({"alg": ALG, "typ": "JWT"}, claims, key, algorithms=[ALG])


async def test_row_wins(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_probe_write(monkeypatch)
    async with maker() as session:
        _, read_token = await seed_mcp_token(session, access=McpAccess.READ)
        write_row, write_token = await seed_mcp_token(session, access=McpAccess.WRITE)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)
    register(app.state.mcp_server, PROBE_TOOLS)

    async with mcp_client(app, _claiming_write(read_token)) as client:
        names = {item.name for item in await client.list_tools()}
        assert "factorial_probe_read" in names
        assert "factorial_probe_write" not in names
        with pytest.raises(ToolError):
            await client.call_tool("factorial_probe_write", {})

    async with mcp_client(app, write_token) as client:
        result = await client.call_tool("factorial_probe_write", {})
    assert result.structured_content == {
        "by": "operator@example.com",
        "via": "mcp",
        "mcp_token_id": write_row.id,
    }
