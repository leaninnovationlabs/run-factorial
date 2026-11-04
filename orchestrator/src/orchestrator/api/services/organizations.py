"""Which organization a request or a conversation acts in (design.md §2.11).

A request acts in its path's `{org}` (tech-stack decision 68). A conversation carries its own: its
turns read that organization's assistant settings, credential and budget, never the default's.
Revision 0027 seeds one organization, `default`; with multi-org on, people create more
(`membership.py`).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import ChatConversation, Organization

__all__ = ["DEFAULT_ORGANIZATION", "conversation_organization_id", "default_organization_id"]

#: The slug revision 0027 seeds. Immutable like every slug; its name can change.
DEFAULT_ORGANIZATION: Final = "default"


async def default_organization_id(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(Organization.id).where(Organization.slug == DEFAULT_ORGANIZATION)
        )
    ).scalar_one()


async def conversation_organization_id(session: AsyncSession, conversation_id: int) -> int:
    return (
        await session.execute(
            select(ChatConversation.organization_id).where(ChatConversation.id == conversation_id)
        )
    ).scalar_one()
