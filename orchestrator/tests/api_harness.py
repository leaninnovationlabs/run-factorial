"""The shared API-test harness: hermetic settings, an app with an overridable session, clients,
and the rows API tests seed.

Hermetic is the point: `Settings()` reads `.env`, and a developer's real App credentials must
never turn a test's GitHub readers live — every field a test depends on is set explicitly.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx2
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.deps import (
    ApiGitHub,
    PageBlobs,
    PageBlobsFor,
    RepoReader,
    account_sessions_dependency,
    get_db_session,
    github_for_dependency,
    settings_dependency,
)
from orchestrator.api.main import create_app
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.github_token import OrganizationTokens
from orchestrator.assistant.runner import RunnerDeps
from orchestrator.assistant.toolbox import TurnToolbox
from orchestrator.assistant.vocabulary import ALL_ASSISTANT_OFFERS
from orchestrator.db.models import (
    ChatConversation,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    CostSource,
    EnteredBy,
    McpAccess,
    McpMintedVia,
    McpToken,
    ProcessVersion,
    Project,
    Runtime,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.factorial import RepoConfigSource
from orchestrator.factorial.runtimes import ALL_OFFERS
from orchestrator.github import (
    BranchState,
    IssueState,
    PullRequestState,
    SnapshotState,
    StateSource,
)
from orchestrator.github.connection import (
    PRIVATE_KEY_SECRET,
    TOKEN_SECRET,
)
from orchestrator.github.connection import WEBHOOK_SECRET as CONNECTION_WEBHOOK_SECRET
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.mcp_server.context import McpDeps
from orchestrator.mcp_server.tokens import LIFETIME, audience, jti_digest, mint_mcp_jwt
from orchestrator.secrets import store
from orchestrator.settings import (
    AssistantProcessSettings,
    AuthSettings,
    E2bSettings,
    GitLabSettings,
    ManagedSandbox,
    RuntimeSettings,
    Settings,
)
from runtime.assistant.contract import ToolOutcome
from tests.assistant_fakes import FakeBackend, FakeTokenFetch, StoredCredential, sessions_of
from tests.members import ensure_member

TOKEN = "test-api-token-9f2c"
WEBHOOK_SECRET = "test-webhook-secret"
APP_ID = "424242"
#: `mcp_url` derives from `auth.external_url`, and the Host guard admits only its hostname.
MCP_BASE = "http://api.test"
MCP_URL = f"{MCP_BASE}/mcp/"
#: The `Origin` a dashboard request carries. `make_settings()` admits no dashboard, so a client
#: that sends it needs `auth.external_url=MCP_BASE`, as `mcp_settings()` has.
DASHBOARD = {"Origin": MCP_BASE}
#: A catalog file the chat surface offers: not under agents/ or skills/.
EXAMPLE = ".factorial/mcp/product-docs.yaml.example"
CHAT_OWNER = "operator@example.com"


def make_settings(
    *,
    api_token: str = TOKEN,
    auth: AuthSettings | None = None,
    gitlab: GitLabSettings | None = None,
    managed: ManagedSandbox | None = None,
) -> Settings:
    """`managed` narrows the offers to the sandbox ones, which a managed key requires."""
    offered = sorted(ALL_OFFERS)
    if managed is not None:
        offered = [offer for offer in offered if offer.endswith("@e2b")]
    return Settings(
        database_url="postgresql+asyncpg://unused:unused@localhost:1/unused",
        api_token=SecretStr(api_token),
        gitlab=gitlab if gitlab is not None else GitLabSettings(),
        auth=auth if auth is not None else AuthSettings(),
        # Every pair, as a test of logins, Bedrock or readiness creates any type; the default is
        # the API-key types, which the settings tests assert.
        runtime=RuntimeSettings(
            e2b=E2bSettings(deployment="factorial-test", managed=managed or ManagedSandbox()),
            offered=offered,
        ),
        # Every assistant sign-in too, for the same reason; the default is the two API keys.
        assistant=AssistantProcessSettings(offered=sorted(ALL_ASSISTANT_OFFERS)),
    )


async def no_github(project: Project) -> ApiGitHub | None:
    """No project reaches GitHub from a test app unless the test stands one in."""
    return None


def blobs_for(blobs: PageBlobs | None) -> PageBlobsFor:
    """A `page_blobs_dependency` stand-in that reads every project's pages from `blobs`."""

    async def resolve(project: Project) -> PageBlobs | None:
        return blobs

    return resolve


def build_app(settings: Settings, session: AsyncSession | None = None) -> FastAPI:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        if session is None:
            raise AssertionError("this request must never reach the database")
        yield session

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        if session is None:
            raise AssertionError("this request must never reach the database")
        yield session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[account_sessions_dependency] = lambda: _scope
    app.dependency_overrides[settings_dependency] = lambda: settings
    app.dependency_overrides[github_for_dependency] = lambda: no_github
    return app


def client_for(app: FastAPI, *, base_url: str = "http://api.test") -> AsyncClient:
    """No credentials: a test sends its own headers, so it can also send none."""
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url)


def authed_client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def seed_project(
    session: AsyncSession, repo: str | None = None, *, organization_id: int | None = None
) -> Project:
    tag = uuid4().hex[:12]
    organization_id = organization_id or await default_organization_id(session)
    project = Project(
        organization_id=organization_id,
        name=f"proj-{tag}",
        github_repo=repo or f"acme/{tag}",
    )
    session.add(project)
    await ensure_runtime(session, organization_id=organization_id)
    await session.flush()
    return project


async def seed_connected(
    session: AsyncSession,
    repo: str | None = None,
    *,
    app_id: str | None = None,
    secret: str | None = WEBHOOK_SECRET,
    organization_id: int | None = None,
) -> Project:
    """A project with its own GitHub connection: an App's when `app_id` is given, else a token."""
    project = await seed_project(session, repo, organization_id=organization_id)
    bag: dict[str, Any] = {"mode": "app", "app_id": app_id} if app_id else {"mode": "token"}
    project.settings = {"github": bag}
    await session.flush()
    await store.put(
        session, project.id, PRIVATE_KEY_SECRET if app_id else TOKEN_SECRET, "credential"
    )
    if secret is not None:
        await store.put(session, project.id, CONNECTION_WEBHOOK_SECRET, secret)
    return project


#: The Runtime the sample process names. Every process names one, so a test that syncs a
#: process seeds it first (`ensure_runtime`).
TEST_RUNTIME = "claude-api"


async def ensure_runtime(
    session: AsyncSession, slug: str = TEST_RUNTIME, *, organization_id: int | None = None
) -> str:
    """A host claude Runtime named `slug` in the organization, the default one unless named.
    Idempotent across tests and workers: the test database is never truncated."""
    await session.execute(
        pg_insert(Runtime)
        .values(
            organization_id=organization_id or await default_organization_id(session),
            slug=slug,
            name=slug,
            type="claude_api",
        )
        .on_conflict_do_nothing(index_elements=["organization_id", "slug"])
    )
    return slug


async def probe_version_id(session: AsyncSession, project: Project) -> int:
    """The pinned snapshot's row id. Version routes key on the id, not on `version`: two lanes
    may both be at v1, so the number alone stopped identifying a row."""
    return (
        await session.execute(
            select(ProcessVersion.id).where(ProcessVersion.project_id == project.id)
        )
    ).scalar_one()


async def seed_ticket(
    session: AsyncSession,
    project: Project,
    *,
    step: str = "develop",
    status: str = TicketStatus.RUNNING,
    external_key: str | None = None,
    title: str | None = None,
) -> Ticket:
    """`external_key` defaults to a unique tag. Pass an issue NUMBER for anything that reads the
    key the way a GitHub source writes it — the default is a hex tag, which no issue ever is."""
    tag = uuid4().hex[:12]
    source = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
    ).scalar_one_or_none()
    if source is None:
        source = TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITHUB,
            external_ref=project.github_repo,
        )
        session.add(source)
    # One snapshot per project: `(project_id, version)` is unique, and seeding a second ticket
    # into the same project must pin the same version rather than mint a colliding one.
    version = (
        await session.execute(
            select(ProcessVersion).where(ProcessVersion.project_id == project.id).limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        version = ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key="p",
            content={"specVersion": "adlc/v1"},
            source_commit_sha="a" * 40,
        )
        session.add(version)
    await session.flush()

    ticket = Ticket(
        project_id=project.id,
        ticket_source_id=source.id,
        external_key=external_key or tag,
        process_version_id=version.id,
        owner_at_admission="operator",
        title=title,
        current_step=step,
        status=status,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def seed_lane_head(
    session: AsyncSession, ticket: Ticket, *, sha: str = "b" * 40
) -> ProcessVersion:
    """A later sync of the ticket's lane, so the ticket's pin is one snapshot behind the lane's
    head. Seed the project's other tickets first: `seed_ticket` pins whichever of the project's
    snapshots it finds first."""
    pinned = await session.get_one(ProcessVersion, ticket.process_version_id)
    head = ProcessVersion(
        project_id=pinned.project_id,
        source_repo=pinned.source_repo,
        process_key=pinned.process_key,
        content=pinned.content,
        source_commit_sha=sha,
        activated_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add(head)
    await session.flush()
    return head


def make_run(
    ticket_id: int,
    step: str,
    attempt: int,
    *,
    cost: str | None,
    entered_by: str | None = EnteredBy.NEXT,
    status: str = StepRunStatus.SUCCEEDED,
    kind: str = StepKind.AGENT,
    source: str | None = CostSource.REPORTED,
    model: str | None = None,
    tokens: tuple[int, int] | None = (100, 10),
    cached_input_tokens: int | None = None,
    started_at: datetime | None = None,
    took: timedelta = timedelta(minutes=10),
    decided_by: str | None = None,
) -> StepRun:
    """An unsaved `step_run` that ends `took` after `started_at`. By default it starts in the hour
    before now, a minute later per attempt, so a ticket's attempts run in attempt order."""
    if started_at is None:
        started_at = datetime.now(UTC) - timedelta(minutes=60 - attempt)
    return StepRun(
        ticket_id=ticket_id,
        step_name=step,
        kind=kind,
        attempt=attempt,
        entered_by=entered_by,
        status=status,
        model=model,
        cost_usd=None if cost is None else Decimal(cost),
        cost_source=source,
        input_tokens=None if tokens is None else tokens[0],
        output_tokens=None if tokens is None else tokens[1],
        cached_input_tokens=cached_input_tokens,
        decided_by=decided_by,
        started_at=started_at,
        ended_at=started_at + took,
    )


def observed_repo(
    *,
    issue_state: str = "open",
    branches: Sequence[BranchState] = (),
    pulls: Sequence[PullRequestState] = (),
    seen: list[tuple[str, int]] | None = None,
) -> RepoReader:
    """A `RepoReader` over literals: the issue asked for is `issue_state`, and the repo holds
    `branches` and `pulls`. `seen` records each `(repo, issue number)` it is asked for."""

    def read(repo: str, number: int) -> StateSource:
        if seen is not None:
            seen.append((repo, number))
        return SnapshotState(
            repo=repo,
            issue_state=IssueState(
                number=number,
                title="Add a widget",
                body="",
                state=issue_state,
                labels=frozenset(),
                assignees=(),
            ),
            branch_states=tuple(branches),
            pull_request_states=tuple(pulls),
        )

    return read


def mcp_settings() -> Settings:
    """Not `make_settings()`'s default: `external_url` also feeds the dashboard origins, page
    headers and links that other tests pin."""
    return make_settings(auth=AuthSettings(external_url=MCP_BASE))


def mcp_deps_for(
    settings: Settings,
    maker: async_sessionmaker[AsyncSession] | None,
    *,
    config_source: RepoConfigSource | None = None,
    repo_reader: RepoReader | None = None,
) -> McpDeps:
    """`maker=None` for a test that must never reach the database. No GitHub ports but
    `config_source` and `repo_reader`: a tool that needs another answers `Unavailable`, as an
    unconfigured App does."""

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        if maker is None:
            raise AssertionError("this request must never reach the database")
        async with maker() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    return McpDeps(
        settings=lambda: settings,
        sessions=sessions,
        issue_writer=port(None),
        config_source=port(config_source),
        commit_writer=port(None),
        github=port(None),
        repo_reader=port(repo_reader),
    )


def port[T](answer: T) -> Callable[[Project], Awaitable[T]]:
    """An MCP GitHub port that gives every project the same answer."""

    async def answered(_project: Project) -> T:
        return answer

    return answered


def build_mcp_app(
    settings: Settings,
    maker: async_sessionmaker[AsyncSession] | None,
    *,
    config_source: RepoConfigSource | None = None,
) -> FastAPI:
    """`build_app` plus the MCP mounts. `/api` routes still read the overridden settings."""
    app = create_app(mcp=mcp_deps_for(settings, maker, config_source=config_source))
    app.dependency_overrides[settings_dependency] = lambda: settings
    return app


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str, *, url: str = MCP_URL) -> AsyncIterator[Client]:
    """A real MCP client over HTTP, inside the app's lifespan. In-memory `Client(server)` sends
    no token, and every scoped tool is hidden from a request without one."""

    def http(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = False,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    async with app.router.lifespan_context(app):
        transport = StreamableHttpTransport(url, auth=token, httpx_client_factory=http)
        async with Client(transport) as client:
            yield client


async def seed_mcp_token(
    session: AsyncSession,
    *,
    access: McpAccess = McpAccess.READ,
    created_by: str = "operator@example.com",
    expires_at: datetime | None = None,
    audience_of: str = TOKEN,
    signed_with: str = TOKEN,
    organization_id: int | None = None,
    member: bool = True,
) -> tuple[McpToken, str]:
    """A token row and a JWT naming it. `audience_of` is the API token the ROW's audience is
    derived from, `signed_with` the one the JWT is signed under: each differs from `TOKEN` to
    stand in for a rotation on one side. Its creator is a member of its organization unless
    `member` is false."""
    now = datetime.now(UTC)
    jti = secrets.token_urlsafe(32)
    organization_id = organization_id or await default_organization_id(session)
    if member:
        await ensure_member(session, created_by, organization_id=organization_id)
    row = McpToken(
        organization_id=organization_id,
        jti_sha256=jti_digest(jti),
        audience=audience(audience_of),
        label="test",
        created_by=created_by,
        minted_via=McpMintedVia.SESSION,
        access=access,
        expires_at=expires_at or now + LIFETIME,
    )
    session.add(row)
    await session.flush()
    jwt = mint_mcp_jwt(api_token=signed_with, jti=jti, issued_at=now, expires_at=now + LIFETIME)
    return row, jwt


async def seed_chat_turn(
    session: AsyncSession,
    *,
    owner: str = CHAT_OWNER,
    status: ChatTurnStatus = ChatTurnStatus.RUNNING,
) -> ChatTurn:
    """A conversation of its own, and its first turn, its owner a member of `default`."""
    await ensure_member(session, owner)
    conversation = ChatConversation(
        organization_id=await default_organization_id(session),
        owner=owner,
        backend_kind="claude_sdk",
    )
    session.add(conversation)
    await session.flush()
    turn = ChatTurn(
        conversation_id=conversation.id, seq=1, status=status, trigger=ChatTurnTrigger.USER
    )
    session.add(turn)
    await session.flush()
    return turn


@dataclass(frozen=True)
class SeededChatTurn:
    conversation_id: int
    turn_id: int
    owner: str
    organization_id: int

    @property
    def scope(self) -> ChatScope:
        return ChatScope(self.turn_id, self.conversation_id, self.owner, self.organization_id)


def chat_toolbox(
    maker: async_sessionmaker[AsyncSession],
    chat: SeededChatTurn,
    *,
    config_source: RepoConfigSource | None = None,
    deps: McpDeps | None = None,
) -> TurnToolbox:
    """The turn's toolbox as the runner builds it, on the test ports (`mcp_deps_for`)."""
    ports = deps or mcp_deps_for(mcp_settings(), maker, config_source=config_source)
    return TurnToolbox(ports, chat.scope)


def tool_data(outcome: ToolOutcome) -> dict[str, Any]:
    """A tool's output: the JSON line `mcp_server.results.respond` ends its text with."""
    assert outcome.ok, outcome.text
    data = json.loads(outcome.text.rsplit("\n", 1)[-1])
    assert isinstance(data, dict)
    return data


def tool_refused(outcome: ToolOutcome, pattern: str = "") -> str:
    """The refusal's text, once the call is known refused and `pattern` found in it."""
    assert not outcome.ok, outcome.text
    assert re.search(pattern, outcome.text), outcome.text
    return outcome.text


async def seed_running_chat(session: AsyncSession, *, owner: str = CHAT_OWNER) -> SeededChatTurn:
    turn = await seed_chat_turn(session, owner=owner)
    return SeededChatTurn(
        turn.conversation_id, turn.id, owner, await default_organization_id(session)
    )


def agent_manifest(name: str, kind: str) -> str:
    return (
        f"specVersion: factorial/v1\nkind: Agent\nname: {name}\n"
        "prompts:\n  system: system-prompt.md\n  instructions: instructions.md\n"
        "model: claude-haiku-4-5-20251001\n"
        "limits:\n  max_duration: 1h\n  max_turns: 40\n"
        f"runtime:\n  kind: {kind}\n"
    )


def agent_files(name: str, kind: str) -> dict[str, str]:
    """An agent's folder on the `factorial` branch, as a repo fake's file map."""
    folder = f".factorial/agents/{name}"
    return {
        f"{folder}/agent.yaml": agent_manifest(name, kind),
        f"{folder}/system-prompt.md": "# system\n",
        f"{folder}/instructions.md": "# instructions\n",
    }


def process_settings(database_url: str, **assistant: Any) -> Settings:
    """The assistant process's settings, on the test database."""
    return mcp_settings().model_copy(
        update={
            "database_url": database_url,
            "assistant": AssistantProcessSettings(**assistant),
        }
    )


def deps_for(
    maker: async_sessionmaker[AsyncSession], settings: Settings, backend: FakeBackend
) -> RunnerDeps:
    return RunnerDeps(
        sessions=sessions_of(maker),
        settings=lambda: settings,
        backend_for=lambda _kind: backend,
        clock=lambda: datetime.now(UTC),
        tools=lambda _cache: mcp_deps_for(settings, maker),
        github_tokens=OrganizationTokens(lambda _org: FakeTokenFetch()),
        credential=StoredCredential(maker),
    )


async def eventually(check: Callable[[], Awaitable[bool]], *, within: float = 5.0) -> None:
    """A bounded poll instead of a sleep-and-hope."""
    for _ in range(int(within / 0.02)):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")
