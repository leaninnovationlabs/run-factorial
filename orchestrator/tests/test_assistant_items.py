"""The chat item store: what a stream resumes from is stamped by the database, not the writer."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.assistant.items import append_item, update_item
from orchestrator.db.models import ChatItem, ChatItemKind
from tests.api_harness import seed_chat_turn

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_an_update_moves_the_rev_and_replaces_the_payload(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        turn = await seed_chat_turn(session)
        item = await append_item(
            session,
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
            kind=ChatItemKind.ASSISTANT_TEXT,
            payload={"text": "Two tick"},
            final=False,
        )
        await session.commit()
        appended_rev = item.rev
    assert appended_rev > 0

    async with maker() as session:
        await update_item(session, item.id, payload={"text": "Two tickets wait."}, final=True)
        await session.commit()
        updated = await session.get_one(ChatItem, item.id, populate_existing=True)

    assert updated.rev > appended_rev
    assert (updated.payload, updated.final) == ({"text": "Two tickets wait."}, True)
