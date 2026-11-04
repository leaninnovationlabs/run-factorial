"""The API's half of the access decision (design.md §7.19): each guarded route names the one
permission it needs with `@needs`, and `authorize_request`, a dependency of every guarded router,
finds the organization the path names and asks `orchestrator.access.decision`.

Fail closed: a guarded route that names nothing is refused, and a test fails while one exists.

A change made with the session cookie must come from the dashboard: `SameSite=Lax` still sends
the cookie with a form POST from another host of the same site, so the `Origin` header, or
`Sec-Fetch-Site: same-origin`, has to say so. The bearer token never rides along on its own.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, TypeVar

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator.access import Permission
from orchestrator.access.accounts import session_ended
from orchestrator.access.decision import (
    ALL_PERMISSIONS,
    OPERATOR,
    Caller,
    Forbidden,
    NotMember,
    UnknownOwner,
    authorize,
    organization_of,
)
from orchestrator.api.auth import bearer_scheme
from orchestrator.api.deps import AccountSessionsDep, SettingsDep
from orchestrator.api.sessions import SESSION_COOKIE, verified_session
from orchestrator.settings import Settings

__all__ = [
    "GUARD",
    "SIGNED_IN",
    "CallerDep",
    "Grant",
    "GrantDep",
    "authorize_request",
    "from_the_dashboard",
    "needs",
    "require_dashboard_origin",
]

_F = TypeVar("_F", bound=Callable[..., Any])

_NEEDS: Final = "__factorial_needs__"


class _SignedIn:
    """A route any admitted caller may reach, in no organization: a listing it filters itself,
    or deployment-wide reference data."""

    def __repr__(self) -> str:
        return "SIGNED_IN"


SIGNED_IN: Final = _SignedIn()


def needs(permission: Permission | _SignedIn) -> Callable[[_F], _F]:
    """Under the route decorator, so the function the router holds carries the mark."""

    def mark(endpoint: _F) -> _F:
        setattr(endpoint, _NEEDS, permission)
        return endpoint

    return mark


def declared_need(endpoint: object) -> Permission | _SignedIn | None:
    return getattr(endpoint, _NEEDS, None)


_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def from_the_dashboard(request: Request, settings: Settings) -> bool:
    """Whether a request the session cookie authenticates may change anything."""
    return (
        request.method in _SAFE_METHODS
        or request.headers.get("sec-fetch-site") == "same-origin"
        or settings.admits_origin(request.headers.get("origin"))
    )


def require_dashboard_origin(request: Request, settings: Settings, refusal: str) -> None:
    """403 with `refusal` unless the `Origin` names the dashboard: for a request that hands out or
    changes a credential or a setting. Stricter than `from_the_dashboard`, which also takes
    `Sec-Fetch-Site: same-origin` and any safe method."""
    if not settings.admits_origin(request.headers.get("origin")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, refusal)


@dataclass(frozen=True, slots=True)
class Grant:
    """What the guard decided, for a route that answers differently by what the caller holds."""

    caller: Caller
    organization_id: int | None
    permissions: frozenset[Permission]


async def authorize_request(
    request: Request,
    sessions: AccountSessionsDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """The shared token makes the operator; a session names a person. `require_api_token` has
    already admitted one of the two, but only this reads the account behind a session, in the
    one database session it also asks the access decision in."""
    expected = settings.api_token.get_secret_value()
    if (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and expected
        and hmac.compare_digest(credentials.credentials, expected)
    ):
        _declared(request)
        request.state.grant = Grant(OPERATOR, None, ALL_PERMISSIONS)
        return
    signed = verified_session(
        request.cookies.get(SESSION_COOKIE), api_token=expected, now=datetime.now(UTC)
    )
    if signed is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "The bearer token is missing or invalid.")
    async with sessions() as session:
        if await session_ended(
            session, email=signed.email, verified=signed.verified, epoch=signed.epoch
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in again.")
        if not from_the_dashboard(request, settings):
            # 403, never 401: the dashboard signs the person out on any 401.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Make this change from the dashboard.")
        caller = Caller(signed.email.lower())
        need = _declared(request)
        if isinstance(need, _SignedIn):
            request.state.grant = Grant(caller, None, frozenset())
            return
        try:
            organization_id = await organization_of(session, request.path_params)
            permissions = await authorize(session, caller, organization_id, need)
        except (UnknownOwner, NotMember):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from None
        except Forbidden as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    request.state.grant = Grant(caller, organization_id, permissions)


def _declared(request: Request) -> Permission | _SignedIn:
    need = declared_need(request.scope.get("endpoint"))
    if need is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This route declares no permission, so it answers no one."
        )
    return need


#: What `api.main` puts on every guarded router.
GUARD: Final = Depends(authorize_request)


def _grant(request: Request) -> Grant:
    return request.state.grant


GrantDep = Annotated[Grant, Depends(_grant)]


def _caller(grant: GrantDep) -> Caller:
    return grant.caller


#: Only under `GUARD`, which sets the grant it reads: anywhere else it fails closed.
CallerDep = Annotated[Caller, Depends(_caller)]
