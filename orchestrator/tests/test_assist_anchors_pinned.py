"""The dashboard's copies of the Show me lists match the server's (`mcp_server/chat/routes.py`).

The model may only name a control the server lists, and the Drawer only looks for one the UI
lists; a name in one list and not the other is a Show me that points at nothing.
"""

from __future__ import annotations

import re

from orchestrator.mcp_server.chat.routes import (
    ASSIST_ANCHORS,
    DASHBOARD_ROUTES,
    SECTION_SLUGS,
    route_for,
)
from tests.paths import REPO_ROOT

UI = REPO_ROOT / "ui" / "src"
#: The forms that create a project, a runtime, an invite or an organization: nothing to show on a
#: page not yet made.
NEW_FORMS = {"/projects/new", "/runtimes/new", "/members/invite", "/organizations/new"}


def _anchors_in_ui() -> list[str]:
    source = (UI / "components" / "assistant" / "assistAnchors.ts").read_text(encoding="utf-8")
    block = re.search(r"export const ASSIST_ANCHORS = \[(.*?)\] as const", source, re.S)
    assert block is not None, "assistAnchors.ts no longer declares ASSIST_ANCHORS as a const list"
    return re.findall(r'"([^"]+)"', block.group(1))


def _routes_in_ui() -> list[str]:
    source = (UI / "components" / "shell" / "routes.tsx").read_text(encoding="utf-8")
    block = re.search(r"export const ROUTES: AppRoute\[\] = \[(.*?)\n\];", source, re.S)
    assert block is not None, "routes.tsx no longer declares ROUTES"
    return re.findall(r'path: "([^"]+)"', block.group(1))


def test_the_ui_anchors_are_the_server_anchors() -> None:
    anchors = _anchors_in_ui()
    assert len(anchors) == len(set(anchors)), "an anchor is listed twice"
    assert set(anchors) == set(ASSIST_ANCHORS)


def test_the_server_routes_are_the_ui_pages_less_the_new_forms() -> None:
    pages = _routes_in_ui()
    assert set(pages) >= NEW_FORMS
    assert [path for path in pages if path not in NEW_FORMS] == list(DASHBOARD_ROUTES)


def test_the_server_section_slugs_are_the_ui_configuration_pages() -> None:
    """A configuration page the UI has and the server does not list is one Show me cannot open;
    one the server lists and the UI lacks is a Show me to a page that says it is not found."""
    source = (UI / "components" / "configuration" / "readiness.ts").read_text(encoding="utf-8")
    declared = re.search(r"export type SectionSlug =([^;]+);", source, re.S)
    assert declared is not None, "readiness.ts no longer declares SectionSlug"
    assert set(re.findall(r'"([^"]+)"', declared.group(1))) == SECTION_SLUGS


def test_a_section_the_ui_does_not_have_is_no_page() -> None:
    assert (
        route_for("/projects/3/configuration/repository-setup")
        == "/projects/:projectId/configuration/:section"
    )
    assert route_for("/projects/3/configuration") == "/projects/:projectId/configuration"
    assert route_for("/projects/3/configuration/plumbing") is None
