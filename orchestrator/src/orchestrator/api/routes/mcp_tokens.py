"""Mint, list and revoke the bearer tokens the MCP server at `/mcp/` accepts (design.md §7.16).

The row is the authority, not the JWT. The token only finds its row, and the verifier reads the
row's access, expiry and revocation on every call. So a mint commits the row before the token
exists, and a revoke is one UPDATE that the token's next request sees.

Minting and revoking are dashboard gestures. The session cookie rides along on a cross-site
POST, so an `Origin` naming the dashboard is required. Every answer is `no-store`, because a
mint carries a year-long credential and the list names everyone who holds one.

A caller without `mcp_tokens:manage` lists and revokes only the tokens they minted; another
person's token is not found, as a stranger's organization is not.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import ColumnElement, func, select, true, update

from orchestrator.access import Permission
from orchestrator.api.access import GrantDep, needs, require_dashboard_origin
from orchestrator.api.auth import BEARER_PRINCIPAL, MinterDep
from orchestrator.api.deps import OrganizationDep, SessionDep, SettingsDep
from orchestrator.api.schemas import (
    McpMinterOut,
    McpTokenIndex,
    McpTokenMinted,
    McpTokenMintIn,
    McpTokenOut,
    McpTokenState,
)
from orchestrator.db.models import McpAccess, McpToken
from orchestrator.mcp_server.tokens import LIFETIME, audience, jti_digest, mint_mcp_jwt

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])
#: Lists and creates within one organization, mounted at `/orgs/{org}` (`api.main`).
org_router = APIRouter(tags=["mcp"])

_NO_STORE: Final = {"Cache-Control": "no-store"}
#: 403, never 401: the dashboard signs the person out on any 401.
REAUTH: Final = "Sign in again. Creating an MCP token needs a sign-in from the last 24 hours."


def _from_dashboard(request: Request, response: Response, settings: SettingsDep) -> None:
    require_dashboard_origin(request, settings, "Create and revoke MCP tokens from the dashboard.")
    response.headers.update(_NO_STORE)


def _state(row: McpToken, now: datetime, current_audience: str) -> McpTokenState:
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    if row.audience != current_audience:
        return "rotated"
    return "active"


def _theirs(grant: GrantDep) -> ColumnElement[bool]:
    if Permission.MCP_TOKENS_MANAGE in grant.permissions:
        return true()
    return func.lower(McpToken.created_by) == (grant.caller.email or "")


def _token_out(row: McpToken, now: datetime, current_audience: str) -> McpTokenOut:
    fields = {name: getattr(row, name) for name in McpTokenOut.model_fields if name != "state"}
    return McpTokenOut.model_validate({**fields, "state": _state(row, now, current_audience)})


@org_router.get("/mcp/tokens", response_model=McpTokenIndex)
@needs(Permission.MCP_TOKENS_MINT)
async def list_tokens(
    session: SessionDep,
    settings: SettingsDep,
    minter: MinterDep,
    response: Response,
    organization: OrganizationDep,
    grant: GrantDep,
) -> McpTokenIndex:
    response.headers.update(_NO_STORE)
    # A chat-access row is a history of the assistant's former mount, not the access page's.
    rows = (
        await session.execute(
            select(McpToken)
            .where(
                McpToken.organization_id == organization.id,
                McpToken.access != McpAccess.CHAT,
                _theirs(grant),
            )
            .order_by(McpToken.id.desc())
        )
    ).scalars()
    now = datetime.now(UTC)
    current = audience(settings.api_token.get_secret_value())
    return McpTokenIndex(
        minter=McpMinterOut.model_validate(minter),
        mcp_url=settings.mcp_url,
        tokens=[_token_out(row, now, current) for row in rows],
    )


@org_router.post(
    "/mcp/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=McpTokenMinted,
    dependencies=[Depends(_from_dashboard)],
)
@needs(Permission.MCP_TOKENS_MINT)
async def mint_token(
    body: McpTokenMintIn,
    session: SessionDep,
    settings: SettingsDep,
    minter: MinterDep,
    organization: OrganizationDep,
) -> McpTokenMinted:
    if minter.reauth_required:
        raise HTTPException(status.HTTP_403_FORBIDDEN, REAUTH)
    created_by = minter.email or BEARER_PRINCIPAL

    api_token = settings.api_token.get_secret_value()
    now = datetime.now(UTC)
    expires = now + LIFETIME
    jti = secrets.token_urlsafe(32)
    row = McpToken(
        organization_id=organization.id,
        jti_sha256=jti_digest(jti),
        audience=audience(api_token),
        label=body.label,
        created_by=created_by,
        minted_via=minter.via,
        access=body.access,
        expires_at=expires,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await session.commit()
    token = mint_mcp_jwt(api_token=api_token, jti=jti, issued_at=now, expires_at=expires)
    log.info("minted MCP token %s for %s", row.id, row.principal)
    return McpTokenMinted(token=token, row=_token_out(row, now, row.audience))


@router.post(
    "/mcp/tokens/{token_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_from_dashboard)],
)
@needs(Permission.MCP_TOKENS_MINT)
async def revoke_token(
    token_id: int, session: SessionDep, minter: MinterDep, grant: GrantDep
) -> Response:
    """A second revoke keeps the first `revoked_by`."""
    by = minter.email or BEARER_PRINCIPAL
    revoked = await session.execute(
        update(McpToken)
        .where(
            McpToken.id == token_id,
            McpToken.access != McpAccess.CHAT,
            McpToken.revoked_at.is_(None),
            _theirs(grant),
        )
        .values(revoked_at=func.now(), revoked_by=by)
        .returning(McpToken.id)
    )
    if revoked.scalar_one_or_none() is None:
        known = select(McpToken.id).where(
            McpToken.id == token_id, McpToken.access != McpAccess.CHAT, _theirs(grant)
        )
        if await session.scalar(known) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"MCP token {token_id} not found.")
    else:
        await session.commit()
        log.info("revoked MCP token %s by %s", token_id, by)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=dict(_NO_STORE))
