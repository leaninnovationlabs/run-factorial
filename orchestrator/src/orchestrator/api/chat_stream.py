"""The assistant's chat stream: one `LISTEN` on the items channel, shared by every open stream, and
the events each stream sends (tech-stack decision 63).

A stream is server-sent events, read by the Drawer with `fetch`, never `EventSource`, which cannot
send the bearer header an API-token sign-in needs. Each item is an event whose id is its `rev`,
so a reconnect with `Last-Event-ID` gets every item written or rewritten since, once each, as it
stands now. A NOTIFY only says "look again": a stream re-reads past its last `rev` on every wake
and on every heartbeat, so a lost notification costs at most one heartbeat.

The hub connects when the first stream opens, not when the API starts, so an API nobody chats
on holds no extra connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final

from orchestrator.api.schemas import ChatItemOut, ChatTurnOut
from orchestrator.assistant.items import items_after
from orchestrator.assistant.turns import current_turn
from orchestrator.db.session import SessionScope
from orchestrator.events.channels import CHAT_ITEMS_CHANNEL
from orchestrator.events.listener import listen_forever
from orchestrator.settings import get_settings

__all__ = ["PING_SECONDS", "ChatStreamHub", "conversation_events"]

#: An idle stream's heartbeat, which also re-reads: proxies close a silent connection.
PING_SECONDS: Final = 15.0

_UNSENT: Final = object()


def _settings_database_url() -> str:
    return get_settings().database_url


class ChatStreamHub:
    def __init__(self, database_url: Callable[[], str] = _settings_database_url) -> None:
        self._database_url = database_url
        self._subscribers: dict[int, set[asyncio.Event]] = {}
        self._started = False
        self._stopping = asyncio.Event()
        self._listener: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._started = True
        self._stopping = asyncio.Event()

    async def stop(self) -> None:
        self._started = False
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    @asynccontextmanager
    async def subscribe(self, conversation_id: int) -> AsyncIterator[asyncio.Event]:
        """An event set whenever the conversation may have changed. Before `start`, or after
        `stop`, nothing sets it, and a stream falls back on its heartbeat."""
        changed = asyncio.Event()
        self._subscribers.setdefault(conversation_id, set()).add(changed)
        if self._started and self._listener is None:
            # A fresh context: the listener outlives the request that happens to start it, and
            # would otherwise log every reconnect with that request's id.
            self._listener = asyncio.create_task(
                listen_forever(
                    self._database_url(),
                    CHAT_ITEMS_CHANNEL,
                    on_notify=self._notified,
                    on_connect=self._everyone,
                    stopping=self._stopping,
                    probe_seconds=PING_SECONDS,
                ),
                context=contextvars.Context(),
            )
        try:
            yield changed
        finally:
            waiting = self._subscribers.get(conversation_id, set())
            waiting.discard(changed)
            if not waiting:
                self._subscribers.pop(conversation_id, None)

    def _notified(self, payload: str) -> None:
        try:
            conversation_id = int(payload)
        except ValueError:
            return
        for changed in self._subscribers.get(conversation_id, ()):
            changed.set()

    def _everyone(self) -> None:
        for waiting in self._subscribers.values():
            for changed in waiting:
                changed.set()


def _event(name: str, data: str, *, event_id: int | None = None) -> str:
    head = "" if event_id is None else f"id: {event_id}\n"
    return f"{head}event: {name}\ndata: {data}\n\n"


async def conversation_events(
    sessions: SessionScope,
    hub: ChatStreamHub,
    conversation_id: int,
    *,
    after: int,
    ping_seconds: float = PING_SECONDS,
) -> AsyncIterator[str]:
    """`item` events past `after`, in `rev` order; a `turn` event at the start and whenever the
    turn's state changes; a `: ping` comment after `ping_seconds` of quiet. Never ends by
    itself."""
    last = after
    sent: object = _UNSENT
    async with hub.subscribe(conversation_id) as changed:
        while True:
            # Cleared before the read: a NOTIFY that lands during it must not be slept through.
            changed.clear()
            async with sessions() as session:
                items = await items_after(session, conversation_id, last)
                row = await current_turn(session, conversation_id)
            for item in items:
                yield _event(
                    "item", ChatItemOut.model_validate(item).model_dump_json(), event_id=item.rev
                )
                last = item.rev
            turn = None if row is None else ChatTurnOut.model_validate(row)
            if turn != sent:
                yield _event("turn", "null" if turn is None else turn.model_dump_json())
                sent = turn
            try:
                await asyncio.wait_for(changed.wait(), timeout=ping_seconds)
            except TimeoutError:
                yield ": ping\n\n"
