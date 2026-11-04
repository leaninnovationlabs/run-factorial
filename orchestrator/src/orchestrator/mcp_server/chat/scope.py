"""Which conversation a chat tool draws in: the turn the call belongs to, while it runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select

from orchestrator.api.services.errors import Conflict
from orchestrator.db.models import ChatConversation, ChatTurn, ChatTurnStatus
from orchestrator.mcp_server.context import ToolContext

__all__ = ["NOT_RUNNING", "ChatScope", "chat_scope"]

NOT_RUNNING: Final = "This assistant turn is not running."


@dataclass(frozen=True)
class ChatScope:
    turn_id: int
    conversation_id: int
    owner: str
    #: The conversation's organization: the turn's tools resolve within it alone.
    organization_id: int


async def chat_scope(tc: ToolContext) -> ChatScope:
    """Refuses a turn that has ended, though the runner stops its backend first: a card must
    land in a turn the Drawer is still drawing."""
    turn = tc.turn
    if turn is None:
        raise Conflict("This tool runs only inside an assistant turn.")
    row = (
        await tc.session.execute(
            select(ChatTurn.status, ChatConversation.owner)
            .join(ChatConversation, ChatConversation.id == ChatTurn.conversation_id)
            .where(ChatTurn.id == turn.turn_id, ChatTurn.conversation_id == turn.conversation_id)
        )
    ).one_or_none()
    if row is None or row.status != ChatTurnStatus.RUNNING:
        raise Conflict(NOT_RUNNING)
    if row.owner != turn.owner:
        raise Conflict("This assistant turn belongs to another person.")
    return turn
