"""Check steps: a stage that is only its acceptance gate. The step completes when its
conditions hold on observed state — no agent, no container, no action to run."""


from __future__ import annotations

import logging

from orchestrator.config import CheckStep
from orchestrator.db.models import (
    StepKind,
    StepRun,
    StepRunStatus,
)
from orchestrator.events import EventType
from orchestrator.predicates import Unmet, evaluate
from orchestrator.worker.failures import _fail_run, spoken
from orchestrator.worker.templates import _pr_for, _pr_values
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    _advance,
    _current,
    _EvaluationUnavailable,
    _insert_run,
    _next_attempt,
    _observed,
    _retry_ready,
    _runs_for,
    _Tick,
    _timeout,
)

logger = logging.getLogger(__name__)


async def _check_step(tick: _Tick, step: CheckStep) -> None:
    """`kind: check` — the fourth dispatch arm: evaluated, never spawned.

    Shaped like `_action_step`: the same `(ticket, step, attempt)` lookup and the same
    latest-row outcomes (start fresh, rework, retry-or-wait). The RUNNING branch differs from
    BOTH siblings: for a check step RUNNING is the normal waiting state, re-evaluated every
    tick, not evidence of an interrupted worker — an interrupted tick loses nothing here,
    because the next one just observes again against the same `started_at` deadline.
    """
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = _current(runs)

    if latest is None:
        await _start_check(
            tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
        )
        return

    status = StepRunStatus(latest.status)
    if status == StepRunStatus.RUNNING:
        await _evaluate_check(tick, step, latest)
        return
    if status == StepRunStatus.SUCCEEDED:
        # Rework: a gate sent the ticket back here (`gate.onReject.rerunStep`) — a new attempt,
        # as the other kinds treat it, and a new attempt is a new deadline against the CURRENT
        # observed state.
        await _start_check(
            tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
        )
        return
    if status in _FAILED_STATUSES:
        if await _retry_ready(tick, step, latest):
            await _start_check(
                tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
            )
        return


async def _start_check(tick: _Tick, step: CheckStep, *, attempt: int) -> None:
    """Create the `(ticket, step, attempt)` row — the timeout's anchor — and evaluate once
    immediately, so conditions that already hold complete the step on this same tick."""
    run = StepRun(
        ticket_id=tick.ticket.id,
        step_name=step.name,
        kind=StepKind.CHECK,
        attempt=attempt,
        status=StepRunStatus.RUNNING,
        started_at=tick.now,
    )
    if not await _insert_run(tick, run):
        return
    try:
        await _evaluate_check(tick, step, run)
    except _EvaluationUnavailable:
        # The row and its STEP_STARTED must survive an unreadable first evaluation: letting the
        # act-phase rollback take them would re-arm `started_at` every tick, so a persistently
        # erroring read would keep the timeout from ever starting to count. The step HAS started
        # waiting; only the verdict is unreadable, and later ticks re-ask.
        logger.exception(
            "ticket %s step %s attempt %d: first evaluation failed; the wait is anchored and "
            "re-asked next tick",
            tick.ticket.external_key,
            step.name,
            attempt,
        )


async def _evaluate_check(tick: _Tick, step: CheckStep, run: StepRun) -> None:
    """One observation of the conditions: complete, keep waiting, or time out.

    Waiting consumes nothing — no event, no attempt — mirroring `_start_attempt`'s
    unmet-preconditions semantics. The deadline is what bounds the wait, because a predicate
    like `ci_green` cannot tell "red" from "still running" (predicates/library.py) and only
    time can; at the deadline the failing leaves become the failure reason, so the escalation
    names what never held rather than just "timed out".
    """
    assert run.started_at is not None  # a check run is created started (`_start_check`)
    deadline = run.started_at + _timeout(tick.config, step)
    try:
        result = await _observed(
            evaluate(step.conditions, tick.ctx),
            what=f"step {step.name} attempt {run.attempt}: conditions",
        )
    except _EvaluationUnavailable as exc:
        # Before the deadline an unreadable verdict says nothing: propagate, the act phase rolls
        # this tick back, and the next one re-asks — the agent path's exact semantics. PAST the
        # deadline the wait is over either way; without this arm a persistently erroring read
        # (the 2026-08-21 check-runs 403 shape) would disable the timeout entirely and park the
        # ticket with nothing on it. No POSTCONDITION_FAILED here — that event asserts the
        # predicates evaluated false, and they evaluated nothing.
        if tick.now < deadline:
            raise
        await _fail_run(
            tick,
            step,
            run,
            StepRunStatus.TIMEOUT,
            f"conditions unreadable after {_timeout(tick.config, step)}: {exc.__cause__}",
            reason="timeout",
            event_type=EventType.CHECK_FAILED,
            summary=f"Couldn't read GitHub for {spoken(_timeout(tick.config, step))} to check "
            "this step.",
        )
        return
    if result.value:
        run.status = StepRunStatus.SUCCEEDED
        run.ended_at = tick.now
        await check_accepted(tick, step, run)
        return

    if tick.now < deadline:
        logger.debug(
            "ticket %s step %s attempt %d: conditions not yet met; waiting",
            tick.ticket.external_key,
            step.name,
            run.attempt,
        )
        return

    unmet = Unmet.of(result)
    await tick.emit(
        EventType.POSTCONDITION_FAILED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        failures=list(unmet.rules),
        needs=list(unmet.words),
    )
    timeout = _timeout(tick.config, step)
    await _fail_run(
        tick,
        step,
        run,
        StepRunStatus.TIMEOUT,
        f"conditions still unmet after {timeout}: {'; '.join(unmet.rules)}",
        reason="timeout",
        event_type=EventType.CHECK_FAILED,
        summary=f"Still waiting after {spoken(timeout)} for {'; '.join(unmet.words)}.",
    )


async def check_accepted(tick: _Tick, step: CheckStep, run: StepRun) -> None:
    """The met path, for a caller that has just seen the conditions hold and marked `run`."""
    await _advance(tick, step, run, step.on_success, extra=await _check_pr_values(tick, step))


async def _check_pr_values(tick: _Tick, step: CheckStep) -> dict[str, str]:
    """`{pr_url}` for the outcome, resolved from the step's own `pr:` matcher.

    The gate's shape, not `_accept`'s: a check step has no `workspace.branch` to key the
    lookup off, so the config names the PR it is about directly, and `_pr_for` widens it the
    way every matcher is widened.
    """
    if step.pr is None:
        return {}
    pull_request = await _pr_for(tick, step.pr, what=f"check {step.name}: pull request listing")
    return await _pr_values(tick, None, pull_request)
