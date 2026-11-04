"""Process sync follows the ticket source: GitLab projects do not need the GitHub App."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import TicketSource, TicketSourceKind
from tests.api_harness import seed_project
from tests.api_probes import MINI_PROCESS_YAML, MINI_SHA, ProbeContents, mini_config_files

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _files() -> dict[str, str]:
    return {
        ".factorial/processes/process-mini.yaml": MINI_PROCESS_YAML,
        **mini_config_files([]),
    }


class _GitLabContents:
    """Stands in for `GitLabContents`: the sync must read these bytes, not GitHub."""

    def __init__(self, client: object) -> None:
        self._inner = ProbeContents(_files())

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.postgres
async def test_sync_reads_gitlab_when_the_ticket_source_is_gitlab(
    client: AsyncClient,
    auth: dict[str, str],
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await seed_project(async_session)
    project.settings = {"gitlab": {"base_url": "https://gitlab.example"}}
    async_session.add(
        TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITLAB,
            external_ref=project.github_repo,
        )
    )
    await async_session.flush()

    async def _client(session: object, proj: object) -> object:
        return object()

    monkeypatch.setattr("orchestrator.gitlab.project_client.client_for_project", _client)
    monkeypatch.setattr("orchestrator.gitlab.project_client.GitLabContents", _GitLabContents)

    response = await client.post(
        f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sha"] == MINI_SHA
    assert body["results"][0]["process"] == "mini"
    assert body["results"][0]["outcome"] == "created"


@pytest.mark.postgres
async def test_sync_gitlab_without_a_token_does_not_ask_for_the_github_app(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    async_session.add(
        TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITLAB,
            external_ref=project.github_repo,
        )
    )
    await async_session.flush()

    response = await client.post(
        f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "GITLAB_TOKEN" in detail
    assert "GitHub" not in detail
