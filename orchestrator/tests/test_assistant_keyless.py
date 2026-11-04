"""The assistant's process holds no master key (design.md §7.18): in the image it refuses to
start with one, on a laptop it blanks the one `.env` hands it, and no turn or chat tool it runs
decrypts a stored secret. A GitLab project's chat reads still work: they go through the API's
GitLab read proxy, which opens the token in the API's process."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount

from orchestrator.api.deps import get_db_session, gitlab_proxy_http_dependency
from orchestrator.api.routes import assistant as assistant_routes
from orchestrator.assistant.__main__ import drop_master_key
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.github_token import (
    GITLAB_PROXY_PATH,
    NO_GITLAB,
    OrganizationTokens,
    ReadEntry,
    tool_deps,
)
from orchestrator.assistant.items import append_item
from orchestrator.assistant.runner import RunnerDeps, run_turn
from orchestrator.assistant.toolbox import registry
from orchestrator.assistant.turns import claim_turn, enqueue_turn
from orchestrator.db.models import (
    ChatItemKind,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    Project,
    TicketSource,
    TicketSourceKind,
)
from orchestrator.events import EventType, append
from orchestrator.factorial import CONFIG_BRANCH
from orchestrator.gitlab.project_client import TOKEN_SECRET as GITLAB_TOKEN
from orchestrator.secrets import crypto, store
from orchestrator.secrets.crypto import SecretsConfigurationError, load_keyring
from orchestrator.settings import APP_PREFIX, GitLabSettings, get_settings
from runtime.assistant.contract import ChatToolbox, TurnDone, TurnStop
from tests.api_harness import (
    CHAT_OWNER,
    TOKEN,
    build_app,
    make_settings,
    mcp_settings,
    seed_connected,
    seed_ticket,
)
from tests.api_probes import MINI_PROCESS_YAML
from tests.assistant_fakes import (
    FakeBackend,
    FakeTokenFetch,
    enable_assistant,
    seed_conversation,
    sessions_of,
    wipe_chat,
)

KEY = base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Settings and the keyring read again from the environment the test sets, and again after."""
    get_settings.cache_clear()
    load_keyring.cache_clear()
    yield monkeypatch
    monkeypatch.undo()
    get_settings.cache_clear()
    load_keyring.cache_clear()


def test_in_the_image_a_master_key_stops_the_assistant(
    fresh_settings: pytest.MonkeyPatch,
) -> None:
    fresh_settings.setenv("FACTORIAL_MASTER_KEY", KEY)
    fresh_settings.setenv("FACTORIAL_ASSISTANT__OWN_USER", "true")

    with pytest.raises(SystemExit, match="FACTORIAL_MASTER_KEY"):
        drop_master_key()


def test_on_a_laptop_the_assistant_blanks_the_master_key_it_was_handed(
    fresh_settings: pytest.MonkeyPatch,
) -> None:
    """`make dev` runs the assistant as the developer, from the `.env` the API reads."""
    fresh_settings.setenv("FACTORIAL_MASTER_KEY", KEY)
    load_keyring()

    drop_master_key()

    assert get_settings().master_key.get_secret_value() == ""
    with pytest.raises(SecretsConfigurationError):
        load_keyring()


def test_an_assistant_without_a_master_key_starts(fresh_settings: pytest.MonkeyPatch) -> None:
    fresh_settings.setenv("FACTORIAL_MASTER_KEY", "")
    fresh_settings.setenv("FACTORIAL_ASSISTANT__OWN_USER", "true")

    drop_master_key()

    assert get_settings().master_key.get_secret_value() == ""


@dataclass(frozen=True)
class _Seeded:
    conversation_id: int
    #: project name -> a ticket ref in it, and an issue no ticket holds yet, which reads the forge.
    projects: dict[str, tuple[str, str]]
    gitlab: str
    gitlab_id: int
    gitlab_repo: str


@pytest.fixture
async def seeded(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> AsyncIterator[_Seeded]:
    """A secret of every kind the assistant's tools could reach for: an App project's key, a
    token project's token, a GitLab project's token, and the assistant's own credential."""
    await wipe_chat(maker)
    await enable_assistant(maker)
    async with maker() as session:
        app = await seed_connected(session, app_id="7")
        token = await seed_connected(session)
        gitlab = Project(
            organization_id=app.organization_id,
            name=f"gl-{app.id}",
            github_repo=f"group/gl-{app.id}",
            settings={"gitlab": {"base_url": "https://gitlab.invalid"}},
        )
        session.add(gitlab)
        await session.flush()
        session.add(
            TicketSource(
                project_id=gitlab.id, kind=TicketSourceKind.GITLAB, external_ref=gitlab.github_repo
            )
        )
        await store.put(session, gitlab.id, GITLAB_TOKEN, "glpat-stored")
        projects: dict[str, tuple[str, str]] = {}
        for project in (app, token, gitlab):
            ticket = await seed_ticket(session, project, external_key="5")
            projects[project.name] = (f"id:{ticket.id}", f"{project.github_repo}#99")
        conversation = await seed_conversation(session, owner=CHAT_OWNER)
        await session.commit()
    yield _Seeded(conversation.id, projects, gitlab.name, gitlab.id, gitlab.github_repo)
    await wipe_chat(maker)


@pytest.fixture
def decrypted(seeded: _Seeded, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """From here the process holds no master key, and any decrypt is recorded, then refused."""
    calls: list[str] = []

    def refuse(*_: Any, aad: bytes | None = None, **__: Any) -> SecretStr:
        calls.append((aad or b"").decode())
        raise AssertionError("the assistant decrypted a stored secret")

    monkeypatch.setenv("FACTORIAL_MASTER_KEY", "")
    get_settings.cache_clear()
    load_keyring.cache_clear()
    monkeypatch.setattr(store, "decrypt", refuse)
    monkeypatch.setattr(crypto, "decrypt", refuse)
    return calls


def _calls(seeded: _Seeded) -> list[tuple[str, dict[str, Any]]]:
    """Every chat tool, on every project: the tests below fail when a tool is added unlisted."""
    calls: list[tuple[str, dict[str, Any]]] = [
        ("factorial_projects_list", {}),
        ("factorial_vocabulary_get", {}),
        ("factorial_metrics_get", {}),
        ("factorial_runs_list", {}),
        ("factorial_tickets_list", {}),
        ("factorial_reference_read", {"name": "process-yaml"}),
        ("factorial_ui_show", {"path": "/overview"}),
        (
            "factorial_chart",
            {
                "kind": "line",
                "title": "t",
                "unit": "USD",
                "x": ["a"],
                "series": [{"name": "s", "values": [1.0]}],
                "source": "factorial_metrics_get",
            },
        ),
    ]
    content = "specVersion: factorial/v1\nkind: Process\nname: p\n"
    for project, (ticket, pending) in seeded.projects.items():
        on_project = {"project": project}
        on_ticket = {"ticket": ticket}
        calls += [
            ("factorial_ticket_get", on_project | {"ticket": pending}),
            ("factorial_project_get", on_project | {"include_pending": True}),
            ("factorial_dashboard_get", on_project),
            ("factorial_costs_get", on_project),
            ("factorial_config_read", on_project),
            ("factorial_config_validate", on_project | {"content": content}),
            ("factorial_editor_open", on_project | {"content": content}),
            ("factorial_config_commit", on_project | {"content": content}),
            ("factorial_ticket_create", on_project | {"title": "t", "assignee": "operator"}),
            ("factorial_ticket_get", on_ticket | {"include_timeline": True}),
            ("factorial_ticket_exit", on_ticket),
            ("factorial_ticket_interrupt", on_ticket),
            ("factorial_ticket_reenter", on_ticket),
            ("factorial_ticket_requeue", on_ticket),
            ("factorial_ticket_restart", on_ticket),
            ("factorial_ticket_resume", on_ticket),
            ("factorial_ticket_retry", on_ticket),
            ("factorial_ticket_unblock", on_ticket),
        ]
    return calls


async def test_a_turn_and_every_chat_tool_run_without_a_decrypt(
    maker: async_sessionmaker[AsyncSession], seeded: _Seeded, decrypted: list[str]
) -> None:
    settings = mcp_settings()
    calls = _calls(seeded)
    assert {name for name, _ in calls} == set(registry())
    answers: list[str] = []
    backend = FakeBackend()

    async def call_every_tool() -> None:
        tools: ChatToolbox = backend.toolboxes[-1]
        for name, arguments in calls:
            outcome = await tools.call(name, arguments)
            answers.append(f"{name} {arguments.get('project')}: {outcome.text[:200]}")

    backend.script = [call_every_tool, TurnDone(stop=TurnStop.END)]

    async def from_api(_org: str, _assistant: AssistantSettings) -> SecretStr | None:
        return SecretStr("sk-ant-from-api")

    deps = RunnerDeps(
        sessions=sessions_of(maker),
        settings=lambda: settings,
        backend_for=lambda _kind: backend,
        clock=lambda: datetime.now(UTC),
        tools=lambda cache: tool_deps(lambda: settings, sessions_of(maker), cache),
        github_tokens=OrganizationTokens(lambda _org: FakeTokenFetch()),
        credential=from_api,
    )
    async with maker() as session:
        turn = await enqueue_turn(session, seeded.conversation_id, trigger=ChatTurnTrigger.USER)
        assert turn is not None
        await append_item(
            session,
            conversation_id=seeded.conversation_id,
            turn_id=turn.id,
            kind=ChatItemKind.USER_TEXT,
            payload={"text": "everything", "page": None},
        )
        claimed = await claim_turn(
            session, owner="host:1", lease=timedelta(seconds=60), now=datetime.now(UTC)
        )
        await session.commit()
    assert claimed is not None

    await run_turn(claimed.id, deps, owner="host:1")

    async with maker() as session:
        ended = await session.get_one(ChatTurn, claimed.id)
    assert ended.status == ChatTurnStatus.DONE, ended.failure_message
    assert len(answers) == len(calls)
    assert decrypted == [], "\n".join(answers)
    assert f"factorial_config_read {seeded.gitlab}: {NO_GITLAB}" in answers


PENDING_TITLE = "An issue on GitLab"
HEAD = "a" * 40
PROCESS_PATH = ".factorial/processes/process-x.yaml"
AGENTS = ".factorial/agents"
PROCESS_YAML = MINI_PROCESS_YAML.replace("name: mini", "name: x")
DRAFT = PROCESS_YAML.replace("version: 1", "version: 2")


def _gitlab_answer(seen: list[httpx.Request]) -> httpx.MockTransport:
    """GitLab, behind the API's proxy: enough of its API for the three reads."""
    issue = {
        "iid": 99,
        "title": PENDING_TITLE,
        "description": "",
        "state": "opened",
        "labels": [],
        "assignees": [],
    }

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.raw_path.decode().split("?")[0]
        # /api/v4/projects/<repo>/<tail>
        parts = path.split("/", 5)
        tail = parts[5] if len(parts) > 5 else ""
        if tail == "":
            body: object = {"default_branch": "main"}
        elif tail.startswith("repository/commits/") and not tail.endswith("/statuses"):
            body = {"id": HEAD, "parent_ids": []}
        elif tail.startswith("repository/files/") and unquote(tail[17:]) != PROCESS_PATH:
            return httpx.Response(404, json={"message": "404 File Not Found"})
        elif tail.startswith(("repository/files/", "repository/blobs/")):
            body = {
                "content": base64.b64encode(PROCESS_YAML.encode()).decode(),
                "encoding": "base64",
            }
        elif tail == "repository/tree" and request.url.params.get("path") == ".factorial":
            body = [
                {"name": "process-x.yaml", "path": PROCESS_PATH, "type": "blob", "id": "b" * 40}
            ]
        elif tail == "repository/tree" and request.url.params.get("path") == AGENTS:
            # The draft's agent is already there, so the commit writes the process alone.
            body = [{"name": "mini-agent", "path": f"{AGENTS}/mini-agent", "type": "tree"}]
        elif tail == "issues/99":
            body = issue
        elif tail == "issues":
            body = [issue]
        elif tail.endswith("/approvals"):
            body = {"approved_by": []}
        else:
            body = []
        return httpx.Response(200, json=body)

    return httpx.MockTransport(answer)


async def test_a_gitlab_project_s_chat_reads_go_through_the_api_s_proxy(
    maker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    decrypted: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process decrypts nothing and holds no GitLab token: it calls the real proxy route
    with the API token, and the API adds the stored token there. The API's own opening of that
    token is stood in, since every decrypt in this test process is refused."""
    settings = mcp_settings()
    api_url = settings.assistant.api_url.rstrip("/")
    upstream: list[httpx.Request] = []
    to_api: list[httpx.Request] = []

    async def opened_by_the_api(_: AsyncSession, project: Any) -> GitLabSettings | None:
        if project.id != seeded.gitlab_id:
            return None
        return GitLabSettings(
            base_url="https://gitlab.invalid",
            token=SecretStr("glpat-stored"),
            bot_login="factorial-bot",
        )

    monkeypatch.setattr(assistant_routes, "settings_for_project", opened_by_the_api)
    async with maker() as api_session:
        api = build_app(make_settings(), api_session)

        async def session_per_request() -> AsyncIterator[AsyncSession]:
            # A config check reads GitLab concurrently, so its requests overlap.
            async with maker() as session:
                yield session

        api.dependency_overrides[get_db_session] = session_per_request
        gitlab_transport = _gitlab_answer(upstream)
        api.dependency_overrides[gitlab_proxy_http_dependency] = lambda: (
            lambda gitlab: httpx.AsyncClient(base_url=gitlab.api_url, transport=gitlab_transport)
        )
        served = httpx.ASGITransport(app=Starlette(routes=[Mount(APP_PREFIX, app=api)]))

        async def handle(request: httpx.Request) -> httpx.Response:
            to_api.append(request)
            return await served.handle_async_request(request)

        class Recorded(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return await handle(request)

        async def entries() -> list[ReadEntry]:
            return [
                ReadEntry(
                    repos=frozenset({seeded.gitlab_repo}),
                    token=SecretStr(""),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    base_url=api_url + GITLAB_PROXY_PATH.format(project_id=seeded.gitlab_id),
                    forge="gitlab",
                    project_id=seeded.gitlab_id,
                    bot_login="factorial-bot",
                )
            ]

        _, pending = seeded.projects[seeded.gitlab]
        on_project = {"project": seeded.gitlab}
        calls: list[tuple[str, dict[str, Any]]] = [
            ("factorial_config_read", on_project | {"path": PROCESS_PATH}),
            ("factorial_project_get", on_project | {"include_pending": True}),
            ("factorial_ticket_get", on_project | {"ticket": pending}),
            ("factorial_config_validate", on_project | {"content": DRAFT}),
            ("factorial_editor_open", on_project | {"content": DRAFT}),
            ("factorial_config_commit", on_project | {"content": DRAFT}),
        ]
        answers: dict[str, str] = {}
        backend = FakeBackend()

        async def call_the_reads() -> None:
            tools: ChatToolbox = backend.toolboxes[-1]
            for name, arguments in calls:
                answers[name] = (await tools.call(name, arguments)).text

        backend.script = [call_the_reads, TurnDone(stop=TurnStop.END)]

        async def from_api(_org: str, _assistant: AssistantSettings) -> SecretStr | None:
            return SecretStr("sk-ant-from-api")

        deps = RunnerDeps(
            sessions=sessions_of(maker),
            settings=lambda: settings,
            backend_for=lambda _kind: backend,
            clock=lambda: datetime.now(UTC),
            tools=lambda cache: tool_deps(
                lambda: settings, sessions_of(maker), cache, transport=Recorded()
            ),
            github_tokens=OrganizationTokens(lambda _org: entries),
            credential=from_api,
        )
        async with maker() as session:
            # The head GitLab answers is the one Factorial seeded, so the read trusts it.
            await append(
                session,
                type=EventType.CONFIG_BRANCH_CREATED,
                project_id=seeded.gitlab_id,
                branch=CONFIG_BRANCH,
                sha=HEAD,
            )
            turn = await enqueue_turn(session, seeded.conversation_id, trigger=ChatTurnTrigger.USER)
            assert turn is not None
            await append_item(
                session,
                conversation_id=seeded.conversation_id,
                turn_id=turn.id,
                kind=ChatItemKind.USER_TEXT,
                payload={"text": "read GitLab", "page": None},
            )
            claimed = await claim_turn(
                session, owner="host:1", lease=timedelta(seconds=60), now=datetime.now(UTC)
            )
            await session.commit()
        assert claimed is not None

        await run_turn(claimed.id, deps, owner="host:1")

    assert decrypted == [], json.dumps(answers, indent=1)
    assert NO_GITLAB not in "".join(answers.values()), json.dumps(answers, indent=1)
    assert "specVersion: factorial/v1" in answers["factorial_config_read"], answers
    assert "1 issues waiting for admission" in answers["factorial_project_get"], answers
    assert "pending issues not read" not in answers["factorial_project_get"], answers
    assert PENDING_TITLE in answers["factorial_ticket_get"], answers
    assert answers["factorial_config_validate"].startswith("valid: 0 errors, 0 blockers"), answers
    assert answers["factorial_editor_open"].startswith("editor card"), answers
    # The commit twin's precheck read the head and drew the diff, so the card is pending.
    assert "pending: the person approves" in answers["factorial_config_commit"], answers
    assert to_api and all(
        str(request.url).startswith(f"{api_url}/") for request in to_api
    ), to_api
    assert {request.headers["authorization"] for request in to_api} == {f"Bearer {TOKEN}"}
    assert not [request for request in to_api if "private-token" in request.headers]
    assert upstream and {request.headers["private-token"] for request in upstream} == {
        "glpat-stored"
    }
    assert {request.method for request in to_api} == {"GET"}
