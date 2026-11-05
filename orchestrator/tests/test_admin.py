"""`orchestrator.admin grant-owner`: an install with data and no owner gets one without the API
token (tech-stack decision 80)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import MEMBER, OWNER
from orchestrator.access.accounts import create_account, user_by_email, vouch_for_email
from orchestrator.access.decision import Caller
from orchestrator.admin import USAGE, invite_link, main
from orchestrator.api.services import membership
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import AppUser, OrganizationMember, OrganizationRemoval, Role
from tests.members import ensure_member

pytestmark = pytest.mark.postgres
DATABASE = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


async def _role(session: AsyncSession, email: str) -> str | None:
    return await session.scalar(
        select(Role.key)
        .join(OrganizationMember, OrganizationMember.role_id == Role.id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .where(
            AppUser.email == email,
            OrganizationMember.organization_id == await default_organization_id(session),
        )
    )


@DATABASE
async def test_an_account_becomes_owner_at_once(async_session: AsyncSession) -> None:
    email = f"member-{uuid4().hex[:8]}@example.com"
    await ensure_member(async_session, email, role=MEMBER)

    token = await membership.grant_owner(async_session, email.upper(), now=datetime.now(UTC))

    assert token is None
    assert await _role(async_session, email) == OWNER


@DATABASE
async def test_granting_an_owner_lifts_a_removal(async_session: AsyncSession) -> None:
    email = f"gone-{uuid4().hex[:8]}@example.com"
    await ensure_member(async_session, email, role=MEMBER)
    user = await user_by_email(async_session, email)
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

    await membership.grant_owner(async_session, email, now=datetime.now(UTC))

    assert await _role(async_session, email) == OWNER
    assert await async_session.get(OrganizationRemoval, (default, email)) is None


@DATABASE
async def test_an_email_with_no_account_gets_an_owners_invite(
    async_session: AsyncSession,
) -> None:
    # Where no SSO is configured, a password-less account made here could never sign in: the
    # person signs up from the invite instead, which signup admits in every shape.
    email = f"op-{uuid4().hex[:8]}@example.com"

    token = await membership.grant_owner(async_session, email, now=datetime.now(UTC))

    assert token is not None
    assert await user_by_email(async_session, email) is None
    await create_account(
        async_session, email=email, name="Op", password="correct horse battery", now=NOW
    )
    accepted = await membership.accept_invite(
        async_session, Caller(email), token, now=datetime.now(UTC)
    )
    assert accepted.role == OWNER
    assert await _role(async_session, email) == OWNER


def test_the_link_is_on_the_dashboard_when_the_deployment_names_it() -> None:
    assert invite_link("t", "https://app.example.com/factorial") == (
        "https://app.example.com/factorial/invite/t"
    )
    assert invite_link("t", "") == "/factorial/invite/t"


@DATABASE
async def test_verify_email_verifies_an_account_and_names_one_that_is_missing(
    async_session: AsyncSession,
) -> None:
    email = f"vouched-{uuid4().hex[:8]}@example.com"
    await create_account(async_session, email=email, name="Pat", password="x" * 12, now=NOW)

    assert await vouch_for_email(async_session, f" {email.upper()} ", now=NOW) is True
    user = await user_by_email(async_session, email)
    assert user is not None and user.email_verified_at == NOW
    assert await vouch_for_email(async_session, f"none-{email}", now=NOW) is False


@pytest.mark.parametrize(
    "argv", [[], ["grant-owner"], ["verify-email"], ["make-owner", "a@b.c"]]
)
def test_the_command_refuses_anything_but_one_email(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert USAGE in capsys.readouterr().err
