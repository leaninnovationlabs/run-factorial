"""The rows a conversation's Drawer draws: written by the assistant's turns and the chat tools,
read back by the stream.

A write never stamps `rev` or the conversation's `updated_at`; the `chat_item` triggers do, so no
writer can forget either. The `rev` trigger also takes the conversation's row lock
(`lock_conversation`), so a conversation's items commit in `rev` order. `notify_items` rides the
caller's transaction, as `wake_ticket` does: the stream hears about a row only once the row is
committed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.base import JsonDict
from orchestrator.db.models import ChatConversation, ChatItem, ChatItemKind
from orchestrator.events.channels import CHAT_ITEMS_CHANNEL

__all__ = [
    "SUMMARY_CAP",
    "TEXT_CAP",
    "append_item",
    "append_notice",
    "items_after",
    "lock_conversation",
    "notify_items",
    "update_item",
]

#: The most of one reply an `assistant_text` item holds.
TEXT_CAP: Final = 64_000
#: The most of a tool's result a `tool` item holds: the Drawer shows a line, not the payload.
SUMMARY_CAP: Final = 2_000


async def append_item(
    session: AsyncSession,
    *,
    conversation_id: int,
    turn_id: int | None,
    kind: ChatItemKind,
    payload: JsonDict,
    final: bool = True,
) -> ChatItem:
    """The new row with every column loaded, `rev` included, in one round trip."""
    return (
        await session.scalars(
            insert(ChatItem)
            .values(
                conversation_id=conversation_id,
                turn_id=turn_id,
                kind=kind,
                payload=payload,
                final=final,
            )
            .returning(ChatItem)
        )
    ).one()


async def lock_conversation(session: AsyncSession, conversation_id: int) -> None:
    """The conversation's row lock, until the caller's transaction ends.

    Every item write takes it (the `chat_item_bump_rev` trigger), so one conversation's items
    commit in `rev` order. A transaction that also writes the conversation's turns, tokens or
    proposals takes it before any of those rows, as a delete does, so two such transactions
    queue on it rather than deadlock. NO KEY: an item insert's foreign-key check, which takes
    KEY SHARE, need not wait on it.
    """
    await session.execute(
        select(ChatConversation.id)
        .where(ChatConversation.id == conversation_id)
        .with_for_update(key_share=True)
    )


async def update_item(
    session: AsyncSession, item_id: int, *, payload: JsonDict, final: bool
) -> None:
    # The UPDATE would lock the item before its trigger locks the conversation; a delete takes
    # them the other way round, so the conversation goes first here.
    conversation_id = await session.scalar(
        select(ChatItem.conversation_id).where(ChatItem.id == item_id)
    )
    if conversation_id is not None:
        await lock_conversation(session, conversation_id)
    await session.execute(
        update(ChatItem).where(ChatItem.id == item_id).values(payload=payload, final=final)
    )


async def items_after(
    session: AsyncSession, conversation_id: int, after: int
) -> Sequence[ChatItem]:
    """Every item written or rewritten since `after`, each once, as it stands now."""
    return (
        await session.scalars(
            select(ChatItem)
            .where(ChatItem.conversation_id == conversation_id, ChatItem.rev > after)
            .order_by(ChatItem.rev)
        )
    ).all()


async def append_notice(
    session: AsyncSession,
    *,
    conversation_id: int,
    turn_id: int | None,
    text: str,
    reason: str | None = None,
) -> ChatItem:
    """One line the Drawer shows about a turn. `reason` is a closed word (a `TurnFailure`, or
    a `TurnStop` limit) the Drawer may key on."""
    payload: JsonDict = {"text": text} if reason is None else {"text": text, "reason": reason}
    return await append_item(
        session,
        conversation_id=conversation_id,
        turn_id=turn_id,
        kind=ChatItemKind.NOTICE,
        payload=payload,
    )


async def notify_items(session: AsyncSession, conversation_id: int) -> None:
    # pg_notify() rather than NOTIFY: NOTIFY takes an identifier, not a bind parameter.
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHAT_ITEMS_CHANNEL, "payload": str(conversation_id)},
    )
