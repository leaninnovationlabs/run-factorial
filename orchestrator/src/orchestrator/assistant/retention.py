"""Conversations are deleted `retain_days` after their last activity (tech-stack decision 63).

The clock is `chat_conversation.updated_at`, which an item insert moves through a trigger.
Deleting a conversation removes its items, turns, proposals and backend state by cascade. The
events its approvals caused stay, as every event row does.
A conversation with a queued or running turn is kept: its turn still writes to it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import CHAT_TURN_LIVE, ChatConversation, ChatTurn

__all__ = ["purge_expired"]


async def purge_expired(session: AsyncSession, *, now: datetime, retain_days: int) -> int:
    """How many conversations were deleted."""
    purged = await session.execute(
        delete(ChatConversation)
        .where(
            ChatConversation.updated_at < now - timedelta(days=retain_days),
            ~exists().where(
                ChatTurn.conversation_id == ChatConversation.id,
                ChatTurn.status.in_(CHAT_TURN_LIVE),
            ),
        )
        .returning(ChatConversation.id)
    )
    return len(purged.all())
