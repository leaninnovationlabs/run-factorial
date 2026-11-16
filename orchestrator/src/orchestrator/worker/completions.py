"""Force complete (decision 78): a person asks the loop to check a step's postconditions now, and
the loop accepts the step when they hold. Having read the failures, the person may ask again
with `allow_unmet`: the loop then accepts the step as forced (`step_run.forced_by`) when every
failure now is one they read, and refuses `changed` otherwise. A rework that has not moved the
pull request its gate rejected is never forced.

The request is answered once, by an acceptance, a force or a refusal. The verdict is a full
evaluation (`every_leaf`), and it and every other read that can fail come before any
adapter or GitHub write: an unreadable verdict writes nothing, and the request stays pending
until `COMPLETION_UNREADABLE_FOR` has passed. A refusal leaves the run and the ticket as they
were, with one exception: an IDLE session is stopped only once the verdict is read, and when its
result cannot be read after that stop, the stop is recorded as a person's interrupt before the
refusal, so the loop never resumes a session nobody asked to continue.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep, CheckStep
from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus
from orchestrator.events import EventType
from orchestrator.events.types import REQUESTED_VIA_WORDS, requested_via
from orchestrator.logs import revert_on_error
from orchestrator.predicates import evaluate, needs
from orchestrator.predicates import failures as false_leaves
from orchestrator.worker.acceptance import (
    Verdict,
    acceptance_verdict,
    accepted,
    accepted_values,
)
from orchestrator.worker.agent_steps import _emit_completed, _record_result
from orchestrator.worker.check_steps import check_accepted
from orchestrator.worker.exits import pending_request, request_run
from orchestrator.worker.inputs import release_run_credential
from orchestrator.worker.interrupts import park_interrupted, pending_resume_request
from orchestrator.worker.runtime_adapters import RuntimeRefused
from orchestrator.worker.tick_context import (
    _EvaluationUnavailable,
    _observed,
    _Plan,
    _Tick,
    latest_current_run,
)
from runtime.contract import AgentRuntimeError, ParkReason, RunStatus

__all__ = [
    "COMPLETION_EVENTS",
    "COMPLETION_UNREADABLE_FOR",
    "CompletionAnswer",
    "CompletionRefusal",
    "answer_checked",
    "answer_completion",
    "completion_outcome",
    "forceable",
    "pending_completion_request",
    "refuse_completion",
    "unreadable_too_long",
]

logger = logging.getLogger(__name__)

#: A completion request, then what answers it.
COMPLETION_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.COMPLETION_REQUESTED,
    EventType.STEP_ACCEPTED_ON_REQUEST,
    EventType.STEP_FORCED,
    EventType.COMPLETION_REFUSED,
    EventType.STEP_REDONE,
)

#: How long an unreadable check keeps a completion or retry request pending before it is refused
#: `unavailable`.
COMPLETION_UNREADABLE_FOR: Final = timedelta(minutes=2)


def unreadable_too_long(tick: _Tick, request: Event) -> bool:
    """Stamp the first tick `request` could not be checked on; True once
    `COMPLETION_UNREADABLE_FOR` has passed since."""
    first = tick.deps.unreadable_requests.setdefault(request.id, tick.now)
    return tick.now - first >= COMPLETION_UNREADABLE_FOR


async def answer_checked(
    tick: _Tick,
    request: Event,
    answer: Callable[[], Awaitable[bool]],
    refuse_unavailable: Callable[[], Awaitable[None]],
    *,
    what: str,
) -> bool:
    """`answer()` under a SAVEPOINT, returning what it does. An unreadable read rolls it back
    and leaves `request` pending, until `COMPLETION_UNREADABLE_FOR` after the first such tick,
    when `refuse_unavailable()` answers it."""
    try:
        with revert_on_error():
            async with tick.session.begin_nested():
                answered = await answer()
    except _EvaluationUnavailable as exc:
        # Refresh first: the rollback expired the ticket, and an expired read on an AsyncSession
        # raises.
        await tick.session.refresh(tick.ticket)
        tick.plan = _Plan()
        logger.exception(
            "ticket %s step %s: %s %s could not be checked (%s)",
            tick.ticket.external_key,
            tick.ticket.current_step,
            what,
            request.id,
            exc,
        )
        if unreadable_too_long(tick, request):
            await refuse_unavailable()
        return False
    tick.deps.unreadable_requests.pop(request.id, None)
    return answered


class CompletionRefusal(StrEnum):
    STALE = "stale"
    ISSUE_CLOSED = "issue_closed"
    NOT_FORCEABLE = "not_forceable"
    MID_TURN = "mid_turn"
    RELEASE_PENDING = "release_pending"
    UNMET = "unmet"
    CHANGED = "changed"
    UNMOVED = "unmoved"
    UNAVAILABLE = "unavailable"
    EXITED = "exited"
    REDONE = "redone"


#: The refusals a person may override, having read the failures. Never `UNMOVED`: forcing would
#: reopen a gate on the commit it rejected.
_OVERRIDABLE: Final[frozenset[CompletionRefusal]] = frozenset(
    {CompletionRefusal.UNMET, CompletionRefusal.CHANGED}
)

#: The statuses a run may be accepted from, by its kind. A check's RUNNING is its wait on
#: observed state; an agent's RUNNING is a turn in progress.
_FORCEABLE: Final[Mapping[str, frozenset[StepRunStatus]]] = {
    StepKind.AGENT: frozenset(
        {
            StepRunStatus.FAILED,
            StepRunStatus.TIMEOUT,
            StepRunStatus.CANCELLED,
            StepRunStatus.INTERRUPTED,
            StepRunStatus.IDLE,
        }
    ),
    StepKind.CHECK: frozenset(
        {
            StepRunStatus.FAILED,
            StepRunStatus.TIMEOUT,
            StepRunStatus.CANCELLED,
            StepRunStatus.RUNNING,
        }
    ),
}


def forceable(kind: str, status: str) -> bool:
    """Whether a run of `kind` may be accepted from `status`. The API and the loop both ask."""
    return status in _FORCEABLE.get(kind, frozenset())


async def pending_completion_request(session: AsyncSession, ticket_id: int) -> Event | None:
    """The ticket's completion request that nothing has answered yet."""
    return await pending_request(session, ticket_id, *COMPLETION_EVENTS)


@dataclass(frozen=True, slots=True)
class CompletionAnswer:
    outcome: Literal["pending", "accepted", "forced", "refused"]
    reason: str | None = None
    failures: tuple[str, ...] = ()
    overridable: bool = False


async def completion_outcome(
    session: AsyncSession, ticket_id: int, request_id: int
) -> CompletionAnswer:
    """What answered the request, or `pending`."""
    answer = (
        await session.execute(
            select(Event.type, Event.details)
            .where(
                Event.ticket_id == ticket_id,
                Event.type.in_(COMPLETION_EVENTS[1:]),
                Event.details["request_id"].as_integer() == request_id,
            )
            .limit(1)
        )
    ).first()
    if answer is None:
        return CompletionAnswer("pending")
    kind, details = answer
    if kind == EventType.STEP_ACCEPTED_ON_REQUEST:
        return CompletionAnswer("accepted")
    if kind == EventType.STEP_FORCED:
        return CompletionAnswer(
            "forced", failures=tuple(str(f) for f in details.get("failures") or ())
        )
    reason = details.get("reason")
    return CompletionAnswer(
        "refused",
        reason=reason if isinstance(reason, str) else None,
        failures=tuple(str(f) for f in details.get("failures") or ()),
        overridable=details.get("overridable") is True,
    )


async def answer_completion(tick: _Tick, request: Event) -> bool:
    """Answer `request`. True when the tick made a transition: the run accepted or forced, or an
    IDLE session stopped and parked when its result could not be read. An unreadable verdict raises
    `_EvaluationUnavailable` with nothing written."""
    # Here, not at the top: `reconcile` imports this module.
    from orchestrator.worker.reconcile import pending_unblock_request

    if tick.issue.state == "closed":
        await refuse_completion(
            tick, request, CompletionRefusal.ISSUE_CLOSED, "the issue is closed"
        )
        return False
    run = await request_run(tick, request)
    latest = await latest_current_run(tick.session, tick.ticket, tick.ticket.current_step)
    if run is None or latest is None or latest.id != run.id:
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.STALE,
            "a newer attempt started, or the ticket moved on",
        )
        return False
    step = tick.config.find_step(run.step_name)
    if not isinstance(step, AgentStep | CheckStep):
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.NOT_FORCEABLE,
            "only agent and check steps can be forced",
        )
        return False
    if (
        await pending_resume_request(tick.session, tick.ticket.id) is not None
        or await pending_unblock_request(tick.session, tick.ticket.id) is not None
    ):
        # Accepted here, a parked run would leave that request unanswered for ever.
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.RELEASE_PENDING,
            "a Resume or Unblock is answered first",
        )
        return False
    if isinstance(step, AgentStep):
        refusal = await _mid_turn(tick, run)
        if refusal is not None:
            await refuse_completion(tick, request, *refusal)
            return False
    if not forceable(run.kind, run.status):
        await refuse_completion(
            tick, request, CompletionRefusal.STALE, f"attempt {run.attempt} is {run.status}"
        )
        return False

    if isinstance(step, AgentStep):
        verdict = await acceptance_verdict(tick, step, run, every_leaf=True)
    else:
        result = await _observed(
            evaluate(step.conditions, tick.ctx, every_leaf=True),
            what=f"step {step.name} attempt {run.attempt}: conditions",
        )
        verdict = Verdict(result.value, false_leaves(result), (), needs=needs(result))
    if verdict.met and not verdict.unmoved:
        return await _accept_on_request(tick, step, run, request)
    if verdict.unmoved:
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.UNMOVED,
            "the rework has not moved the pull request its gate rejected",
            (*verdict.failures, *verdict.unmoved),
        )
        return False
    if not request.details.get("allow_unmet"):
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.UNMET,
            "its postconditions do not hold",
            verdict.failures,
        )
        return False
    # Every false leaf, never a short-circuit's first: a leaf the person never read cannot pass.
    if set(verdict.failures) <= set(request.details.get("seen_failures") or ()):
        return await _accept_on_request(
            tick, step, run, request, forced=verdict.failures, missing=verdict.needs
        )
    await refuse_completion(
        tick,
        request,
        CompletionRefusal.CHANGED,
        "the failures changed since the person read them",
        verdict.failures,
    )
    return False


async def _mid_turn(tick: _Tick, run: StepRun) -> tuple[CompletionRefusal, str] | None:
    """Why an agent run cannot be accepted while its session may be in a turn, or None. An IDLE
    row is the last poll's word: the session is asked again."""
    if run.status == StepRunStatus.RUNNING:
        return CompletionRefusal.MID_TURN, "the agent is mid-turn; interrupt it first"
    if run.status != StepRunStatus.IDLE:
        return None
    assert run.runtime_run_id is not None
    try:
        adapter = await tick.deps.adapter_for_run(tick.session, run)
        status = await adapter.status(run.runtime_run_id)
    except (AgentRuntimeError, RuntimeRefused) as exc:
        return CompletionRefusal.UNAVAILABLE, f"the session could not be read: {exc}"
    if status.is_terminal:
        return CompletionRefusal.STALE, f"the session ended by itself ({status})"
    if status is not RunStatus.IDLE:
        return CompletionRefusal.MID_TURN, "the agent is mid-turn; interrupt it first"
    return None


async def _accept_on_request(
    tick: _Tick,
    step: AgentStep | CheckStep,
    run: StepRun,
    request: Event,
    *,
    forced: tuple[str, ...] | None = None,
    missing: tuple[str, ...] = (),
) -> bool:
    """Accept `run`, or force it over `forced`, the failures that still hold, `missing` in
    words."""
    from_status = StepRunStatus(run.status)
    by = request.details.get("requested_by")
    # Before any write: the API names who asked on every request (`Actor.fields`), and a forced
    # run that names nobody would read as a plain success.
    if forced is not None and not isinstance(by, str):
        logger.warning(
            "ticket %s: completion request %s names no requester; refused",
            tick.ticket.external_key,
            request.id,
        )
        await refuse_completion(
            tick, request, CompletionRefusal.STALE, "the request names nobody who asked"
        )
        return False
    extra: dict[str, str] | None = None
    if isinstance(step, AgentStep):
        extra = await accepted_values(tick, step)
        if from_status in (StepRunStatus.IDLE, StepRunStatus.INTERRUPTED):
            ended = await _end_session(tick, step, run, request, from_status, forced=forced)
            if ended != "ended":
                return ended == "parked"
    elif from_status == StepRunStatus.RUNNING:
        run.ended_at = tick.now
    run.status = StepRunStatus.SUCCEEDED
    run.failure_reason = None
    answer = {
        "step": step.name,
        "attempt": run.attempt,
        "request_id": request.id,
        "requested_by": by,
        "requested_via": request.details.get("requested_via"),
        "from_status": from_status.value,
    }
    if forced is None:
        await tick.emit(EventType.STEP_ACCEPTED_ON_REQUEST, run=run, **answer)
        tick.plan.comments.append(
            f"`{step.name}` (attempt {run.attempt}) is accepted on request by {by}: its "
            "expected results are there now."
        )
    else:
        run.forced_by = by
        await tick.emit(EventType.STEP_FORCED, run=run, **answer, failures=list(forced))
        still = "\n".join(f"- {need}" for need in missing) or "\n".join(f"- `{f}`" for f in forced)
        tick.plan.comments.append(
            f"`{step.name}` (attempt {run.attempt}) was forced complete by {by} from the "
            f"dashboard. These were still missing:\n{still}"
        )
    tick.deps.unreadable_requests.pop(request.id, None)
    if isinstance(step, AgentStep):
        await accepted(tick, step, run, extra=extra)
    else:
        await check_accepted(tick, step, run)
    return True


async def _end_session(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    request: Event,
    from_status: StepRunStatus,
    *,
    forced: tuple[str, ...] | None,
) -> Literal["ended", "refused", "parked"]:
    """End a parked or idle session and record its result, as `recheck_parked` does. `ended` when
    it did; otherwise the request is refused `unavailable`, and `parked` when an IDLE session was
    stopped first, so that stop is recorded as the person's."""
    assert run.runtime_run_id is not None
    try:
        adapter = await tick.deps.adapter_for_run(tick.session, run)
        if from_status == StepRunStatus.IDLE:
            # Stopped, not finished: `finish()` returns before the session ends, and the loop's
            # own idle handling would act on the run across ticks (decision 78).
            await adapter.interrupt(run.runtime_run_id, ParkReason.USER)
    except (AgentRuntimeError, RuntimeRefused) as exc:
        await refuse_completion(
            tick, request, CompletionRefusal.UNAVAILABLE, f"the session could not be reached: {exc}"
        )
        return "refused"
    if from_status == StepRunStatus.IDLE:
        await release_run_credential(tick, run.id)
    try:
        result = await adapter.result(run.runtime_run_id)
    except (AgentRuntimeError, RuntimeRefused) as exc:
        stopped = from_status == StepRunStatus.IDLE
        if stopped:
            await park_interrupted(
                tick, step, run, REQUESTED_VIA_WORDS[requested_via(request.details)]
            )
        await refuse_completion(
            tick,
            request,
            CompletionRefusal.UNAVAILABLE,
            f"the run's result could not be read: {exc}",
        )
        return "parked" if stopped else "refused"
    try:
        # Ends the park, which on a host holds the branch a later fresh attempt checks out.
        await adapter.finish(run.runtime_run_id)
    except AgentRuntimeError as exc:
        logger.warning(
            "ticket %s step %s: run %s accepted on request with its park kept: %s",
            tick.ticket.external_key,
            step.name,
            run.runtime_run_id,
            exc,
        )
    await _record_result(tick, step, run, result)
    run.ended_at = run.ended_at or tick.now
    by = request.details.get("requested_by")
    if forced is None:
        await _emit_completed(tick, step, run, result, accepted_on_request=by)
    else:
        await _emit_completed(tick, step, run, result, forced_by=by)
    return "ended"


async def refuse_completion(
    tick: _Tick,
    request: Event,
    reason: CompletionRefusal,
    detail: str,
    failures: Sequence[str] = (),
) -> None:
    """No issue comment: the dialog and the timeline show it."""
    run = await request_run(tick, request)
    await tick.emit(
        EventType.COMPLETION_REFUSED,
        run=run,
        step=request.details.get("step"),
        attempt=request.details.get("attempt"),
        request_id=request.id,
        reason=reason.value,
        detail=detail,
        failures=list(failures),
        overridable=reason in _OVERRIDABLE,
    )
    tick.deps.unreadable_requests.pop(request.id, None)
