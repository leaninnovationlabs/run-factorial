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

With mail configured (design.md §7.19 items 16 and 17), a password signup is sent a link that
verifies its email, and only a session of that account can redeem it. A forgotten password gets
a reset link, which needs no session; asking for one answers 202 whatever the email, and the
lookup, the token and the email all come after the response, so its time does not tell either.
A password sign-in to an unverified account with no live link sends one: an account made
before mail was configured was never sent one. A token is committed before
its email is sent: a link must never name a token the database does not hold. Without mail every
route here that sends answers 404, and nothing is gated.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from orchestrator.access.accounts import (
    PASSWORD_MAX,
    AccountExists,
    AccountLocked,
    InvalidAccountInput,
    SignInRefused,
    create_account,
    forgive_failures,
    needs_verification,
    normalize_email,
    password_sign_in,
    set_password,
    user_by_email,
)
from orchestrator.access.verification import (
    DAILY_SENDS,
    RESEND_GAP,
    InvalidToken,
    TooSoon,
    has_live,
    issue,
    redeem_reset,
    redeem_verify,
    reset_message,
    verify_message,
)
from orchestrator.api.access import from_the_dashboard, unverified_refusal
from orchestrator.api.auth import session_issued
from orchestrator.api.deps import (
    AccountSessions,
    AccountSessionsDep,
    MailerDep,
    SessionDep,
    SettingsDep,
)
from orchestrator.api.ratelimit import RateLimit, client_key
from orchestrator.api.routes.auth import live_session, set_session_cookie
from orchestrator.api.services import membership
from orchestrator.db.models import VerificationPurpose
from orchestrator.mail import Mailer, MailError, Message
from orchestrator.settings import Settings

__all__ = ["router"]

log = logging.getLogger(__name__)

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


class TokenIn(BaseModel):
    token: _Text


class ForgotIn(BaseModel):
    email: _Text


class ResetIn(BaseModel):
    token: _Text
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


def _require_mail(mailer: Mailer | None) -> Mailer:
    if mailer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This deployment sends no email.")
    return mailer


def _link(settings: Settings, page: str, token: str) -> str:
    """The token rides in the fragment, which no browser sends: it reaches neither a log nor a
    `Referer`. `refuse_mail_misconfiguration` keeps the origin set while mail is."""
    return f"{settings.dashboard_url}/{page}#{token}"


async def _deliver(mailer: Mailer, message: Message) -> bool:
    try:
        await mailer.send(message)
    except MailError as exc:
        log.warning("an account email was not sent: %s", exc)
        return False
    return True


def _too_soon(exc: TooSoon) -> HTTPException:
    detail = (
        "An email was sent a moment ago. Check your inbox, or try again in a minute."
        if exc.retry_after <= RESEND_GAP
        else f"{DAILY_SENDS} emails were sent today. Check your inbox, or try again tomorrow."
    )
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail,
        headers={"Retry-After": str(max(1, math.ceil(exc.retry_after.total_seconds())))},
    )


_INVALID_LINK: Final = (
    "This link has expired, has been used, or belongs to another account. Ask for a new one."
)


def _limit_tries(request: Request) -> None:
    """`app.state.password_tries`, built in `api.main.create_app`: each app counts its own."""
    limit: RateLimit = request.app.state.password_tries
    limit.check(request)


@router.post("/auth/signup", status_code=status.HTTP_201_CREATED, response_model=AccountOut)
async def signup(
    body: SignupIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
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
        if not invited and not await membership.install_is_empty(session, setup_token=setup_token):
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
    if mailer is not None:
        token = await issue(session, user, VerificationPurpose.VERIFY, now=now)
        await session.commit()
        background.add_task(
            _deliver,
            mailer,
            verify_message(
                user.email,
                _link(settings, "verify-email", token),
                dashboard=settings.dashboard_url,
            ),
        )
    set_session_cookie(
        request, response, user.email, settings, verified=False, epoch=user.session_epoch
    )
    return AccountOut(email=user.email, name=user.name)


@router.post("/auth/password/login", response_model=AccountOut)
async def password_login(
    body: SignInIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
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
    now = datetime.now(UTC)
    if (
        mailer is not None
        and user.email_verified_at is None
        and not await has_live(session, user, VerificationPurpose.VERIFY, now=now)
    ):
        try:
            token = await issue(session, user, VerificationPurpose.VERIFY, now=now)
        except TooSoon:
            pass
        else:
            await session.commit()
            link = _link(settings, "verify-email", token)
            background.add_task(
                _deliver, mailer, verify_message(user.email, link, dashboard=settings.dashboard_url)
            )
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
    if needs_verification(verified=signed.verified, mail=settings.mail.enabled):
        raise unverified_refusal()
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


@router.post("/auth/email/verification", status_code=status.HTTP_204_NO_CONTENT)
async def send_verification(
    request: Request, session: SessionDep, settings: SettingsDep, mailer: MailerDep
) -> Response:
    """Send the signed-in account a new verification link. A failed send says so: the person is
    waiting for it, and the link is the only way past the gate."""
    _require_accounts(settings)
    sender = _require_mail(mailer)
    signed = await live_session(request, settings, session)
    user = None if signed is None else await user_by_email(session, signed.email)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    if not from_the_dashboard(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ask for the email from the dashboard.")
    if user.email_verified_at is None:
        try:
            token = await issue(session, user, VerificationPurpose.VERIFY, now=datetime.now(UTC))
        except TooSoon as exc:
            raise _too_soon(exc) from None
        await session.commit()
        if not await _deliver(
            sender,
            verify_message(
                user.email,
                _link(settings, "verify-email", token),
                dashboard=settings.dashboard_url,
            ),
        ):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The email could not be sent. Try again in a minute.",
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/email/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    body: TokenIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> Response:
    """Redeem a verification link with a session of the account it was sent to. This browser
    gets a verified session; every other one that began unverified ends (`session_ended`)."""
    _require_accounts(settings)
    _require_mail(mailer)
    _limit_tries(request)
    signed = await live_session(request, settings, session)
    if signed is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    if not from_the_dashboard(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify from the dashboard.")
    try:
        user = await redeem_verify(session, body.token, email=signed.email, now=datetime.now(UTC))
    except InvalidToken:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID_LINK) from None
    # Before the cookie: a verified cookie for a change that did not commit passes every gate.
    await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_session_cookie(
        request, response, user.email, settings, verified=True, epoch=user.session_epoch
    )
    return response


@router.post("/auth/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotIn,
    request: Request,
    background: BackgroundTasks,
    sessions: AccountSessionsDep,
    settings: SettingsDep,
    mailer: MailerDep,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> Response:
    """Send a reset link if the email has an account. The answer is the same either way, and
    so is a request within the resend limit."""
    _require_accounts(settings)
    sender = _require_mail(mailer)
    _refuse_cross_site(sec_fetch_site)
    _limit_tries(request)
    try:
        address = normalize_email(body.email)
    except InvalidAccountInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    background.add_task(_send_reset, sessions, sender, settings, address)
    return Response(status_code=status.HTTP_202_ACCEPTED)


async def _send_reset(
    sessions: AccountSessions, mailer: Mailer, settings: Settings, address: str
) -> None:
    async with sessions() as session:
        user = await user_by_email(session, address)
        if user is None:
            return
        try:
            token = await issue(session, user, VerificationPurpose.RESET, now=datetime.now(UTC))
        except TooSoon:
            return
        await session.commit()
    link = _link(settings, "reset-password", token)
    await _deliver(mailer, reset_message(user.email, link, dashboard=settings.dashboard_url))


@router.post("/auth/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> Response:
    """Set a new password from a reset link. It ends every session of the account, and signs
    this browser in: the link proved the mailbox."""
    _require_accounts(settings)
    _require_mail(mailer)
    _refuse_cross_site(sec_fetch_site)
    _limit_tries(request)
    try:
        user = await redeem_reset(
            session, body.token, password=body.password, now=datetime.now(UTC)
        )
    except InvalidAccountInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except InvalidToken:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID_LINK) from None
    forgive_failures(user.email, client_key(request))
    # Before the cookie: a verified cookie for a change that did not commit passes every gate.
    await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_session_cookie(
        request, response, user.email, settings, verified=True, epoch=user.session_epoch
    )
    return response
