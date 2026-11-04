"""The assistant's turn queue in Postgres: enqueue, claim, recovery after a restart, the daily
spend, and the backend's own state log."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.items import append_item, update_item
from orchestrator.assistant.state_store import PostgresStateStore
from orchestrator.assistant.turns import (
    RESTART_NOTICE,
    claim_turn,
    enqueue_turn,
    finish_turn,
    recover_lost_turns,
    spent_today,
)
from orchestrator.db.models import (
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatSpend,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    Organization,
)
from orchestrator.events.channels import CHAT_QUEUE_CHANNEL
from tests.assistant_fakes import seed_conversation, seed_turn, sessions_of, wipe_chat
from tests.wake_probe import listening

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
LEASE = timedelta(seconds=60)


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


async def test_enqueue_numbers_turns_and_wakes_the_queue(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session)
        await session.commit()
    async with listening(database_url, CHAT_QUEUE_CHANNEL) as heard:
        async with maker() as session:
            first = await enqueue_turn(session, conversation.id, trigger=ChatTurnTrigger.USER)
            assert heard.empty()
            await session.commit()
        await asyncio.wait_for(heard.get(), timeout=5)
    assert first is not None
    assert (first.seq, first.status, first.trigger) == (1, "queued", "user")

    async with maker() as session:
        await session.execute(update(ChatTurn).where(ChatTurn.id == first.id).values(status="done"))
        second = await enqueue_turn(session, conversation.id, trigger=ChatTurnTrigger.APPROVAL)
        await session.commit()
    assert second is not None
    assert second.seq == 2


async def test_claim_skips_a_conversation_with_a_running_turn(async_session: AsyncSession) -> None:
    busy = await seed_conversation(async_session)
    await seed_turn(async_session, busy.id, seq=1, status=ChatTurnStatus.RUNNING)
    await seed_turn(async_session, busy.id, seq=2)
    free = await seed_conversation(async_session)
    waiting = await seed_turn(async_session, free.id)

    claimed = await claim_turn(async_session, owner="host:1", lease=LEASE, now=NOW)

    assert claimed is not None
    assert claimed.id == waiting.id
    assert claimed.status == ChatTurnStatus.RUNNING
    assert (claimed.lease_owner, claimed.lease_expires_at, claimed.started_at) == (
        "host:1",
        NOW + LEASE,
        NOW,
    )
    assert await claim_turn(async_session, owner="host:1", lease=LEASE, now=NOW) is None


async def test_claim_is_exclusive_between_two_sessions(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session)
        await seed_turn(session, conversation.id)
        await session.commit()

    async with maker() as first, maker() as second:
        mine = await claim_turn(first, owner="host:1", lease=LEASE, now=NOW)
        # SKIP LOCKED: the second claim passes the row the first holds rather than wait for it.
        theirs = await asyncio.wait_for(
            claim_turn(second, owner="host:2", lease=LEASE, now=NOW), timeout=5
        )
        await first.commit()

    assert mine is not None
    assert theirs is None


async def test_a_second_approval_shares_the_queued_turn(async_session: AsyncSession) -> None:
    conversation = await seed_conversation(async_session)
    first = await enqueue_turn(async_session, conversation.id, trigger=ChatTurnTrigger.APPROVAL)
    second = await enqueue_turn(async_session, conversation.id, trigger=ChatTurnTrigger.APPROVAL)

    assert first is not None
    assert second is None
    queued = (
        await async_session.scalars(
            select(ChatTurn).where(ChatTurn.conversation_id == conversation.id)
        )
    ).all()
    assert [turn.id for turn in queued] == [first.id]


async def test_recovery_interrupts_expired_leases(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        lost_conversation = await seed_conversation(session)
        lost = await seed_turn(
            session,
            lost_conversation.id,
            status=ChatTurnStatus.RUNNING,
            lease_owner="gone:1",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        alive_conversation = await seed_conversation(session)
        alive = await seed_turn(
            session,
            alive_conversation.id,
            status=ChatTurnStatus.RUNNING,
            lease_owner="here:1",
            lease_expires_at=NOW + timedelta(seconds=30),
        )
        await session.commit()

    async with maker() as session:
        recovered = await recover_lost_turns(session, now=NOW)
        await session.commit()

    assert recovered == [lost.id]
    async with maker() as session:
        lost_row = await session.get_one(ChatTurn, lost.id)
        alive_row = await session.get_one(ChatTurn, alive.id)
        notices = (
            await session.scalars(
                select(ChatItem).where(
                    ChatItem.conversation_id == lost_conversation.id,
                    ChatItem.kind == ChatItemKind.NOTICE,
                )
            )
        ).all()
    assert (lost_row.status, lost_row.ended_at) == (ChatTurnStatus.INTERRUPTED, NOW)
    assert alive_row.status == ChatTurnStatus.RUNNING
    assert [(notice.turn_id, notice.payload["text"]) for notice in notices] == [
        (lost.id, RESTART_NOTICE)
    ]
    assert RESTART_NOTICE == "The assistant restarted. Send your message again."


async def test_spent_today_sums_only_the_owners_turns_since_midnight_utc(
    async_session: AsyncSession,
) -> None:
    midnight = NOW.replace(hour=0)
    mine = await seed_conversation(async_session, owner="operator@example.com")
    for seq, (started, cost) in enumerate(
        [
            (NOW - timedelta(hours=1), Decimal("0.5")),
            (midnight + timedelta(seconds=1), Decimal("0.25")),
            (midnight - timedelta(seconds=1), Decimal("1.0")),
        ],
        start=1,
    ):
        await seed_turn(
            async_session,
            mine.id,
            seq=seq,
            status=ChatTurnStatus.DONE,
            started_at=started,
            cost_usd=cost,
        )
    await seed_turn(async_session, mine.id, seq=4)
    theirs = await seed_conversation(async_session, owner="teammate@example.com")
    await seed_turn(
        async_session, theirs.id, status=ChatTurnStatus.DONE, started_at=NOW, cost_usd=Decimal(2)
    )

    ours = await default_organization_id(async_session)
    spent = await spent_today(async_session, ours, "operator@example.com", now=NOW)
    assert spent == Decimal("0.75")
    assert await spent_today(async_session, ours, "outsider@example.com", now=NOW) == Decimal(0)


async def test_a_persons_spend_in_another_organization_is_that_organizations(
    async_session: AsyncSession,
) -> None:
    """Each organization sets its own daily budget, so one person's turns in two of them land on
    two ledger rows and neither budget pays for the other's turns."""
    ours = await default_organization_id(async_session)
    other = Organization(slug=f"org-{NOW.timestamp():.0f}", name="Other", updated_by="test")
    async_session.add(other)
    await async_session.flush()
    for organization_id, cost in ((ours, Decimal("0.5")), (other.id, Decimal(3))):
        conversation = await seed_conversation(
            async_session, owner="operator@example.com", organization_id=organization_id
        )
        await seed_turn(
            async_session,
            conversation.id,
            status=ChatTurnStatus.DONE,
            started_at=NOW,
            cost_usd=cost,
        )

    assert await spent_today(async_session, ours, "operator@example.com", now=NOW) == Decimal("0.5")
    assert await spent_today(async_session, other.id, "operator@example.com", now=NOW) == Decimal(3)


async def test_backend_state_appends_in_order_and_an_unknown_key_loads_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session)
        other = await seed_conversation(session)
        await session.commit()
    store = PostgresStateStore(sessions_of(maker), conversation.id)

    await store.append("session-a", [{"n": 1}, {"n": 2}])
    await store.append("session-a", [{"n": 3}])
    await store.append("session-b", [{"n": 9}])

    assert await store.load("session-a") == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert await store.load("session-b") == [{"n": 9}]
    assert await store.load("never") is None
    assert await PostgresStateStore(sessions_of(maker), other.id).load("session-a") is None


async def test_finish_ends_only_a_running_turn_this_owner_holds(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session)
        mine = await seed_turn(
            session, conversation.id, status=ChatTurnStatus.RUNNING, lease_owner="host:1"
        )
        recovered = await seed_turn(
            session, (await seed_conversation(session)).id, status=ChatTurnStatus.INTERRUPTED
        )
        theirs = await seed_turn(
            session,
            (await seed_conversation(session)).id,
            status=ChatTurnStatus.RUNNING,
            lease_owner="host:2",
        )
        await session.commit()

    async with maker() as session:
        ended = [
            await finish_turn(
                session, turn, owner="host:1", status=ChatTurnStatus.DONE, now=NOW, stop="end"
            )
            for turn in (mine, recovered, theirs)
        ]
        await session.commit()

    assert ended == [True, False, False]
    async with maker() as session:
        rows = [await session.get_one(ChatTurn, turn.id) for turn in (mine, recovered, theirs)]
    assert [(row.status, row.stop_reason) for row in rows] == [
        ("done", "end"),
        ("interrupted", None),
        ("running", None),
    ]


async def test_the_spend_ledger_adds_a_rewritten_cost_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        conversation = await seed_conversation(session, owner="operator@example.com")
        first = await seed_turn(
            session,
            conversation.id,
            status=ChatTurnStatus.DONE,
            started_at=NOW,
            cost_usd=Decimal("0.50"),
        )
        await seed_turn(session, conversation.id, seq=2, status=ChatTurnStatus.DONE, started_at=NOW)
        await session.commit()
    async with maker() as session:
        # The backend reports a turn's cost again, higher: only the difference is new spend.
        await session.execute(
            update(ChatTurn).where(ChatTurn.id == first.id).values(cost_usd=Decimal("0.75"))
        )
        await session.execute(
            update(ChatTurn).where(ChatTurn.id == first.id).values(cost_usd=Decimal("0.75"))
        )
        await session.commit()

    async with maker() as session:
        ledger = (await session.scalars(select(ChatSpend))).all()
        spent = await spent_today(
            session, await default_organization_id(session), "operator@example.com", now=NOW
        )
    assert [(row.owner, row.day, row.cost_usd) for row in ledger] == [
        ("operator@example.com", NOW.date(), Decimal("0.75"))
    ]
    assert spent == Decimal("0.75")


async def test_an_item_update_queues_behind_a_conversation_delete(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A delete locks the conversation, then its items. An update that locked its item first and
    then waited on the conversation would close the circle, and Postgres would abort one of
    them: so an update takes the conversation first, and simply finds its item gone."""
    async with maker() as session:
        conversation = await seed_conversation(session)
        item = await append_item(
            session,
            conversation_id=conversation.id,
            turn_id=None,
            kind=ChatItemKind.ASSISTANT_TEXT,
            payload={"text": "streaming"},
        )
        await session.commit()

    async def write() -> None:
        async with maker() as session:
            await update_item(session, item.id, payload={"text": "more"}, final=False)
            await session.commit()

    async with maker() as deleting:
        await deleting.execute(
            select(ChatConversation.id)
            .where(ChatConversation.id == conversation.id)
            .with_for_update()
        )
        writing = asyncio.create_task(write())
        await asyncio.sleep(0.3)
        await deleting.execute(
            delete(ChatConversation).where(ChatConversation.id == conversation.id)
        )
        await deleting.commit()
    await asyncio.wait_for(writing, timeout=5)

    async with maker() as session:
        assert await session.get(ChatItem, item.id) is None
