"""`/api/assistant/*`: a person's conversations, sending a message, and stopping a turn. The
stream has its own module, `test_assistant_stream.py`."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.routes import settings as settings_routes
from orchestrator.api.services import assistant_send
from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.items import append_item
from orchestrator.db.models import (
    AppSetting,
    ChatAttachment,
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatSpend,
    ChatTurn,
    ChatTurnStatus,
)
from orchestrator.events.channels import CHAT_QUEUE_CHANNEL
from tests.api_harness import build_app, client_for, make_settings
from tests.assistant_fakes import enable_assistant, seed_conversation, seed_turn, wipe_chat
from tests.wake_probe import listening

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

API = "/api/assistant"
#: The list and the create are an organization's; a conversation's own routes keep `API`.
ORG = "/api/orgs/default"
CONVERSATIONS = f"{ORG}/assistant/conversations"
#: Who a bearer-token request is (decision 12): its conversations are `api-token`'s.
ME = "api-token"


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """These routes commit, and the database outlives the test."""
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


@pytest.fixture
async def enabled_assistant(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> AssistantSettings:
    return await enable_assistant(maker)


async def new_conversation(client: AsyncClient, auth: dict[str, str], title: str = "Chat") -> int:
    response = await client.post(CONVERSATIONS, json={"title": title}, headers=auth)
    assert response.status_code == 201
    return response.json()["id"]


async def send(
    client: AsyncClient, auth: dict[str, str], conversation_id: int, text: str = "hi"
) -> tuple[int, dict]:
    response = await client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"text": text, "page": "/overview"},
        headers=auth,
    )
    return response.status_code, response.json()


async def set_status(
    maker: async_sessionmaker[AsyncSession], turn_id: int, status: ChatTurnStatus
) -> None:
    async with maker() as session:
        await session.execute(update(ChatTurn).where(ChatTurn.id == turn_id).values(status=status))
        await session.commit()


async def turns_of(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> list[ChatTurn]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ChatTurn)
                    .where(ChatTurn.conversation_id == conversation_id)
                    .order_by(ChatTurn.id)
                )
            ).all()
        )


async def test_conversations_are_listed_for_their_owner_only(
    client: AsyncClient, auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    older = await new_conversation(client, auth, "Older")
    newer = await new_conversation(client, auth, "Newer")
    archived = await new_conversation(client, auth, "Archived")
    patched = await client.patch(
        f"{API}/conversations/{archived}", json={"archived": True}, headers=auth
    )
    assert patched.status_code == 200 and patched.json()["archived_at"] is not None
    async with maker() as session:
        theirs = (await seed_conversation(session, owner="outsider@example.com")).id
        await asyncio.sleep(0.01)
        # An item moves its conversation up: the list is by last activity.
        await append_item(
            session,
            conversation_id=older,
            turn_id=None,
            kind=ChatItemKind.USER_TEXT,
            payload={"text": "bump"},
        )
        await session.commit()

    listed = await client.get(CONVERSATIONS, headers=auth)

    assert listed.status_code == 200
    assert [(c["id"], c["title"]) for c in listed.json()] == [(older, "Older"), (newer, "Newer")]
    for method, path, body in [
        ("GET", f"{API}/conversations/{theirs}/items", None),
        ("PATCH", f"{API}/conversations/{theirs}", {"title": "mine now"}),
        ("DELETE", f"{API}/conversations/{theirs}", None),
        ("POST", f"{API}/conversations/{theirs}/messages", {"text": "hi"}),
    ]:
        response = await client.request(method, path, json=body, headers=auth)
        assert response.status_code == 404, (method, path)
    async with maker() as session:
        kept = await session.get_one(ChatConversation, theirs)
    assert kept.title is None


async def test_send_queues_a_turn_and_answers_202(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
    database_url: str,
) -> None:
    conversation = await new_conversation(client, auth)

    async with listening(database_url, CHAT_QUEUE_CHANNEL) as heard:
        status, body = await send(client, auth, conversation, "Why is #12 blocked?")
        await asyncio.wait_for(heard.get(), timeout=5)

    assert status == 202
    [turn] = await turns_of(maker, conversation)
    assert body == {"turn_id": turn.id}
    assert (turn.status, turn.trigger, turn.seq) == ("queued", "user", 1)
    items = (await client.get(f"{API}/conversations/{conversation}/items", headers=auth)).json()
    [item] = items["items"]
    assert (item["kind"], item["turn_id"], item["payload"]) == (
        "user_text",
        turn.id,
        {"text": "Why is #12 blocked?", "page": "/overview"},
    )
    assert items["turn"]["id"] == turn.id and items["turn"]["status"] == "queued"
    later = await client.get(
        f"{API}/conversations/{conversation}/items", params={"after": item["rev"]}, headers=auth
    )
    assert later.json()["items"] == []


async def test_second_send_while_live_is_409(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Two tabs on one conversation: the second send loses, whether the turn waits or runs."""
    conversation = await new_conversation(client, auth)
    first_status, first = await send(client, auth, conversation, "from tab one")
    assert first_status == 202

    queued_status, queued = await send(client, auth, conversation, "from tab two")
    await set_status(maker, first["turn_id"], ChatTurnStatus.RUNNING)
    running_status, running = await send(client, auth, conversation, "from tab two again")

    assert (queued_status, queued["code"]) == (409, "turn_live")
    assert (running_status, running["code"]) == (409, "turn_live")
    assert [turn.id for turn in await turns_of(maker, conversation)] == [first["turn_id"]]
    items = (await client.get(f"{API}/conversations/{conversation}/items", headers=auth)).json()
    assert [item["payload"]["text"] for item in items["items"]] == ["from tab one"]

    await set_status(maker, first["turn_id"], ChatTurnStatus.DONE)
    assert (await send(client, auth, conversation, "after it ended"))[0] == 202


async def test_send_over_the_daily_budget_is_429(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await new_conversation(client, auth)
    async with maker() as session:
        spent = await seed_turn(
            session,
            conversation,
            status=ChatTurnStatus.DONE,
            started_at=datetime.now(UTC),
            cost_usd=Decimal("19.99"),
        )
        await session.commit()
    assert enabled_assistant.daily_budget_usd == Decimal("20.00")
    status, body = await send(client, auth, conversation)
    assert status == 202
    await set_status(maker, body["turn_id"], ChatTurnStatus.DONE)

    async with maker() as session:
        await session.execute(
            update(ChatTurn).where(ChatTurn.id == spent.id).values(cost_usd=Decimal("20.00"))
        )
        await session.commit()
    status, body = await send(client, auth, conversation)

    assert (status, body["code"]) == (429, "daily_budget")
    assert len(await turns_of(maker, conversation)) == 2


async def seed_spend(
    maker: async_sessionmaker[AsyncSession], owner: str, cost: str, *, day: date | None = None
) -> None:
    async with maker() as session:
        session.add(
            ChatSpend(
                organization_id=await default_organization_id(session),
                owner=owner,
                day=day or datetime.now(UTC).date(),
                cost_usd=Decimal(cost),
            )
        )
        await session.commit()


async def test_send_over_the_organization_s_daily_budget_is_429(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    master_key: None,
) -> None:
    """The sender's own spend is under the person's budget; everyone's together is not."""
    await enable_assistant(maker, organization_daily_budget_usd="30.00")
    conversation = await new_conversation(client, auth)
    await seed_spend(maker, "someone-else@example.com", "15.00")
    await seed_spend(maker, ME, "15.00")

    status, body = await send(client, auth, conversation)

    assert (status, body["code"]) == (429, "daily_budget")
    assert body["detail"] == "Today's assistant budget of 30.00 USD for this organization is spent."


async def test_the_organization_budget_counts_only_today(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    master_key: None,
) -> None:
    await enable_assistant(maker, organization_daily_budget_usd="30.00")
    conversation = await new_conversation(client, auth)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    await seed_spend(maker, "someone-else@example.com", "30.00", day=yesterday)

    assert (await send(client, auth, conversation))[0] == 202


async def test_send_while_not_ready_is_409_not_configured_naming_the_cause(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    master_key: None,
) -> None:
    conversation = await new_conversation(client, auth)
    never = await send(client, auth, conversation)
    async with maker() as session:
        await svc.save_assistant_settings(
            session,
            await default_organization_id(session),
            AssistantSettings(enabled=True),
            by="test",
        )
        await session.commit()
    no_credential = await send(client, auth, conversation)
    await enable_assistant(maker, enabled=False)
    disabled = await send(client, auth, conversation)

    for status, body in (never, no_credential, disabled):
        assert (status, body["code"]) == (409, "not_configured")
    # A stored key with Enabled left off is named as off, never as a missing or faulty key.
    assert never[1]["detail"] == svc.ASSISTANT_OFF
    assert no_credential[1]["detail"] == svc.NO_CREDENTIAL
    assert disabled[1]["detail"] == svc.ASSISTANT_OFF
    assert "Enabled" in svc.ASSISTANT_OFF
    assert await turns_of(maker, conversation) == []


async def test_disabled_assistant_keeps_history_readable(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await new_conversation(client, auth)
    _, sent = await send(client, auth, conversation, "remember this")
    await set_status(maker, sent["turn_id"], ChatTurnStatus.DONE)
    await enable_assistant(maker, enabled=False)

    listed = await client.get(CONVERSATIONS, headers=auth)
    items = await client.get(f"{API}/conversations/{conversation}/items", headers=auth)
    status, body = await send(client, auth, conversation, "and this")

    assert [c["id"] for c in listed.json()] == [conversation]
    assert items.status_code == 200
    assert [item["payload"]["text"] for item in items.json()["items"]] == ["remember this"]
    assert items.json()["turn"]["status"] == "done"
    assert (status, body["code"]) == (409, "not_configured")


async def test_cancel_sets_cancel_requested(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
    database_url: str,
) -> None:
    running_in = await new_conversation(client, auth)
    _, running = await send(client, auth, running_in)
    await set_status(maker, running["turn_id"], ChatTurnStatus.RUNNING)
    waiting_in = await new_conversation(client, auth)
    _, waiting = await send(client, auth, waiting_in)

    async with listening(database_url, CHAT_QUEUE_CHANNEL) as heard:
        stopped = await client.post(f"{API}/turns/{running['turn_id']}/cancel", headers=auth)
        await asyncio.wait_for(heard.get(), timeout=5)
    dropped = await client.post(f"{API}/turns/{waiting['turn_id']}/cancel", headers=auth)

    assert (stopped.status_code, dropped.status_code) == (202, 202)
    [live] = await turns_of(maker, running_in)
    assert (live.cancel_requested, live.status) == (True, "running")
    # Nothing runs a queued turn yet, so its cancel is immediate.
    [never_ran] = await turns_of(maker, waiting_in)
    assert (never_ran.status, never_ran.stop_reason) == ("cancelled", "cancelled")
    assert never_ran.ended_at is not None
    async with maker() as session:
        theirs = await seed_conversation(session, owner="outsider@example.com")
        their_turn = await seed_turn(session, theirs.id, status=ChatTurnStatus.RUNNING)
        await session.commit()
    refused = await client.post(f"{API}/turns/{their_turn.id}/cancel", headers=auth)
    assert refused.status_code == 404
    assert not (await turns_of(maker, theirs.id))[0].cancel_requested


async def test_delete_removes_everything_of_a_conversation_with_a_live_turn(
    client: AsyncClient, auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    conversation = await new_conversation(client, auth)
    async with maker() as session:
        await seed_turn(session, conversation, status=ChatTurnStatus.RUNNING)

        await session.commit()

    deleted = await client.delete(f"{API}/conversations/{conversation}", headers=auth)

    assert deleted.status_code == 204
    async with maker() as session:
        assert await session.get(ChatConversation, conversation) is None
        assert (
            await session.scalars(select(ChatItem).where(ChatItem.conversation_id == conversation))
        ).all() == []


async def test_a_message_must_have_text_and_fit(
    client: AsyncClient, auth: dict[str, str], enabled_assistant: AssistantSettings
) -> None:
    conversation = await new_conversation(client, auth)
    for text in ("", "x" * 8001):
        response = await client.post(
            f"{API}/conversations/{conversation}/messages", json={"text": text}, headers=auth
        )
        assert response.status_code == 422


async def test_a_message_can_be_only_what_was_pointed_at_and_keeps_it(
    client: AsyncClient, auth: dict[str, str], enabled_assistant: AssistantSettings
) -> None:
    conversation = await new_conversation(client, auth)
    pointed = {
        "label": "Ticket #212 · Checkout total ignores the discount code",
        "text": "Blocked at build",
        "page": "/projects/5/tickets",
        "href": "/tickets/212",
    }
    url = f"{API}/conversations/{conversation}/messages"
    response = await client.post(url, json={"text": "", "context": [pointed]}, headers=auth)
    assert response.status_code == 202, response.text
    items = (await client.get(f"{API}/conversations/{conversation}/items", headers=auth)).json()
    [item] = items["items"]
    assert item["payload"] == {"text": "", "page": None, "context": [pointed]}


async def test_what_is_pointed_at_has_limits(
    client: AsyncClient, auth: dict[str, str], enabled_assistant: AssistantSettings
) -> None:
    conversation = await new_conversation(client, auth)
    url = f"{API}/conversations/{conversation}/messages"
    one = {"label": "A row", "text": "words"}
    for body in (
        {"text": "   "},
        {"text": "hi", "context": [one] * 6},
        {"text": "hi", "context": [{**one, "label": "x" * 81}]},
        {"text": "hi", "context": [{**one, "label": ""}]},
        {"text": "hi", "context": [{**one, "text": "x" * 2001}]},
        {"text": "hi", "context": [{**one, "lines": [0, 3]}]},
    ):
        response = await client.post(url, json=body, headers=auth)
        assert response.status_code == 422, body


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
LOG = b"12:00 started\n12:01 timed out\n"


def attached(name: str, content: bytes) -> dict[str, str]:
    return {"name": name, "data": base64.b64encode(content).decode()}


async def test_a_message_keeps_its_files_and_loses_them_with_its_conversation(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await new_conversation(client, auth)
    response = await client.post(
        f"{API}/conversations/{conversation}/messages",
        json={"text": "", "attachments": [attached("shot.png", PNG), attached("worker.log", LOG)]},
        headers=auth,
    )
    assert response.status_code == 202, response.text
    items = (await client.get(f"{API}/conversations/{conversation}/items", headers=auth)).json()
    [item] = items["items"]
    assert item["payload"] == {
        "text": "",
        "page": None,
        "attachments": [
            {"name": "shot.png", "media_type": "image/png", "size": len(PNG)},
            {"name": "worker.log", "media_type": "text/plain", "size": len(LOG)},
        ],
    }
    async with maker() as session:
        rows = (
            await session.scalars(
                select(ChatAttachment)
                .where(ChatAttachment.item_id == item["id"])
                .order_by(ChatAttachment.id)
            )
        ).all()
    assert [(row.name, row.media_type, row.content) for row in rows] == [
        ("shot.png", "image/png", PNG),
        ("worker.log", "text/plain", LOG),
    ]

    await client.delete(f"{API}/conversations/{conversation}", headers=auth)
    async with maker() as session:
        assert (await session.scalars(select(ChatAttachment))).all() == []


async def test_a_file_the_assistant_cannot_take_refuses_the_message_and_stores_nothing(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await new_conversation(client, auth)
    url = f"{API}/conversations/{conversation}/messages"
    for files, says in (
        ([attached("page.png", b"<html>hi</html>")], "`page.png` is not a PNG"),
        ([attached("empty.txt", b"")], None),
        ([{"name": "x.png", "data": "not base64!"}], "could not be read"),
        ([{"name": "x.png", "data": "caf\u00e9"}], "could not be read"),
        ([attached("worker.log", LOG)] * 5, None),
        ([attached("../../" + "x" * 130, LOG)], None),
    ):
        response = await client.post(url, json={"text": "hi", "attachments": files}, headers=auth)
        assert response.status_code == 422, files
        if says:
            assert response.json()["code"] == "attachment_refused"
            assert says in response.json()["detail"]
    assert await turns_of(maker, conversation) == []
    async with maker() as session:
        assert (await session.scalars(select(ChatAttachment))).all() == []
        assert (
            await session.scalars(select(ChatItem).where(ChatItem.conversation_id == conversation))
        ).all() == []


async def test_an_image_for_a_backend_that_takes_none_is_refused_and_a_text_file_is_not(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saved backend and sign-in, as one that is handed no images. Which those are is
    `vocabulary.takes_images`, whose own test lists every one a deployment can offer."""
    monkeypatch.setattr(assistant_send, "takes_images", lambda backend, auth_mode: False)
    monkeypatch.setattr(settings_routes, "takes_images", lambda backend, auth_mode: False)
    conversation = await new_conversation(client, auth)
    url = f"{API}/conversations/{conversation}/messages"
    refused = await client.post(
        url, json={"text": "see", "attachments": [attached("shot.png", PNG)]}, headers=auth
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "attachment_refused"
    assert "takes no images" in refused.json()["detail"]
    taken = await client.post(
        url, json={"text": "see", "attachments": [attached("worker.log", LOG)]}, headers=auth
    )
    assert taken.status_code == 202, taken.text

    ready = (await client.get(f"{ORG}/settings/assistant/ready", headers=auth)).json()
    assert ready["accepts_images"] is False


async def test_the_ready_answer_says_the_backend_takes_images(
    client: AsyncClient, auth: dict[str, str], enabled_assistant: AssistantSettings
) -> None:
    ready = (await client.get(f"{ORG}/settings/assistant/ready", headers=auth)).json()
    assert ready["accepts_images"] is True


async def test_deleting_a_conversation_does_not_give_back_the_daily_budget(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    spent_in = await new_conversation(client, auth)
    async with maker() as session:
        await seed_turn(
            session,
            spent_in,
            status=ChatTurnStatus.DONE,
            started_at=datetime.now(UTC),
            cost_usd=enabled_assistant.daily_budget_usd,
        )
        await session.commit()
    assert (await send(client, auth, spent_in))[1]["code"] == "daily_budget"

    deleted = await client.delete(f"{API}/conversations/{spent_in}", headers=auth)
    fresh = await new_conversation(client, auth)
    status, body = await send(client, auth, fresh)

    assert deleted.status_code == 204
    assert (status, body["code"]) == (429, "daily_budget")
    assert await turns_of(maker, fresh) == []


async def test_two_sends_at_once_one_is_accepted_and_one_conflicts(
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
    auth: dict[str, str],
) -> None:
    """Two tabs press Send together (Review Focus 1): each request on its own session, as two
    API workers would run them."""
    async with maker() as session:
        chat = (await seed_conversation(session, owner=ME)).id
        await session.commit()
    async with maker() as one, maker() as two:
        clients = [client_for(build_app(make_settings(), session)) for session in (one, two)]
        async with clients[0] as first, clients[1] as second:
            answers = await asyncio.gather(
                *(
                    tab.post(
                        f"{API}/conversations/{chat}/messages", json={"text": text}, headers=auth
                    )
                    for tab, text in ((first, "from tab one"), (second, "from tab two"))
                )
            )

    assert sorted(answer.status_code for answer in answers) == [202, 409]
    [refused] = [answer.json() for answer in answers if answer.status_code == 409]
    assert refused["code"] == "turn_live"
    assert len(await turns_of(maker, chat)) == 1


async def test_send_while_the_assistant_cannot_run_is_503(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A failed preflight: a turn queued now would wait for ever and block the next send."""
    conversation = await new_conversation(client, auth)

    async def status_row(value: dict | None) -> None:
        async with maker() as session:
            if value is None:
                await session.execute(delete(AppSetting).where(AppSetting.key == svc.STATUS_KEY))
            else:
                await svc.save_assistant_status(session, value)
            await session.commit()

    await status_row({"ok": False, "message": "claude_sdk: drift", "version": "x", "at": "x"})
    down = await send(client, auth, conversation)
    # No row: a fresh install whose process is still starting. Its send is not refused.
    await status_row(None)
    starting = await send(client, auth, conversation)
    await set_status(maker, starting[1]["turn_id"], ChatTurnStatus.DONE)
    await status_row({"ok": True, "message": None, "version": "x", "at": "x"})
    up = await send(client, auth, conversation)

    assert down == (
        503,
        {
            "code": "assistant_unavailable",
            "detail": "The assistant can't run a turn: claude_sdk: drift.",
        },
    )
    assert (starting[0], up[0]) == (202, 202)
    assert len(await turns_of(maker, conversation)) == 2


async def test_a_send_is_refused_only_when_its_own_backend_failed(
    client: AsyncClient,
    auth: dict[str, str],
    enabled_assistant: AssistantSettings,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The process serves every backend that passed: Codex failing its check on this host does
    not stop an organization whose assistant runs on Claude."""
    assert enabled_assistant.backend == "claude_sdk"
    conversation = await new_conversation(client, auth)
    backends = {
        "claude_sdk": {"ok": True, "message": None},
        "codex_cli": {"ok": False, "message": "`codex` is not on PATH"},
    }
    async with maker() as session:
        await svc.save_assistant_status(
            session,
            {
                "ok": False,
                "message": "codex_cli: `codex` is not on PATH",
                "version": "x",
                "at": "x",
                "backends": backends,
            },
        )
        await session.commit()

    status, _ = await send(client, auth, conversation)

    assert status == 202
