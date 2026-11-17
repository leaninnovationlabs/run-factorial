"""Retry (decision 77): a person's press starts a failed step's next attempt now, and an agent
step's attempt continues the failed attempt's session where it can. Where it cannot, the attempt
starts fresh, told why the failed one was not accepted and what it delivered.

One attempt per press, and nothing here counts it: `_ordinal` counts the generation's rows, so
the next failure re-escalates, or, during a backoff, the press takes the attempt the ladder
booked. Retry takes every `_FAILED_STATUSES` run, CANCELLED included, so a run an exit cancelled
qualifies too once the ticket has re-entered at its step.

The request is answered once: by the attempt's `step_started`, or by `STEP_RETRY_REFUSED`. A
redo, a restart or an exit answers it too. No request raises out of the tick: an unreadable
precondition leaves it pending until `COMPLETION_UNREADABLE_FOR` has passed, when it is refused
`unavailable`, and a substrate that fails the spawn refuses it `spawn_failed`.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import partial
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import ActionStep, AgentStep, CheckStep
from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType
from orchestrator.predicates import Unmet, evaluate
from orchestrator.worker.action_steps import _start_action
from orchestrator.worker.agent_steps import (
    _BLOCKED_BY,
    _held_by_limit,
    _placement,
    _returned_to_gate,
    _start_attempt,
)
from orchestrator.worker.check_steps import _start_check
from orchestrator.worker.completions import answer_checked
from orchestrator.worker.exits import RESETS, pending_request, request_run
from orchestrator.worker.inputs import retry_briefing
from orchestrator.worker.runtime_adapters import RuntimeRefused
from orchestrator.worker.sandbox_limit import LimitReached
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    _next_attempt,
    _observed,
    _Plan,
    _Tick,
    latest_current_run,
)
from runtime.contract import AgentRuntimeError

__all__ = [
    "STEP_RETRY_EVENTS",
    "RetryRefusal",
    "answer_step_retry",
    "pending_step_retry",
    "retry_refusal",
]

logger = logging.getLogger(__name__)


class RetryRefusal(StrEnum):
    STALE = "stale"
    GATE = "gate"
    NOT_ESCALATED = "not_escalated"
    OFF_THE_FLOOR = "off_the_floor"
    PRECONDITIONS_UNMET = "preconditions_unmet"
    SPAWN_REFUSED = "spawn_refused"
    SPAWN_FAILED = "spawn_failed"
    UNAVAILABLE = "unavailable"
    SANDBOX_LIMIT = "sandbox_limit"
    #: The failed run was a conflict fix whose pull request needs none now: the ticket went
    #: back to its gate instead (`_returned_to_gate`).
    CONFLICT_FIXED = "conflict_fixed"


#: A step retry request, then what answers it.
STEP_RETRY_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.STEP_RETRY_REQUESTED,
    EventType.STEP_RETRY_REFUSED,
    EventType.STEP_STARTED,
    EventType.TICKET_EXITED,
    *RESETS,
)


async def pending_step_retry(session: AsyncSession, ticket_id: int) -> Event | None:
    """The ticket's step retry request that nothing has answered yet."""
    return await pending_request(session, ticket_id, *STEP_RETRY_EVENTS)


async def escalated_on(session: AsyncSession, ticket_id: int, run_id: int) -> bool:
    """Whether the ticket's block is the escalation of `run_id`'s failure. A refused spawn after
    a failure also blocks with a failed latest run, and is not one."""
    latest = (
        await session.execute(
            select(Event.type, Event.step_run_id)
            .where(Event.ticket_id == ticket_id, Event.type.in_(_BLOCKED_BY))
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).first()
    return (
        latest is not None
        and latest.type == EventType.ESCALATED
        and latest.step_run_id == run_id
    )


async def retry_refusal(
    session: AsyncSession, ticket: Ticket, run: StepRun
) -> RetryRefusal | None:
    """Why a Retry of `run` cannot start the next attempt, or None. The API and the loop both
    ask it."""
    if ticket.status not in (TicketStatus.RUNNING, TicketStatus.BLOCKED):
        return RetryRefusal.OFF_THE_FLOOR
    if run.kind == StepKind.HUMAN_GATE:
        return RetryRefusal.GATE
    if (
        run.step_name != ticket.current_step
        or run.generation != ticket.generation
        or StepRunStatus(run.status) not in _FAILED_STATUSES
    ):
        return RetryRefusal.STALE
    latest = await latest_current_run(session, ticket, run.step_name)
    if latest is None or latest.id != run.id:
        return RetryRefusal.STALE
    if ticket.status == TicketStatus.BLOCKED and not await escalated_on(
        session, ticket.id, run.id
    ):
        return RetryRefusal.NOT_ESCALATED
    return None


async def answer_step_retry(tick: _Tick, request: Event) -> bool:
    """Answer `request`. True when it started an attempt, or tried to and the runtime refused
    the spawn, which blocked the ticket, or the ticket went back to its gate."""
    try:
        return await answer_checked(
            tick,
            request,
            partial(_answer, tick, request),
            partial(_refuse, tick, request, RetryRefusal.UNAVAILABLE),
            what="step retry request",
        )
    except (AgentRuntimeError, RuntimeRefused) as exc:
        await tick.session.refresh(tick.ticket)
        logger.warning(
            "ticket %s: step retry request %s refused, the substrate failed the spawn: %s",
            tick.ticket.external_key,
            request.id,
            exc,
            exc_info=True,
        )
        tick.plan = _Plan()
        await _refuse(tick, request, RetryRefusal.SPAWN_FAILED, detail=str(exc))
        return False


async def _answer(tick: _Tick, request: Event) -> bool:
    run = await request_run(tick, request)
    refusal = (
        RetryRefusal.STALE
        if run is None
        else await retry_refusal(tick.session, tick.ticket, run)
    )
    step = None if run is None else tick.config.find_step(run.step_name)
    if refusal is None and not isinstance(step, AgentStep | CheckStep | ActionStep):
        refusal = RetryRefusal.STALE
    if refusal is not None:
        await _refuse(tick, request, refusal)
        return False
    assert run is not None
    attempt = await _next_attempt(tick.session, tick.ticket.id, run.step_name)
    if isinstance(step, AgentStep) and await _returned_to_gate(tick, step, attempt):
        await _refuse(tick, request, RetryRefusal.CONFLICT_FIXED)
        return True
    if isinstance(step, AgentStep) and step.preconditions is not None:
        result = await _observed(
            evaluate(step.preconditions, tick.ctx),
            what=f"step {step.name} retry of attempt {run.attempt}: preconditions",
        )
        if not result.value:
            await _refuse(
                tick, request, RetryRefusal.PRECONDITIONS_UNMET, **Unmet.of(result).details()
            )
            return False
    placement = None
    if isinstance(step, AgentStep):
        # Asked before `_start_attempt`, whose None at the limit reads like a lost race. Not
        # queued: the person presses again once a sandbox frees.
        placement = await _placement(tick)
        reached = await _held_by_limit(tick, placement) if placement is not None else None
        if reached is not None:
            await _refuse_at_limit(tick, request, reached)
            return False

    before = TicketStatus(tick.ticket.status)
    tick.ticket.status = TicketStatus.RUNNING
    if isinstance(step, AgentStep):
        started = await _start_attempt(
            tick,
            step,
            attempt=attempt,
            rework_of=run,
            briefing=await retry_briefing(tick, run),
            announce_hold=False,
            placed=(placement,),
        )
        if started is not None:
            return True
        if tick.held_by_limit is not None:
            # Met since the check above: a run another ticket started has committed.
            tick.ticket.status = before
            await _refuse_at_limit(tick, request, tick.held_by_limit)
            return False
        if tick.ticket.status == TicketStatus.BLOCKED:
            # `_block_on_refusal` labelled the ticket and said why on it.
            await _refuse(tick, request, RetryRefusal.SPAWN_REFUSED)
            return True
        # Another runner won the attempt's unique key.
        tick.ticket.status = before
        await _refuse(tick, request, RetryRefusal.STALE)
        return False
    if isinstance(step, CheckStep):
        await _start_check(tick, step, attempt=attempt)
    else:
        assert isinstance(step, ActionStep)
        await _start_action(tick, step, attempt=attempt)
    return True


async def _refuse_at_limit(tick: _Tick, request: Event, reached: LimitReached) -> None:
    await _refuse(
        tick,
        request,
        RetryRefusal.SANDBOX_LIMIT,
        scope=reached.scope,
        limit=reached.limit,
        running=reached.running,
    )


async def _refuse(
    tick: _Tick, request: Event, reason: RetryRefusal, **extra: object
) -> None:
    """No issue comment: the dashboard's timeline shows it."""
    tick.deps.unreadable_requests.pop(request.id, None)
    run = await request_run(tick, request)
    await tick.emit(
        EventType.STEP_RETRY_REFUSED,
        run=run,
        step=request.details.get("step"),
        attempt=request.details.get("attempt"),
        request_id=request.id,
        reason=reason.value,
        **extra,
    )
