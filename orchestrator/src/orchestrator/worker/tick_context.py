"""The tick's shared context: per-tick state (`_Tick`, `_Plan`), the
evaluation-read guard (`_observed`), row loaders, and the outcome/retry policy readers every
step kind consumes. It imports only `context` from the worker package, so every step module can
import it."""


from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.bindings import Bindings, ForgePort
from orchestrator.config import (
    ActionStep,
    AgentStep,
    CheckStep,
    GateStep,
    Outcome,
    ProcessConfig,
    Retry,
    expand,
    expand_principals,
)
from orchestrator.db.models import (
    Agent,
    EnteredBy,
    Event,
    ProcessVersion,
    Project,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
    current_runs,
)
from orchestrator.db.session import SessionScope
from orchestrator.events import EventType, append
from orchestrator.factorial import AgentPins
from orchestrator.github import (
    IssueState,
    StateSource,
)
from orchestrator.logs import keep, note
from orchestrator.predicates import EvalContext, Unmet
from orchestrator.worker.context import Projector, ReconcileDeps
from runtime.contract import (
    RunStatus,
    RuntimeAdapter,
)

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    """Raised by `_start_attempt` when the tick's lease renewal fails immediately before a spawn
    (decision 41). Never caught locally — see `_start_attempt` for why propagating is the correct
    shape."""


class _EvaluationUnavailable(Exception):
    """Observed state could not be READ while deciding a step's verdict — a predicate evaluation,
    or one of the gate's direct verdict reads, raised instead of answering.

    Raised only by `_observed`. `reconcile_ticket`'s act-phase wrap catches it, and so does a
    check step, which keeps its row and re-asks next tick. A failed read
    says nothing about the ticket's state, so the tick rolls back the half-made transition, logs
    loudly, and still projects the last persisted state. Without this, one erroring predicate
    aborts reconcile before `_project` and the ticket's whole label/comment stream goes dark for
    as long as the error persists.

    Worker-private: a control-flow shape internal to one tick, not a contract.
    """


async def _observed[T](reading: Awaitable[T], *, what: str) -> T:
    """Await an EVALUATION read — an `evaluate()` call, or a direct observed-state read a verdict
    rests on — converting any failure into `_EvaluationUnavailable`. The original travels as
    `__cause__`, so one `logger.exception` keeps the full chained traceback.

    Wrapped beyond the `evaluate()` sites wherever a verdict or an outcome's `{pr_url}` reads
    GitHub directly (gate, check step, rework, page, `_accept`); grep `_observed(` for the
    current set.

    Not wrapped, deliberately: WRITE acts (`_approve`'s merge), which keep whole-tick
    rollback-and-retry rather than half-applying; `_build_run_spec`'s input resolution, inside a
    spawn already under way; and the Postgres-only reads (`_resolve_slug`, `_runs_for`) — a
    session that cannot SELECT could not append `_project`'s events either, so falling through
    would just move the abort into projection.

    `except Exception` rather than a curated tuple: what a predicate raises is vendor-shaped
    (`githubkit`'s `RequestFailed` is a plain `Exception`) and unowned here. A genuine bug stays
    as loud as before, it just no longer takes label projection down with it. `CancelledError` is
    a `BaseException` and still propagates.
    """
    try:
        return await reading
    except Exception as exc:
        raise _EvaluationUnavailable(what) from exc


#: Agent-run statuses that the retry/escalate path consumes.
#:
#: `INTERRUPTED` is deliberately absent. A human who stopped a run to redirect it has not
#: experienced a failure the step should recover from, and retrying ten minutes later is the
#: precise opposite of what they asked for (decision 26). An interrupted run parks its ticket
#: instead, and a human releases it.
_FAILED_STATUSES: Final = frozenset(
    {StepRunStatus.FAILED, StepRunStatus.TIMEOUT, StepRunStatus.CANCELLED}
)


_RUN_STATUS: Final[Mapping[RunStatus, StepRunStatus]] = {
    RunStatus.SUCCEEDED: StepRunStatus.SUCCEEDED,
    RunStatus.FAILED: StepRunStatus.FAILED,
    RunStatus.TIMEOUT: StepRunStatus.TIMEOUT,
    RunStatus.CANCELLED: StepRunStatus.CANCELLED,
    RunStatus.INTERRUPTED: StepRunStatus.INTERRUPTED,
}


#: Fallback when neither the step nor `defaults` sets one. Matches the GitHub App token's life,
#: which is the real ceiling on any run (design.md §7.1.3).
_DEFAULT_TIMEOUT: Final = timedelta(hours=1)


@dataclass(slots=True)
class _Plan:
    """What this tick wants the ticket to look like. Applied once, at the end.

    `label` is the *status* label an outcome asked for; `assign` and `comment` are only ever
    set by an outcome that actually fired, so a tick with no transition posts nothing.
    """

    label: str | None = None
    assign: tuple[str, ...] | None = None
    comments: list[str] = field(default_factory=list)
    #: `(pull request number, body)`: comments for a pull request rather than the issue.
    pr_comments: list[tuple[int, str]] = field(default_factory=list)
    #: Only the terminal step sets this, and only when `terminal.actions.closeTicket` is true.
    close_issue: bool = False


@dataclass(frozen=True, slots=True)
class Launch:
    """A run this pass started, in plain values: a rollback expires the ORM rows it came from."""

    adapter: RuntimeAdapter
    runtime_run_id: str
    step_run_id: int
    ticket_id: int
    step: str
    attempt: int
    repo: str
    #: Releases the run's credential with the run (`ForgePort.release_run_credential`); None
    #: where the tick had no binding.
    forge: ForgePort | None


@dataclass(slots=True)
class Launches:
    """The runs one pass launched, settled once its transaction has ended (decision 87).

    The `step_run` row is a spawn's only idempotency record: a launch whose row did not commit
    is a run nothing tracks, and the next pass, finding no row, launches another. Settling
    re-reads the rows rather than trusting the exception, because a SAVEPOINT can absorb a raise
    and still commit the pass without the row.
    """

    started: list[Launch] = field(default_factory=list)

    def record(self, launch: Launch) -> None:
        self.started.append(launch)

    async def settle(self, scope: SessionScope) -> None:
        """Cancel each launch whose row does not hold its run. Never raises."""
        if not self.started:
            return
        try:
            async with scope() as session:
                rows = await session.execute(
                    select(StepRun.id, StepRun.runtime_run_id).where(
                        StepRun.id.in_([launch.step_run_id for launch in self.started])
                    )
                )
                committed = {(row_id, run_id) for row_id, run_id in rows}
        except Exception:
            # Cancelling a run whose row did commit costs one failed attempt; leaving one whose
            # row did not costs a run per pass until quarantine.
            logger.exception("could not read back this pass's launches; cancelling them all")
            committed = set()
        for launch in self.started:
            if (launch.step_run_id, launch.runtime_run_id) not in committed:
                await _cancel_orphan(launch)


async def _cancel_orphan(launch: Launch) -> None:
    logger.error(
        "ticket id=%s step %s attempt %d: the pass that launched run %s did not commit it; "
        "cancelling it",
        launch.ticket_id,
        launch.step,
        launch.attempt,
        launch.runtime_run_id,
    )
    try:
        await launch.adapter.cancel(launch.runtime_run_id)
    except Exception:
        logger.exception(
            "ticket id=%s: could not cancel run %s; it may still be running",
            launch.ticket_id,
            launch.runtime_run_id,
        )
    if launch.forge is None:
        return
    try:
        await launch.forge.release_run_credential(launch.repo, launch.step_run_id)
    except Exception:
        logger.exception(
            "ticket id=%s: could not release the run credential for run %s",
            launch.ticket_id,
            launch.step_run_id,
        )


@dataclass(slots=True)
class _Tick:
    session: AsyncSession
    deps: ReconcileDeps
    ticket: Ticket
    project: Project
    config: ProcessConfig
    #: The pinned snapshot's `agent_pins` — where each agent it names was read from when the
    #: version was registered. NULL for a snapshot from before revision 0013; `resolve_agent`
    #: falls back to the project-wide `agent` row exactly as it did then.
    agent_pins: AgentPins | None
    #: The pinned version's `source_commit_sha`: the commit every run reads its config at.
    config_sha: str
    source: StateSource
    issue: IssueState
    now: datetime
    values: dict[str, str]
    ctx: EvalContext
    plan: _Plan
    #: The same owner/ttl `loop.tick()` acquired this ticket's lease with (decision 41), threaded
    #: through rather than re-read from settings so a caller that overrides them gets a renewal that
    #: agrees with the acquisition.
    lease_owner: str
    lease_ttl: timedelta
    #: The project's tracker and forge. Required for a live tick.
    bindings: Bindings | None = None
    #: The pass's launch registry (decision 87); None where nothing settles it, as in tests that
    #: call `reconcile_ticket` directly.
    launches: Launches | None = None

    def host(self) -> Bindings:
        """The tracker/forge pair this tick observed through."""
        if self.bindings is None:
            raise RuntimeError(f"project {self.project.name!r} has no tracker/forge binding")
        return self.bindings

    def projector(self) -> Projector:
        """The host's projector for this ticket's repo."""
        return self.host().projector(self.project.github_repo)  # type: ignore[return-value]

    async def emit(
        self, event_type: EventType, *, run: StepRun | None = None, **details: object
    ) -> None:
        await append(
            self.session,
            type=event_type,
            project_id=self.ticket.project_id,
            ticket_id=self.ticket.id,
            step_run_id=run.id if run is not None else None,
            **details,
        )


async def _load_project(session: AsyncSession, project_id: int) -> Project:
    project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    return project


async def _load_pinned_config(
    session: AsyncSession, process_version_id: int
) -> tuple[ProcessConfig, AgentPins | None, str]:
    """Re-parse the snapshot the ticket pinned at admission, the agent pins beside it (§2.8), and
    the commit it was read at.

    Never `load_process()` of the repo's current file: a config edit creates a new version row and
    must not change the process under a ticket already flowing through it. The pins extend that to
    the agents the steps name, which used to resolve through a project-wide row any sync rewrote;
    NULL for a snapshot registered before they were recorded (revision 0013).
    """
    row = (
        await session.execute(select(ProcessVersion).where(ProcessVersion.id == process_version_id))
    ).scalar_one()
    return ProcessConfig.model_validate(row.content), row.agent_pins, row.source_commit_sha


async def _runs_for(session: AsyncSession, ticket: Ticket, step_name: str) -> Sequence[StepRun]:
    """The current runs at `step_name`, oldest attempt first. A run from before the ticket's
    last restart, or one a redo set aside, is history the loop must not act on (decisions 67,
    76)."""
    return list(
        (
            await session.execute(
                select(StepRun)
                .where(
                    StepRun.ticket_id == ticket.id,
                    current_runs(ticket.generation),
                    StepRun.step_name == step_name,
                )
                .order_by(StepRun.attempt)
            )
        )
        .scalars()
        .all()
    )


def _current(runs: Sequence[StepRun]) -> StepRun | None:
    """The step's latest attempt of `runs`, noted on the tick's log lines. Kept, because the run
    existed before the act phase, and a rollback of it does not erase the run."""
    latest = runs[-1] if runs else None
    if latest is not None:
        keep(step_run_id=latest.id, attempt=latest.attempt)
    return latest


async def latest_current_run(
    session: AsyncSession, ticket: Ticket, step_name: str, *, for_update: bool = False
) -> StepRun | None:
    """The highest-attempt current run at `step_name`, row-locked when `for_update`."""
    query = (
        select(StepRun)
        .where(
            StepRun.ticket_id == ticket.id,
            current_runs(ticket.generation),
            StepRun.step_name == step_name,
        )
        .order_by(StepRun.attempt.desc())
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def _next_attempt(session: AsyncSession, ticket_id: int, step_name: str) -> int:
    """The attempt number the next start at this step will carry.

    One function because every site needing this number must produce the SAME one: the starters
    (`_agent_step`, `_action_step`, `_check_step`) hand it to the row they are about to create,
    and `_enter_step` stamps it on `step_entered` before any row exists. A disagreement files
    the entry event under a different `step#attempt` than the run that follows it, which splits
    one attempt across two blocks on the ticket page.

    Every generation's rows, not `_runs_for`'s: numbering keeps counting across a restart, so a
    new generation's first start never meets an old row on the unique key and reads as a lost
    race for ever. Rows a redo set aside count too, for the same reason.
    """
    highest = await session.scalar(
        select(func.max(StepRun.attempt)).where(
            StepRun.ticket_id == ticket_id, StepRun.step_name == step_name
        )
    )
    return (highest or 0) + 1


async def _ordinal(session: AsyncSession, run: StepRun) -> int:
    """`run`'s place among the current runs at its step, from 1: what a retry budget counts,
    since a restart or a redo grants the whole budget again while the attempt number keeps
    counting."""
    count = await session.scalar(
        select(func.count())
        .select_from(StepRun)
        .where(
            StepRun.ticket_id == run.ticket_id,
            StepRun.step_name == run.step_name,
            current_runs(run.generation),
            StepRun.attempt <= run.attempt,
        )
    )
    return int(count or 0)


async def _entered_by(session: AsyncSession, run: StepRun) -> str:
    """`EnteredBy` for a row about to be inserted: the reason of the newest `step_entered` that
    predicted this attempt number, else `retry`.

    Read from the log rather than passed in by the four starters because the entry IS the
    decision: `_enter_step` stamped the number this run now carries, and the ticket page files the
    entry and the run under one `step#attempt` from the same match. One read of the ticket's own
    events (`event_ticket_idx`), once per run created.
    """
    reason = await session.scalar(
        select(Event.details["reason"].astext)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.type == EventType.STEP_ENTERED,
            Event.details["step"].astext == run.step_name,
            Event.details["attempt"].as_integer() == run.attempt,
        )
        .order_by(Event.id.desc())
        .limit(1)
    )
    return reason if reason is not None else EnteredBy.RETRY


async def _insert_run(tick: _Tick, run: StepRun) -> bool:
    """Insert `run` with its `STEP_STARTED`. False when another runner already holds the same
    `(ticket_id, step_name, attempt)`.

    A SAVEPOINT, not a bare flush: that unique key is what makes starts idempotent (design.md
    §3.1), and losing the race must cost this tick only. An IntegrityError deactivates the
    transaction it happens in.
    """
    run.generation = tick.ticket.generation
    # Before the SAVEPOINT, so a lost race costs this tick the one read and nothing more.
    run.entered_by = await _entered_by(tick.session, run)
    try:
        async with tick.session.begin_nested():
            tick.session.add(run)
            await tick.session.flush()
            await tick.emit(
                EventType.STEP_STARTED,
                run=run,
                step=run.step_name,
                attempt=run.attempt,
                kind=run.kind,
            )
    except IntegrityError:
        logger.info(
            "ticket %s step %s attempt %d already started by another runner",
            tick.ticket.external_key,
            run.step_name,
            run.attempt,
        )
        return False
    note(step_run_id=run.id, attempt=run.attempt)
    return True


#: Why the ticket moved onto a step. `reject_rerun` is worth telling apart from `next`: it is the
#: one case where the graph jumps BACKWARD, and a reader who cannot see that reads a repeated step
#: as a retry rather than as rework a human asked for. `db.models.EnteredBy` carries the same
#: five words onto the run, plus `retry`.
EnterReason = Literal["next", "reject_rerun", "admission", "restart", "redo"]


async def _enter_step(
    session: AsyncSession,
    ticket: Ticket,
    config: ProcessConfig,
    *,
    step: str,
    from_step: str | None,
    reason: EnterReason,
) -> None:
    """Append `STEP_ENTERED` for a `ticket.current_step` assignment.

    Every site that assigns `current_step` calls this in the same unit of work, so the row that
    moves the ticket page's graph and the row that moves its timeline commit — or roll back —
    together. A promotion the act phase takes back therefore leaves no orphan entry behind.

    Takes `session`/`ticket` rather than a `_Tick` because `admission._admit` makes the first
    assignment of all, and it runs before any tick exists.
    """
    if step == config.terminal.step:
        # The terminal step has no `Step` in the config to read a kind off, and never gets a
        # `step_run` to number — see `STEP_ENTERED`'s null-attempt note.
        kind: str = "terminal"
        attempt: int | None = None
    else:
        # `Step.kind` is `process.yaml`'s own discriminator, so the event's vocabulary and the
        # config's cannot drift apart.
        kind = config.step(step).kind
        attempt = await _next_attempt(session, ticket.id, step)
    await append(
        session,
        type=EventType.STEP_ENTERED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step=step,
        from_step=from_step,
        reason=reason,
        attempt=attempt,
        kind=kind,
    )


async def _agent_id(session: AsyncSession, project_id: int, name: str) -> int | None:
    """The registry row for `agent:`; `NULL` is legal (data-model.md) and not worth failing on."""
    return (
        await session.execute(
            select(Agent.id).where(Agent.project_id == project_id, Agent.name == name)
        )
    ).scalar_one_or_none()


def _retry_policy(config: ProcessConfig, step: AgentStep | ActionStep | CheckStep) -> Retry | None:
    return step.retry or config.defaults.retry


def _retry_due(run: StepRun, retry: Retry, now: datetime) -> bool:
    if run.ended_at is None:
        return True
    return now >= run.ended_at + retry.backoff


async def _retry_ready(tick: _Tick, step: AgentStep | ActionStep | CheckStep, run: StepRun) -> bool:
    """`run`'s retry may start: `step` has a retry policy, and either its backoff has run or
    someone asked from the dashboard for it to start now (`RETRY_REQUESTED`, against this run
    only)."""
    retry = _retry_policy(tick.config, step)
    if retry is None:
        return False
    if _retry_due(run, retry, tick.now):
        return True
    asked = await tick.session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            Event.type == EventType.RETRY_REQUESTED,
        )
        .limit(1)
    )
    return asked is not None


def _timeout(config: ProcessConfig, step: AgentStep | CheckStep) -> timedelta:
    return step.timeout or config.defaults.timeout or _DEFAULT_TIMEOUT


def _apply_outcome(
    tick: _Tick, outcome: Outcome | None, *, extra: Mapping[str, str] | None = None
) -> None:
    """Record an outcome block's intent (design.md §4). Nothing is written until `_project`."""
    if outcome is None:
        return
    values = dict(tick.values)
    if extra:
        values.update(extra)
    if outcome.ticket_label is not None:
        tick.plan.label = expand(outcome.ticket_label, values)
    if outcome.assign is not None:
        # `assign` REPLACES the assignees so the ticket sits with whoever must act next.
        tick.plan.assign = expand_principals(outcome.assign, tick.config.roles, values)
    if outcome.comment is not None:
        tick.plan.comments.append(expand(outcome.comment, values))


async def _move_to(tick: _Tick, step: str, *, from_step: str, reason: EnterReason) -> None:
    """Put the ticket on `step`, with its `STEP_ENTERED`: the one place a tick assigns
    `current_step`."""
    await _enter_step(
        tick.session, tick.ticket, tick.config, step=step, from_step=from_step, reason=reason
    )
    tick.ticket.current_step = step
    tick.ticket.status = TicketStatus.RUNNING
    note(step=step, step_run_id=None, attempt=None)


async def note_next_step_waiting(tick: _Tick, step_name: str, unmet: Unmet) -> None:
    """Say once per entry of `step_name` that it waits on `unmet`, when the step it came from
    was forced: the person who pushed past its checks would otherwise see the ticket stall in
    silence (decision 78)."""
    entered = (
        await tick.session.execute(
            select(Event.id, Event.details)
            .where(
                Event.ticket_id == tick.ticket.id,
                Event.type == EventType.STEP_ENTERED,
                Event.details["step"].astext == step_name,
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).first()
    if entered is None:
        return
    entered_id, details = entered
    if tick.deps.next_step_waiting_seen.get(tick.ticket.id) == entered_id:
        return
    tick.deps.next_step_waiting_seen[tick.ticket.id] = entered_id
    from_step = details.get("from_step")
    if details.get("reason") != "next" or not isinstance(from_step, str):
        return
    latest = await latest_current_run(tick.session, tick.ticket, from_step)
    if latest is None or not latest.forced_by:
        return
    # After a worker restart empties the memo, the log keeps it once per entry.
    noted = await tick.session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == tick.ticket.id,
            Event.type == EventType.NEXT_STEP_WAITING,
            Event.details["step"].astext == step_name,
            Event.id > entered_id,
        )
        .limit(1)
    )
    if noted is not None:
        return
    await tick.emit(
        EventType.NEXT_STEP_WAITING,
        step=step_name,
        attempt=details.get("attempt"),
        from_step=from_step,
        forced_by=latest.forced_by,
        unmet=list(unmet.rules),
        needs=list(unmet.words),
    )


async def _advance(
    tick: _Tick,
    step: AgentStep | ActionStep | CheckStep | GateStep,
    run: StepRun,
    outcome: Outcome | None,
    *,
    extra: Mapping[str, str],
) -> None:
    """Complete `run` and move the ticket on to `step.next`."""
    await tick.emit(EventType.STEP_COMPLETED, run=run, step=step.name, attempt=run.attempt)
    _apply_outcome(tick, outcome, extra=extra)
    await _move_to(tick, step.next, from_step=step.name, reason="next")
