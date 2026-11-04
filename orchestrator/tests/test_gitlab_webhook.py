"""The GitLab webhook: a wake-up signal on the same terms as the GitHub routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    Event,
    Organization,
    Project,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.gitlab.project_client import TOKEN_SECRET, WEBHOOK_SECRET_NAME
from orchestrator.secrets import store
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("master_key"),
]

URL = "/webhooks/gitlab"


async def _gitlab_project(
    session: AsyncSession, repo: str, secret: str, organization_id: int | None = None
) -> Project:
    project = await seed_project(session, repo, organization_id=organization_id)
    project.settings = {"gitlab": {"base_url": "https://gitlab.invalid"}}
    session.add(
        TicketSource(project_id=project.id, kind=TicketSourceKind.GITLAB, external_ref=repo)
    )
    await session.flush()
    await store.put(session, project.id, WEBHOOK_SECRET_NAME, secret)
    await store.put(session, project.id, TOKEN_SECRET, f"glpat-{secret}")
    return project


async def _events(session: AsyncSession, project: Project) -> int:
    query = select(func.count()).select_from(Event).where(Event.project_id == project.id)
    return int((await session.execute(query)).scalar_one())


async def test_a_gitlab_delivery_wakes_only_the_projects_whose_secret_it_carries(
    async_session: AsyncSession,
) -> None:
    """Organizations may share a repo; one owner's secret must never verify for the other."""
    repo = f"group/shared-{uuid4().hex[:8]}"
    other = Organization(slug=f"org-{uuid4().hex[:12]}", name="Other", updated_by="test")
    async_session.add(other)
    await async_session.flush()
    ours = await _gitlab_project(async_session, repo, "ours-secret")
    theirs = await _gitlab_project(async_session, repo, "theirs-secret", other.id)
    body = json.dumps({"project": {"path_with_namespace": repo}}).encode()

    async with client_for(build_app(make_settings(), async_session)) as client:
        wrong = await client.post(URL, content=body, headers={"X-Gitlab-Token": "guess"})
        theirs_only = await client.post(
            URL, content=body, headers={"X-Gitlab-Token": "theirs-secret"}
        )

    assert (wrong.status_code, theirs_only.status_code) == (401, 202)
    assert (await _events(async_session, ours), await _events(async_session, theirs)) == (0, 1)


async def test_a_gitlab_issue_delivery_wakes_its_ticket_not_the_fleet(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet wake leases a long-exited ticket only every ten minutes, so removing
    `factorial:exit` on GitLab waited that long for the re-entry it asks for."""
    from orchestrator.api.routes import webhooks

    woken: list[object] = []

    async def ticket_wake(session: AsyncSession, ticket_id: int) -> None:
        woken.append(("ticket", ticket_id))

    async def fleet_wake(session: AsyncSession, **_: object) -> None:
        woken.append(("fleet",))

    monkeypatch.setattr(webhooks, "wake_ticket", ticket_wake)
    monkeypatch.setattr(webhooks, "wake_fleet", fleet_wake)
    repo = f"group/exited-{uuid4().hex[:8]}"
    project = await _gitlab_project(async_session, repo, "the-secret")
    ticket = await seed_ticket(
        async_session, project, status=TicketStatus.EXITED, external_key="7"
    )
    issue = {
        "object_kind": "issue",
        "project": {"path_with_namespace": repo},
        "object_attributes": {"iid": 7, "id": 99001},
    }
    push = {"object_kind": "push", "project": {"path_with_namespace": repo}}
    headers = {"X-Gitlab-Token": "the-secret"}

    async with client_for(build_app(make_settings(), async_session)) as client:
        labelled = await client.post(URL, content=json.dumps(issue).encode(), headers=headers)
        pushed = await client.post(URL, content=json.dumps(push).encode(), headers=headers)

    assert (labelled.status_code, pushed.status_code) == (202, 202)
    assert woken == [("ticket", ticket.id), ("fleet",)]


async def test_a_gitlab_delivery_about_a_new_issue_asks_admission_about_it(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet wake waits out the pass gap, so a new issue waited up to 30 s (#476)."""
    from orchestrator.api.routes import webhooks

    woken: list[object] = []

    async def fleet_wake(session: AsyncSession, **_: object) -> None:
        woken.append(("fleet",))

    async def admission_wake(session: AsyncSession, *, project_id: int, issue: str) -> None:
        woken.append(("admit", project_id, issue))

    monkeypatch.setattr(webhooks, "wake_fleet", fleet_wake)
    monkeypatch.setattr(webhooks, "wake_admission", admission_wake)
    repo = f"group/new-{uuid4().hex[:8]}"
    project = await _gitlab_project(async_session, repo, "the-secret")
    note = {
        "object_kind": "note",
        "project": {"path_with_namespace": repo},
        "object_attributes": {"noteable_type": "Issue"},
        "issue": {"iid": 8},
    }

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            URL, content=json.dumps(note).encode(), headers={"X-Gitlab-Token": "the-secret"}
        )

    assert response.status_code == 202
    assert woken == [("admit", project.id, "8")]


async def test_a_gitlab_delivery_about_a_closed_issue_does_not_ask_admission(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission reads open issues only, so a close can never admit; it wakes the fleet as
    before instead of costing a read."""
    from orchestrator.api.routes import webhooks

    woken: list[object] = []

    async def fleet_wake(session: AsyncSession, **_: object) -> None:
        woken.append(("fleet",))

    async def admission_wake(session: AsyncSession, *, project_id: int, issue: str) -> None:
        woken.append(("admit", project_id, issue))

    monkeypatch.setattr(webhooks, "wake_fleet", fleet_wake)
    monkeypatch.setattr(webhooks, "wake_admission", admission_wake)
    repo = f"group/closed-{uuid4().hex[:8]}"
    await _gitlab_project(async_session, repo, "the-secret")
    issue = {
        "object_kind": "issue",
        "project": {"path_with_namespace": repo},
        "object_attributes": {"iid": 9, "action": "close"},
    }

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            URL, content=json.dumps(issue).encode(), headers={"X-Gitlab-Token": "the-secret"}
        )

    assert response.status_code == 202
    assert woken == [("fleet",)]


async def test_an_oversized_gitlab_delivery_is_refused_as_it_streams(
    async_session: AsyncSession,
) -> None:
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(64):
            sent += 1
            yield b"x" * (1024 * 1024)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(URL, content=chunks(), headers={"X-Gitlab-Token": "t"})

    assert response.status_code == 413
    assert sent < 64
