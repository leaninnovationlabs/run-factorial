"""Accounts: sign up, sign in with a password, set a password (design.md §7.19).

Outside the API's guard, beside the SSO routes, in every deployment (tech-stack decision 80):
nobody signs in to the dashboard with the API token. Each signs the browser in with the same
session cookie the SSO callback sets, so everything behind the guard is unchanged.

With multi-org off, signup is for the first owner of an empty install and for the holder of a
live invite, who sends its token: the invited address alone is not enough, because anyone who
knows it could take the account first. Anyone else is refused before an account exists. The first
account on an empty install owns `default`, whatever the flag says, and must send
`FACTORIAL_AUTH__BOOTSTRAP_TOKEN` when one is set; then the install is empty while `default` has
no owner (`membership.install_is_empty`).

A signup or sign-in POST from another site is refused: a page could otherwise sign a visitor in
to the attacker's account, and `SameSite=Lax` does not stop a top-level form POST.

Setting a password ends every other session of the account. An account with no password (SSO
only) sets its first one only within `PASSWORD_FRESHNESS` of a sign-in: a copied cookie alone
would otherwise buy a password that outlives the cookie and every sign-out.

Every route here hashes a password, so they share one per-address request limit
(`PASSWORD_TRIES_PER_MINUTE`), and `HasherBusy` answers 429 (`api.errors`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from orchestrator.access.accounts import (
    PASSWORD_MAX,
    AccountExists,
    AccountLocked,
    InvalidAccountInput,
    SignInRefused,
    create_account,
    normalize_email,
    password_sign_in,
    set_password,
    user_by_email,
)
from orchestrator.api.access import from_the_dashboard
from orchestrator.api.auth import session_issued
from orchestrator.api.deps import SessionDep, SettingsDep
from orchestrator.api.ratelimit import RateLimit, client_key
from orchestrator.api.routes.auth import live_session, set_session_cookie
from orchestrator.api.services import membership
from orchestrator.settings import Settings

__all__ = ["router"]

router = APIRouter(tags=["auth"])

PASSWORD_TRIES_PER_MINUTE = 10
PASSWORD_FRESHNESS: Final = timedelta(minutes=10)
SETUP_TOKEN_REFUSED: Final = (
    "This install has no owner yet. Its first account needs the setup token: the value of "
    "FACTORIAL_AUTH__BOOTSTRAP_TOKEN."
)
#: 403, never 401: the dashboard signs the person out on any 401.
REAUTH: Final = "Sign in again. Setting a first password needs a sign-in from the last 10 minutes."

#: Generous bounds on the wire; `access.accounts` applies the real rules and says which failed.
_Text = Annotated[str, Field(max_length=PASSWORD_MAX)]


class SignupIn(BaseModel):
    email: _Text
    name: _Text
    password: _Text
    #: The token of the invite whose link the signup came from.
    invite: _Text | None = None
    #: `FACTORIAL_AUTH__BOOTSTRAP_TOKEN`, from the first owner of an empty install.
    setup_token: _Text | None = None


class SignInIn(BaseModel):
    email: _Text
    password: _Text


class PasswordIn(BaseModel):
    current_password: _Text | None = None
    password: _Text


class AccountOut(BaseModel):
    email: str
    name: str


def _require_accounts(settings: Settings) -> None:
    if not settings.api_token.get_secret_value():
        # The session cookie is signed with a key derived from it (tech-stack decision 12).
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "FACTORIAL_API_TOKEN is not configured."
        )


def _refuse_cross_site(sec_fetch_site: str | None) -> None:
    if sec_fetch_site == "cross-site":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A sign-in must come from this site.")


_REFUSED = HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")


def _limit_tries(request: Request) -> None:
    """`app.state.password_tries`, built in `api.main.create_app`: each app counts its own."""
    limit: RateLimit = request.app.state.password_tries
    limit.check(request)


@router.post("/auth/signup", status_code=status.HTTP_201_CREATED, response_model=AccountOut)
async def signup(
    body: SignupIn,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> AccountOut:
    _require_accounts(settings)
    _refuse_cross_site(sec_fetch_site)
    _limit_tries(request)
    now = datetime.now(UTC)
    first_owner = settings.auth.admits_first_owner(body.setup_token)
    setup_token = bool(settings.auth.bootstrap_token.get_secret_value())
    if not settings.multi_org.enabled:
        try:
            address = normalize_email(body.email)
        except InvalidAccountInput as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        invited = body.invite is not None and await membership.invite_admits(
            session, body.invite, address, now=now
        )
        if not invited and not await membership.install_is_empty(
            session, setup_token=setup_token
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Signing up needs an invite. Ask an owner for one."
            )
        if not invited and not first_owner:
            raise HTTPException(status.HTTP_403_FORBIDDEN, SETUP_TOKEN_REFUSED)
    try:
        user = await create_account(
            session,
            email=body.email,
            name=body.name,
            password=body.password,
            now=now,
        )
    except InvalidAccountInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except AccountExists:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this email exists. Sign in instead."
        ) from None
    if first_owner:
        await membership.claim_empty_install(session, user, setup_token=setup_token)
    set_session_cookie(
        request, response, user.email, settings, verified=False, epoch=user.session_epoch
    )
    return AccountOut(email=user.email, name=user.name)


@router.post("/auth/password/login", response_model=AccountOut)
async def password_login(
    body: SignInIn,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> AccountOut:
    _require_accounts(settings)
    _refuse_cross_site(sec_fetch_site)
    _limit_tries(request)
    try:
        user = await password_sign_in(
            session,
            email=body.email,
            password=body.password,
            now=datetime.now(UTC),
            client=client_key(request),
        )
    except SignInRefused:
        raise _REFUSED from None
    except AccountLocked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins. Try again later."
        ) from None
    set_session_cookie(
        request,
        response,
        user.email,
        settings,
        verified=user.email_verified_at is not None,
        epoch=user.session_epoch,
    )
    return AccountOut(email=user.email, name=user.name)


@router.put("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_accounts(settings)
    _limit_tries(request)
    now = datetime.now(UTC)
    # Ended too when an SSO sign-in has verified the account since the session was minted: its
    # holder is the one stranger who could otherwise set the password that sign-in removed.
    signed = await live_session(request, settings, session)
    user = None if signed is None else await user_by_email(session, signed.email)
    if signed is None or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    if not from_the_dashboard(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Set a password from the dashboard.")
    issued = session_issued(signed.expires, settings)
    if user.password_hash is None and (now - issued > PASSWORD_FRESHNESS or issued > now):
        raise HTTPException(status.HTTP_403_FORBIDDEN, REAUTH)
    try:
        await set_password(
            session,
            user,
            current=body.current_password,
            password=body.password,
            now=now,
            client=client_key(request),
        )
    except InvalidAccountInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except SignInRefused:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The current password is wrong.") from None
    except AccountLocked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins. Try again later."
        ) from None
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # Every other session ended with the change; this browser stays signed in.
    set_session_cookie(
        request,
        response,
        user.email,
        settings,
        verified=user.email_verified_at is not None,
        epoch=user.session_epoch,
    )
    return response
