"""Why a send or a Settings test is refused, one sentence per cause.

One sentence for every cause told a person who had stored a key, and only left Enabled off, to set
the assistant up; they looked for a fault in the key. The causes that touch the credential store
are proven against the routes (`test_assistant_routes.py`); these need no database.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services import assistant_settings as svc
from orchestrator.assistant.config import AssistantSettings
from orchestrator.factorial.bedrock_token import BedrockMintError

#: The host-role branches never read the database.
NO_SESSION = cast(AsyncSession, None)

BEDROCK = AssistantSettings(
    enabled=True,
    backend="bedrock_responses",
    auth_mode="bedrock",
    model="us.xai.grok-4.6",
    bedrock_region="eu-west-1",
)


@pytest.fixture(autouse=True)
def _fresh_mint_cache() -> None:
    svc._host_mint_ok.clear()


async def test_off_is_named_as_off_before_anything_about_the_credential() -> None:
    off = BEDROCK.model_copy(update={"enabled": False})
    assert await svc.assistant_not_ready(NO_SESSION, 1, off, multi_org=True) == svc.ASSISTANT_OFF


async def test_the_host_role_under_many_organizations_says_why() -> None:
    reason = await svc.assistant_not_ready(NO_SESSION, 1, BEDROCK, multi_org=True)
    assert reason == svc.HOST_BEDROCK_REFUSED


async def test_a_host_that_cannot_mint_names_the_host_and_the_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_: object) -> str:
        raise BedrockMintError("the factory host has no AWS role")

    monkeypatch.setattr("orchestrator.api.services.assistant_settings.mint_bearer", refuse)
    reason = await svc.assistant_not_ready(NO_SESSION, 1, BEDROCK, multi_org=False)
    assert reason is not None
    assert "eu-west-1" in reason
    assert "AWS role" in reason
    assert not await svc.assistant_ready_to_send(NO_SESSION, 1, BEDROCK, multi_org=False)


async def test_a_host_that_can_mint_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orchestrator.api.services.assistant_settings.mint_bearer", lambda **_: "bedrock-key"
    )
    assert await svc.assistant_not_ready(NO_SESSION, 1, BEDROCK, multi_org=False) is None
    assert await svc.assistant_ready_to_send(NO_SESSION, 1, BEDROCK, multi_org=False)
