"""The built dashboard, served by the API under `APP_PREFIX` (tech-stack decisions 52 and 74)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from starlette._utils import get_route_path
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["DashboardFallback", "SpaStaticFiles"]

#: First path segments that are never a dashboard page, so a mistyped API path stays a JSON 404
#: rather than a page that loads and says nothing.
NOT_PAGES: Final = frozenset(
    {
        "api",
        "auth",
        "pages",
        "webhooks",
        "mcp",
        "healthz",
        "readyz",
        "docs",
        "redoc",
        "openapi.json",
        "terminal",
        ".well-known",
    }
)


class SpaStaticFiles(StaticFiles):
    """`ui/dist`, with a missing route answered by `index.html` so the client router takes it.

    A missing path whose last segment has a dot stays a 404: `/assets/app.js` names a file that
    should exist, and HTML in its place would hide a broken build behind a blank page.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or "." in path.rsplit("/", 1)[-1]:
                raise
            response = await super().get_response("index.html", scope)
        # index.html names the hashed bundle, so a cached copy hides a deploy from an open tab.
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


class DashboardFallback:
    """The API router's default: what no route matched is a dashboard page, unless it is a
    WebSocket, a write, or a path under one of `NOT_PAGES`, which get the router's own 404.

    A default rather than a mount at `/`: a mount matches every path, so it would answer a wrong
    method's 405 and the `/mcp` slash redirect with `index.html`.
    """

    def __init__(self, directory: Path, not_found: ASGIApp) -> None:
        self.files = SpaStaticFiles(directory=directory, html=True)
        self.not_found = not_found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        first = get_route_path(scope).lstrip("/").split("/", 1)[0]
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD") or first in NOT_PAGES:
            await self.not_found(scope, receive, send)
        else:
            await self.files(scope, receive, send)
