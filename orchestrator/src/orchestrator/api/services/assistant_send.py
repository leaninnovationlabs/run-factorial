"""Queueing a person's message as a turn, with every check a send makes.

The conversation route and the Assistant settings page's test share this one function, so the
test proves the same gate a real send meets. The caller holds the conversation's write lock
(`items.lock_conversation`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.errors import as_sentence
from orchestrator.api.services.organizations import conversation_organization_id
from orchestrator.assistant.items import append_item, notify_items
from orchestrator.assistant.turns import NOT_SET_UP, budget_spent, enqueue_turn, live_turn
from orchestrator.db.base import JsonDict
from orchestrator.db.models import ChatItemKind, ChatTurn, ChatTurnTrigger

__all__ = ["TURN_LIVE", "SendRefused", "queue_user_message"]

TURN_LIVE = "A turn is already queued or running in this conversation."


class SendRefused(Exception):
    """A send the assistant will not take: an HTTP status, a closed `code` and a `detail`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _backend_ok(health: JsonDict, backend: str) -> bool:
    """The organization's own backend passed its check. A row written before the check was per
    backend answers for every backend at once."""
    backends = health.get("backends")
    if isinstance(backends, dict) and isinstance(backends.get(backend), dict):
        return bool(backends[backend].get("ok"))
    return bool(health.get("ok"))


async def queue_user_message(
    session: AsyncSession,
    conversation_id: int,
    owner: str,
    *,
    text: str,
    page: str | None,
    multi_org: bool,
) -> ChatTurn:
    organization_id = await conversation_organization_id(session, conversation_id)
    assistant = await svc.load_assistant_settings(session, organization_id)
    if not await svc.assistant_ready_to_send(
        session, organization_id, assistant, multi_org=multi_org
    ):
        raise SendRefused(409, "not_configured", NOT_SET_UP)
    health = await svc.load_assistant_status(session)
    # No row yet is a process still starting: its first turn is only seconds away.
    if health is not None and not _backend_ok(health, assistant.backend):
        raise SendRefused(
            503,
            "assistant_unavailable",
            "The assistant can't run a turn: "
            + as_sentence(str(health.get("message") or "its check failed")),
        )
    if await live_turn(session, conversation_id) is not None:
        raise SendRefused(409, "turn_live", TURN_LIVE)
    spent = await budget_spent(session, organization_id, owner, assistant, now=datetime.now(UTC))
    if spent == "person":
        raise SendRefused(
            429,
            "daily_budget",
            f"Today's assistant budget of {assistant.daily_budget_usd} USD is spent.",
        )
    if spent == "organization":
        raise SendRefused(
            429,
            "daily_budget",
            f"Today's assistant budget of {assistant.organization_daily_budget_usd} USD for this "
            "organization is spent.",
        )
    turn = await enqueue_turn(session, conversation_id, trigger=ChatTurnTrigger.USER)
    if turn is None:
        # A queued turn the check above did not see: only its meaning is certain.
        raise SendRefused(409, "turn_live", TURN_LIVE)
    await append_item(
        session,
        conversation_id=conversation_id,
        turn_id=turn.id,
        kind=ChatItemKind.USER_TEXT,
        payload={"text": text, "page": page},
    )
    await notify_items(session, conversation_id)
    return turn
