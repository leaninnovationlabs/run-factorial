"""The SDK's `SessionStore` over the contract's `BackendStateStore`, so a conversation's session
lives in Postgres and a turn on a fresh host or after a restart resumes it.

The SDK keys a stored session by a `SessionKey` dict whose `project_key` comes from the child's
cwd; that is why the backend gives each conversation a stable working directory.
"""

from __future__ import annotations

import json
from typing import cast

from claude_agent_sdk.types import SessionKey, SessionStoreEntry

from runtime.assistant.contract import BackendStateStore

__all__ = ["StoreBridge"]


class StoreBridge:
    """Implements the pinned SDK's required `append` and `load`. The optional methods stay
    absent, which the SDK probes for and treats as unimplemented."""

    def __init__(self, state: BackendStateStore) -> None:
        self._state = state

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        await self._state.append(_key(key), entries)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        loaded = await self._state.load(_key(key))
        return None if loaded is None else [cast(SessionStoreEntry, dict(e)) for e in loaded]


def _key(key: SessionKey | str) -> str:
    return key if isinstance(key, str) else json.dumps(key, sort_keys=True)
