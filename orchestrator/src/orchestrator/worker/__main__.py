"""`python -m orchestrator.worker` / `factorial-worker` — the reconciliation loop's entrypoint.

One codebase, four entrypoints (tech-stack decision 3): s6 runs this beside `api` as a service of
its own, on a laptop and on ECS (decision 52), and they share only the database and the run area.
"""

from __future__ import annotations

import asyncio
import logging

from orchestrator.db.session import dispose_engine, session_scope
from orchestrator.logs import configure_logging
from orchestrator.settings import Settings, get_settings
from orchestrator.worker import leases, singleton
from orchestrator.worker.context import production_deps
from orchestrator.worker.loop import run_forever
from orchestrator.worker.parked import GitHubParked

__all__ = ["main"]

logger = logging.getLogger(__name__)


async def _run(settings: Settings) -> None:
    """Take the singleton lock (decision 40) FIRST — before anything opens a GitHub client, a
    runtime adapter, or a database pool — so a second worker refuses to start without having touched
    any of them. `WorkerAlreadyRunning` propagates uncaught: the hook `configure_logging` installs
    logs the message it already carries, and the process exits non-zero.

    Then drop every ticket lease. With the lock held no other runner should exist (decision 40's
    gap aside), so a lease left over is a killed worker's (a deploy kills the old task mid-tick),
    and it would keep its ticket out of every pass for up to `worker.lease_seconds`.
    """
    lock = await singleton.acquire(settings.database_url, worker_name=settings.worker.name)
    try:
        try:
            async with session_scope() as session:
                released = await leases.release_all(session)
            logger.info("released %d lease(s) a previous worker left", released)
            deps = production_deps(settings)
            await run_forever(
                deps,
                settings=settings,
                parked=GitHubParked,
            )
        finally:
            await dispose_engine()
    finally:
        await singleton.release(lock)


def main() -> None:
    configure_logging("worker")
    asyncio.run(_run(get_settings()))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
