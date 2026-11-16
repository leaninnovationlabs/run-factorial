"""The requests a person makes of a ticket's run or its park: stop the run, start its retry
now, resume its session, or release an escalation (decisions 26, 29, 56), check its
postconditions now and accept it (decision 78), retry a failed step, continuing its session
(decision 77), or approve a run of a step that awaits review. The last three are dashboard only.

Transport-neutral, like `services.tickets`. Each one appends the event the loop honours,
commits, and waits for the pass it wakes (`api.wakes`). The API never calls a substrate: a
route or tool that reached for the adapter would race the loop for a run the loop owns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from githubkit.exception import RequestError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.registry import host_failure
from orchestrator.adapters.state import BranchState
from orchestrator.api.deps import RunObserver
from orchestrator.api.schemas import ApprovalOut, ReviewOut
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import (
    Conflict,
    Forbidden,
    Invalid,
    ServiceError,
    Unavailable,
    UpstreamFailed,
)
from orchestrator.api.services.outputs import pinned_config, pinned_review_step
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
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.approvals import latest_approval
from orchestrator.events.outputs import outputs_observation, pushed, spawn_heads
from orchestrator.events.read_models import (
    blocked_on,
    latest_park,
    parked_at_prompt,
    ticket_park_reason,
    ticket_row,
)
from orchestrator.events.terminals import is_current_run
from orchestrator.events.types import RequestedVia
from orchestrator.predicates.globs import resolve_refs, sole_ref
from orchestrator.worker.branches import declares_files
from orchestrator.worker.completions import forceable, pending_completion_request
from orchestrator.worker.failures import spoken
from orchestrator.worker.interrupts import pending_resume_request
from orchestrator.worker.reconcile import escalated_since, pending_unblock_request
from orchestrator.worker.retries import RetryRefusal, pending_step_retry, retry_refusal
from orchestrator.worker.templates import _glob
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    _next_attempt,
    latest_current_run,
)
from runtime.contract import ParkReason

__all__ = [
    "STEP_KINDS",
    "dashboard_only",
    "parked_at_its_step",
    "request_approval",
    "request_completion",
    "request_interrupt",
    "request_resume",
    "request_retry",
    "request_step_retry",
    "request_unblock",
    "review_state",
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
    RetryRefusal.GATE: (
        "A gate isn't retried.",
        "Review the pull request, or press Unblock.",
    ),
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
                attempt=run.attempt,
                step=run.step_name,
                status=_TICKET_STATES[ticket.status],
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
            "This run isn't paused, so there is no session to resume.",
            code="not_parked",
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


_NO_BRANCH: Final = "Nothing is pushed yet, so there is no commit to approve."
_BRANCHES: Final = (
    "More than one branch matches this step's branch, so there is no one commit to approve."
)
_UNREAD: Final = "Factorial couldn't read this step's branch, so it can't be approved now."
_FILES_STALE: Final = (
    "The branch moved after its files were read. Look again in a moment, then approve."
)
#: How long a repeat press by the same person at the same head is the same press. Past it a press
#: records again, so an approval a later issue comment holds can be renewed.
_DOUBLE_PRESS: Final = timedelta(seconds=60)
#: A forge that gave no answer at all: githubkit's timeout or transport error, or httpx's
#: beneath a GitLab call.
_NO_ANSWER: Final = (RequestError, httpx.TransportError, TimeoutError)


async def request_approval(
    session: AsyncSession,
    run: StepRun,
    actor: Actor,
    *,
    head_sha: str,
    observed: RunObserver,
    retention: timedelta,
) -> RequestResult:
    """Approve, in the dashboard, the run of a step whose postconditions call
    `request_approval()`, at `head_sha`, the commit its branch ends on. It queues nothing for the
    loop: the `step_approved` it appends is what the predicate reads when the loop next judges
    the run.

    Refused unless a person on the dashboard asks, the run is the one its ticket is on, and it
    waits at its prompt: IDLE, or parked where `recheck_parked` re-reads it, within `retention`.
    `head_sha` must still be the head of the one branch the step's branch names, and for a step
    that declares `preview.files` the run's latest record of them must be a read at that head.
    The same person approving the same head again within `_DOUBLE_PRESS` records nothing, and
    `status` is then `pending`.
    """
    dashboard_only(
        actor,
        action="approve a step",
        hint="Press Approve on the ticket page in the dashboard.",
    )
    ticket = await ticket_of(session, run)
    step = await pinned_review_step(session, ticket, run)
    # Before the forge read, so a press that cannot land costs no branch listing.
    await _refuse_approval(session, run, ticket, step, now=datetime.now(UTC), retention=retention)
    # Read before the lock: the worker's tick waits on that row, and a forge can be slow.
    branches = await _branches(observed)
    await _lock_run(session, run)
    # The worker may have moved the ticket while the forge answered: judge the run against now.
    await session.refresh(ticket)
    step = await _refuse_approval(
        session, run, ticket, step, now=datetime.now(UTC), retention=retention
    )
    if isinstance(branches, ServiceError):
        raise branches
    head = await _branch_head(session, ticket, step, branches)
    if head.sha != head_sha:
        raise Conflict(
            "The branch moved after you opened this. Look again, then approve.",
            code="head_moved",
        )
    if await _files_stale(session, step, run, head):
        raise Conflict(_FILES_STALE, code="files_stale")
    latest = await latest_approval(session, run.ticket_id, run.id)
    duplicate = (
        latest is not None
        and (latest.approved_by, latest.head_sha) == (actor.by, head_sha)
        and datetime.now(UTC) - latest.at < _DOUBLE_PRESS
    )
    if not duplicate:
        await append(
            session,
            type=EventType.STEP_APPROVED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            head_sha=head_sha,
            branch=head.name,
            **actor.fields("approved_by"),
        )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(
        ticket_id=ticket.id,
        action="approve",
        status="pending" if duplicate else "requested",
        landed=landed,
    )


async def review_state(
    session: AsyncSession, run: StepRun, observed: RunObserver, *, retention: timedelta
) -> ReviewOut:
    """What Approve on this run would approve, whether it applies to the run at all, whether the
    run has pushed, and the refusal a press now would get, in `request_approval`'s order. The
    forge is read only when no refusal short of it is decided: a step that does not await
    review, or a press refused before the branch read, reads none, and has no branch or head."""
    ticket = await ticket_of(session, run)
    step = await pinned_review_step(session, ticket, run)
    if step is None:
        return ReviewOut(
            awaits_review=False,
            branch=None,
            head_sha=None,
            approval=None,
            offered=False,
            refusal=None,
            pushed=False,
        )
    latest = await latest_approval(session, run.ticket_id, run.id)
    approval = (
        None
        if latest is None
        else ApprovalOut(approved_by=latest.approved_by, at=latest.at, head_sha=latest.head_sha)
    )
    refused, offered = await _approval_refusal(
        session, run, ticket, step, now=datetime.now(UTC), retention=retention
    )
    unread = ReviewOut(
        awaits_review=True,
        branch=None,
        head_sha=None,
        approval=approval,
        offered=offered,
        refusal=None if refused is None else refused.message,
        pushed=False,
    )
    if refused is not None:
        return unread
    branches = await _branches(observed)
    if isinstance(branches, ServiceError):
        return unread.model_copy(update={"refusal": _UNREAD})
    try:
        head = await _branch_head(session, ticket, step, branches)
    except Conflict as exc:
        return unread.model_copy(update={"refusal": exc.message})
    return unread.model_copy(
        update={
            "branch": head.name,
            "head_sha": head.sha,
            "refusal": _FILES_STALE if await _files_stale(session, step, run, head) else None,
            "pushed": pushed(head, await spawn_heads(session, run)),
        }
    )


async def _refuse_approval(
    session: AsyncSession,
    run: StepRun,
    ticket: Ticket,
    step: AgentStep | None,
    *,
    now: datetime,
    retention: timedelta,
) -> AgentStep:
    """`step`, the run's pinned step that awaits review, unless an approval is refused short of
    the branch read: then that refusal is raised."""
    refused, _ = await _approval_refusal(session, run, ticket, step, now=now, retention=retention)
    if refused is not None:
        raise refused
    assert step is not None  # `_approval_refusal` refuses `not_reviewed` for no step
    return step


async def _approval_refusal(
    session: AsyncSession,
    run: StepRun,
    ticket: Ticket,
    step: AgentStep | None,
    *,
    now: datetime,
    retention: timedelta,
) -> tuple[Conflict | None, bool]:
    """The first refusal of an approval short of the branch read, or None, and whether Approve
    applies to the run at all, refused now or not: it is current and at its prompt."""
    current = await _current(session, ticket, run)
    at_prompt = await _at_prompt(session, ticket, run)
    offered = current and at_prompt
    try:
        refuse_while_quarantined(ticket, "approve")
    except Conflict as exc:
        return exc, offered
    if ticket.status in (TicketStatus.DONE, TicketStatus.EXITED):
        return nothing_to(ticket, "approve"), offered
    if not current:
        return Conflict(
            f"Attempt {run.attempt} of {run.step_name} isn't the one this ticket is on.",
            code="stale",
        ), offered
    if step is None:
        return Conflict(
            f"{run.step_name} doesn't wait for an approval.", code="not_reviewed"
        ), offered
    if run.status == StepRunStatus.RUNNING:
        return Conflict(
            "The agent is still working. Approve when it stops.", code="mid_turn"
        ), offered
    if not at_prompt:
        return Conflict(
            f"Attempt {run.attempt} of {run.step_name} is {_RUN_STATES[run.status]}, so it "
            "can't be approved.",
            code="not_waiting",
        ), offered
    if await pending_resume_request(session, ticket.id) is not None:
        return Conflict(
            "A Resume of this run is still being answered. Approve when the agent stops again.",
            code="release_pending",
        ), offered
    if (
        run.status == StepRunStatus.INTERRUPTED
        and run.ended_at is not None
        and run.ended_at < now - retention
    ):
        return Conflict(
            f"This step waited past its {spoken(retention)} and its session is gone. Press Redo "
            "to run it again.",
            code="expired",
        ), offered
    return None, offered


async def _files_stale(
    session: AsyncSession, step: AgentStep, run: StepRun, head: BranchState
) -> bool:
    """For a step that declares files, is the run's latest record of them anything but a read of
    its branch at `head`? The person would approve a commit whose files they never saw. A step
    that declares none has no record and is never held."""
    if not declares_files(step):
        return False
    record = await outputs_observation(session, run)
    return record is None or record.get("status") != "observed" or record.get("commit") != head.sha


async def _current(session: AsyncSession, ticket: Ticket, run: StepRun) -> bool:
    return await is_current_run(
        session,
        ticket_id=ticket.id,
        step=run.step_name,
        attempt=run.attempt,
        current_step=ticket.current_step,
    )


async def _at_prompt(session: AsyncSession, ticket: Ticket, run: StepRun) -> bool:
    """The run waits at its prompt: IDLE, or parked by the loop at its prompt on a BLOCKED
    ticket, never by a person's stop. Whether it is current is the caller's to judge."""
    if run.status == StepRunStatus.IDLE:
        return True
    if run.status != StepRunStatus.INTERRUPTED or ticket.status != TicketStatus.BLOCKED:
        return False
    park = await latest_park(session, run.ticket_id, run.id)
    return park is not None and parked_at_prompt(park.reason, park.at_prompt)


async def _branches(observed: RunObserver) -> Sequence[BranchState] | ServiceError:
    """The repo's branches, or the refusal a request that needed them answers with."""
    source = await observed()
    if source is None:
        return Unavailable(
            "This project's repository isn't connected, so its branch can't be read."
        )
    try:
        return await source.branches()
    except Exception as exc:
        failure = host_failure(exc)
        if failure is None and not isinstance(exc, _NO_ANSWER):
            raise
        status = None if failure is None else failure.status
        return UpstreamFailed(
            "Factorial couldn't read this step's branch"
            + ("." if status is None else f" (HTTP {status}).")
        )


async def _branch_head(
    session: AsyncSession,
    ticket: Ticket,
    step: AgentStep,
    branches: Sequence[BranchState],
) -> BranchState:
    """The one branch the step's branch names, as `request_approval()` resolves it."""
    repo = (
        await session.execute(select(Project.github_repo).where(Project.id == ticket.project_id))
    ).scalar_one()
    values = {
        "ticket": ticket.external_key,
        "ticket_owner": ticket.owner_at_admission,
        "repo": repo,
    }
    glob = _glob(step.workspace.branch, values)
    head = sole_ref(branches, glob)
    if head is None:
        raise Conflict(_BRANCHES if resolve_refs(branches, glob) else _NO_BRANCH, code="no_branch")
    return head
