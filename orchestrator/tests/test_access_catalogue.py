"""The permission catalogue in code and the rows revisions 0035 and 0037 seeded agree (tech-stack.md
decision 80). Roles are data and permissions are code, so these tests are what keeps them one
catalogue."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import BUILT_IN_ROLES, OWNER, Permission
from orchestrator.db.models import PermissionRecord, Role, RolePermission

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _granted(session: AsyncSession, key: str) -> set[str]:
    rows = await session.scalars(
        select(RolePermission.permission_key)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.key == key, Role.organization_id.is_(None))
    )
    return set(rows)


async def test_the_permission_rows_are_the_enum(async_session: AsyncSession) -> None:
    rows = (await async_session.scalars(select(PermissionRecord))).all()
    assert {row.key for row in rows} == {permission.value for permission in Permission}
    for row in rows:
        assert row.key == f"{row.module}:{row.action}"


async def test_the_built_in_roles_are_the_code_s(async_session: AsyncSession) -> None:
    keys = await async_session.scalars(select(Role.key).where(Role.built_in))
    assert set(keys) == set(BUILT_IN_ROLES)


async def test_the_owner_holds_every_permission(async_session: AsyncSession) -> None:
    """A revision that adds a permission must grant it to the owner, or no one could ever be
    given it: only an owner assigns roles."""
    assert await _granted(async_session, OWNER) == {permission.value for permission in Permission}


async def test_each_built_in_role_holds_what_the_one_below_it_does(
    async_session: AsyncSession,
) -> None:
    """Rank is containment: nobody grants a role holding a permission they lack, so an admin
    can invite a member only because admin holds everything member does."""
    owner, admin, member = [await _granted(async_session, key) for key in BUILT_IN_ROLES]
    assert member < admin < owner
