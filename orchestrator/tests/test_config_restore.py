"""Restoring the config folder to an earlier synced version (tech-stack decision 93).

A restore is the builder's commit computed by the server: the difference between the version's
tree and the branch head, written and deleted in one commit, recorded before the ref moves and
then synced. The caller names a version id and nothing else, so no path or sha it supplies
decides what is read or written.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.config import sync_config
from orchestrator.db.models import Event, Project, Runtime, RuntimeType
from orchestrator.events import EventType, append
from orchestrator.factorial import CONFIG_BRANCH, RepoConfigSource
from tests.api_harness import authed_client_for
from tests.test_config_commits import (
    BASE_SHA,
    NEW_SHA,
    PROCESS_PATH,
    FakeGitApi,
    _runtime,
    app_with,
    lane,
    project_in,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

OLD_SHA = "d" * 40
A = ".factorial/notes/a.md"
B = ".factorial/notes/b.md"
C = ".factorial/notes/c.md"


async def restorable(
    session: AsyncSession,
    *,
    old: Mapping[str, str],
    head: Mapping[str, str],
) -> tuple[FastAPI, FakeGitApi, Project, int]:
    """A project whose `factorial` head holds `head`, with a version synced at `OLD_SHA` that
    held `old`. Returns the id of one of that version's rows."""
    app, api = app_with(session, extra=head)
    project = await project_in(session)
    api.contents.files[OLD_SHA] = dict(old)
    await append(
        session,
        type=EventType.CONFIG_COMMITTED,
        project_id=project.id,
        branch=CONFIG_BRANCH,
        sha=OLD_SHA,
    )
    synced = await sync_config(session, RepoConfigSource(api.contents), project, sha=OLD_SHA)
    row = synced.result.results[0].row
    assert row is not None
    return app, api, project, row.id


async def _preview(app: FastAPI, project: Project, version_id: int) -> httpx.Response:
    async with authed_client_for(app) as client:
        return await client.get(
            f"/api/projects/{project.id}/process-versions/{version_id}/restore"
        )


async def _restore(
    app: FastAPI, project: Project, version_id: int, expected_head: str = BASE_SHA
) -> httpx.Response:
    async with authed_client_for(app) as client:
        return await client.post(
            f"/api/projects/{project.id}/config-restores",
            json={"version_id": version_id, "expected_head": expected_head},
        )


OLD = {PROCESS_PATH: lane(), A: "1", B: "1"}
HEAD = {PROCESS_PATH: lane(), A: "2", C: "1"}


async def test_the_plan_is_the_difference_between_the_version_and_the_head(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    response = await _preview(app, project, version_id)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "target_sha": OLD_SHA,
        "head_sha": BASE_SHA,
        "writes": [A, B],
        "deletes": [C],
        "retires": [],
        "unchanged": False,
    }
    assert api.trees == []


async def test_restore_commits_the_difference_records_it_and_syncs(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    response = await _restore(app, project, version_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert api.written == {A: "1", B: "1"}
    assert api.removed == [C]
    # The event names the commit before the ref moves (decision 53).
    assert api.order == ["record", "ref"]
    assert api.commits[0]["message"] == (
        f"Restore the config to {OLD_SHA[:7]} from the Factorial dashboard"
    )
    assert api.commits[0]["parents"] == [BASE_SHA]
    assert (body["sha"], body["committed"]) == (NEW_SHA, True)
    assert (body["written"], body["deleted"]) == ([A, B], [C])
    assert body["sync_error"] is None
    assert api.contents.files[NEW_SHA] == OLD
    events = (
        (
            await async_session.execute(
                select(Event).where(
                    Event.project_id == project.id, Event.type == str(EventType.CONFIG_COMMITTED)
                )
            )
        )
        .scalars()
        .all()
    )
    restored = [event.details for event in events if event.details["sha"] == NEW_SHA]
    assert [details["restored_from"] for details in restored] == [OLD_SHA]


async def test_a_process_the_version_lacks_is_named_and_retired(
    async_session: AsyncSession,
) -> None:
    other = ".factorial/processes/process-other.yaml"
    app, api, project, version_id = await restorable(
        async_session, old=OLD, head={**OLD, other: lane("other")}
    )
    # The head is synced too, so `other` is a process the project offers until the restore.
    await sync_config(async_session, RepoConfigSource(api.contents), project)
    preview = (await _preview(app, project, version_id)).json()
    assert (preview["deletes"], preview["retires"]) == ([other], ["other"])
    response = await _restore(app, project, version_id)
    assert response.status_code == 201, response.text
    assert api.removed == [other]
    outcomes = {row["process"]: row["outcome"] for row in response.json()["sync"]["results"]}
    assert outcomes["other"] == "retired"


async def test_restoring_the_version_the_head_already_holds_commits_nothing(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=OLD)
    assert (await _preview(app, project, version_id)).json()["unchanged"] is True
    response = await _restore(app, project, version_id)
    assert response.status_code == 201, response.text
    assert response.json()["committed"] is False
    assert response.json()["sha"] == BASE_SHA
    assert api.trees == []


async def test_a_version_of_another_project_or_repository_is_a_404(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    assert (await _preview(app, project, version_id)).status_code == 200
    stranger = await project_in(async_session)
    assert (await _preview(app, stranger, version_id)).status_code == 404
    assert (await _restore(app, stranger, version_id)).status_code == 404
    project.github_repo = "o/moved"
    await async_session.flush()
    assert (await _restore(app, project, version_id)).status_code == 404
    assert api.trees == []


async def test_an_unknown_head_refuses_the_restore_and_writes_nothing(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    pushed = "e" * 40
    api.contents.files[pushed] = dict(HEAD)
    api.contents.head = pushed
    response = await _restore(app, project, version_id, expected_head=pushed)
    assert response.status_code == 409
    assert response.json()["code"] == "unknown_head"
    assert (await _preview(app, project, version_id)).status_code == 409
    assert api.trees == []


async def test_a_head_that_moved_since_the_preview_refuses_the_restore(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    response = await _restore(app, project, version_id, expected_head="f" * 40)
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The factorial branch moved since you opened the history. Open it again."
    )
    assert api.trees == []


async def test_a_target_file_that_is_not_text_refuses_the_restore_naming_it(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(async_session, old=OLD, head=HEAD)
    api.contents.binary.add(B)
    response = await _restore(app, project, version_id)
    assert response.status_code == 422, response.text
    assert response.json()["path"] == B
    assert f"is not text at {OLD_SHA[:7]}" in response.json()["detail"]
    assert api.trees == []


async def test_a_target_naming_a_runtime_that_no_longer_exists_refuses_before_any_write(
    async_session: AsyncSession,
) -> None:
    slug = await _runtime(async_session, RuntimeType.CLAUDE_API)
    app, api, project, version_id = await restorable(
        async_session, old={**OLD, PROCESS_PATH: lane(runtime=slug)}, head=HEAD
    )
    await async_session.execute(delete(Runtime).where(Runtime.slug == slug))
    await async_session.flush()
    response = await _restore(app, project, version_id)
    assert response.status_code == 422, response.text
    assert slug in response.json()["detail"]
    assert api.trees == []


async def test_a_version_in_the_root_process_file_layout_cannot_be_restored(
    async_session: AsyncSession,
) -> None:
    app, api, project, version_id = await restorable(
        async_session, old={".factorial/process.yaml": lane()}, head=HEAD
    )
    response = await _restore(app, project, version_id)
    assert response.status_code == 422, response.text
    assert "layout the editor no longer writes" in response.json()["detail"]
    assert api.trees == []
