"""`BackendStateStore` over `chat_backend_state`: where a backend keeps its own session for a
conversation, so a turn after a restart, or on another host, resumes it.

Entries are opaque: only the adapter that wrote them reads them. Each append is one transaction,
numbered after the key's last entry. Appends to one key never race: one turn per conversation
runs at a time, and the adapter serialises its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, insert, select

from orchestrator.db.models import ChatBackendState
from orchestrator.db.session import SessionScope

__all__ = ["PostgresStateStore"]


class PostgresStateStore:
    def __init__(self, sessions: SessionScope, conversation_id: int) -> None:
        self._sessions = sessions
        self._conversation_id = conversation_id

    async def append(self, key: str, entries: Sequence[Mapping[str, Any]]) -> None:
        if not entries:
            return
        async with self._sessions() as session:
            last = await session.scalar(
                select(func.coalesce(func.max(ChatBackendState.seq), 0)).where(
                    ChatBackendState.conversation_id == self._conversation_id,
                    ChatBackendState.key == key,
                )
            )
            await session.execute(
                insert(ChatBackendState),
                [
                    {
                        "conversation_id": self._conversation_id,
                        "key": key,
                        "seq": (last or 0) + offset,
                        "entry": dict(entry),
                    }
                    for offset, entry in enumerate(entries, start=1)
                ],
            )

    async def load(self, key: str) -> list[Mapping[str, Any]] | None:
        async with self._sessions() as session:
            entries = (
                await session.scalars(
                    select(ChatBackendState.entry)
                    .where(
                        ChatBackendState.conversation_id == self._conversation_id,
                        ChatBackendState.key == key,
                    )
                    .order_by(ChatBackendState.seq)
                )
            ).all()
        return list(entries) or None
