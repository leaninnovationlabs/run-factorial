"""Who an MCP request is (design.md §7.16): the `mcp_token` row, read on every request.

The JWT only locates the row. Principal, scopes, expiry and revocation all come from the row, and
nothing is cached, so a revoke takes effect on the next request. A database error propagates as a
500: an outage must never read as an invalid token.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.access import Permission
from orchestrator.access.decision import (
    Forbidden,
    NotMember,
    authorize,
    caller_named,
)
from orchestrator.api.ratelimit import client_address
from orchestrator.api.services.actor import Actor
from orchestrator.db.models import McpAccess, McpToken
from orchestrator.events.types import RequestedVia
from orchestrator.mcp_server.context import McpDeps
from orchestrator.mcp_server.tokens import READ_SCOPE, jti_digest, read_mcp_jwt

__all__ = [
    "FactorialTokenVerifier",
    "McpAccessToken",
    "RecordTokenUse",
    "current_actor",
]

log = logging.getLogger(__name__)

_USE_INTERVAL: Final = timedelta(minutes=1)
_MAX_CLIENT: Final = 120


class McpAccessToken(AccessToken):
    token: str = Field(repr=False)
    token_id: int
    #: The one organization the token acts in; every tool call resolves within it.
    organization_id: int
    principal: str
    can_write: bool
    last_used_at: datetime | None
    #: What the token's holder may do in its organization, read with the row (design.md §7.19).
    permissions: frozenset[Permission]


class FactorialTokenVerifier(TokenVerifier):
    """Every token but a chat-access row: those are history from the assistant's former mount."""

    def __init__(self, deps: McpDeps) -> None:
        super().__init__(required_scopes=[READ_SCOPE])
        self._deps = deps

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = self._deps.settings()
        api_token = settings.api_token.get_secret_value()
        if not api_token:
            return None
        now = datetime.now(UTC)
        claims = read_mcp_jwt(token, api_token=api_token, now=now)
        if claims is None:
            return None
        jti, aud = claims
        async with self._deps.sessions() as session:
            row = (
                await session.execute(
                    select(McpToken).where(McpToken.jti_sha256 == jti_digest(jti))
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.revoked_at is not None
                or row.expires_at <= now
                or row.audience != aud
                or row.access == McpAccess.CHAT
            ):
                return None
            try:
                # Removing a member stops their tokens here, on the next request.
                held = await authorize(
                    session,
                    caller_named(row.principal),
                    row.organization_id,
                    Permission.ORG_READ,
                )
            except (NotMember, Forbidden):
                return None
        return McpAccessToken(
            token=token,
            client_id=f"mcp-token-{row.id}",
            scopes=row.scopes(),
            expires_at=int(row.expires_at.timestamp()),
            subject=row.principal,
            token_id=row.id,
            organization_id=row.organization_id,
            principal=row.principal,
            can_write=row.access == McpAccess.WRITE,
            last_used_at=row.last_used_at,
            permissions=held,
        )


def current_actor(*, write: bool = False) -> Actor:
    """The one identity path for a tool. `write=True` re-checks the row's access, beside the
    tool's `require_scopes`, so dropping either lock alone does not open a write."""
    token = get_access_token()
    if not isinstance(token, McpAccessToken):
        raise AuthorizationError("This request carries no Factorial MCP token.")
    if write and not token.can_write:
        raise AuthorizationError("This MCP token is read-only.")
    return Actor(by=token.principal, via=RequestedVia.MCP, mcp_token_id=token.token_id)


def _client_label(context: MiddlewareContext[Any]) -> str | None:
    info = getattr(getattr(context.message, "params", None), "client_info", None)
    if info is None and context.fastmcp_context is not None:
        try:
            params = context.fastmcp_context.session.client_params
        except RuntimeError:
            params = None
        info = params.client_info if params is not None else None
    if info is not None:
        text = f"{info.name} {info.version}"
    else:
        try:
            text = get_http_request().headers.get("user-agent", "")
        except RuntimeError:
            text = ""
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(printable.split())[:_MAX_CLIENT] or None


def _client_ip() -> str | None:
    try:
        return client_address(get_http_request(), None)
    except RuntimeError:
        return None


class RecordTokenUse(Middleware):
    """`last_used_*` on the row, at most once a minute per token. The verifier's read of the row
    decides whether to write, so a busy client costs no extra query."""

    def __init__(self, deps: McpDeps) -> None:
        self._deps = deps

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        token = get_access_token()
        if isinstance(token, McpAccessToken):
            now = datetime.now(UTC)
            if token.last_used_at is None or token.last_used_at < now - _USE_INTERVAL:
                await self._record(token.token_id, now, context)
        return await call_next(context)

    async def _record(self, token_id: int, now: datetime, context: MiddlewareContext[Any]) -> None:
        try:
            async with self._deps.sessions() as session:
                await session.execute(
                    update(McpToken)
                    .where(
                        McpToken.id == token_id,
                        or_(
                            McpToken.last_used_at.is_(None),
                            McpToken.last_used_at < now - _USE_INTERVAL,
                        ),
                    )
                    .values(
                        last_used_at=now,
                        last_used_client=_client_label(context),
                        last_used_ip=_client_ip(),
                    )
                )
        except SQLAlchemyError:
            log.warning("could not record use of MCP token %s", token_id, exc_info=True)
