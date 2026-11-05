"""Action steps (design.md §4): the closed action registry, dispatched outside the
runtime adapter seam — no agent, no workspace, no prompt."""


from __future__ import annotations

from typing import Final

from orchestrator.actions import ACTIONS, ActionContext, UnknownActionError
from orchestrator.config import (
    ActionStep,
)
from orchestrator.db.models import (
    StepKind,
    StepRun,
    StepRunStatus,
)
from orchestrator.events import EventType
from orchestrator.worker.failures import _PERMANENT_CONFIG_ERRORS, _fail_permanently, _fail_run
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    LeaseLost,
    _advance,
    _current,
    _insert_run,
    _next_attempt,
    _retry_ready,
    _runs_for,
    _Tick,
)
from runtime.contract import (
    AgentRuntimeError,
)

#: `tick.emit`'s own keyword parameters. An action's `ActionResult.details` must not shadow them
#: when spread into an `ACTION_EXECUTED` event, or the call raises an opaque `TypeError`.
_EMIT_KEYWORDS: Final = frozenset({"run", "step", "attempt"})


async def _action_step(tick: _Tick, step: ActionStep) -> None:
    """`kind: action` (design.md §4) — dispatched to a registered action, never a substrate.

    Shaped like `_agent_step`: the same `(ticket, step, attempt)` lookup and the same three
    outcomes for the latest row (start fresh, rework, retry-or-wait) — only the RUNNING branch
    differs, because an action has no adapter to poll.
    """
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = _current(runs)

    if latest is None:
        await _start_action(
            tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
        )
        return

    status = StepRunStatus(latest.status)
    if status == StepRunStatus.RUNNING:
        # An action executes synchronously inside `_start_action`, so a RUNNING row means a
        # previous tick was interrupted mid-execution, not work still in flight. Fail it so the
        # retry/escalate machinery can recover it.
        await _fail_run(
            tick,
            step,
            latest,
            StepRunStatus.FAILED,
            "action step_run left running by an interrupted worker",
            event_type=EventType.ACTION_FAILED,
            summary="This step was cut off when Factorial restarted.",
        )
        return
    if status == StepRunStatus.SUCCEEDED:
        # Rework: a gate sent the ticket back here (`gate.onReject.rerunStep`) — a new attempt,
        # as `_agent_step` treats it.
        await _start_action(
            tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
        )
        return
    if status in _FAILED_STATUSES:
        if await _retry_ready(tick, step, latest):
            await _start_action(
                tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
            )
        return


async def _start_action(tick: _Tick, step: ActionStep, *, attempt: int) -> None:
    """Create the `(ticket, step, attempt)` row and run the registered action to completion.

    Unlike `_start_attempt`, there is no substrate to spawn and later poll: the action executes
    inside this call, so the row moves from RUNNING to a terminal status within one tick.
    """
    run = StepRun(
        ticket_id=tick.ticket.id,
        step_name=step.name,
        kind=StepKind.ACTION,
        attempt=attempt,
        status=StepRunStatus.RUNNING,
        started_at=tick.now,
    )
    if not await _insert_run(tick, run):
        return

    action = ACTIONS.get(step.action)
    if action is None:
        # `ActionStep._check_registry` refuses an unregistered name at parse time, so reaching
        # here means the two action tables drifted apart — a code bug, not bad input. Not
        # classified permanent: an operator cannot fix code drift by editing `.factorial/`, so
        # escalating it as "misconfiguration" would send them chasing a fix they cannot make.
        raise UnknownActionError(
            f"no implementation for action {step.action!r}; the registry is closed"
        )

    try:
        contract = tick.host().forge.contract(tick.project.github_repo)

        ctx = ActionContext(
            source=tick.source,
            values=tick.values,
            github=contract,
        )
        result = await action(ctx)
    except (LeaseLost, AgentRuntimeError):
        # The same structural guard as `_start_attempt`'s: `_start_action` never renews a lease or
        # spawns today, but the bare `RuntimeError` catch below must not depend on that.
        #
        # No reason-aware arm here, unlike the spawn call site. An action step has no `agent:` and
        # no `workspace:` and never touches the adapter (design.md §4), so a `SpawnError` arriving
        # here is code drift, not a classified refusal — reading a reason would dress a bug up as a
        # misconfiguration and send an operator to edit `.factorial/`.
        raise
    except _PERMANENT_CONFIG_ERRORS as exc:
        # The action-path twin of `_start_attempt`'s wrap, scoped to the PRE-tag region:
        # `fix_the_contract`'s irreversible `create_or_move_tag` and everything after it are the
        # action's own business, and a failure there keeps the tick's ordinary rollback/retry. The
        # post-tag verbs already carry their own log-and-continue handlers, and the one post-tag
        # read (`ctx.source.pull_requests()`) is `AsyncOnce`-memoized and already resolved
        # pre-tag, so it cannot raise here at all.
        #
        # Session purity, as in `_start_attempt`: every member is a Python-level failure, never a
        # failed SQL statement, and nothing on this path writes before one can raise.
        await _fail_permanently(tick, step, run, exc, event_type=EventType.ACTION_FAILED)
        return
    run.ended_at = tick.now

    if not result.ok:
        await _fail_run(
            tick,
            step,
            run,
            StepRunStatus.FAILED,
            result.reason or "action failed",
            event_type=EventType.ACTION_FAILED,
        )
        return

    run.status = StepRunStatus.SUCCEEDED
    # `**result.details` must not shadow `emit`'s own keyword parameters. Nothing enforces that
    # for a future action, and the collision would surface as an opaque "got multiple values for
    # keyword argument" TypeError, so fail loud here instead.
    if collision := _EMIT_KEYWORDS & set(result.details):
        raise ValueError(
            f"action {step.action!r} detail key(s) {sorted(collision)} collide with "
            "tick.emit's own parameters (run, step, attempt)"
        )
    await tick.emit(
        EventType.ACTION_EXECUTED, run=run, step=step.name, attempt=run.attempt, **result.details
    )
    await _advance(tick, step, run, step.on_success, extra=dict(result.details))
