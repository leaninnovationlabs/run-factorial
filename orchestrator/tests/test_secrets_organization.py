"""An organization's own credential store (`organization_credential`): write-only from the API,
and bound to its organization and name by the AAD, like the project and runtime stores."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import Organization, OrganizationCredential
from orchestrator.secrets import store
from orchestrator.secrets.crypto import DecryptionError, encrypt

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

SLOT = "assistant:claude_sdk:api_key"


async def _other_organization(session: AsyncSession) -> int:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Other", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization.id


@pytest.mark.usefixtures("master_key")
async def test_a_credential_round_trips_and_is_write_only(async_session: AsyncSession) -> None:
    ours = await default_organization_id(async_session)
    await store.put_for_organization(async_session, ours, SLOT, "sk-1", by="operator@example.com")
    value = await store.get_for_organization(async_session, ours, SLOT)
    assert value is not None and value.get_secret_value() == "sk-1"
    meta = await store.organization_credential_meta(async_session, ours, SLOT)
    assert meta is not None and meta[0] == "operator@example.com"
    ciphertext = (
        await async_session.execute(
            select(OrganizationCredential.value_ciphertext).where(
                OrganizationCredential.organization_id == ours
            )
        )
    ).scalar_one()
    assert b"sk-1" not in bytes(ciphertext)


@pytest.mark.usefixtures("master_key")
async def test_a_second_put_replaces_the_value_and_the_author(
    async_session: AsyncSession,
) -> None:
    ours = await default_organization_id(async_session)
    await store.put_for_organization(async_session, ours, SLOT, "sk-1", by="operator@example.com")
    await store.put_for_organization(async_session, ours, SLOT, "sk-2", by="bo@example.com")
    value = await store.get_for_organization(async_session, ours, SLOT)
    assert value is not None and value.get_secret_value() == "sk-2"
    meta = await store.organization_credential_meta(async_session, ours, SLOT)
    assert meta is not None and meta[0] == "bo@example.com"


@pytest.mark.usefixtures("master_key")
async def test_delete_reports_whether_a_row_went(async_session: AsyncSession) -> None:
    ours = await default_organization_id(async_session)
    assert await store.delete_for_organization(async_session, ours, SLOT) is False
    await store.put_for_organization(async_session, ours, SLOT, "sk-1", by="operator@example.com")
    assert await store.delete_for_organization(async_session, ours, SLOT) is True
    assert await store.get_for_organization(async_session, ours, SLOT) is None
    assert await store.organization_credential_meta(async_session, ours, SLOT) is None


@pytest.mark.usefixtures("master_key")
async def test_each_organization_holds_its_own_value_in_the_same_slot(
    async_session: AsyncSession,
) -> None:
    ours = await default_organization_id(async_session)
    theirs = await _other_organization(async_session)
    await store.put_for_organization(
        async_session, ours, SLOT, "sk-ours", by="operator@example.com"
    )
    await store.put_for_organization(async_session, theirs, SLOT, "sk-theirs", by="bo@example.com")
    mine = await store.get_for_organization(async_session, ours, SLOT)
    other = await store.get_for_organization(async_session, theirs, SLOT)
    assert mine is not None and mine.get_secret_value() == "sk-ours"
    assert other is not None and other.get_secret_value() == "sk-theirs"


@pytest.mark.usefixtures("master_key")
async def test_a_blob_copied_from_another_organization_does_not_decrypt(
    async_session: AsyncSession,
) -> None:
    """The AAD names the organization: a row copied between tenants, same slot and all, must not
    hand one tenant the other's key."""
    ours = await default_organization_id(async_session)
    theirs = await _other_organization(async_session)
    await store.put_for_organization(async_session, theirs, SLOT, "sk-theirs", by="bo@example.com")
    stolen = (
        await async_session.execute(
            select(OrganizationCredential).where(OrganizationCredential.organization_id == theirs)
        )
    ).scalar_one()
    async_session.add(
        OrganizationCredential(
            organization_id=ours,
            name=SLOT,
            value_ciphertext=stolen.value_ciphertext,
            enc_key_id=stolen.enc_key_id,
            updated_by="operator@example.com",
        )
    )
    await async_session.flush()
    with pytest.raises(DecryptionError):
        await store.get_for_organization(async_session, ours, SLOT)


@pytest.mark.usefixtures("master_key")
async def test_a_blob_moved_from_the_runtime_store_does_not_decrypt(
    async_session: AsyncSession,
) -> None:
    ours = await default_organization_id(async_session)
    moved = encrypt("sk-stolen", aad=f"runtime:{ours}:{SLOT}".encode())
    async_session.add(
        OrganizationCredential(
            organization_id=ours,
            name=SLOT,
            value_ciphertext=moved.ciphertext,
            enc_key_id=moved.enc_key_id,
            updated_by="operator@example.com",
        )
    )
    await async_session.flush()
    with pytest.raises(DecryptionError):
        await store.get_for_organization(async_session, ours, SLOT)
