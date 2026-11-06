"""The assistant's settings document and credential: where they live and how they are read.

Both are an organization's (design.md §2.11). The document is an `organization_setting` row; a
credential is an `organization_credential` row in the slot a backend and auth mode name. A turn
reads only the slot the document names, so a key set for one mode is never used for another;
the settings page writes and removes the slot it names. A value has no read path out of this
module except `assistant_credential`, which only the API's credential route calls, for the
assistant's process; `stored_credentials` lists who set each slot and when. The process's own
health is the deployment's, an `app_setting` row, because whether a backend starts is a fact
about the host.
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
from orchestrator.assistant.vocabulary import (
    BACKENDS,
    SLOT_PREFIX,
    assistant_offer,
    credential_slot,
    parse_credential_slot,
)
from orchestrator.db.base import JsonDict
from orchestrator.db.models import AppSetting, OrganizationSetting
from orchestrator.factorial.bedrock_token import BedrockMintError, mint_bearer
from orchestrator.secrets import store

__all__ = [
    "HOST_BEDROCK_REFUSED",
    "SETTING_KEY",
    "STATUS_KEY",
    "CredentialState",
    "StoredCredential",
    "assistant_credential",
    "assistant_ready_to_send",
    "clear_assistant_credential",
    "credential_state",
    "host_bedrock_refused",
    "load_assistant_settings",
    "load_assistant_status",
    "model_refused",
    "offer_refused",
    "save_assistant_settings",
    "save_assistant_status",
    "set_assistant_credential",
    "stored_credentials",
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


def model_refused(assistant: AssistantSettings) -> str | None:
    """Why a save's model belongs to the other vendor, or None. Checked on save only: a document
    stored before the rule must still load."""
    is_claude = assistant.model.startswith("claude-")
    if assistant.backend == "claude_sdk" and not is_claude:
        return "A Claude backend needs a Claude model id, such as claude-opus-5-5."
    if assistant.backend == "codex_cli" and is_claude:
        example = BACKENDS["codex_cli"].default_models[assistant.auth_mode]
        return f"Codex can't run a Claude model. Use a model such as {example}."
    return None


def host_bedrock_refused(assistant: AssistantSettings, *, multi_org: bool) -> bool:
    return multi_org and assistant.backend == "codex_cli" and assistant.auth_mode == "bedrock"


def offer_refused(
    saved: AssistantSettings, body: AssistantSettings, offers: frozenset[str]
) -> str | None:
    """Why a save may not switch to the body's sign-in, or None. Only a switch is gated: an
    organization re-saving the sign-in it already has keeps it, offered or not, so existing
    servers keep the methods they set up (`Settings.assistant_offers`)."""
    pair = (body.backend, body.auth_mode)
    if pair == (saved.backend, saved.auth_mode) or assistant_offer(*pair) in offers:
        return None
    return "This Factorial install doesn't offer that sign-in for the assistant. Choose another."


def _first_offered(offers: frozenset[str]) -> AssistantSettings:
    # A subscription bills one person's plan, so an offered key comes first.
    first = min(offers, key=lambda offer: (not offer.endswith(":api_key"), offer))
    backend, _, auth_mode = first.partition(":")
    model = BACKENDS[backend].default_models[auth_mode]
    return AssistantSettings(backend=backend, auth_mode=auth_mode, model=model)


@dataclass(frozen=True)
class CredentialState:
    set: bool
    updated_by: str | None
    updated_at: datetime | None


async def load_assistant_settings(
    session: AsyncSession, organization_id: int, *, offers: frozenset[str] | None = None
) -> AssistantSettings:
    """The organization's saved document. With `offers`, an organization that saved nothing
    starts on an offered sign-in, so the page never marks one it did not choose as gone. Every
    settings route passes it so they agree on that sign-in; a turn's path need not, because an
    organization that saved nothing is not enabled."""
    value = (
        await session.execute(
            select(OrganizationSetting.value).where(
                OrganizationSetting.organization_id == organization_id,
                OrganizationSetting.key == SETTING_KEY,
            )
        )
    ).scalar_one_or_none()
    if value is not None:
        return AssistantSettings.model_validate(value)
    default = AssistantSettings()
    if offers and assistant_offer(default.backend, default.auth_mode) not in offers:
        return _first_offered(offers)
    return default


async def save_assistant_settings(
    session: AsyncSession, organization_id: int, value: AssistantSettings, *, by: str
) -> None:
    # The settings forbid unknown keys, so an unset cap stays out of the row: an older release
    # reads it after a rollback.
    document = value.model_dump(mode="json", exclude_none=True)
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
    session: AsyncSession,
    organization_id: int,
    backend: str,
    auth_mode: str,
    value: str,
    *,
    by: str,
) -> None:
    name = credential_slot(backend, auth_mode)
    await store.put_for_organization(session, organization_id, name, value, by=by)


async def clear_assistant_credential(
    session: AsyncSession, organization_id: int, backend: str, auth_mode: str
) -> bool:
    name = credential_slot(backend, auth_mode)
    return await store.delete_for_organization(session, organization_id, name)


@dataclass(frozen=True)
class StoredCredential:
    backend: str
    auth_mode: str
    updated_by: str
    updated_at: datetime


async def stored_credentials(session: AsyncSession, organization_id: int) -> list[StoredCredential]:
    rows = await store.organization_credentials_meta(session, organization_id, SLOT_PREFIX)
    found = [
        StoredCredential(slot[0], slot[1], by, at)
        for name, by, at in rows
        if (slot := parse_credential_slot(name)) is not None
    ]
    return sorted(found, key=lambda credential: (credential.backend, credential.auth_mode))


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
