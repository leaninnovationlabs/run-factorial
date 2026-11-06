"""The three database guarantees the reconciliation loop actually leans on."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db import (
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.db.models import Organization, Runtime, RuntimeCredential, RuntimeType

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _seed_ticket(session: AsyncSession) -> Ticket:
    """Minimum chain project → source + process_version → ticket. Names are unique per call
    because these tests commit; nothing here relies on a clean database."""
    tag = uuid4().hex[:12]
    project = Project(
        organization_id=await default_organization_id(session),
        name=f"proj-{tag}",
        github_repo="acme/widgets",
    )
    session.add(project)
    await session.flush()

    source = TicketSource(
        project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref="acme/widgets"
    )
    version = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key="p",
        content={"specVersion": "adlc/v1"},
        source_commit_sha="a" * 40,
    )
    session.add_all([source, version])
    await session.flush()

    ticket = Ticket(
        project_id=project.id,
        ticket_source_id=source.id,
        external_key=tag,
        process_version_id=version.id,
        owner_at_admission="operator",
        current_step="spec",
        status=TicketStatus.RUNNING,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def test_duplicate_spawn_is_rejected(async_session: AsyncSession) -> None:
    """`UNIQUE (ticket_id, step_name, attempt)` is what makes agent spawns idempotent."""
    ticket = await _seed_ticket(async_session)

    def spawn() -> StepRun:
        return StepRun(
            ticket_id=ticket.id,
            step_name="spec",
            kind=StepKind.AGENT,
            attempt=1,
            status=StepRunStatus.RUNNING,
        )

    async_session.add(spawn())
    await async_session.flush()

    async_session.add(spawn())
    with pytest.raises(IntegrityError):
        await async_session.flush()


async def test_updated_at_is_maintained_by_the_trigger(async_session: AsyncSession) -> None:
    """The app never writes `updated_at`; the BEFORE UPDATE trigger does.

    Two commits, because `now()` is the transaction timestamp — inside one transaction the
    trigger would write back the value the row already has.
    """
    ticket = await _seed_ticket(async_session)
    await async_session.commit()
    before = ticket.updated_at

    await async_session.execute(
        update(Ticket).where(Ticket.id == ticket.id).values(status=TicketStatus.BLOCKED)
    )
    await async_session.commit()

    after = await async_session.scalar(select(Ticket.updated_at).where(Ticket.id == ticket.id))
    assert after is not None
    assert after > before


async def test_bad_ticket_status_is_rejected(async_session: AsyncSession) -> None:
    """TEXT + CHECK, not a native enum — but still enforced by the database."""
    ticket = await _seed_ticket(async_session)
    ticket.status = "in_progress"  # not one of the four
    with pytest.raises(IntegrityError):
        await async_session.flush()


async def _seed_runtime(session: AsyncSession, organization_id: int | None = None) -> Runtime:
    runtime = Runtime(
        organization_id=organization_id or await default_organization_id(session),
        slug=f"rt-{uuid4().hex[:12]}",
        name="Team Codex",
        type=RuntimeType.CODEX_SUBSCRIPTION,
    )
    session.add(runtime)
    await session.flush()
    return runtime


async def test_runtime_rows_get_updated_at_from_their_own_triggers(
    async_session: AsyncSession,
) -> None:
    """Revision 0011 wires `set_updated_at` onto both new tables itself: 0001's loop ran once,
    at creation, and a table it never saw would keep `updated_at` frozen at insert."""
    runtime = await _seed_runtime(async_session)
    credential = RuntimeCredential(
        runtime_id=runtime.id, name="CODEX_AUTH_JSON", value_ciphertext=b"x", enc_key_id="k"
    )
    async_session.add(credential)
    await async_session.commit()
    runtime_before, credential_before = runtime.updated_at, credential.updated_at

    await async_session.execute(
        update(Runtime).where(Runtime.id == runtime.id).values(name="Renamed")
    )
    await async_session.execute(
        update(RuntimeCredential)
        .where(RuntimeCredential.id == credential.id)
        .values(enc_key_id="k2")
    )
    await async_session.commit()

    runtime_after = await async_session.scalar(
        select(Runtime.updated_at).where(Runtime.id == runtime.id)
    )
    credential_after = await async_session.scalar(
        select(RuntimeCredential.updated_at).where(RuntimeCredential.id == credential.id)
    )
    assert runtime_after is not None and runtime_after > runtime_before
    assert credential_after is not None and credential_after > credential_before


async def test_deleting_a_runtime_cascades_credentials_and_unlinks_runs(
    async_session: AsyncSession,
) -> None:
    """Credentials are meaningless without their runtime; a run's history is not."""
    ticket = await _seed_ticket(async_session)
    runtime = await _seed_runtime(async_session)
    async_session.add(
        RuntimeCredential(
            runtime_id=runtime.id, name="CODEX_AUTH_JSON", value_ciphertext=b"x", enc_key_id="k"
        )
    )
    run = StepRun(
        ticket_id=ticket.id,
        step_name="spec",
        kind=StepKind.AGENT,
        attempt=1,
        status=StepRunStatus.SUCCEEDED,
        runtime_id=runtime.id,
    )
    async_session.add(run)
    await async_session.commit()

    await async_session.delete(runtime)
    await async_session.commit()

    assert (
        await async_session.scalar(
            select(RuntimeCredential.id).where(RuntimeCredential.runtime_id == runtime.id)
        )
        is None
    )
    unlinked = await async_session.scalar(select(StepRun.runtime_id).where(StepRun.id == run.id))
    assert unlinked is None
    assert await async_session.scalar(select(StepRun.id).where(StepRun.id == run.id)) == run.id


async def test_a_duplicate_runtime_slug_is_rejected(async_session: AsyncSession) -> None:
    """Repos reference a runtime by slug alone, so two rows in one organization could never be
    told apart."""
    runtime = await _seed_runtime(async_session)
    async_session.add(
        Runtime(
            organization_id=runtime.organization_id,
            slug=runtime.slug,
            name="Other",
            type=RuntimeType.CODEX_API,
        )
    )
    with pytest.raises(IntegrityError):
        await async_session.flush()


async def _seed_organization(session: AsyncSession) -> Organization:
    tag = uuid4().hex[:12]
    organization = Organization(slug=f"org-{tag}", name=f"Org {tag}", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization


async def test_another_organization_may_reuse_a_runtime_slug(async_session: AsyncSession) -> None:
    """A slug names a row within its organization: two tenants may both call theirs `claude-api`."""
    runtime = await _seed_runtime(async_session)
    other = await _seed_organization(async_session)
    async_session.add(
        Runtime(organization_id=other.id, slug=runtime.slug, name="Theirs", type=runtime.type)
    )
    await async_session.flush()


async def test_a_project_name_is_unique_within_its_organization_only(
    async_session: AsyncSession,
) -> None:
    ours = await default_organization_id(async_session)
    other = await _seed_organization(async_session)
    name = f"web-{uuid4().hex[:12]}"
    async_session.add(Project(organization_id=ours, name=name, github_repo="acme/web"))
    async_session.add(Project(organization_id=other.id, name=name, github_repo="other/web"))
    await async_session.flush()
    async_session.add(Project(organization_id=ours, name=name, github_repo="acme/web2"))
    with pytest.raises(IntegrityError):
        await async_session.flush()
