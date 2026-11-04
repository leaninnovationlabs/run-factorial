"""Every guarded route names its permission, and the guard can find its organization
(design.md §7.19). The guard refuses a route that names nothing, so this test is what turns a
forgotten `@needs` into a red build rather than a route nobody can reach."""

from __future__ import annotations

import re
from collections.abc import Iterator

from fastapi.routing import APIRoute

from orchestrator.access import Permission
from orchestrator.access.decision import OWNER_PARAMS
from orchestrator.api.access import GUARD, declared_need
from orchestrator.api.main import create_app


def _guarded() -> Iterator[tuple[str, APIRoute]]:
    app = create_app()
    for included in app.router.routes:
        context = getattr(included, "include_context", None)
        router = getattr(included, "original_router", None)
        if context is None or router is None:
            continue
        if not any(dep is GUARD for dep in context.dependencies):
            continue
        for route in router.routes:
            if isinstance(route, APIRoute):
                yield context.prefix + route.path, route


def test_the_guard_covers_the_api() -> None:
    assert sum(1 for _ in _guarded()) > 100


def test_every_guarded_route_names_what_it_needs() -> None:
    bare = [path for path, route in _guarded() if declared_need(route.endpoint) is None]
    assert bare == []


def test_a_route_that_needs_a_permission_names_its_organization() -> None:
    """`organization_of` reads the first owner parameter the path holds; a permission on a path
    with none could never be granted to a member."""
    lost = [
        path
        for path, route in _guarded()
        if isinstance(declared_need(route.endpoint), Permission)
        and not set(re.findall(r"\{(\w+)", path)) & set(OWNER_PARAMS)
    ]
    assert lost == []
