"""Read models over the log and the state tables (design.md §5).

Dashboard, audit trail and cost-per-issue are queries, never separately maintained state
(design.md §2.7) — nothing here writes.

Every query is shaped to the indexes that already exist: `event_ticket_idx (ticket_id, id)`,
`event_project_idx (project_id, id)`, `ticket_project_status_idx (project_id, status)`, and the
partial `event_closure_idx (project_id, created_at)` over `ticket_done` / `ticket_exited`. A new
query that needs a new index needs a migration first.

Rows come back as frozen dataclasses: the API layer owns its own Pydantic response models, and a
tuple would leak column order into every caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import (
    ColumnElement,
    Float,
    Numeric,
    Select,
    Subquery,
    and_,
    bindparam,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    CostSource,
    EnteredBy,
    Event,
    ProcessVersion,
    Project,
    Runtime,
    RuntimeType,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
    current_runs,
)
from orchestrator.events.types import EventType
from runtime import ParkReason

__all__ = [
    "NEXT_ACTION",
    "OUTCOMES",
    "P90_MIN_RUNS",
    "BlockedOn",
    "BoardCell",
    "ClosedTicket",
    "DeliveryMetrics",
    "DoneCost",
    "EventRow",
    "InFlightRun",
    "LargestRun",
    "LongestGate",
    "MostAttempts",
    "OpenGate",
    "Outcome",
    "OutcomeSpend",
    "OverviewTicket",
    "PeriodFigures",
    "ProjectSpend",
    "RunCounts",
    "RunExtremes",
    "RunHistoryRow",
    "SlowestDone",
    "SpendBucket",
    "SpendFold",
    "StepCost",
    "StepTime",
    "TicketCost",
    "TicketRow",
    "TicketSpend",
    "TicketSpends",
    "TicketState",
    "TimeSplit",
    "TimelineEntry",
    "UnmergedSpend",
    "WeekCount",
    "WeekSummary",
    "WindowSpend",
    "blocked_clause",
    "blocked_on",
    "cost_per_ticket",
    "delivery_metrics",
    "done_ticket_cost",
    "factory_overview",
    "first_admission",
    "in_flight_runs",
    "is_one_of",
    "latest_run",
    "latest_run_event",
    "open_gates",
    "open_ticket_count",
    "period_figures",
    "pipeline_board",
    "project_spend",
    "project_tickets",
    "recent_events",
    "run_extremes",
    "select_tickets",
    "spend_by_outcome",
    "spend_by_ticket",
    "step_medians",
    "step_run_history",
    "ticket_park_reason",
    "ticket_row",
    "ticket_step_costs",
    "ticket_timeline",
    "time_split",
    "unmerged_spend",
    "week_summaries",
]

#: `step_run` states that mean "the orchestrator or a human still owes this ticket something":
#: an agent container is up (working, or idle at its prompt), or a gate is waiting on a review.
IN_FLIGHT_STATUSES: tuple[str, ...] = (
    StepRunStatus.RUNNING,
    StepRunStatus.IDLE,
    StepRunStatus.WAITING,
)


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One line of the per-ticket audit trail. `id` is the log's total order and the only stable
    identity a client has for an entry."""

    id: int
    type: str
    details: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BoardCell:
    """One `(step, status)` bucket of the pipeline board, with how many tickets sit in it."""

    current_step: str
    status: str
    count: int


@dataclass(frozen=True, slots=True)
class TicketCost:
    """Spend for one ticket. Usage belongs to the run, so this sums `step_run` (data-model.md)."""

    ticket_id: int
    external_key: str
    cost_usd: Decimal
    tokens: int


@dataclass(frozen=True, slots=True)
class InFlightRun:
    """A step run that has not reached a terminal state, with the ticket it belongs to."""

    step_run_id: int
    ticket_id: int
    external_key: str
    step_name: str
    kind: str
    attempt: int
    status: str
    runtime_run_id: str | None
    started_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TicketRow:
    """One line of the project's ticket table.

    `current_step` + `status` is the pipeline position (data-model.md); the issue's labels are a
    projection of it. `attempts` counts `step_run` rows across all steps — the cheapest signal
    that something is retrying.
    """

    id: int
    project_id: int
    external_key: str
    #: The issue title as the last tick observed it, or None for a ticket never observed since
    #: revision 0005 — the table falls back to the number rather than printing a blank.
    title: str | None
    current_step: str
    status: str
    #: Status of the highest attempt at `current_step`, or None when it has none — see
    #: `_latest_run_status`. `status` alone cannot tell an interrupt from an escalation.
    latest_run_status: str | None
    #: Why that run is parked, while it is — see `_park_reason`.
    park_reason: str | None
    owner_at_admission: str
    process_version_id: int
    #: Which lane this ticket runs, from the snapshot it pinned. The table badges it: with
    #: several processes per project, a step name no longer says which process it belongs to.
    process_key: str
    attempts: int
    #: Usage summed over the ticket's attempts — the per-attempt figures `step_run` carries, added
    #: up. Zero for a ticket that has not run, never None: the row draws a figure either way, and
    #: "nothing spent" is what a reader wants to see rather than a gap.
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    #: Whether any part of `cost_usd` is a list-price estimate (`StepRun.cost_source`). Carried
    #: with every sum, because one estimated attempt makes the ticket's total an estimate.
    cost_estimated: bool
    admitted_at: datetime
    closed_at: datetime | None
    #: Non-None means the loop has stopped leasing this ticket (decision 31); see
    #: `Ticket.quarantined_at`.
    quarantined_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EventRow:
    """One row of the audit feed. `id` is the cursor — see `recent_events`."""

    id: int
    type: str
    ticket_id: int | None
    step_run_id: int | None
    details: dict[str, Any]
    created_at: datetime


def _details(value: Any) -> dict[str, Any]:
    """JSONB comes back as whatever was stored; the column defaults to `{}` but is not typed."""
    return value if isinstance(value, dict) else {}


async def ticket_timeline(
    session: AsyncSession, ticket_id: int, *, limit: int | None = None
) -> list[TimelineEntry]:
    """The lifecycle of one ticket, in log order — or its newest `limit` events, still in log
    order.

    Ordered by `id`, not `created_at`: identity is the total order, and two events written in the
    same transaction share a `now()`. A limit reads newest first on `event_ticket_idx
    (ticket_id, id)`, so a long-lived ticket costs `limit` rows rather than its whole history.
    """
    stmt = select(Event.id, Event.type, Event.details, Event.created_at).where(
        Event.ticket_id == ticket_id
    )
    if limit is None:
        rows = list((await session.execute(stmt.order_by(Event.id))).all())
    else:
        newest = (await session.execute(stmt.order_by(Event.id.desc()).limit(limit))).all()
        rows = list(reversed(newest))
    return [
        TimelineEntry(id=int(r[0]), type=r[1], details=_details(r[2]), created_at=r[3])
        for r in rows
    ]


async def pipeline_board(session: AsyncSession, project_id: int) -> list[BoardCell]:
    """Tickets by `(current_step, status)`, open ones only — the dashboard's board.

    `closed_at IS NULL` rather than `status <> 'done'`: `closed_at` is the business fact that a
    ticket left the pipeline (data-model.md), and a blocked-then-closed ticket must not reappear.
    """
    count = func.count().label("count")
    stmt = (
        select(Ticket.current_step, Ticket.status, count)
        .where(Ticket.project_id == project_id, Ticket.closed_at.is_(None))
        .group_by(Ticket.current_step, Ticket.status)
        .order_by(Ticket.current_step, Ticket.status)
    )
    rows = (await session.execute(stmt)).all()
    return [BoardCell(current_step=r[0], status=r[1], count=int(r[2])) for r in rows]


def is_one_of(types: Sequence[EventType]) -> ColumnElement[bool]:
    """`event.type IN (...)` written as literals, so the planner can prove a lifecycle's partial
    index (revision 0021) answers it, as it cannot for bound parameters in a generic plan."""
    return Event.type.in_(
        bindparam(
            "event_types",
            [str(kind) for kind in types],
            expanding=True,
            literal_execute=True,
            unique=True,
        )
    )


def latest_run_event(
    ticket_id: int, step_run_id: int, types: Sequence[str], *columns: Any
) -> Select[Any]:
    """The newest event of `types` on one run. The ticket filter is for `event_ticket_idx`; the run
    is what selects."""
    return (
        select(*columns)
        .where(
            Event.ticket_id == ticket_id,
            Event.step_run_id == step_run_id,
            Event.type.in_([str(t) for t in types]),
        )
        .order_by(Event.id.desc())
        .limit(1)
    )


def _ticket_runs() -> Any:
    return (
        select(func.count())
        .select_from(StepRun)
        .where(StepRun.ticket_id == Ticket.id)
        .scalar_subquery()
    )


def latest_run(column: Any) -> Any:
    """`column` of the highest-numbered current attempt at the ticket's current step: one of its
    current generation that no redo set aside (decisions 67, 76).

    Correlated to `ticket` explicitly: auto-correlation reaches only the enclosing SELECT, and
    nested inside `_park_reason` this would otherwise scan a fresh `ticket` of its own."""
    return (
        select(column)
        .where(
            StepRun.ticket_id == Ticket.id,
            current_runs(Ticket.generation),
            StepRun.step_name == Ticket.current_step,
        )
        .order_by(StepRun.attempt.desc())
        .limit(1)
        .correlate(Ticket)
        .scalar_subquery()
    )


def _latest_run_status() -> Any:
    """The status of the highest-numbered attempt at the ticket's current step, or NULL.

    A correlated subquery, like the `attempts` and `attempt` counts beside it, so a ticket with no
    runs still appears in its own row rather than being joined away.

    It exists because `ticket.status` is coarser than what a reader has to be told. Three unrelated
    endings park a ticket as `blocked` — a run a human interrupted (`worker/agent_steps.py`), a gate
    rejected with no `onReject` to loop back to (`worker/gates.py`), and a step that exhausted its
    retries (`worker/failures.py`) — and only the run says which. Deliberately not a fifth
    `TicketStatus`: `blocked` is the state machine's own word, and `worker/reconcile.py` releases
    both kinds of parking through the one branch (decision 26). This is the same fact
    `_resume_parked` reads to tell a resume from an unblock, published for the UI.
    """
    return latest_run(StepRun.status).label("latest_run_status")


def _park_reason() -> Any:
    """Why the latest run at the current step was last parked, while it is still parked (ticket
    `blocked`, run `interrupted`): an `agent_parked`'s `reason` (`idle`, `exited`, `rollover`,
    `ceiling`), `user` for a human's `agent_interrupted`, or NULL.

    An idle park (decision 56) and a human's stop both leave the run `interrupted` and the ticket
    `blocked`, but the first is waiting on a human and the second was stopped by one.
    `worker/interrupts.py::_latest_park` reads the same event to choose the label it re-parks under.

    Behind a CASE, which Postgres evaluates lazily, so only a parked ticket pays for the scan — of
    its own events, newest first, on `event_ticket_idx` — and a park the run has since moved on
    from reads NULL.
    """
    park = (
        select(
            case(
                (Event.type == EventType.AGENT_INTERRUPTED, literal(ParkReason.USER.value)),
                else_=Event.details["reason"].astext,
            )
        )
        .where(
            Event.ticket_id == Ticket.id,
            Event.step_run_id == latest_run(StepRun.id),
            Event.type.in_([EventType.AGENT_INTERRUPTED, EventType.AGENT_PARKED]),
        )
        .order_by(Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    parked = and_(
        Ticket.status == TicketStatus.BLOCKED,
        latest_run(StepRun.status) == StepRunStatus.INTERRUPTED,
    )
    return case((parked, park)).label("park_reason")


async def ticket_park_reason(session: AsyncSession, ticket_id: int) -> str | None:
    """`_park_reason` for one ticket, for the ticket page's header."""
    return (await session.execute(select(_park_reason()).where(Ticket.id == ticket_id))).scalar()


async def ticket_row(session: AsyncSession, ticket_id: int) -> TicketRow:
    """One ticket's row of the ticket table, for a caller that holds a single ticket."""
    row = (await session.execute(_ticket_rows_stmt().where(Ticket.id == ticket_id))).one()
    return _ticket_row(row)


def _ticket_estimated() -> Any:
    """Whether any of a ticket's attempts was priced rather than measured, correlated like
    `_ticket_sum`. False, not NULL, for a ticket with no runs — nothing there is an estimate."""
    return (
        select(func.coalesce(func.bool_or(StepRun.cost_source == CostSource.ESTIMATED), False))
        .where(StepRun.ticket_id == Ticket.id)
        .scalar_subquery()
    )


def _ticket_sum(column: Any) -> Any:
    """One `step_run` column summed over a ticket's attempts, correlated to the row being built.

    Coalesced twice over: the SUM is NULL for a ticket with no runs at all, and the columns
    themselves are NULL on a gate or an action, which reports no usage. Both mean the same thing
    here — nothing was spent — so both come back as zero rather than as a gap the caller has to
    decide about a second time.
    """
    return (
        select(func.coalesce(func.sum(func.coalesce(column, 0)), 0))
        .where(StepRun.ticket_id == Ticket.id)
        .scalar_subquery()
    )


def _ticket_rows_stmt() -> Select[Any]:
    """The ticket table's columns, unfiltered and unordered. `project_tickets` and
    `select_tickets` narrow this one statement, so a ticket list and the dashboard's table cannot
    drift apart.

    `attempts` comes from a correlated subquery rather than a join + GROUP BY so that a ticket
    with no runs yet still appears, with 0. The spend figures beside it are the same shape for the
    same reason — a join would drop an admitted ticket out of its own table.
    """
    stmt: Select[Any] = select(
        Ticket.id,
        Ticket.project_id,
        Ticket.external_key,
        Ticket.title,
        Ticket.current_step,
        Ticket.status,
        _latest_run_status(),
        _park_reason(),
        Ticket.owner_at_admission,
        Ticket.process_version_id,
        ProcessVersion.process_key,
        _ticket_runs().label("attempts"),
        _ticket_sum(StepRun.input_tokens).label("input_tokens"),
        _ticket_sum(StepRun.output_tokens).label("output_tokens"),
        _ticket_sum(StepRun.cost_usd).label("cost_usd"),
        _ticket_estimated().label("cost_estimated"),
        Ticket.admitted_at,
        Ticket.closed_at,
        Ticket.quarantined_at,
        Ticket.updated_at,
    )
    # An inner join: `process_version_id` is NOT NULL with a foreign key, so a ticket without its
    # snapshot is a constraint violation rather than a row this table has to render around.
    return stmt.join(ProcessVersion, ProcessVersion.id == Ticket.process_version_id)


def _ticket_row(r: Any) -> TicketRow:
    return TicketRow(
        id=int(r[0]),
        project_id=int(r[1]),
        external_key=r[2],
        title=r[3],
        current_step=r[4],
        status=r[5],
        latest_run_status=r[6],
        park_reason=r[7],
        owner_at_admission=r[8],
        process_version_id=int(r[9]),
        process_key=r[10],
        attempts=int(r[11]),
        input_tokens=int(r[12]),
        output_tokens=int(r[13]),
        cost_usd=Decimal(r[14]),
        cost_estimated=bool(r[15]),
        admitted_at=r[16],
        closed_at=r[17],
        quarantined_at=r[18],
        updated_at=r[19],
    )


async def project_tickets(
    session: AsyncSession,
    project_id: int,
    *,
    status: str | None = None,
    open_only: bool = False,
) -> list[TicketRow]:
    """Every ticket in a project, newest first — the dashboard's ticket table.

    Ordered by `id` descending, not `admitted_at`: identity is the total order, and two tickets
    admitted in the same pass share a `now()`. Not paginated — a project's ticket count is bounded
    by its repo's issues.
    """
    stmt = _ticket_rows_stmt().where(Ticket.project_id == project_id)
    if status is not None:
        # Hits ticket_project_status_idx (project_id, status).
        stmt = stmt.where(Ticket.status == status)
    if open_only:
        # `closed_at IS NULL`, matching pipeline_board — see its docstring.
        stmt = stmt.where(Ticket.closed_at.is_(None))
    rows = (await session.execute(stmt.order_by(Ticket.id.desc()))).all()
    return [_ticket_row(r) for r in rows]


TicketState = Literal["running", "blocked", "completed", "exited", "open", "all"]

BlockedOn = Literal[
    "quarantined", "gate", "escalated", "interrupted", "waiting", "ceiling", "rollover"
]

#: What releases each kind of blocked ticket: an MCP tool name, or for a gate the human step no
#: tool may take (design.md §7.3). A rollover resumes itself on the same tick.
NEXT_ACTION: dict[BlockedOn, str | None] = {
    "quarantined": "factorial_ticket_requeue",
    "gate": "Review the pull request on GitHub.",
    "escalated": "factorial_ticket_unblock",
    "interrupted": "factorial_ticket_resume",
    "waiting": "factorial_ticket_resume",
    "ceiling": "A person presses Resume in the dashboard.",
    "rollover": None,
}

_PARKED_ON: dict[str, BlockedOn] = {
    ParkReason.USER: "interrupted",
    ParkReason.EXITED: "interrupted",
    ParkReason.IDLE: "waiting",
    ParkReason.CEILING: "ceiling",
    ParkReason.ROLLOVER: "rollover",
}


def blocked_clause() -> ColumnElement[bool]:
    """The one definition of a blocked ticket: open, and either waiting on a person or parked.

    `waiting_gate` counts, because a gate is a human's move. Quarantine counts whatever the status
    says, because the loop has stopped leasing the ticket (decision 31). `select_tickets` and the
    projects list's counts both read this, so a list and its count cannot disagree.
    """
    return and_(
        Ticket.closed_at.is_(None),
        or_(
            Ticket.status.in_((TicketStatus.BLOCKED, TicketStatus.WAITING_GATE)),
            Ticket.quarantined_at.is_not(None),
        ),
    )


def _state_clause(state: TicketState, closed_since: datetime) -> ColumnElement[bool]:
    match state:
        case "running":
            return and_(Ticket.status == TicketStatus.RUNNING, Ticket.quarantined_at.is_(None))
        case "blocked":
            return blocked_clause()
        case "completed":
            return and_(Ticket.status == TicketStatus.DONE, Ticket.closed_at >= closed_since)
        case "exited":
            return and_(Ticket.status == TicketStatus.EXITED, Ticket.closed_at >= closed_since)
        case "open":
            return Ticket.closed_at.is_(None)
        case "all":
            return or_(Ticket.closed_at.is_(None), Ticket.closed_at >= closed_since)


def _in_organization(organization_id: int) -> ColumnElement[bool]:
    """The ticket belongs to one of the organization's projects."""
    return Ticket.project_id.in_(
        select(Project.id).where(Project.organization_id == organization_id)
    )


async def select_tickets(
    session: AsyncSession,
    *,
    organization_id: int,
    project_ids: Sequence[int] | None,
    state: TicketState,
    closed_since: datetime,
    limit: int,
) -> tuple[list[TicketRow], int]:
    """Tickets in one state, newest first, across `project_ids` (every project of the organization
    when None), plus how many matched before `limit` — so a caller can say the list was cut.

    `closed_since` bounds the states that include closed tickets (`completed`, `exited`, `all`);
    the open ones ignore it.
    """
    where = and_(_state_clause(state, closed_since), _in_organization(organization_id))
    if project_ids is not None:
        where = and_(where, Ticket.project_id.in_(project_ids))
    total = (
        await session.execute(select(func.count()).select_from(Ticket).where(where))
    ).scalar_one()
    stmt = _ticket_rows_stmt().where(where).order_by(Ticket.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [_ticket_row(r) for r in rows], int(total)


def blocked_on(row: TicketRow) -> BlockedOn | None:
    """What a ticket `blocked_clause` admits is waiting for; None for any other ticket.

    Quarantine comes first: until a requeue, the loop acts on no other release.
    """
    if row.closed_at is not None:
        return None
    if row.quarantined_at is not None:
        return "quarantined"
    if row.status == TicketStatus.WAITING_GATE:
        return "gate"
    if row.status != TicketStatus.BLOCKED:
        return None
    if row.latest_run_status != StepRunStatus.INTERRUPTED:
        return "escalated"
    # A run left interrupted with no park event to name a reason still wants a resume:
    # `request_unblock` refuses any interrupted run.
    return _PARKED_ON.get(row.park_reason or "", "interrupted")


async def cost_per_ticket(
    session: AsyncSession, project_id: int, *, since: datetime | None = None
) -> list[TicketCost]:
    """Cost per issue for one project. With `since`, over the runs that ended since then, as
    `delivery_metrics` counts its window's spend; a ticket with none is left out.

    Grouped by ticket id as well as `external_key` so two sources that reuse a key cannot merge.
    """
    cost = func.coalesce(func.sum(StepRun.cost_usd), 0).label("cost")
    tokens = func.coalesce(func.sum(_run_tokens()), 0).label("tokens")
    stmt: Select[Any] = (
        select(Ticket.id, Ticket.external_key, cost, tokens)
        .join(StepRun, StepRun.ticket_id == Ticket.id)
        .where(Ticket.project_id == project_id)
        .group_by(Ticket.id, Ticket.external_key)
        .order_by(Ticket.id)
    )
    if since is not None:
        stmt = stmt.where(StepRun.ended_at >= since)
    rows = (await session.execute(stmt)).all()
    return [
        TicketCost(
            ticket_id=int(r[0]),
            external_key=r[1],
            cost_usd=Decimal(r[2]),
            tokens=int(r[3]),
        )
        for r in rows
    ]


#: A run on one of these is billed to a subscription: its `cost_usd` is an API-rate equivalent
#: (`CostSource`), which the dashboard says beside the figure.
_SUBSCRIPTION_TYPES: Final = (RuntimeType.CLAUDE_SUBSCRIPTION, RuntimeType.CODEX_SUBSCRIPTION)


@dataclass(frozen=True, slots=True)
class ProjectSpend:
    """What a whole project has used, summed over every attempt of every ticket.

    Tokens stay split into input and output rather than added together the way `TicketCost` adds
    them: the two halves are priced differently, and a single total hides which of them a project
    is heavy in.
    """

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool
    #: The cache-read part of `input_tokens`, where a substrate reported one.
    cached_input_tokens: int
    #: Every `step_run` row, gates included; `priced_runs` have a `cost_usd`, and
    #: `estimated_runs` are the priced ones at list price.
    runs: int
    priced_runs: int
    estimated_runs: int
    #: Runs on a registry runtime of a subscription type (`_SUBSCRIPTION_TYPES`).
    subscription_runs: int


async def project_spend(session: AsyncSession, project_id: int) -> ProjectSpend:
    """The project's lifetime spend, in one row.

    Summed from `step_run` rather than added up from the ticket rows the table already draws,
    because that table is filtered by the status chips: a summary computed from it would move
    whenever the reader changed the filter, and describe the selection instead of the project.
    """
    stmt: Select[Any] = (
        select(
            func.coalesce(func.sum(StepRun.input_tokens), 0),
            func.coalesce(func.sum(StepRun.output_tokens), 0),
            func.coalesce(func.sum(StepRun.cost_usd), 0),
            func.coalesce(func.bool_or(StepRun.cost_source == CostSource.ESTIMATED), False),
            func.coalesce(func.sum(StepRun.cached_input_tokens), 0),
            func.count(StepRun.id),
            func.count(StepRun.cost_usd),
            func.count(StepRun.id).filter(StepRun.cost_source == CostSource.ESTIMATED),
            func.count(StepRun.id).filter(Runtime.type.in_(_SUBSCRIPTION_TYPES)),
        )
        .select_from(StepRun)
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .outerjoin(Runtime, Runtime.id == StepRun.runtime_id)
        .where(Ticket.project_id == project_id)
    )
    row = (await session.execute(stmt)).one()
    return ProjectSpend(
        input_tokens=int(row[0]),
        output_tokens=int(row[1]),
        cost_usd=Decimal(row[2]),
        cost_estimated=bool(row[3]),
        cached_input_tokens=int(row[4]),
        runs=int(row[5]),
        priced_runs=int(row[6]),
        estimated_runs=int(row[7]),
        subscription_runs=int(row[8]),
    )


@dataclass(frozen=True, slots=True)
class RunHistoryRow:
    """One step run with its ticket, for a list across tickets (`step_run_history`)."""

    step_run_id: int
    ticket_id: int
    external_key: str
    step_name: str
    kind: str
    attempt: int
    status: str
    model: str | None
    started_at: datetime | None
    ended_at: datetime | None
    #: None until the run has both ends.
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    cost_source: str | None
    #: Who forced a SUCCEEDED run past its failed conditions (decision 78), else None.
    forced_by: str | None
    #: When a redo set the run aside (decision 76): history, not where the ticket stands.
    superseded_at: datetime | None = None


async def step_run_history(
    session: AsyncSession,
    *,
    organization_id: int,
    project_id: int | None,
    ticket_id: int | None,
    status: str | None,
    since: datetime | None,
    limit: int,
    after_id: int | None,
) -> list[RunHistoryRow]:
    """Newest first, by id: `after_id` is the last id of the previous page, so a run created
    while a client pages never repeats or shifts a page. `since` bounds `started_at`. A forced
    run is not `succeeded`."""
    duration = func.extract("epoch", StepRun.ended_at - StepRun.started_at)
    stmt: Select[Any] = (
        select(
            StepRun.id,
            StepRun.ticket_id,
            Ticket.external_key,
            StepRun.step_name,
            StepRun.kind,
            StepRun.attempt,
            StepRun.status,
            StepRun.model,
            StepRun.started_at,
            StepRun.ended_at,
            duration,
            StepRun.input_tokens,
            StepRun.output_tokens,
            StepRun.cost_usd,
            StepRun.cost_source,
            StepRun.forced_by,
            StepRun.superseded_at,
        )
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .order_by(StepRun.id.desc())
        .limit(limit)
    )
    stmt = stmt.where(_in_organization(organization_id))
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    if ticket_id is not None:
        stmt = stmt.where(StepRun.ticket_id == ticket_id)
    if status is not None:
        stmt = stmt.where(StepRun.status == status)
    if status == StepRunStatus.SUCCEEDED:
        stmt = stmt.where(StepRun.forced_by.is_(None))
    if since is not None:
        stmt = stmt.where(StepRun.started_at >= since)
    if after_id is not None:
        stmt = stmt.where(StepRun.id < after_id)
    rows = (await session.execute(stmt)).all()
    return [
        RunHistoryRow(
            step_run_id=int(r[0]),
            ticket_id=int(r[1]),
            external_key=r[2],
            step_name=r[3],
            kind=r[4],
            attempt=int(r[5]),
            status=r[6],
            model=r[7],
            started_at=r[8],
            ended_at=r[9],
            duration_seconds=None if r[10] is None else float(r[10]),
            input_tokens=r[11],
            output_tokens=r[12],
            cost_usd=r[13],
            cost_source=r[14],
            forced_by=r[15],
            superseded_at=r[16],
        )
        for r in rows
    ]


async def in_flight_runs(session: AsyncSession, project_id: int) -> list[InFlightRun]:
    """Agent runs still executing and gates still waiting — the "in-flight agents" panel."""
    stmt = (
        select(
            StepRun.id,
            StepRun.ticket_id,
            Ticket.external_key,
            StepRun.step_name,
            StepRun.kind,
            StepRun.attempt,
            StepRun.status,
            StepRun.runtime_run_id,
            StepRun.started_at,
            StepRun.created_at,
        )
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .where(Ticket.project_id == project_id, StepRun.status.in_(IN_FLIGHT_STATUSES))
        .order_by(StepRun.created_at, StepRun.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        InFlightRun(
            step_run_id=int(r[0]),
            ticket_id=int(r[1]),
            external_key=r[2],
            step_name=r[3],
            kind=r[4],
            attempt=int(r[5]),
            status=r[6],
            runtime_run_id=r[7],
            started_at=r[8],
            created_at=r[9],
        )
        for r in rows
    ]


async def recent_events(
    session: AsyncSession,
    project_id: int,
    limit: int = 100,
    after_id: int | None = None,
    ticket_id: int | None = None,
) -> list[EventRow]:
    """The audit feed, keyset-paginated on `id` and returned oldest-first.

    Pagination is on `id`, never `created_at`: a timestamp is not unique, so a poller resuming
    from one silently drops events written in the same tick.

    `ticket_id` narrows the feed to one ticket's events (hits `event_ticket_idx (ticket_id, id)`).
    """
    stmt = select(
        Event.id,
        Event.type,
        Event.ticket_id,
        Event.step_run_id,
        Event.details,
        Event.created_at,
    ).where(Event.project_id == project_id)
    if after_id is not None:
        stmt = stmt.where(Event.id > after_id)
    if ticket_id is not None:
        stmt = stmt.where(Event.ticket_id == ticket_id)
    stmt = stmt.order_by(Event.id).limit(limit)

    rows = (await session.execute(stmt)).all()
    return [
        EventRow(
            id=int(r[0]),
            type=r[1],
            ticket_id=r[2],
            step_run_id=r[3],
            details=_details(r[4]),
            created_at=r[5],
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class OverviewTicket:
    """One ticket as the factory board draws it: its lane, and the three figures it shades by."""

    ticket_id: int
    project_id: int
    external_key: str
    #: Observed issue title, or None for a ticket that has not ticked since revision 0005.
    title: str | None
    current_step: str
    status: str
    #: Which ADLC lane this ticket runs, from the snapshot it pinned. The board badges it and
    #: deliberately does not band by it — with several processes per project, step names are no
    #: longer stable even within one project, which is why `factory-board/model.ts` bands on
    #: `TicketStatus` instead.
    process_key: str
    #: Status of the highest attempt at `current_step`, or None — see `_latest_run_status`. It is
    #: what puts an interrupted ticket in the Gate lane rather than beside the escalations.
    latest_run_status: str | None
    #: See `TicketRow.park_reason`.
    park_reason: str | None
    #: `step_run` rows across every step — "has this ticket run at all", not a retry count.
    runs: int
    #: Attempt number at the step it is on now, counted from the ticket's last restart, or None
    #: before its first run there. `attempt > 1` is Retrying.
    attempt: int | None
    cost_usd: Decimal
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool
    tokens: int
    minutes: float
    #: See `TicketRow.quarantined_at`.
    quarantined_at: datetime | None


async def factory_overview(
    session: AsyncSession, done_days: int, organization_id: int
) -> list[OverviewTicket]:
    """Every ticket on one organization's factory board, across its projects.

    One query rather than three per project: the board is a read model like the cost report beside
    it, and a client-side join of costs onto tickets would be the same work done worse.

    Closed tickets stay for `done_days`, because Done is the biggest lane on a healthy board and an
    empty one would misreport the factory — but all of history would swamp it.

    Correlated subqueries rather than a join and GROUP BY, for the same reason `project_tickets`
    uses one: an admitted ticket has no `step_run` rows, and a join would drop it out of its own
    lane.
    """
    cutoff = datetime.now(UTC) - timedelta(days=done_days)
    # The attempts at the current step since the last restart or redo: attempt numbers keep
    # counting across both.
    attempt = (
        select(func.nullif(func.count(StepRun.id), 0))
        .where(
            StepRun.ticket_id == Ticket.id,
            StepRun.step_name == Ticket.current_step,
            current_runs(Ticket.generation),
        )
        .scalar_subquery()
        .label("attempt")
    )
    seconds = (
        select(
            func.coalesce(func.sum(func.extract("epoch", StepRun.ended_at - StepRun.started_at)), 0)
        )
        .where(
            StepRun.ticket_id == Ticket.id,
            StepRun.started_at.is_not(None),
            StepRun.ended_at.is_not(None),
        )
        .scalar_subquery()
        .label("seconds")
    )
    stmt: Select[Any] = (
        select(
            Ticket.id,
            Ticket.project_id,
            Ticket.external_key,
            Ticket.title,
            Ticket.current_step,
            Ticket.status,
            ProcessVersion.process_key,
            _latest_run_status(),
            _park_reason(),
            _ticket_runs().label("runs"),
            attempt,
            _ticket_sum(StepRun.cost_usd).label("cost"),
            _ticket_estimated().label("cost_estimated"),
            _ticket_sum(_run_tokens()).label("tokens"),
            seconds,
            Ticket.quarantined_at,
        )
        .join(ProcessVersion, ProcessVersion.id == Ticket.process_version_id)
        .join(Project, Project.id == Ticket.project_id)
        .where(
            Project.organization_id == organization_id,
            or_(Ticket.closed_at.is_(None), Ticket.closed_at >= cutoff),
        )
        .order_by(Ticket.project_id, Ticket.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        OverviewTicket(
            ticket_id=int(r[0]),
            project_id=int(r[1]),
            external_key=r[2],
            title=r[3],
            current_step=r[4],
            status=r[5],
            process_key=r[6],
            latest_run_status=r[7],
            park_reason=r[8],
            runs=int(r[9]),
            attempt=None if r[10] is None else int(r[10]),
            cost_usd=Decimal(r[11]),
            cost_estimated=bool(r[12]),
            tokens=int(r[13]),
            minutes=round(float(r[14]) / 60, 2),
            quarantined_at=r[15],
        )
        for r in rows
    ]


#: Counted from these events, never from `closed_at`: re-entry clears `closed_at`
#: (`worker/reconcile.py`), and a ticket that left in a week and came back still left that week.
_CLOSURES: Final = (EventType.TICKET_DONE, EventType.TICKET_EXITED)
#: A cancelled run was taken down by an exit and an interrupted one is still parked; neither says
#: how long the step takes.
_RUN_ENDINGS: Final = (StepRunStatus.SUCCEEDED, StepRunStatus.FAILED, StepRunStatus.TIMEOUT)
_GATE_DECISIONS: Final = (StepRunStatus.APPROVED, StepRunStatus.REJECTED)
#: A gate wait an exit cut short was still a wait on a person, so it counts with the decided ones.
_GATE_ENDS: Final = (*_GATE_DECISIONS, StepRunStatus.CANCELLED)
#: Below this many runs a 90th percentile is the slowest run under another name, so a step or a
#: gate with fewer has none (`StepTime.p90_seconds`). Served to the page as `limits.p90_min_runs`.
P90_MIN_RUNS: Final = 7

Outcome = Literal["done", "open", "exited"]
#: `done` and `exited` are `ticket.status`; `open` is every other status — the complement, not a
#: stored outcome. Fixed order, so a chart draws the same three segments for every project.
OUTCOMES: Final[tuple[Outcome, ...]] = ("done", "open", "exited")
_CLOSURE_OUTCOME: Final[dict[str, Literal["done", "exited"]]] = {
    EventType.TICKET_DONE: "done",
    EventType.TICKET_EXITED: "exited",
}


def _run_at() -> Any:
    """Where a run sits in time: `ended_at`, else `started_at` while it runs, else `created_at`
    for a spawn still coming up."""
    return func.coalesce(StepRun.ended_at, StepRun.started_at, StepRun.created_at)


def _in_span(since: datetime | None) -> ColumnElement[bool]:
    """The span rule (tech-stack decision 61): a run belongs to the span when
    `coalesce(ended_at, started_at, created_at) >= since`; None is the project's lifetime."""
    return true() if since is None else _run_at() >= since


def _run_tokens() -> Any:
    """Input plus output; NULL on a gate, an action or a check reads as zero. Added as NUMERIC: a
    report the worker clamped holds BIGINT's maximum in both columns, whose BIGINT sum overflows."""
    return cast(func.coalesce(StepRun.input_tokens, 0), Numeric) + func.coalesce(
        StepRun.output_tokens, 0
    )


def _run_seconds() -> Any:
    """Active seconds: `ended_at - started_at` less `parked_seconds`, the time a person held the
    run paused (tech-stack decision 61). NULL while the run is going or never started. A run
    parked now ends at its park, so no open pause is left to subtract; a gate never parks."""
    return (
        cast(func.extract("epoch", StepRun.ended_at - StepRun.started_at), Float)
        - StepRun.parked_seconds
    )


def _any_estimated() -> Any:
    """The aggregate behind every `cost_estimated`: any run in the group priced at list."""
    return func.coalesce(func.bool_or(StepRun.cost_source == CostSource.ESTIMATED), False)


def _gate_wait() -> ColumnElement[bool]:
    """A gate that opened and ended (`_GATE_ENDS`), so its wait has both ends."""
    return and_(
        StepRun.kind == StepKind.HUMAN_GATE,
        StepRun.status.in_(_GATE_ENDS),
        StepRun.started_at.is_not(None),
        StepRun.ended_at.is_not(None),
    )


def _span_runs(project_id: int, since: datetime | None, *columns: Any) -> Select[Any]:
    """`columns` over the project's runs in the span (`_in_span`)."""
    return (
        select(*columns)
        .select_from(StepRun)
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .where(Ticket.project_id == project_id, _in_span(since))
    )


@dataclass(frozen=True, slots=True)
class ClosedTicket:
    """One ticket's closure in a week; `outcome` is `done` or `exited`."""

    ticket_id: int
    external_key: str
    outcome: Literal["done", "exited"]


@dataclass(frozen=True, slots=True)
class WeekCount:
    """Distinct tickets done, and exited, in one UTC week starting Monday."""

    week_start: date
    done: int
    exited: int


@dataclass(frozen=True, slots=True)
class WeekSummary:
    """One UTC week starting Monday: the tickets that closed in it and what the runs in it cost.
    `done` and `exited` count `closed` by outcome; a ticket that exited twice in a week is one."""

    week_start: date
    done: int
    exited: int
    #: Spend of the runs the span rule places in this week (`_run_at`), whatever week their
    #: ticket closed in.
    cost_usd: Decimal
    cost_estimated: bool
    #: `done` first, then `exited`, each by key.
    closed: list[ClosedTicket]


@dataclass(frozen=True, slots=True)
class StepTime:
    """The active time (`_run_seconds`) of one step of one lane: run time for an agent, check or
    action, and for a gate the wait from opening to decision."""

    process_key: str
    step: str
    kind: str
    runs: int
    median_seconds: float
    #: None under `P90_MIN_RUNS` runs.
    p90_seconds: float | None
    max_seconds: float


@dataclass(frozen=True, slots=True)
class WindowSpend:
    """`ProjectSpend` over the runs in a span (`_in_span`) or a period (`period_figures`)."""

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class DeliveryMetrics:
    """Velocity and cost for one project over a window — see `delivery_metrics`."""

    #: Zero-filled and oldest first, so a quiet week reads as zero rather than as a gap.
    weeks: list[WeekCount]
    #: Distinct tickets across the window: a ticket that exited twice counts once.
    done: int
    exited: int
    step_times: list[StepTime]
    gate_waits: list[StepTime]
    #: Lifetime spend of the tickets done in the window, runs before `since` included.
    done_cost_usd: Decimal
    done_tokens: int
    #: None when nothing was done in the window.
    cost_per_done_usd: Decimal | None
    #: See `TicketRow.cost_estimated`; it covers `done_cost_usd`.
    cost_estimated: bool
    spend: WindowSpend


def _week_starts(since: datetime, until: datetime) -> list[date]:
    first = since.astimezone(UTC).date()
    first -= timedelta(days=first.weekday())
    last = until.astimezone(UTC).date()
    weeks: list[date] = []
    while first <= last:
        weeks.append(first)
        first += timedelta(weeks=1)
    return weeks


@dataclass(frozen=True, slots=True)
class DoneCost:
    """The lifetime cost of the tickets done in a span, runs before `since` included — a done
    ticket is priced whole. `mean_usd` and `median_usd` are over per-ticket sums, a ticket with
    no runs at zero; both None when nothing was done."""

    tickets: int
    cost_usd: Decimal
    tokens: int
    mean_usd: Decimal | None
    median_usd: Decimal | None
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool


async def done_ticket_cost(
    session: AsyncSession,
    project_id: int,
    *,
    since: datetime | None,
    until: datetime | None = None,
) -> DoneCost:
    """`DoneCost` for the tickets with a `ticket_done` in `[since, until)`; a None bound is open.

    Counted from the event, never from `ticket.status`: re-entry clears `closed_at`, and a ticket
    done in the span was done in the span. The span picks the closures, not the runs: each ticket
    is priced whole. Rides `event_closure_idx`.
    """
    done_ids = select(Event.ticket_id).where(
        Event.project_id == project_id, is_one_of((EventType.TICKET_DONE,))
    )
    if since is not None:
        done_ids = done_ids.where(Event.created_at >= since)
    if until is not None:
        done_ids = done_ids.where(Event.created_at < until)
    per_ticket = (
        select(
            func.coalesce(func.sum(StepRun.cost_usd), 0).label("cost"),
            func.coalesce(func.sum(_run_tokens()), 0).label("tokens"),
            _any_estimated().label("estimated"),
        )
        .select_from(Ticket)
        .outerjoin(StepRun, StepRun.ticket_id == Ticket.id)
        .where(Ticket.id.in_(done_ids))
        .group_by(Ticket.id)
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(per_ticket.c.cost), 0),
                func.coalesce(func.sum(per_ticket.c.tokens), 0),
                func.percentile_cont(0.5).within_group(per_ticket.c.cost),
                func.coalesce(func.bool_or(per_ticket.c.estimated), False),
            )
        )
    ).one()
    tickets = int(row[0])
    cost = Decimal(row[1])
    return DoneCost(
        tickets=tickets,
        cost_usd=cost,
        tokens=int(row[2]),
        mean_usd=(cost / tickets).quantize(Decimal("0.0001")) if tickets else None,
        median_usd=None if row[3] is None else Decimal(row[3]).quantize(Decimal("0.0001")),
        cost_estimated=bool(row[4]),
    )


async def first_admission(session: AsyncSession, project_id: int) -> datetime | None:
    """The project's first `admitted_at`, where every window is clipped; None before any ticket."""
    return await session.scalar(
        select(func.min(Ticket.admitted_at)).where(Ticket.project_id == project_id)
    )


def _closures(project_id: int, since: datetime) -> Subquery:
    # `is_one_of` writes literals, so the planner can prove the partial `event_closure_idx`
    # answers this instead of scanning the project's whole log.
    return (
        select(
            func.date_trunc("week", func.timezone("UTC", Event.created_at)).label("week"),
            Event.type.label("type"),
            Event.ticket_id.label("ticket_id"),
        )
        .where(Event.project_id == project_id, is_one_of(_CLOSURES), Event.created_at >= since)
        .subquery()
    )


def _done_in(project_id: int, since: datetime | None, until: datetime | None) -> Subquery:
    """`(ticket_id, done_at)` for each ticket done in `[since, until)`, `done_at` its latest
    `ticket_done` there; a None bound is open. Rides `event_closure_idx`."""
    stmt = select(
        Event.ticket_id.label("ticket_id"), func.max(Event.created_at).label("done_at")
    ).where(Event.project_id == project_id, is_one_of((EventType.TICKET_DONE,)))
    if since is not None:
        stmt = stmt.where(Event.created_at >= since)
    if until is not None:
        stmt = stmt.where(Event.created_at < until)
    return stmt.group_by(Event.ticket_id).subquery()


def _lead_seconds(done: Subquery) -> Any:
    """Admission to `done.c.done_at` in seconds, in a query that joins `ticket`."""
    return cast(func.extract("epoch", done.c.done_at - Ticket.admitted_at), Float)


async def week_summaries(
    session: AsyncSession, project_id: int, *, since: datetime, until: datetime
) -> list[WeekSummary]:
    """`WeekSummary` for every UTC week from `since` to `until`, zero-filled and oldest first: the
    tickets whose `ticket_done` / `ticket_exited` falls in it, and the spend of the runs the span
    rule places in it."""
    closures = _closures(project_id, since)
    # One row per (week, closure, ticket): a ticket that exited twice in a week is one.
    keyed = (
        select(closures.c.week, closures.c.type, closures.c.ticket_id, Ticket.external_key)
        .join(Ticket, Ticket.id == closures.c.ticket_id)
        .distinct()
        .order_by(closures.c.week, closures.c.type, Ticket.external_key)
    )
    per_week: dict[date, list[ClosedTicket]] = {}
    for week, kind, ticket_id, key in (await session.execute(keyed)).all():
        per_week.setdefault(week.date(), []).append(
            ClosedTicket(ticket_id=int(ticket_id), external_key=key, outcome=_CLOSURE_OUTCOME[kind])
        )
    run_week = func.date_trunc("week", func.timezone("UTC", _run_at())).label("week")
    weekly: dict[date, tuple[Decimal, bool]] = {
        start.date(): (Decimal(cost), bool(estimated))
        for start, cost, estimated in (
            await session.execute(
                _span_runs(
                    project_id,
                    since,
                    run_week,
                    func.coalesce(func.sum(StepRun.cost_usd), 0),
                    _any_estimated(),
                ).group_by(run_week)
            )
        ).all()
    }
    weeks: list[WeekSummary] = []
    for start in _week_starts(since, until):
        closed = per_week.get(start, [])
        cost, estimated = weekly.get(start, (Decimal(0), False))
        weeks.append(
            WeekSummary(
                week_start=start,
                done=sum(1 for row in closed if row.outcome == "done"),
                exited=sum(1 for row in closed if row.outcome == "exited"),
                cost_usd=cost,
                cost_estimated=estimated,
                closed=closed,
            )
        )
    return weeks


async def step_medians(
    session: AsyncSession, project_id: int, *, since: datetime
) -> tuple[list[StepTime], list[StepTime]]:
    """`(step_times, gate_waits)`: the median, 90th percentile and longest active time of each
    step, and the same of each gate's wait from opening to decision, over the finished runs and
    decided gates in the span, first start first.
    """
    seconds = _run_seconds()
    timed: Select[Any] = (
        _span_runs(
            project_id,
            since,
            ProcessVersion.process_key,
            StepRun.step_name,
            StepRun.kind,
            func.count(),
            func.percentile_cont(0.5).within_group(seconds),
            func.percentile_cont(0.9).within_group(seconds),
            func.max(seconds),
        )
        .join(ProcessVersion, ProcessVersion.id == Ticket.process_version_id)
        .where(
            StepRun.started_at.is_not(None),
            or_(
                and_(StepRun.kind == StepKind.HUMAN_GATE, StepRun.status.in_(_GATE_DECISIONS)),
                and_(StepRun.kind != StepKind.HUMAN_GATE, StepRun.status.in_(_RUN_ENDINGS)),
            ),
        )
        .group_by(ProcessVersion.process_key, StepRun.step_name, StepRun.kind)
        # First start first: the lane's own order, as the tickets actually walked it.
        .order_by(func.min(StepRun.started_at), ProcessVersion.process_key, StepRun.step_name)
    )
    step_times: list[StepTime] = []
    gate_waits: list[StepTime] = []
    for process_key, step, kind, runs, median, p90, longest in (await session.execute(timed)).all():
        row = StepTime(
            process_key=process_key,
            step=step,
            kind=kind,
            runs=int(runs),
            median_seconds=float(median),
            p90_seconds=float(p90) if runs >= P90_MIN_RUNS else None,
            max_seconds=float(longest),
        )
        (gate_waits if kind == StepKind.HUMAN_GATE else step_times).append(row)
    return step_times, gate_waits


async def delivery_metrics(
    session: AsyncSession, project_id: int, *, since: datetime
) -> DeliveryMetrics:
    """Throughput, step time, gate wait and cost for one project, from `since` until now.

    A run belongs to the window when `coalesce(ended_at, started_at, created_at) >= since`
    (`_in_span`); a closure belongs when its `ticket_done` / `ticket_exited` is at or after
    `since`. One project per call: a median of per-project medians is not a median.
    """
    until = datetime.now(UTC)
    closures = _closures(project_id, since)
    distinct_tickets = func.count(func.distinct(closures.c.ticket_id))
    per_week: dict[tuple[date, str], int] = {
        (week.date(), kind): int(count)
        for week, kind, count in (
            await session.execute(
                select(closures.c.week, closures.c.type, distinct_tickets).group_by(
                    closures.c.week, closures.c.type
                )
            )
        ).all()
    }
    totals: dict[str, int] = {
        kind: int(count)
        for kind, count in (
            await session.execute(
                select(closures.c.type, distinct_tickets).group_by(closures.c.type)
            )
        ).all()
    }
    step_times, gate_waits = await step_medians(session, project_id, since=since)
    done_ids = select(Event.ticket_id).where(
        Event.project_id == project_id,
        is_one_of((EventType.TICKET_DONE,)),
        Event.created_at >= since,
    )
    done_cost = (
        await session.execute(
            select(
                func.coalesce(func.sum(StepRun.cost_usd), 0),
                func.coalesce(func.sum(_run_tokens()), 0),
                _any_estimated(),
            ).where(StepRun.ticket_id.in_(done_ids))
        )
    ).one()
    spend = (
        await session.execute(
            _span_runs(
                project_id,
                since,
                func.coalesce(func.sum(StepRun.input_tokens), 0),
                func.coalesce(func.sum(StepRun.output_tokens), 0),
                func.coalesce(func.sum(StepRun.cost_usd), 0),
                _any_estimated(),
            )
        )
    ).one()
    done = totals.get(EventType.TICKET_DONE, 0)
    done_cost_usd = Decimal(done_cost[0])
    return DeliveryMetrics(
        weeks=[
            WeekCount(
                week_start=week,
                done=per_week.get((week, EventType.TICKET_DONE), 0),
                exited=per_week.get((week, EventType.TICKET_EXITED), 0),
            )
            for week in _week_starts(since, until)
        ],
        done=done,
        exited=totals.get(EventType.TICKET_EXITED, 0),
        step_times=step_times,
        gate_waits=gate_waits,
        done_cost_usd=done_cost_usd,
        done_tokens=int(done_cost[1]),
        cost_per_done_usd=(done_cost_usd / done).quantize(Decimal("0.0001")) if done else None,
        cost_estimated=bool(done_cost[2]),
        spend=WindowSpend(
            input_tokens=int(spend[0]),
            output_tokens=int(spend[1]),
            cost_usd=Decimal(spend[2]),
            cost_estimated=bool(spend[3]),
        ),
    )


# ------------------------------------------------------------------------ project dashboard
# Every figure below is a query over `step_run ⋈ ticket` scoped by `_in_span` (tech-stack
# decision 61), or by `[since, until)` for `period_figures`; the log is read only for closures
# (`_CLOSURES`), which `event_closure_idx` answers.


@dataclass(frozen=True, slots=True)
class RunCounts:
    """How a period's runs were priced: `ProjectSpend`'s counts over the runs in it."""

    total: int
    priced: int
    estimated: int
    subscription: int
    cached_input_tokens: int


@dataclass(frozen=True, slots=True)
class PeriodFigures:
    """The cards' figures over `[since, until)`; see `period_figures`."""

    spend: WindowSpend
    runs: RunCounts
    #: Distinct tickets with that closure in the period.
    tickets_done: int
    tickets_exited: int
    tickets_admitted: int
    #: The tickets done in the period, each priced over its whole life.
    per_done: DoneCost
    #: None for a period under a day, where a weekly rate is noise.
    velocity_per_week: float | None
    lead_time_tickets: int
    lead_time_median_seconds: float | None
    #: Runs in the period a person forced past their failed conditions (decision 78). Their
    #: tickets still count as done: a gate after the step still had a human review.
    forced_steps: int


async def period_figures(
    session: AsyncSession, project_id: int, *, since: datetime, until: datetime
) -> PeriodFigures:
    """What the cards draw for `[since, until)`. A run is in the period when its `_run_at` is,
    a closure when its event is, an admission when its `admitted_at` is. Lead time runs from
    admission to the ticket's latest `ticket_done` in the period.

    Bounded on both sides, unlike the sections, so the previous period is the same call.
    """
    runs = (
        await session.execute(
            _span_runs(
                project_id,
                since,
                func.coalesce(func.sum(StepRun.input_tokens), 0),
                func.coalesce(func.sum(StepRun.output_tokens), 0),
                func.coalesce(func.sum(StepRun.cost_usd), 0),
                _any_estimated(),
                func.coalesce(func.sum(StepRun.cached_input_tokens), 0),
                func.count(StepRun.id),
                func.count(StepRun.cost_usd),
                func.count(StepRun.id).filter(StepRun.cost_source == CostSource.ESTIMATED),
                func.count(StepRun.id).filter(Runtime.type.in_(_SUBSCRIPTION_TYPES)),
                func.count(StepRun.id).filter(StepRun.forced_by.is_not(None)),
            )
            .outerjoin(Runtime, Runtime.id == StepRun.runtime_id)
            .where(_run_at() < until)
        )
    ).one()
    closed: dict[str, int] = {
        kind: int(count)
        for kind, count in (
            await session.execute(
                select(Event.type, func.count(func.distinct(Event.ticket_id)))
                .where(
                    Event.project_id == project_id,
                    is_one_of(_CLOSURES),
                    Event.created_at >= since,
                    Event.created_at < until,
                )
                .group_by(Event.type)
            )
        ).all()
    }
    admitted = await session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.project_id == project_id,
            Ticket.admitted_at >= since,
            Ticket.admitted_at < until,
        )
    )
    done = _done_in(project_id, since, until)
    lead = _lead_seconds(done)
    lead_row = (
        await session.execute(
            select(func.count(), func.percentile_cont(0.5).within_group(lead))
            .select_from(done)
            .join(Ticket, Ticket.id == done.c.ticket_id)
        )
    ).one()
    tickets_done = closed.get(EventType.TICKET_DONE, 0)
    length = until - since
    return PeriodFigures(
        spend=WindowSpend(
            input_tokens=int(runs[0]),
            output_tokens=int(runs[1]),
            cost_usd=Decimal(runs[2]),
            cost_estimated=bool(runs[3]),
        ),
        runs=RunCounts(
            total=int(runs[5]),
            priced=int(runs[6]),
            estimated=int(runs[7]),
            subscription=int(runs[8]),
            cached_input_tokens=int(runs[4]),
        ),
        tickets_done=tickets_done,
        tickets_exited=closed.get(EventType.TICKET_EXITED, 0),
        tickets_admitted=int(admitted or 0),
        per_done=await done_ticket_cost(session, project_id, since=since, until=until),
        velocity_per_week=(
            None if length < timedelta(days=1) else tickets_done / (length / timedelta(weeks=1))
        ),
        lead_time_tickets=int(lead_row[0]),
        lead_time_median_seconds=None if lead_row[1] is None else float(lead_row[1]),
        forced_steps=int(runs[9]),
    )


async def open_ticket_count(session: AsyncSession, project_id: int) -> int:
    """Tickets neither done nor exited, now: the `open` of `OUTCOMES`."""
    count = await session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.project_id == project_id,
            Ticket.status.not_in((TicketStatus.DONE, TicketStatus.EXITED)),
        )
    )
    return int(count or 0)


@dataclass(frozen=True, slots=True)
class TimeSplit:
    """Where the lead time of the tickets done in a period went, each ticket over its whole life.
    `agent_seconds` is the active time of its agent, check and action runs; `gate_seconds` its
    gate waits, cut-short ones included; `paused_seconds` what its runs sat parked for a person;
    `idle_seconds` the rest (queued, blocked, between runs), floored at zero per ticket."""

    tickets: int
    lead_seconds: float
    agent_seconds: float
    gate_seconds: float
    paused_seconds: float
    idle_seconds: float


async def time_split(
    session: AsyncSession, project_id: int, *, since: datetime, until: datetime
) -> TimeSplit:
    """`TimeSplit` over the tickets done in `[since, until)`, each timed to its latest
    `ticket_done` there, as `period_figures` times lead time."""
    done = _done_in(project_id, since, until)
    worked = StepRun.kind.in_((StepKind.AGENT, StepKind.CHECK, StepKind.ACTION))
    per_ticket = (
        select(
            _lead_seconds(done).label("lead"),
            func.coalesce(func.sum(_run_seconds()).filter(worked), 0).label("agent"),
            func.coalesce(func.sum(_run_seconds()).filter(_gate_wait()), 0).label("gate"),
            func.coalesce(func.sum(StepRun.parked_seconds), 0).label("paused"),
        )
        .select_from(done)
        .join(Ticket, Ticket.id == done.c.ticket_id)
        .outerjoin(StepRun, StepRun.ticket_id == Ticket.id)
        .group_by(done.c.ticket_id, done.c.done_at, Ticket.admitted_at)
        .subquery()
    )
    t = per_ticket.c
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(t.lead), 0),
                func.coalesce(func.sum(t.agent), 0),
                func.coalesce(func.sum(t.gate), 0),
                func.coalesce(func.sum(t.paused), 0),
                func.coalesce(func.sum(func.greatest(t.lead - t.agent - t.gate - t.paused, 0)), 0),
            )
        )
    ).one()
    return TimeSplit(
        tickets=int(row[0]),
        lead_seconds=float(row[1]),
        agent_seconds=float(row[2]),
        gate_seconds=float(row[3]),
        paused_seconds=float(row[4]),
        idle_seconds=float(row[5]),
    )


@dataclass(frozen=True, slots=True)
class OpenGate:
    """A human gate still waiting for a decision; `waiting_since` is when it opened."""

    ticket_id: int
    external_key: str
    title: str | None
    step: str
    attempt: int
    waiting_since: datetime


async def open_gates(session: AsyncSession, project_id: int, *, limit: int) -> list[OpenGate]:
    """The gates waiting now, longest first, cut at `limit`. Not windowed: a wait still open
    belongs to now."""
    stmt = (
        select(
            Ticket.id,
            Ticket.external_key,
            Ticket.title,
            StepRun.step_name,
            StepRun.attempt,
            StepRun.started_at,
        )
        .select_from(StepRun)
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .where(
            Ticket.project_id == project_id,
            StepRun.kind == StepKind.HUMAN_GATE,
            StepRun.status == StepRunStatus.WAITING,
            StepRun.started_at.is_not(None),
        )
        .order_by(StepRun.started_at, StepRun.id)
        .limit(limit)
    )
    return [
        OpenGate(
            ticket_id=int(r[0]),
            external_key=r[1],
            title=r[2],
            step=r[3],
            attempt=int(r[4]),
            waiting_since=r[5],
        )
        for r in (await session.execute(stmt)).all()
    ]


@dataclass(frozen=True, slots=True)
class OutcomeSpend:
    """Spend by what the ticket became (`OUTCOMES`). Under a span `tickets` counts the tickets of
    the outcome with a run in it; over the lifetime, every ticket of it, unrun ones included."""

    outcome: Outcome
    tickets: int
    cost_usd: Decimal
    tokens: int
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool


def _outcome() -> Any:
    return case(
        (Ticket.status == TicketStatus.DONE, literal("done")),
        (Ticket.status == TicketStatus.EXITED, literal("exited")),
        else_=literal("open"),
    )


async def spend_by_outcome(
    session: AsyncSession, project_id: int, *, since: datetime | None
) -> list[OutcomeSpend]:
    """`OutcomeSpend` for each of `OUTCOMES`, in that order, zero-filled.

    A LEFT JOIN rather than `cost_per_ticket`'s inner one: over the lifetime (`since=None`) an
    admitted ticket with no runs is an open ticket, and the inner join would drop it from its own
    count. Under a span the join carries the span rule, and a ticket counts only with a run in
    it, so the count and the spend beside it describe the same tickets.
    """
    outcome = _outcome().label("outcome")
    # The outer join leaves a ticket with no run in the span a NULL `StepRun.ticket_id`.
    counted = Ticket.id if since is None else StepRun.ticket_id
    stmt: Select[Any] = (
        select(
            outcome,
            func.count(func.distinct(counted)),
            func.coalesce(func.sum(StepRun.cost_usd), 0),
            func.coalesce(func.sum(_run_tokens()), 0),
            _any_estimated(),
        )
        .select_from(Ticket)
        .outerjoin(StepRun, and_(StepRun.ticket_id == Ticket.id, _in_span(since)))
        .where(Ticket.project_id == project_id)
        .group_by(outcome)
    )
    found: dict[str, Any] = {r[0]: r for r in (await session.execute(stmt)).all()}
    rows = [found.get(name, (name, 0, 0, 0, False)) for name in OUTCOMES]
    return [
        OutcomeSpend(
            outcome=r[0],
            tickets=int(r[1]),
            cost_usd=Decimal(r[2]),
            tokens=int(r[3]),
            cost_estimated=bool(r[4]),
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class TicketSpend:
    """One ticket's spend over the span — a bar of "cost by ticket". `attempts` counts its runs
    in the span, gates included."""

    ticket_id: int
    external_key: str
    title: str | None
    status: str
    attempts: int
    tokens: int
    cost_usd: Decimal
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class SpendFold:
    """The tickets a top-N list did not draw, summed into one row."""

    tickets: int
    cost_usd: Decimal
    #: See `TicketRow.cost_estimated`.
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class TicketSpends:
    """Cost by ticket, cut in SQL — see `spend_by_ticket`."""

    top: list[TicketSpend]
    #: None when `top` holds every ticket that ran in the span.
    rest: SpendFold | None
    #: Tickets with no `step_run` at all, whatever the span.
    unrun: int


async def spend_by_ticket(
    session: AsyncSession, project_id: int, *, since: datetime | None, limit: int
) -> TicketSpends:
    """The `limit` tickets with runs in the span, costliest first and newest among equals; the
    others with runs in the span, summed; and the count of tickets that never ran.

    A run belongs when `coalesce(ended_at, started_at, created_at) >= since`, and None is the
    lifetime. A ticket whose runs all predate `since` is in none of the three: it ran, just not
    in the span.
    """
    cost = func.coalesce(func.sum(StepRun.cost_usd), 0).label("cost")
    ranked = (
        _span_runs(
            project_id,
            since,
            Ticket.id,
            Ticket.external_key,
            Ticket.title,
            Ticket.status,
            func.count(StepRun.id),
            func.coalesce(func.sum(_run_tokens()), 0),
            cost,
            _any_estimated().label("estimated"),
        )
        .group_by(Ticket.id)
        .order_by(cost.desc(), Ticket.id.desc())
    )
    top = [
        TicketSpend(
            ticket_id=int(r[0]),
            external_key=r[1],
            title=r[2],
            status=r[3],
            attempts=int(r[4]),
            tokens=int(r[5]),
            cost_usd=Decimal(r[6]),
            cost_estimated=bool(r[7]),
        )
        for r in (await session.execute(ranked.limit(limit))).all()
    ]
    rest = ranked.offset(limit).subquery()
    folded = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(rest.c.cost), 0),
                func.coalesce(func.bool_or(rest.c.estimated), False),
            )
        )
    ).one()
    unrun = await session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.project_id == project_id,
            ~exists().where(StepRun.ticket_id == Ticket.id),
        )
    )
    return TicketSpends(
        top=top,
        rest=(
            SpendFold(
                tickets=int(folded[0]),
                cost_usd=Decimal(folded[1]),
                cost_estimated=bool(folded[2]),
            )
            if folded[0]
            else None
        ),
        unrun=int(unrun or 0),
    )


@dataclass(frozen=True, slots=True)
class StepCost:
    """One attempt of one step of one ticket, priced — the segment of the per-ticket bar.

    `entered_by` is the rework rule (`EnteredBy`): `reject_rerun` is rework a gate asked for and
    `redo` rework a person did, `retry` a re-run after a failure or a human release, anything else
    the step's first run, and
    NULL a row older than revision 0023 that is neither. `runtime_type` is the registry row's auth
    family; None for gates and for a run whose row is gone.
    """

    ticket_id: int
    external_key: str
    title: str | None
    step: str
    kind: str
    attempt: int
    entered_by: str | None
    status: str
    model: str | None
    runtime_kind: str | None
    runtime_type: str | None
    #: Zero for a gate, an action or a check, which report no usage.
    cost_usd: Decimal
    cost_estimated: bool
    tokens: int
    #: Active seconds (`_run_seconds`); None while the run is going, or for a row that never
    #: started.
    seconds: float | None
    #: The run's span, which the time view draws its gaps between attempts from. It is longer
    #: than `seconds` by `parked_seconds`.
    started_at: datetime | None
    ended_at: datetime | None
    parked_seconds: float
    #: Who forced the run past its failed conditions (decision 78); its `status` then reads
    #: `succeeded`, which no reader may show as a success.
    forced_by: str | None
    #: When a redo set the run aside (decision 76). It still counts in cost.
    superseded_at: datetime | None = None


async def ticket_step_costs(
    session: AsyncSession, ticket_ids: Sequence[int], *, since: datetime | None
) -> list[StepCost]:
    """Every attempt of the given tickets in the span, in run order (`started_at`, else
    `created_at`, then id), one statement for all of them; `[]` for no ids without a query.

    A run belongs when `coalesce(ended_at, started_at, created_at) >= since`; None is the
    lifetime. Rides the `(ticket_id, step_name, attempt)` unique index for `ticket_id IN (…)`.
    """
    if not ticket_ids:
        return []
    stmt: Select[Any] = (
        select(
            StepRun.ticket_id,
            Ticket.external_key,
            Ticket.title,
            StepRun.step_name,
            StepRun.kind,
            StepRun.attempt,
            StepRun.entered_by,
            StepRun.status,
            StepRun.model,
            StepRun.runtime_kind,
            Runtime.type,
            func.coalesce(StepRun.cost_usd, 0),
            func.coalesce(StepRun.cost_source == CostSource.ESTIMATED, False),
            _run_tokens(),
            _run_seconds(),
            StepRun.started_at,
            StepRun.ended_at,
            StepRun.parked_seconds,
            StepRun.forced_by,
            StepRun.superseded_at,
        )
        .select_from(StepRun)
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .outerjoin(Runtime, Runtime.id == StepRun.runtime_id)
        .where(StepRun.ticket_id.in_(list(ticket_ids)), _in_span(since))
        .order_by(
            StepRun.ticket_id, func.coalesce(StepRun.started_at, StepRun.created_at), StepRun.id
        )
    )
    return [
        StepCost(
            ticket_id=int(r[0]),
            external_key=r[1],
            title=r[2],
            step=r[3],
            kind=r[4],
            attempt=int(r[5]),
            entered_by=r[6],
            status=r[7],
            model=r[8],
            runtime_kind=r[9],
            runtime_type=r[10],
            cost_usd=Decimal(r[11]),
            cost_estimated=bool(r[12]),
            tokens=int(r[13]),
            seconds=None if r[14] is None else float(r[14]),
            started_at=r[15],
            ended_at=r[16],
            parked_seconds=float(r[17]),
            forced_by=r[18],
            superseded_at=r[19],
        )
        for r in (await session.execute(stmt)).all()
    ]


@dataclass(frozen=True, slots=True)
class SpendBucket:
    """One row of "spend that produced no merged change": how many (attempts, or tickets for the
    exited bucket), what they cost, and whether any part of that is an estimate."""

    count: int
    cost_usd: Decimal
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class UnmergedSpend:
    """Spend that produced no merged change, by rule and never by judgement:

    `rework` — agent attempts entered by `reject_rerun` (a gate sent the step back) or by `redo`
    (a person did);
    `exited` — every attempt of a ticket whose status is `exited` (counted in tickets);
    `failed` — agent attempts that ended `failed` or `timeout`;
    `set_aside` — agent attempts a redo set aside: work thrown away, never merged.

    The buckets overlap (a failed rework attempt on an exited ticket is in three), so
    `total_usd` is the union: each attempt counted once.
    """

    rework: SpendBucket
    exited: SpendBucket
    failed: SpendBucket
    set_aside: SpendBucket
    total_usd: Decimal
    cost_estimated: bool


async def unmerged_spend(
    session: AsyncSession, project_id: int, *, since: datetime | None
) -> UnmergedSpend:
    """`UnmergedSpend` in one pass over the project's runs in the span (filtered aggregates).

    A run belongs when `coalesce(ended_at, started_at, created_at) >= since`; None is the
    lifetime. A ticket's `exited` status is read as it is now, whatever the span.
    """
    estimated = StepRun.cost_source == CostSource.ESTIMATED
    rework = and_(
        StepRun.kind == StepKind.AGENT,
        StepRun.entered_by.in_((EnteredBy.REJECT_RERUN, EnteredBy.REDO)),
    )
    exited = Ticket.status == TicketStatus.EXITED
    failed = and_(
        StepRun.kind == StepKind.AGENT,
        StepRun.status.in_((StepRunStatus.FAILED, StepRunStatus.TIMEOUT)),
    )
    set_aside = and_(StepRun.kind == StepKind.AGENT, StepRun.superseded_at.is_not(None))
    unmerged = or_(rework, exited, failed, set_aside)

    def bucket(condition: Any, counted: Any) -> tuple[Any, Any, Any]:
        return (
            func.count(counted).filter(condition),
            func.coalesce(func.sum(StepRun.cost_usd).filter(condition), 0),
            func.coalesce(func.bool_or(estimated).filter(condition), False),
        )

    stmt = _span_runs(
        project_id,
        since,
        *bucket(rework, StepRun.id),
        *bucket(exited, func.distinct(Ticket.id)),
        *bucket(failed, StepRun.id),
        *bucket(set_aside, StepRun.id),
        func.coalesce(func.sum(StepRun.cost_usd).filter(unmerged), 0),
        func.coalesce(func.bool_or(estimated).filter(unmerged), False),
    )
    r = (await session.execute(stmt)).one()

    def out(offset: int) -> SpendBucket:
        return SpendBucket(
            count=int(r[offset]),
            cost_usd=Decimal(r[offset + 1]),
            cost_estimated=bool(r[offset + 2]),
        )

    return UnmergedSpend(
        rework=out(0),
        exited=out(3),
        failed=out(6),
        set_aside=out(9),
        total_usd=Decimal(r[12]),
        cost_estimated=bool(r[13]),
    )


@dataclass(frozen=True, slots=True)
class LargestRun:
    """The costliest single agent attempt in the span."""

    ticket_id: int
    external_key: str
    title: str | None
    step: str
    attempt: int
    model: str | None
    cost_usd: Decimal
    cost_estimated: bool
    tokens: int
    seconds: float | None


@dataclass(frozen=True, slots=True)
class MostAttempts:
    """The `(ticket, step)` with the most agent attempts in the span, ties broken by spend.
    `rework` and `retries` split the attempts by `entered_by`; `rework_cost_usd` is what the
    rework attempts cost."""

    ticket_id: int
    external_key: str
    title: str | None
    step: str
    attempts: int
    rework: int
    retries: int
    cost_usd: Decimal
    rework_cost_usd: Decimal
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class LongestGate:
    """The gate in the span that waited longest from opening to its end, a decision or an exit
    that cut it short (`_gate_wait`). A gate still waiting is in `open_gates`, not here."""

    ticket_id: int
    external_key: str
    title: str | None
    step: str
    attempt: int
    status: str
    seconds: float


@dataclass(frozen=True, slots=True)
class SlowestDone:
    """The ticket done in the span with the longest lead time, admission to its latest
    `ticket_done`, and its single longest gate wait (`_gate_wait`); both wait fields are None
    when it passed no gate."""

    ticket_id: int
    external_key: str
    title: str | None
    lead_seconds: float
    wait_step: str | None
    wait_seconds: float | None


@dataclass(frozen=True, slots=True)
class RunExtremes:
    """The callouts. Each is None when its rule finds nothing: a project with one attempt per
    step has no "most attempts", and a project with no ended gate has no longest wait."""

    largest_run: LargestRun | None
    most_attempts: MostAttempts | None
    longest_gate: LongestGate | None
    slowest_done: SlowestDone | None


async def run_extremes(
    session: AsyncSession, project_id: int, *, since: datetime | None
) -> RunExtremes:
    """`RunExtremes` for one project: an `ORDER BY … LIMIT 1` statement per callout over the
    runs in the span, and one more for the slowest ticket's longest wait. A run belongs when
    `coalesce(ended_at, started_at, created_at) >= since`; a done ticket when its `ticket_done`
    is at or after `since`. None is the lifetime."""
    seconds = _run_seconds()

    largest_row = (
        await session.execute(
            _span_runs(
                project_id,
                since,
                StepRun.ticket_id,
                Ticket.external_key,
                Ticket.title,
                StepRun.step_name,
                StepRun.attempt,
                StepRun.model,
                StepRun.cost_usd,
                func.coalesce(StepRun.cost_source == CostSource.ESTIMATED, False),
                _run_tokens(),
                seconds,
            )
            .where(StepRun.kind == StepKind.AGENT, StepRun.cost_usd.is_not(None))
            .order_by(StepRun.cost_usd.desc(), StepRun.id)
            .limit(1)
        )
    ).one_or_none()
    largest = (
        None
        if largest_row is None
        else LargestRun(
            ticket_id=int(largest_row[0]),
            external_key=largest_row[1],
            title=largest_row[2],
            step=largest_row[3],
            attempt=int(largest_row[4]),
            model=largest_row[5],
            cost_usd=Decimal(largest_row[6]),
            cost_estimated=bool(largest_row[7]),
            tokens=int(largest_row[8]),
            seconds=None if largest_row[9] is None else float(largest_row[9]),
        )
    )

    is_rework = StepRun.entered_by.in_((EnteredBy.REJECT_RERUN, EnteredBy.REDO))
    attempts = func.count(StepRun.id)
    spent = func.coalesce(func.sum(StepRun.cost_usd), 0)
    most_row = (
        await session.execute(
            _span_runs(
                project_id,
                since,
                StepRun.ticket_id,
                Ticket.external_key,
                Ticket.title,
                StepRun.step_name,
                attempts,
                func.count(StepRun.id).filter(is_rework),
                func.count(StepRun.id).filter(StepRun.entered_by == EnteredBy.RETRY),
                spent,
                func.coalesce(func.sum(StepRun.cost_usd).filter(is_rework), 0),
                _any_estimated(),
            )
            .where(StepRun.kind == StepKind.AGENT)
            .group_by(StepRun.ticket_id, Ticket.external_key, Ticket.title, StepRun.step_name)
            .having(attempts > 1)
            .order_by(attempts.desc(), spent.desc(), StepRun.ticket_id)
            .limit(1)
        )
    ).one_or_none()
    most = (
        None
        if most_row is None
        else MostAttempts(
            ticket_id=int(most_row[0]),
            external_key=most_row[1],
            title=most_row[2],
            step=most_row[3],
            attempts=int(most_row[4]),
            rework=int(most_row[5]),
            retries=int(most_row[6]),
            cost_usd=Decimal(most_row[7]),
            rework_cost_usd=Decimal(most_row[8]),
            cost_estimated=bool(most_row[9]),
        )
    )

    gate_row = (
        await session.execute(
            _span_runs(
                project_id,
                since,
                StepRun.ticket_id,
                Ticket.external_key,
                Ticket.title,
                StepRun.step_name,
                StepRun.attempt,
                StepRun.status,
                seconds,
            )
            .where(_gate_wait())
            .order_by(seconds.desc().nulls_last(), StepRun.id)
            .limit(1)
        )
    ).one_or_none()
    gate = (
        None
        if gate_row is None
        else LongestGate(
            ticket_id=int(gate_row[0]),
            external_key=gate_row[1],
            title=gate_row[2],
            step=gate_row[3],
            attempt=int(gate_row[4]),
            status=gate_row[5],
            seconds=float(gate_row[6]),
        )
    )
    done = _done_in(project_id, since, None)
    lead = _lead_seconds(done)
    slow_row = (
        await session.execute(
            select(Ticket.id, Ticket.external_key, Ticket.title, lead)
            .select_from(done)
            .join(Ticket, Ticket.id == done.c.ticket_id)
            .order_by(lead.desc(), Ticket.id)
            .limit(1)
        )
    ).one_or_none()
    slowest: SlowestDone | None = None
    if slow_row is not None:
        wait_row = (
            await session.execute(
                select(StepRun.step_name, seconds)
                .where(StepRun.ticket_id == slow_row[0], _gate_wait())
                .order_by(seconds.desc(), StepRun.id)
                .limit(1)
            )
        ).one_or_none()
        slowest = SlowestDone(
            ticket_id=int(slow_row[0]),
            external_key=slow_row[1],
            title=slow_row[2],
            lead_seconds=float(slow_row[3]),
            wait_step=None if wait_row is None else wait_row[0],
            wait_seconds=None if wait_row is None else float(wait_row[1]),
        )
    return RunExtremes(
        largest_run=largest, most_attempts=most, longest_gate=gate, slowest_done=slowest
    )
