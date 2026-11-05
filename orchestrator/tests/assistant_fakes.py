"""Stand-ins for the assistant's tests: a scripted chat backend, a clock the test moves, and the
rows a turn needs.

The chat tables are shared by every test in the session and `claim_turn` takes the oldest queued
turn of any conversation, so a test that claims or purges wipes them first and after
(`wipe_chat`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.github_token import ReadEntry
from orchestrator.db.models import (
    ChatConversation,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    Organization,
)
from orchestrator.db.session import SessionScope
from orchestrator.github.client import GITHUB_API_URL
from runtime.assistant.contract import (
    BackendStateStore,
    ChatEvent,
    ChatToolbox,
    ChatTurnSpec,
    TurnDone,
    TurnStop,
)
from runtime.contract import AdapterUnavailable
from tests.members import ensure_member

#: `AssistantProcessSettings.lease_seconds`'s default, which the tests' claims use too.
LEASE = timedelta(seconds=60)

#: A script step that is not an event: awaited between two events, so a test can move the clock
#: or look at the database mid-turn.
Hook = Callable[[], Awaitable[None]]


class FakeBackend:
    kind: ClassVar[str] = "claude_sdk"
    pinned_version: ClassVar[str] = "fake"
    supports_partial_text: ClassVar[bool] = True
    supports_resume: ClassVar[bool] = True
    supports_cancel: ClassVar[bool] = True
    supports_effort: ClassVar[bool] = True

    def __init__(
        self,
        script: Sequence[ChatEvent | Hook] = (),
        *,
        hang: bool = False,
        unavailable: str | None = None,
    ) -> None:
        self.script = list(script)
        self.hang = hang
        self.unavailable = unavailable
        self.specs: list[ChatTurnSpec] = []
        self.toolboxes: list[ChatToolbox] = []
        self.cancelled: list[str] = []
        self._released: dict[str, asyncio.Event] = {}
        self._began = asyncio.Event()

    async def preflight(self) -> None:
        if self.unavailable is not None:
            raise AdapterUnavailable(self.unavailable)

    async def turn(
        self, spec: ChatTurnSpec, state: BackendStateStore, tools: ChatToolbox
    ) -> AsyncIterator[ChatEvent]:
        self.specs.append(spec)
        self.toolboxes.append(tools)
        self._began.set()
        released = self._released.setdefault(spec.turn_id, asyncio.Event())
        for step in self.script:
            if callable(step):
                await step()
            else:
                yield step
        if self.hang:
            # As the contract asks: a cancelled live turn ends `TurnDone(CANCELLED)`.
            await released.wait()
            yield TurnDone(stop=TurnStop.CANCELLED)

    async def cancel(self, turn_id: str) -> None:
        self.cancelled.append(turn_id)
        self._released.setdefault(turn_id, asyncio.Event()).set()

    async def started(self, count: int = 1) -> None:
        """Until `count` turns have begun, for at most five seconds."""
        async with asyncio.timeout(5):
            while len(self.specs) < count:
                self._began.clear()
                await self._began.wait()


class FakeTokenFetch:
    """The API's token route, stood in: each call mints a token good for an hour."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls = 0

    async def __call__(self) -> list[ReadEntry]:
        self.calls += 1
        if self.fails:
            raise RuntimeError("the API is down")
        return [
            ReadEntry(
                repos=frozenset(),
                token=SecretStr(f"ghs_test_{self.calls}"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                base_url=GITHUB_API_URL,
            )
        ]


class StoredCredential:
    """The API's credential route, stood in: the organization's credential, opened as the route
    opens it, so a test stores one with `enable_assistant` and the turn runs on it."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self.asked: list[tuple[str, str, str]] = []

    async def __call__(self, org: str, assistant: AssistantSettings) -> SecretStr | None:
        self.asked.append((org, assistant.backend, assistant.auth_mode))
        if not assistant.enabled:
            return None
        async with self._maker() as session:
            organization_id = await session.scalar(
                select(Organization.id).where(Organization.slug == org)
            )
            assert organization_id is not None
            return await svc.assistant_credential(session, organization_id, assistant)


@dataclass
class FakeClock:
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def sessions_of(maker: async_sessionmaker[AsyncSession]) -> SessionScope:
    """`session_scope`'s contract over the test engine: commit on a clean exit."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    return scope


async def wipe_chat(maker: async_sessionmaker[AsyncSession]) -> None:
    """Chat-access token rows and the spend ledger too: both outlive their conversations by
    design, and other modules count token rows and check budgets."""
    async with maker() as session:
        await session.execute(text("DELETE FROM chat_conversation"))
        await session.execute(text("DELETE FROM mcp_token WHERE access = 'chat'"))
        await session.execute(text("DELETE FROM chat_spend"))
        await session.execute(text("DELETE FROM app_setting"))
        await session.execute(text("DELETE FROM organization_setting"))
        await session.execute(text("DELETE FROM organization_credential"))
        await session.commit()


async def enable_assistant(
    maker: async_sessionmaker[AsyncSession], *, credential: str = "sk-ant-test", **overrides: object
) -> AssistantSettings:
    """Needs the `master_key` fixture: the credential is stored encrypted."""
    settings = AssistantSettings.model_validate({"enabled": True, **overrides})
    async with maker() as session:
        organization_id = await default_organization_id(session)
        await svc.save_assistant_settings(session, organization_id, settings, by="test")
        await svc.set_assistant_credential(
            session, organization_id, settings.backend, settings.auth_mode, credential, by="test"
        )
        await session.commit()
    return settings


async def renewed(maker: async_sessionmaker[AsyncSession], turn_id: int) -> None:
    """Until the turn's keeper has renewed the lease its claim set, `started_at` plus the lease.
    From then on, only a wake makes the keeper look again before a third of the lease passes."""
    for _ in range(250):
        async with maker() as session:
            turn = await session.get_one(ChatTurn, turn_id)
        claimed_until = None if turn.started_at is None else turn.started_at + LEASE
        if claimed_until and turn.lease_expires_at and turn.lease_expires_at > claimed_until:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn {turn_id}'s lease was never renewed")


async def seed_conversation(
    session: AsyncSession,
    *,
    owner: str = "operator@example.com",
    organization_id: int | None = None,
) -> ChatConversation:
    """A conversation whose owner is a member of its organization."""
    organization_id = organization_id or await default_organization_id(session)
    await ensure_member(session, owner, organization_id=organization_id)
    conversation = ChatConversation(
        organization_id=organization_id,
        owner=owner,
        backend_kind="claude_sdk",
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def seed_turn(
    session: AsyncSession,
    conversation_id: int,
    *,
    seq: int = 1,
    status: ChatTurnStatus = ChatTurnStatus.QUEUED,
    trigger: ChatTurnTrigger = ChatTurnTrigger.USER,
    **columns: object,
) -> ChatTurn:
    turn = ChatTurn(
        conversation_id=conversation_id, seq=seq, status=status, trigger=trigger, **columns
    )
    session.add(turn)
    await session.flush()
    return turn
