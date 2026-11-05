"""Dashboard sign-in: Google and generic OpenID Connect (authorization-code flow).

The identity provider only ever 302s the operator's browser here — the one server-to-server
call is the code exchange, which this host initiates outbound.

Who gets an account is ours, not the IdP's (design.md §7.19). With multi-org off, the identity
that comes back needs `FACTORIAL_AUTH__ALLOWED_DOMAINS`, a membership of `default`, a pending
invite or an empty install before an account or a session exists; one in the domain list joins
`default` as a member. With it on, anyone gets an account, and membership decides what it
reaches. The session cookie then satisfies the same guard the bearer token does
(`orchestrator.api.auth`), so scripts and Bruno are untouched.

An SSO sign-in never sends `FACTORIAL_AUTH__BOOTSTRAP_TOKEN`, so where one is set, an empty
install refuses SSO until its owner signs up with a password: with multi-org off, a member joined
first would leave the install with data and no owner.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import ssl
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access.accounts import (
    InvalidAccountInput,
    end_sessions,
    needs_verification,
    session_ended,
    sso_sign_in,
    user_by_email,
)
from orchestrator.api.access import from_the_dashboard
from orchestrator.api.deps import AccountSessionsDep, SettingsDep
from orchestrator.api.services import membership
from orchestrator.api.sessions import (
    SESSION_COOKIE,
    SignedSession,
    mint_session,
    verified_session,
)
from orchestrator.settings import APP_PREFIX, AuthSettings, Settings

__all__ = [
    "GoogleIdentity",
    "OidcIdentity",
    "google_identity_dependency",
    "live_session",
    "oidc_identity_dependency",
    "router",
    "set_session_cookie",
]

router = APIRouter(tags=["auth"])

STATE_COOKIE = "sf_oauth_state"
PROVIDER_COOKIE = "sf_oauth_provider"
PKCE_COOKIE = "sf_oauth_pkce"
ID_TOKEN_COOKIE = "sf_id_token"
#: The dashboard path to land on after the callback. The dashboard shows its sign-in screen in
#: place of whatever page a lapsed session was on, so that page is what `next` names.
NEXT_COOKIE = "sf_oauth_next"
_NEXT_MAX = 2048
Provider = Literal["google", "oidc"]

#: (code, redirect_uri) -> (email, email_verified).
GoogleIdentity = Callable[[str, str], Awaitable[tuple[str, bool]]]
OidcIdentity = Callable[[str, str, str | None], Awaitable[tuple[str, bool, str | None]]]


async def google_identity_dependency(settings: SettingsDep) -> GoogleIdentity:
    """The real Google exchange. Identity comes from the userinfo endpoint over TLS rather than
    by verifying the id_token's signature — both legs of the exchange are direct calls to Google,
    so the transport is the trust anchor and no JWKS cache has to exist."""

    async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
        email, verified, _id_token = await _token_then_userinfo(
            code=code,
            redirect_uri=redirect_uri,
            client_id=settings.auth.google_client_id,
            client_secret=settings.auth.google_client_secret.get_secret_value(),
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
            detail="Google did not complete the sign-in. Try again.",
            require_verified=True,
        )
        return email, verified

    return exchange


async def oidc_identity_dependency(settings: SettingsDep) -> OidcIdentity:
    """Generic OIDC: token + userinfo at the configured (or discovered) endpoints.

    `email_verified` is accepted when the claim is missing: Okta and many enterprise issuers
    treat the mailbox as already verified by the directory.
    """

    async def exchange(
        code: str, redirect_uri: str, code_verifier: str | None
    ) -> tuple[str, bool, str | None]:
        endpoints = await _oidc_endpoints(settings.auth)
        return await _token_then_userinfo(
            code=code,
            redirect_uri=redirect_uri,
            client_id=settings.auth.resolved_oidc_client_id(),
            client_secret=settings.auth.resolved_oidc_client_secret(),
            token_url=endpoints["token"],
            userinfo_url=endpoints["userinfo"],
            detail="The OIDC provider did not complete the sign-in. Try again.",
            require_verified=False,
            code_verifier=code_verifier,
        )

    return exchange


def _tls_verify() -> ssl.SSLContext:
    """httpx's own trust (certifi, or SSL_CERT_FILE when set), plus REQUESTS_CA_BUNDLE's
    certificates when it names a file: a private IdP's CA is added to the public
    roots Google's endpoints need, never put in their place."""
    context = httpx.create_ssl_context()
    extra = os.environ.get("REQUESTS_CA_BUNDLE", "")
    if extra and Path(extra).is_file():
        context.load_verify_locations(cafile=extra)
    return context


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def _token_then_userinfo(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    token_url: str,
    userinfo_url: str,
    detail: str,
    require_verified: bool,
    code_verifier: str | None = None,
) -> tuple[str, bool, str | None]:
    upstream = HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=10, verify=_tls_verify()) as client:
        token_response = await client.post(
            token_url,
            data=payload,
        )
        if token_response.status_code != 200:
            raise upstream
        body = token_response.json()
        access_token = body.get("access_token")
        id_token = body.get("id_token") if isinstance(body.get("id_token"), str) else None
        if not access_token:
            raise upstream
        userinfo = await client.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo.status_code != 200:
            raise upstream
        claims = userinfo.json()
        email = claims.get("email")
        if not email:
            raise upstream
        verified = (
            bool(claims.get("email_verified"))
            if "email_verified" in claims
            else not require_verified
        )
        return email, verified, id_token


_DISCOVERY_TTL_SECONDS = 300
_discovery_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _oidc_endpoints(auth: AuthSettings) -> dict[str, str]:
    if auth.oidc_authorize_url and auth.oidc_token_url and auth.oidc_userinfo_url:
        return {
            "authorize": auth.oidc_authorize_url,
            "token": auth.oidc_token_url,
            "userinfo": auth.oidc_userinfo_url,
        }
    okta = auth.okta_endpoints()
    if okta:
        return okta
    issuer = auth.oidc_issuer.rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached is not None:
        expires, endpoints = cached
        if expires > datetime.now(UTC).timestamp():
            return endpoints
    document_url = issuer + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10, verify=_tls_verify()) as client:
        response = await client.get(document_url)
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Factorial couldn't read the OIDC discovery document.",
        )
    body = response.json()
    endpoints = {
        "authorize": str(body.get("authorization_endpoint") or ""),
        "token": str(body.get("token_endpoint") or ""),
        "userinfo": str(body.get("userinfo_endpoint") or ""),
    }
    if body.get("end_session_endpoint"):
        endpoints["end_session"] = str(body["end_session_endpoint"])
    required = ("authorize", "token", "userinfo")
    if not all(endpoints.get(name) for name in required):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The OIDC discovery document is incomplete.",
        )
    _discovery_cache[issuer] = (datetime.now(UTC).timestamp() + _DISCOVERY_TTL_SECONDS, endpoints)
    return endpoints


def _require_google(settings: Settings) -> None:
    if not settings.auth.google_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Google sign-in is not configured."
        )


def _require_oidc(settings: Settings) -> None:
    if not settings.auth.oidc_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OIDC sign-in is not configured."
        )


def _require_enabled(settings: Settings) -> None:
    if not settings.auth.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Google sign-in is not configured."
        )


def _secure(settings: Settings) -> bool:
    return settings.auth.external_url.startswith("https")


def _reached_by_https(request: Request) -> bool:
    """A password sign-in needs no external URL, so the session cookie also asks the request and
    the proxy in front of it. A forged header can only add `Secure`, never take it away."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _redirect_uri(settings: Settings) -> str:
    # The constant, not the request's mount: Google compares it with the registered URI exactly.
    return f"{settings.auth.external_url.rstrip('/')}{APP_PREFIX}/auth/callback"


def _app_root(request: Request) -> str:
    """Where the app is mounted (`APP_PREFIX` behind the site, empty in a test of the app alone).
    Its cookies and redirects go there, because a browser sends `/auth` cookies only to `/auth`."""
    return str(request.scope.get("root_path", ""))


def _local_path(value: str | None, root: str) -> str | None:
    """`value` if it is a path on this origin, else None, so `next` cannot become an open
    redirect: a scheme, a host, a protocol-relative `//`, a backslash (which browsers read as a
    slash) or a control character each refuse it. So does anything outside ASCII: the cookie that
    carries it is latin-1, and the dashboard always sends its path percent-encoded."""
    if not value or len(value) > _NEXT_MAX or not value.startswith("/") or value.startswith("//"):
        return None
    if not value.isascii():
        return None
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    if root and value != root and not value.startswith(f"{root}/"):
        return None
    return value


def set_session_cookie(
    request: Request,
    response: Response,
    email: str,
    settings: Settings,
    *,
    verified: bool,
    epoch: int,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        mint_session(
            email,
            verified=verified,
            api_token=settings.api_token.get_secret_value(),
            now=datetime.now(UTC),
            max_age=settings.auth.session_max_age,
            epoch=epoch,
        ),
        max_age=settings.auth.session_max_age,
        httponly=True,
        secure=_secure(settings) or _reached_by_https(request),
        samesite="lax",
        path="/",
    )


@router.get("/auth/providers")
async def providers(
    settings: SettingsDep, account_sessions: AccountSessionsDep
) -> dict[str, bool | str]:
    """What the sign-in page can offer. Unauthenticated and boolean-only on purpose. With
    multi-org off, signup is offered only to the first owner of an empty install: an invite's
    link reaches signup on its own. `setup_token` says the first owner must send one."""
    awaits_owner = bool(settings.auth.bootstrap_token.get_secret_value())
    empty = False
    if awaits_owner or not settings.multi_org.enabled:
        async with account_sessions() as session:
            empty = await membership.install_is_empty(session, setup_token=awaits_owner)
    return {
        "google": settings.auth.google_enabled,
        "oidc": settings.auth.oidc_enabled,
        "oidc_label": settings.auth.resolved_oidc_label(),
        "password": True,
        "signup": settings.multi_org.enabled or empty,
        "multi_org": settings.multi_org.enabled,
        "setup_token": empty and awaits_owner,
        "mail": settings.mail.enabled,
    }


def _start_login(
    settings: Settings,
    root: str,
    *,
    authorize_url: str,
    client_id: str,
    scope: str,
    extra: dict[str, str],
    provider: Provider,
    next_path: str | None,
    pkce: bool = False,
) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    verifier = _pkce_verifier() if pkce else None
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(settings),
        "response_type": "code",
        "scope": scope,
        "state": state,
        **extra,
    }
    if verifier:
        params["code_challenge"] = _pkce_challenge(verifier)
        params["code_challenge_method"] = "S256"
    response = RedirectResponse(
        f"{authorize_url}?{urlencode(params)}",
        status_code=status.HTTP_302_FOUND,
    )
    cookies: list[tuple[str, str]] = [
        (STATE_COOKIE, state),
        (PROVIDER_COOKIE, provider),
    ]
    if verifier:
        cookies.append((PKCE_COOKIE, verifier))
    for name, value in cookies:
        response.set_cookie(
            name,
            value,
            max_age=600,
            httponly=True,
            secure=_secure(settings),
            samesite="lax",
            path=f"{root}/auth",
        )
    target = _local_path(next_path, root)
    if target is None:
        response.delete_cookie(NEXT_COOKIE, path=f"{root}/auth")
    else:
        response.set_cookie(
            NEXT_COOKIE,
            target,
            max_age=600,
            httponly=True,
            secure=_secure(settings),
            samesite="lax",
            path=f"{root}/auth",
        )
    return response


@router.get("/auth/login")
async def login(
    request: Request,
    settings: SettingsDep,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """Google when it is configured; otherwise the generic OIDC issuer. Existing dashboard
    links keep working when a deployment swaps Google for Okta."""
    _require_enabled(settings)
    if settings.auth.google_enabled:
        return _google_login(settings, _app_root(request), next_path)
    return await _oidc_login(settings, _app_root(request), next_path)


@router.get("/auth/google/login")
async def google_login(
    request: Request,
    settings: SettingsDep,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    _require_google(settings)
    return _google_login(settings, _app_root(request), next_path)


@router.get("/auth/oidc/login")
async def oidc_login(
    request: Request,
    settings: SettingsDep,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    _require_oidc(settings)
    return await _oidc_login(settings, _app_root(request), next_path)


def _google_login(settings: Settings, root: str, next_path: str | None) -> RedirectResponse:
    return _start_login(
        settings,
        root,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        client_id=settings.auth.google_client_id,
        scope="openid email",
        extra={"prompt": "select_account"},
        provider="google",
        next_path=next_path,
    )


async def _oidc_login(settings: Settings, root: str, next_path: str | None) -> RedirectResponse:
    endpoints = await _oidc_endpoints(settings.auth)
    return _start_login(
        settings,
        root,
        authorize_url=endpoints["authorize"],
        client_id=settings.auth.resolved_oidc_client_id(),
        scope=settings.auth.oidc_scopes,
        extra={},
        provider="oidc",
        next_path=next_path,
        pkce=True,
    )


def _back_to_sign_in(root: str, reason: str) -> RedirectResponse:
    """A browser that cannot be signed in goes back to the sign-in page with the reason as a
    closed word, never the account: an address in a URL ends up in history and logs."""
    response = RedirectResponse(f"{root}/?signin={reason}", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(STATE_COOKIE, path=f"{root}/auth")
    response.delete_cookie(PROVIDER_COOKIE, path=f"{root}/auth")
    response.delete_cookie(PKCE_COOKIE, path=f"{root}/auth")
    response.delete_cookie(NEXT_COOKIE, path=f"{root}/auth")
    return response


@router.get("/auth/callback")
async def callback(
    request: Request,
    settings: SettingsDep,
    google_exchange: Annotated[GoogleIdentity, Depends(google_identity_dependency)],
    oidc_exchange: Annotated[OidcIdentity, Depends(oidc_identity_dependency)],
    account_sessions: AccountSessionsDep,
    code: str,
    state: str | None = None,
) -> RedirectResponse:
    _require_enabled(settings)
    root = _app_root(request)
    expected_state = request.cookies.get(STATE_COOKIE)
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return _back_to_sign_in(root, "retry")

    provider = request.cookies.get(PROVIDER_COOKIE) or "google"
    id_token: str | None = None
    if provider == "oidc":
        _require_oidc(settings)
        email, verified, id_token = await oidc_exchange(
            code, _redirect_uri(settings), request.cookies.get(PKCE_COOKIE)
        )
    else:
        _require_google(settings)
        email, verified = await google_exchange(code, _redirect_uri(settings))
    if not verified:
        return _back_to_sign_in(root, "refused")
    # Membership is the tenant boundary (design.md §7.19). With multi-org off an account comes
    # only from the domain list, an invite or an empty install; the IdP's consent screen admits
    # any account, so it is never the boundary.
    domain = email.rsplit("@", 1)[-1].lower()
    in_domain = domain in settings.auth.allowed_domains
    now = datetime.now(UTC)
    awaits_owner = bool(settings.auth.bootstrap_token.get_secret_value())
    try:
        async with account_sessions() as session:
            empty = await membership.install_is_empty(session, setup_token=awaits_owner)
            if (empty and awaits_owner and not settings.multi_org.enabled) or not (
                settings.multi_org.enabled
                or in_domain
                or await membership.member_of_default(session, email)
                or await membership.has_pending_invite(session, email, now=now)
                or empty
            ):
                return _back_to_sign_in(root, "refused")
            user = await sso_sign_in(session, email=email, now=now)
            claimed = not awaits_owner and await membership.claim_empty_install(
                session, user, setup_token=False
            )
            if not claimed and not settings.multi_org.enabled and in_domain:
                await membership.join_default(session, user)
            email = user.email
            verified = user.email_verified_at is not None
            epoch = user.session_epoch
    except InvalidAccountInput:
        return _back_to_sign_in(root, "refused")

    # Checked again here: the cookie is ours, but it is a value the browser sent back.
    target = _local_path(request.cookies.get(NEXT_COOKIE), root) or f"{root}/"
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(STATE_COOKIE, path=f"{root}/auth")
    response.delete_cookie(PROVIDER_COOKIE, path=f"{root}/auth")
    response.delete_cookie(PKCE_COOKIE, path=f"{root}/auth")
    response.delete_cookie(NEXT_COOKIE, path=f"{root}/auth")
    set_session_cookie(request, response, email, settings, verified=verified, epoch=epoch)
    # Okta RP-initiated logout needs this hint; without it SSO stays and Continue skips the
    # password.
    if id_token:
        response.set_cookie(
            ID_TOKEN_COOKIE,
            id_token,
            max_age=settings.auth.session_max_age,
            httponly=True,
            secure=_secure(settings),
            samesite="lax",
            path="/",
        )
    return response


def _signed(request: Request, settings: Settings) -> SignedSession | None:
    return verified_session(
        request.cookies.get(SESSION_COOKIE),
        api_token=settings.api_token.get_secret_value(),
        now=datetime.now(UTC),
    )


async def _standing(signed: SignedSession | None, session: AsyncSession) -> SignedSession | None:
    if signed is None or await session_ended(
        session, email=signed.email, verified=signed.verified, epoch=signed.epoch
    ):
        return None
    return signed


async def live_session(
    request: Request, settings: Settings, session: AsyncSession
) -> SignedSession | None:
    """The request's signed session, unless it has expired or ended (`session_ended`)."""
    return await _standing(_signed(request, settings), session)


@router.get("/auth/me")
async def me(
    request: Request, settings: SettingsDep, account_sessions: AccountSessionsDep
) -> dict[str, str | bool]:
    """Who is signed in, and whether they must verify their email before anything else
    (design.md §7.19 item 17)."""
    signed = _signed(request, settings)
    if signed is not None:
        # Only a validly signed cookie costs a connection.
        async with account_sessions() as session:
            signed = await _standing(signed, session)
    if signed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="You are not signed in."
        )
    return {
        "email": signed.email,
        "verified": signed.verified,
        "must_verify": needs_verification(verified=signed.verified, mail=settings.mail.enabled),
    }


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(ID_TOKEN_COOKIE, path="/")


@router.get("/auth/logout")
async def logout_and_leave_idp(request: Request, settings: SettingsDep) -> RedirectResponse:
    """Clear the factory session, then end the IdP session when the issuer supports it.

    A POST only drops our cookie. Okta (and any OIDC issuer with `end_session_endpoint`)
    would still have an SSO session, so the next Continue with Okta would skip the password.
    """
    # The bare origin, not the prefix: it is the post-logout URI the IdP has registered, and the
    # app 302s it into the prefix.
    root = _app_root(request)
    destination = (
        settings.auth.external_url.rstrip("/") + "/" if settings.auth.external_url else f"{root}/"
    )
    if request.headers.get("sec-fetch-site") == "cross-site":
        # A GET can be a link on another site; only the dashboard's own sign-out, or the address
        # bar, ends a session this way. A browser too old to send the header is let through.
        return RedirectResponse(destination, status_code=status.HTTP_302_FOUND)
    end_session = ""
    if settings.auth.oidc_enabled:
        try:
            end_session = (await _oidc_endpoints(settings.auth)).get("end_session") or ""
        except HTTPException:
            end_session = ""
    if end_session:
        params = {
            "post_logout_redirect_uri": destination,
            "client_id": settings.auth.resolved_oidc_client_id(),
        }
        hint = request.cookies.get(ID_TOKEN_COOKIE)
        if hint:
            params["id_token_hint"] = hint
        response = RedirectResponse(
            f"{end_session}?{urlencode(params)}", status_code=status.HTTP_302_FOUND
        )
    else:
        response = RedirectResponse(
            destination if destination.startswith("http") else f"{root}/",
            status_code=status.HTTP_302_FOUND,
        )
    _clear_session_cookies(response)
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response)
    return response


@router.post("/auth/logout/everywhere", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(
    request: Request, settings: SettingsDep, account_sessions: AccountSessionsDep
) -> Response:
    """End every session of the signed-in account, in every browser: a sign-out drops only this
    browser's cookie, and a copy of it would keep working until it expired."""
    if not from_the_dashboard(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign out from the dashboard.")
    async with account_sessions() as session:
        signed = await live_session(request, settings, session)
        user = None if signed is None else await user_by_email(session, signed.email)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "You are not signed in.")
        await end_sessions(session, user)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response)
    return response
