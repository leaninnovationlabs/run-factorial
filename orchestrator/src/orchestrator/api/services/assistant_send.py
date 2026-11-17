"""Queueing a person's message as a turn, with every check a send makes.

The conversation route and the Assistant settings page's test share this one function, so the
test proves the same gate a real send meets. The caller holds the conversation's write lock
(`items.lock_conversation`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.errors import as_sentence
from orchestrator.api.services.organizations import conversation_organization_id
from orchestrator.assistant.attachments import (
    IMAGE_TYPES,
    AttachmentRefused,
    classify,
    safe_name,
)
from orchestrator.assistant.items import append_item, notify_items
from orchestrator.assistant.turns import budget_spent, enqueue_turn, live_turn
from orchestrator.assistant.vocabulary import takes_images
from orchestrator.db.models import ChatAttachment, ChatItemKind, ChatTurn, ChatTurnTrigger

__all__ = ["NO_IMAGES", "TURN_LIVE", "SendRefused", "queue_user_message"]

TURN_LIVE = "A turn is already queued or running in this conversation."
NO_IMAGES = (
    "The assistant's model connection takes no images. Attach a text file, or describe the image."
)


class SendRefused(Exception):
    """A send the assistant will not take: an HTTP status, a closed `code` and a `detail`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


async def queue_user_message(
    session: AsyncSession,
    conversation_id: int,
    owner: str,
    *,
    text: str,
    page: str | None,
    multi_org: bool,
    context: Sequence[Mapping[str, Any]] = (),
    attachments: Sequence[tuple[str, bytes]] = (),
) -> ChatTurn:
    """`attachments` are each file's name and bytes. A file the assistant will not take refuses
    the whole message before anything is written, so no turn runs on half of what was sent."""
    organization_id = await conversation_organization_id(session, conversation_id)
    assistant = await svc.load_assistant_settings(session, organization_id)
    not_ready = await svc.assistant_not_ready(
        session, organization_id, assistant, multi_org=multi_org
    )
    if not_ready is not None:
        raise SendRefused(409, "not_configured", not_ready)
    health = await svc.load_assistant_status(session)
    # No row yet is a process still starting: its first turn is only seconds away.
    own = None if health is None else svc.backend_status(health, assistant.backend)
    if own is not None and not own.get("ok"):
        raise SendRefused(
            503,
            "assistant_unavailable",
            "The assistant can't run a turn: "
            + as_sentence(str(own.get("message") or "its check failed")),
        )
    files = _checked(attachments, takes_images(assistant.backend, assistant.auth_mode))
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
    listed = [
        {"name": name, "media_type": media_type, "size": len(content)}
        for name, media_type, content in files
    ]
    item = await append_item(
        session,
        conversation_id=conversation_id,
        turn_id=turn.id,
        kind=ChatItemKind.USER_TEXT,
        # What was pointed at and what was attached stay with their message: the bubble shows
        # them, and the history tells the model of them again (`assistant/prompt.py`).
        payload={
            "text": text,
            "page": page,
            **({"context": list(context)} if context else {}),
            **({"attachments": listed} if listed else {}),
        },
    )
    session.add_all(
        ChatAttachment(
            item_id=item.id, name=name, media_type=media_type, size=len(content), content=content
        )
        for name, media_type, content in files
    )
    await session.flush()
    await notify_items(session, conversation_id)
    return turn


def _checked(
    attachments: Sequence[tuple[str, bytes]], images_go: bool
) -> list[tuple[str, str, bytes]]:
    """Each file as its kept name, what its bytes are, and the bytes."""
    files: list[tuple[str, str, bytes]] = []
    for given, content in attachments:
        name = safe_name(given)
        try:
            media_type = classify(name, content)
        except AttachmentRefused as refused:
            raise SendRefused(422, "attachment_refused", str(refused)) from refused
        if media_type in IMAGE_TYPES and not images_go:
            raise SendRefused(422, "attachment_refused", NO_IMAGES)
        files.append((name, media_type, content))
    return files
