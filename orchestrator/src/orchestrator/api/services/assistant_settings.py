"""The assistant's settings document and credential: where they live and how they are read.

Both are an organization's (design.md §2.11). The document is an `organization_setting` row; the
credential is an `organization_credential` row in the slot the document's backend and auth mode
name. Switching either reads a different slot, so a key set for one mode is never used for
another. The credential has no read path out of this module except `assistant_credential`, which
only the API's credential route calls, for the assistant's process. The process's own health is
the deployment's, an `app_setting` row, because whether a backend starts is a fact about the
host.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.vocabulary import credential_slot
from orchestrator.db.base import JsonDict
from orchestrator.db.models import AppSetting, OrganizationSetting
from orchestrator.factorial.bedrock_token import BedrockMintError, mint_bearer
from orchestrator.secrets import store

__all__ = [
    "HOST_BEDROCK_REFUSED",
    "SETTING_KEY",
    "STATUS_KEY",
    "CredentialState",
    "assistant_credential",
    "assistant_ready_to_send",
    "clear_assistant_credential",
    "credential_state",
    "host_bedrock_refused",
    "load_assistant_settings",
    "load_assistant_status",
    "save_assistant_settings",
    "save_assistant_status",
    "set_assistant_credential",
]

SETTING_KEY: Final = "assistant"
#: Written by the assistant process when it starts (its backends' preflight); the settings page
#: and `/readyz` only read it.
STATUS_KEY: Final = "assistant_status"

#: Under multi-org a Bedrock bearer for the assistant would be minted from the host's AWS role
#: for any organization that asked, and the mint reads instance metadata on the request path.
HOST_BEDROCK_REFUSED: Final = (
    "This Factorial install runs many organizations, so the assistant cannot use AWS Bedrock "
    "through the host's role here. Choose another sign-in for it."
)


#: Readiness asks whether the host can mint, per region. On a host with no AWS identity the
#: answer costs an instance-metadata timeout, and the dashboard asks on every chat render.
_HOST_MINT_TTL_SECONDS: Final = 60.0
_host_mint_ok: dict[str, tuple[float, bool]] = {}


async def _host_can_mint(region: str) -> bool:
    now = time.monotonic()
    cached = _host_mint_ok.get(region)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        await asyncio.to_thread(mint_bearer, region=region)
        ok = True
    except BedrockMintError:
        ok = False
    _host_mint_ok[region] = (time.monotonic() + _HOST_MINT_TTL_SECONDS, ok)
    return ok


def host_bedrock_refused(assistant: AssistantSettings, *, multi_org: bool) -> bool:
    return multi_org and assistant.backend == "codex_cli" and assistant.auth_mode == "bedrock"


@dataclass(frozen=True)
class CredentialState:
    set: bool
    updated_by: str | None
    updated_at: datetime | None


async def load_assistant_settings(session: AsyncSession, organization_id: int) -> AssistantSettings:
    value = (
        await session.execute(
            select(OrganizationSetting.value).where(
                OrganizationSetting.organization_id == organization_id,
                OrganizationSetting.key == SETTING_KEY,
            )
        )
    ).scalar_one_or_none()
    return AssistantSettings() if value is None else AssistantSettings.model_validate(value)


async def save_assistant_settings(
    session: AsyncSession, organization_id: int, value: AssistantSettings, *, by: str
) -> None:
    document = value.model_dump(mode="json")
    insert = pg_insert(OrganizationSetting).values(
        organization_id=organization_id, key=SETTING_KEY, value=document, updated_by=by
    )
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[OrganizationSetting.organization_id, OrganizationSetting.key],
            set_={"value": insert.excluded.value, "updated_by": insert.excluded.updated_by},
        )
    )
    await session.flush()


async def load_assistant_status(session: AsyncSession) -> JsonDict | None:
    """`{ok, message, version, at}`, or None when the assistant process never started."""
    return (
        await session.execute(select(AppSetting.value).where(AppSetting.key == STATUS_KEY))
    ).scalar_one_or_none()


async def save_assistant_status(session: AsyncSession, value: JsonDict) -> None:
    insert = pg_insert(AppSetting).values(key=STATUS_KEY, value=value, updated_by="assistant")
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": insert.excluded.value, "updated_by": insert.excluded.updated_by},
        )
    )


def _slot(settings: AssistantSettings) -> str:
    return credential_slot(settings.backend, settings.auth_mode)


async def set_assistant_credential(
    session: AsyncSession, organization_id: int, value: str, *, by: str
) -> None:
    settings = await load_assistant_settings(session, organization_id)
    await store.put_for_organization(session, organization_id, _slot(settings), value, by=by)


async def clear_assistant_credential(session: AsyncSession, organization_id: int) -> bool:
    settings = await load_assistant_settings(session, organization_id)
    return await store.delete_for_organization(session, organization_id, _slot(settings))


async def assistant_credential(
    session: AsyncSession, organization_id: int, settings: AssistantSettings
) -> SecretStr | None:
    return await store.get_for_organization(session, organization_id, _slot(settings))


async def credential_state(
    session: AsyncSession, organization_id: int, settings: AssistantSettings
) -> CredentialState:
    meta = await store.organization_credential_meta(session, organization_id, _slot(settings))
    if meta is None:
        return CredentialState(set=False, updated_by=None, updated_at=None)
    return CredentialState(set=True, updated_by=meta[0], updated_at=meta[1])


async def assistant_ready_to_send(
    session: AsyncSession, organization_id: int, settings: AssistantSettings, *, multi_org: bool
) -> bool:
    """Whether a message or Settings test may queue a turn: enabled, and a credential the turn
    can use — stored, or Bedrock minted from the factory host role at send time."""
    if not settings.enabled or host_bedrock_refused(settings, multi_org=multi_org):
        return False
    if settings.backend == "codex_cli" and settings.auth_mode == "bedrock":
        return await _host_can_mint(settings.bedrock_region)
    return (await credential_state(session, organization_id, settings)).set
