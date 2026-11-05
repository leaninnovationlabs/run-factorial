"""Exit (decision 29): a human asks a ticket to leave its process, and the tick cancels its live
work and parks it as `exited`."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType
from orchestrator.events.types import REQUESTED_VIA_WORDS, RequestedVia, requested_via
from orchestrator.github import EXIT_LABELS, LABEL_EXIT
from orchestrator.github.state import IssueState
from orchestrator.worker.inputs import release_run_credential
from orchestrator.worker.runtime_adapters import RuntimeRefused
from orchestrator.worker.tick_context import _runs_for, _Tick
from runtime.contract import AgentRuntimeError

logger = logging.getLogger(__name__)


#: A restart or a redo answers every request about the run it discards.
RESETS: Final = (EventType.TICKET_RESTARTED, EventType.STEP_REDONE)

#: What an exit writes on the run it stops. Read back by the ticket page's attempt tag.
EXIT_CANCEL_REASON: Final = "cancelled: ticket exited"

#: The words the exit comment uses for each way in.
_EXIT_CAUSES: Final[dict[str, str]] = {
    **REQUESTED_VIA_WORDS,
    "label": f"the `{LABEL_EXIT}` label was added",
    "issue_closed": "the issue was closed",
}


async def _unanswered(
    session: AsyncSession, ticket_id: int, request: EventType, *answers: EventType
) -> int | None:
    """The id of a `request` with no answer recorded since it, or None.

    By event id, not by count: `ticket_exited` and its re-entry twins are also written for
    gestures nobody requested — a label, a closed issue — so a lifetime count of answers
    outruns the requests and a later request would be swallowed. Otherwise the shape of
    `interrupts._interrupt_pending`, ticket-scoped because these events carry no
    `step_run_id`: an append-only log cannot mark a request done in place.
    """
    return _asked_since(await _latest(session, ticket_id, request, *answers), request, *answers)


async def _latest(session: AsyncSession, ticket_id: int, *types: EventType) -> dict[str, int]:
    """The newest event id of each of `types` on the ticket, in one query."""
    return {
        str(kind): int(event_id)
        for kind, event_id in (
            await session.execute(
                select(Event.type, func.max(Event.id))
                .where(Event.ticket_id == ticket_id, Event.type.in_(types))
                .group_by(Event.type)
            )
        ).all()
    }


def _asked_since(latest: Mapping[str, int], request: EventType, *answers: EventType) -> int | None:
    asked = latest.get(request)
    if asked is None or asked <= max((latest.get(answer, 0) for answer in answers), default=0):
        return None
    return asked


async def _request_event(
    session: AsyncSession, latest: Mapping[str, int], request: EventType, *answers: EventType
) -> Event | None:
    """`_asked_since` over a `_latest` read shared by several requests, as the event."""
    asked = _asked_since(latest, request, *answers)
    return None if asked is None else await session.get(Event, asked)


async def pending_request(
    session: AsyncSession, ticket_id: int, request: EventType, *answers: EventType
) -> Event | None:
    """The newest `request` with no answer recorded since it, as the event."""
    asked = await _unanswered(session, ticket_id, request, *answers)
    return None if asked is None else await session.get(Event, asked)


async def request_run(tick: _Tick, request: Event) -> StepRun | None:
    """The run `request` names, if any."""
    if request.step_run_id is None:
        return None
    return await tick.session.get(StepRun, request.step_run_id)


async def _pending(
    session: AsyncSession, ticket_id: int, request: EventType, *answers: EventType
) -> bool:
    return await _unanswered(session, ticket_id, request, *answers) is not None


async def _request_via(session: AsyncSession, request_id: int) -> RequestedVia:
    return requested_via(
        await session.scalar(select(Event.details).where(Event.id == request_id))
    )


async def _exit_wanted(
    session: AsyncSession, ticket: Ticket, issue: IssueState, terminal_step: str
) -> str | None:
    """Which of the ways in fired, or None (decision 29): a request names its surface.

    Observations first, the event log last, so a steady tick costs one query at most.
    """
    if (
        issue.state == "closed"
        and ticket.current_step != terminal_step
        and ticket.status != TicketStatus.WAITING_GATE
    ):
        # Not at the terminal step and not waiting on a gate: an issue closed while work remains
        # is a departure. At the terminal step it is the opposite — the build agent's own
        # `Closes #N` closes the issue when the spec PR merges, and the terminal step runs on
        # its own tick. At a gate it is the same merge seen one tick earlier: a reviewer who
        # merged between polls has closed the issue before the gate could observe the approval,
        # and the gate's own evaluation — not an exit — is what that tick owes it (decision 29).
        # A label or a request still exits either: those are gestures, not a side effect of
        # merging.
        return "issue_closed"
    if issue.labels & EXIT_LABELS:
        return "label"
    asked = await _unanswered(
        session, ticket.id, EventType.EXIT_REQUESTED, EventType.TICKET_EXITED
    )
    return None if asked is None else await _request_via(session, asked)


_LIVE: Final = frozenset({StepRunStatus.RUNNING, StepRunStatus.IDLE, StepRunStatus.WAITING})


@dataclass(frozen=True, slots=True)
class LiveRunStop:
    """What `cancel_live_run` found at the current step and did to it."""

    #: The latest current-generation run at the step, live or not.
    latest: StepRun | None
    #: The row marked CANCELLED, if any.
    cancelled: StepRun | None
    #: The substrate's error when it could not confirm the stop.
    unstoppable: str | None


async def cancel_live_run(tick: _Tick, *, reason: str, force: bool) -> LiveRunStop:
    """Stop the latest run at the current step while it is RUNNING, IDLE or WAITING: an agent
    run through the adapter, a check's or a gate's wait by marking the row alone. A run already
    terminal — an interrupted one included — is left as it is.

    `force` marks the row CANCELLED even when the substrate could not confirm the stop: exit's
    choice, because an exit is the lever pulled when the substrate misbehaves. Without it the
    row is left as it was, for a caller that must not start a second agent beside one that may
    still be live."""
    step_name = tick.ticket.current_step
    if step_name == tick.config.terminal.step:
        return LiveRunStop(latest=None, cancelled=None, unstoppable=None)
    runs = await _runs_for(tick.session, tick.ticket, step_name)
    latest = runs[-1] if runs else None
    if latest is None or StepRunStatus(latest.status) not in _LIVE:
        return LiveRunStop(latest=latest, cancelled=None, unstoppable=None)
    unstoppable: str | None = None
    if latest.kind == StepKind.AGENT and latest.runtime_run_id is not None:
        try:
            adapter = await tick.deps.adapter_for_run(tick.session, latest)
            await adapter.cancel(latest.runtime_run_id)
        except (AgentRuntimeError, RuntimeRefused) as exc:
            logger.exception(
                "ticket %s: could not cancel run %s",
                tick.ticket.external_key,
                latest.runtime_run_id,
            )
            unstoppable = str(exc)
    if unstoppable is not None and not force:
        return LiveRunStop(latest=latest, cancelled=None, unstoppable=unstoppable)
    latest.status = StepRunStatus.CANCELLED
    latest.ended_at = tick.now
    latest.failure_reason = reason
    await release_run_credential(tick, latest.id)
    return LiveRunStop(latest=latest, cancelled=latest, unstoppable=unstoppable)


async def _exit(tick: _Tick, *, via: str) -> None:
    """Take the ticket off the floor: cancel live work, park it as `exited` (decision 29).

    Live work is cancelled by `cancel_live_run` with `force`: the ticket leaves even when the
    substrate cannot confirm the stop, and the comment names the run. An interrupted run keeps its
    workspace and resumes on re-entry exactly as it would have from blocked. Cancel rather than
    interrupt because an exit is a departure, and a kept worktree for a ticket that may never
    return is the cost decision 26 refused to pay for every run.
    """
    step_name = tick.ticket.current_step
    # Read before `TICKET_EXITED` answers it, whichever way in fired: a Close and exit asks for
    # the issue too (decision 79).
    request = await pending_request(
        tick.session, tick.ticket.id, EventType.EXIT_REQUESTED, EventType.TICKET_EXITED
    )
    close = request is not None and request.details.get("close_issue") is True
    stop = await cancel_live_run(tick, reason=EXIT_CANCEL_REASON, force=True)
    cancelled, latest, unstoppable = stop.cancelled, stop.latest, stop.unstoppable

    tick.ticket.status = TicketStatus.EXITED
    # The fact every read model filters on: the project board, the open-only ticket list and the
    # factory board's aging window all read `closed_at`, so this one assignment is what seats an
    # exited ticket with Done everywhere. Re-entry clears it.
    tick.ticket.closed_at = tick.now
    tick.plan.label = LABEL_EXIT
    await tick.emit(
        EventType.TICKET_EXITED,
        run=cancelled,
        step=step_name,
        # The LATEST attempt, not the cancelled one: the ticket page keys a timeline block on
        # this pair, and a blocked ticket leaves behind a FAILED run that nothing cancelled —
        # keying on the cancellation would open an attempt-less block beside the real one.
        attempt=latest.attempt if latest is not None else None,
        cancelled=cancelled is not None,
        via=via,
    )
    stopped = (
        f" The `{step_name}` attempt {cancelled.attempt} that was in flight was canceled."
        if cancelled is not None
        else ""
    )
    if unstoppable is not None and latest is not None:
        stopped += (
            f" The running agent (`{latest.runtime_run_id}`) couldn't be stopped; check it by "
            "hand."
        )
    if close:
        tick.plan.close_issue = True
    closing = " The issue closes with it, as asked." if close else ""
    reopen = "reopen the issue, then " if via == "issue_closed" or close else ""
    tick.plan.comments.append(
        f"Left the process at `{step_name}` ({_EXIT_CAUSES[via]}).{closing} Nothing runs "
        f"for this ticket while it is exited.{stopped}\n\nTo bring it back, {reopen}remove the "
        f"`{LABEL_EXIT}` label or press Re-enter on the dashboard. That grants a fresh attempt "
        f"at `{step_name}`."
    )
