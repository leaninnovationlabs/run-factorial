"""A gate's rework of an agent step continues the previous attempt's session in its kept
workspace (decision 56), or starts fresh and says why.

Only a rework, or a person's Retry, continues a session. An automatic retry after a failure
starts fresh.

The rework window (`_rejecting_gates`) lives here too: a rework's inputs and its acceptance both
read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select

from orchestrator.config import AgentStep, GateReject, GateStep
from orchestrator.db.models import Event, Runtime, StepRun, StepRunStatus
from orchestrator.events import EventType
from orchestrator.worker.tick_context import _runs_for, _Tick
from runtime.contract import ResumeFrom, ResumeMode

#: A workspace this close to the end of its retention is not adopted: the reaper could take it
#: between the decision and the spawn.
REAPER_MARGIN: Final = timedelta(hours=1)


class SkipReason(StrEnum):
    """`SESSION_RESUME_SKIPPED.reason`."""

    RUNTIME_CHANGED = "runtime_changed"
    CLI_VERSION_CHANGED = "cli_version_changed"
    PAST_RETENTION = "past_retention"
    NO_SESSION = "no_session"
    #: The substrate refused the resume with `SessionUnavailable`.
    NOT_KEPT = "not_kept"
    #: The substrate found the failed run on no branch (`NoBranchHeld`).
    NO_BRANCH = "no_branch"


@dataclass(frozen=True, slots=True)
class Skip:
    reason: SkipReason
    previous_step_run_id: int


@dataclass(frozen=True, slots=True)
class Adopt:
    resume_from: ResumeFrom
    previous_step_run_id: int
    #: The workspace the session lives in: the first attempt that created it, however many
    #: reworks have adopted it since. The keep rule reads it off `agent_spawned`.
    workspace_run_id: str


@dataclass(frozen=True, slots=True)
class Rejection:
    """A gate that sent a step back and still stands rejected."""

    gate: GateStep
    on_reject: GateReject
    #: The gate's latest run, the REJECTED one.
    run: StepRun


async def _rejecting_gates(tick: _Tick, step: AgentStep, attempt: int) -> list[Rejection]:
    """The gates that sent `step` back and still stand rejected, in config order: the rework
    window. Empty outside it.

    `attempt > 1` is not by itself proof of a rework — a retry after failure also increments it.
    The guard that matters is the gate's latest run being REJECTED right now. That holds on a
    retry OF a rework too, which is wanted: the reviewer's request stays outstanding until the
    gate decides again. The window closes when the gate's latest run stops being REJECTED.

    Nothing is remembered: the gate's `step_run` row is already durable, and its status is read
    fresh, like every other fact this loop reasons about. Where two gates are REJECTED at once (a
    hand-written config can do that), the caller takes them in this order.
    """
    if attempt <= 1:
        return []
    window: list[Rejection] = []
    for gate in tick.config.steps:
        if not isinstance(gate, GateStep):
            continue
        on_reject = gate.gate.on_reject
        if on_reject is None or on_reject.rerun_step != step.name:
            continue
        gate_runs = await _runs_for(tick.session, tick.ticket, gate.name)
        if gate_runs and StepRunStatus(gate_runs[-1].status) == StepRunStatus.REJECTED:
            window.append(Rejection(gate, on_reject, gate_runs[-1]))
    return window


async def _latest_details(tick: _Tick, run: StepRun, event_type: EventType) -> dict[str, Any]:
    details = (
        await tick.session.execute(
            select(Event.details)
            .where(
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type == event_type,
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(details or {})


async def _resume_plan(
    tick: _Tick,
    prev: StepRun,
    runtime: Runtime | None,
    pin: str,
    *,
    mode: ResumeMode = "rework",
) -> Adopt | Skip:
    """Whether the rework or retry of `prev` continues its session; `pin` is the CLI version the
    new attempt boots."""
    if runtime is None or prev.runtime_id != runtime.id:
        return Skip(SkipReason.RUNTIME_CHANGED, prev.id)
    spawned = await _latest_details(tick, prev, EventType.AGENT_SPAWNED)
    # The session log is vendor-internal: another CLI version may not read it.
    if spawned.get("cli_pin") != pin:
        return Skip(SkipReason.CLI_VERSION_CHANGED, prev.id)
    retention = tick.deps.sessions.retention
    # The keep rule does not keep a non-live run with no end time, so neither is it adopted.
    if prev.ended_at is None or prev.ended_at < tick.now - retention + REAPER_MARGIN:
        return Skip(SkipReason.PAST_RETENTION, prev.id)
    session_id = await _ended_session(tick, prev)
    if session_id is None or prev.runtime_run_id is None:
        return Skip(SkipReason.NO_SESSION, prev.id)
    workspace = spawned.get("workspace_run_id")
    return Adopt(
        resume_from=ResumeFrom(run_id=prev.runtime_run_id, session_id=session_id, mode=mode),
        previous_step_run_id=prev.id,
        workspace_run_id=workspace if isinstance(workspace, str) else prev.runtime_run_id,
    )


async def _ended_session(tick: _Tick, run: StepRun) -> str | None:
    """The session id `run`'s end recorded, a failed end's included."""
    for details in (
        await tick.session.execute(
            select(Event.details)
            .where(
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type.in_((EventType.AGENT_COMPLETED, EventType.AGENT_FAILED)),
            )
            .order_by(Event.id.desc())
        )
    ).scalars():
        session_id = (details or {}).get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None
