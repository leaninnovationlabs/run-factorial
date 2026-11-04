"""`/api/orgs/default/settings/assistant`: the settings document and its write-only credential."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.config import AssistantSettings
from orchestrator.db.models import ChatConversation, ChatItem, ChatTurn
from orchestrator.factorial.bedrock_token import BedrockMintError
from orchestrator.settings import AuthSettings, MultiOrgSettings
from tests.api_harness import DASHBOARD, MCP_BASE, build_app, client_for, make_settings
from tests.assistant_fakes import wipe_chat

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

URL = "/api/orgs/default/settings/assistant"
CREDENTIAL = f"{URL}/credential"


@pytest.fixture(autouse=True)
async def _clean(async_session: AsyncSession, master_key: None) -> AsyncIterator[None]:
    """These routes commit, and the database outlives the test."""

    async def wipe() -> None:
        await async_session.execute(text("DELETE FROM app_setting"))
        await async_session.execute(text("DELETE FROM organization_setting"))
        await async_session.execute(text("DELETE FROM organization_credential"))
        await async_session.commit()

    svc._host_mint_ok.clear()
    await wipe()
    yield
    await wipe()
    svc._host_mint_ok.clear()


@pytest.fixture
async def client(async_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    settings = make_settings(auth=AuthSettings(external_url=MCP_BASE))
    async with client_for(build_app(settings, async_session)) as client:
        yield client


async def test_get_returns_defaults_and_never_a_credential(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(URL, headers=auth)
    body = response.json()
    assert body["settings"]["model"] == "claude-opus-5-5"
    assert body["settings"]["enabled"] is False
    assert body["credential"] == {"set": False, "updated_by": None, "updated_at": None}
    assert body["ready_to_send"] is False
    assert body["backends"][0] == {
        "kind": "claude_sdk",
        "label": "Claude",
        "auth_modes": ["api_key", "subscription"],
        "default_model": "claude-opus-5-5",
    }
    assert body["status"] is None
    assert response.headers["cache-control"] == "no-store"


async def test_get_needs_the_api_token(client: AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401


async def test_put_saves_and_get_reads_it_back(client: AsyncClient, auth: dict[str, str]) -> None:
    changed = AssistantSettings(enabled=True, model="claude-sonnet-4-5", max_steps=10)
    response = await client.put(URL, json=changed.model_dump(mode="json"), headers=auth | DASHBOARD)
    assert response.status_code == 200
    body = (await client.get(URL, headers=auth)).json()
    assert body["settings"]["enabled"] is True
    assert body["settings"]["model"] == "claude-sonnet-4-5"
    assert body["settings"]["max_steps"] == 10


async def test_put_refuses_an_alias_with_422(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.put(URL, json={"model": "opus"}, headers=auth | DASHBOARD)
    assert response.status_code == 422
    assert (await client.get(URL, headers=auth)).json()["settings"]["model"] == "claude-opus-5-5"


async def test_put_needs_the_dashboard_origin(client: AsyncClient, auth: dict[str, str]) -> None:
    assert (await client.put(URL, json={}, headers=auth)).status_code == 403
    other = auth | {"Origin": "http://evil.test"}
    assert (await client.put(URL, json={}, headers=other)).status_code == 403


async def test_credential_put_needs_the_dashboard_origin(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth)
    assert response.status_code == 403
    assert (await client.delete(CREDENTIAL, headers=auth)).status_code == 403
    assert (await client.get(URL, headers=auth)).json()["credential"]["set"] is False


async def test_credential_is_write_only(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth | DASHBOARD)
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    text_ = (await client.get(URL, headers=auth)).text
    assert "sk-9" not in text_
    assert '"set":true' in text_.replace(" ", "")


async def test_credential_value_is_bounded(client: AsyncClient, auth: dict[str, str]) -> None:
    for value in ("", "x" * 4097):
        response = await client.put(CREDENTIAL, json={"value": value}, headers=auth | DASHBOARD)
        assert response.status_code == 422


async def test_a_refused_credential_is_not_echoed_back(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """FastAPI's own 422 repeats the refused `input`; a credential is never readable back, so
    these routes answer with where and why only, which the Assistant settings page draws under a
    field."""
    too_long = "sk-ant-" + "q" * 4096
    for value in ("", too_long):
        response = await client.put(CREDENTIAL, json={"value": value}, headers=auth | DASHBOARD)
        assert response.status_code == 422
        assert "q" * 64 not in response.text
        [error] = response.json()["detail"]
        assert "input" not in error
        assert error["loc"] == ["body", "value"]
        assert error["msg"].startswith("String should have at ")


async def test_delete_clears_the_credential(client: AsyncClient, auth: dict[str, str]) -> None:
    await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth | DASHBOARD)
    assert (await client.delete(CREDENTIAL, headers=auth | DASHBOARD)).status_code == 204
    assert (await client.get(URL, headers=auth)).json()["credential"]["set"] is False


async def test_changes_record_who_made_them(client: AsyncClient, auth: dict[str, str]) -> None:
    await client.put(URL, json={"enabled": True}, headers=auth | DASHBOARD)
    await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth | DASHBOARD)
    body = (await client.get(URL, headers=auth)).json()
    assert body["credential"]["updated_by"] == "api-token"
    assert body["credential"]["updated_at"] is not None


async def test_settings_row_records_who_saved_it(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    await client.put(URL, json={"enabled": True}, headers=auth | DASHBOARD)
    by = (
        await async_session.execute(
            text("SELECT updated_by FROM organization_setting WHERE key = :key"),
            {"key": svc.SETTING_KEY},
        )
    ).scalar_one()
    assert by == "api-token"


async def test_switching_auth_mode_does_not_reuse_the_other_slot(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    await client.put(CREDENTIAL, json={"value": "sk-key"}, headers=auth | DASHBOARD)
    await client.put(URL, json={"auth_mode": "subscription"}, headers=auth | DASHBOARD)

    body = (await client.get(URL, headers=auth)).json()
    assert body["credential"]["set"] is False
    subscription = AssistantSettings(auth_mode="subscription")
    ours = await default_organization_id(async_session)
    assert await svc.assistant_credential(async_session, ours, subscription) is None

    await client.put(CREDENTIAL, json={"value": "tok-sub"}, headers=auth | DASHBOARD)
    stored = await svc.assistant_credential(async_session, ours, subscription)
    assert stored is not None and stored.get_secret_value() == "tok-sub"
    original = await svc.assistant_credential(async_session, ours, AssistantSettings())
    assert original is not None and original.get_secret_value() == "sk-key"


async def test_get_reports_the_status_the_assistant_wrote(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    await async_session.execute(
        text(
            "INSERT INTO app_setting (key, value, updated_by) "
            "VALUES (:k, CAST(:v AS jsonb), 'assistant')"
        ),
        {"k": svc.STATUS_KEY, "v": '{"ok": false, "reason": "auth"}'},
    )
    await async_session.commit()
    assert (await client.get(URL, headers=auth)).json()["status"] == {"ok": False, "reason": "auth"}


TEST = f"{URL}/test"


@pytest.fixture
async def _wipe_chat(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


@pytest.mark.usefixtures("_wipe_chat")
async def test_settings_test_creates_an_archived_conversation_and_queues_one_turn(
    client: AsyncClient, auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    await client.put(URL, json={"enabled": True}, headers=auth | DASHBOARD)
    await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth | DASHBOARD)

    response = await client.post(TEST, headers=auth | DASHBOARD)

    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    async with maker() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        assert conversation is not None
        assert (conversation.owner, conversation.title) == ("api-token", "Connection test")
        assert conversation.archived_at is not None
        [turn] = (await session.scalars(select(ChatTurn))).all()
        [item] = (await session.scalars(select(ChatItem))).all()
    assert (turn.conversation_id, turn.status, turn.trigger) == (conversation_id, "queued", "user")
    assert (item.kind, item.turn_id, item.payload["text"]) == (
        "user_text",
        turn.id,
        "Reply with the single word OK.",
    )
    listed = await client.get("/api/orgs/default/assistant/conversations", headers=auth)
    assert listed.json() == []


async def test_get_ready_to_send_without_a_stored_credential_for_codex_bedrock(
    client: AsyncClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.api.services.assistant_settings.mint_bearer",
        lambda **_: "bedrock-api-key-test",
    )
    await client.put(
        URL,
        json={
            "enabled": True,
            "backend": "codex_cli",
            "auth_mode": "bedrock",
            "model": "us.xai.grok-4.6",
            "bedrock_region": "us-east-1",
        },
        headers=auth | DASHBOARD,
    )
    body = (await client.get(URL, headers=auth)).json()
    assert body["credential"]["set"] is False
    assert body["ready_to_send"] is True


async def test_bedrock_readiness_mints_off_the_event_loop_and_caches_a_refusal(
    client: AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no AWS identity a mint waits out the instance-metadata timeout. On the event loop
    that stalls every tenant's requests, and the dashboard asks on every chat render."""
    loop_thread = threading.get_ident()
    calls: list[int] = []

    def no_identity(**_: object) -> str:
        calls.append(threading.get_ident())
        raise BedrockMintError("no role")

    monkeypatch.setattr("orchestrator.api.services.assistant_settings.mint_bearer", no_identity)
    await client.put(
        URL,
        json={
            "enabled": True,
            "backend": "codex_cli",
            "auth_mode": "bedrock",
            "model": "us.xai.grok-4.6",
            "bedrock_region": "us-east-1",
        },
        headers=auth | DASHBOARD,
    )
    for _ in range(3):
        assert (await client.get(URL, headers=auth)).json()["ready_to_send"] is False
    assert len(calls) == 1
    assert calls[0] != loop_thread


async def test_multi_org_refuses_the_host_s_bedrock_role_for_the_assistant(
    async_session: AsyncSession,
    maker: async_sessionmaker[AsyncSession],
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any organization's owner could otherwise mint from the host's AWS role, and each ready
    check would read instance metadata to do it. A setting saved before the refusal mints
    nothing either."""
    minted: list[object] = []
    monkeypatch.setattr(
        "orchestrator.api.services.assistant_settings.mint_bearer",
        lambda **kw: minted.append(kw) or "bedrock-api-key-test",
    )
    bedrock = AssistantSettings(
        enabled=True,
        backend="codex_cli",
        auth_mode="bedrock",
        model="us.xai.grok-4.6",
        bedrock_region="us-east-1",
    )
    settings = make_settings(auth=AuthSettings(external_url=MCP_BASE)).model_copy(
        update={"multi_org": MultiOrgSettings(enabled=True)}
    )
    async with client_for(build_app(settings, async_session)) as client:
        body = bedrock.model_dump(mode="json")
        refused = await client.put(URL, json=body, headers=auth | DASHBOARD)
        assert refused.status_code == 422
        assert "AWS Bedrock" in refused.json()["detail"]
        async with maker() as session:
            organization = await default_organization_id(session)
            await svc.save_assistant_settings(session, organization, bedrock, by="earlier")
            await session.commit()
        assert (await client.get(URL, headers=auth)).json()["ready_to_send"] is False
    assert minted == []


@pytest.mark.usefixtures("_wipe_chat")
async def test_settings_test_queues_without_a_stored_credential_for_codex_bedrock(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.api.services.assistant_settings.mint_bearer",
        lambda **_: "bedrock-api-key-test",
    )
    await client.put(
        URL,
        json={
            "enabled": True,
            "backend": "codex_cli",
            "auth_mode": "bedrock",
            "model": "us.xai.grok-4.6",
            "bedrock_region": "us-east-1",
        },
        headers=auth | DASHBOARD,
    )
    response = await client.post(TEST, headers=auth | DASHBOARD)
    assert response.status_code == 200


@pytest.mark.usefixtures("_wipe_chat")
async def test_settings_test_is_refused_as_a_send_is_and_leaves_nothing_behind(
    client: AsyncClient, auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    refused = await client.post(TEST, headers=auth | DASHBOARD)
    assert refused.status_code == 409
    assert refused.json() == {
        "code": "not_configured",
        "detail": "Set up the assistant in Assistant settings.",
    }

    await client.put(URL, json={"enabled": True}, headers=auth | DASHBOARD)
    await client.put(CREDENTIAL, json={"value": "sk-9"}, headers=auth | DASHBOARD)
    async with maker() as session:
        await svc.save_assistant_status(
            session, {"ok": False, "message": "claude_sdk: drift", "version": "x", "at": "x"}
        )
        await session.commit()
    down = await client.post(TEST, headers=auth | DASHBOARD)
    assert (down.status_code, down.json()["code"]) == (503, "assistant_unavailable")

    async with maker() as session:
        assert (await session.scalars(select(ChatConversation))).all() == []


async def test_settings_test_needs_the_dashboard_origin(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    assert (await client.post(TEST, headers=auth)).status_code == 403
