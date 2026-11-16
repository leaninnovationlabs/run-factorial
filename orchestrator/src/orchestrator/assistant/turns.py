"""The assistant's turn queue. Postgres is the queue (tech-stack decision 4).

A send queues a turn. The assistant process claims it with `FOR UPDATE SKIP LOCKED` and a lease,
runs it, and finishes it. One turn per conversation runs at a time, because the backend gives a
conversation one working directory: the claim skips a conversation with a running turn, and a
partial unique index refuses a second. Another allows one queued turn, so two approvals in a row
share one continuation.

Every status change wakes the conversation's streams, and a change that frees a conversation
wakes the queue, both on the caller's transaction: nothing is heard before it commits. A
function here that writes a conversation's turn and also its items or tokens takes the
conversation's lock first (`items.lock_conversation`), the order a delete keeps; claim, renew and
cancel touch the one turn row only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.items import (
    SUMMARY_CAP,
    append_notice,
    lock_conversation,
    notify_items,
)
from orchestrator.db.models import (
    CHAT_TURN_LIVE,
    ChatSpend,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
)
from orchestrator.events.channels import CHAT_QUEUE_CHANNEL
from runtime.assistant.contract import TurnFailure

__all__ = [
    "NOT_SET_UP",
    "RESTART_NOTICE",
    "budget_spent",
    "claim_turn",
    "current_turn",
    "enqueue_turn",
    "finish_turn",
    "live_turn",
    "recover_lost_turns",
    "renew_lease",
    "spent_today",
    "spent_today_in_organization",
    "wake_queue",
]

RESTART_NOTICE: Final = "The assistant restarted. Send your message again."
#: Why a turn fails when its credential can't be had at run time. A send is refused before that
#: with a sentence for its own cause (`assistant_settings.assistant_not_ready`).
NOT_SET_UP: Final = "Set up the assistant in Assistant settings."


async def wake_queue(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": CHAT_QUEUE_CHANNEL})


async def enqueue_turn(
    session: AsyncSession, conversation_id: int, *, trigger: ChatTurnTrigger
) -> ChatTurn | None:
    """None when the conversation already has a queued turn, which then answers for this one."""
    # Two enqueues on one conversation would otherwise number their turns alike.
    await lock_conversation(session, conversation_id)
    seq = (
        select(func.coalesce(func.max(ChatTurn.seq), 0) + 1)
        .where(ChatTurn.conversation_id == conversation_id)
        .scalar_subquery()
    )
    insert = (
        pg_insert(ChatTurn)
        .values(
            conversation_id=conversation_id,
            seq=seq,
            status=ChatTurnStatus.QUEUED,
            trigger=trigger,
        )
        .on_conflict_do_nothing(
            index_elements=[ChatTurn.conversation_id],
            index_where=ChatTurn.status == ChatTurnStatus.QUEUED,
        )
        .returning(ChatTurn)
    )
    turn = (await session.scalars(insert)).one_or_none()
    if turn is not None:
        await wake_queue(session)
    return turn


async def live_turn(session: AsyncSession, conversation_id: int) -> ChatTurn | None:
    """The oldest queued or running turn: the running one when a continuation waits behind it."""
    return (
        await session.scalars(
            select(ChatTurn)
            .where(ChatTurn.conversation_id == conversation_id, ChatTurn.status.in_(CHAT_TURN_LIVE))
            .order_by(ChatTurn.id)
            .limit(1)
        )
    ).one_or_none()


async def current_turn(session: AsyncSession, conversation_id: int) -> ChatTurn | None:
    """The live turn, or else the last one: what the Drawer shows as the turn's state."""
    live = await live_turn(session, conversation_id)
    if live is not None:
        return live
    return (
        await session.scalars(
            select(ChatTurn)
            .where(ChatTurn.conversation_id == conversation_id)
            .order_by(ChatTurn.id.desc())
            .limit(1)
        )
    ).one_or_none()


async def claim_turn(
    session: AsyncSession, *, owner: str, lease: timedelta, now: datetime
) -> ChatTurn | None:
    """The oldest queued turn of a conversation with nothing running, marked running under
    `owner`'s lease. Running before any backend spawns: a chat tool refuses a turn that is not
    running."""
    queued = aliased(ChatTurn)
    running = aliased(ChatTurn)
    candidate = (
        select(queued.id)
        .where(
            queued.status == ChatTurnStatus.QUEUED,
            ~exists().where(
                running.conversation_id == queued.conversation_id,
                running.status == ChatTurnStatus.RUNNING,
            ),
        )
        .order_by(queued.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    claim = (
        update(ChatTurn)
        .where(ChatTurn.id == candidate)
        .values(
            status=ChatTurnStatus.RUNNING,
            lease_owner=owner,
            lease_expires_at=now + lease,
            started_at=now,
        )
        .returning(ChatTurn)
    )
    turn = (await session.scalars(claim)).one_or_none()
    if turn is not None:
        await notify_items(session, turn.conversation_id)
    return turn


async def renew_lease(
    session: AsyncSession, turn_id: int, *, owner: str, until: datetime
) -> bool | None:
    """Whether a cancel is requested, or None when the turn is no longer this owner's to run."""
    return (
        await session.execute(
            update(ChatTurn)
            .where(
                ChatTurn.id == turn_id,
                ChatTurn.lease_owner == owner,
                ChatTurn.status == ChatTurnStatus.RUNNING,
            )
            .values(lease_expires_at=until)
            .returning(ChatTurn.cancel_requested)
        )
    ).scalar_one_or_none()


async def finish_turn(
    session: AsyncSession,
    turn: ChatTurn,
    *,
    owner: str,
    status: ChatTurnStatus,
    now: datetime,
    stop: str | None = None,
    failure: TurnFailure | None = None,
    message: str | None = None,
) -> bool:
    """Whether this call ended the turn. Only a turn still running under `owner`'s lease ends:
    one another process recovered, or one already ended, stays as it is, and its caller writes
    no notice. Reads `turn`'s ids only; the row is the authority."""
    await lock_conversation(session, turn.conversation_id)
    ended = await session.scalar(
        update(ChatTurn)
        .where(
            ChatTurn.id == turn.id,
            ChatTurn.status == ChatTurnStatus.RUNNING,
            ChatTurn.lease_owner == owner,
        )
        .values(
            status=status,
            ended_at=now,
            lease_expires_at=None,
            stop_reason=stop,
            failure_reason=None if failure is None else str(failure),
            failure_message=None if message is None else message[:SUMMARY_CAP],
        )
        .returning(ChatTurn.id)
        .execution_options(synchronize_session=False)
    )
    if ended is None:
        return False
    await notify_items(session, turn.conversation_id)
    await wake_queue(session)
    return True


async def recover_lost_turns(session: AsyncSession, *, now: datetime) -> list[int]:
    """Interrupt every running turn whose lease ran out: its process is gone, and the backend's
    session went with it. Each conversation's lock comes before its turn's, in conversation
    order, so two processes recovering at once queue rather than deadlock."""
    candidates = (
        await session.execute(
            select(ChatTurn.id, ChatTurn.conversation_id)
            .where(ChatTurn.status == ChatTurnStatus.RUNNING, ChatTurn.lease_expires_at < now)
            .order_by(ChatTurn.conversation_id, ChatTurn.id)
        )
    ).all()
    recovered: list[int] = []
    for turn_id, conversation_id in candidates:
        await lock_conversation(session, conversation_id)
        lost = await session.scalar(
            update(ChatTurn)
            .where(
                ChatTurn.id == turn_id,
                ChatTurn.status == ChatTurnStatus.RUNNING,
                ChatTurn.lease_expires_at < now,
            )
            .values(status=ChatTurnStatus.INTERRUPTED, ended_at=now, lease_expires_at=None)
            .returning(ChatTurn.id)
            .execution_options(synchronize_session=False)
        )
        if lost is None:
            continue
        await append_notice(
            session, conversation_id=conversation_id, turn_id=turn_id, text=RESTART_NOTICE
        )
        await notify_items(session, conversation_id)
        recovered.append(turn_id)
    if recovered:
        await wake_queue(session)
    return sorted(recovered)


async def spent_today(
    session: AsyncSession, organization_id: int, owner: str, *, now: datetime
) -> Decimal:
    """What `owner`'s turns in one organization cost on today's UTC date, estimates included.
    Read from the `chat_spend` ledger, which a deleted conversation does not take with it."""
    total = await session.scalar(
        select(ChatSpend.cost_usd).where(
            ChatSpend.organization_id == organization_id,
            ChatSpend.owner == owner,
            ChatSpend.day == now.astimezone(UTC).date(),
        )
    )
    return Decimal(total or 0)


async def spent_today_in_organization(
    session: AsyncSession, organization_id: int, *, now: datetime
) -> Decimal:
    total = await session.scalar(
        select(func.sum(ChatSpend.cost_usd)).where(
            ChatSpend.organization_id == organization_id,
            ChatSpend.day == now.astimezone(UTC).date(),
        )
    )
    return Decimal(total or 0)


async def budget_spent(
    session: AsyncSession,
    organization_id: int,
    owner: str,
    assistant: AssistantSettings,
    *,
    now: datetime,
) -> Literal["person", "organization"] | None:
    """Which of today's budgets is spent, the person's first."""
    if await spent_today(session, organization_id, owner, now=now) >= assistant.daily_budget_usd:
        return "person"
    cap = assistant.organization_daily_budget_usd
    if cap is not None and await spent_today_in_organization(
        session, organization_id, now=now
    ) >= cap:
        return "organization"
    return None
