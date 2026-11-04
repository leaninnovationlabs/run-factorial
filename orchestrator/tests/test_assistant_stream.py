"""The chat stream: server-sent events of a conversation's items, its turn, and a heartbeat.

httpx's ASGI transport collects a whole body before it answers, and a stream never ends, so these
tests call the ASGI app themselves (`SseClient`): the request goes through the real app, its
auth and the real route, and the test reads each event as it is sent, then disconnects.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message

from orchestrator.api import chat_stream
from orchestrator.api.chat_stream import ChatStreamHub
from orchestrator.api.routes.assistant import stream_sessions_dependency
from orchestrator.assistant.items import append_item, items_after, notify_items, update_item
from orchestrator.assistant.turns import claim_turn, finish_turn
from orchestrator.db.models import ChatItem, ChatItemKind, ChatTurnStatus
from tests.api_harness import TOKEN, build_app, make_settings
from tests.assistant_fakes import seed_conversation, seed_turn, sessions_of, wipe_chat

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

AUTH = {"Authorization": f"Bearer {TOKEN}"}
ME = "api-token"
Event = dict[str, str]


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


@pytest.fixture(autouse=True)
def _slow_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long enough that only the hub's NOTIFY can deliver an event in time. The heartbeat test
    shortens it again."""
    monkeypatch.setattr(chat_stream, "PING_SECONDS", 30.0)


@pytest.fixture
async def hub(database_url: str) -> AsyncIterator[ChatStreamHub]:
    hub = ChatStreamHub(lambda: database_url)
    await hub.start()
    yield hub
    await hub.stop()


@pytest.fixture
def app(maker: async_sessionmaker[AsyncSession], hub: ChatStreamHub) -> FastAPI:
    # No request session: the stream route must not hold one.
    app = build_app(make_settings(), None)
    app.dependency_overrides[stream_sessions_dependency] = lambda: sessions_of(maker)
    app.state.chat_hub = hub
    return app


def _parse(block: str) -> Event:
    event: Event = {}
    for line in block.split("\n"):
        if line.startswith(":"):
            event["comment"] = line[1:].strip()
        else:
            key, _, value = line.partition(": ")
            event[key] = value
    return event


class SseClient:
    def __init__(self, app: FastAPI, url: str, headers: dict[str, str]) -> None:
        self._app = app
        self._url = urlsplit(url)
        self._headers = headers
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._started = asyncio.Event()
        self._gone = asyncio.Event()
        self._requested = False
        self._buffer = ""
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = ""

    async def _receive(self) -> Message:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"").decode()
            self.body += chunk
            self._buffer += chunk
            while "\n\n" in self._buffer:
                block, self._buffer = self._buffer.split("\n\n", 1)
                self._events.put_nowait(_parse(block))

    async def run(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self._url.path,
            "raw_path": self._url.path.encode(),
            "query_string": self._url.query.encode(),
            "root_path": "",
            "headers": [(b"host", b"api.test")]
            + [(k.lower().encode(), v.encode()) for k, v in self._headers.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("api.test", 80),
        }
        await self._app(scope, self._receive, self._send)

    async def next(self) -> Event:
        return await asyncio.wait_for(self._events.get(), timeout=5)

    async def until(self, done: Callable[[Event], bool]) -> list[Event]:
        seen = [await self.next()]
        while not done(seen[-1]):
            seen.append(await self.next())
        return seen

    def disconnect(self) -> None:
        self._gone.set()


@asynccontextmanager
async def stream(
    app: FastAPI, conversation_id: int, *, query: str = "", headers: dict[str, str] | None = None
) -> AsyncIterator[SseClient]:
    url = f"/api/assistant/conversations/{conversation_id}/stream{query}"
    client = SseClient(app, url, AUTH | (headers or {}))
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client._started.wait(), timeout=5)
    try:
        yield client
    finally:
        client.disconnect()
        await asyncio.wait_for(task, timeout=5)


def item_of(event: Event) -> dict[str, Any]:
    assert event["event"] == "item"
    return json.loads(event["data"])


async def add(
    maker: async_sessionmaker[AsyncSession],
    conversation_id: int,
    kind: ChatItemKind,
    payload: dict[str, Any],
) -> ChatItem:
    async with maker() as session:
        item = await append_item(
            session, conversation_id=conversation_id, turn_id=None, kind=kind, payload=payload
        )
        await notify_items(session, conversation_id)
        await session.commit()
    return item


async def rewrite(
    maker: async_sessionmaker[AsyncSession], item: ChatItem, payload: dict[str, Any]
) -> None:
    async with maker() as session:
        await update_item(session, item.id, payload=payload, final=True)
        await notify_items(session, item.conversation_id)
        await session.commit()


async def conversation(maker: async_sessionmaker[AsyncSession], owner: str = ME) -> int:
    async with maker() as session:
        conversation_id = (await seed_conversation(session, owner=owner)).id
        await session.commit()
    return conversation_id


async def test_stream_sends_items_in_rev_order_with_ids(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    chat = await conversation(maker)
    asked = await add(maker, chat, ChatItemKind.USER_TEXT, {"text": "hi"})
    await add(maker, chat, ChatItemKind.ASSISTANT_TEXT, {"text": "hello"})
    await add(maker, chat, ChatItemKind.TOOL, {"name": "factorial_tickets_list"})
    # Rewritten last, so it is sent last: the order is `rev`, not `id`.
    await rewrite(maker, asked, {"text": "hi there"})

    async with stream(app, chat) as client:
        assert client.status == 200
        assert client.headers["content-type"].startswith("text/event-stream")
        assert client.headers["cache-control"] == "no-store"
        first = [await client.next() for _ in range(4)]
        added = await add(maker, chat, ChatItemKind.NOTICE, {"text": "live"})
        live = await client.next()

    items = [item_of(event) for event in first[:3]]
    assert [item["kind"] for item in items] == ["assistant_text", "tool", "user_text"]
    assert items[2]["payload"] == {"text": "hi there"}
    assert [event["id"] for event in first[:3]] == [str(item["rev"]) for item in items]
    assert [item["rev"] for item in items] == sorted(item["rev"] for item in items)
    assert first[3] == {"event": "turn", "data": "null"}
    assert (item_of(live)["id"], live["id"]) == (added.id, str(added.rev))


async def test_stream_resume_delivers_latest_text_once(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A laptop sleeps mid-turn: the reply is rewritten twice while the stream is gone."""
    chat = await conversation(maker)
    reply = await add(maker, chat, ChatItemKind.ASSISTANT_TEXT, {"text": "Two"})
    await add(maker, chat, ChatItemKind.USER_TEXT, {"text": "and?"})
    async with stream(app, chat) as client:
        seen = await client.until(lambda event: event.get("event") == "turn")
    last = max(int(event["id"]) for event in seen if "id" in event)

    await rewrite(maker, reply, {"text": "Two tickets"})
    await rewrite(maker, reply, {"text": "Two tickets wait on review."})
    tail = await add(maker, chat, ChatItemKind.NOTICE, {"text": "done"})

    async with stream(app, chat, query="?after=0", headers={"Last-Event-ID": str(last)}) as client:
        resumed = await client.until(lambda event: event.get("event") == "turn")

    items = [item_of(event) for event in resumed if event.get("event") == "item"]
    assert [(item["id"], item["payload"]["text"]) for item in items] == [
        (reply.id, "Two tickets wait on review."),
        (tail.id, "done"),
    ]


async def test_stream_sends_turn_status_changes(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    chat = await conversation(maker)
    async with maker() as session:
        await seed_turn(session, chat)
        await session.commit()

    async with stream(app, chat) as client:
        queued = json.loads((await client.next())["data"])
        async with maker() as session:
            claimed = await claim_turn(
                session, owner="host:1", lease=timedelta(seconds=60), now=datetime.now(UTC)
            )
            await session.commit()
        running = json.loads((await client.next())["data"])
        assert claimed is not None
        async with maker() as session:
            await finish_turn(
                session,
                claimed,
                owner="host:1",
                status=ChatTurnStatus.DONE,
                now=datetime.now(UTC),
                stop="end",
            )
            await session.commit()
        done = await client.next()

    assert (queued["id"], queued["status"]) == (claimed.id, "queued")
    assert running["status"] == "running"
    assert done["event"] == "turn"
    assert json.loads(done["data"])["status"] == "done"
    assert json.loads(done["data"])["stop_reason"] == "end"


async def test_stream_heartbeats_while_idle(
    app: FastAPI, maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_stream, "PING_SECONDS", 0.05)
    chat = await conversation(maker)

    async with stream(app, chat) as client:
        events = [await client.next() for _ in range(3)]

    assert events == [{"event": "turn", "data": "null"}, {"comment": "ping"}, {"comment": "ping"}]


async def test_stream_is_its_owners_only(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    theirs = await conversation(maker, owner="outsider@example.com")

    async with stream(app, theirs) as client:
        pass

    assert client.status == 404
    assert json.loads(client.body) == {"detail": "Conversation not found."}


async def test_hub_wakes_only_the_conversations_subscribers(
    hub: ChatStreamHub, maker: async_sessionmaker[AsyncSession]
) -> None:
    mine, other = await conversation(maker), await conversation(maker)
    async with hub.subscribe(mine) as changed, hub.subscribe(other) as unchanged:
        # The first subscription connects the hub, and a connect wakes everyone once.
        await asyncio.wait_for(changed.wait(), timeout=5)
        changed.clear()
        unchanged.clear()
        await add(maker, mine, ChatItemKind.NOTICE, {"text": "x"})
        await asyncio.wait_for(changed.wait(), timeout=5)
    assert not unchanged.is_set()


async def _write(
    session: AsyncSession, conversation_id: int, how: str, existing: ChatItem, text: str
) -> int:
    """One item write as any writer could make it: an insert, or a plain UPDATE of a row that
    takes no lock of its own first. The id of the item written."""
    if how == "insert":
        item = await append_item(
            session,
            conversation_id=conversation_id,
            turn_id=None,
            kind=ChatItemKind.NOTICE,
            payload={"text": text},
        )
        return item.id
    await session.execute(
        update(ChatItem).where(ChatItem.id == existing.id).values(payload={"text": text})
    )
    return existing.id


@pytest.mark.parametrize(
    ("first", "second"),
    [("insert", "insert"), ("insert", "update"), ("update", "insert"), ("update", "update")],
)
async def test_a_cursor_never_passes_an_item_written_before_it(
    maker: async_sessionmaker[AsyncSession], first: str, second: str
) -> None:
    """Two writers on one conversation: the first draws its rev and has not committed when the
    second writes. The second must wait for the first, or it commits a higher rev first, a
    stream reads it, and the first item lands below the stream's cursor for good."""
    chat = await conversation(maker)
    one = await add(maker, chat, ChatItemKind.ASSISTANT_TEXT, {"text": "one"})
    two = await add(maker, chat, ChatItemKind.ASSISTANT_TEXT, {"text": "two"})
    seen: dict[int, Any] = {}
    last = two.rev

    async def read() -> None:
        nonlocal last
        async with maker() as session:
            for item in await items_after(session, chat, last):
                seen[item.id] = item.payload
                last = item.rev

    async def second_writer() -> int:
        async with maker() as session:
            written = await _write(session, chat, second, two, "second")
            await session.commit()
        return written

    async with maker() as session:
        first_id = await _write(session, chat, first, one, "first")
        later = asyncio.create_task(second_writer())
        await asyncio.sleep(0.3)
        waited = not later.done()
        await read()
        await session.commit()
    second_id = await asyncio.wait_for(later, timeout=5)
    await read()

    assert seen.get(first_id) == {"text": "first"}
    assert seen.get(second_id) == {"text": "second"}
    assert waited


async def test_two_streams_on_one_conversation_both_see_the_running_turn(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Two tabs on one conversation (Review Focus 1)."""
    chat = await conversation(maker)
    async with maker() as session:
        await seed_turn(session, chat)
        await session.commit()

    async with stream(app, chat) as first, stream(app, chat) as second:
        assert json.loads((await first.next())["data"])["status"] == "queued"
        assert json.loads((await second.next())["data"])["status"] == "queued"
        async with maker() as session:
            await claim_turn(
                session, owner="host:1", lease=timedelta(seconds=60), now=datetime.now(UTC)
            )
            await session.commit()
        running = [json.loads((await tab.next())["data"]) for tab in (first, second)]

    assert [turn["status"] for turn in running] == ["running", "running"]


async def test_the_cors_preflight_lets_a_reconnect_name_its_last_event() -> None:
    """The dashboard is another origin, in development and in the local image alike: a
    reconnect's `Last-Event-ID` is a header CORS must admit, or the browser never sends it."""
    from httpx import ASGITransport, AsyncClient

    from orchestrator.api.main import create_app

    app = create_app(cors_origins=["http://dash.test"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        response = await client.options(
            "/api/assistant/conversations/1/stream",
            headers={
                "Origin": "http://dash.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization, last-event-id",
            },
        )
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "last-event-id" in allowed
