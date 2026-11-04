"""The organization routes and the collections under `/api/orgs/{org}` (tech-stack decision 68).

Each collection lists and creates within the path's organization only, an unknown slug is a 404
rather than the default, and no collection answers on a path without its organization.
These routes commit and the database outlives the test, so every organization here is new and
nothing assumes a clean database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import DEFAULT_ORGANIZATION
from orchestrator.db.models import ChatConversation, McpToken, Organization
from orchestrator.settings import AuthSettings
from tests.api_harness import (
    DASHBOARD,
    MCP_BASE,
    build_app,
    client_for,
    make_settings,
    seed_project,
    seed_ticket,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _organization(session: AsyncSession) -> Organization:
    organization = Organization(slug=f"org-{uuid4().hex[:12]}", name="Acme", updated_by="test")
    session.add(organization)
    await session.commit()
    return organization


def _names(response_json: list[dict[str, object]]) -> set[object]:
    return {row["name"] for row in response_json}


async def test_the_default_organization_is_listed_with_its_counts(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    await seed_project(async_session)
    await async_session.commit()
    listed = (await client.get("/api/orgs", headers=auth)).json()
    default = next(row for row in listed if row["slug"] == DEFAULT_ORGANIZATION)
    assert default["project_count"] >= 1

    one = await client.get(f"/api/orgs/{DEFAULT_ORGANIZATION}", headers=auth)
    assert one.status_code == 200
    assert one.json()["project_count"] == default["project_count"]


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/orgs/no-such-org", 404),
        ("/api/orgs/no-such-org/projects", 404),
        ("/api/orgs/no-such-org/settings/assistant", 404),
        # No organization can carry a slug the pattern refuses, so it is simply not found.
        ("/api/orgs/Not_A_Slug/projects", 404),
    ],
)
async def test_an_unknown_organization_is_refused_never_defaulted(
    client: AsyncClient, auth: dict[str, str], path: str, status: int
) -> None:
    assert (await client.get(path, headers=auth)).status_code == status


async def test_a_rename_keeps_the_slug_and_records_who_made_it(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    renamed = await client.patch(
        f"/api/orgs/{organization.slug}", headers=auth, json={"name": "Acme Corp"}
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert (body["slug"], body["name"], body["updated_by"]) == (
        organization.slug,
        "Acme Corp",
        "api-token",
    )
    refused = await client.patch(
        f"/api/orgs/{organization.slug}", headers=auth, json={"slug": "other"}
    )
    assert refused.status_code == 422


async def test_projects_are_listed_and_created_within_their_organization(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    name = f"web-{uuid4().hex[:8]}"
    theirs = await client.post(
        f"/api/orgs/{organization.slug}/projects",
        headers=auth,
        json={"name": name, "github_repo": "acme/web"},
    )
    assert theirs.status_code == 201, theirs.text
    assert theirs.json()["organization"] == organization.slug
    ours = await client.post(
        f"/api/orgs/{DEFAULT_ORGANIZATION}/projects",
        headers=auth,
        json={"name": name, "github_repo": "ours/web"},
    )
    assert ours.status_code == 201, "a name is unique within its organization only"
    again = await client.post(
        f"/api/orgs/{organization.slug}/projects",
        headers=auth,
        json={"name": name, "github_repo": "acme/web2"},
    )
    assert again.status_code == 409
    assert organization.slug in again.json()["detail"]

    listed = (await client.get(f"/api/orgs/{organization.slug}/projects", headers=auth)).json()
    assert [(row["id"], row["organization"]) for row in listed] == [
        (theirs.json()["id"], organization.slug)
    ]
    default = (await client.get(f"/api/orgs/{DEFAULT_ORGANIZATION}/projects", headers=auth)).json()
    assert theirs.json()["id"] not in {row["id"] for row in default}
    detail = await client.get(f"/api/projects/{theirs.json()['id']}", headers=auth)
    assert detail.json()["organization"] == organization.slug


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects",
        "/api/runtimes",
        "/api/overview",
        "/api/mcp/tokens",
        "/api/settings/assistant",
        "/api/assistant/conversations",
    ],
)
async def test_a_collection_has_no_path_without_its_organization(
    client: AsyncClient, auth: dict[str, str], path: str
) -> None:
    """The bare paths the dashboard called before it moved under `/orgs/{org}` are gone: a client
    still on one learns it at once, instead of reading the default organization unawares."""
    assert (await client.get(path, headers=auth)).status_code in (404, 405)


async def test_runtimes_are_listed_and_created_within_their_organization(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    slug = f"rt-{uuid4().hex[:8]}"
    body = {"slug": slug, "name": "Codex", "type": "codex_api"}
    theirs = await client.post(f"/api/orgs/{organization.slug}/runtimes", headers=auth, json=body)
    assert theirs.status_code == 201, theirs.text
    assert theirs.json()["organization"] == organization.slug
    ours = await client.post(f"/api/orgs/{DEFAULT_ORGANIZATION}/runtimes", headers=auth, json=body)
    assert ours.status_code == 201, "a slug is unique within its organization only"

    listed = (await client.get(f"/api/orgs/{organization.slug}/runtimes", headers=auth)).json()
    assert [row["id"] for row in listed] == [theirs.json()["id"]]
    default = (await client.get(f"/api/orgs/{DEFAULT_ORGANIZATION}/runtimes", headers=auth)).json()
    assert theirs.json()["id"] not in {row["id"] for row in default}


async def test_the_factory_board_shows_its_own_organizations_tickets(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    theirs = await seed_ticket(
        async_session, await seed_project(async_session, organization_id=organization.id)
    )
    ours = await seed_ticket(async_session, await seed_project(async_session))
    await async_session.commit()

    board = (await client.get(f"/api/orgs/{organization.slug}/overview", headers=auth)).json()
    assert [row["ticket_id"] for row in board["tickets"]] == [theirs.id]
    assert len(board["projects"]) == 1
    default = (await client.get(f"/api/orgs/{DEFAULT_ORGANIZATION}/overview", headers=auth)).json()
    ids = {row["ticket_id"] for row in default["tickets"]}
    assert ours.id in ids and theirs.id not in ids


async def test_mcp_tokens_are_listed_within_their_organization(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    label = f"theirs-{uuid4().hex[:8]}"
    async_session.add(
        McpToken(
            organization_id=organization.id,
            jti_sha256=uuid4().hex,
            audience="aud",
            label=label,
            created_by="bo@example.com",
            minted_via="api_token",
            access="read",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await async_session.commit()

    try:
        theirs = await client.get(f"/api/orgs/{organization.slug}/mcp/tokens", headers=auth)
        ours = await client.get(f"/api/orgs/{DEFAULT_ORGANIZATION}/mcp/tokens", headers=auth)
    finally:
        # Other modules count the token rows they made; this one must not outlive the test.
        await async_session.execute(delete(McpToken).where(McpToken.label == label))
        await async_session.commit()
    assert [row["label"] for row in theirs.json()["tokens"]] == [label]
    assert label not in {row["label"] for row in ours.json()["tokens"]}


async def test_conversations_are_listed_within_their_organization(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    title = f"theirs-{uuid4().hex[:8]}"
    created = await client.post(
        f"/api/orgs/{organization.slug}/assistant/conversations",
        headers=auth,
        json={"title": title},
    )
    assert created.status_code == 201, created.text
    row = await async_session.get_one(ChatConversation, created.json()["id"])
    assert row.organization_id == organization.id

    theirs = await client.get(
        f"/api/orgs/{organization.slug}/assistant/conversations", headers=auth
    )
    assert [c["title"] for c in theirs.json()] == [title]
    ours = await client.get(
        f"/api/orgs/{DEFAULT_ORGANIZATION}/assistant/conversations", headers=auth
    )
    assert title not in {c["title"] for c in ours.json()}


async def test_assistant_settings_are_each_organizations_own(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    organization = await _organization(async_session)
    url = f"/api/orgs/{organization.slug}/settings/assistant"
    # A settings write must come from the dashboard's origin, as `test_settings_routes` sets it.
    settings = make_settings(auth=AuthSettings(external_url=MCP_BASE))
    async with client_for(build_app(settings, async_session)) as client:
        saved = await client.put(
            url, headers=auth | DASHBOARD, json={"enabled": True, "daily_budget_usd": "7.00"}
        )
        assert saved.status_code == 200, saved.text
        theirs = (await client.get(url, headers=auth)).json()["settings"]
        ours = await client.get(
            f"/api/orgs/{DEFAULT_ORGANIZATION}/settings/assistant", headers=auth
        )
    assert (theirs["enabled"], theirs["daily_budget_usd"]) == (True, "7.00")
    assert ours.json()["settings"]["daily_budget_usd"] != "7.00"
