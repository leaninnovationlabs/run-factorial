"""The gallery's "From <default branch>" card: a read-only preview of the config folder the seed
would copy. Nothing is synced or recorded, so the sync guard has nothing to say here."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import config_source_dependency
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import RepoNotVisible
from tests.api_harness import authed_client_for, build_app, make_settings, seed_project
from tests.scaffold_lanes import STACKED
from tests.test_config_commits import FakeContents
from tests.test_repo_contents import request_failed

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

MAIN_SHA = "9" * 40
LANE = STACKED.replace("\nname: sample-lane\n", "\nname: standard\n")
AGENT = (
    "specVersion: factorial/v1\nkind: Agent\nname: {name}\ndescription: test agent\n"
    "prompts:\n  system: system-prompt.md\n  instructions: instructions.md\n"
    "model: claude-haiku-4-5-20251001\nlimits:\n  max_duration: 1h\n  max_turns: 40\n"
    "runtime:\n  kind: claude\n"
)


class MainContents(FakeContents):
    async def resolve_ref(self, repo: str, ref: str) -> str:
        return MAIN_SHA if ref == "main" else await super().resolve_ref(repo, ref)


def app_for(session: AsyncSession, files: dict[str, str]) -> FastAPI:
    contents = MainContents({})
    contents.files[MAIN_SHA] = files
    app = build_app(make_settings(), session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(contents)
    return app


async def _get(session: AsyncSession, files: dict[str, str]) -> httpx.Response:
    project = await seed_project(session)
    async with authed_client_for(app_for(session, files)) as client:
        return await client.get(f"/api/projects/{project.id}/default-branch-config")


async def test_no_config_folder_is_null(async_session: AsyncSession) -> None:
    response = await _get(async_session, {"src/app.py": "print()\n"})
    assert response.status_code == 200
    assert response.json() is None


async def test_a_folder_previews_each_lane_with_its_agents(async_session: AsyncSession) -> None:
    files = {".factorial/processes/process-standard.yaml": LANE}
    for name in ("plan-agent", "build-agent"):
        files[f".factorial/agents/{name}/agent.yaml"] = AGENT.format(name=name)
        files[f".factorial/agents/{name}/system-prompt.md"] = "x\n"
        files[f".factorial/agents/{name}/instructions.md"] = "x\n"
    response = await _get(async_session, files)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["branch"], body["sha"], body["config_dir"], body["error"]) == (
        "main",
        MAIN_SHA,
        ".factorial",
        None,
    )
    assert [lane["key"] for lane in body["lanes"]] == ["standard"]
    agents = body["lanes"][0]["agents"]
    assert {agent["name"] for agent in agents} == {"build-agent", "plan-agent"}
    assert all(agent["warning"] is None for agent in agents)


async def test_a_folder_that_does_not_parse_answers_its_error(async_session: AsyncSession) -> None:
    response = await _get(
        async_session, {".factorial/processes/process-standard.yaml": "kind: Process\n"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lanes"] == []
    assert body["error"]


async def test_an_unconfigured_app_answers_503(async_session: AsyncSession) -> None:
    project = await seed_project(async_session)
    app = build_app(make_settings(), async_session)  # no source override: the real dep refuses
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/default-branch-config")
    assert response.status_code == 503


class InvisibleRepo(MainContents):
    async def default_branch(self, repo: str) -> str:
        raise RepoNotVisible(f"the GitHub App cannot see {repo}")


async def test_a_repo_the_app_cannot_see_answers_502_naming_it(async_session: AsyncSession) -> None:
    """It answered a bare 500. Retrying cannot help, so the detail must not say to."""
    project = await seed_project(async_session)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(InvisibleRepo({}))
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/default-branch-config")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "GitHub App cannot see" in detail and detail.endswith(f"{project.github_repo}.")
    assert "Try again" not in detail


class RefusingRepo(MainContents):
    async def default_branch(self, repo: str) -> str:
        raise request_failed(403)


async def test_a_github_refusal_no_route_maps_answers_502(async_session: AsyncSession) -> None:
    """The app-wide handler: GitHub's status, not this server's "internal server error"."""
    project = await seed_project(async_session)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(RefusingRepo({}))
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/default-branch-config")
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub answered 403"
