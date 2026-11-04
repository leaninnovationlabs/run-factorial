"""The workspace reaper (decision 25): which runs still want their workspace, told to the adapter.

A paused E2B sandbox, or a host run's worktree, is kept until something deletes it, and only the
orchestrator knows which are still wanted: the one keep rule (`events.workspaces`, decision 54).
The adapter removes the rest of its own deployment's workspaces
(`runtime.contract.WorkspaceReaper`): one reaper per E2B team the runtimes spawn into, read
afresh each pass, and one per host adapter's `local_root`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.session import SessionScope
from orchestrator.events.workspaces import kept_workspaces
from runtime.contract import WorkspaceReaper

logger = logging.getLogger(__name__)

INTERVAL: Final = timedelta(minutes=10)

__all__ = ["INTERVAL", "Reapers", "reap_forever", "reap_once"]


#: `session -> the reapers`, read in the same session as the wanted runs.
Reapers = Callable[[AsyncSession], Awaitable[Sequence[WorkspaceReaper]]]


async def reap_once(
    scope: SessionScope,
    reapers: Reapers,
    *,
    now: datetime,
    retention: timedelta,
    retain: timedelta,
) -> tuple[str, ...]:
    async with scope() as session:
        wanted = (await kept_workspaces(session, now, retention)).keep
        listed = await reapers(session)
    reaped: list[str] = []
    for index, reaper in enumerate(listed):
        try:
            # The mirrors are one store whatever team ran them: the first reaper, which holds
            # no key and so cannot fail on a team, prunes it.
            reaped += await reaper.reap(wanted, retain=retain if index == 0 else None)
        except Exception:
            # One team's outage or revoked key must not keep the others' workspaces alive.
            logger.exception("one workspace reaper failed")
    return tuple(reaped)


async def reap_forever(
    scope: SessionScope,
    reapers: Reapers,
    stopping: asyncio.Event,
    *,
    now: Callable[[], datetime],
    retention: timedelta,
    retain: timedelta,
    interval: timedelta = INTERVAL,
) -> None:
    while not stopping.is_set():
        try:
            reaped = await reap_once(scope, reapers, now=now(), retention=retention, retain=retain)
            if reaped:
                logger.info("reaped the workspaces of %d unwanted runs: %s", len(reaped), reaped)
        except Exception:
            # A missed pass only delays cleanup; the next one reads the same state.
            logger.exception("the workspace reaper's pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), interval.total_seconds())
