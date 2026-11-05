"""Email verification and password reset (design.md §7.19 items 16 and 17): the links, who may
redeem them, the send limits, and the gate an unverified session meets while mail is on."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.access.accounts import create_account, user_by_email
from orchestrator.access.verification import (
    DAILY_SENDS,
    RESEND_GAP,
    InvalidToken,
    TooSoon,
    issue,
    redeem_reset,
    redeem_verify,
)
from orchestrator.api.deps import account_sessions_dependency, mailer_dependency
from orchestrator.api.services import membership
from orchestrator.api.sessions import SESSION_COOKIE, verified_session
from orchestrator.db.models import EmailVerification, VerificationPurpose
from orchestrator.mail import MailError, Message
from orchestrator.mcp_server.auth import FactorialTokenVerifier, McpAccessToken
from orchestrator.settings import AuthSettings, MailSettings, MultiOrgSettings, Settings
from tests.api_harness import (
    MCP_BASE,
    TOKEN,
    build_app,
    client_for,
    make_settings,
    mcp_deps_for,
    seed_mcp_token,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

DASH = "https://dash.example.ts.net"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new passphrase"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FROM_DASHBOARD = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(autouse=True)
def _an_install_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no(session: AsyncSession, **_: object) -> bool:
        return False

    monkeypatch.setattr(membership, "install_is_empty", no)


class Outbox:
    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[Message] = []
        self.fails = fails

    async def send(self, message: Message) -> None:
        if self.fails:
            raise MailError("the server said no")
        self.sent.append(message)

    def link(self) -> str:
        return next(line for line in self.sent[-1].text.splitlines() if "#" in line)

    def token(self) -> str:
        return self.link().split("#", 1)[1]


def _email() -> str:
    return f"verify-{uuid4().hex[:10]}@example.com"


def _settings(*, mail: bool = True) -> Settings:
    settings = make_settings(auth=AuthSettings(external_url=DASH))
    settings.multi_org = MultiOrgSettings(enabled=True)
    if mail:
        settings.mail = MailSettings(smtp_host="smtp.test", from_address="no-reply@example.com")
    return settings


def _app(settings: Settings, session: AsyncSession, outbox: Outbox | None) -> FastAPI:
    app = build_app(settings, session)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[account_sessions_dependency] = lambda: scope
    app.dependency_overrides[mailer_dependency] = lambda: outbox
    return app


async def _signup(client: AsyncClient, email: str) -> None:
    created = await client.post(
        "/auth/signup", json={"email": email, "name": "Pat", "password": PASSWORD}
    )
    assert created.status_code == 201, created.text


# --- the gate and the verification link ---------------------------------------------------


async def test_a_signup_is_gated_until_its_link_is_opened(async_session: AsyncSession) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    async with client_for(app, base_url=DASH) as client:
        await _signup(client, email)
        me = (await client.get("/auth/me")).json()
        gated = await client.get("/api/orgs")
        verified = await client.post(
            "/auth/email/verify", json={"token": outbox.token()}, headers=FROM_DASHBOARD
        )
        after = (await client.get("/auth/me")).json()
        open_ = await client.get("/api/orgs")

    assert [m.to for m in outbox.sent] == [email]
    assert outbox.link().startswith(f"{DASH}/factorial/verify-email#")
    # The branded part carries the same link and the mark this deployment serves itself.
    html = outbox.sent[0].html or ""
    assert f'href="{outbox.link()}"' in html
    assert f'src="{DASH}/factorial/email-mark.png"' in html
    assert me["must_verify"] is True
    assert gated.status_code == 403
    assert gated.json()["code"] == "email_unverified"
    assert verified.status_code == 204, verified.text
    assert after == {"email": email, "verified": True, "must_verify": False}
    assert open_.status_code == 200


async def test_a_link_verifies_only_the_account_that_is_signed_in(
    async_session: AsyncSession,
) -> None:
    """A stranger registers the owner's address; the owner opens the link the stranger caused.
    Without a session of that account, the owner would verify the stranger's password."""
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    victim, other = _email(), _email()
    async with (
        client_for(app, base_url=DASH) as squatter,
        client_for(app, base_url=DASH) as owner,
        client_for(app, base_url=DASH) as bystander,
    ):
        await _signup(squatter, victim)
        token = outbox.token()
        await _signup(bystander, other)
        no_session = await owner.post(
            "/auth/email/verify", json={"token": token}, headers=FROM_DASHBOARD
        )
        wrong_account = await bystander.post(
            "/auth/email/verify", json={"token": token}, headers=FROM_DASHBOARD
        )

    assert no_session.status_code == 401
    assert wrong_account.status_code == 400
    user = await user_by_email(async_session, victim)
    assert user is not None and user.email_verified_at is None


async def test_verifying_ends_the_account_s_other_unverified_sessions(
    async_session: AsyncSession,
) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    async with client_for(app, base_url=DASH) as first, client_for(app, base_url=DASH) as second:
        await _signup(first, email)
        await second.post("/auth/password/login", json={"email": email, "password": PASSWORD})
        await first.post(
            "/auth/email/verify", json={"token": outbox.token()}, headers=FROM_DASHBOARD
        )
        assert (await first.get("/auth/me")).status_code == 200
        assert (await second.get("/auth/me")).status_code == 401
        again = await second.post(
            "/auth/password/login", json={"email": email, "password": PASSWORD}
        )
    cookie = verified_session(again.cookies[SESSION_COOKIE], api_token=TOKEN, now=datetime.now(UTC))
    assert cookie is not None and cookie.verified


async def test_an_unverified_session_cannot_set_a_password(async_session: AsyncSession) -> None:
    app = _app(_settings(), async_session, Outbox())
    async with client_for(app, base_url=DASH) as client:
        await _signup(client, _email())
        changed = await client.put(
            "/auth/password",
            json={"current_password": PASSWORD, "password": NEW_PASSWORD},
            headers=FROM_DASHBOARD,
        )
    assert changed.status_code == 403
    assert changed.json()["code"] == "email_unverified"


async def _age(session: AsyncSession, email: str) -> None:
    """Move the account's sends back past the resend gap, as if a minute had passed."""
    await session.execute(
        update(EmailVerification)
        .where(EmailVerification.email == email)
        .values(created_at=EmailVerification.created_at - RESEND_GAP)
    )


async def test_asking_again_sends_a_new_link_and_the_old_one_stops_working(
    async_session: AsyncSession,
) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    async with client_for(app, base_url=DASH) as client:
        await _signup(client, email)
        first = outbox.token()
        too_soon = await client.post("/auth/email/verification", headers=FROM_DASHBOARD)
        await _age(async_session, email)
        resent = await client.post("/auth/email/verification", headers=FROM_DASHBOARD)
        stale = await client.post(
            "/auth/email/verify", json={"token": first}, headers=FROM_DASHBOARD
        )
        fresh = await client.post(
            "/auth/email/verify", json={"token": outbox.token()}, headers=FROM_DASHBOARD
        )
    assert too_soon.status_code == 429
    assert 0 < int(too_soon.headers["Retry-After"]) <= 60
    assert resent.status_code == 204
    assert len(outbox.sent) == 2
    assert stale.status_code == 400
    assert fresh.status_code == 204


async def test_a_password_sign_in_sends_a_link_when_none_is_live(
    async_session: AsyncSession,
) -> None:
    """An account made before mail was configured was never sent one, and its wall says a link
    is on its way."""
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    await create_account(async_session, email=email, name="Pat", password=PASSWORD, now=NOW)
    credentials = {"email": email, "password": PASSWORD}
    async with client_for(app, base_url=DASH) as client:
        first = await client.post("/auth/password/login", json=credentials)
        await _age(async_session, email)
        again = await client.post("/auth/password/login", json=credentials)
        verified = await client.post(
            "/auth/email/verify", json={"token": outbox.token()}, headers=FROM_DASHBOARD
        )
        await client.post("/auth/password/login", json=credentials)
    assert (first.status_code, again.status_code, verified.status_code) == (200, 200, 204)
    assert [m.to for m in outbox.sent] == [email]


async def test_a_failed_send_keeps_the_signup_and_says_so_on_a_resend(
    async_session: AsyncSession,
) -> None:
    outbox = Outbox(fails=True)
    app = _app(_settings(), async_session, outbox)
    email = _email()
    async with client_for(app, base_url=DASH) as client:
        await _signup(client, email)
        await _age(async_session, email)
        resent = await client.post("/auth/email/verification", headers=FROM_DASHBOARD)
    assert resent.status_code == 503
    assert await user_by_email(async_session, email) is not None


async def test_without_mail_nothing_is_gated_and_nothing_sends(
    async_session: AsyncSession,
) -> None:
    app = _app(_settings(mail=False), async_session, None)
    async with client_for(app, base_url=DASH) as client:
        await _signup(client, _email())
        me = (await client.get("/auth/me")).json()
        orgs = await client.get("/api/orgs")
        resend = await client.post("/auth/email/verification", headers=FROM_DASHBOARD)
        forgot = await client.post("/auth/password/forgot", json={"email": _email()})
        providers = (await client.get("/auth/providers")).json()
    assert me["must_verify"] is False
    assert orgs.status_code == 200
    assert (resend.status_code, forgot.status_code) == (404, 404)
    assert providers["mail"] is False


# --- password reset ------------------------------------------------------------------------


async def test_a_reset_takes_the_account_back_from_whoever_set_its_password(
    async_session: AsyncSession,
) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    async with client_for(app, base_url=DASH) as squatter, client_for(app, base_url=DASH) as owner:
        await _signup(squatter, email)
        asked = await owner.post("/auth/password/forgot", json={"email": email})
        assert outbox.link().startswith(f"{DASH}/factorial/reset-password#")
        reset = await owner.post(
            "/auth/password/reset", json={"token": outbox.token(), "password": NEW_PASSWORD}
        )
        squatter_after = await squatter.get("/auth/me")
        owner_after = (await owner.get("/auth/me")).json()
        old = await squatter.post(
            "/auth/password/login", json={"email": email, "password": PASSWORD}
        )
    assert asked.status_code == 202
    assert reset.status_code == 204, reset.text
    assert squatter_after.status_code == 401
    assert owner_after["must_verify"] is False
    assert old.status_code == 401


async def test_asking_for_a_reset_says_nothing_about_the_email(
    async_session: AsyncSession,
) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    await create_account(async_session, email=email, name="Pat", password=PASSWORD, now=NOW)
    async with client_for(app, base_url=DASH) as client:
        known = await client.post("/auth/password/forgot", json={"email": email})
        unknown = await client.post("/auth/password/forgot", json={"email": _email()})
        repeat = await client.post("/auth/password/forgot", json={"email": email})
    assert known.status_code == unknown.status_code == repeat.status_code == 202
    assert [m.to for m in outbox.sent] == [email]


async def test_a_reset_link_works_once(async_session: AsyncSession) -> None:
    outbox = Outbox()
    app = _app(_settings(), async_session, outbox)
    email = _email()
    await create_account(async_session, email=email, name="Pat", password=PASSWORD, now=NOW)
    async with client_for(app, base_url=DASH) as client:
        await client.post("/auth/password/forgot", json={"email": email})
        body = {"token": outbox.token(), "password": NEW_PASSWORD}
        first = await client.post("/auth/password/reset", json=body)
        second = await client.post("/auth/password/reset", json=body)
        short = await client.post(
            "/auth/password/reset", json={"token": outbox.token(), "password": "short"}
        )
    assert first.status_code == 204
    assert second.status_code == 400
    assert short.status_code == 422


# --- the service ---------------------------------------------------------------------------


async def test_sends_are_limited_per_account_and_purpose(async_session: AsyncSession) -> None:
    user = await create_account(
        async_session, email=_email(), name="Pat", password=PASSWORD, now=NOW
    )
    await issue(async_session, user, VerificationPurpose.VERIFY, now=NOW)
    with pytest.raises(TooSoon):
        await issue(async_session, user, VerificationPurpose.VERIFY, now=NOW + RESEND_GAP / 2)
    await issue(async_session, user, VerificationPurpose.RESET, now=NOW + RESEND_GAP / 2)
    for n in range(1, DAILY_SENDS):
        await issue(async_session, user, VerificationPurpose.VERIFY, now=NOW + n * RESEND_GAP)
    with pytest.raises(TooSoon) as refused:
        await issue(
            async_session, user, VerificationPurpose.VERIFY, now=NOW + DAILY_SENDS * RESEND_GAP
        )
    assert refused.value.retry_after == timedelta(days=1) - DAILY_SENDS * RESEND_GAP
    await issue(async_session, user, VerificationPurpose.VERIFY, now=NOW + timedelta(days=1))
    live = await async_session.scalar(
        select(EmailVerification.id).where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == VerificationPurpose.VERIFY,
            EmailVerification.consumed_at.is_(None),
        )
    )
    assert live is not None


async def test_a_token_expires(async_session: AsyncSession) -> None:
    user = await create_account(
        async_session, email=_email(), name="Pat", password=PASSWORD, now=NOW
    )
    verify = await issue(async_session, user, VerificationPurpose.VERIFY, now=NOW)
    reset = await issue(async_session, user, VerificationPurpose.RESET, now=NOW)
    with pytest.raises(InvalidToken):
        await redeem_reset(
            async_session, reset, password=NEW_PASSWORD, now=NOW + timedelta(hours=1)
        )
    with pytest.raises(InvalidToken):
        await redeem_verify(async_session, verify, email=user.email, now=NOW + timedelta(days=1))
    with pytest.raises(InvalidToken):
        await redeem_verify(async_session, reset, email=user.email, now=NOW)
    verified = await redeem_verify(async_session, verify, email=user.email, now=NOW)
    assert verified.email_verified_at == NOW


# --- the other ways in ---------------------------------------------------------------------


async def test_an_mcp_token_of_an_unverified_account_is_refused_while_mail_is_on(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    settings = make_settings(auth=AuthSettings(external_url=MCP_BASE))
    settings.mail = MailSettings(smtp_host="smtp.test", from_address="no-reply@example.com")
    gated = FactorialTokenVerifier(mcp_deps_for(settings, maker))
    ungated = FactorialTokenVerifier(
        mcp_deps_for(make_settings(auth=AuthSettings(external_url=MCP_BASE)), maker)
    )
    email = _email()
    async with maker() as session:
        _, token = await seed_mcp_token(session, created_by=email)
        await session.commit()

    assert await gated.verify_token(token) is None
    assert isinstance(await ungated.verify_token(token), McpAccessToken)
    async with maker() as session:
        user = await user_by_email(session, email)
        assert user is not None
        user.email_verified_at = datetime.now(UTC)
        await session.commit()
    # The owner's later verification does not adopt what a stranger may have minted.
    assert await gated.verify_token(token) is None
    async with maker() as session:
        _, fresh = await seed_mcp_token(session, created_by=email)
        await session.commit()
    assert isinstance(await gated.verify_token(fresh), McpAccessToken)
