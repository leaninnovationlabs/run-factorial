"""The gateway's only writes: `terminal_attached`, `terminal_detached`, `terminal_input` and
`terminal_seat_taken` (decision 57). Input is recorded as a byte count per burst, never its
content: a terminal is where a person pastes a credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.events import EventType, append
from orchestrator.events.terminals import TerminalRun

__all__ = ["InputBursts", "ViewerAudit"]

log = logging.getLogger(__name__)


class ViewerAudit:
    """One viewer's audit events, each committed on its own: none belongs to a state change."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run: TerminalRun,
        *,
        by: str,
        viewer: str,
    ) -> None:
        self._session_factory = session_factory
        self._run = run
        self._by = by
        self._viewer = viewer

    async def _append(self, type: EventType, **details: Any) -> None:
        async with self._session_factory() as session:
            await append(
                session,
                type=type,
                project_id=self._run.project_id,
                ticket_id=self._run.ticket_id,
                step_run_id=self._run.step_run_id,
                step=self._run.step,
                attempt=self._run.attempt,
                by=self._by,
                viewer=self._viewer,
                **details,
            )
            await session.commit()

    async def attached(self, *, writable: bool, target: str, via: str) -> None:
        await self._append(EventType.TERMINAL_ATTACHED, writable=writable, target=target, via=via)

    async def detached(self, *, seconds: float, code: int) -> None:
        await self._append(EventType.TERMINAL_DETACHED, seconds=round(seconds, 1), code=code)

    async def input(self, count: int) -> None:
        await self._append(EventType.TERMINAL_INPUT, bytes=count)

    async def seat_taken(self, *, holder: str | None, forced: bool) -> None:
        await self._append(EventType.TERMINAL_SEAT_TAKEN, forced=forced, **{"from": holder})


class InputBursts:
    """Coalesces a writer's input into bursts: one record after `quiet` seconds without input,
    or `max_wait` after the burst's first byte, and the rest on `stop()`.

    Input reaches the session at once; a burst is recorded within `quiet` or `max_wait`. A
    record that fails sets `failed` and calls `on_failure`, which closes the writer (1011) and
    refuses its later input; the failed burst itself goes unrecorded.
    """

    def __init__(
        self,
        write: Callable[[int], Awaitable[None]],
        *,
        quiet: float,
        max_wait: float,
        on_failure: Callable[[], None],
    ) -> None:
        self._write = write
        self._quiet = quiet
        self._max_wait = max_wait
        self._on_failure = on_failure
        self._loop = asyncio.get_running_loop()
        self._pending = 0
        self._first = 0.0
        self._last = 0.0
        self._wake = asyncio.Event()
        self._stopping = False
        self.failed = False
        self._task = self._loop.create_task(self._run())

    def count(self, n: int) -> None:
        if n <= 0 or self.failed:
            return
        now = self._loop.time()
        if self._pending == 0:
            self._first = now
            self._wake.set()
        self._pending += n
        self._last = now

    async def _run(self) -> None:
        while True:
            if self._pending == 0:
                if self._stopping:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            due = min(self._last + self._quiet, self._first + self._max_wait)
            delay = due - self._loop.time()
            if delay > 0 and not self._stopping:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
                continue
            count, self._pending = self._pending, 0
            try:
                await self._write(count)
            except Exception:
                log.exception("recording a terminal input burst of %d bytes failed", count)
                self.failed = True
                self._on_failure()
                return

    async def stop(self) -> None:
        """Records what is pending, and returns once nothing more will be written. Shielded: a
        burst cut off mid-write would go unrecorded."""
        self._stopping = True
        self._wake.set()
        await asyncio.shield(self._task)
