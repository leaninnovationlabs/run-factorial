"""A `LISTEN` that outlives its connection: the worker's wake listener, the assistant's queue
listener and the API's chat streams all hold theirs through `listen_forever`.

It reconnects with backoff, probes the connection on an interval because a half-open socket never
reports its own death, and reports every connect through `on_connect`, since whatever was sent
while the connection was down is gone. A listener only shortens latency: whoever holds one also
polls, so a failed connection is a warning, never an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Final

import asyncpg

from orchestrator.db.session import asyncpg_dsn

__all__ = ["RETRY_SECONDS", "listen_forever"]

_log = logging.getLogger(__name__)

#: First wait before reconnecting; it doubles up to the probe interval.
RETRY_SECONDS: Final = 1.0


async def listen_forever(
    database_url: str,
    channel: str,
    *,
    on_notify: Callable[[str], None],
    on_connect: Callable[[], None],
    stopping: asyncio.Event,
    probe_seconds: float,
    retry_seconds: float = RETRY_SECONDS,
    application_name: str | None = None,
    log: logging.Logger = _log,
) -> None:
    """Until `stopping` is set. `application_name` names the connection in `pg_stat_activity`
    once its `LISTEN` is registered, so a listener that would still miss a NOTIFY never shows."""
    dsn = asyncpg_dsn(database_url)
    longest_delay = max(probe_seconds, retry_seconds)
    clock = asyncio.get_running_loop()
    delay = retry_seconds
    while not stopping.is_set():
        try:
            connection = await asyncpg.connect(dsn)
        except Exception as exc:
            log.warning(
                "LISTEN %s unavailable (%s); retrying in %.1fs, polling meanwhile",
                channel,
                exc,
                delay,
            )
            await _pause(stopping, delay)
            delay = min(delay * 2, longest_delay)
            continue

        lost = asyncio.Event()
        connected_at = clock.time()
        try:
            # Bound now: a late callback from this connection must not mark the next one lost.
            connection.add_termination_listener(lambda _connection, lost=lost: lost.set())
            await connection.add_listener(
                channel, lambda _connection, _pid, _channel, payload: on_notify(payload)
            )
            if application_name is not None:
                await connection.execute(
                    "SELECT set_config('application_name', $1, false)", application_name
                )
            log.info("LISTEN %s connected", channel)
            on_connect()
            while not stopping.is_set() and not lost.is_set():
                await _wait(lost, stopping, probe_seconds)
                if not stopping.is_set() and not lost.is_set():
                    await asyncio.wait_for(connection.fetchval("SELECT 1"), timeout=5)
        except Exception as exc:
            if not stopping.is_set():
                log.warning("LISTEN %s lost (%s); reconnecting", channel, exc)
        else:
            if lost.is_set() and not stopping.is_set():
                log.warning("LISTEN %s lost; reconnecting", channel)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=5)
            if not connection.is_closed():
                connection.terminate()

        # Every reconnect is reported, and a report starts work (a fleet pass, a re-read), so a
        # connection that keeps dropping — an `idle_session_timeout` below the interval, a
        # database too busy to answer the probe — backs off like a failed connect. Only one
        # that held for a whole probe interval resets the delay.
        if clock.time() - connected_at >= probe_seconds:
            delay = retry_seconds
        await _pause(stopping, delay)
        delay = min(delay * 2, longest_delay)


async def _pause(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


async def _wait(lost: asyncio.Event, stopping: asyncio.Event, seconds: float) -> None:
    waiters = [asyncio.create_task(lost.wait()), asyncio.create_task(stopping.wait())]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
