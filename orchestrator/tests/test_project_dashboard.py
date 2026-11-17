"""The project dashboard's read models (tech-stack decision 61)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.dashboard import project_dashboard
from orchestrator.db.models import (
    CostSource,
    EnteredBy,
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    TicketStatus,
)
from orchestrator.events.read_models import (
    ClosedTicket,
    SlowestDone,
    SpendFold,
    done_ticket_cost,
    open_gates,
    open_ticket_count,
    period_figures,
    run_extremes,
    spend_by_outcome,
    spend_by_ticket,
    step_medians,
    ticket_step_costs,
    time_split,
    unmerged_spend,
    week_summaries,
)
from orchestrator.events.types import EventType
from tests.api_harness import make_run, seed_project, seed_ticket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_rework_and_retry_are_told_apart_by_entered_by(async_session: AsyncSession) -> None:
    """The rework bucket is attempts a gate sent back, never attempts a failure re-ran. The two
    look alike on `step_run` except for `entered_by`, and the one number the tab exists for
    would be wrong by every retry, with no error anywhere."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="build", title="comments")
    async_session.add_all(
        [
            make_run(ticket.id, "build", 1, cost="1.00"),
            make_run(ticket.id, "build", 2, cost="2.00", entered_by=EnteredBy.REJECT_RERUN),
            make_run(
                ticket.id,
                "build",
                3,
                cost="0.50",
                entered_by=EnteredBy.RETRY,
                status=StepRunStatus.FAILED,
            ),
            make_run(ticket.id, "build", 4, cost="3.00", entered_by=EnteredBy.RETRY),
            make_run(
                ticket.id,
                "review",
                1,
                cost="0",
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.APPROVED,
                source=None,
                started_at=datetime.now(UTC) - timedelta(minutes=55),
            ),
        ]
    )
    await async_session.flush()

    unmerged = await unmerged_spend(async_session, project.id, since=None)
    assert (unmerged.rework.count, unmerged.rework.cost_usd) == (1, Decimal("2.00"))
    assert (unmerged.failed.count, unmerged.failed.cost_usd) == (1, Decimal("0.50"))
    assert unmerged.exited.count == 0
    # Each attempt once: the failed retry is not also counted as rework.
    assert unmerged.total_usd == Decimal("2.50")

    extremes = await run_extremes(async_session, project.id, since=None)
    assert extremes.most_attempts is not None
    assert (
        extremes.most_attempts.attempts,
        extremes.most_attempts.rework,
        extremes.most_attempts.retries,
        extremes.most_attempts.rework_cost_usd,
    ) == (4, 1, 2, Decimal("2.00"))
    assert extremes.largest_run is not None
    assert (extremes.largest_run.attempt, extremes.largest_run.cost_usd) == (4, Decimal("3.00"))

    segments = await ticket_step_costs(async_session, [ticket.id], since=None)
    assert [(s.step, s.attempt, s.entered_by) for s in segments] == [
        ("build", 1, "next"),
        ("build", 2, "reject_rerun"),
        ("build", 3, "retry"),
        ("build", 4, "retry"),
        ("review", 1, "next"),
    ]
    assert segments[4].cost_usd == Decimal(0)


async def test_a_set_aside_attempt_is_unmerged_spend_once(async_session: AsyncSession) -> None:
    """Thrown-away work never merged. Missing it makes the card read low, with no error. The
    set-aside attempt succeeded, as one a gate reviewed does, so no other bucket holds it."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="build")
    discarded = make_run(ticket.id, "build", 1, cost="1.00")
    discarded.superseded_at = datetime.now(UTC)
    async_session.add_all(
        [discarded, make_run(ticket.id, "build", 2, cost="2.00", entered_by=EnteredBy.REDO)]
    )
    await async_session.flush()

    unmerged = await unmerged_spend(async_session, project.id, since=None)
    assert (unmerged.set_aside.count, unmerged.set_aside.cost_usd) == (1, Decimal("1.00"))
    assert (unmerged.rework.count, unmerged.rework.cost_usd) == (1, Decimal("2.00"))
    assert unmerged.failed.count == 0 and unmerged.total_usd == Decimal("3.00")


async def test_a_ticket_with_no_runs_counts_as_open_with_nothing_spent(
    async_session: AsyncSession,
) -> None:
    """An admitted ticket is an open ticket before its first run. `cost_per_ticket`'s inner join
    drops it, and a stacked bar that loses it under-counts the project by a ticket."""
    project = await seed_project(async_session)
    await seed_ticket(async_session, project)
    done = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    async_session.add(make_run(done.id, "build", 1, cost="4.00"))
    await async_session.flush()

    outcomes = await spend_by_outcome(async_session, project.id, since=None)

    assert [row.outcome for row in outcomes] == ["done", "open", "exited"]
    rows = {row.outcome: row for row in outcomes}
    assert (rows["open"].tickets, rows["open"].cost_usd) == (1, Decimal(0))
    assert (rows["done"].tickets, rows["done"].cost_usd) == (1, Decimal("4.00"))
    assert (rows["exited"].tickets, rows["exited"].cost_usd) == (0, Decimal(0))

    by_ticket = await spend_by_ticket(async_session, project.id, since=None, limit=12)
    assert [(t.ticket_id, t.attempts, t.cost_usd) for t in by_ticket.top] == [
        (done.id, 1, Decimal("4.00")),
    ]
    assert (by_ticket.rest, by_ticket.unrun) == (None, 1)


async def test_a_ticket_whose_runs_predate_the_span_is_not_counted_as_unrun(
    async_session: AsyncSession,
) -> None:
    """Under a span a ticket whose runs all predate it is neither drawn nor folded; the un-run
    count stays lifetime, so it does not take every ticket older than the span for one."""
    project = await seed_project(async_session)
    old = await seed_ticket(async_session, project)
    await seed_ticket(async_session, project)
    async_session.add(make_run(old.id, "build", 1, cost="1.00"))
    await async_session.flush()
    since = datetime.now(UTC) - timedelta(minutes=5)

    spanned = await spend_by_ticket(async_session, project.id, since=since, limit=12)
    assert (spanned.top, spanned.rest, spanned.unrun) == ([], None, 1)

    lifetime = await spend_by_ticket(async_session, project.id, since=None, limit=12)
    assert [(t.ticket_id, t.attempts) for t in lifetime.top] == [(old.id, 1)]
    assert lifetime.unrun == 1


async def test_cost_by_ticket_folds_every_ticket_past_the_cut(async_session: AsyncSession) -> None:
    """The cut is made in SQL, so the fold is a second aggregate over the same ranking. One that
    drifts from it drops or double-counts a ticket in the line under the bars, silently."""
    project = await seed_project(async_session)
    tickets = [await seed_ticket(async_session, project) for _ in range(3)]
    async_session.add_all(
        [
            make_run(tickets[0].id, "build", 1, cost="1.00"),
            make_run(tickets[1].id, "build", 1, cost="3.00"),
            make_run(tickets[2].id, "build", 1, cost="2.00", source=CostSource.ESTIMATED),
        ]
    )
    await async_session.flush()

    cut = await spend_by_ticket(async_session, project.id, since=None, limit=1)

    assert [t.ticket_id for t in cut.top] == [tickets[1].id]
    assert cut.rest == SpendFold(tickets=2, cost_usd=Decimal("3.00"), cost_estimated=True)


async def test_one_estimated_attempt_makes_its_bucket_an_estimate(
    async_session: AsyncSession,
) -> None:
    """The `~` travels with every sum it is part of and no further: one priced-at-list attempt
    marks its ticket's outcome bucket, its week and the unmerged total, not the other buckets."""
    project = await seed_project(async_session)
    exited = await seed_ticket(async_session, project, step="build", status=TicketStatus.EXITED)
    done = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    # The runs below are inside the last hour; a lifetime span starts at the first admission,
    # which `seed_ticket` leaves at the transaction's now().
    exited.admitted_at = datetime.now(UTC) - timedelta(hours=2)
    done.admitted_at = datetime.now(UTC) - timedelta(hours=2)
    async_session.add_all(
        [
            make_run(exited.id, "build", 1, cost="1.00", source=CostSource.ESTIMATED),
            make_run(done.id, "build", 1, cost="2.00"),
            Event(project_id=project.id, ticket_id=done.id, type=EventType.TICKET_DONE, details={}),
        ]
    )
    await async_session.flush()

    rows = {
        row.outcome: row for row in await spend_by_outcome(async_session, project.id, since=None)
    }
    assert rows["exited"].cost_estimated is True
    assert rows["done"].cost_estimated is False

    unmerged = await unmerged_spend(async_session, project.id, since=None)
    assert unmerged.exited.cost_estimated is True
    assert unmerged.rework.cost_estimated is False
    assert unmerged.cost_estimated is True

    weeks = await week_summaries(
        async_session, project.id, since=exited.admitted_at, until=datetime.now(UTC)
    )
    week = next(w for w in weeks if w.cost_usd)
    assert (week.cost_usd, week.cost_estimated) == (Decimal("3.00"), True)
    assert next(w for w in weeks if w.closed).closed == [
        ClosedTicket(ticket_id=done.id, external_key=done.external_key, outcome="done")
    ]
    per_done = await done_ticket_cost(async_session, project.id, since=None)
    assert per_done.median_usd == Decimal("2.0000")


async def test_step_times_leave_out_pauses_and_hold_p90_until_seven_runs(
    async_session: AsyncSession,
) -> None:
    """Every duration subtracts the time a person held the run paused: a resume keeps
    `started_at`, so one run parked for an afternoon reads as the step's longest by hours. And
    a 90th percentile of a handful of waits is the slowest one under another name, so the gate's
    two waits get none."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="build")
    t0 = datetime.now(UTC) - timedelta(days=2)
    builds = [
        make_run(
            ticket.id,
            "build",
            n,
            cost="1.00",
            started_at=t0 + timedelta(hours=n),
            took=timedelta(minutes=n),
        )
        for n in range(1, 7)
    ]
    # Two hours on the clock, seven minutes of them working.
    paused = make_run(
        ticket.id,
        "build",
        7,
        cost="1.00",
        started_at=t0 + timedelta(hours=7),
        took=timedelta(hours=2),
    )
    paused.parked_seconds = 7200.0 - 420.0
    gates = [
        make_run(
            ticket.id,
            "review",
            attempt,
            cost="0",
            kind=StepKind.HUMAN_GATE,
            status=StepRunStatus.APPROVED,
            source=None,
            started_at=t0 + timedelta(hours=10 + 4 * attempt),
            took=timedelta(hours=wait),
        )
        for attempt, wait in ((1, 1), (2, 3))
    ]
    async_session.add_all([*builds, paused, *gates])
    await async_session.flush()

    step_times, gate_waits = await step_medians(async_session, project.id, since=t0)

    assert [(s.step, s.runs, s.median_seconds, s.max_seconds) for s in step_times] == [
        ("build", 7, 240.0, 420.0)
    ]
    # percentile_cont over 60…420 s: 360 + 0.4 * 60.
    assert step_times[0].p90_seconds == pytest.approx(384.0)
    assert [
        (g.step, g.runs, g.median_seconds, g.p90_seconds, g.max_seconds) for g in gate_waits
    ] == [("review", 2, 7200.0, None, 10800.0)]

    segments = await ticket_step_costs(async_session, [ticket.id], since=None)
    last = next(s for s in segments if (s.step, s.attempt) == ("build", 7))
    assert (last.seconds, last.parked_seconds) == (420.0, 6780.0)
    assert last.started_at is not None and last.ended_at is not None
    assert last.ended_at - last.started_at == timedelta(hours=2)


async def test_the_period_counts_only_what_falls_inside_it(async_session: AsyncSession) -> None:
    """Both bounds are half-open, and each figure has its own clock: a run's `_run_at`, a
    closure's event, an admission's `admitted_at`. A figure that reads the wrong clock or drops
    `until` still renders a card, and the change line against the previous period is wrong with
    it, silently."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    since, until = now - timedelta(days=14), now - timedelta(days=7)
    slow = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    quick = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    later = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    await seed_ticket(async_session, project)
    await seed_ticket(async_session, project, step="build", status=TicketStatus.EXITED)
    slow.admitted_at = now - timedelta(days=20)
    quick.admitted_at = now - timedelta(days=12)
    later.admitted_at = now - timedelta(days=30)
    async_session.add_all(
        [
            make_run(slow.id, "develop", 1, cost="1.00", started_at=now - timedelta(days=11)),
            make_run(slow.id, "develop", 2, cost="2.00", started_at=now - timedelta(days=15)),
            # Started before the period and ended in it: a run sits where it ended.
            make_run(
                quick.id,
                "develop",
                1,
                cost="0.50",
                source=CostSource.ESTIMATED,
                started_at=since - timedelta(minutes=5),
            ),
            make_run(later.id, "develop", 1, cost="4.00", started_at=now - timedelta(days=2)),
            Event(
                project_id=project.id,
                ticket_id=slow.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=10),
            ),
            # Done, re-entered and done again: one ticket, timed to its latest done in the period.
            Event(
                project_id=project.id,
                ticket_id=quick.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=11),
            ),
            Event(
                project_id=project.id,
                ticket_id=quick.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=8),
            ),
            Event(
                project_id=project.id,
                ticket_id=later.id,
                type=EventType.TICKET_EXITED,
                details={},
                created_at=now - timedelta(days=13),
            ),
            Event(
                project_id=project.id,
                ticket_id=later.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=3),
            ),
        ]
    )
    await async_session.flush()

    period = await period_figures(async_session, project.id, since=since, until=until)

    assert (period.spend.cost_usd, period.spend.cost_estimated) == (Decimal("1.50"), True)
    assert (period.runs.total, period.runs.priced, period.runs.estimated) == (2, 2, 1)
    assert (period.tickets_done, period.tickets_exited, period.tickets_admitted) == (2, 1, 1)
    # Priced whole: slow's run before the period counts; later, done after it, does not.
    assert (period.per_done.tickets, period.per_done.cost_usd) == (2, Decimal("3.50"))
    assert period.velocity_per_week == pytest.approx(2.0)
    assert period.lead_time_tickets == 2
    # slow took 10 days, quick 4 to its second done.
    assert period.lead_time_median_seconds == pytest.approx(7 * 86400)

    short = await period_figures(
        async_session, project.id, since=now - timedelta(hours=1), until=now
    )
    assert short.velocity_per_week is None
    assert await open_ticket_count(async_session, project.id) == 1


async def test_a_period_with_nothing_in_it_has_no_rate_and_no_median(
    async_session: AsyncSession,
) -> None:
    """The cards draw a dash for None and the figure for a zero. A project with no tickets has
    `since = as_of`, a period of no length: its rate must be None, never a division by zero. A
    week with runs but nothing done is a rate of zero, and still has no median lead time."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)

    empty = await period_figures(async_session, project.id, since=now, until=now)

    assert (empty.tickets_done, empty.tickets_exited, empty.tickets_admitted) == (0, 0, 0)
    assert (empty.runs.total, empty.spend.cost_usd) == (0, Decimal(0))
    assert (empty.per_done.tickets, empty.per_done.mean_usd, empty.per_done.median_usd) == (
        0,
        None,
        None,
    )
    assert empty.velocity_per_week is None
    assert (empty.lead_time_tickets, empty.lead_time_median_seconds) == (0, None)

    ticket = await seed_ticket(async_session, project)
    async_session.add(make_run(ticket.id, "develop", 1, cost="1.00"))
    await async_session.flush()

    idle = await period_figures(async_session, project.id, since=now - timedelta(days=7), until=now)

    assert (idle.runs.total, idle.tickets_done) == (1, 0)
    assert idle.velocity_per_week == 0.0
    assert idle.lead_time_median_seconds is None


async def test_a_done_tickets_lead_time_splits_into_work_waits_pauses_and_idle(
    async_session: AsyncSession,
) -> None:
    """Arithmetic someone will change. Active time already has the pause taken out, so counting
    it again under paused, or leaving out a gate an exit cut short, mis-splits every ticket and
    the bar still adds up to 100%."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    admitted = now - timedelta(days=3)
    ticket = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    earlier = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    ticket.admitted_at = admitted
    earlier.admitted_at = now - timedelta(days=30)
    develop = make_run(
        ticket.id,
        "develop",
        1,
        cost="1.00",
        started_at=admitted + timedelta(hours=1),
        took=timedelta(hours=5),
    )
    # One of the five hours sat parked for a person.
    develop.parked_seconds = 3600.0
    async_session.add_all(
        [
            develop,
            make_run(
                ticket.id,
                "spec-review",
                1,
                cost=None,
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.APPROVED,
                source=None,
                tokens=None,
                started_at=admitted + timedelta(hours=6),
                took=timedelta(hours=10),
            ),
            make_run(
                ticket.id,
                "verify",
                1,
                cost=None,
                kind=StepKind.CHECK,
                source=None,
                tokens=None,
                started_at=admitted + timedelta(hours=16),
                took=timedelta(minutes=30),
            ),
            # Cut short by an exit; the ticket came back and finished.
            make_run(
                ticket.id,
                "impl-review",
                1,
                cost=None,
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.CANCELLED,
                source=None,
                tokens=None,
                started_at=admitted + timedelta(hours=17),
                took=timedelta(hours=1),
            ),
            make_run(earlier.id, "develop", 1, cost="1.00", started_at=now - timedelta(days=29)),
            Event(
                project_id=project.id,
                ticket_id=ticket.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=admitted + timedelta(hours=24),
            ),
            Event(
                project_id=project.id,
                ticket_id=earlier.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=20),
            ),
        ]
    )
    await async_session.flush()

    split = await time_split(async_session, project.id, since=now - timedelta(days=7), until=now)

    hour = 3600.0
    assert split.tickets == 1
    assert split.lead_seconds == pytest.approx(24 * hour)
    assert split.agent_seconds == pytest.approx(4.5 * hour)
    assert split.gate_seconds == pytest.approx(11 * hour)
    assert split.paused_seconds == pytest.approx(hour)
    assert split.idle_seconds == pytest.approx(7.5 * hour)


async def test_a_run_still_going_adds_nothing_to_the_time_split(
    async_session: AsyncSession,
) -> None:
    """A run that has not ended has no active seconds yet (NULL). A split that counted it up to
    now would move a done ticket's time from idle to agent while the run goes on, and the bar
    would still add up to 100%."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    admitted = now - timedelta(days=2)
    ticket = await seed_ticket(async_session, project, step="develop", status=TicketStatus.RUNNING)
    ticket.admitted_at = admitted
    # Done, then re-entered: its second attempt is still going.
    running = make_run(
        ticket.id,
        "develop",
        2,
        cost="1.00",
        status=StepRunStatus.RUNNING,
        started_at=now - timedelta(hours=1),
    )
    running.ended_at = None
    async_session.add_all(
        [
            make_run(
                ticket.id,
                "develop",
                1,
                cost="1.00",
                started_at=admitted + timedelta(hours=1),
                took=timedelta(hours=2),
            ),
            running,
            Event(
                project_id=project.id,
                ticket_id=ticket.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=admitted + timedelta(hours=10),
            ),
        ]
    )
    await async_session.flush()

    split = await time_split(async_session, project.id, since=now - timedelta(days=7), until=now)

    hour = 3600.0
    assert (split.tickets, split.lead_seconds) == (1, pytest.approx(10 * hour))
    assert split.agent_seconds == pytest.approx(2 * hour)
    assert split.idle_seconds == pytest.approx(8 * hour)


async def test_gate_waits_count_a_cancelled_wait_and_open_gates_wait_oldest_first(
    async_session: AsyncSession,
) -> None:
    """A wait an exit cut short was still a wait on a person, and the slowest ticket names the
    gate it sat at longest. Drop either rule and an Outliers line names the wrong ticket or step,
    with nothing on the page to say so."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    slow = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    quick = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    slow.admitted_at = now - timedelta(days=10)
    quick.admitted_at = now - timedelta(days=6)
    waiting = [
        await seed_ticket(async_session, project, step="review", status=TicketStatus.WAITING_GATE)
        for _ in range(3)
    ]

    def gate(
        ticket_id: int, step: str, status: str, started_at: datetime, took: timedelta
    ) -> StepRun:
        return make_run(
            ticket_id,
            step,
            1,
            cost=None,
            kind=StepKind.HUMAN_GATE,
            status=status,
            source=None,
            tokens=None,
            started_at=started_at,
            took=took,
        )

    opened = [
        gate(t.id, "review", StepRunStatus.WAITING, now - timedelta(hours=h), timedelta(0))
        for t, h in zip(waiting, (1, 4, 2), strict=True)
    ]
    for run in opened:
        run.ended_at = None
    async_session.add_all(
        [
            gate(
                slow.id,
                "spec-review",
                StepRunStatus.APPROVED,
                now - timedelta(days=9),
                timedelta(days=2),
            ),
            gate(
                slow.id,
                "impl-review",
                StepRunStatus.APPROVED,
                now - timedelta(days=5),
                timedelta(hours=1),
            ),
            gate(
                quick.id,
                "spec-review",
                StepRunStatus.CANCELLED,
                now - timedelta(days=5, hours=12),
                timedelta(days=3),
            ),
            *opened,
            Event(
                project_id=project.id,
                ticket_id=slow.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=1),
            ),
            Event(
                project_id=project.id,
                ticket_id=quick.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=2),
            ),
        ]
    )
    await async_session.flush()

    extremes = await run_extremes(async_session, project.id, since=None)
    assert extremes.longest_gate is not None
    assert (extremes.longest_gate.ticket_id, extremes.longest_gate.status) == (
        quick.id,
        "cancelled",
    )
    assert extremes.slowest_done == SlowestDone(
        ticket_id=slow.id,
        external_key=slow.external_key,
        title=None,
        lead_seconds=9 * 86400.0,
        wait_step="spec-review",
        wait_seconds=2 * 86400.0,
    )

    gates = await open_gates(async_session, project.id, limit=2)
    assert [(g.ticket_id, g.waiting_since) for g in gates] == [
        (waiting[1].id, now - timedelta(hours=4)),
        (waiting[2].id, now - timedelta(hours=2)),
    ]


async def test_a_wait_a_conflict_send_back_ended_is_no_gate_wait(
    async_session: AsyncSession,
) -> None:
    """The loop ended that wait, no person decided it, so it enters neither the gate's wait
    figures nor the longest gate."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="review")
    t0 = datetime.now(UTC) - timedelta(days=2)
    approved, rejected, sent_back = (
        make_run(
            ticket.id,
            "review",
            attempt,
            cost=None,
            kind=StepKind.HUMAN_GATE,
            status=status,
            source=None,
            tokens=None,
            started_at=t0 + timedelta(hours=12 * attempt),
            took=timedelta(hours=wait),
        )
        for attempt, status, wait in (
            (1, StepRunStatus.APPROVED, 1),
            (2, StepRunStatus.REJECTED, 3),
            (3, StepRunStatus.REJECTED, 10),
        )
    )
    async_session.add_all([approved, rejected, sent_back])
    await async_session.flush()
    async_session.add_all(
        [
            Event(
                project_id=project.id,
                ticket_id=ticket.id,
                step_run_id=rejected.id,
                type=EventType.GATE_REJECTED,
                details={},
            ),
            Event(
                project_id=project.id,
                ticket_id=ticket.id,
                step_run_id=sent_back.id,
                type=EventType.GATE_CONFLICT_SENT_BACK,
                details={},
            ),
        ]
    )
    await async_session.flush()

    _, gate_waits = await step_medians(async_session, project.id, since=t0)
    assert [(g.step, g.runs, g.max_seconds) for g in gate_waits] == [("review", 2, 3 * 3600.0)]
    extremes = await run_extremes(async_session, project.id, since=None)
    assert extremes.longest_gate is not None
    assert (extremes.longest_gate.attempt, extremes.longest_gate.seconds) == (2, 3 * 3600.0)


async def test_under_a_span_an_outcome_counts_only_tickets_that_ran_in_it(
    async_session: AsyncSession,
) -> None:
    """The count sits beside the window's spend, so it must describe the same tickets; only the
    lifetime row keeps every ticket, unrun ones included."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    old = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    fresh = await seed_ticket(async_session, project)
    await seed_ticket(async_session, project)
    async_session.add_all(
        [
            make_run(old.id, "develop", 1, cost="2.00", started_at=now - timedelta(hours=3)),
            make_run(fresh.id, "develop", 1, cost="1.00"),
        ]
    )
    await async_session.flush()

    spanned = await spend_by_outcome(async_session, project.id, since=now - timedelta(minutes=90))
    lifetime = await spend_by_outcome(async_session, project.id, since=None)

    assert {row.outcome: row.tickets for row in spanned} == {"done": 0, "open": 1, "exited": 0}
    assert {row.outcome: row.tickets for row in lifetime} == {"done": 1, "open": 2, "exited": 0}


async def test_every_card_and_section_counts_only_the_window(async_session: AsyncSession) -> None:
    """The regression behind the windowed tab: a figure that loses its bound still renders, just
    over the wrong period. Runs and closures sit on both sides of a 4-week window, and every
    figure but `lifetime` and the NOW ones must see only the inside.

    A 4-week window reaches back 28 to 35 days, whatever the weekday, so -40 days is always
    outside and -3 always inside; a project first admitted 45 days ago is too young for a
    previous 4 weeks."""
    project = await seed_project(async_session)
    now = datetime.now(UTC)
    old = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    fresh = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    gone = await seed_ticket(async_session, project, step="build", status=TicketStatus.EXITED)
    live = await seed_ticket(
        async_session, project, step="review", status=TicketStatus.WAITING_GATE
    )
    old.admitted_at = now - timedelta(days=45)
    gone.admitted_at = now - timedelta(days=44)
    live.admitted_at = now - timedelta(days=43)
    fresh.admitted_at = now - timedelta(days=10)
    waiting = make_run(
        live.id,
        "review",
        1,
        cost=None,
        kind=StepKind.HUMAN_GATE,
        status=StepRunStatus.WAITING,
        source=None,
        tokens=None,
        started_at=now - timedelta(hours=5),
    )
    waiting.ended_at = None
    async_session.add_all(
        [
            make_run(old.id, "develop", 1, cost="8.00", started_at=now - timedelta(days=41)),
            make_run(gone.id, "develop", 1, cost="16.00", started_at=now - timedelta(days=42)),
            make_run(fresh.id, "develop", 1, cost="2.00", started_at=now - timedelta(days=3)),
            make_run(
                fresh.id,
                "review",
                1,
                cost=None,
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.APPROVED,
                source=None,
                tokens=None,
                started_at=now - timedelta(days=2, hours=12),
                took=timedelta(hours=6),
            ),
            make_run(live.id, "develop", 1, cost="1.00", started_at=now - timedelta(days=1)),
            waiting,
            Event(
                project_id=project.id,
                ticket_id=old.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=40),
            ),
            Event(
                project_id=project.id,
                ticket_id=fresh.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=2),
            ),
            Event(
                project_id=project.id,
                ticket_id=gone.id,
                type=EventType.TICKET_EXITED,
                details={},
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    await async_session.flush()

    out = await project_dashboard(async_session, project.id, span="4w")

    assert (out.since.weekday(), out.since.hour, out.since.minute, out.clipped) == (0, 0, 0, False)
    assert (out.previous, out.previous_since) == (None, None)
    period = out.period
    assert (period.spend.cost_usd, period.runs.total, period.runs.priced) == (
        Decimal("3.00"),
        4,
        2,
    )
    assert (period.tickets.done, period.tickets.exited, period.tickets.admitted) == (1, 1, 1)
    assert (period.per_done.tickets, period.per_done.median_usd) == (1, Decimal("2.00"))
    assert period.velocity_per_week == pytest.approx(
        1 / ((out.as_of - out.since) / timedelta(weeks=1))
    )
    assert (period.lead_time.tickets, period.lead_time.median_seconds) == (1, 8 * 86400.0)
    assert (out.time_split.tickets, out.time_split.lead_seconds) == (1, 8 * 86400.0)

    assert {row.outcome: (row.tickets, row.cost_usd) for row in out.by_outcome} == {
        "done": (1, Decimal("2.00")),
        "open": (1, Decimal("1.00")),
        "exited": (0, Decimal(0)),
    }
    assert [t.ticket_id for t in out.by_ticket] == [fresh.id, live.id]
    assert [t.ticket_id for t in out.step_costs] == [fresh.id, live.id]
    assert (out.unmerged.exited.count, out.unmerged.total_usd) == (0, Decimal(0))
    assert [s.runs for s in out.steps] == [2]
    assert [g.runs for g in out.gates] == [1]
    assert len(out.weeks) == 5
    assert out.weeks[0].week_start == out.since.date()
    assert (
        sum(w.done for w in out.weeks),
        sum(w.exited for w in out.weeks),
        sum(w.cost_usd for w in out.weeks),
    ) == (1, 1, Decimal("3.00"))
    extremes = out.extremes
    assert extremes.largest_run is not None
    assert extremes.largest_run.cost_usd == Decimal("2.00")
    assert extremes.slowest_done is not None
    assert extremes.slowest_done.ticket_id == fresh.id
    assert extremes.longest_gate is not None
    assert extremes.longest_gate.ticket_id == fresh.id

    # NOW figures and the lifetime block ignore the window.
    assert out.open_now == 1
    assert [g.ticket_id for g in out.open_gates] == [live.id]
    assert extremes.longest_open_wait == out.open_gates[0]
    assert (
        out.lifetime.tickets.total,
        out.lifetime.tickets.done,
        out.lifetime.tickets.exited,
    ) == (4, 2, 1)
    assert out.lifetime.spend.cost_usd == Decimal("27.00")
