"""Who may act in an organization (design.md §7.19): membership is the boundary, a role's
permissions decide the rest, and the rules no role grants hold whatever the role.

Two organizations: Acme, with an owner, an admin and a member, and Beta, with its own owner. A
stranger has an account and no membership. Everyone signs in with a session cookie; the
operator uses the shared token. The routes commit, so the people are new each test and the
teardown removes their memberships.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastmcp.exceptions import ToolError
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.access import ADMIN, MEMBER, OWNER, Permission
from orchestrator.api.services import membership
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.assistant.items import append_item
from orchestrator.db.models import (
    AppUser,
    ChatConversation,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    McpAccess,
    Organization,
    OrganizationInvite,
    OrganizationMember,
    OrganizationRemoval,
    Role,
    StepKind,
    StepRun,
    StepRunStatus,
)
from orchestrator.mcp_server.auth import FactorialTokenVerifier, McpAccessToken
from orchestrator.settings import AuthSettings, MultiOrgSettings, Settings
from tests.api_harness import (
    DASHBOARD,
    MCP_BASE,
    TOKEN,
    build_app,
    build_mcp_app,
    client_for,
    make_settings,
    mcp_client,
    mcp_deps_for,
    seed_mcp_token,
    seed_project,
    seed_ticket,
)
from tests.members import viewer_role

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _settings(*, multi_org: bool = True) -> Settings:
    settings = make_settings()
    settings.multi_org = MultiOrgSettings(enabled=multi_org)
    return settings


@dataclass
class People:
    acme: Organization
    beta: Organization
    owner: AppUser
    admin: AppUser
    member: AppUser
    #: Holds `org:read` alone, in a role of Acme's own (`tests.members.viewer_role`).
    viewer: AppUser
    beta_owner: AppUser
    stranger: AppUser


async def _user(session: AsyncSession, label: str) -> AppUser:
    user = AppUser(email=f"{label}-{uuid4().hex[:10]}@example.com", name=label.title())
    session.add(user)
    await session.flush()
    return user


async def _role_id(session: AsyncSession, key: str) -> int:
    return (
        await session.scalar(select(Role.id).where(Role.key == key, Role.organization_id.is_(None)))
    ) or 0


async def _join(
    session: AsyncSession,
    organization: Organization,
    user: AppUser,
    role: str,
    *,
    role_id: int | None = None,
) -> None:
    session.add(
        OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            role_id=role_id or await _role_id(session, role),
            added_by="test",
        )
    )


@pytest.fixture
async def people(async_session: AsyncSession) -> AsyncIterator[People]:
    acme = Organization(slug=f"acme-{uuid4().hex[:8]}", name="Acme", updated_by="test")
    beta = Organization(slug=f"beta-{uuid4().hex[:8]}", name="Beta", updated_by="test")
    async_session.add_all([acme, beta])
    await async_session.flush()
    found = People(
        acme=acme,
        beta=beta,
        owner=await _user(async_session, "owner"),
        admin=await _user(async_session, "admin"),
        member=await _user(async_session, "member"),
        viewer=await _user(async_session, "viewer"),
        beta_owner=await _user(async_session, "beta"),
        stranger=await _user(async_session, "stranger"),
    )
    await _join(async_session, acme, found.owner, OWNER)
    await _join(async_session, acme, found.admin, ADMIN)
    await _join(async_session, acme, found.member, MEMBER)
    await _join(
        async_session, acme, found.viewer, "", role_id=await viewer_role(async_session, acme.id)
    )
    await _join(async_session, beta, found.beta_owner, OWNER)
    await async_session.commit()
    # Read now: a rollback in the teardown expires the rows, and an expired row's id is IO.
    ids = [acme.id, beta.id]
    try:
        yield found
    finally:
        await async_session.rollback()
        await async_session.execute(
            delete(OrganizationInvite).where(OrganizationInvite.organization_id.in_(ids))
        )
        await async_session.execute(
            delete(OrganizationMember).where(OrganizationMember.organization_id.in_(ids))
        )
        await async_session.execute(
            delete(OrganizationRemoval).where(OrganizationRemoval.organization_id.in_(ids))
        )
        await async_session.execute(delete(Role).where(Role.organization_id.in_(ids)))
        await async_session.commit()


def _as(app: FastAPI, user: AppUser) -> AsyncClient:
    client = client_for(app)
    client.cookies.set(
        SESSION_COOKIE,
        mint_session(
            user.email, verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=3600
        ),
    )
    # What a browser sends from the dashboard; a change by cookie needs it (`api.access`).
    client.headers["Sec-Fetch-Site"] = "same-origin"
    return client


def _operator(app: FastAPI) -> AsyncClient:
    client = client_for(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


# --- reaching an organization -------------------------------------------------


async def test_a_person_lists_only_their_organizations_and_the_operator_lists_all(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    async with _as(app, people.member) as client:
        listed = (await client.get("/api/orgs")).json()
    async with _as(app, people.stranger) as client:
        nothing = (await client.get("/api/orgs")).json()
    async with _operator(app) as client:
        everything = (await client.get("/api/orgs")).json()
    assert [(row["slug"], row["role"]) for row in listed] == [(people.acme.slug, MEMBER)]
    assert nothing == []
    assert {people.acme.slug, people.beta.slug} <= {row["slug"] for row in everything}


async def test_a_stranger_gets_404_where_a_member_reads(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    project = await seed_project(async_session, organization_id=people.acme.id)
    await async_session.commit()
    async with _as(app, people.beta_owner) as client:
        org = await client.get(f"/api/orgs/{people.acme.slug}")
        item = await client.get(f"/api/projects/{project.id}")
    async with _as(app, people.member) as client:
        mine = await client.get(f"/api/orgs/{people.acme.slug}")
        my_item = await client.get(f"/api/projects/{project.id}")
    assert org.status_code == 404
    assert item.status_code == 404
    assert mine.status_code == 200
    assert mine.json()["role"] == MEMBER
    assert "org:read" in mine.json()["permissions"]
    assert "projects:manage" not in mine.json()["permissions"]
    assert my_item.status_code == 200


async def test_a_member_without_the_permission_gets_403(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    project = await seed_project(async_session, organization_id=people.acme.id)
    await async_session.commit()
    async with _as(app, people.member) as client:
        rename = await client.patch(f"/api/orgs/{people.acme.slug}", json={"name": "Mine"})
        activate = await client.put(f"/api/projects/{project.id}/active", json={"active": False})
    async with _as(app, people.admin) as client:
        admin_rename = await client.patch(f"/api/orgs/{people.acme.slug}", json={"name": "Mine"})
    assert rename.status_code == 403
    assert activate.status_code == 403
    # The admin holds the organization's settings since revision 0037; only roles stay the owner's.
    assert admin_rename.status_code == 200


# --- settings are the admin's -------------------------------------------------


async def test_a_member_reads_project_config_but_no_other_settings(
    people: People, async_session: AsyncSession
) -> None:
    """Silent and costly: runtime credentials, the assistant's settings and the member list
    answered a member before revision 0037. A project's configuration is the member's to read
    since 0038, and still refused to a role without `projects:read`."""
    app = build_app(_settings(), async_session)
    project = await seed_project(async_session, organization_id=people.acme.id)
    await async_session.commit()
    org = f"/api/orgs/{people.acme.slug}"
    config_reads = [
        f"/api/projects/{project.id}/readiness",
        f"/api/projects/{project.id}/ticket-source",
        f"/api/projects/{project.id}/secrets",
        f"/api/projects/{project.id}/processes",
        f"/api/projects/{project.id}/process-versions",
        f"/api/projects/{project.id}/labels",
    ]
    settings_reads = [
        f"{org}/runtimes",
        f"{org}/settings/assistant",
        f"{org}/members",
        f"{org}/invites",
    ]
    async with _as(app, people.member) as client:
        read = {url: (await client.get(url)).status_code for url in config_reads}
        refused = {url: (await client.get(url)).status_code for url in settings_reads}
        board = await client.get(f"/api/projects/{project.id}/board")
        ready = await client.get(f"{org}/settings/assistant/ready")
    async with _as(app, people.viewer) as client:
        viewer = {url: (await client.get(url)).status_code for url in config_reads}
    async with _as(app, people.admin) as client:
        admitted = {url: (await client.get(url)).status_code for url in settings_reads}
    # Readiness and labels read GitHub, which this harness does not answer; 403 is what matters.
    assert all(code != 403 for code in read.values()), read
    assert set(refused.values()) == {403}, refused
    assert set(viewer.values()) == {403}, viewer
    assert board.status_code == 200
    assert ready.status_code == 200
    assert set(ready.json()) == {
        "enabled",
        "credential_set",
        "credential_updated_at",
        "ready_to_send",
        "refusal",
    }
    # Readiness and labels read GitHub, which this harness does not answer; 403 is what matters.
    assert all(code != 403 for code in admitted.values()), admitted


async def test_a_role_without_projects_read_gets_a_project_without_its_configuration(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    project = await seed_project(async_session, organization_id=people.acme.id)
    project.settings = {
        "mcp_allowlist": [{"name": "fs"}],
        "gitlab": {"base_url": "https://git.example.com", "ca_bundle": "/etc/ca.pem"},
    }
    await async_session.commit()
    async with _as(app, people.viewer) as client:
        detail = (await client.get(f"/api/projects/{project.id}")).json()
        listed = (await client.get(f"/api/orgs/{people.acme.slug}/projects")).json()
    async with _as(app, people.member) as client:
        member_detail = (await client.get(f"/api/projects/{project.id}")).json()
        organization = (await client.get(f"/api/orgs/{people.acme.slug}")).json()
    async with _as(app, people.admin) as client:
        admin_organization = (await client.get(f"/api/orgs/{people.acme.slug}")).json()
    forge_only = {"gitlab": {"base_url": "https://git.example.com"}}
    assert detail["settings"] == forge_only
    assert [row["settings"] for row in listed if row["id"] == project.id] == [forge_only]
    assert detail["readiness"] == [] and detail["secret_names"] == []
    assert member_detail["settings"] == project.settings
    # The organization's own settings stay the admin's (`org:manage`).
    assert organization["settings"] is None
    assert admin_organization["settings"] == {}


async def test_a_member_lists_and_revokes_only_their_own_mcp_tokens(
    people: People, async_session: AsyncSession
) -> None:
    settings = _settings()
    # A revoke is a dashboard gesture: its Origin must be one the dashboard is served on.
    settings.auth = AuthSettings(external_url="http://api.test")
    app = build_app(settings, async_session)
    mine, _ = await seed_mcp_token(
        async_session, created_by=people.member.email, organization_id=people.acme.id
    )
    theirs, _ = await seed_mcp_token(
        async_session, created_by=people.admin.email, organization_id=people.acme.id
    )
    await async_session.commit()
    tokens = f"/api/orgs/{people.acme.slug}/mcp/tokens"
    origin = {"Origin": "http://api.test"}
    async with _as(app, people.member) as client:
        listed = {row["id"] for row in (await client.get(tokens)).json()["tokens"]}
        refused = await client.post(f"/api/mcp/tokens/{theirs.id}/revoke", headers=origin)
        revoked = await client.post(f"/api/mcp/tokens/{mine.id}/revoke", headers=origin)
    async with _as(app, people.admin) as client:
        everyone = {row["id"] for row in (await client.get(tokens)).json()["tokens"]}
    assert listed == {mine.id}
    assert refused.status_code == 404
    assert revoked.status_code == 204
    assert {mine.id, theirs.id} <= everyone


async def test_a_role_without_tickets_terminal_cannot_open_a_terminal_or_a_session(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    project = await seed_project(async_session, organization_id=people.acme.id)
    ticket = await seed_ticket(async_session, project)
    run = StepRun(
        ticket_id=ticket.id,
        step_name="develop",
        kind=StepKind.AGENT,
        attempt=1,
        status=StepRunStatus.SUCCEEDED,
        runtime_run_id="run-terminal",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
    async_session.add(run)
    await async_session.commit()
    async with _as(app, people.viewer) as client:
        state = await client.get(f"/api/step-runs/{run.id}/terminal")
        mint = await client.post(f"/api/step-runs/{run.id}/terminal-ticket")
        inspect = await client.post(f"/api/step-runs/{run.id}/inspect")
    async with _as(app, people.member) as client:
        member_mint = await client.post(f"/api/step-runs/{run.id}/terminal-ticket")
    assert state.status_code == 200, "what the terminal offers stays readable, for Resume"
    assert mint.status_code == 403
    assert inspect.status_code == 403
    assert "tickets:terminal" in mint.json()["detail"]
    # The member holds `tickets:terminal` since revision 0038.
    assert member_mint.status_code != 403


async def test_a_token_without_projects_read_cannot_read_a_project_s_config(
    people: People, async_session: AsyncSession, maker: async_sessionmaker[AsyncSession]
) -> None:
    project = await seed_project(async_session, organization_id=people.acme.id)
    _, jwt = await seed_mcp_token(
        async_session, created_by=people.viewer.email, organization_id=people.acme.id
    )
    await async_session.commit()
    app = build_mcp_app(_settings(), maker)
    async with mcp_client(app, jwt) as client:
        with pytest.raises(ToolError, match="projects:read"):
            await client.call_tool("factorial_config_read", {"project": project.name})


async def test_with_the_flag_off_membership_still_decides_and_nobody_creates_an_organization(
    people: People, async_session: AsyncSession
) -> None:
    # The flag decides only signup and new organizations (design.md §7.19): off, a member still
    # reads their organization, a stranger still gets 404, and the operator still reads it all.
    app = build_app(_settings(multi_org=False), async_session)
    async with _as(app, people.owner) as client:
        member = await client.get(f"/api/orgs/{people.acme.slug}")
        created = await client.post("/api/orgs", json={"name": "X", "slug": "xyz-org"})
    async with _as(app, people.stranger) as client:
        refused = await client.get(f"/api/orgs/{people.acme.slug}")
        listing = await client.get("/api/orgs")
    async with _operator(app) as client:
        operator = await client.get(f"/api/orgs/{people.acme.slug}")
    assert member.status_code == 200
    assert created.status_code == 404
    assert refused.status_code == 404
    assert listing.json() == []
    assert operator.status_code == 200


# --- creating an organization -------------------------------------------------


async def test_a_change_made_by_cookie_must_come_from_the_dashboard(
    people: People, async_session: AsyncSession
) -> None:
    """`SameSite=Lax` still sends the cookie with a form POST from another host of the same
    site. The reserved slug refuses past the guard, so a 422 is a request that reached the route."""
    settings = _settings()
    settings.cors_origins = ["https://app.example.com"]
    app = build_app(settings, async_session)
    body = {"name": "New", "slug": "signup"}
    answers = []
    async with _as(app, people.stranger) as client:
        client.headers.pop("Sec-Fetch-Site")
        for headers in (
            {},
            {"Origin": "https://www.example.com", "Sec-Fetch-Site": "same-site"},
            {"Origin": "https://app.example.com", "Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "same-origin"},
        ):
            answers.append((await client.post("/api/orgs", json=body, headers=headers)).status_code)
    assert answers == [403, 403, 422, 422]


async def test_a_person_creates_an_organization_and_owns_it(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    slug = f"new-{uuid4().hex[:8]}"
    async with _as(app, people.stranger) as client:
        created = await client.post("/api/orgs", json={"name": "New", "slug": slug})
        taken = await client.post("/api/orgs", json={"name": "New", "slug": slug})
        reserved = await client.post("/api/orgs", json={"name": "New", "slug": "signup"})
        joining = await client.post("/api/orgs", json={"name": "New", "slug": "join"})
        listed = (await client.get("/api/orgs")).json()
    async with _operator(app) as client:
        operator = await client.post("/api/orgs", json={"name": "Op", "slug": f"op-{slug}"})
    try:
        assert created.status_code == 201
        assert created.json()["role"] == OWNER
        assert taken.status_code == 409
        assert reserved.status_code == 422
        assert joining.status_code == 422
        assert [row["slug"] for row in listed] == [slug]
        assert operator.status_code == 403
    finally:
        new = await async_session.scalar(select(Organization.id).where(Organization.slug == slug))
        await async_session.execute(
            delete(OrganizationMember).where(OrganizationMember.organization_id == new)
        )
        await async_session.commit()


# --- members ------------------------------------------------------------------


async def test_an_admin_removes_a_member_but_not_another_admin(
    people: People, async_session: AsyncSession
) -> None:
    second_admin = await _user(async_session, "admin2")
    await _join(async_session, people.acme, second_admin, ADMIN)
    await async_session.commit()
    app = build_app(_settings(), async_session)
    base = f"/api/orgs/{people.acme.slug}/members"
    async with _as(app, people.admin) as client:
        admin = await client.delete(f"{base}/{second_admin.id}")
        member = await client.delete(f"{base}/{people.member.id}")
    async with _as(app, people.member) as client:
        listing = await client.get(base)
    assert admin.status_code == 403
    assert member.status_code == 204
    assert listing.status_code == 404


async def _removal(
    session: AsyncSession, organization: Organization, user: AppUser
) -> OrganizationRemoval | None:
    return await session.scalar(
        select(OrganizationRemoval)
        .where(
            OrganizationRemoval.organization_id == organization.id,
            OrganizationRemoval.email == user.email,
        )
        .execution_options(populate_existing=True)
    )


async def test_a_removal_is_recorded_and_kills_the_person_s_pending_invite(
    people: People, async_session: AsyncSession
) -> None:
    # A member holds a pending invite only if they joined some other way after it was sent;
    # the invite must not outlive the removal.
    token = uuid4().hex
    async_session.add(
        OrganizationInvite(
            organization_id=people.acme.id,
            email=people.member.email,
            role_id=await _role_id(async_session, MEMBER),
            token_hash=hashlib.sha256(token.encode()).digest(),
            created_by=people.owner.email,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await async_session.commit()
    app = build_app(_settings(), async_session)
    async with _as(app, people.admin) as client:
        removed = await client.delete(f"/api/orgs/{people.acme.slug}/members/{people.member.id}")
    async with _as(app, people.member) as client:
        stale = await client.post(f"/api/invites/{token}/accept")
    assert removed.status_code == 204
    assert stale.status_code == 409
    assert "revoked" in stale.json()["detail"]
    record = await _removal(async_session, people.acme, people.member)
    assert record is not None
    assert (record.reason, record.removed_by) == ("removed", people.admin.email)
    assert await _removal(async_session, people.beta, people.member) is None


async def test_leaving_is_recorded_and_a_new_invite_lets_them_back(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    async with _as(app, people.member) as client:
        left = await client.delete(f"/api/orgs/{people.acme.slug}/members/{people.member.id}")
    assert left.status_code == 204
    record = await _removal(async_session, people.acme, people.member)
    assert record is not None
    assert (record.reason, record.removed_by) == ("left", people.member.email)

    async with _as(app, people.owner) as client:
        invited = await client.post(
            f"/api/orgs/{people.acme.slug}/invites",
            json={"email": people.member.email, "role": MEMBER},
        )
    token = invited.json()["path"].rsplit("/", 1)[1]
    async with _as(app, people.member) as client:
        accepted = await client.post(f"/api/invites/{token}/accept")
        reached = (await client.get("/api/orgs")).json()
    assert accepted.status_code == 200
    assert [row["slug"] for row in reached] == [people.acme.slug]
    assert await _removal(async_session, people.acme, people.member) is None


@pytest.mark.parametrize("change", ["removed", "demoted"])
async def test_a_removed_or_demoted_admin_s_invites_stop_working(
    people: People, async_session: AsyncSession, change: str
) -> None:
    """Silent and costly: accepting an invite never asks whether its sender still may invite,
    so an admin's pending link would hand their role back after they lost it."""
    app = build_app(_settings(), async_session)
    alt = f"alt-{uuid4().hex[:8]}@example.com"
    async with _as(app, people.admin) as client:
        invited = await client.post(
            f"/api/orgs/{people.acme.slug}/invites", json={"email": alt, "role": ADMIN}
        )
    assert invited.status_code == 201, invited.text
    member = f"/api/orgs/{people.acme.slug}/members/{people.admin.id}"
    async with _as(app, people.owner) as client:
        if change == "removed":
            changed = await client.delete(member)
        else:
            changed = await client.patch(member, json={"role": MEMBER})
    assert changed.status_code == 204, changed.text
    invite = await async_session.scalar(
        select(OrganizationInvite).where(
            OrganizationInvite.organization_id == people.acme.id, OrganizationInvite.email == alt
        )
    )
    assert invite is not None
    await async_session.refresh(invite)
    assert invite.revoked_at is not None


async def test_an_install_with_members_is_not_empty(
    people: People, async_session: AsyncSession
) -> None:
    """The real query, which the suite otherwise replaces: its database is never empty."""
    assert await membership.install_is_empty(async_session, setup_token=False) is False


async def test_an_invite_to_a_higher_role_promotes_a_member(
    people: People, async_session: AsyncSession
) -> None:
    """The owner's invite `grant-owner` printed, accepted after an SSO sign-in already joined
    the person as a member; an invite to no more than they hold still says they are in."""
    app = build_app(_settings(), async_session)

    async def accept(role: str) -> tuple[int, OrganizationInvite]:
        token = uuid4().hex
        invite = OrganizationInvite(
            organization_id=people.acme.id,
            email=people.member.email,
            role_id=await _role_id(async_session, role),
            token_hash=hashlib.sha256(token.encode()).digest(),
            created_by="orchestrator.admin",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        async_session.add(invite)
        await async_session.commit()
        async with _as(app, people.member) as client:
            return (await client.post(f"/api/invites/{token}/accept")).status_code, invite

    refused, same = await accept(MEMBER)
    same.revoked_at, same.revoked_by = datetime.now(UTC), "test"
    await async_session.commit()
    accepted, _ = await accept(ADMIN)
    async with _as(app, people.member) as client:
        role = (await client.get(f"/api/orgs/{people.acme.slug}")).json()["role"]
    assert refused == 409
    assert accepted == 200
    assert role == ADMIN


async def test_the_last_owner_stays(people: People, async_session: AsyncSession) -> None:
    app = build_app(_settings(), async_session)
    base = f"/api/orgs/{people.acme.slug}/members"
    async with _as(app, people.owner) as client:
        leave = await client.delete(f"{base}/{people.owner.id}")
        demote = await client.patch(f"{base}/{people.owner.id}", json={"role": ADMIN})
        promote = await client.patch(f"{base}/{people.admin.id}", json={"role": OWNER})
        then_leave = await client.delete(f"{base}/{people.owner.id}")
    assert leave.status_code == 409
    assert demote.status_code == 409
    assert promote.status_code == 204
    assert then_leave.status_code == 204


async def test_only_an_owner_changes_roles(people: People, async_session: AsyncSession) -> None:
    app = build_app(_settings(), async_session)
    async with _as(app, people.admin) as client:
        response = await client.patch(
            f"/api/orgs/{people.acme.slug}/members/{people.member.id}", json={"role": ADMIN}
        )
    assert response.status_code == 403


async def test_every_member_reads_what_each_role_may_do(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    async with _as(app, people.member) as client:
        roles = (await client.get("/api/roles")).json()
        permissions = (await client.get("/api/permissions")).json()
    assert [row["key"] for row in permissions] == [permission.value for permission in Permission]
    assert all(row["description"] for row in permissions)
    granted = {key for role in roles for key in role["permissions"]}
    assert granted <= {row["key"] for row in permissions}


# --- invites ------------------------------------------------------------------


async def test_an_invite_joins_only_the_account_it_names(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    async with _as(app, people.admin) as client:
        created = await client.post(
            f"/api/orgs/{people.acme.slug}/invites",
            json={"email": people.stranger.email.upper(), "role": MEMBER},
        )
        pending = (await client.get(f"/api/orgs/{people.acme.slug}/invites")).json()
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    token = created.json()["path"].rsplit("/", 1)[1]
    assert [row["email"] for row in pending] == [people.stranger.email]

    async with _as(app, people.beta_owner) as client:
        preview = (await client.get(f"/api/invites/{token}")).json()
        wrong = await client.post(f"/api/invites/{token}/accept")
    assert preview["for_you"] is False
    assert wrong.status_code == 403

    async with _as(app, people.stranger) as client:
        mine = (await client.get(f"/api/invites/{token}")).json()
        accepted = await client.post(f"/api/invites/{token}/accept")
        again = await client.post(f"/api/invites/{token}/accept")
        reached = (await client.get("/api/orgs")).json()
    assert mine["for_you"] is True
    assert mine["organization_name"] == "Acme"
    assert accepted.status_code == 200
    assert accepted.json() == {"organization_slug": people.acme.slug, "role": MEMBER}
    assert again.status_code == 409
    assert [row["slug"] for row in reached] == [people.acme.slug]


async def test_nobody_invites_to_a_role_above_their_own(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    url = f"/api/orgs/{people.acme.slug}/invites"
    async with _as(app, people.admin) as client:
        owner = await client.post(url, json={"email": people.stranger.email, "role": OWNER})
        admin = await client.post(url, json={"email": people.stranger.email, "role": ADMIN})
    async with _as(app, people.member) as client:
        member = await client.post(url, json={"email": people.stranger.email, "role": MEMBER})
    assert owner.status_code == 403
    assert admin.status_code == 201
    assert member.status_code == 403


async def test_a_new_invite_replaces_the_pending_one_and_a_revoked_one_is_dead(
    people: People, async_session: AsyncSession
) -> None:
    app = build_app(_settings(), async_session)
    url = f"/api/orgs/{people.acme.slug}/invites"
    async with _as(app, people.owner) as client:
        first = (await client.post(url, json={"email": people.stranger.email, "role": MEMBER}))
        second = (await client.post(url, json={"email": people.stranger.email, "role": ADMIN}))
        revoked = await client.delete(f"{url}/{second.json()['id']}")
    first_token = first.json()["path"].rsplit("/", 1)[1]
    second_token = second.json()["path"].rsplit("/", 1)[1]
    async with _as(app, people.stranger) as client:
        replaced = await client.post(f"/api/invites/{first_token}/accept")
        dead = await client.post(f"/api/invites/{second_token}/accept")
    assert revoked.status_code == 204
    assert replaced.status_code == 409
    assert dead.status_code == 409


# --- MCP ----------------------------------------------------------------------


async def test_a_removed_member_s_token_stops_on_the_next_request(
    people: People, async_session: AsyncSession, maker: async_sessionmaker[AsyncSession]
) -> None:
    _, jwt = await seed_mcp_token(
        async_session,
        access=McpAccess.WRITE,
        created_by=people.member.email,
        organization_id=people.acme.id,
    )
    await async_session.commit()
    verifier = FactorialTokenVerifier(mcp_deps_for(_settings(), maker))
    token = await verifier.verify_token(jwt)
    assert isinstance(token, McpAccessToken)
    assert "tickets:operate" in token.permissions
    assert "projects:manage" not in token.permissions

    await async_session.execute(
        delete(OrganizationMember).where(OrganizationMember.user_id == people.member.id)
    )
    await async_session.commit()
    assert await verifier.verify_token(jwt) is None


async def test_a_member_s_write_token_cannot_do_what_their_role_cannot(
    people: People, async_session: AsyncSession, maker: async_sessionmaker[AsyncSession]
) -> None:
    project = await seed_project(async_session, organization_id=people.acme.id)
    _, jwt = await seed_mcp_token(
        async_session,
        access=McpAccess.WRITE,
        created_by=people.member.email,
        organization_id=people.acme.id,
    )
    await async_session.commit()
    app = build_mcp_app(_settings(), maker)
    async with mcp_client(app, jwt) as client:
        with pytest.raises(ToolError, match="projects:manage"):
            await client.call_tool(
                "factorial_config_commit", {"project": project.name, "content": "kind: Process"}
            )


# --- the assistant ------------------------------------------------------------


async def test_a_person_approves_only_what_they_could_do_themselves(
    people: People, async_session: AsyncSession, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The assistant proposes; a click runs the write as the person who clicks. A member cannot
    commit config, so their click on a config commit is refused and the card stays pending."""
    conversation = ChatConversation(
        organization_id=people.acme.id, owner=people.member.email, backend_kind="claude_sdk"
    )
    async_session.add(conversation)
    await async_session.flush()
    turn = ChatTurn(
        conversation_id=conversation.id,
        seq=1,
        status=ChatTurnStatus.DONE,
        trigger=ChatTurnTrigger.USER,
    )
    async_session.add(turn)
    await async_session.flush()
    card = await append_item(
        async_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        kind=ChatItemKind.PROPOSAL,
        payload={},
    )
    proposal = ChatProposal(
        conversation_id=conversation.id,
        turn_id=turn.id,
        item_id=card.id,
        tool="factorial_config_commit",
        args={},
        preview={},
        status=ChatProposalStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    async_session.add(proposal)
    await async_session.commit()
    settings = _settings()
    # Proposals are decided from the dashboard's origin only.
    settings.auth = AuthSettings(external_url=MCP_BASE)
    app = build_app(settings, async_session)
    app.state.mcp_deps = mcp_deps_for(settings, maker)
    async with _as(app, people.member) as client:
        response = await client.post(
            f"/api/assistant/proposals/{proposal.id}/approve", headers=DASHBOARD
        )
    assert response.status_code == 403
    assert "projects:manage" in response.json()["detail"]
    await async_session.refresh(proposal)
    assert proposal.status == ChatProposalStatus.PENDING
