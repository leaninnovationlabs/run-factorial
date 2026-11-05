"""The assistant's proposals: a write the model asked for, held until its conversation's owner
decides it (design.md §7.18).

A proposal stores the arguments its twin was called with, as JSON, and a card in the
conversation. Approve runs those arguments once, through the `run_*` function behind the MCP
write tool, as `Actor.chat`: the services the dashboard's buttons call, so a refusal reads the
same on every surface. The approval is committed before anything runs, so a second click finds
it decided even while the first is still running. Decline and expiry run nothing.

Lock order is the one `items.lock_conversation` names: the conversation's row first, then the
proposal's.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, get_type_hints

from fastmcp.tools.base import ToolResult
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.assistant_settings import load_assistant_settings
from orchestrator.api.services.errors import Conflict, NotFound, ServiceError, render
from orchestrator.api.services.organizations import conversation_organization_id
from orchestrator.assistant.items import (
    append_item,
    append_notice,
    lock_conversation,
    notify_items,
    update_item,
)
from orchestrator.assistant.turns import budget_spent, enqueue_turn
from orchestrator.db.base import JsonDict
from orchestrator.db.models import (
    ChatConversation,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatTurnTrigger,
)
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.mcp_server.context import ToolContext
from orchestrator.mcp_server.tools import ALL_TOOLS
from orchestrator.mcp_server.tools.config import run_config_commit
from orchestrator.mcp_server.tools.controls import (
    run_ticket_exit,
    run_ticket_interrupt,
    run_ticket_reenter,
    run_ticket_requeue,
    run_ticket_restart,
    run_ticket_resume,
    run_ticket_retry,
    run_ticket_unblock,
)
from orchestrator.mcp_server.tools.tickets import run_ticket_create

__all__ = [
    "EXECUTORS",
    "Expired",
    "Unrunnable",
    "approve",
    "decline",
    "expire_stale",
    "outcome_line",
    "proposal_organization_id",
    "record_proposal",
]

#: Each write tool's body, as both surfaces call it: the tool's own arguments after `tc, actor`.
EXECUTORS: Final[Mapping[str, Callable[..., Awaitable[ToolResult]]]] = MappingProxyType(
    {
        "factorial_ticket_exit": run_ticket_exit,
        "factorial_ticket_reenter": run_ticket_reenter,
        "factorial_ticket_restart": run_ticket_restart,
        "factorial_ticket_interrupt": run_ticket_interrupt,
        "factorial_ticket_resume": run_ticket_resume,
        "factorial_ticket_unblock": run_ticket_unblock,
        "factorial_ticket_retry": run_ticket_retry,
        "factorial_ticket_requeue": run_ticket_requeue,
        "factorial_ticket_create": run_ticket_create,
        "factorial_config_commit": run_config_commit,
    }
)

log = logging.getLogger(__name__)

_TITLES: Final = {spec.name: spec.title for spec in ALL_TOOLS}
_NO_SUCH: Final = "No such proposal."
#: What to look at after a run that failed unexpectedly; every other write acts on a ticket.
_LOOK_AT: Final = {"factorial_config_commit": "the factorial branch"}
_BUDGET_SPENT: Final = {
    "person": "Your daily budget for the assistant is spent, so it will not reply until tomorrow.",
    "organization": (
        "The organization's daily budget for the assistant is spent, so it will not reply until "
        "tomorrow."
    ),
}


class Expired(ServiceError):
    """A proposal past its time: nothing runs it now."""

    status = 410


class Unrunnable(Conflict):
    """A pending proposal for a tool a later deploy removed or renamed. It stays pending, so the
    person can still decline it."""


def _card(proposal: ChatProposal) -> JsonDict:
    """What the Drawer draws: the action, its target, and where the decision stands."""
    return {
        "proposal_id": proposal.id,
        "tool": proposal.tool,
        # A later deploy may have renamed the tool a stored proposal names.
        "title": _TITLES.get(proposal.tool, proposal.tool),
        "preview": proposal.preview,
        "status": proposal.status,
        "expires_at": proposal.expires_at.isoformat(),
        "decided_by": proposal.decided_by,
        "decided_at": None if proposal.decided_at is None else proposal.decided_at.isoformat(),
        "result": proposal.result,
    }


async def record_proposal(
    session: AsyncSession,
    *,
    scope: ChatScope,
    tool: str,
    args: JsonDict,
    preview: JsonDict,
    ttl: timedelta,
    now: datetime,
) -> ChatProposal:
    """A pending proposal and its card in the turn's conversation. The card is written before the
    proposal, which names it, and redrawn once the proposal has its id."""
    await lock_conversation(session, scope.conversation_id)
    card = await append_item(
        session,
        conversation_id=scope.conversation_id,
        turn_id=scope.turn_id,
        kind=ChatItemKind.PROPOSAL,
        payload={},
    )
    proposal = ChatProposal(
        conversation_id=scope.conversation_id,
        turn_id=scope.turn_id,
        item_id=card.id,
        tool=tool,
        args=args,
        preview=preview,
        status=ChatProposalStatus.PENDING,
        expires_at=now + ttl,
    )
    session.add(proposal)
    await session.flush()
    await _redraw(session, proposal)
    return proposal


async def _locked(session: AsyncSession, proposal_id: int) -> ChatProposal | None:
    return (
        await session.scalars(
            select(ChatProposal)
            .where(ChatProposal.id == proposal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()


async def proposal_organization_id(session: AsyncSession, proposal_id: int) -> int:
    """The organization a proposal's write acts in: its conversation's, as its turn's tools did."""
    organization_id = await session.scalar(
        select(ChatConversation.organization_id)
        .join(ChatProposal, ChatProposal.conversation_id == ChatConversation.id)
        .where(ChatProposal.id == proposal_id)
    )
    if organization_id is None:
        raise NotFound(_NO_SUCH)
    return organization_id


async def _decidable(
    session: AsyncSession, proposal_id: int, *, caller: str, now: datetime
) -> ChatProposal:
    """The pending proposal, locked, if `caller` owns its conversation. Anyone else's reads as
    missing. One found past its time is expired here, and committed, before the refusal."""
    conversation_id = await session.scalar(
        select(ChatProposal.conversation_id).where(ChatProposal.id == proposal_id)
    )
    if conversation_id is None:
        raise NotFound(_NO_SUCH)
    await lock_conversation(session, conversation_id)
    owner = await session.scalar(
        select(ChatConversation.owner).where(ChatConversation.id == conversation_id)
    )
    proposal = await _locked(session, proposal_id)
    if proposal is None or owner != caller:
        raise NotFound(_NO_SUCH)
    if proposal.status == ChatProposalStatus.PENDING and proposal.expires_at <= now:
        _decide(proposal, ChatProposalStatus.EXPIRED, by=None, now=now)
        await _redraw(session, proposal)
        await session.commit()
    if proposal.status == ChatProposalStatus.EXPIRED:
        raise Expired(f"Proposal P{proposal.id} expired. Ask the assistant to propose it again.")
    if proposal.status != ChatProposalStatus.PENDING:
        raise Conflict(f"Proposal P{proposal.id} is already {proposal.status}.")
    return proposal


def _decide(
    proposal: ChatProposal, status: ChatProposalStatus, *, by: str | None, now: datetime
) -> None:
    proposal.status = status
    proposal.decided_by = by
    proposal.decided_at = now


async def _redraw(session: AsyncSession, proposal: ChatProposal) -> None:
    await update_item(session, proposal.item_id, payload=_card(proposal), final=True)
    await notify_items(session, proposal.conversation_id)


def _typed(run: Callable[..., Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """The stored JSON as the types `run` takes: a config commit's files and edits become their
    models again."""
    hints = get_type_hints(run)
    return {name: TypeAdapter(hints[name]).validate_python(value) for name, value in args.items()}


def outcome_line(tool: str, result: JsonDict) -> str:
    """What the person reads once a proposal ran: their words for the result the model reads in
    full. A `RequestResultOut` has `landed`, a `TicketCreateOut` `admitted`, a `ConfigCommitOut`
    `committed`."""
    ref = result.get("ref")
    line = f"Approved: {_TITLES.get(tool, tool)}" + (f" for {ref}." if ref else ".")
    if "landed" in result:
        if result["landed"]:
            line += f" Done: the ticket is {result.get('ticket_status')}."
        else:
            line += " The worker has not acted on it yet."
    elif "admitted" in result:
        line += " It is a ticket now." if result["admitted"] else " It is not a ticket yet."
    elif "committed" in result:
        if result["committed"]:
            line += f" Committed {str(result.get('sha'))[:7]}."
        else:
            line += " The branch already had this config."
        if result.get("sync_error"):
            line += " The sync failed; the card shows why."
    return line


async def _settle(
    session: AsyncSession,
    proposal_id: int,
    conversation_id: int,
    *,
    status: ChatProposalStatus,
    result: JsonDict,
    line: str,
    owner: str,
    now: datetime,
    item_id: int,
) -> ChatProposal:
    """The run's outcome on the proposal and its card, a notice, and the continuation turn
    unless a daily budget, the owner's or the organization's, is spent. A conversation deleted
    as the run went takes the card with it, so only the outcome comes back."""
    await lock_conversation(session, conversation_id)
    settled = await _locked(session, proposal_id)
    if settled is None:
        log.warning(
            "proposal P%s ended %s, but its conversation was deleted as it ran", proposal_id, status
        )
        await session.commit()
        return ChatProposal(id=proposal_id, item_id=item_id, status=status, result=result)
    settled.status = status
    settled.result = result
    await _redraw(session, settled)
    organization_id = await conversation_organization_id(session, conversation_id)
    assistant = await load_assistant_settings(session, organization_id)
    spent = await budget_spent(session, organization_id, owner, assistant, now=now)
    await append_notice(
        session,
        conversation_id=conversation_id,
        turn_id=None,
        text=f"{line} {_BUDGET_SPENT[spent]}" if spent else line,
    )
    if not spent:
        await enqueue_turn(session, conversation_id, trigger=ChatTurnTrigger.APPROVAL)
    await session.commit()
    return settled


async def approve(
    session: AsyncSession, proposal_id: int, *, caller: str, tc: ToolContext, now: datetime
) -> ChatProposal:
    """Run the stored arguments once, as `caller`; `tc.session` is `session`. A service refusal
    ends the proposal `refused` with the service's words, as an answer, not an error. A fault
    leaves it `approved`, because nobody knows what ran, and is raised once the card says so.
    Every outcome gets a notice and a continuation turn, so the model reads it."""
    proposal = await _decidable(session, proposal_id, caller=caller, now=now)
    conversation_id, item_id = proposal.conversation_id, proposal.item_id
    tool, args = proposal.tool, proposal.args
    run = EXECUTORS.get(tool)
    if run is None:
        raise Unrunnable("This proposal names a tool this deployment no longer has.")
    _decide(proposal, ChatProposalStatus.APPROVED, by=caller, now=now)
    await _redraw(session, proposal)
    await session.commit()

    def settle(status: ChatProposalStatus, result: JsonDict, line: str) -> Awaitable[ChatProposal]:
        return _settle(
            session,
            proposal_id,
            conversation_id,
            status=status,
            result=result,
            line=line,
            owner=caller,
            now=now,
            item_id=item_id,
        )

    try:
        done = await run(tc, Actor.chat(caller, proposal_id), **_typed(run, args))
    except ServiceError as exc:
        # Whatever the refused call left uncommitted is not the factory's to keep.
        await session.rollback()
        return await settle(
            ChatProposalStatus.REFUSED,
            {"error": render(exc)},
            f"Approved, but refused: {exc.message.splitlines()[0]}",
        )
    except Exception as exc:
        await session.rollback()
        await settle(
            ChatProposalStatus.APPROVED,
            {"error": f"the run failed unexpectedly ({type(exc).__name__})"},
            "Approved, but the run failed unexpectedly; "
            f"check {_LOOK_AT.get(tool, 'the ticket')} before asking again.",
        )
        raise
    result = dict(done.structured_content or {})
    return await settle(ChatProposalStatus.SUCCEEDED, result, outcome_line(tool, result))


async def decline(
    session: AsyncSession, proposal_id: int, *, caller: str, now: datetime
) -> ChatProposal:
    """No turn follows: the model reads the decision when the conversation next runs."""
    proposal = await _decidable(session, proposal_id, caller=caller, now=now)
    _decide(proposal, ChatProposalStatus.DECLINED, by=caller, now=now)
    await _redraw(session, proposal)
    await session.commit()
    return proposal


async def expire_stale(session: AsyncSession, *, now: datetime) -> int:
    """End every pending proposal past its time. Each conversation's lock comes before its
    proposal's, in conversation order, as `turns.recover_lost_turns` takes them."""
    candidates = (
        await session.execute(
            select(ChatProposal.id, ChatProposal.conversation_id)
            .where(
                ChatProposal.status == ChatProposalStatus.PENDING,
                ChatProposal.expires_at <= now,
            )
            .order_by(ChatProposal.conversation_id, ChatProposal.id)
        )
    ).all()
    expired = 0
    for proposal_id, conversation_id in candidates:
        await lock_conversation(session, conversation_id)
        proposal = await _locked(session, proposal_id)
        if proposal is None or proposal.status != ChatProposalStatus.PENDING:
            continue
        _decide(proposal, ChatProposalStatus.EXPIRED, by=None, now=now)
        await _redraw(session, proposal)
        expired += 1
    return expired
