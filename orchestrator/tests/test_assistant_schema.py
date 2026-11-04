"""The assistant's tables (revision 0024): the guarantees the database itself gives the chat."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import (
    ChatConversation,
    ChatItem,
    ChatTurn,
    McpAccess,
    McpMintedVia,
    McpToken,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

OWNER = "operator@example.com"
DEFAULT_ORG = "(SELECT id FROM organization WHERE slug = 'default')"


async def _conversation(session: AsyncSession) -> ChatConversation:
    conv = ChatConversation(
        organization_id=await default_organization_id(session),
        owner=OWNER,
        backend_kind="claude_sdk",
    )
    session.add(conv)
    await session.flush()
    return conv


async def test_one_queued_and_one_running_turn_per_conversation(
    async_session: AsyncSession,
) -> None:
    conv = await _conversation(async_session)
    async_session.add(ChatTurn(conversation_id=conv.id, seq=1, status="queued", trigger="user"))
    async_session.add(ChatTurn(conversation_id=conv.id, seq=2, status="running", trigger="user"))
    await async_session.flush()
    async_session.add(ChatTurn(conversation_id=conv.id, seq=3, status="queued", trigger="approval"))
    with pytest.raises(IntegrityError, match="chat_turn_one_queued"):
        await async_session.flush()


async def test_a_second_running_turn_is_refused_too(async_session: AsyncSession) -> None:
    conv = await _conversation(async_session)
    async_session.add(ChatTurn(conversation_id=conv.id, seq=1, status="running", trigger="user"))
    await async_session.flush()
    async_session.add(ChatTurn(conversation_id=conv.id, seq=2, status="running", trigger="user"))
    with pytest.raises(IntegrityError, match="chat_turn_one_running"):
        await async_session.flush()


async def test_finished_turns_are_not_limited(async_session: AsyncSession) -> None:
    conv = await _conversation(async_session)
    for seq in (1, 2, 3):
        async_session.add(ChatTurn(conversation_id=conv.id, seq=seq, status="done", trigger="user"))
    await async_session.flush()


async def test_a_turn_status_outside_the_vocabulary_is_refused(
    async_session: AsyncSession,
) -> None:
    conv = await _conversation(async_session)
    async_session.add(ChatTurn(conversation_id=conv.id, seq=1, status="paused", trigger="user"))
    with pytest.raises(IntegrityError, match="ck_chat_turn_status"):
        await async_session.flush()


async def test_item_rev_moves_on_every_write(async_session: AsyncSession) -> None:
    conv = await _conversation(async_session)
    item = ChatItem(
        conversation_id=conv.id, kind="assistant_text", payload={"text": "a"}, final=False
    )
    async_session.add(item)
    await async_session.flush()
    await async_session.refresh(item)
    first = item.rev
    item.payload = {"text": "ab"}
    await async_session.flush()
    await async_session.refresh(item)
    assert item.rev > first


async def test_updated_at_moves_when_a_conversation_does(async_session: AsyncSession) -> None:
    conv = await _conversation(async_session)
    await async_session.execute(
        text("UPDATE chat_conversation SET updated_at = :then WHERE id = :id"),
        {"then": datetime.now(UTC) - timedelta(days=40), "id": conv.id},
    )
    await async_session.execute(
        text("UPDATE chat_conversation SET title = 'x' WHERE id = :id"), {"id": conv.id}
    )
    stamp = (
        await async_session.execute(
            text("SELECT updated_at FROM chat_conversation WHERE id = :id"), {"id": conv.id}
        )
    ).scalar_one()
    assert stamp > datetime.now(UTC) - timedelta(minutes=1)


async def _aged_conversation(session: AsyncSession) -> tuple[int, datetime]:
    """Inserted with an old `updated_at`: the BEFORE UPDATE trigger would overwrite it later."""
    then = datetime.now(UTC) - timedelta(days=40)
    conv_id = (
        await session.execute(
            text(
                "INSERT INTO chat_conversation (organization_id, owner, backend_kind, updated_at) "
                f"VALUES ({DEFAULT_ORG}, :owner, 'claude_sdk', :then) RETURNING id"
            ),
            {"owner": OWNER, "then": then},
        )
    ).scalar_one()
    return conv_id, then


async def _conversation_stamp(session: AsyncSession, conv_id: int) -> datetime:
    return (
        await session.execute(
            text("SELECT updated_at FROM chat_conversation WHERE id = :id"), {"id": conv_id}
        )
    ).scalar_one()


async def test_an_item_insert_touches_its_conversation(async_session: AsyncSession) -> None:
    conv_id, then = await _aged_conversation(async_session)
    assert await _conversation_stamp(async_session, conv_id) == then
    async_session.add(ChatItem(conversation_id=conv_id, kind="user_text", payload={"text": "hi"}))
    await async_session.flush()
    assert await _conversation_stamp(async_session, conv_id) > datetime.now(UTC) - timedelta(
        minutes=1
    )


async def test_an_item_update_does_not_touch_its_conversation(
    async_session: AsyncSession,
) -> None:
    conv_id, then = await _aged_conversation(async_session)
    await async_session.execute(
        text("ALTER TABLE chat_item DISABLE TRIGGER chat_item_touch_conversation")
    )
    item = ChatItem(conversation_id=conv_id, kind="assistant_text", payload={"text": "a"})
    async_session.add(item)
    await async_session.flush()
    await async_session.execute(
        text("ALTER TABLE chat_item ENABLE TRIGGER chat_item_touch_conversation")
    )
    item.payload = {"text": "ab"}
    await async_session.flush()
    assert await _conversation_stamp(async_session, conv_id) == then


async def test_deleting_a_conversation_keeps_its_token_rows(async_session: AsyncSession) -> None:
    conv = await _conversation(async_session)
    turn = ChatTurn(conversation_id=conv.id, seq=1, status="running", trigger="user")
    async_session.add(turn)
    await async_session.flush()
    token = McpToken(
        organization_id=conv.organization_id,
        jti_sha256="a" * 64,
        audience="aud",
        label="chat",
        created_by=OWNER,
        minted_via=McpMintedVia.CHAT,
        access=McpAccess.CHAT,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        chat_turn_id=turn.id,
    )
    async_session.add(token)
    await async_session.flush()

    await async_session.execute(
        text("DELETE FROM chat_conversation WHERE id = :id"), {"id": conv.id}
    )
    await async_session.refresh(token)
    assert token.chat_turn_id is None
    # Its own row only: other tests on this database commit tokens of their own.
    kept = select(McpToken.id).where(McpToken.id == token.id)
    assert (await async_session.execute(kept)).scalar_one() == token.id


async def test_chat_token_scopes_never_include_write() -> None:
    row = McpToken(access=McpAccess.CHAT, minted_via=McpMintedVia.CHAT, created_by=OWNER)
    assert row.scopes() == ["factorial:read", "factorial:chat"]
    assert row.principal == OWNER


def _tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def test_migration_round_trips(migration_database: tuple[Config, AsyncEngine]) -> None:
    config, engine = migration_database
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO mcp_token (organization_id, jti_sha256, audience, label, created_by, "
                "minted_via, access, expires_at) "
                f"VALUES ({DEFAULT_ORG}, 'c', 'a', 'l', 'operator', 'chat', 'chat', now()), "
                f"({DEFAULT_ORG}, 'r', 'a', 'l', 'operator', 'session', 'read', now())"
            )
        )
        # A proposal that points at the chat token: the downgrade must drop it first.
        await connection.execute(
            text(
                "WITH conv AS (INSERT INTO chat_conversation "
                f"(organization_id, owner, backend_kind) VALUES ({DEFAULT_ORG}, 'operator', "
                "'claude_sdk') RETURNING id), "
                "item AS (INSERT INTO chat_item (conversation_id, kind, payload) "
                "SELECT id, 'proposal', '{}'::jsonb FROM conv RETURNING id, conversation_id) "
                "INSERT INTO chat_proposal (conversation_id, item_id, tool, args, preview, "
                "status, mcp_token_id, expires_at) SELECT item.conversation_id, item.id, 't', "
                "'{}'::jsonb, '{}'::jsonb, 'pending', "
                "(SELECT id FROM mcp_token WHERE jti_sha256 = 'c'), now() FROM item"
            )
        )
    try:
        await asyncio.to_thread(command.downgrade, config, "0023")
        async with engine.connect() as connection:
            assert "chat_turn" not in await connection.run_sync(_tables)
            kept = (
                (await connection.execute(text("SELECT jti_sha256 FROM mcp_token"))).scalars().all()
            )
        assert kept == ["r"]

        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            assert {"app_setting", "chat_turn", "chat_backend_state"} <= await connection.run_sync(
                _tables
            )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_conversation"))
            await connection.execute(text("DELETE FROM mcp_token"))
