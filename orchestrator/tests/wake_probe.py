"""A test-side `LISTEN factorial_wake` (or another channel): what the worker would hear, as a
queue of raw payloads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from orchestrator.db.session import asyncpg_dsn
from orchestrator.events import WAKE_CHANNEL


@asynccontextmanager
async def listening(
    database_url: str, channel: str = WAKE_CHANNEL
) -> AsyncIterator[asyncio.Queue[str]]:
    received: asyncio.Queue[str] = asyncio.Queue()
    connection = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        await connection.add_listener(
            channel, lambda _conn, _pid, _channel, payload: received.put_nowait(payload)
        )
        yield received
    finally:
        await connection.close()


async def notify(database_url: str, payload: str) -> None:
    """A committed NOTIFY from outside the code under test — a sentinel, or a stand-in sender."""
    connection = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        await connection.execute("SELECT pg_notify($1, $2)", WAKE_CHANNEL, payload)
    finally:
        await connection.close()
