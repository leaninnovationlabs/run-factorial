"""The project and ticket read tools, called through a real MCP client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.deps import settings_dependency
from orchestrator.api.main import create_app
from orchestrator.db.models import Event, TicketStatus
from orchestrator.events import EventType
from orchestrator.mcp_server.context import McpDeps
from tests.api_harness import (
    TOKEN,
    build_app,
    build_mcp_app,
    client_for,
    make_run,
    mcp_client,
    mcp_deps_for,
    mcp_settings,
    seed_mcp_token,
    seed_project,
    seed_ticket,
)


def _data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_a_ticket_is_listed_and_read_by_its_ref(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        project = await seed_project(session)
        await seed_ticket(session, project, external_key="7", title="Fix‮ the\n\tbug")
        name, repo = project.name, project.github_repo
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        listed = await client.call_tool("factorial_tickets_list", {"project": name})
        got = await client.call_tool("factorial_ticket_get", {"ticket": f"{repo}#7"})
        by_number = await client.call_tool("factorial_ticket_get", {"ticket": "7", "project": name})
        projects = await client.call_tool("factorial_projects_list", {})
        detail = await client.call_tool("factorial_project_get", {"project": repo})

    (summary,) = _data(listed)["tickets"]
    assert summary["ref"] == f"{repo}#7"
    assert summary["title"] == "Fix the bug"
    assert _data(got)["ticket"]["ref"] == f"{repo}#7"
    assert _data(got)["runs_total"] == 0
    assert _data(by_number) == _data(got)
    assert name in [p["name"] for p in _data(projects)["projects"]]
    assert _data(detail)["board"][0]["count"] == 1
    assert _data(detail)["pending"] is None


async def test_a_project_is_read_by_the_id_its_dashboard_page_shows(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The dashboard names a project by its id (`/projects/3`), and a Show me card needs that id
    to open one of the project's pages: the project reads carry it, and `id:N` reads it back."""
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        project = await seed_project(session)
        project_id, name = project.id, project.name
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        listed = await client.call_tool("factorial_projects_list", {})
        by_id = await client.call_tool("factorial_project_get", {"project": f"id:{project_id}"})

    assert {p["name"]: p["project_id"] for p in _data(listed)["projects"]}[name] == project_id
    assert (_data(by_id)["project"], _data(by_id)["project_id"]) == (name, project_id)


async def test_the_production_ports_report_an_unconnected_project_rather_than_crash(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`McpDeps()`'s own config ports, as `/mcp/` and an approved proposal run them, each called
    with the settings alone. Every other test here stubs them."""
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        project = await seed_project(session)
        repo = project.github_repo
        await session.commit()
    settings = mcp_settings()
    deps = McpDeps(settings=lambda: settings, sessions=mcp_deps_for(settings, maker).sessions)
    app = create_app(mcp=deps)
    app.dependency_overrides[settings_dependency] = lambda: settings

    async with mcp_client(app, token) as client:
        detail = await client.call_tool("factorial_project_get", {"project": repo})
        with pytest.raises(ToolError, match="no GitHub connection"):
            await client.call_tool(
                "factorial_config_read", {"project": repo, "path": "processes/a.yaml"}
            )

    assert _data(detail)["repo"] == repo


async def test_an_issue_that_is_not_a_ticket_needs_a_github_connection(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        project = await seed_project(session)
        await seed_ticket(session, project, external_key="7")
        repo = project.github_repo
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        with pytest.raises(ToolError, match="no GitHub connection"):
            await client.call_tool("factorial_ticket_get", {"ticket": f"{repo}#8"})
        with pytest.raises(ToolError, match="No ticket has ID"):
            await client.call_tool("factorial_ticket_get", {"ticket": "id:999999"})


async def test_the_dashboard_tool_answers_the_route_body(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """One service behind both (decision 61), so an operator's client reads the tab's figures.
    Only `as_of` and the velocity divided by it differ: each call reads the clock."""
    now = datetime.now(UTC)
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        project = await seed_project(session)
        ticket = await seed_ticket(session, project, step="done", status=TicketStatus.DONE)
        ticket.admitted_at = now - timedelta(days=3)
        ticket.closed_at = now - timedelta(days=1)
        session.add_all(
            [
                make_run(
                    ticket.id,
                    "develop",
                    1,
                    cost="1.25",
                    model="claude-sonnet-4-5-20250929",
                    tokens=(1000, 250),
                    started_at=now - timedelta(days=2),
                    took=timedelta(minutes=20),
                ),
                Event(
                    project_id=project.id,
                    ticket_id=ticket.id,
                    type=EventType.TICKET_DONE,
                    details={},
                    created_at=now - timedelta(days=1),
                ),
            ]
        )
        name, project_id = project.name, project.id
        await session.commit()
    settings = mcp_settings()

    async with mcp_client(build_mcp_app(settings, maker), token) as client:
        tool_body = _data(await client.call_tool("factorial_dashboard_get", {"project": name}))
    async with maker() as session, client_for(build_app(settings, session)) as api:
        response = await api.get(
            f"/api/projects/{project_id}/dashboard", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200, response.text

    route_body = response.json()
    assert tool_body["span"] == "all"
    assert tool_body["lifetime"]["runs"]["total"] == 1
    assert tool_body["period"]["tickets"]["done"] == 1
    for body in (tool_body, route_body):
        del body["as_of"], body["period"]["velocity_per_week"]
    assert tool_body == route_body


async def test_a_gitlab_project_is_read_with_the_gitlab_run_bot_fence(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The MCP once judged a GitLab issue through a bare snapshot whose comment check knew only
    the configured bot login, so a run token's own comments (`project_<id>_bot_…`) never counted
    there, though the worker counted them."""
    from dataclasses import replace
    from types import SimpleNamespace

    from orchestrator.adapters import CompositeStateSource
    from orchestrator.db.models import TicketSource, TicketSourceKind
    from orchestrator.mcp_server.context import ToolContext
    from tests.api_harness import port

    async with maker() as session:
        project = await seed_project(session)
        session.add(
            TicketSource(
                project_id=project.id,
                kind=TicketSourceKind.GITLAB,
                external_ref=project.github_repo,
            )
        )
        await session.flush()
        settings = mcp_settings()
        client = SimpleNamespace(settings=SimpleNamespace(bot_login=""))
        deps = replace(mcp_deps_for(settings, None), gitlab=port(client))
        tc = ToolContext(
            session=session,
            settings=settings,
            deps=deps,
            organization_id=project.organization_id,
        )
        bindings = await tc.bindings(project)
        assert bindings is not None
        source = bindings.snapshot(project.github_repo, "7")
        assert isinstance(source, CompositeStateSource)
        assert source.tracker.is_delivery_author("project_42_bot_0a1b2c")
        assert source.forge.is_delivery_author("project_42_bot_0a1b2c")
        await session.rollback()
