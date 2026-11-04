"""A project that is not on GitLab reads its config through GitHub alone."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import TicketSource, TicketSourceKind
from orchestrator.factorial import RepoConfigSource
from orchestrator.gitlab import project_client
from tests.api_harness import seed_project


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("kind", [TicketSourceKind.GITHUB, TicketSourceKind.JIRA, None])
async def test_a_github_projects_config_is_read_through_the_app_without_asking_gitlab(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kind: TicketSourceKind | None
) -> None:
    """Every agent resolve and process sync now asks which forge holds `.factorial/`. A project
    that is not on GitLab gets the App's reader, and no GitLab credential is even looked up."""
    project = await seed_project(async_session)
    if kind is not None:
        async_session.add(
            TicketSource(project_id=project.id, kind=kind, external_ref=project.github_repo)
        )
        await async_session.flush()

    async def no_gitlab(*_: object) -> None:
        raise AssertionError("a GitHub project's config read asked for GitLab credentials")

    monkeypatch.setattr(project_client, "client_for_project", no_gitlab)
    app_source = RepoConfigSource(_NeverRead())  # type: ignore[arg-type]

    assert await project_client.config_source_for(async_session, project, github=app_source) is (
        app_source
    )


class _NeverRead:
    """The App's contents reader; choosing it reads nothing."""
