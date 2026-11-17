"""The terminal gateway's Starlette app (decision 57): `/factorial/terminal/runs/{step_run_id}`,
and `/terminal/healthz` for liveness, which asks for no Origin or session and reads no database.
The probe answers under `APP_PREFIX` too, where the load balancer forwards; its root path is the
one the target group checks, which an old image answered as well (decision 74).

It reaches a sandbox only through a relay: `host` attaches to tmux on this machine, `e2b`
through a `TerminalSandboxClient` holding the key the run's runtime now resolves to
(`factorial.sandbox_team`), which cannot wake a paused sandbox (decision 25). Its shutdown
closes every client it attached before it returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from orchestrator.db.models import Runtime
from orchestrator.events.terminals import TerminalRun
from orchestrator.factorial.runtimes import E2B_CREDENTIAL
from orchestrator.factorial.sandbox_team import sandbox_team
from orchestrator.logs import bind
from orchestrator.settings import APP_PREFIX, ManagedSandbox, Settings
from orchestrator.terminal.connection import Gateway, Relays, Timing, serve
from runtime.adapters.e2b.client import E2bTerminalClient
from runtime.contract import TerminalTarget
from runtime.terminal import E2bTmuxRelay, HostTmuxRelay, TerminalRelay

__all__ = ["WS_PROTOCOL", "SubstrateRelays", "create_app"]

#: uvicorn's WebSocket implementation for the gateway: the one on websockets' maintained API.
WS_PROTOCOL: Final = "websockets-sansio"


def _e2b_relay(api_key: str) -> TerminalRelay:
    return E2bTmuxRelay(E2bTerminalClient(api_key))


class SubstrateRelays:
    """A relay per `TerminalTarget.substrate`."""

    def __init__(
        self,
        *,
        host: TerminalRelay | None = None,
        e2b: Callable[[str], TerminalRelay] = _e2b_relay,
        managed: ManagedSandbox | None = None,
    ) -> None:
        self._host = host or HostTmuxRelay()
        self._e2b = e2b
        self._managed = managed or ManagedSandbox()

    async def relay_for(
        self, session: AsyncSession, run: TerminalRun, target: TerminalTarget
    ) -> TerminalRelay:
        if target.substrate == "host":
            return self._host
        if target.substrate != "e2b":
            raise ValueError(f"no relay attaches to a {target.substrate!r} terminal")
        if run.runtime_id is None:
            raise ValueError("an E2B run with no runtime row names no key to reach it with")
        row = await session.get(Runtime, run.runtime_id)
        if row is None:
            raise ValueError(f"runtime {run.runtime_id} no longer exists")
        # The runtime's key now, not the one the run spawned with: a team the runtime moved
        # away from is unreachable here (decision 25).
        team = await sandbox_team(session, row, self._managed)
        if team.api_key is None:
            # An operator's fix, not a moment's: retrying cannot reach the sandbox.
            raise ValueError(
                f"runtime {run.runtime_id} is on the Factorial-managed key, which this "
                "deployment does not provide"
                if team.managed
                else f"runtime {run.runtime_id} holds no {E2B_CREDENTIAL}"
            )
        return self._e2b(team.api_key.get_secret_value())


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    relays: Relays | None = None,
    *,
    timing: Timing | None = None,
) -> Starlette:
    gateway = Gateway(
        settings=settings,
        session_factory=session_factory,
        relays=relays or SubstrateRelays(managed=settings.runtime.e2b.managed),
        timing=timing or Timing(),
    )

    async def terminal(websocket: WebSocket) -> None:
        step_run_id = websocket.path_params["step_run_id"]
        with bind(step_run_id=step_run_id):
            await serve(gateway, websocket, step_run_id)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await gateway.shutdown()

    app = Starlette(
        routes=[
            Route("/terminal/healthz", _healthz),
            Route(f"{APP_PREFIX}/terminal/healthz", _healthz),
            WebSocketRoute(f"{APP_PREFIX}/terminal/runs/{{step_run_id:int}}", terminal),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    return app
