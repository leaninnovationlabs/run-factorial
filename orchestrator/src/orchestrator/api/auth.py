"""Auth for the API and dashboard (tech-stack decision 12, extended by the Google login).

One gate, two credentials: the shared bearer token (scripts, Bruno) or the signed session
cookie the Google callback mints (`orchestrator.api.routes.auth`). Health and the GitHub
webhook stay outside it — the webhook authenticates by HMAC, because GitHub cannot send a
bearer token.

An unset `FACTORIAL_API_TOKEN` rejects every request rather than admitting every request: an
accidentally unconfigured deployment must be broken, not open. Sessions are signed with a key
derived from the same token, so that property covers both credentials.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.api.deps import SettingsDep
from orchestrator.api.sessions import SESSION_COOKIE, verified_session, verify_session
from orchestrator.settings import Settings

__all__ = [
    "BEARER_PRINCIPAL",
    "Caller",
    "CallerDep",
    "Minter",
    "MinterDep",
    "PersonDep",
    "PrincipalDep",
    "bearer_scheme",
    "require_api_token",
    "require_bearer",
    "session_issued",
]

# auto_error=False so a missing header reaches us as None and we answer 401 + WWW-Authenticate,
# rather than HTTPBearer's bare 403.
_bearer = HTTPBearer(auto_error=False, description="FACTORIAL_API_TOKEN as a bearer token")
#: The same scheme for `api.access`, which reads the caller beside this module's guard.
bearer_scheme = _bearer

#: Who a bearer-token request is recorded as: the token is shared, so it names no one.
BEARER_PRINCIPAL = "api-token"

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="The bearer token is missing or invalid.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def require_api_token(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected = settings.api_token.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "FACTORIAL_API_TOKEN is not configured. The API refuses all requests until it is."
            ),
        )
    if credentials is not None and credentials.scheme.lower() == "bearer":
        # Constant time: closes a timing oracle on the shared secret.
        if hmac.compare_digest(credentials.credentials, expected):
            return
        raise _UNAUTHORIZED
    if verify_session(
        request.cookies.get(SESSION_COOKIE), api_token=expected, now=datetime.now(UTC)
    ):
        return
    raise _UNAUTHORIZED


async def require_bearer(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """The API token itself, never a session: for a route only a process calls."""
    expected = settings.api_token.get_secret_value()
    if (
        not expected
        or credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise _UNAUTHORIZED


@dataclass(frozen=True, slots=True)
class Caller:
    principal: str
    #: When the credential lapses, in Unix seconds: a session's expiry, or None for the bearer
    #: token, which lasts until it is rotated.
    until: int | None
    #: The session's epoch, so a credential minted from it ends with it; None for the bearer.
    epoch: int | None = None


async def request_caller(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    """Who is asking, for the record a request leaves — never an authorization decision, which
    `require_api_token` has already made for the router. A Google session names its email; the
    shared bearer token names no one (decision 12), and says so."""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return Caller(BEARER_PRINCIPAL, None)
    session = verified_session(
        request.cookies.get(SESSION_COOKIE),
        api_token=settings.api_token.get_secret_value(),
        now=datetime.now(UTC),
    )
    return (
        Caller(session.email, session.expires, session.epoch)
        if session is not None
        else Caller(BEARER_PRINCIPAL, None)
    )


async def request_principal(caller: Annotated[Caller, Depends(request_caller)]) -> str:
    return caller.principal


CallerDep = Annotated[Caller, Depends(request_caller)]
PrincipalDep = Annotated[str, Depends(request_principal)]


#: How recent the sign-in behind a new MCP token must be. The token lives a year and outlives
#: its session, so a browser left signed in all week is not enough.
_MINT_FRESHNESS: Final = timedelta(hours=24)


def session_issued(expires: int, settings: Settings) -> datetime:
    """When a session was minted. One minted before `session_max_age` was lowered appears issued
    in the future, and its age is then unknowable."""
    return datetime.fromtimestamp(expires - settings.auth.session_max_age, UTC)


@dataclass(frozen=True, slots=True)
class Minter:
    """Who an MCP token would be minted for: a session's email, or the shared bearer, which
    names no one and is recorded as `api-token`."""

    email: str | None
    via: Literal["session", "api_token"]
    reauth_required: bool


async def request_minter(request: Request, settings: SettingsDep) -> Minter:
    """Session first, unlike `request_caller`. The dashboard sends the bearer whenever one is
    stored, and bearer-first would let a signed-in person mint as `api_token`, skipping the
    freshness check here and the verifier's membership check."""
    now = datetime.now(UTC)
    session = verified_session(
        request.cookies.get(SESSION_COOKIE),
        api_token=settings.api_token.get_secret_value(),
        now=now,
    )
    if session is None:
        return Minter(email=None, via="api_token", reauth_required=False)
    issued = session_issued(session.expires, settings)
    stale = now - issued > _MINT_FRESHNESS or issued > now
    return Minter(email=session.email, via="session", reauth_required=stale)


MinterDep = Annotated[Minter, Depends(request_minter)]


async def request_person(request: Request, settings: SettingsDep) -> str | None:
    """The signed-in person's email, session first as `request_minter` reads it, or None when
    only the shared bearer came: a gesture that must name someone cannot take `api-token`."""
    session = verified_session(
        request.cookies.get(SESSION_COOKIE),
        api_token=settings.api_token.get_secret_value(),
        now=datetime.now(UTC),
    )
    return session.email if session is not None else None


PersonDep = Annotated[str | None, Depends(request_person)]
