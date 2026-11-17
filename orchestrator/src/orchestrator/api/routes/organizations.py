"""Organizations (`/api/orgs`), the tenant root (design.md §2.11, tech-stack decision 68), with
their members, invites, roles and permissions (design.md §7.19).

Members, invites, roles and settings work whatever the multi-org flag says; creating an
organization answers 404 while it is off (tech-stack decision 80). The list holds only the
caller's organizations; the operator's shared token still sees every one.

A rename and a membership change are not events (`event.project_id` is NOT NULL), so the rows
record who made them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import Permission, decision
from orchestrator.access.decision import Caller
from orchestrator.api.access import SIGNED_IN, CallerDep, GrantDep, needs
from orchestrator.api.auth import PrincipalDep
from orchestrator.api.deps import OrganizationDep, SessionDep, SettingsDep, organization_path
from orchestrator.api.schemas import (
    InviteAccepted,
    InviteCreated,
    InviteIn,
    InviteOut,
    InvitePreviewOut,
    MemberOut,
    MemberPatch,
    OrganizationDetail,
    OrganizationIn,
    OrganizationOut,
    OrganizationPatch,
    OrganizationSettings,
    PermissionOut,
    RoleOut,
)
from orchestrator.api.services import membership
from orchestrator.db.models import Organization, PermissionRecord, Project, Runtime
from orchestrator.settings import APP_PREFIX, Settings

__all__ = ["router"]

router = APIRouter(tags=["organizations"])

_IN_ORG = [Depends(organization_path)]


def _require_multi_org(settings: Settings) -> None:
    if not settings.multi_org.enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Multiple organizations are not enabled on this Factorial install.",
        )


def _count(model: type[Project] | type[Runtime]) -> ColumnElement[int]:
    return (
        select(func.count(model.id))
        .where(model.organization_id == Organization.id)
        .correlate(Organization)
        .scalar_subquery()
    )


async def _out(
    session: AsyncSession,
    settings: Settings,
    *where: ColumnElement[bool],
    roles: dict[int, str | None] | None = None,
) -> list[OrganizationOut]:
    managed = settings.runtime.e2b.managed
    rows = await session.execute(
        select(Organization, _count(Project), _count(Runtime))
        .where(*where)
        .order_by(Organization.id)
        # A rename's `updated_at` is the trigger's, so the row must be read again, not reused.
        .execution_options(populate_existing=True)
    )
    return [
        OrganizationOut(
            slug=organization.slug,
            name=organization.name,
            project_count=projects,
            runtime_count=runtimes,
            updated_by=organization.updated_by,
            created_at=organization.created_at,
            updated_at=organization.updated_at,
            role=(roles or {}).get(organization.id),
            managed_sandbox_limit=(
                None if managed.api_key is None else managed.limit_for(organization.slug)
            ),
        )
        for organization, projects, runtimes in rows.all()
    ]


async def _one(
    session: AsyncSession, settings: Settings, organization: Organization, caller: Caller
) -> OrganizationOut:
    member = (
        None
        if caller.email is None
        else await decision.membership(session, organization_id=organization.id, email=caller.email)
    )
    role = member.role if member is not None else None
    (out,) = await _out(
        session, settings, Organization.id == organization.id, roles={organization.id: role}
    )
    return out


@router.get("/orgs", response_model=list[OrganizationOut])
@needs(SIGNED_IN)
async def list_organizations(
    session: SessionDep, settings: SettingsDep, caller: CallerDep
) -> list[OrganizationOut]:
    # The dashboard's organization gate and switcher read this list, not `/auth/me`.
    if caller.operator:
        return await _out(session, settings)
    reached = await membership.organizations_of(session, caller)
    if not reached:
        return []
    roles = {organization.id: role for organization, role in reached}
    return await _out(session, settings, Organization.id.in_(roles), roles=roles)


@router.post("/orgs", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
@needs(SIGNED_IN)
async def create_organization(
    body: OrganizationIn, session: SessionDep, settings: SettingsDep, caller: CallerDep
) -> OrganizationOut:
    _require_multi_org(settings)
    organization = await membership.create_organization(
        session,
        caller,
        name=body.name,
        slug=body.slug,
        setup_token=bool(settings.auth.bootstrap_token.get_secret_value()),
    )
    out = await _one(session, settings, organization, caller)
    await session.commit()
    return out


@router.get("/orgs/{org}", response_model=OrganizationDetail, dependencies=_IN_ORG)
@needs(Permission.ORG_READ)
async def get_organization(
    session: SessionDep, settings: SettingsDep, organization: OrganizationDep, grant: GrantDep
) -> OrganizationDetail:
    out = await _one(session, settings, organization, grant.caller)
    return OrganizationDetail(
        **out.model_dump(),
        settings=(
            OrganizationSettings.model_validate(organization.settings)
            if Permission.ORG_MANAGE in grant.permissions
            else None
        ),
        permissions=sorted(grant.permissions),
    )


@router.patch("/orgs/{org}", response_model=OrganizationOut, dependencies=_IN_ORG)
@needs(Permission.ORG_MANAGE)
async def rename_organization(
    body: OrganizationPatch,
    session: SessionDep,
    settings: SettingsDep,
    organization: OrganizationDep,
    principal: PrincipalDep,
    caller: CallerDep,
) -> OrganizationOut:
    organization.name = body.name
    organization.updated_by = principal
    await session.flush()
    out = await _one(session, settings, organization, caller)
    await session.commit()
    return out


@router.patch("/orgs/{org}/settings", response_model=OrganizationSettings, dependencies=_IN_ORG)
@needs(Permission.ORG_MANAGE)
async def update_organization_settings(
    body: OrganizationSettings,
    session: SessionDep,
    organization: OrganizationDep,
    principal: PrincipalDep,
) -> OrganizationSettings:
    organization.settings = {**organization.settings, **body.model_dump(exclude_unset=True)}
    organization.updated_by = principal
    await session.commit()
    return OrganizationSettings.model_validate(organization.settings)


@router.get("/roles", response_model=list[RoleOut])
@needs(SIGNED_IN)
async def list_roles(session: SessionDep) -> list[RoleOut]:
    return [
        RoleOut(key=key, name=role.name, description=role.description, permissions=sorted(held))
        for key, (role, held) in (await membership.built_in_roles(session)).items()
    ]


@router.get("/permissions", response_model=list[PermissionOut])
@needs(SIGNED_IN)
async def list_permissions(session: SessionDep) -> list[PermissionOut]:
    rows = {row.key: row for row in (await session.scalars(select(PermissionRecord))).all()}
    return [
        PermissionOut(key=key, description=rows[key].description)
        for key in (permission.value for permission in Permission)
        if key in rows
    ]


@router.get("/orgs/{org}/members", response_model=list[MemberOut], dependencies=_IN_ORG)
@needs(Permission.MEMBERS_INVITE)
async def list_members(session: SessionDep, organization: OrganizationDep) -> list[MemberOut]:
    return [
        MemberOut.model_validate(row)
        for row in await membership.list_members(session, organization.id)
    ]


@router.patch(
    "/orgs/{org}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_IN_ORG
)
@needs(Permission.MEMBERS_ASSIGN_ROLES)
async def change_member_role(
    user_id: int,
    body: MemberPatch,
    session: SessionDep,
    organization: OrganizationDep,
    grant: GrantDep,
) -> Response:
    await membership.change_role(
        session,
        grant.caller,
        grant.permissions,
        organization_id=organization.id,
        user_id=user_id,
        role=body.role,
        now=datetime.now(UTC),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/orgs/{org}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_IN_ORG
)
# Leaving needs only membership; removing someone else is the service's to check.
@needs(Permission.ORG_READ)
async def remove_member(
    user_id: int,
    session: SessionDep,
    organization: OrganizationDep,
    grant: GrantDep,
) -> Response:
    await membership.remove_member(
        session,
        grant.caller,
        grant.permissions,
        organization_id=organization.id,
        user_id=user_id,
        now=datetime.now(UTC),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/orgs/{org}/invites", response_model=list[InviteOut], dependencies=_IN_ORG)
@needs(Permission.MEMBERS_INVITE)
async def list_invites(session: SessionDep, organization: OrganizationDep) -> list[InviteOut]:
    return [
        InviteOut(
            id=invite.id,
            email=invite.email,
            role=role,
            created_by=invite.created_by,
            created_at=invite.created_at,
            expires_at=invite.expires_at,
        )
        for invite, role in await membership.list_invites(
            session, organization.id, now=datetime.now(UTC)
        )
    ]


@router.post(
    "/orgs/{org}/invites",
    response_model=InviteCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=_IN_ORG,
)
@needs(Permission.MEMBERS_INVITE)
async def create_invite(
    body: InviteIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    organization: OrganizationDep,
    grant: GrantDep,
) -> InviteCreated:
    created = await membership.create_invite(
        session,
        grant.caller,
        grant.permissions,
        organization_id=organization.id,
        email=body.email,
        role=body.role,
        now=datetime.now(UTC),
    )
    await session.commit()
    # The link is a credential until it is used.
    response.headers["Cache-Control"] = "no-store"
    path = f"{APP_PREFIX}/invite/{created.token}"
    origin = settings.auth.external_url.rstrip("/")
    invite = created.invite
    return InviteCreated(
        id=invite.id,
        email=invite.email,
        role=created.role,
        created_by=invite.created_by,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        path=path,
        url=f"{origin}{path}" if origin else None,
    )


@router.delete(
    "/orgs/{org}/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_IN_ORG,
)
@needs(Permission.MEMBERS_INVITE)
async def revoke_invite(
    invite_id: int,
    session: SessionDep,
    organization: OrganizationDep,
    caller: CallerDep,
) -> Response:
    await membership.revoke_invite(
        session,
        caller,
        organization_id=organization.id,
        invite_id=invite_id,
        now=datetime.now(UTC),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/invites/{token}", response_model=InvitePreviewOut)
@needs(SIGNED_IN)
async def preview_invite(token: str, session: SessionDep, caller: CallerDep) -> InvitePreviewOut:
    found = await membership.preview_invite(session, caller, token, now=datetime.now(UTC))
    return InvitePreviewOut(
        organization_slug=found.organization.slug,
        organization_name=found.organization.name,
        role=found.role,
        created_by=found.created_by,
        expires_at=found.expires_at,
        for_you=found.for_you,
    )


@router.post("/invites/{token}/accept", response_model=InviteAccepted)
@needs(SIGNED_IN)
async def accept_invite(token: str, session: SessionDep, caller: CallerDep) -> InviteAccepted:
    accepted = await membership.accept_invite(session, caller, token, now=datetime.now(UTC))
    await session.commit()
    return InviteAccepted(organization_slug=accepted.organization.slug, role=accepted.role)
