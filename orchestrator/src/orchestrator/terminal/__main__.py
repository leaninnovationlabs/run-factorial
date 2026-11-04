"""`python -m orchestrator.terminal` — the terminal gateway's entrypoint (decision 57).

The third process beside `api` and `worker` (decision 3): the `terminal` s6 service in the image,
locally and on ECS. It listens on `terminal.bind:terminal.port`, loopback by default. It reads
`.env` as the API does, so it needs FACTORIAL_API_TOKEN to verify a session and
FACTORIAL_MASTER_KEY to decrypt an E2B runtime's key; a rotated token takes a restart.

A stop can take 45 s: the shutdown closes every tmux client it attached before it exits, and a
supervisor that kills it sooner can leave a root client attached in an E2B sandbox.
"""

from __future__ import annotations

import asyncio
from typing import Final

import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.session import dispose_engine, get_sessionmaker
from orchestrator.logs import configure_logging
from orchestrator.settings import Settings, get_settings
from orchestrator.terminal.app import WS_PROTOCOL, create_app

__all__ = ["main", "server_config"]

#: A client frame carries keystrokes, a paste or a resize: far below websockets' 16 MiB default.
WS_MAX_SIZE: Final = 1 << 20

#: Longer than the load balancer's 60 s idle timeout, so the balancer, not uvicorn, closes an idle
#: connection: a request it sends down one uvicorn has just closed is a 502.
KEEP_ALIVE_SECONDS: Final = 75


def server_config(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> uvicorn.Config:
    return uvicorn.Config(
        create_app(settings, session_factory),
        host=settings.terminal.bind,
        port=settings.terminal.port,
        ws=WS_PROTOCOL,
        ws_max_size=WS_MAX_SIZE,
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
        # The app's lifespan shutdown is what closes the tmux clients; uvicorn waits for open
        # sockets without a deadline before it runs it.
        lifespan="on",
        log_config=None,
    )


async def _serve(settings: Settings) -> None:
    try:
        await uvicorn.Server(server_config(settings, get_sessionmaker())).serve()
    finally:
        await dispose_engine()


def main() -> None:
    configure_logging("terminal")
    asyncio.run(_serve(get_settings()))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
