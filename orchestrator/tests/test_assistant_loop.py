"""The assistant's process: its claims under the concurrency cap, the queue channel, a stop,
its preflight and its retention."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.assistant_settings import STATUS_KEY
from orchestrator.assistant import loop as assistant_loop
from orchestrator.assistant.items import append_item
from orchestrator.assistant.loop import serve
from orchestrator.assistant.retention import purge_expired
from orchestrator.assistant.turns import RESTART_NOTICE, enqueue_turn, wake_queue
from orchestrator.db.models import (
    AppSetting,
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    Event,
    McpAccess,
    McpToken,
)
from orchestrator.events import EventType, append
from orchestrator.settings import AssistantProcessSettings, Settings
from runtime.assistant.contract import TurnDone, TurnStop
from tests.api_harness import deps_for, eventually, process_settings, seed_mcp_token, seed_project
from tests.assistant_fakes import (
    FakeBackend,
    enable_assistant,
    renewed,
    seed_conversation,
    seed_turn,
    wipe_chat,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

UNREACHABLE = "postgresql+asyncpg://unused:unused@localhost:1/unused"


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession], master_key: None) -> AsyncIterator[None]:
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


async def queue_messages(maker: async_sessionmaker[AsyncSession], count: int) -> list[int]:
    turn_ids = []
    async with maker() as session:
        for n in range(count):
            conversation = await seed_conversation(session)
            turn = await enqueue_turn(session, conversation.id, trigger=ChatTurnTrigger.USER)
            assert turn is not None
            await append_item(
                session,
                conversation_id=conversation.id,
                turn_id=turn.id,
                kind=ChatItemKind.USER_TEXT,
                payload={"text": f"message {n}"},
            )
            turn_ids.append(turn.id)
        await session.commit()
    return turn_ids


async def statuses(maker: async_sessionmaker[AsyncSession], turn_ids: list[int]) -> list[str]:
    async with maker() as session:
        rows = (await session.scalars(select(ChatTurn).where(ChatTurn.id.in_(turn_ids)))).all()
    by_id = {row.id: row.status for row in rows}
    return [by_id[turn_id] for turn_id in turn_ids]


async def backend_ready(maker: async_sessionmaker[AsyncSession]) -> None:
    """Until the process has written its preflight, after which it ticks."""

    async def written() -> bool:
        async with maker() as session:
            return await session.get(AppSetting, STATUS_KEY) is not None

    await eventually(written)


async def test_loop_claims_up_to_the_cap(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await enable_assistant(maker)
    turns = await queue_messages(maker, 3)
    backend = FakeBackend(hang=True)
    settings = process_settings(database_url, concurrency=2, tick_seconds=0.05)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )

    await backend.started(2)
    await asyncio.sleep(0.3)
    assert len(backend.specs) == 2
    assert sorted(await statuses(maker, turns)) == ["queued", "running", "running"]

    stop.set()
    await asyncio.wait_for(serving, timeout=15)
    started = [int(spec.turn_id) for spec in backend.specs]
    assert sorted(backend.cancelled) == sorted(map(str, started))
    # A stop is not the person's cancel: the turn says the assistant restarted.
    assert await statuses(maker, started) == ["interrupted", "interrupted"]
    async with maker() as session:
        notices = (
            await session.scalars(
                select(ChatItem.payload).where(
                    ChatItem.turn_id.in_(started), ChatItem.kind == ChatItemKind.NOTICE
                )
            )
        ).all()
    assert notices == [{"text": RESTART_NOTICE}] * 2


async def test_a_cancel_reaches_a_running_turn_through_the_queue_channel(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    """The tick is 30 s and the next lease renewal 20 s away: only the LISTEN can make this
    quick."""
    await enable_assistant(maker)
    first, second = await queue_messages(maker, 2)
    backend = FakeBackend(hang=True)
    settings = process_settings(database_url, concurrency=1, tick_seconds=30)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )
    try:
        await backend.started(1)
        await renewed(maker, first)
        async with maker() as session:
            await session.execute(
                update(ChatTurn).where(ChatTurn.id == first).values(cancel_requested=True)
            )
            await wake_queue(session)
            await session.commit()

        await backend.started(2)
        assert backend.cancelled == [str(first)]
        assert await statuses(maker, [first, second]) == ["cancelled", "running"]
    finally:
        stop.set()
        await asyncio.wait_for(serving, timeout=15)


async def test_a_finished_turn_frees_its_slot_at_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """No LISTEN (its database is unreachable) and a 30 s tick: only the finished turn's own
    wake can claim the next one this soon."""
    await enable_assistant(maker)
    first, second = await queue_messages(maker, 2)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    settings = process_settings(UNREACHABLE, concurrency=1, tick_seconds=30)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )
    try:
        await backend.started(2)

        async def both_done() -> bool:
            return await statuses(maker, [first, second]) == ["done", "done"]

        await eventually(both_done)
    finally:
        stop.set()
        await asyncio.wait_for(serving, timeout=15)


async def test_restart_recovery_runs_before_the_first_claim(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await enable_assistant(maker)
    async with maker() as session:
        conversation = await seed_conversation(session)
        lost = await seed_turn(
            session,
            conversation.id,
            status=ChatTurnStatus.RUNNING,
            lease_owner="gone:1",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()
    backend = FakeBackend()
    settings = process_settings(database_url, tick_seconds=0.05)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )

    async def interrupted() -> bool:
        return await statuses(maker, [lost.id]) == ["interrupted"]

    await eventually(interrupted)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)


async def test_a_claim_another_process_won_is_nothing_to_claim(
    maker: async_sessionmaker[AsyncSession],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two assistant processes: this one's claim read a conversation as free while the other's
    claim of it was still uncommitted, and `chat_turn_one_running` refused a second running
    turn. A lost race, not a fault: the tick claims nothing, rolls back and logs no error."""
    async with maker() as session:
        conversation = await seed_conversation(session)
        await seed_turn(
            session,
            conversation.id,
            status=ChatTurnStatus.RUNNING,
            lease_owner="other:2",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        waiting = await seed_turn(session, conversation.id, seq=2)
        await session.commit()
    raced = 0

    async def lost(session: AsyncSession, *, owner: str, lease: timedelta, now: datetime) -> None:
        """The claim's UPDATE, as it runs once its NOT EXISTS has missed the other claim."""
        nonlocal raced
        raced += 1
        await session.execute(
            update(ChatTurn)
            .where(ChatTurn.id == waiting.id)
            .values(
                status=ChatTurnStatus.RUNNING,
                lease_owner=owner,
                lease_expires_at=now + lease,
                started_at=now,
            )
        )

    async def raced_twice() -> bool:
        return raced >= 2

    monkeypatch.setattr(assistant_loop, "claim_turn", lost)
    backend = FakeBackend()
    settings = process_settings(database_url, tick_seconds=0.05)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )
    await eventually(raced_twice)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert backend.specs == []
    assert await statuses(maker, [waiting.id]) == ["queued"]


async def test_preflight_failure_is_written_to_the_status_row(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await enable_assistant(maker)
    [turn] = await queue_messages(maker, 1)
    backend = FakeBackend(unavailable="claude-agent-sdk 0.1.0 is installed")
    settings = process_settings(database_url, tick_seconds=0.05)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )

    async def written() -> bool:
        async with maker() as session:
            return await session.get(AppSetting, STATUS_KEY) is not None

    await eventually(written)
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)

    async with maker() as session:
        status = (await session.get_one(AppSetting, STATUS_KEY)).value
    assert status["ok"] is False
    assert status["message"] == (
        "claude_sdk: claude-agent-sdk 0.1.0 is installed; "
        "codex_cli: claude-agent-sdk 0.1.0 is installed; "
        "bedrock_responses: claude-agent-sdk 0.1.0 is installed"
    )
    assert status["version"] == "claude_sdk fake, codex_cli fake, bedrock_responses fake"
    assert datetime.fromisoformat(status["at"]).tzinfo is not None
    # Sends stay queued while the backend cannot run them.
    assert backend.specs == []
    assert await statuses(maker, [turn]) == ["queued"]


async def test_a_backend_that_fails_its_check_idles_only_its_own_turns(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    """An organization on Claude keeps its assistant on a host whose Codex CLI is missing or
    drifted; `/readyz` still names the backend that failed."""
    await enable_assistant(maker)
    [turn] = await queue_messages(maker, 1)
    claude = FakeBackend()
    codex = FakeBackend(unavailable="`codex` is not on PATH")
    settings = process_settings(database_url, tick_seconds=0.05)
    deps = replace(
        deps_for(maker, settings, claude),
        backend_for=lambda kind: codex if kind == "codex_cli" else claude,
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(serve(deps, stop=stop, settings=settings))

    await claude.started(1)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)

    async with maker() as session:
        status = (await session.get_one(AppSetting, STATUS_KEY)).value
    assert [spec.turn_id for spec in claude.specs] == [str(turn)]
    assert (status["ok"], status["message"]) == (False, "codex_cli: `codex` is not on PATH")
    assert status["backends"] == {
        "claude_sdk": {"ok": True, "message": None},
        "codex_cli": {"ok": False, "message": "`codex` is not on PATH"},
        "bedrock_responses": {"ok": True, "message": None},
    }


async def test_a_passing_preflight_says_so(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = process_settings(database_url, tick_seconds=0.05)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, FakeBackend()), stop=stop, settings=settings)
    )

    async def written() -> bool:
        async with maker() as session:
            return await session.get(AppSetting, STATUS_KEY) is not None

    await eventually(written)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)
    async with maker() as session:
        status = (await session.get_one(AppSetting, STATUS_KEY)).value
    assert (status["ok"], status["message"]) == (True, None)


async def test_concurrency_and_retention_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load() -> AssistantProcessSettings:
        # `_env_file` is a pydantic-settings init kwarg pyright does not model.
        return Settings(_env_file=None).assistant  # pyright: ignore[reportCallIssue]

    assert (load().concurrency, load().retain_days) == (4, 30)
    monkeypatch.setenv("FACTORIAL_ASSISTANT__CONCURRENCY", "7")
    monkeypatch.setenv("FACTORIAL_ASSISTANT__RETAIN_DAYS", "9")
    monkeypatch.setenv("FACTORIAL_ASSISTANT__WORK_ROOT", "/data/assistant")
    assert (load().concurrency, load().retain_days, load().work_root) == (
        7,
        9,
        Path("/data/assistant"),
    )
    for name, value in [
        ("FACTORIAL_ASSISTANT__CONCURRENCY", "0"),
        ("FACTORIAL_ASSISTANT__CONCURRENCY", "33"),
        ("FACTORIAL_ASSISTANT__RETAIN_DAYS", "0"),
    ]:
        with monkeypatch.context() as scoped:
            scoped.setenv(name, value)
            with pytest.raises(ValidationError):
                load()


async def _exists(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> bool:
    async with maker() as session:
        return await session.get(ChatConversation, conversation_id) is not None


async def _updated_at(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> datetime:
    async with maker() as session:
        return (await session.get_one(ChatConversation, conversation_id)).updated_at


async def _purge(maker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with maker() as session:
        purged = await purge_expired(session, now=now, retain_days=30)
        await session.commit()
    return purged


async def test_conversation_is_purged_30_days_after_its_last_item(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session)
        await session.commit()
    created = await _updated_at(maker, conversation.id)
    await asyncio.sleep(0.05)
    async with maker() as session:
        await append_item(
            session,
            conversation_id=conversation.id,
            turn_id=None,
            kind=ChatItemKind.USER_TEXT,
            payload={"text": "still here"},
        )
        await session.commit()
    touched = await _updated_at(maker, conversation.id)
    assert touched > created + timedelta(milliseconds=10)
    month = timedelta(days=30)

    assert await _purge(maker, created + month + timedelta(milliseconds=5)) == 0
    assert await _purge(maker, touched + month) == 0
    assert await _exists(maker, conversation.id)
    assert await _purge(maker, touched + month + timedelta(milliseconds=1)) == 1
    assert not await _exists(maker, conversation.id)


async def test_purge_keeps_events_and_token_rows(maker: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    async with maker() as session:
        project = await seed_project(session)
        event = await append(session, type=EventType.ADMISSION_REFUSED, project_id=project.id)
        conversation = await seed_conversation(session)
        turn = await seed_turn(session, conversation.id, status=ChatTurnStatus.DONE)
        token, _ = await seed_mcp_token(session, access=McpAccess.CHAT)
        token.chat_turn_id = turn.id
        card = await append_item(
            session,
            conversation_id=conversation.id,
            turn_id=turn.id,
            kind=ChatItemKind.PROPOSAL,
            payload={},
        )
        session.add(
            ChatProposal(
                conversation_id=conversation.id,
                turn_id=turn.id,
                item_id=card.id,
                tool="factorial_ticket_retry",
                args={},
                preview={},
                status=ChatProposalStatus.SUCCEEDED,
                mcp_token_id=token.id,
                expires_at=now,
            )
        )
        await session.commit()

    assert await _purge(maker, now + timedelta(days=31)) == 1

    async with maker() as session:
        assert await session.get(ChatConversation, conversation.id) is None
        assert await session.get(ChatTurn, turn.id) is None
        assert (await session.scalars(select(ChatProposal))).all() == []
        kept = await session.get_one(McpToken, token.id)
        assert kept.chat_turn_id is None
        assert await session.get(Event, event.id) is not None


async def test_purge_skips_a_conversation_with_a_live_turn(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        waiting = await seed_conversation(session)
        await seed_turn(session, waiting.id, status=ChatTurnStatus.QUEUED)
        running = await seed_conversation(session)
        await seed_turn(session, running.id, status=ChatTurnStatus.RUNNING)
        finished = await seed_conversation(session)
        await seed_turn(session, finished.id, status=ChatTurnStatus.FAILED)
        await session.commit()

    assert await _purge(maker, datetime.now(UTC) + timedelta(days=31)) == 1

    assert await _exists(maker, waiting.id)
    assert await _exists(maker, running.id)
    assert not await _exists(maker, finished.id)


async def test_retention_runs_on_the_first_tick_and_then_hourly(
    maker: async_sessionmaker[AsyncSession], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    purges: list[datetime] = []

    async def counted(_session: AsyncSession, *, now: datetime, retain_days: int) -> int:
        purges.append(now)
        return 0

    monkeypatch.setattr(assistant_loop, "purge_expired", counted)
    settings = process_settings(database_url, tick_seconds=0.02)
    backend = FakeBackend()
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )
    await backend_ready(maker)
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(serving, timeout=15)

    assert len(purges) == 1
