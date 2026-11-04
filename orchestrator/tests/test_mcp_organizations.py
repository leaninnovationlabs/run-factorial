"""An MCP token, and an assistant turn, act in one organization (design.md §2.11).

Every ref a tool takes resolves within the caller's organization, and every list is drawn from
it: a project, a ticket or a run in another organization is not found, so a caller cannot learn
it exists. The assistant's read-only GitHub token is per organization too, minted over that
organization's repos alone.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.deps import ApiGitHub, github_for_dependency, read_token_minter_dependency
from orchestrator.assistant.github_token import Fetch, OrganizationTokens, ReadEntry
from orchestrator.assistant.toolbox import TurnToolbox
from orchestrator.db.models import Organization, Project
from orchestrator.github.client import GitHubClient, ReadToken
from orchestrator.github.connection import GitHubConnection
from orchestrator.github.contents import RepoContents
from orchestrator.mcp_server.chat.scope import ChatScope
from tests.api_harness import (
    TOKEN,
    build_app,
    build_mcp_app,
    client_for,
    make_run,
    make_settings,
    mcp_client,
    mcp_deps_for,
    mcp_settings,
    seed_mcp_token,
    seed_project,
    seed_ticket,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _organization(session: AsyncSession) -> Organization:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Acme", updated_by="test")
    session.add(organization)
    await session.flush()
    return organization


async def test_a_token_sees_its_own_organization_and_nothing_else(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        theirs = await _organization(session)
        _, token = await seed_mcp_token(session, organization_id=theirs.id)
        their_project = await seed_project(session, organization_id=theirs.id)
        their_ticket = await seed_ticket(session, their_project, external_key="7")
        our_project = await seed_project(session)
        our_ticket = await seed_ticket(session, our_project, external_key="7")
        session.add_all(
            [
                make_run(their_ticket.id, "develop", 1, cost="0.10"),
                make_run(our_ticket.id, "develop", 1, cost="0.10"),
            ]
        )
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        projects = await client.call_tool("factorial_projects_list", {})
        tickets = await client.call_tool("factorial_tickets_list", {"state": "all"})
        runs = await client.call_tool("factorial_runs_list", {})
        metrics = await client.call_tool("factorial_metrics_get", {})
        refused = {}
        for tool, args in [
            ("factorial_project_get", {"project": our_project.name}),
            ("factorial_project_get", {"project": our_project.github_repo}),
            ("factorial_project_get", {"project": f"id:{our_project.id}"}),
            ("factorial_ticket_get", {"ticket": f"id:{our_ticket.id}"}),
            ("factorial_ticket_get", {"ticket": f"{our_project.github_repo}#7"}),
        ]:
            with pytest.raises(ToolError) as error:
                await client.call_tool(tool, args)
            refused[f"{tool} {next(iter(args.values()))}"] = str(error.value)

    assert projects.structured_content is not None
    assert [p["name"] for p in projects.structured_content["projects"]] == [their_project.name]
    assert tickets.structured_content is not None
    assert [t["ticket_id"] for t in tickets.structured_content["tickets"]] == [their_ticket.id]
    assert runs.structured_content is not None
    assert {r["ticket_id"] for r in runs.structured_content["runs"]} == {their_ticket.id}
    assert metrics.structured_content is not None
    assert [m["project"] for m in metrics.structured_content["projects"]] == [their_project.name]
    assert all(message.startswith("No ") for message in refused.values()), refused
    # A refusal repeats what the caller typed, and offers names from the caller's organization only.
    for asked, message in refused.items():
        if asked.startswith("factorial_project_get"):
            assert message.endswith(f"Valid: {their_project.name}"), message


async def test_an_assistant_turn_reads_its_conversations_organization(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        theirs = await _organization(session)
        their_project = await seed_project(session, organization_id=theirs.id)
        our_project = await seed_project(session)
        await session.commit()
    tools = TurnToolbox(mcp_deps_for(mcp_settings(), maker), ChatScope(1, 1, "operator", theirs.id))

    listed = await tools.call("factorial_projects_list", {})
    other = await tools.call("factorial_project_get", {"project": our_project.name})

    assert listed.ok and their_project.name in listed.text
    assert our_project.name not in listed.text
    assert not other.ok


class _FakeMint:
    def __init__(self) -> None:
        self.wanted: list[frozenset[str]] = []

    async def __call__(self, client: GitHubClient, wanted: Collection[str]) -> ReadToken:
        self.wanted.append(frozenset(wanted))
        until = datetime(2026, 10, 13, 13, 0, tzinfo=UTC)
        return ReadToken(SecretStr("ghs_read"), until, tuple(sorted(wanted)))


async def test_the_assistants_github_token_covers_one_organizations_repos(
    async_session: AsyncSession,
) -> None:
    theirs = await _organization(async_session)
    their_repo = f"acme/{uuid4().hex[:8]}"
    our_repo = f"ours/{uuid4().hex[:8]}"
    await seed_project(async_session, their_repo, organization_id=theirs.id)
    await seed_project(async_session, our_repo)
    await async_session.commit()
    mint = _FakeMint()
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[read_token_minter_dependency] = lambda: mint
    github = GitHubClient(
        GitHubConnection(
            mode="app",
            app_id="7",
            private_key=SecretStr("key"),
            token=None,
            webhook_secret=None,
            bot_login="factorial-test[bot]",
            installations={},
        )
    )

    async def github_for(project: Project) -> ApiGitHub:
        return ApiGitHub(client=github, contents=RepoContents(github), bot_login="")

    app.dependency_overrides[github_for_dependency] = lambda: github_for

    async with client_for(app) as client:
        response = await client.post(
            f"/api/orgs/{theirs.slug}/assistant/github-token",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200, response.text
    assert mint.wanted == [frozenset({their_repo})]


async def test_each_organization_gets_a_token_cache_of_its_own() -> None:
    asked: list[str] = []

    def fetch_for(slug: str) -> Fetch:
        asked.append(slug)

        async def fetch() -> list[ReadEntry]:
            raise AssertionError("not fetched here")

        return fetch

    tokens = OrganizationTokens(fetch_for)
    first = tokens.for_organization(1, "default")
    assert tokens.for_organization(1, "default") is first
    assert tokens.for_organization(2, "acme") is not first
    assert asked == ["default", "acme"]
