"""A person who belongs: membership is the boundary in every deployment (design.md §7.19), so a
test that signs someone in, or seeds a row they own, makes them a member first."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import OWNER
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import AppUser, OrganizationMember, Role, RolePermission

#: What a row records for the shared API token, which no membership names.
OPERATOR_PRINCIPAL = "api-token"


async def ensure_member(
    session: AsyncSession,
    email: str,
    *,
    organization_id: int | None = None,
    role: str = OWNER,
    role_id: int | None = None,
) -> None:
    """`email` holds `role` in the organization (`default` unless named), its account created when
    it has none. A membership already there keeps its role."""
    if email == OPERATOR_PRINCIPAL:
        return
    address = email.lower()
    await session.execute(
        insert(AppUser)
        .values(email=address, name=address.split("@", 1)[0])
        .on_conflict_do_nothing()
    )
    user_id = await session.scalar(select(AppUser.id).where(AppUser.email == address))
    if role_id is None:
        role_id = await session.scalar(
            select(Role.id).where(Role.key == role, Role.organization_id.is_(None))
        )
    await session.execute(
        insert(OrganizationMember)
        .values(
            organization_id=organization_id or await default_organization_id(session),
            user_id=user_id,
            role_id=role_id,
            added_by="test",
        )
        .on_conflict_do_nothing()
    )


async def viewer_role(session: AsyncSession, organization_id: int) -> int:
    """A role of the organization's own that holds `org:read` alone: narrower than the built-in
    member, which reads configuration and opens terminals, so a gate on those still has someone
    to refuse."""
    role = Role(
        organization_id=organization_id,
        key=f"viewer-{uuid4().hex[:8]}",
        name="Viewer",
        description="Reads the organization",
        built_in=False,
    )
    session.add(role)
    await session.flush()
    session.add(RolePermission(role_id=role.id, permission_key="org:read"))
    await session.flush()
    return role.id
