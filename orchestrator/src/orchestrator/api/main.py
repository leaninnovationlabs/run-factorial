"""The FastAPI application — `orchestrator.api.main:app`, under `APP_PREFIX`, served through
`orchestrator.api.serve:app` (tech-stack.md §4).

One codebase, four entrypoints (tech-stack decision 3): this one serves read models and admin, the
worker runs the reconciliation loop, the terminal gateway relays terminals, and the assistant's
process runs chat turns. They share the package and the database and nothing else — a wedged
GitHub poll or a stuck chat turn cannot degrade the API, and the API cannot advance a ticket.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from githubkit.exception import RateLimitExceeded, RequestFailed
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orchestrator.access.accounts import HasherBusy
from orchestrator.api.access import GUARD
from orchestrator.api.auth import require_api_token
from orchestrator.api.chat_stream import ChatStreamHub
from orchestrator.api.deps import organization_path
from orchestrator.api.errors import backoff_handler, hasher_busy_handler, service_error_handler
from orchestrator.api.ratelimit import RateLimit, UntrustedForwardedFor
from orchestrator.api.routes import (
    accounts,
    admin,
    assistant,
    costs,
    dashboard,
    events,
    health,
    mcp_tokens,
    organizations,
    overview,
    pages,
    runs,
    runtimes,
    tickets,
    version,
    webhooks,
)
from orchestrator.api.routes import auth as auth_routes
from orchestrator.api.routes import mcp as mcp_routes
from orchestrator.api.routes import settings as settings_routes
from orchestrator.api.services.errors import ServiceError
from orchestrator.backoff import ConnectionBackoff
from orchestrator.dashboard import DashboardFallback
from orchestrator.db.session import dispose_engine
from orchestrator.logs import bind
from orchestrator.mcp_server.app import MCP_PATH, build_mcp_app
from orchestrator.mcp_server.context import McpDeps
from orchestrator.settings import APP_PREFIX, Settings, get_settings

__all__ = ["RequestIds", "app", "create_app", "create_site"]

log = logging.getLogger(__name__)

API_PREFIX = "/api"
#: Where a route that lists, creates or configures for one organization lives (decision 68).
ORG_PREFIX = f"{API_PREFIX}/orgs/{{org}}"


def warn_without_external_url(settings: Settings) -> None:
    """An API serving the dashboard, with no external URL: on any address but the admitted ones,
    every origin-checked route refuses the dashboard, and its refusal does not say why."""
    if settings.dashboard_dir is None or settings.auth.external_url.strip():
        return
    log.warning(
        "FACTORIAL_AUTH__EXTERNAL_URL is empty, so only %s may change the assistant settings, "
        "mint MCP tokens or open the terminal. Set it to the origin people open the dashboard "
        "on, such as https://factorial.example.com.",
        ", ".join(sorted(settings.dashboard_origins)) or "no origin",
    )


def warn_without_setup_token(settings: Settings) -> None:
    """With multi-org on or off, the first signup on an empty install owns `default`."""
    if not settings.auth.bootstrap_token.get_secret_value():
        log.warning(
            "FACTORIAL_AUTH__BOOTSTRAP_TOKEN is unset: on an empty install, whoever signs up "
            "first owns it. Sign up before others can reach the port, or set the token."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Nothing to open: the engine is created lazily on first use, so an API that starts before
    # Postgres does still comes up and answers /healthz. The chat hub connects on its first
    # stream, for the same reason.
    settings = get_settings()
    warn_without_external_url(settings)
    warn_without_setup_token(settings)
    hub: ChatStreamHub = app.state.chat_hub
    await hub.start()
    yield
    await hub.stop()
    # Before the engine goes: a device login's watcher may still want to store an outcome, and
    # its scratch home holds a plaintext login until it is settled or discarded.
    from orchestrator.api.device_login import close_logins

    await close_logins()
    await dispose_engine()


def create_app(
    cors_origins: Sequence[str] = (),
    *,
    mcp: McpDeps | None = None,
    docs: bool = False,
    dashboard_dir: Path | None = None,
) -> FastAPI:
    """Build the app, its paths relative to wherever it is mounted. `cors_origins` defaults to
    none; the module-level `app` passes `settings.cors_origins`, which is how the dashboard's dev
    server is let in. `mcp` mounts the operator MCP server at `/mcp/`; without it there is none.
    `dashboard_dir` answers every other GET with a dashboard page."""
    mounted = None if mcp is None else build_mcp_app(mcp)
    app = FastAPI(
        title="Factorial Orchestrator",
        version="0.1.0",
        summary="ADLC read models, admin, and the wake-up signals (webhook, refresh).",
        # Off unless asked: under the prefix the load balancer forwards them to the internet.
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        # Without the MCP transports' lifespan every authenticated call is a 500. Combined, they
        # stop before the engine is disposed.
        lifespan=(lifespan if mounted is None else combine_lifespans(lifespan, mounted.lifespan)),
    )

    app.state.chat_hub = ChatStreamHub()
    app.state.password_tries = RateLimit(accounts.PASSWORD_TRIES_PER_MINUTE, 60.0)

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            # Last-Event-ID: the Drawer resumes the chat stream with it (tech-stack decision 63).
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        )

    # Health is unauthenticated: a probe has no token. So is the webhook — it authenticates by
    # HMAC instead, because GitHub cannot present a bearer token.
    app.include_router(health.router)
    app.include_router(webhooks.router, prefix="/webhooks")
    # Unauthenticated by construction — these routes CREATE the session credential. Disabled
    # deployments answer 404 from inside the routes.
    app.include_router(auth_routes.router)
    app.include_router(accounts.router)
    # Outside the gate too: a frame cannot send the bearer token, so a page is reached by a link
    # the guarded API signed, plus the session cookie when Google sign-in is on (decision 58).
    app.include_router(pages.router)

    # Everything under /api sits behind the shared secret (tech-stack decision 12).
    guarded = [Depends(require_api_token), GUARD]
    app.include_router(tickets.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(events.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(costs.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(dashboard.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(runs.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(admin.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(admin.secret_router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(runtimes.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(mcp_tokens.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(mcp_routes.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(version.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(assistant.router, prefix=API_PREFIX, dependencies=guarded)
    app.include_router(organizations.router, prefix=API_PREFIX, dependencies=guarded)

    # Collections: one organization's projects, runtimes, tokens, conversations, factory board and
    # assistant settings, under `/orgs/{org}` and nowhere else (decision 68).
    org_scoped = [
        admin.org_router,
        overview.router,
        runtimes.org_router,
        mcp_tokens.org_router,
        assistant.org_router,
        settings_routes.router,
    ]
    for router in org_scoped:
        app.include_router(
            router, prefix=ORG_PREFIX, dependencies=[*guarded, Depends(organization_path)]
        )

    # Outside /api and its gate: the MCP server checks its own bearer.
    if mounted is not None:
        app.mount(MCP_PATH, mounted)
        app.state.mcp_server = mounted.state.fastmcp_server
        # An approved proposal runs on the ports the tools use (`routes/assistant.py`).
        app.state.mcp_deps = mcp

    if dashboard_dir is not None:
        app.router.default = DashboardFallback(dashboard_dir, app.router.default)

    # Starlette puts the `Exception` handler below in ServerErrorMiddleware, outside the
    # class-keyed handlers, so a ServiceError reaches this one first.
    app.exception_handler(ServiceError)(service_error_handler)
    app.exception_handler(HasherBusy)(hasher_busy_handler)
    # A connection waiting out a limit is not this server's fault, nor GitHub refusing the
    # request: `RateLimitExceeded` is a `RequestFailed`, and its own key wins over that one's.
    app.exception_handler(ConnectionBackoff)(backoff_handler)
    app.exception_handler(RateLimitExceeded)(backoff_handler)

    @app.exception_handler(RequestFailed)
    async def github_refused(request: Request, exc: RequestFailed) -> JSONResponse:
        """A GitHub read a route did not map is GitHub's answer, not this server's fault, so it
        is a 502 that says what GitHub said. A 404 on a private repo is also what the App sees
        for a repo its installation does not include, which a bare 500 left unexplained."""
        log.warning(
            "GitHub answered %s on %s %s",
            exc.response.status_code,
            request.method,
            request.url.path,
        )
        hint = (
            ": the GitHub App may not see this repository"
            if exc.response.status_code == 404
            else ""
        )
        return JSONResponse(
            status_code=502,
            content={"detail": f"GitHub answered {exc.response.status_code}{hint}"},
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """JSON, never an HTML traceback: every client here is a machine, and a stack trace on
        the wire is an information leak the log already holds."""
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


class RequestIds:
    """Binds a fresh `request_id` around each request and answers it as `X-Request-Id`, so the id
    a person quotes from a failed call finds its log lines.

    It is also the last error handler of an HTTP request. Starlette keeps its own
    `ServerErrorMiddleware` outside every middleware, and that one re-raises to uvicorn, which
    would log the traceback a second time, outside the bind. So nothing passes this one: an error
    the API's handler has answered is already logged, and any other is logged here and answered
    with a 500 that carries the id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(8)
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message).append("X-Request-Id", request_id)
            await send(message)

        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                if scope["type"] != "http":
                    raise
                if not started:
                    log.exception("unhandled error on %s %s", scope["method"], scope["path"])
                    response = JSONResponse(
                        status_code=500, content={"detail": "internal server error"}
                    )
                    await response(scope, receive, send_with_id)


async def _into_the_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Anything outside the prefix moves under it, as the load balancer sends it on ECS: a 302,
    because a browser keeps a 301 for ever and the host's root may serve something else later."""
    if scope["type"] != "http":
        await send({"type": "websocket.close", "code": 1000})
        return
    query = scope.get("query_string", b"").decode("latin-1")
    target = f"{APP_PREFIX}{scope['path']}" + (f"?{query}" if query else "")
    await RedirectResponse(target, status_code=302)(scope, receive, send)


def create_site(api: FastAPI) -> Starlette:
    """`api` at `APP_PREFIX`, with the health probes also at the root: a load balancer's and
    compose's checks go to the process directly, and an old image answered there too."""

    # A mount never runs its app's lifespan, and the MCP transports and chat hub start there.
    @asynccontextmanager
    async def lifespan(_site: Starlette) -> AsyncIterator[None]:
        async with api.router.lifespan_context(api):
            yield

    site = Starlette(
        routes=[
            Route("/healthz", api),
            Route("/readyz", api),
            Mount(APP_PREFIX, app=api),
        ],
        middleware=[Middleware(RequestIds), Middleware(UntrustedForwardedFor)],
        lifespan=lifespan,
    )
    site.router.default = _into_the_app
    return site


_settings = get_settings()
app = create_site(
    create_app(
        _settings.cors_origins,
        mcp=McpDeps(),
        docs=_settings.api_docs,
        dashboard_dir=_settings.dashboard_dir,
    )
)
