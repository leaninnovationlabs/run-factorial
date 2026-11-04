"""The requests a person makes of a ticket's run or its park: stop the run, start its retry
now, resume its session, or release an escalation (decisions 26, 29, 56), check its
postconditions now and accept it (decision 78), or retry a failed step, continuing its session
(decision 77). The last two are dashboard only.

Transport-neutral, like `services.tickets`. Each one appends the event the loop honours,
commits, and waits for the pass it wakes (`api.wakes`). The API never calls a substrate: a
route or tool that reached for the adapter would race the loop for a run the loop owns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict, Forbidden, Invalid
from orchestrator.api.services.outputs import pinned_config
from orchestrator.api.services.results import RequestResult
from orchestrator.api.services.tickets import (
    _latest_attempt,
    nothing_to,
    refuse_while_quarantined,
)
from orchestrator.api.wakes import wake_and_wait
from orchestrator.config import AgentStep, CheckStep, GateStep, ProcessConfig, Retry
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.read_models import blocked_on, ticket_park_reason, ticket_row
from orchestrator.events.terminals import is_current_run
from orchestrator.events.types import RequestedVia
from orchestrator.worker.completions import forceable, pending_completion_request
from orchestrator.worker.interrupts import pending_resume_request
from orchestrator.worker.reconcile import escalated_since, pending_unblock_request
from orchestrator.worker.retries import RetryRefusal, pending_step_retry, retry_refusal
from orchestrator.worker.tick_context import _FAILED_STATUSES, _next_attempt, latest_current_run
from runtime.contract import ParkReason

__all__ = [
    "STEP_KINDS",
    "dashboard_only",
    "parked_at_its_step",
    "request_completion",
    "request_interrupt",
    "request_resume",
    "request_retry",
    "request_step_retry",
    "request_unblock",
    "ticket_of",
]


async def _lock_run(session: AsyncSession, run: StepRun) -> None:
    """Lock `run`'s row and re-read it into `run`."""
    await session.execute(
        select(StepRun)
        .where(StepRun.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def ticket_of(session: AsyncSession, run: StepRun) -> Ticket:
    return (await session.execute(select(Ticket).where(Ticket.id == run.ticket_id))).scalar_one()


#: A step's kind, as a refusal names it.
STEP_KINDS: Final[dict[str, str]] = {
    StepKind.AGENT: "an agent",
    StepKind.HUMAN_GATE: "a gate",
    StepKind.ACTION: "an action",
    StepKind.CHECK: "a check",
}

#: A run's status, as a refusal names it.
_RUN_STATES: Final[dict[str, str]] = {
    StepRunStatus.RUNNING: "running",
    StepRunStatus.WAITING: "waiting",
    StepRunStatus.SUCCEEDED: "finished",
    StepRunStatus.FAILED: "failed",
    StepRunStatus.TIMEOUT: "timed out",
    StepRunStatus.CANCELLED: "cancelled",
    StepRunStatus.APPROVED: "approved",
    StepRunStatus.REJECTED: "rejected",
    StepRunStatus.INTERRUPTED: "paused",
    StepRunStatus.IDLE: "waiting for an answer",
}

#: A ticket's status, as a refusal names it.
_TICKET_STATES: Final[dict[str, str]] = {
    TicketStatus.RUNNING: "running",
    TicketStatus.WAITING_GATE: "waiting for a review",
    TicketStatus.BLOCKED: "blocked",
    TicketStatus.DONE: "done",
    TicketStatus.EXITED: "exited",
}


def dashboard_only(actor: Actor, *, action: str, hint: str) -> None:
    """Refuse every surface but the dashboard: an MCP caller, and an assistant's proposal, which a
    person may approve without reading it."""
    if actor.via is not RequestedVia.DASHBOARD:
        raise Forbidden(f"Only a person on the dashboard can {action}.", hint=hint)


async def request_interrupt(session: AsyncSession, run: StepRun, actor: Actor) -> RequestResult:
    """Ask the loop to stop this run and park its ticket.

    What is accepted is a request. The worker is woken for this ticket and honours it on that
    pass; `step_run.status` becoming `interrupted` is what actually happened.

    Refused unless the run is still RUNNING or IDLE, and unless it is an agent run: only an
    agent has a session to stop and a workspace to keep — a check step's RUNNING row is the
    loop's own wait on observed state. Refused, too, while the ticket is quarantined. Every
    refusal is the same honesty: accepting would record a request the loop can never honour.
    """
    if run.status not in (StepRunStatus.RUNNING, StepRunStatus.IDLE):
        state = (
            "is already paused"
            if run.status == StepRunStatus.INTERRUPTED
            else "isn't running"
            if run.status == StepRunStatus.WAITING
            else "has already ended"
        )
        raise Conflict(f"This run {state}, so there is nothing to interrupt.", code="not_running")
    if run.kind != StepKind.AGENT:
        raise Conflict(
            f"This is {STEP_KINDS[run.kind]} step. Only an agent's run can be interrupted.",
            code="not_an_agent_run",
        )
    ticket = await ticket_of(session, run)
    refuse_while_quarantined(ticket, "interrupt")
    await append(
        session,
        type=EventType.INTERRUPT_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        **actor.fields(),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="interrupt", status="requested", landed=landed)


async def request_retry(session: AsyncSession, run: StepRun, actor: Actor) -> RequestResult:
    """Ask the loop to start this failed run's retry now, rather than once its backoff has run.

    Refused unless the ticket is waiting out exactly this run's backoff: the run is the latest
    attempt at the ticket's current step, it failed, the ticket is still running (an exhausted
    retry blocks it, and the un-block grants the next attempt), and its pinned step retries with
    a backoff that has not run yet. A request already recorded is answered as accepted again, so
    a second press starts nothing more. The loop starts the attempt on the pass this wakes
    (`worker.tick_context._retry_ready`).
    """
    ticket = await ticket_of(session, run)
    refuse_while_quarantined(ticket, "retry")
    latest = await latest_current_run(session, ticket, run.step_name)
    if (
        latest is None
        or latest.id != run.id
        or run.generation != ticket.generation
        or run.step_name != ticket.current_step
        or run.status not in _FAILED_STATUSES
        or ticket.status != TicketStatus.RUNNING
    ):
        raise Conflict(f"Attempt {run.attempt} of {run.step_name} isn't waiting to be retried.")
    retry = await _pinned_retry(session, ticket, run)
    if retry is None:
        raise Conflict(f"This ticket's process doesn't retry {run.step_name}.")
    if run.ended_at is None or datetime.now(UTC) >= run.ended_at + retry.backoff:
        raise Conflict(f"The retry of {run.step_name} is starting now.", code="already_due")
    asked = await session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == ticket.id,
            Event.step_run_id == run.id,
            Event.type == EventType.RETRY_REQUESTED,
        )
        .limit(1)
    )
    if asked is None:
        await append(
            session,
            type=EventType.RETRY_REQUESTED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            next_attempt=run.attempt + 1,
            **actor.fields(),
        )
        await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="retry", status="requested", landed=landed)


async def request_completion(
    session: AsyncSession,
    run: StepRun,
    actor: Actor,
    *,
    allow_unmet: bool,
    seen_failures: Sequence[str],
) -> tuple[RequestResult, int]:
    """Ask the loop to check this run's postconditions now and accept it when they hold
    (decision 78). Returns the result and the request's event id, which the dashboard polls.

    Refused unless the run is the latest attempt at the ticket's current step, of an agent or a
    check step, in a status the loop may accept it from. An agent mid-turn is refused: the
    person interrupts it first. While one request is unanswered for this run another with the
    same `allow_unmet` records nothing, and `status` is then `pending`; one with the other
    `allow_unmet`, or one pending for another run, is refused.
    """
    dashboard_only(
        actor,
        action="force a step complete",
        hint="Press Force complete on the ticket page in the dashboard.",
    )
    # Locked and re-read, as `request_resume` does: a second press waits for the first one's
    # commit and finds its request pending.
    await _lock_run(session, run)
    ticket = await ticket_of(session, run)
    refuse_while_quarantined(ticket, "force complete")
    if ticket.status in (TicketStatus.DONE, TicketStatus.EXITED):
        raise nothing_to(ticket, "complete")
    if not await is_current_run(
        session,
        ticket_id=ticket.id,
        step=run.step_name,
        attempt=run.attempt,
        current_step=ticket.current_step,
    ):
        raise Conflict(f"Attempt {run.attempt} of {run.step_name} isn't the one this ticket is on.")
    step = (await pinned_config(session, ticket)).find_step(run.step_name)
    if not isinstance(step, AgentStep | CheckStep):
        raise Conflict(
            f"{run.step_name} isn't an agent or check step, so it can't be forced complete."
        )
    if run.kind == StepKind.AGENT and run.status == StepRunStatus.RUNNING:
        raise Conflict(
            "The agent is still working. Interrupt it first.",
            tool_hint="factorial_ticket_interrupt",
        )
    if not forceable(run.kind, run.status):
        raise Conflict(
            f"Attempt {run.attempt} of {run.step_name} is {_RUN_STATES[run.status]}, so it can't "
            "be forced complete."
        )
    if allow_unmet and not seen_failures:
        raise Invalid("allow_unmet needs the failures the person saw.")

    pending = await pending_completion_request(session, ticket.id)
    if pending is not None and pending.step_run_id != run.id:
        raise Conflict(
            f"A Force complete for attempt {pending.details['attempt']} is still being checked."
        )
    if pending is not None and bool(pending.details.get("allow_unmet")) != allow_unmet:
        # Folded into the pending one, a Continue with warning would come back as the plain
        # check's answer, or a plain check as a force the person did not ask for.
        raise Conflict(
            f"A Force complete for attempt {run.attempt} is still being checked.",
            hint="Wait for its answer, then press again.",
        )
    if pending is not None:
        landed = await wake_and_wait(session, ticket)
        result = RequestResult(
            ticket_id=ticket.id, action="complete", status="pending", landed=landed
        )
        return result, pending.id
    row = await append(
        session,
        type=EventType.COMPLETION_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        allow_unmet=allow_unmet,
        seen_failures=list(seen_failures),
        **actor.fields(),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    result = RequestResult(
        ticket_id=ticket.id, action="complete", status="requested", landed=landed
    )
    return result, row.id


#: A refused Retry's words, and where to look instead.
_RETRY_REFUSALS: Final[dict[RetryRefusal, tuple[str, str | None]]] = {
    RetryRefusal.STALE: (
        "Attempt {attempt} of {step} isn't the step's latest failed attempt.",
        None,
    ),
    RetryRefusal.GATE: ("A gate isn't retried.", "Review the pull request, or press Unblock."),
    RetryRefusal.NOT_ESCALATED: (
        "This ticket is blocked on something other than this attempt's failure.",
        "Press Unblock to ask again.",
    ),
    RetryRefusal.OFF_THE_FLOOR: ("This ticket is {status}.", None),
}


async def request_step_retry(session: AsyncSession, run: StepRun, actor: Actor) -> RequestResult:
    """Ask the loop to start a failed step's next attempt now, continuing the failed attempt's
    session where it can (decision 77; `worker.retries`).

    Refused unless the run is its step's latest failed attempt on a running ticket, or on one
    blocked by this attempt's escalation, and not a gate. While one request for this run is
    unanswered another records nothing, and `status` is then `pending`.
    """
    dashboard_only(
        actor,
        action="retry a step",
        hint="Press Retry on the failed step in the dashboard.",
    )
    # Locked and re-read, as `request_resume` does: a second press waits for the first one's
    # commit and finds its request pending.
    await _lock_run(session, run)
    ticket = await ticket_of(session, run)
    refuse_while_quarantined(ticket, "retry")
    if (refusal := await retry_refusal(session, ticket, run)) is not None:
        words, hint = _RETRY_REFUSALS[refusal]
        raise Conflict(
            words.format(
                attempt=run.attempt, step=run.step_name, status=_TICKET_STATES[ticket.status]
            ),
            hint=hint,
        )
    pending = await pending_step_retry(session, ticket.id)
    if pending is not None and pending.step_run_id == run.id:
        await session.commit()
        landed = await wake_and_wait(session, ticket)
        return RequestResult(
            ticket_id=ticket.id, action="retry_step", status="pending", landed=landed
        )
    await append(
        session,
        type=EventType.STEP_RETRY_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        next_attempt=await _next_attempt(session, ticket.id, run.step_name),
        **actor.fields(),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(
        ticket_id=ticket.id, action="retry_step", status="requested", landed=landed
    )


async def _pinned_retry(session: AsyncSession, ticket: Ticket, run: StepRun) -> Retry | None:
    """The run's step's retry policy in the ticket's PINNED process, or None."""
    content = (
        await session.execute(
            select(ProcessVersion.content).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    config = ProcessConfig.model_validate(content)
    step = config.find_step(run.step_name)
    if step is None or isinstance(step, GateStep):
        return None
    return step.retry or config.defaults.retry


async def request_resume(
    session: AsyncSession, run: StepRun, actor: Actor, *, answer: str | None
) -> RequestResult:
    """Ask the loop to continue a parked session, with `answer` as its turn (decision 26). The
    twin of removing the park's label, honoured on the pass it wakes.

    Refused unless the run is the parked one: INTERRUPTED, the latest at its step, on a BLOCKED
    ticket that is not quarantined. At the session ceiling only the dashboard's Resume is
    accepted (decision 56): not an MCP caller, and not an assistant's proposal, which a person
    may approve without reading it.
    While one request is unanswered another records nothing, its answer included: `status` is
    then `pending`. The event keeps `by`, not `requested_by`: the loop reads it into the turn.
    """
    # Locked and re-read before it is judged: a release the loop commits meanwhile is seen, not
    # raced. The tick locks the same row (`worker.interrupts._lock_latest_run`).
    await _lock_run(session, run)
    ticket = await ticket_of(session, run)
    if not await parked_at_its_step(session, ticket, run):
        raise Conflict(
            "This run isn't paused, so there is no session to resume.", code="not_parked"
        )
    refuse_while_quarantined(ticket, "resume")
    if (
        actor.via is not RequestedVia.DASHBOARD
        and await ticket_park_reason(session, ticket.id) == ParkReason.CEILING
    ):
        # Decision 56: the ceiling bounds spend only because a person lifts it. A model under
        # prompt injection could otherwise resume every park.
        raise Conflict(
            "This run paused at its session time limit, and only a person can resume it.",
            hint="Press Resume on the ticket page in the dashboard.",
            code="ceiling",
        )

    pending = await pending_resume_request(session, ticket.id)
    if pending is not None and pending.step_run_id == run.id:
        await session.commit()
        landed = await wake_and_wait(session, ticket)
        return RequestResult(ticket_id=ticket.id, action="resume", status="pending", landed=landed)
    await append(
        session,
        type=EventType.SESSION_RESUME_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        **actor.fields("by"),
        has_answer=answer is not None,
        # The one place the text is kept: the loop reads it from here into the turn, and copies
        # it into no other event.
        **({"answer": answer} if answer is not None else {}),
        step=run.step_name,
        attempt=run.attempt,
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="resume", status="requested", landed=landed)


async def parked_at_its_step(session: AsyncSession, ticket: Ticket, run: StepRun) -> bool:
    """The run is the one its ticket is parked on: INTERRUPTED, the latest attempt at the
    ticket's current step, on a BLOCKED ticket."""
    return (
        run.status == StepRunStatus.INTERRUPTED
        and ticket.status == TicketStatus.BLOCKED
        and await is_current_run(
            session,
            ticket_id=run.ticket_id,
            step=run.step_name,
            attempt=run.attempt,
            current_step=ticket.current_step,
        )
    )


#: Where to look instead, by the status a refused un-block found. The dashboard shows it
#: beside the refusal, and an MCP caller reads it as `Next:`, so it names no tool.
_NOT_BLOCKED_HINTS: Final[dict[str, str]] = {
    TicketStatus.WAITING_GATE: "It is waiting for a review. Review the pull request on GitHub.",
    TicketStatus.RUNNING: "It is running, so nothing needs to be unblocked.",
    TicketStatus.EXITED: "It has exited. Re-enter it to bring it back.",
}
#: The same, for a tool caller (`ServiceError.tool_hint`).
_NOT_BLOCKED_TOOL_HINTS: Final[dict[str, str]] = {
    TicketStatus.RUNNING: "Nothing is paused. Call factorial_ticket_get to see what it is doing.",
    TicketStatus.EXITED: "factorial_ticket_reenter",
}


async def request_unblock(session: AsyncSession, ticket: Ticket, actor: Actor) -> RequestResult:
    """Ask the loop to release an escalation, as removing `factorial:blocked` would. It is the
    same grant path, honoured on the pass this request wakes
    (`worker.reconcile._unblock_requested`).

    Refused unless the ticket is BLOCKED on an escalation. It must not be quarantined, and it
    must not be parked: a Resume continues a parked session in its kept workspace. While one
    request is unanswered and the ticket has not escalated since, another records nothing, and
    `status` is then `pending`. A request older than the latest escalation was about an
    escalation already released, so it does not stand in for this one.
    """
    if ticket.status != TicketStatus.BLOCKED:
        raise Conflict(
            "This ticket isn't blocked, so there is nothing to unblock.",
            hint=_NOT_BLOCKED_HINTS.get(ticket.status),
            code="not_blocked",
            tool_hint=_NOT_BLOCKED_TOOL_HINTS.get(ticket.status),
        )
    refuse_while_quarantined(ticket, "unblock")
    # Quarantine is refused above and the status is BLOCKED, so anything but `escalated` is a
    # parked run, which the worker refuses too. It may still refuse an accepted request: as
    # stale if the ticket escalated again first, or for a step with no retry policy.
    if blocked_on(await ticket_row(session, ticket.id)) != "escalated":
        raise Conflict(
            "This ticket is paused, not stopped on a failure. Resume it to continue its session.",
            code="parked",
            tool_hint="factorial_ticket_resume",
        )
    pending = await pending_unblock_request(session, ticket.id)
    if pending is not None and not await escalated_since(session, ticket.id, pending.id):
        landed = await wake_and_wait(session, ticket)
        return RequestResult(ticket_id=ticket.id, action="unblock", status="pending", landed=landed)
    await append(
        session,
        type=EventType.UNBLOCK_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=None,
        **actor.fields(),
        step=ticket.current_step,
        attempt=await _latest_attempt(session, ticket),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="unblock", status="requested", landed=landed)
