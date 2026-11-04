"""Saving a project's GitHub connection, what reads back, and the activation gate."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from githubkit.exception import RequestFailed
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.deps import connection_client_dependency
from orchestrator.api.services.refs import ticket_source
from orchestrator.db.models import Event, Project, TicketSource, TicketSourceKind
from orchestrator.events.types import EventType
from orchestrator.github.client import AppCredentialError
from orchestrator.github.connection import (
    PRIVATE_KEY_SECRET,
    TOKEN_SECRET,
    WEBHOOK_SECRET,
    GitHubConnection,
    connection_for_project,
)
from orchestrator.secrets import store
from orchestrator.settings import AuthSettings
from tests.api_harness import build_app, client_for, make_settings, seed_project
from tests.api_probes import seed_probe_project

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.usefixtures("master_key"),
    pytest.mark.asyncio(loop_scope="session"),
]

KEY = "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"


class FakeClient:
    """Stands in for `GitHubClient`: the App is installed on `installed` and nowhere else."""

    def __init__(self, connection: GitHubConnection, installed: dict[str, int]) -> None:
        self.connection = connection
        self.installed = installed
        self.asked: list[str] = []

    async def describe_app(self) -> str:
        self.asked.append("app")
        return "factorial-app[bot]"

    async def describe_token_owner(self) -> str:
        self.asked.append("token")
        if self.connection.token and self.connection.token.get_secret_value() == "ghp_revoked":
            refusal = SimpleNamespace(status_code=401, raw_request=None, raw_response=None)
            raise RequestFailed(cast(Any, refusal))
        return "the-token-owner"

    async def installation_for(self, repo: str) -> int:
        owner = repo.partition("/")[0]
        if owner not in self.installed:
            raise AppCredentialError(f"the GitHub App 7 is not installed on {owner}")
        return self.installed[owner]


def _app(
    session: AsyncSession,
    installed: dict[str, int],
    *,
    clients: list[FakeClient] | None = None,
    **settings: Any,
) -> FastAPI:
    def make(connection: GitHubConnection) -> FakeClient:
        client = FakeClient(connection, installed)
        if clients is not None:
            clients.append(client)
        return client

    app = build_app(make_settings(**settings), session)
    app.dependency_overrides[connection_client_dependency] = lambda: make
    return app


APP_BODY = {"mode": "app", "app_id": "7", "private_key": KEY, "webhook_secret": "whsec"}


async def test_saving_an_app_connection_finds_installations_and_the_bot(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    async with client_for(_app(async_session, {owner: 99})) as client:
        saved = await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=APP_BODY
        )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["bot_login"] == "factorial-app[bot]"
    assert body["installations"] == {owner: 99}
    assert KEY not in saved.text and "whsec" not in saved.text
    connection = await connection_for_project(async_session, project)
    assert connection is not None
    assert connection.bot_login == "factorial-app[bot]"
    assert connection.installations == {owner: 99}
    assert connection.private_key is not None and connection.private_key.get_secret_value() == KEY


async def test_a_token_connection_takes_its_bot_login_from_the_token(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    async with client_for(_app(async_session, {})) as client:
        saved = await client.put(
            f"/api/projects/{project.id}/github-connection",
            headers=auth,
            json={"mode": "token", "token": "ghp_x"},
        )
        named = await client.put(
            f"/api/projects/{project.id}/github-connection",
            headers=auth,
            json={"mode": "token", "bot_login": "The-Token-Owner"},
        )
        wrong = await client.put(
            f"/api/projects/{project.id}/github-connection",
            headers=auth,
            json={"mode": "token", "bot_login": "chosen"},
        )
        shown = await client.get(
            f"/api/projects/{project.id}/github-connection", headers=auth
        )
    assert saved.status_code == 200, saved.text
    assert saved.json()["bot_login"] == "the-token-owner"
    assert named.json()["bot_login"] == "the-token-owner"
    assert wrong.status_code == 422
    assert "This token belongs to the-token-owner." in wrong.text
    assert shown.json()["bot_login"] == "the-token-owner"


async def test_an_app_save_asks_github_for_its_login_whatever_is_supplied(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    clients: list[FakeClient] = []
    async with client_for(_app(async_session, {owner: 99}, clients=clients)) as client:
        saved = await client.put(
            f"/api/projects/{project.id}/github-connection",
            headers=auth,
            json={**APP_BODY, "bot_login": "someone-else[bot]"},
        )
    assert saved.status_code == 200, saved.text
    assert saved.json()["bot_login"] == "factorial-app[bot]"
    assert [c.asked for c in clients] == [["app"]]


async def test_a_token_save_checks_the_token_even_with_a_stored_login(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    clients: list[FakeClient] = []
    url = f"/api/projects/{project.id}/github-connection"
    named = {"mode": "token", "bot_login": "the-token-owner"}
    async with client_for(_app(async_session, {}, clients=clients)) as client:
        await client.put(url, headers=auth, json={**named, "token": "ghp_x"})
        again = await client.put(url, headers=auth, json=named)
        revoked = await client.put(url, headers=auth, json={**named, "token": "ghp_revoked"})
    assert again.status_code == 200, again.text
    assert again.json()["bot_login"] == "the-token-owner"
    assert [c.asked for c in clients] == [["token"]] * 3
    assert revoked.status_code == 422
    assert "refused" in revoked.text


async def test_an_owner_without_the_app_is_refused_by_name(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    other = await seed_project(async_session, "elsewhere/repo")
    source = TicketSource(
        project_id=other.id, kind=TicketSourceKind.GITHUB, external_ref="beyond/repo"
    )
    async_session.add(source)
    await async_session.flush()
    owner = project.github_repo.partition("/")[0]
    async with client_for(_app(async_session, {owner: 1})) as client:
        refused = await client.put(
            f"/api/projects/{other.id}/github-connection", headers=auth, json=APP_BODY
        )
    assert refused.status_code == 422
    assert "elsewhere" in refused.text
    assert await connection_for_project(async_session, other) is None
    assert other.settings.get("github") is None


async def test_a_blank_key_keeps_the_stored_one(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    async with client_for(_app(async_session, {owner: 99})) as client:
        url = f"/api/projects/{project.id}/github-connection"
        await client.put(url, headers=auth, json=APP_BODY)
        again = await client.put(
            url, headers=auth, json={"mode": "app", "private_key": "", "webhook_secret": ""}
        )
    assert again.status_code == 200, again.text
    assert again.json()["app_id"] == "7"
    key = await store.get(async_session, project.id, PRIVATE_KEY_SECRET)
    assert key is not None and key.get_secret_value() == KEY
    webhook = await store.get(async_session, project.id, WEBHOOK_SECRET)
    assert webhook is not None and webhook.get_secret_value() == "whsec"


async def test_a_reader_sees_only_whether_secrets_are_set(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    async with client_for(_app(async_session, {owner: 99})) as client:
        url = f"/api/projects/{project.id}/github-connection"
        await client.put(url, headers=auth, json=APP_BODY)
        read = await client.get(url, headers=auth)
    assert read.json() | {"webhook_url": ""} == {
        "mode": "app",
        "app_id": "7",
        "bot_login": "factorial-app[bot]",
        "installations": {owner: 99},
        "private_key_set": True,
        "webhook_secret_set": True,
        "token_set": False,
        "webhook_url": "",
    }
    assert KEY not in read.text and "whsec" not in read.text


async def test_the_secrets_route_refuses_connection_names(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    async with client_for(_app(async_session, {})) as client:
        for name in (PRIVATE_KEY_SECRET, WEBHOOK_SECRET, TOKEN_SECRET):
            put = await client.put(
                f"/api/projects/{project.id}/secrets/{name}", headers=auth, json={"value": "x"}
            )
            assert put.status_code == 409
            assert "Ticket source" in put.text
            gone = await client.delete(f"/api/projects/{project.id}/secrets/{name}", headers=auth)
            assert gone.status_code == 409
    assert await store.list_names(async_session, project.id) == []


async def test_patching_settings_keeps_the_connection(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    async with client_for(_app(async_session, {})) as client:
        await client.put(
            f"/api/projects/{project.id}/github-connection",
            headers=auth,
            json={"mode": "token", "token": "ghp_x"},
        )
        patched = await client.patch(
            f"/api/projects/{project.id}",
            headers=auth,
            json={"settings": {"egress_allowlist": [], "github": {"mode": "app", "app_id": "1"}}},
        )
    assert patched.status_code == 200, patched.text
    assert patched.json()["settings"]["github"]["mode"] == "token"
    assert patched.json()["settings"]["egress_allowlist"] == []
    assert await connection_for_project(async_session, project) is not None


async def test_an_unconnected_github_project_cannot_be_activated(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    async with client_for(_app(async_session, {owner: 99})) as client:
        active = f"/api/projects/{project.id}/active"
        refused = await client.put(active, headers=auth, json={"active": True})
        assert refused.status_code == 409
        assert "GitHub connection" in refused.json()["detail"]
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        rows = {row["key"]: row for row in detail["readiness"]}
        assert rows["github_connection"]["state"] == "blocked"
        assert "github_webhook" not in rows

        await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=APP_BODY
        )
        allowed = await client.put(active, headers=auth, json={"active": True})
    assert allowed.status_code == 200, allowed.text


async def test_the_webhook_url_follows_the_mode(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    owner = project.github_repo.partition("/")[0]
    settings: dict[str, Any] = {"auth": AuthSettings(external_url="https://factorial.example")}
    async with client_for(_app(async_session, {owner: 1}, **settings)) as client:
        url = f"/api/projects/{project.id}/github-connection"
        assert (await client.get(url, headers=auth)).json()["webhook_url"] == ""
        token = await client.put(url, headers=auth, json={"mode": "token", "token": "t"})
        assert token.json()["webhook_url"] == (
            f"https://factorial.example/factorial/webhooks/github/projects/{project.id}"
        )
        app = await client.put(url, headers=auth, json=APP_BODY | {"app_id": "12"})
    assert app.json()["webhook_url"] == (
        "https://factorial.example/factorial/webhooks/github/apps/12"
    )


async def test_a_failed_webhook_newer_than_the_last_verified_one_warns(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, connected=False)
    owner = project.github_repo.partition("/")[0]
    row = await ticket_source(async_session, project.id)
    assert row is not None
    now = datetime.now(UTC)
    async with client_for(_app(async_session, {owner: 99})) as client:
        await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=APP_BODY
        )

        async def webhook() -> dict[str, object]:
            detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
            return {r["key"]: r for r in detail["readiness"]}["github_webhook"]

        assert (await webhook())["state"] == "unknown"
        row.webhook_verified_at = now - timedelta(hours=2)
        await async_session.flush()
        assert (await webhook())["state"] == "ok"
        row.webhook_failed_at = now
        await async_session.flush()
        warned = await webhook()
        assert warned["state"] == "warn"
        assert "did not match" in str(warned["summary"])
        row.webhook_verified_at = now + timedelta(minutes=1)
        await async_session.flush()
        assert (await webhook())["state"] == "ok"


async def test_a_refused_body_does_not_echo_its_secrets(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    body = {"private_key": KEY, "token": "ghp_secret", "webhook_secret": "whsec-secret"}
    async with client_for(_app(async_session, {})) as client:
        refused = await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=body
        )
        bad_secret = await client.put(
            f"/api/projects/{project.id}/secrets/X", headers=auth, json={"value": ["ghp_secret"]}
        )
    assert refused.status_code == 422
    for leaked in ("fake", "ghp_secret", "whsec-secret"):
        assert leaked not in refused.text and leaked not in bad_secret.text


async def test_the_ticket_sources_owner_needs_the_app_too(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session, "mine/repo")
    async_session.add(
        TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref="theirs/issues"
        )
    )
    await async_session.flush()
    async with client_for(_app(async_session, {"mine": 1})) as client:
        refused = await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=APP_BODY
        )
    assert refused.status_code == 422
    assert "theirs" in refused.text


async def test_both_owners_are_resolved_when_the_app_is_on_both(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session, "mine/repo")
    async_session.add(
        TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref="theirs/issues"
        )
    )
    await async_session.flush()
    async with client_for(_app(async_session, {"mine": 1, "theirs": 2})) as client:
        saved = await client.put(
            f"/api/projects/{project.id}/github-connection", headers=auth, json=APP_BODY
        )
    assert saved.json()["installations"] == {"mine": 1, "theirs": 2}


class HeldClient(FakeClient):
    """GitHub answering only once `answer` is set; `asked` is set when the App is first asked."""

    def __init__(
        self,
        connection: GitHubConnection,
        installed: dict[str, int],
        asked: asyncio.Event,
        answer: asyncio.Event,
    ) -> None:
        super().__init__(connection, installed)
        self.asked_event = asked
        self.answer = answer

    async def describe_app(self) -> str:
        self.asked_event.set()
        await self.answer.wait()
        return await super().describe_app()


Edit = Callable[[AsyncClient, Project], Awaitable[Response]]


async def _save_around(
    maker: async_sessionmaker[AsyncSession],
    auth: dict[str, str],
    edit: Edit,
    *,
    body: dict[str, str] = APP_BODY,
    connected: bool = False,
) -> tuple[Project, Response, Response]:
    """A save whose GitHub lookup is held while `edit` runs in its own request and transaction,
    which must finish meanwhile; then GitHub answers and the save completes. `connected` starts
    the project on App 7 with `KEY` stored."""
    async with maker() as session:
        project = await seed_project(session)
        project.settings = {"kept": "before"}
        session.add(
            TicketSource(
                project_id=project.id,
                kind=TicketSourceKind.GITHUB,
                external_ref=project.github_repo,
            )
        )
        if connected:
            owner = project.github_repo.partition("/")[0]
            project.settings = {
                "kept": "before",
                "github": {
                    "mode": "app",
                    "app_id": "7",
                    "bot_login": "factorial-app[bot]",
                    "installations": {owner: 99},
                },
            }
            await session.flush()
            await store.put(session, project.id, PRIVATE_KEY_SECRET, KEY)
        await session.commit()
    owner = project.github_repo.partition("/")[0]
    asked, answer = asyncio.Event(), asyncio.Event()
    saving_session, editing_session = maker(), maker()
    saving = build_app(make_settings(), saving_session)
    saving.dependency_overrides[connection_client_dependency] = lambda: (
        lambda connection: HeldClient(connection, {owner: 99}, asked, answer)
    )
    editing = build_app(make_settings(), editing_session)
    put: asyncio.Task[Response] | None = None
    try:
        async with client_for(saving) as saver, client_for(editing) as editor:
            put = asyncio.create_task(
                saver.put(f"/api/projects/{project.id}/github-connection", headers=auth, json=body)
            )
            await asyncio.wait_for(asked.wait(), timeout=5)
            edited = await asyncio.wait_for(edit(editor, project), timeout=5)
            await editing_session.commit()
            answer.set()
            saved = await put
            await saving_session.commit()
    finally:
        answer.set()
        if put is not None and not put.done():
            with contextlib.suppress(Exception):
                await put
        await saving_session.close()
        await editing_session.close()
    return project, edited, saved


async def test_a_save_asks_github_without_holding_the_project_and_keeps_an_edit_made_meanwhile(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    async def patch(editor: AsyncClient, project: Project) -> Response:
        return await editor.patch(
            f"/api/projects/{project.id}", headers=auth, json={"settings": {"kept": "after"}}
        )

    project, patched, saved = await _save_around(maker, auth, patch)

    assert patched.status_code == 200, patched.text
    assert saved.status_code == 200, saved.text
    async with maker() as session:
        settings = (await session.get_one(Project, project.id)).settings
    assert settings["kept"] == "after"
    assert settings["github"]["bot_login"] == "factorial-app[bot]"


@pytest.mark.parametrize("moved", ["github_repo", "intake"])
async def test_a_save_whose_repositories_moved_meanwhile_is_refused(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession], moved: str
) -> None:
    """The installations it looked up are for the repositories it read; it stores none for
    repositories it never looked at."""

    async def move(editor: AsyncClient, project: Project) -> Response:
        if moved == "github_repo":
            return await editor.patch(
                f"/api/projects/{project.id}",
                headers=auth,
                json={"github_repo": f"elsewhere/{uuid4().hex[:8]}"},
            )
        return await editor.put(
            f"/api/projects/{project.id}/ticket-source",
            headers=auth,
            json={"kind": "github", "external_ref": f"elsewhere/{uuid4().hex[:8]}"},
        )

    project, edited, saved = await _save_around(maker, auth, move)

    assert edited.status_code == 200, edited.text
    assert saved.status_code == 409, saved.text
    assert saved.json()["detail"] == "The project changed while saving. Save again."
    async with maker() as session:
        stored = await session.get_one(Project, project.id)
        assert "github" not in stored.settings
        assert await store.list_names(session, project.id) == []


async def test_a_save_whose_connection_another_save_changed_meanwhile_is_refused(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    """Its App id and key were checked together against GitHub; another save's App id must not
    end up beside this save's key."""
    other = {"mode": "app", "app_id": "8", "bot_login": "other[bot]", "installations": {}}

    async def resave(_editor: AsyncClient, project: Project) -> Response:
        async with maker() as session:
            stored = await session.get_one(Project, project.id)
            stored.settings = {**stored.settings, "github": other}
            await session.commit()
        return Response(200)

    project, _, saved = await _save_around(maker, auth, resave)

    assert saved.status_code == 409, saved.text
    assert saved.json()["detail"] == "The project changed while saving. Save again."
    async with maker() as session:
        assert (await session.get_one(Project, project.id)).settings["github"] == other
        assert await store.list_names(session, project.id) == []


async def test_a_save_that_keeps_a_key_rotated_meanwhile_is_refused(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    """A blank key keeps the stored one: the one checked against GitHub is the one it read."""

    async def rotate(_editor: AsyncClient, project: Project) -> Response:
        async with maker() as session:
            await store.put(session, project.id, PRIVATE_KEY_SECRET, "rotated")
            await session.commit()
        return Response(200)

    project, _, saved = await _save_around(
        maker, auth, rotate, body={"mode": "app", "webhook_secret": "whsec"}, connected=True
    )

    assert saved.status_code == 409, saved.text
    async with maker() as session:
        assert await store.list_names(session, project.id) == [PRIVATE_KEY_SECRET]
        assert (await store.get(session, project.id, PRIVATE_KEY_SECRET)) == SecretStr("rotated")


async def test_a_ticket_source_save_and_a_verified_webhook_do_not_deadlock(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession]
) -> None:
    """The webhook stamps the source's row, then inserts an event naming the project; the save
    locks the project, then writes the source's row. Each waits on the other's first lock unless
    the project lock lets a foreign key through."""
    async with maker() as session:
        project = await seed_project(session)
        session.add(
            TicketSource(
                project_id=project.id,
                kind=TicketSourceKind.GITHUB,
                external_ref=project.github_repo,
            )
        )
        await session.commit()
    webhook, saving, watcher = maker(), maker(), maker()
    app = build_app(make_settings(), saving)
    put: asyncio.Task[Response] | None = None
    try:
        await webhook.execute(
            update(TicketSource)
            .where(TicketSource.project_id == project.id)
            .values(webhook_verified_at=datetime.now(UTC))
        )
        async with client_for(app) as client:
            put = asyncio.create_task(
                client.put(
                    f"/api/projects/{project.id}/ticket-source",
                    headers=auth,
                    json={"kind": "github", "external_ref": f"{project.github_repo}-intake"},
                )
            )
            for _ in range(250):
                waiting = await watcher.scalar(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                )
                await watcher.rollback()
                if waiting:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("the ticket source save never waited on the webhook")
            webhook.add(Event(project_id=project.id, type=EventType.WEBHOOK_RECEIVED, details={}))
            await asyncio.wait_for(webhook.flush(), timeout=5)
            await webhook.commit()
            saved = await asyncio.wait_for(put, timeout=5)
            await saving.commit()
    finally:
        if put is not None and not put.done():
            put.cancel()
            with contextlib.suppress(BaseException):
                await put
        for session in (webhook, saving, watcher):
            await session.close()
    assert saved.status_code == 200, saved.text
