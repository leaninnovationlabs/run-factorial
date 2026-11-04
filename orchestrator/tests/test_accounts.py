"""Accounts (design.md §7.19): signup, password sign-in with its lockout, SSO sign-in and the
takeover of an unverified account, and the routes behind the multi-org flag."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message, Receive, Scope, Send

from orchestrator.access import MEMBER, OWNER
from orchestrator.access import accounts as accounts_module
from orchestrator.access.accounts import (
    LOCKOUT,
    MAX_FAILED_SIGN_INS,
    AccountExists,
    AccountLocked,
    HasherBusy,
    InvalidAccountInput,
    SignInRefused,
    create_account,
    password_sign_in,
    set_password,
    sso_sign_in,
    user_by_email,
)
from orchestrator.api.deps import account_sessions_dependency
from orchestrator.api.ratelimit import RateLimit, UntrustedForwardedFor, client_key
from orchestrator.api.routes.accounts import PASSWORD_TRIES_PER_MINUTE
from orchestrator.api.routes.auth import google_identity_dependency
from orchestrator.api.services import membership
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import SESSION_COOKIE, mint_session, verify_session
from orchestrator.db.models import AppUser, OrganizationInvite, OrganizationMember, Role
from orchestrator.settings import AuthSettings, MultiOrgSettings, Settings
from tests.api_harness import TOKEN, build_app, client_for, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture(autouse=True)
def _an_install_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether this test database happens to be empty must not decide who owns `default`: the
    empty install has its own tests."""

    async def no(session: AsyncSession, **_: object) -> bool:
        return False

    monkeypatch.setattr(membership, "install_is_empty", no)


#: The real query, before `_an_install_with_data` replaces it.
_INSTALL_IS_EMPTY = membership.install_is_empty
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
IP = "203.0.113.7"
DASH = "https://dash.example.ts.net"


def _email() -> str:
    return f"Person-{uuid4().hex[:10]}@Example.com"


def _settings(*, multi_org: bool = True, auth: AuthSettings | None = None) -> Settings:
    settings = make_settings(auth=auth)
    settings.multi_org = MultiOrgSettings(enabled=multi_org)
    return settings


# --- the service --------------------------------------------------------------


async def test_a_password_guess_holds_no_lock_on_the_account_while_it_hashes(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row lock held across the hash gate let guesses at one email pin pooled connections on
    that row, queueing its owner's own sign-in behind them."""
    email = _email()
    async with maker() as session, session.begin():
        await create_account(session, email=email, name="Owner", password=PASSWORD, now=NOW)
    hashing, release = asyncio.Event(), asyncio.Event()
    real = accounts_module._matches  # pyright: ignore[reportPrivateUsage]

    async def held(password_hash: str | None, password: str) -> bool:
        hashing.set()
        await release.wait()
        return await real(password_hash, password)

    monkeypatch.setattr(accounts_module, "_matches", held)

    async def guess() -> None:
        async with maker() as session, session.begin():
            with contextlib.suppress(SignInRefused):
                await password_sign_in(
                    session, email=email, password="a wrong guess", now=NOW, client=IP
                )

    task = asyncio.create_task(guess())
    try:
        await asyncio.wait_for(hashing.wait(), 5)
        async with maker() as other, other.begin():
            locked = select(AppUser.id).where(AppUser.email == email.lower())
            assert await other.scalar(locked.with_for_update(nowait=True)) is not None
    finally:
        release.set()
        await task
        async with maker() as session, session.begin():
            await session.execute(delete(AppUser).where(AppUser.email == email.lower()))


async def test_an_account_is_its_lowercase_email(async_session: AsyncSession) -> None:
    email = _email()
    user = await create_account(
        async_session, email=f"  {email} ", name=" Operator ", password=PASSWORD, now=NOW
    )
    assert user.email == email.lower()
    assert user.name == "Operator"
    assert user.password_hash and PASSWORD not in user.password_hash
    assert user.email_verified_at is None
    with pytest.raises(AccountExists):
        await create_account(
            async_session, email=email.upper(), name="X", password=PASSWORD, now=NOW
        )


@pytest.mark.parametrize(
    ("email", "password"),
    [("no-at-sign", PASSWORD), ("a@b", PASSWORD), (None, "short"), (None, "x" * 257)],
)
async def test_signup_refuses_what_cannot_be_an_account(
    async_session: AsyncSession, email: str | None, password: str
) -> None:
    with pytest.raises(InvalidAccountInput):
        await create_account(
            async_session, email=email or _email(), name="Operator", password=password, now=NOW
        )


async def _sign_in(
    session: AsyncSession, email: str, password: str, *, now: datetime = NOW, client: str = IP
) -> AppUser:
    return await password_sign_in(session, email=email, password=password, now=now, client=client)


async def _set_password(
    session: AsyncSession, user: AppUser, current: str | None, password: str
) -> None:
    await set_password(session, user, current=current, password=password, now=NOW, client=IP)


async def test_a_password_signs_in_and_clears_earlier_failures(
    async_session: AsyncSession,
) -> None:
    email = _email()
    await create_account(async_session, email=email, name="Operator", password=PASSWORD, now=NOW)
    for _ in range(MAX_FAILED_SIGN_INS - 1):
        with pytest.raises(SignInRefused):
            await _sign_in(async_session, email, "wrong password!")
    user = await _sign_in(async_session, email, PASSWORD)
    assert user.last_sign_in_at == NOW
    with pytest.raises(SignInRefused):
        await _sign_in(async_session, email, "wrong password!")
    assert (await _sign_in(async_session, email, PASSWORD)).email


async def test_an_unknown_email_is_refused_as_a_wrong_password_is(
    async_session: AsyncSession,
) -> None:
    with pytest.raises(SignInRefused):
        await _sign_in(async_session, _email(), PASSWORD)
    with pytest.raises(SignInRefused):
        await _sign_in(async_session, "not an email", PASSWORD)


async def test_ten_failures_lock_that_address_out_until_the_lockout_ends(
    async_session: AsyncSession,
) -> None:
    """Even with the right password, and only from that address: keyed on the account alone,
    anyone's ten wrong guesses locked its owner out."""
    email = _email()
    await create_account(async_session, email=email, name="Operator", password=PASSWORD, now=NOW)
    for _ in range(MAX_FAILED_SIGN_INS):
        with pytest.raises(SignInRefused):
            await _sign_in(async_session, email, "wrong password!")
    with pytest.raises(AccountLocked) as locked:
        await _sign_in(async_session, email, PASSWORD)
    assert locked.value.until == NOW + LOCKOUT
    assert (await _sign_in(async_session, email, PASSWORD, client="198.51.100.9")).email
    later = NOW + LOCKOUT + timedelta(seconds=1)
    assert (await _sign_in(async_session, email, PASSWORD, now=later)).email


async def test_the_first_sso_sign_in_creates_a_verified_account(
    async_session: AsyncSession,
) -> None:
    email = _email()
    user = await sso_sign_in(async_session, email=email, now=NOW)
    assert user.email == email.lower()
    assert user.email_verified_at == NOW
    assert user.password_hash is None
    assert user.name == email.lower().split("@")[0]


async def test_sso_takes_an_unverified_account_from_whoever_set_its_password(
    async_session: AsyncSession,
) -> None:
    """A stranger can register someone's address first with a password; the mailbox's owner
    signing in by SSO keeps the account and the stranger's password stops working."""
    email = _email()
    await create_account(async_session, email=email, name="Squatter", password=PASSWORD, now=NOW)
    user = await sso_sign_in(async_session, email=email, now=NOW)
    assert user.email_verified_at == NOW
    assert user.password_hash is None
    with pytest.raises(SignInRefused):
        await _sign_in(async_session, email, PASSWORD)


async def test_sso_keeps_the_password_of_an_account_it_already_verified(
    async_session: AsyncSession,
) -> None:
    email = _email()
    user = await sso_sign_in(async_session, email=email, now=NOW)
    await _set_password(async_session, user, None, PASSWORD)
    await sso_sign_in(async_session, email=email, now=NOW + timedelta(days=1))
    assert (await _sign_in(async_session, email, PASSWORD)).email


async def test_changing_a_password_needs_the_current_one(async_session: AsyncSession) -> None:
    email = _email()
    user = await create_account(
        async_session, email=email, name="Operator", password=PASSWORD, now=NOW
    )
    with pytest.raises(SignInRefused):
        await _set_password(async_session, user, None, "a new password!")
    with pytest.raises(SignInRefused):
        await _set_password(async_session, user, "wrong password!", "new one!!!!!")
    await _set_password(async_session, user, PASSWORD, "a new password!")
    assert await _sign_in(async_session, email, "a new password!")


async def test_guessing_the_current_password_locks_the_account(
    async_session: AsyncSession,
) -> None:
    """A stolen session must not try current passwords where sign-in would lock it out."""
    email = _email()
    user = await create_account(
        async_session, email=email, name="Operator", password=PASSWORD, now=NOW
    )
    for _ in range(MAX_FAILED_SIGN_INS):
        with pytest.raises(SignInRefused):
            await _set_password(async_session, user, "wrong password!", "new one!!!!!")
    with pytest.raises(AccountLocked):
        await _set_password(async_session, user, PASSWORD, "new one!!!!!")


# --- the routes ---------------------------------------------------------------


def _app(settings: Settings, session: AsyncSession) -> FastAPI:
    app = build_app(settings, session)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[account_sessions_dependency] = lambda: scope
    return app


def _session_email(response) -> str | None:
    return verify_session(
        response.cookies.get(SESSION_COOKIE), api_token=TOKEN, now=datetime.now(UTC)
    )


async def _role_in_default(session: AsyncSession, email: str) -> str | None:
    return await session.scalar(
        select(Role.key)
        .join(OrganizationMember, OrganizationMember.role_id == Role.id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .where(
            AppUser.email == email.lower(),
            OrganizationMember.organization_id == await default_organization_id(session),
        )
    )


async def test_with_one_organization_signup_needs_an_invite(async_session: AsyncSession) -> None:
    # Nobody signs in with the API token any more, so accounts exist with the flag off too
    # (design.md §7.19); signing up is what the flag still keeps closed.
    app = _app(_settings(multi_org=False), async_session)
    body = {"email": _email(), "name": "Operator", "password": PASSWORD}
    known = _email()
    await create_account(async_session, email=known, name="Teammate", password=PASSWORD, now=NOW)
    async with client_for(app) as client:
        refused = await client.post("/auth/signup", json=body)
        signed_in = await client.post(
            "/auth/password/login", json={"email": known, "password": PASSWORD}
        )
        providers = (await client.get("/auth/providers")).json()
    assert refused.status_code == 403
    assert await user_by_email(async_session, body["email"]) is None
    assert signed_in.status_code == 200
    assert (providers["password"], providers["signup"], providers["multi_org"]) == (
        True,
        False,
        False,
    )


async def test_with_one_organization_an_invite_lets_its_email_sign_up(
    async_session: AsyncSession,
) -> None:
    """With its token only: whoever knows the invited address could otherwise take the account
    first, and lock its owner out."""
    email = _email()
    token = uuid4().hex
    async_session.add(
        OrganizationInvite(
            organization_id=await default_organization_id(async_session),
            email=email.lower(),
            role_id=await async_session.scalar(select(Role.id).where(Role.key == MEMBER)),
            token_hash=hashlib.sha256(token.encode()).digest(),
            created_by="owner@example.com",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await async_session.flush()
    app = _app(_settings(multi_org=False), async_session)
    body = {"email": email, "name": "Operator", "password": PASSWORD}
    async with client_for(app) as client:
        no_token = await client.post("/auth/signup", json=body)
        other_email = await client.post(
            "/auth/signup", json={**body, "email": _email(), "invite": token}
        )
        created = await client.post("/auth/signup", json={**body, "invite": token})
    assert no_token.status_code == 403
    assert other_email.status_code == 403
    assert created.status_code == 201, created.text
    assert _session_email(created) == email.lower()
    # The invite is accepted on its own page: signing up joins nothing by itself.
    assert await _role_in_default(async_session, email) is None


@pytest.mark.parametrize("multi_org", [False, True])
async def test_the_first_signup_on_an_empty_install_owns_default(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, multi_org: bool
) -> None:
    async def empty(session: AsyncSession, **_: object) -> bool:
        return True

    monkeypatch.setattr(membership, "install_is_empty", empty)
    app = _app(_settings(multi_org=multi_org), async_session)
    email = _email()
    async with client_for(app) as client:
        assert (await client.get("/auth/providers")).json()["signup"] is True
        created = await client.post(
            "/auth/signup", json={"email": email, "name": "Operator", "password": PASSWORD}
        )
    assert created.status_code == 201, created.text
    assert await _role_in_default(async_session, email) == OWNER


async def test_with_a_setup_token_only_its_holder_owns_an_empty_install(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise whoever reaches a fresh install's port first owns it, and every runtime and
    credential its operator adds later. An SSO sign-in cannot send the token, so it waits."""

    async def empty(session: AsyncSession, **_: object) -> bool:
        return True

    monkeypatch.setattr(membership, "install_is_empty", empty)
    settings = _settings(
        multi_org=False,
        auth=AuthSettings(
            google_client_id="cid",
            google_client_secret=SecretStr("secret"),
            allowed_domains=["example.com"],
            external_url=DASH,
            bootstrap_token=SecretStr("the-setup-token"),
        ),
    )
    app = _app(settings, async_session)
    stranger, sso, owner = _email(), _email(), _email()

    async def identity():
        async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
            return sso, True

        return exchange

    app.dependency_overrides[google_identity_dependency] = identity
    body = {"name": "Operator", "password": PASSWORD}
    async with client_for(app, base_url=DASH) as client:
        providers = (await client.get("/auth/providers")).json()
        bare = await client.post("/auth/signup", json={**body, "email": stranger})
        wrong = await client.post(
            "/auth/signup", json={**body, "email": stranger, "setup_token": "a guess"}
        )
        client.cookies.set("sf_oauth_state", "s")
        signed_in = await client.get("/auth/callback", params={"code": "c", "state": "s"})
        created = await client.post(
            "/auth/signup", json={**body, "email": owner, "setup_token": "the-setup-token"}
        )
    assert providers["setup_token"] is True
    assert (bare.status_code, wrong.status_code) == (403, 403)
    assert await user_by_email(async_session, stranger) is None
    assert signed_in.headers["location"].endswith("signin=refused")
    assert await user_by_email(async_session, sso) is None
    assert created.status_code == 201, created.text
    assert await _role_in_default(async_session, owner) == OWNER


async def test_with_a_setup_token_a_stranger_s_organization_cannot_shut_its_holder_out(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With multi-org on, a stranger signed up without the token and made an organization; the
    install then had data, and the token's holder could never own `default`."""

    async def no_owner(session: AsyncSession) -> bool:
        return False

    monkeypatch.setattr(membership, "install_is_empty", _INSTALL_IS_EMPTY)
    monkeypatch.setattr(membership, "default_has_owner", no_owner)
    settings = _settings(
        multi_org=True,
        auth=AuthSettings(external_url=DASH, bootstrap_token=SecretStr("the-setup-token")),
    )
    app = _app(settings, async_session)
    stranger, owner = _email(), _email()
    body = {"name": "Operator", "password": PASSWORD}
    async with client_for(app, base_url=DASH) as client:
        client.headers["Origin"] = DASH
        joined = await client.post("/auth/signup", json={**body, "email": stranger})
        organization = await client.post(
            "/api/orgs", json={"name": "Squat", "slug": f"squat-{uuid4().hex[:8]}"}
        )
        client.cookies.clear()
        created = await client.post(
            "/auth/signup", json={**body, "email": owner, "setup_token": "the-setup-token"}
        )
    assert joined.status_code == 201, joined.text
    assert created.status_code == 201, created.text
    assert await _role_in_default(async_session, owner) == OWNER
    assert await _role_in_default(async_session, stranger) is None
    assert organization.status_code == 403, organization.text


async def test_a_signup_on_an_install_with_data_owns_nothing(async_session: AsyncSession) -> None:
    app = _app(_settings(), async_session)
    email = _email()
    async with client_for(app) as client:
        created = await client.post(
            "/auth/signup", json={"email": email, "name": "Operator", "password": PASSWORD}
        )
    assert created.status_code == 201, created.text
    assert await _role_in_default(async_session, email) is None


async def test_signup_signs_the_browser_in(async_session: AsyncSession) -> None:
    app = _app(_settings(), async_session)
    email = _email()
    async with client_for(app) as client:
        assert (await client.get("/auth/providers")).json()["signup"] is True
        created = await client.post(
            "/auth/signup", json={"email": email, "name": "Operator", "password": PASSWORD}
        )
        again = await client.post(
            "/auth/signup", json={"email": email, "name": "Operator", "password": PASSWORD}
        )
        short = await client.post(
            "/auth/signup", json={"email": _email(), "name": "Operator", "password": "short"}
        )
    assert created.status_code == 201
    assert created.json() == {"email": email.lower(), "name": "Operator"}
    assert _session_email(created) == email.lower()
    assert again.status_code == 409
    assert short.status_code == 422


async def test_a_sign_in_posted_from_another_site_is_refused(
    async_session: AsyncSession,
) -> None:
    app = _app(_settings(), async_session)
    email = _email()
    cross = {"Sec-Fetch-Site": "cross-site"}
    async with client_for(app) as client:
        signup = await client.post(
            "/auth/signup",
            json={"email": email, "name": "Operator", "password": PASSWORD},
            headers=cross,
        )
        await client.post(
            "/auth/signup", json={"email": email, "name": "Operator", "password": PASSWORD}
        )
        login = await client.post(
            "/auth/password/login", json={"email": email, "password": PASSWORD}, headers=cross
        )
    assert signup.status_code == 403
    assert login.status_code == 403
    assert login.cookies.get(SESSION_COOKIE) is None


async def test_password_tries_from_one_address_are_limited(async_session: AsyncSession) -> None:
    """Each try costs an argon2 hash, unknown email or not. Unlimited, a few dozen requests a
    second from one address stalled the loop every tenant shares."""
    app = _app(_settings(), async_session)
    body = {"password": "wrong password!"}
    async with client_for(app) as client:
        for _ in range(PASSWORD_TRIES_PER_MINUTE):
            wrong = await client.post("/auth/password/login", json={**body, "email": _email()})
            assert wrong.status_code == 401
            assert wrong.json()["detail"] == "Wrong email or password."
        limited = await client.post("/auth/password/login", json={**body, "email": _email()})
        signup = await client.post(
            "/auth/signup", json={"email": _email(), "name": "Operator", "password": PASSWORD}
        )
    assert limited.status_code == 429
    assert 0 < int(limited.headers["Retry-After"]) <= 60
    assert signup.status_code == 429, "every route that hashes shares the limit"


async def test_an_ipv6_client_is_limited_by_its_slash_64_not_its_address() -> None:
    """A subscriber is handed a whole /64: keyed on the address, each new one from it reset the
    password limit and the lockout."""

    def request(host: str) -> Request:
        return Request({"type": "http", "headers": [], "client": (host, 50000)})

    limit = RateLimit(1, 60.0)
    limit.check(request("2001:db8:1:2::1"))
    with pytest.raises(HTTPException) as refused:
        limit.check(request("2001:db8:1:2:ffff:abcd:1234:5678"))
    assert refused.value.status_code == 429
    limit.check(request("2001:db8:1:3::1"))
    assert client_key(request("203.0.113.7")) != client_key(request("203.0.113.8"))
    assert client_key(request("::ffff:203.0.113.7")) == "203.0.113.7"


async def test_a_proxy_forwarded_for_names_that_the_api_ignores_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Behind a proxy `FORWARDED_ALLOW_IPS` does not name, every client is the proxy's address,
    so one client's tries lock out everyone, and nothing said why."""
    seen: list[tuple[str, int]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["client"])

    async def call(client: str, forwarded: bytes) -> None:
        scope = {
            "type": "http",
            "client": (client, 50000),
            "headers": [(b"x-forwarded-for", forwarded)],
        }
        await middleware(scope, _no_receive, _no_send)

    middleware = UntrustedForwardedFor(app)
    with caplog.at_level("WARNING", logger="orchestrator.api.ratelimit"):
        # What uvicorn leaves when it trusts the proxy: the client is one the header names.
        await call("203.0.113.7", b"203.0.113.7, 172.18.0.2")
        assert "FORWARDED_ALLOW_IPS" not in caplog.text
        await call("172.18.0.2", b"203.0.113.7")
        await call("172.18.0.2", b"203.0.113.8")
    assert caplog.text.count("FORWARDED_ALLOW_IPS does not name") == 1
    assert "172.18.0.2" in caplog.text
    assert len(seen) == 3


async def _no_receive() -> Message:
    raise AssertionError("unread")


async def _no_send(message: Message) -> None:
    raise AssertionError("unsent")


async def test_hashing_waits_off_the_loop_and_refuses_past_its_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argon2 ran on the event loop: every hash stopped every tenant's requests while it ran."""
    release = threading.Event()

    class SlowHasher:
        def hash(self, password: str) -> str:
            release.wait(5)
            return "hashed"

    monkeypatch.setattr(accounts_module, "_hasher", SlowHasher())
    held = [
        asyncio.create_task(accounts_module._hash(PASSWORD))
        for _ in range(accounts_module.HASH_WORKERS + accounts_module.HASH_QUEUE)
    ]
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(HasherBusy):
            await accounts_module._hash(PASSWORD)
    finally:
        release.set()
    assert await asyncio.gather(*held) == ["hashed"] * len(held)


async def test_with_the_flag_on_sso_admits_any_domain_and_creates_the_account(
    async_session: AsyncSession,
) -> None:
    auth = AuthSettings(
        google_client_id="cid",
        google_client_secret=SecretStr("secret"),
        allowed_domains=["leaninnovationlabs.com"],
        external_url=DASH,
    )
    app = _app(_settings(auth=auth), async_session)
    email = _email()

    async def identity():
        async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
            return email, True

        return exchange

    app.dependency_overrides[google_identity_dependency] = identity
    async with client_for(app, base_url=DASH) as client:
        client.cookies.set("sf_oauth_state", "s")
        response = await client.get("/auth/callback", params={"code": "c", "state": "s"})
    assert response.status_code == 302
    assert _session_email(response) == email.lower()
    user = await user_by_email(async_session, email)
    assert user is not None
    assert user.email_verified_at is not None


async def test_sso_ends_the_sessions_of_whoever_registered_the_address_first(
    async_session: AsyncSession,
) -> None:
    """The cookie names only the email, so without this the stranger would act as the owner
    once SSO verified the account, and set its password again."""
    auth = AuthSettings(
        google_client_id="cid", google_client_secret=SecretStr("secret"), external_url=DASH
    )
    app = _app(_settings(auth=auth), async_session)
    email = _email()

    async def identity():
        async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
            return email, True

        return exchange

    app.dependency_overrides[google_identity_dependency] = identity
    async with client_for(app, base_url=DASH) as squatter, client_for(app, base_url=DASH) as owner:
        await squatter.post(
            "/auth/signup", json={"email": email, "name": "Squatter", "password": PASSWORD}
        )
        assert (await squatter.get("/auth/me")).status_code == 200
        owner.cookies.set("sf_oauth_state", "s")
        await owner.get("/auth/callback", params={"code": "c", "state": "s"})
        me = await squatter.get("/auth/me")
        password = await squatter.put("/auth/password", json={"password": "the squatter's own"})
        guarded = await squatter.get("/api/orgs")
        owners = await owner.get("/auth/me")
    assert me.status_code == 401
    assert password.status_code == 401
    assert guarded.status_code == 401
    assert owners.status_code == 200


async def test_signing_out_everywhere_ends_a_copied_cookie(async_session: AsyncSession) -> None:
    """A signed cookie could not be revoked: a copy kept working after its owner signed out."""
    app = _app(_settings(), async_session)
    email = _email()
    await create_account(async_session, email=email, name="Operator", password=PASSWORD, now=NOW)
    async with client_for(app) as owner, client_for(app) as thief:
        signed_in = await owner.post(
            "/auth/password/login", json={"email": email, "password": PASSWORD}
        )
        thief.cookies.set(SESSION_COOKIE, signed_in.cookies[SESSION_COOKIE])
        assert (await thief.get("/api/orgs")).status_code == 200
        ended = await owner.post(
            "/auth/logout/everywhere", headers={"Sec-Fetch-Site": "same-origin"}
        )
        me = await thief.get("/auth/me")
        guarded = await thief.get("/api/orgs")
    assert ended.status_code == 204
    assert (me.status_code, guarded.status_code) == (401, 401)


async def test_a_copied_cookie_alone_cannot_give_an_sso_account_a_password(
    async_session: AsyncSession,
) -> None:
    """The password would outlive the cookie and every sign-out. A fresh sign-in may set one,
    and that ends every other session."""
    settings = _settings()
    app = _app(settings, async_session)
    email = _email().lower()
    await sso_sign_in(async_session, email=email, now=NOW)
    now = datetime.now(UTC)

    def cookie(minted: datetime) -> str:
        return mint_session(
            email, verified=True, api_token=TOKEN, now=minted, max_age=settings.auth.session_max_age
        )

    body = {"password": "a password of my own"}
    dashboard = {"Sec-Fetch-Site": "same-origin"}
    async with client_for(app) as copied, client_for(app) as fresh:
        copied.cookies.set(SESSION_COOKIE, cookie(now - timedelta(hours=1)))
        fresh.cookies.set(SESSION_COOKIE, cookie(now))
        refused = await copied.put("/auth/password", json=body, headers=dashboard)
        accepted = await fresh.put("/auth/password", json=body, headers=dashboard)
        copied_after = await copied.get("/auth/me")
        fresh_after = await fresh.get("/auth/me")
    assert refused.status_code == 403
    assert accepted.status_code == 204
    assert copied_after.status_code == 401
    assert fresh_after.status_code == 200


async def test_the_session_cookie_is_secure_behind_https_without_an_external_url(
    async_session: AsyncSession,
) -> None:
    """A password-only deployment sets no external URL, and its proxy speaks plain HTTP to the
    app: the forwarded scheme is what says the browser is on https."""
    app = _app(_settings(), async_session)
    email = _email()
    await create_account(async_session, email=email, name="Operator", password=PASSWORD, now=NOW)
    body = {"email": email, "password": PASSWORD}
    async with client_for(app) as client:
        proxied = await client.post(
            "/auth/password/login", json=body, headers={"X-Forwarded-Proto": "https"}
        )
        plain = await client.post("/auth/password/login", json=body)
    assert "secure" in proxied.headers["set-cookie"].lower()
    assert "secure" not in plain.headers["set-cookie"].lower()
