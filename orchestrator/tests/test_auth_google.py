"""Google sign-in for the dashboard: settings, sessions, the OAuth routes, and the auth union.

The exchange with Google is behind a dependency and overridden here — these tests prove our half
of the flow (state, allowlist, cookie, auth union), not Google's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import MEMBER, OWNER
from orchestrator.access.decision import ALL_PERMISSIONS, OPERATOR, Caller
from orchestrator.api.routes.auth import GoogleIdentity, OidcIdentity, google_identity_dependency
from orchestrator.api.services import membership
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import SESSION_COOKIE, mint_session, verify_session
from orchestrator.db.models import AppUser, OrganizationInvite, OrganizationMember, Role
from orchestrator.settings import AuthSettings, MultiOrgSettings
from tests.api_harness import TOKEN, build_app, client_for, make_settings
from tests.members import ensure_member

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _an_install_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests sign in to a deployment that has data: whether this test database happens to
    be empty must not decide a refusal. The empty install has its own tests."""

    async def no(session: AsyncSession, **_: object) -> bool:
        return False

    monkeypatch.setattr(membership, "install_is_empty", no)


DASH = "https://dash.example.ts.net"
NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)


def enabled_auth() -> AuthSettings:
    return AuthSettings(
        google_client_id="cid-123.apps.googleusercontent.com",
        google_client_secret=SecretStr("csecret"),
        allowed_domains=["leaninnovationlabs.com"],
        external_url=DASH,
    )


# --- settings ----------------------------------------------------------------


def test_auth_disabled_by_default() -> None:
    assert make_settings().auth.enabled is False


def test_auth_enabled_needs_all_three() -> None:
    assert enabled_auth().enabled is True
    assert AuthSettings(google_client_id="x").enabled is False
    assert AuthSettings(google_client_id="x", google_client_secret=SecretStr("y")).enabled is False


def test_allowed_domains_accepts_comma_string() -> None:
    auth = AuthSettings(allowed_domains="a.com, B.com")  # type: ignore[arg-type]
    assert auth.allowed_domains == ["a.com", "b.com"]


# --- sessions ----------------------------------------------------------------


def test_session_roundtrip() -> None:
    token = mint_session("user@example.com", verified=True, api_token=TOKEN, now=NOW, max_age=3600)
    email = verify_session(token, api_token=TOKEN, now=NOW + timedelta(minutes=59))
    assert email == "user@example.com"


def test_session_expires() -> None:
    token = mint_session("user@example.com", verified=True, api_token=TOKEN, now=NOW, max_age=3600)
    assert verify_session(token, api_token=TOKEN, now=NOW + timedelta(hours=2)) is None


def test_session_rejects_tamper() -> None:
    token = mint_session("user@example.com", verified=True, api_token=TOKEN, now=NOW, max_age=3600)
    forged = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert verify_session(forged, api_token=TOKEN, now=NOW) is None


def test_session_key_is_the_api_token() -> None:
    token = mint_session("user@example.com", verified=True, api_token=TOKEN, now=NOW, max_age=3600)
    assert verify_session(token, api_token="rotated", now=NOW) is None
    assert verify_session(None, api_token=TOKEN, now=NOW) is None
    assert verify_session("not-a-token", api_token=TOKEN, now=NOW) is None


# --- /auth/login -------------------------------------------------------------


async def test_login_404_when_disabled() -> None:
    async with client_for(build_app(make_settings()), base_url=DASH) as client:
        response = await client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 404


async def test_login_redirects_to_google_with_state_cookie() -> None:
    async with client_for(build_app(make_settings(auth=enabled_auth())), base_url=DASH) as client:
        response = await client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 307 or response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid-123.apps.googleusercontent.com" in location
    redirect_uri = "https%3A%2F%2Fdash.example.ts.net%2Ffactorial%2Fauth%2Fcallback"
    assert f"redirect_uri={redirect_uri}" in location
    state_cookie = response.cookies.get("sf_oauth_state")
    assert state_cookie
    assert f"state={state_cookie}" in location


# --- /auth/callback ----------------------------------------------------------


def identity(email: str = "person@leaninnovationlabs.com", verified: bool = True):
    async def _dependency() -> GoogleIdentity:
        async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
            return email, verified

        return exchange

    return _dependency


async def callback(app: FastAPI, *, state_cookie: str | None, state_param: str | None):
    async with client_for(app, base_url=DASH) as client:
        if state_cookie is not None:
            client.cookies.set("sf_oauth_state", state_cookie)
        params = {"code": "auth-code"}
        if state_param is not None:
            params["state"] = state_param
        return await client.get("/auth/callback", params=params)


async def test_callback_rejects_missing_state(async_session: AsyncSession) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    for cookie, param in ((None, "s"), ("s", None), ("s", "other")):
        response = await callback(app, state_cookie=cookie, state_param=param)
        assert response.headers["location"] == "/?signin=retry"
        assert response.cookies.get(SESSION_COOKIE) is None


async def test_callback_sets_session_and_redirects(async_session: AsyncSession) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    response = await callback(app, state_cookie="s", state_param="s")
    assert response.status_code == 302 or response.status_code == 307
    assert response.headers["location"] == "/"
    session = response.cookies.get(SESSION_COOKIE)
    assert session
    assert verify_session(session, api_token=TOKEN, now=datetime.now(UTC)) == (
        "person@leaninnovationlabs.com"
    )


async def test_login_carries_a_local_next_path_to_the_callback(async_session: AsyncSession) -> None:
    # A lapsed session renders the sign-in page in place, so the page the reader was on is the
    # URL; the round trip through Google has to come back to it rather than to `/`.
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    async with client_for(app, base_url=DASH) as client:
        login = await client.get("/auth/login", params={"next": "/tickets/204?tab=activity"})
        state = login.cookies.get("sf_oauth_state")
        assert state
        response = await client.get("/auth/callback", params={"code": "c", "state": state})
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/tickets/204?tab=activity"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.com/",
        "//evil.example.com/",
        "/\\evil.example.com",
        "javascript:alert(1)",
        "tickets/204",
        "/tickets/204\r\nSet-Cookie: x=1",
        "/" + "a" * 2100,
        "/\u4e2d/tickets",
    ],
)
async def test_login_refuses_a_next_that_could_leave_the_origin(
    target: str, async_session: AsyncSession
) -> None:
    # An open redirect would make this sign-in page a launch point for phishing: only a path on
    # this origin comes back, and anything else lands on `/` as before.
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    async with client_for(app, base_url=DASH) as client:
        login = await client.get("/auth/login", params={"next": target})
        state = login.cookies.get("sf_oauth_state")
        assert state
        response = await client.get("/auth/callback", params={"code": "c", "state": state})
    assert response.headers["location"] == "/"


async def test_login_without_next_drops_one_left_from_an_earlier_attempt(
    async_session: AsyncSession,
) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    async with client_for(app, base_url=DASH) as client:
        await client.get("/auth/login", params={"next": "/runtimes"})
        login = await client.get("/auth/login")
        state = login.cookies.get("sf_oauth_state")
        assert state
        response = await client.get("/auth/callback", params={"code": "c", "state": state})
    assert response.headers["location"] == "/"


async def test_callback_rejects_foreign_domain(async_session: AsyncSession) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email="a@gmail.com")
    response = await callback(app, state_cookie="s", state_param="s")
    # The reason, never the address: a URL lands in history and logs.
    assert response.headers["location"] == "/?signin=refused"
    assert response.cookies.get(SESSION_COOKIE) is None


async def _role_in_default(session: AsyncSession, email: str) -> str | None:
    return await session.scalar(
        select(Role.key)
        .join(OrganizationMember, OrganizationMember.role_id == Role.id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .where(
            AppUser.email == email,
            OrganizationMember.organization_id == await default_organization_id(session),
        )
    )


async def _signed_in_as(response: Any) -> str | None:
    cookie = response.cookies.get(SESSION_COOKIE)
    return verify_session(cookie, api_token=TOKEN, now=datetime.now(UTC)) if cookie else None


async def test_an_allowed_domain_joins_default_as_a_member(async_session: AsyncSession) -> None:
    # One organization: the domain list is how a deployment that admitted its domain before
    # accounts keeps admitting it (design.md §7.19).
    email = f"new-{uuid4().hex[:8]}@leaninnovationlabs.com"
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email=email)

    response = await callback(app, state_cookie="s", state_param="s")

    assert await _signed_in_as(response) == email
    assert await _role_in_default(async_session, email) == MEMBER


async def test_a_removed_member_in_the_domain_stays_out_until_they_accept_an_invite(
    async_session: AsyncSession,
) -> None:
    email = f"gone-{uuid4().hex[:8]}@leaninnovationlabs.com"
    await ensure_member(async_session, email, role=MEMBER)
    user = await async_session.scalar(select(AppUser).where(AppUser.email == email))
    assert user is not None
    default = await default_organization_id(async_session)
    await membership.remove_member(
        async_session,
        Caller(email),
        frozenset(),
        organization_id=default,
        user_id=user.id,
        now=datetime.now(UTC),
    )
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email=email)

    # Still an account in the domain, so still signed in: just a member of nothing.
    response = await callback(app, state_cookie="s", state_param="s")
    assert await _signed_in_as(response) == email
    assert await _role_in_default(async_session, email) is None

    invite = await membership.create_invite(
        async_session,
        OPERATOR,
        ALL_PERMISSIONS,
        organization_id=default,
        email=email,
        role=MEMBER,
        now=datetime.now(UTC),
    )
    await membership.accept_invite(
        async_session, Caller(email), invite.token, now=datetime.now(UTC)
    )
    await async_session.execute(
        delete(OrganizationMember).where(OrganizationMember.user_id == user.id)
    )
    # The invite cleared the removal, so the domain joins them again as it did before.
    await callback(app, state_cookie="s", state_param="s")
    assert await _role_in_default(async_session, email) == MEMBER


@pytest.mark.parametrize("why", ["invited", "member"])
async def test_an_outside_email_signs_in_with_an_invite_or_a_membership(
    async_session: AsyncSession, why: str
) -> None:
    email = f"guest-{uuid4().hex[:8]}@gmail.com"
    if why == "invited":
        async_session.add(
            OrganizationInvite(
                organization_id=await default_organization_id(async_session),
                email=email,
                role_id=await async_session.scalar(select(Role.id).where(Role.key == MEMBER)),
                token_hash=uuid4().bytes,
                created_by="owner@leaninnovationlabs.com",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await async_session.flush()
    else:
        await ensure_member(async_session, email, role=MEMBER)
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email=email)

    response = await callback(app, state_cookie="s", state_param="s")

    assert await _signed_in_as(response) == email
    # An invite is accepted on its own page; the sign-in joins nothing by itself.
    assert await _role_in_default(async_session, email) == (None if why == "invited" else MEMBER)


async def test_the_first_sign_in_on_an_empty_install_owns_default(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(session: AsyncSession, **_: object) -> bool:
        return True

    monkeypatch.setattr(membership, "install_is_empty", empty)
    email = f"first-{uuid4().hex[:8]}@gmail.com"
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email=email)

    response = await callback(app, state_cookie="s", state_param="s")

    assert await _signed_in_as(response) == email
    assert await _role_in_default(async_session, email) == OWNER


async def test_with_multi_org_on_anyone_signs_in_and_joins_nothing(
    async_session: AsyncSession,
) -> None:
    settings = make_settings(auth=enabled_auth())
    settings.multi_org = MultiOrgSettings(enabled=True)
    email = f"anyone-{uuid4().hex[:8]}@gmail.com"
    app = build_app(settings, session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(email=email)

    response = await callback(app, state_cookie="s", state_param="s")

    assert await _signed_in_as(response) == email
    assert await _role_in_default(async_session, email) is None


async def test_callback_domain_match_is_suffix_after_last_at_case_insensitive(
    async_session: AsyncSession,
) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(
        email="Person@LeanInnovationLabs.COM"
    )
    response = await callback(app, state_cookie="s", state_param="s")
    assert response.headers["location"] == "/"
    assert response.cookies.get(SESSION_COOKIE)


async def test_callback_rejects_unverified_email(async_session: AsyncSession) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity(verified=False)
    response = await callback(app, state_cookie="s", state_param="s")
    assert response.headers["location"] == "/?signin=refused"
    assert response.cookies.get(SESSION_COOKIE) is None


# --- /auth/me and /auth/logout ----------------------------------------------


async def test_me_reports_the_session(async_session: AsyncSession) -> None:
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    await ensure_member(async_session, "p@leaninnovationlabs.com")
    token = mint_session(
        "p@leaninnovationlabs.com",
        verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=600
    )
    async with client_for(app, base_url=DASH) as client:
        assert (await client.get("/auth/me")).status_code == 401
        client.cookies.set(SESSION_COOKIE, token)
        response = await client.get("/auth/me")
    assert response.status_code == 200
    assert response.json() == {"email": "p@leaninnovationlabs.com"}


async def test_logout_clears_the_cookie(async_session: AsyncSession) -> None:
    """Through the real flow: the callback sets the cookie, logout must unset that same
    cookie — name, path and domain all have to line up for a browser to drop it."""
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        await client.get("/auth/callback", params={"code": "c", "state": "s"})
        assert (await client.get("/auth/me")).status_code == 200
        assert (await client.post("/auth/logout")).status_code == 204
        assert (await client.get("/auth/me")).status_code == 401


async def test_a_cross_site_link_to_the_browser_logout_signs_no_one_out(
    async_session: AsyncSession,
) -> None:
    """The GET logout exists so the dashboard can send the browser through the IdP; a link on
    another site must not end a session with it."""
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    app.dependency_overrides[google_identity_dependency] = identity()
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        await client.get("/auth/callback", params={"code": "c", "state": "s"})
        crossed = await client.get(
            "/auth/logout", headers={"sec-fetch-site": "cross-site"}, follow_redirects=False
        )
        assert (await client.get("/auth/me")).status_code == 200
        await client.get(
            "/auth/logout", headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
        )
        assert (await client.get("/auth/me")).status_code == 401
    assert crossed.status_code == 302


# --- the auth union ----------------------------------------------------------


@pytest.mark.postgres
async def test_session_cookie_satisfies_the_api_guard(async_session: AsyncSession) -> None:
    """A valid session is the second way through the same gate the bearer token guards, for a
    member of the organization (design.md §7.19)."""
    app = build_app(make_settings(auth=enabled_auth()), session=async_session)
    await ensure_member(async_session, "p@leaninnovationlabs.com")
    token = mint_session(
        "p@leaninnovationlabs.com",
        verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=600
    )
    async with client_for(app, base_url=DASH) as client:
        assert (await client.get("/api/orgs/default/projects")).status_code == 401
        client.cookies.set(SESSION_COOKIE, token)
        response = await client.get("/api/orgs/default/projects")
    assert response.status_code == 200


async def test_expired_session_is_still_401() -> None:
    app = build_app(make_settings(auth=enabled_auth()))
    stale = mint_session(
        "p@leaninnovationlabs.com",
        verified=True, api_token=TOKEN,
        now=datetime.now(UTC) - timedelta(days=8),
        max_age=7 * 24 * 3600,
    )
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set(SESSION_COOKIE, stale)
        assert (await client.get("/auth/me")).status_code == 401


# --- /auth/providers ---------------------------------------------------------


async def test_providers_reports_google_availability(async_session: AsyncSession) -> None:
    async with client_for(
        build_app(make_settings(), session=async_session), base_url=DASH
    ) as client:
        assert (await client.get("/auth/providers")).json() == {
            "google": False,
            "oidc": False,
            "oidc_label": "SSO",
            "password": True,
            "signup": False,
            "multi_org": False,
            "setup_token": False,
        }
    async with client_for(
        build_app(make_settings(auth=enabled_auth()), session=async_session), base_url=DASH
    ) as client:
        assert (await client.get("/auth/providers")).json() == {
            "google": True,
            "oidc": False,
            "oidc_label": "SSO",
            "password": True,
            "signup": False,
            "multi_org": False,
            "setup_token": False,
        }


# --- OIDC --------------------------------------------------------------------


def oidc_auth() -> AuthSettings:
    return AuthSettings(
        oidc_issuer="https://id.example.okta.com",
        oidc_client_id="okta-client",
        oidc_client_secret=SecretStr("okta-secret"),
        oidc_authorize_url="https://id.example.okta.com/oauth2/v1/authorize",
        oidc_token_url="https://id.example.okta.com/oauth2/v1/token",
        oidc_userinfo_url="https://id.example.okta.com/oauth2/v1/userinfo",
        oidc_label="Okta",
        allowed_domains=["leaninnovationlabs.com"],
        external_url=DASH,
    )


def test_oidc_enabled_needs_client_and_endpoints() -> None:
    assert oidc_auth().oidc_enabled is True
    assert oidc_auth().google_enabled is False
    assert AuthSettings(oidc_client_id="x", external_url=DASH).oidc_enabled is False
    assert (
        AuthSettings(
            oidc_issuer="https://id.example.okta.com",
            oidc_client_id="x",
            oidc_client_secret=SecretStr("y"),
            external_url=DASH,
        ).oidc_enabled
        is True
    )


def oidc_identity(
    email: str = "person@leaninnovationlabs.com",
    verified: bool = True,
    id_token: str | None = "oidc-id-token",
):
    async def _dependency() -> OidcIdentity:
        async def exchange(
            code: str, redirect_uri: str, code_verifier: str | None
        ) -> tuple[str, bool, str | None]:
            return email, verified, id_token

        return exchange

    return _dependency


async def test_login_falls_back_to_oidc_when_google_is_off() -> None:
    async with client_for(build_app(make_settings(auth=oidc_auth())), base_url=DASH) as client:
        response = await client.get("/auth/login", follow_redirects=False)
    assert response.status_code in {302, 307}
    location = response.headers["location"]
    assert location.startswith("https://id.example.okta.com/oauth2/v1/authorize?")
    assert "client_id=okta-client" in location
    assert response.cookies.get("sf_oauth_provider") == "oidc"


async def test_oidc_callback_mints_a_session(async_session: AsyncSession) -> None:
    from orchestrator.api.routes.auth import oidc_identity_dependency

    app = build_app(make_settings(auth=oidc_auth()), session=async_session)
    app.dependency_overrides[oidc_identity_dependency] = oidc_identity()
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        client.cookies.set("sf_oauth_provider", "oidc")
        response = await client.get("/auth/callback", params={"code": "c", "state": "s"})
        assert response.status_code in {302, 307}
        assert (await client.get("/auth/me")).status_code == 200


async def test_an_empty_allowlist_refuses_every_account(async_session: AsyncSession) -> None:
    from orchestrator.api.routes.auth import oidc_identity_dependency

    auth = oidc_auth()
    auth.allowed_domains = []
    app = build_app(make_settings(auth=auth), session=async_session)
    app.dependency_overrides[oidc_identity_dependency] = oidc_identity()
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        client.cookies.set("sf_oauth_provider", "oidc")
        response = await client.get("/auth/callback", params={"code": "c", "state": "s"})
        assert response.status_code in {302, 307}
        assert "signin=refused" in response.headers["location"]


async def test_providers_reports_oidc(async_session: AsyncSession) -> None:
    async with client_for(
        build_app(make_settings(auth=oidc_auth()), session=async_session), base_url=DASH
    ) as client:
        assert (await client.get("/auth/providers")).json() == {
            "google": False,
            "oidc": True,
            "oidc_label": "Okta",
            "password": True,
            "signup": False,
            "multi_org": False,
            "setup_token": False,
        }


def okta_auth() -> AuthSettings:
    return AuthSettings(
        okta_domain="okta.example.com",
        okta_token_domain="okta-token.example.com",
        okta_client_id="okta-web",
        okta_client_secret=SecretStr("okta-secret"),
        allowed_domains=["example.org"],
        external_url=DASH,
    )


def test_okta_settings_build_split_host_endpoints() -> None:
    """Browser host != token host, authorization server `default`."""
    auth = okta_auth()
    assert auth.oidc_enabled is True
    assert auth.resolved_oidc_label() == "Okta"
    assert auth.okta_endpoints() == {
        "authorize": "https://okta.example.com/oauth2/default/v1/authorize",
        "token": "https://okta-token.example.com/oauth2/default/v1/token",
        "userinfo": "https://okta-token.example.com/oauth2/default/v1/userinfo",
        "end_session": "https://okta.example.com/oauth2/default/v1/logout",
    }


def test_okta_accepts_https_prefix_on_domain() -> None:
    auth = AuthSettings(
        okta_domain="https://example.okta.com/",
        okta_client_id="id",
        okta_client_secret=SecretStr("secret"),
        external_url=DASH,
    )
    endpoints = auth.okta_endpoints()
    assert endpoints is not None
    assert endpoints["authorize"] == "https://example.okta.com/oauth2/default/v1/authorize"


async def test_okta_login_uses_pkce_and_browser_host() -> None:
    async with client_for(build_app(make_settings(auth=okta_auth())), base_url=DASH) as client:
        response = await client.get("/auth/oidc/login", follow_redirects=False)
    assert response.status_code in {302, 307}
    location = response.headers["location"]
    assert location.startswith("https://okta.example.com/oauth2/default/v1/authorize?")
    assert "client_id=okta-web" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert response.cookies.get("sf_oauth_pkce")


async def test_providers_reports_okta(async_session: AsyncSession) -> None:
    async with client_for(
        build_app(make_settings(auth=okta_auth()), session=async_session), base_url=DASH
    ) as client:
        assert (await client.get("/auth/providers")).json() == {
            "google": False,
            "oidc": True,
            "oidc_label": "Okta",
            "password": True,
            "signup": False,
            "multi_org": False,
            "setup_token": False,
        }


async def test_browser_logout_sends_the_browser_to_the_idp() -> None:
    app = build_app(make_settings(auth=okta_auth()))
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_id_token", "oidc-id-token")
        response = await client.get("/auth/logout", follow_redirects=False)
    assert response.status_code in {302, 307}
    location = response.headers["location"]
    assert location.startswith("https://okta.example.com/oauth2/default/v1/logout?")
    assert "id_token_hint=oidc-id-token" in location
    assert "client_id=okta-web" in location
    assert "post_logout_redirect_uri=" in location


async def test_oidc_callback_keeps_the_id_token_for_logout(async_session: AsyncSession) -> None:
    from orchestrator.api.routes.auth import oidc_identity_dependency

    app = build_app(make_settings(auth=okta_auth()), session=async_session)
    app.dependency_overrides[oidc_identity_dependency] = oidc_identity(email="operator@example.org")
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        client.cookies.set("sf_oauth_provider", "oidc")
        minted = await client.get("/auth/callback", params={"code": "c", "state": "s"})
        assert minted.cookies.get("sf_id_token") == "oidc-id-token"
        response = await client.get("/auth/logout", follow_redirects=False)
    assert "id_token_hint=oidc-id-token" in response.headers["location"]


def test_an_extra_ca_bundle_adds_to_the_public_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private IdP's CA in REQUESTS_CA_BUNDLE must not cost the public roots that Google's
    token and userinfo endpoints are signed by."""
    import certifi
    import httpx

    from orchestrator.api.routes import auth

    public = Path(certifi.where()).read_text()
    first = public[public.index("-----BEGIN CERTIFICATE-----") :]
    one = first[: first.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    private = tmp_path / "private-ca.pem"
    private.write_text(one + "\n")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(private))

    trusted = httpx.create_ssl_context(verify=auth._tls_verify()).get_ca_certs()

    assert len(trusted) > 1
