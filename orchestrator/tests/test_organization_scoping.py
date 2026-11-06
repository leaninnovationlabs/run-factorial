"""What an organization owns stays its own (revision 0027). The worker's spawn, a sync's binding
check and the bindings read model each resolve a runtime slug against the project's own
organization: two tenants may both call a runtime `claude-api`, and neither ever runs on the
other's credentials. The assistant's settings are read per organization the same way."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.bindings import process_bindings
from orchestrator.api.services.assistant_settings import (
    load_assistant_settings,
    save_assistant_settings,
)
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.config import AssistantSettings
from orchestrator.db.models import Organization, ProcessVersion, Project, Runtime, RuntimeType
from orchestrator.factorial.registry import _runtime_exists
from orchestrator.worker.runtime_adapters import load_runtime_by_slug
from tests.api_harness import seed_project

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _organization(session: AsyncSession) -> int:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Other", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization.id


async def _runtime(session: AsyncSession, organization_id: int, slug: str) -> Runtime:
    row = Runtime(
        organization_id=organization_id, slug=slug, name=slug, type=RuntimeType.CLAUDE_API
    )
    session.add(row)
    await session.flush()
    return row


async def _project_naming(session: AsyncSession, organization_id: int, slug: str) -> Project:
    project = await seed_project(session, organization_id=organization_id)
    session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key="standard",
            content={"runtime": slug},
            source_commit_sha=uuid4().hex + "0" * 8,
        )
    )
    await session.flush()
    return project


async def test_the_worker_loads_the_runtime_of_the_tickets_organization(
    async_session: AsyncSession,
) -> None:
    slug = f"rt-{uuid4().hex[:12]}"
    ours = await default_organization_id(async_session)
    theirs = await _organization(async_session)
    our_row = await _runtime(async_session, ours, slug)
    their_row = await _runtime(async_session, theirs, slug)

    loaded = await load_runtime_by_slug(async_session, ours, slug)
    assert loaded is not None and loaded.id == our_row.id
    loaded = await load_runtime_by_slug(async_session, theirs, slug)
    assert loaded is not None and loaded.id == their_row.id


async def test_a_sync_never_binds_another_organizations_runtime(
    async_session: AsyncSession,
) -> None:
    slug = f"rt-{uuid4().hex[:12]}"
    await _runtime(async_session, await default_organization_id(async_session), slug)
    theirs = await _organization(async_session)

    assert not await _runtime_exists(async_session, theirs, slug)


async def test_each_projects_binding_is_its_own_organizations_row(
    async_session: AsyncSession,
) -> None:
    slug = f"rt-{uuid4().hex[:12]}"
    ours = await default_organization_id(async_session)
    theirs = await _organization(async_session)
    our_row = await _runtime(async_session, ours, slug)
    their_row = await _runtime(async_session, theirs, slug)
    our_project = await _project_naming(async_session, ours, slug)
    their_project = await _project_naming(async_session, theirs, slug)

    bindings = {b.project_id: b.runtime_id for b in await process_bindings(async_session)}
    assert bindings[our_project.id] == our_row.id
    assert bindings[their_project.id] == their_row.id


async def test_each_organization_reads_its_own_assistant_settings(
    async_session: AsyncSession,
) -> None:
    """A turn runs on its conversation's organization's settings and budget: one tenant enabling
    the assistant, or raising its budget, changes nothing for another."""
    ours = await default_organization_id(async_session)
    theirs = await _organization(async_session)
    await save_assistant_settings(
        async_session,
        theirs,
        AssistantSettings(enabled=True, daily_budget_usd=Decimal(5)),
        by="test",
    )

    assert await load_assistant_settings(async_session, theirs) == AssistantSettings(
        enabled=True, daily_budget_usd=Decimal(5)
    )
    assert (await load_assistant_settings(async_session, ours)).daily_budget_usd != Decimal(5)
