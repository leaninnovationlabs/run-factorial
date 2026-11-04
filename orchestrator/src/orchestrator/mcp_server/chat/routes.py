"""The dashboard places a Show me card may point at: the pages and the controls.

Closed lists, so the model cannot send a person anywhere else. `DASHBOARD_ROUTES` follows the
pages of `ui/src/components/shell/routes.tsx`, less its two "new" forms. An anchor is the name a
control carries in its `data-assist-anchor` attribute.

A project is a place with pages (tech-stack decision 69): its dashboard, its tickets, its
configuration overview and one page per configuration section, whose `:section` is one of
`SECTION_SLUGS` — the UI's `SectionSlug` (`ui/src/components/configuration/readiness.ts`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

__all__ = ["ASSIST_ANCHORS", "DASHBOARD_ROUTES", "ID_PARAMS", "SECTION_SLUGS", "route_for"]

#: react-router patterns; a `:param` is one path segment.
DASHBOARD_ROUTES: Final[tuple[str, ...]] = (
    "/overview",
    "/projects",
    "/projects/:projectId",
    "/projects/:projectId/tickets",
    "/projects/:projectId/configuration",
    "/projects/:projectId/configuration/:section",
    "/tickets/:ticketId",
    "/runtimes",
    "/runtimes/:slug",
    "/assistant",
    "/account/mcp",
    "/settings",
    "/members",
)

#: The configuration pages a project has, as `/projects/:projectId/configuration/:section` names
#: them. A slug the UI does not have is a page that says it is not found, so it is no place to
#: send anyone.
SECTION_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "details",
        "repository-setup",
        "process",
        "ticket-source",
        "runtime",
        "secrets",
    }
)

ASSIST_ANCHORS: Final[frozenset[str]] = frozenset(
    {
        # /overview
        "overview.board",
        # /projects/:projectId and the project's other pages: the side navigation inside a project
        "project.nav",
        # /projects/:projectId and /projects/:projectId/configuration
        "project.admission",
        # /projects/:projectId
        "project.spend",
        # /projects/:projectId/configuration
        "configuration.check",
        # /projects/:projectId/configuration/process, the process editor
        "process.commit",
        # /tickets/:ticketId
        "ticket.actions",
        "ticket.graph",
        "ticket.activity",
        # /runtimes
        "runtimes.new",
        # /account/mcp
        "mcp.new_token",
        # /settings
        "settings.credential",
    }
)

#: The params that are factorial's own id of a row, and what the id names: a tool takes it as
#: `id:N`. The dashboard reads them as numbers, so a name there opens a page that says not found.
ID_PARAMS: Final[Mapping[str, str]] = {":projectId": "project", ":ticketId": "ticket"}

#: An unreserved URL character run: no `/`, `?`, `#` or `%`, so a path cannot smuggle a query.
_SEGMENT: Final = r"[A-Za-z0-9._~-]+"


def _part(part: str) -> str:
    if part == ":section":
        return "(?:" + "|".join(re.escape(slug) for slug in sorted(SECTION_SLUGS)) + ")"
    if part in ID_PARAMS:
        return "[0-9]+"
    return _SEGMENT if part.startswith(":") else re.escape(part)


_PATTERNS: Final = {
    route: re.compile("/".join(_part(part) for part in route.split("/")))
    for route in DASHBOARD_ROUTES
}


def route_for(path: str) -> str | None:
    """The `DASHBOARD_ROUTES` pattern `path` is a page of, or None."""
    return next((route for route, pattern in _PATTERNS.items() if pattern.fullmatch(path)), None)
