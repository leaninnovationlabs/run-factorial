"""What the loop does with a ticket whose tick raises on every single tick.

This module documents a CONTRACT. Before quarantine there was none: `worker/loop.py`'s `one()`
wrapped each `reconcile_ticket` call in a bare `except Exception: logger.exception(...)`, and
`db/session.py`'s `session_scope` rolls the unit of work back on any raise — so a ticket whose
transition raised committed nothing, and the next tick attempted the identical transition against
the identical observed state, and raised again, every 30 seconds for ever, with nothing counting
it and nothing outside one log line to say so.

What must hold now:

* a raising tick still commits nothing, and counts one against `ticket.consecutive_failures` —
  written in the error handler's OWN session, because the failing tick's transaction is already
  rolled back and a counter incremented inside it would vanish with the failure it counts;
* under `worker.quarantine_after` that is the only trace: no event, no state change, and the
  ticket is still leased, so a transient failure costs a ticket nothing;
* at the threshold the ticket is stamped `quarantined_at`, one `ticket_quarantined` event carries
  the error and the count, and `acquire_leases` stops handing the ticket out. Leasing is what
  quarantine gates, not status: marking the ticket BLOCKED would not stop it, because
  `reconcile_ticket`'s parked check needs the parking LABEL on the issue, and writing that label
  is exactly what fails here;
* past the threshold nothing is attempted again at all, and the one event stays one;
* a tick that does not raise clears the counter;
* the courtesy comment on the issue can never decide whether the quarantine happens — the whole
  motivating case is GitHub refusing that write.

The live route this module drives: a repository ARCHIVED while a ticket is in flight 403s every
write, so the projector raises on the label write of every projecting
tick. `mockdown` is archived in this fleet, and its ticket had to be retired by hand.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import Event, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github.client import RateLimited
from orchestrator.settings import Settings, WorkerSettings
from orchestrator.worker.context import Projector
from orchestrator.worker.leases import acquire_leases, release
from orchestrator.worker.loop import _MAX_ERROR_CHARS, _clear_failures, _record_failure, tick
from runtime.contract import SubstrateUnavailable
from tests.reconcile_world import (
    _LEASE_OWNER,
    RecordingProjector,
    World,
    _build_world,
    _FakeRequestFailed,
    event_rows,
    run_tick,
    step_runs,
    ticket_row,
    with_github,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_OWNER = "runner-poison"
#: The lease `_hold_the_bystanders` gives every ticket but this module's own.
_HOLD = "poison-bystanders"
_TTL = timedelta(seconds=60)
#: Low enough to reach inside a test, and the one number every assertion below counts against.
_AFTER = 3


def _settings() -> Settings:
    """Hermetic, like `api_harness.make_settings`: `Settings()` reads `.env`, and this module
    must count against its own `quarantine_after` rather than a developer's."""
    return Settings(worker=WorkerSettings(quarantine_after=_AFTER))


class _ArchivedRepo(RecordingProjector):
    """A projector for a repository archived under a live ticket: GitHub 403s every write.

    The label write is where the 403 lands first — `_project` writes labels before assignees,
    comments and the close — so raising there is raising on the whole projection. The attempt is
    recorded before the raise, and the fake repo is left exactly as a refused write leaves it.
    """

    async def set_labels(self, issue_number: int, labels: Any) -> None:
        self.calls.append(("set_labels", frozenset(labels)))
        raise _FakeRequestFailed(f"PUT /repos/…/issues/{issue_number}/labels → 403 (archived)")


class _ArchivedRepoRefusingComments(_ArchivedRepo):
    """The honest version of the motivating case: an archived repo refuses the courtesy comment
    too. Quarantine must happen anyway."""

    async def comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("comment", body))
        raise _FakeRequestFailed(f"POST /repos/…/issues/{issue_number}/comments → 403 (archived)")


@dataclass
class _ArchivedRepoThatHangsOnTheComment(_ArchivedRepo):
    """The courtesy comment, held open. A slow GitHub holds a call up to the client's timeout,
    and this one hangs on purpose, to ask what ELSE the announcement is holding while it
    waits."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("comment", body))
        self.started.set()
        await self.release.wait()


class _SpentRateLimit(_ArchivedRepo):
    """The connection's token spent its hourly GitHub limit: every write refuses until reset."""

    async def set_labels(self, issue_number: int, labels: Any) -> None:
        self.calls.append(("set_labels", frozenset(labels)))
        raise RateLimited(datetime.now(UTC) + timedelta(hours=1))


class _ArchivedRepoWithALongExcuse(_ArchivedRepo):
    """A githubkit error carries the whole response body; the event store must not."""

    async def set_labels(self, issue_number: int, labels: Any) -> None:
        self.calls.append(("set_labels", frozenset(labels)))
        raise _FakeRequestFailed("403 (archived). " * 500)


def _archive_the_repo[P: _ArchivedRepo](world: World, projector_cls: type[P]) -> tuple[World, P]:
    """Substitute the projector through the project's GitHub, the seam `_build_world` fills.

    Scoped to this world's repo, which is unique to it: `loop.tick` leases every reconcilable
    ticket in the shared test database, and neither this projector's call list nor this world's
    fake repo may take a foreign ticket's projection. The others get their own instance over a
    copy of the repo.
    """
    archived = projector_cls(github=world.github)
    elsewhere = projector_cls(github=replace(world.github))

    def choose(repo: str) -> Projector:
        return archived if repo == world.repo else elsewhere

    github = replace(world.project_github, projector=choose)
    poisoned = replace(
        world, deps=with_github(world.deps, github), project_github=github, projector=archived
    )
    return poisoned, archived


async def _press_exit(world: World) -> None:
    """What the dashboard route records; the loop's half is what this module exercises."""
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.EXIT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            requested_via="dashboard",
        )
        await session.commit()


async def _hand_the_ticket_back(world: World) -> None:
    """`run_tick` leaves its own 300s lease held, which `loop.tick` could not then claim."""
    async with world.sessions() as session:
        await release(session, world.ticket_id, _LEASE_OWNER)


async def _spare_the_bystanders(world: World, *, and_ours: bool = False) -> None:
    """Un-count every ticket but this world's.

    `loop.tick` leases every reconcilable ticket in this shared, never-truncated database, and
    the archived projector raises for all of them — so a module about ONE poisoned ticket would
    otherwise count, and eventually quarantine, every bystander a previous test left behind.
    Their reconciles rolled back anyway; this puts back the one thing that now survives one.

    `and_ours` widens the sweep to this world's own ticket, for the exit half of the fixture
    below: on the way out there is nothing left to spare.
    """
    async with world.sessions() as session:
        statement = update(Ticket).values(consecutive_failures=0, quarantined_at=None)
        if not and_ours:
            statement = statement.where(Ticket.id != world.ticket_id)
        await session.execute(statement)


async def _hold_the_bystanders(world: World) -> None:
    """Lease every other ticket to `_HOLD`, so `loop.tick` reconciles only this world's.

    Without it every tick here reconciled each bystander a previous module left, and this module
    took four times as long in the suite as alone. A ticket made after the hold stays leasable.
    """
    async with world.sessions() as session:
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id != world.ticket_id,
                or_(Ticket.lease_owner.is_(None), Ticket.lease_expires_at < func.now()),
            )
            .values(lease_owner=_HOLD, lease_expires_at=text("now() + interval '1 hour'"))
        )


async def _release_the_bystanders(world: World) -> None:
    async with world.sessions() as session:
        await session.execute(
            update(Ticket)
            .where(Ticket.lease_owner == _HOLD)
            .values(lease_owner=None, lease_expires_at=None)
        )


@pytest.fixture(autouse=True)
async def _spare_the_bystanders_around_each_test(world: World) -> AsyncIterator[None]:
    """The same guard on both sides of every test here.

    Before, because `_spare_the_bystanders` otherwise only runs between ticks inside `_one_tick`:
    entered with a foreign ticket already at `limit - 1` — reachable whenever `test_reconcile.py`
    runs first (`--lf`, `-k`, an explicit file list) — this module's own first tick would
    quarantine it and append an event the append-only log then keeps for ever.

    After, because the counts this module leaves on the table are what a LATER module's first
    failing tick inherits, in a database no test truncates.
    """
    await _spare_the_bystanders(world)
    await _hold_the_bystanders(world)
    try:
        yield
    finally:
        await _release_the_bystanders(world)
        await _spare_the_bystanders(world, and_ours=True)


async def _leasable(world: World) -> bool:
    """`acquire_leases`' own answer, rolled back — claiming for real would take every other
    ticket in this shared database with it. The limit is deliberately larger than the whole
    table: the claim is `ORDER BY id LIMIT`, and this world's ticket is the newest, so a small
    limit would answer "not leasable" for the wrong reason."""
    async with world.sessions() as session:
        claimed = await acquire_leases(session, "quarantine-probe", 10_000, _TTL)
        leasable = world.ticket_id in {ticket.id for ticket in claimed}
        await session.rollback()
        return leasable


async def _one_tick(world: World, deps: Any = None) -> None:
    """One `loop.tick` over the whole database, then `_spare_the_bystanders`."""
    await tick(
        world.deps if deps is None else deps,
        sessions=world.sessions,
        settings=_settings(),
        owner=_OWNER,
        limit=50,
        ttl=_TTL,
    )
    await _spare_the_bystanders(world)


async def _poison[P: _ArchivedRepo](
    world: World, projector_cls: type[P]
) -> tuple[World, P, StepRun]:
    """A ticket with a live run, an exit pressed, and a repo that 403s every write — so every
    tick from here on cancels the run, fails on the label write, and rolls back."""
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    await _hand_the_ticket_back(world)
    await _press_exit(world)
    return (*_archive_the_repo(world, projector_cls), run)


async def _quarantine_events(world: World) -> list[Any]:
    return [row for row in await event_rows(world) if row.type == "ticket_quarantined"]


async def test_a_failing_tick_is_counted_and_changes_nothing_else(world: World) -> None:
    """Under the threshold: the counter rises, the ticket is otherwise untouched, and the loop
    keeps trying. A transient failure must cost a ticket nothing."""
    poisoned, archived, _ = await _poison(world, _ArchivedRepo)
    events_before = len(await event_rows(poisoned))

    for failures in range(1, _AFTER):
        await _one_tick(poisoned)

        ticket = await ticket_row(poisoned)
        assert ticket.consecutive_failures == failures
        assert ticket.quarantined_at is None
        assert ticket.status == TicketStatus.RUNNING, "the exit never commits"
        assert ticket.closed_at is None
        assert ticket.current_step == "plan"
        (row,) = await step_runs(poisoned)
        assert row.status == StepRunStatus.RUNNING, "the cancellation rolls back with it"

        assert archived.calls == [archived.calls[0]] * failures, "the same write, once per tick"
        assert len(await event_rows(poisoned)) == events_before, "counting is not a transition"
        assert ticket.lease_owner is None
        assert await _leasable(poisoned), "under the threshold the loop keeps trying"


async def test_the_threshold_quarantines_the_ticket_and_ends_the_leasing(world: World) -> None:
    """At the threshold: stamped, evented, commented, and out of `acquire_leases`."""
    poisoned, archived, _ = await _poison(world, _ArchivedRepo)

    for _ in range(_AFTER):
        await _one_tick(poisoned)

    ticket = await ticket_row(poisoned)
    assert ticket.consecutive_failures == _AFTER
    assert ticket.quarantined_at is not None
    assert not await _leasable(poisoned), "quarantine, not status, is what ends the retry"
    assert ticket.status == TicketStatus.RUNNING, "the state machine is left where it stood"

    (quarantined,) = await _quarantine_events(poisoned)
    assert quarantined.details["failures"] == _AFTER
    assert "403 (archived)" in quarantined.details["error"]
    assert quarantined.step_run_id is None, "the tick died before it knew which step it was on"

    (body,) = [text for verb, text in archived.calls if verb == "comment"]
    assert str(_AFTER) in body
    assert "403 (archived)" in body
    assert "requeue" in body.lower()


async def test_a_repo_that_refuses_the_comment_is_still_quarantined(world: World) -> None:
    """The courtesy comment is best effort by construction. The motivating case is GitHub itself
    refusing the write, so a comment that 403s must not be able to decide the quarantine."""
    poisoned, archived, _ = await _poison(world, _ArchivedRepoRefusingComments)

    for _ in range(_AFTER):
        await _one_tick(poisoned)

    assert [verb for verb, _ in archived.calls].count("comment") == 1, "it was attempted"
    ticket = await ticket_row(poisoned)
    assert ticket.quarantined_at is not None
    assert len(await _quarantine_events(poisoned)) == 1


async def test_the_error_is_truncated_before_it_reaches_the_event_store(world: World) -> None:
    """`events.writer` rejects credential-shaped keys but caps no value's length, and an
    exception's text is untrusted input the loop did not compose."""
    poisoned, _, _ = await _poison(world, _ArchivedRepoWithALongExcuse)

    for _ in range(_AFTER):
        await _one_tick(poisoned)

    (quarantined,) = await _quarantine_events(poisoned)
    assert len(quarantined.details["error"]) == _MAX_ERROR_CHARS


async def test_a_quarantined_ticket_is_never_attempted_again(world: World) -> None:
    """Past the threshold: no write, no cancel, no second event, however many ticks pass."""
    poisoned, archived, run = await _poison(world, _ArchivedRepo)
    for _ in range(_AFTER):
        await _one_tick(poisoned)

    writes = len(archived.calls)
    cancels = poisoned.adapter.cancelled.count(run.runtime_run_id or "")
    events = len(await event_rows(poisoned))

    for _ in range(3):
        await _one_tick(poisoned)

    assert len(archived.calls) == writes, "nothing is attempted again"
    assert poisoned.adapter.cancelled.count(run.runtime_run_id or "") == cancels, (
        "a poisoned exit re-cancelled the substrate on every tick; quarantine stops that too"
    )
    assert len(await event_rows(poisoned)) == events, "one quarantine, however many ticks pass"
    assert (await ticket_row(poisoned)).consecutive_failures == _AFTER


async def test_the_courtesy_comment_does_not_hold_the_ticket_row_locked(
    world: World, maker: async_sessionmaker[AsyncSession]
) -> None:
    """`_record_failure` takes `SELECT … FOR UPDATE` on the ticket, and githubkit is built with
    no timeout. Announcing inside that transaction would hold the row for as long as GitHub
    hangs — and `POST /requeue` is exactly what an operator reaches for at that moment, so its
    UPDATE would queue behind the hang. `acquire_leases` is `SKIP LOCKED` and survives this;
    the requeue does not.
    """
    poisoned, archived, _ = await _poison(world, _ArchivedRepoThatHangsOnTheComment)

    async def to_quarantine() -> None:
        for _ in range(_AFTER):
            await _one_tick(poisoned)

    ticking = asyncio.create_task(to_quarantine())
    started = asyncio.create_task(archived.started.wait())
    try:
        # No wall-clock budget: a slow runner only delays the comment. The ticks can only finish
        # without reaching it, so this wait always returns.
        await asyncio.wait({started, ticking}, return_when=asyncio.FIRST_COMPLETED)
        assert archived.started.is_set(), "the quarantine reached its courtesy comment"
        # The requeue route's own write, under a lock timeout so a held row fails fast rather
        # than hanging this test for as long as the comment does.
        async with maker() as probe:
            await probe.execute(text("SET LOCAL lock_timeout = '2s'"))
            await probe.execute(
                update(Ticket)
                .where(Ticket.id == poisoned.ticket_id)
                .values(quarantined_at=None, consecutive_failures=0)
            )
            await probe.commit()
    finally:
        started.cancel()
        archived.release.set()
        await ticking

    assert (await ticket_row(poisoned)).quarantined_at is None, "the requeue landed"
    (quarantined,) = await _quarantine_events(poisoned)
    assert quarantined.details["failures"] == _AFTER, "and the record of it was committed first"


async def test_a_tick_that_does_not_raise_clears_the_counter(world: World) -> None:
    """One failure then a healthy tick: the count is CONSECUTIVE failures, not a total."""
    poisoned, _, _ = await _poison(world, _ArchivedRepo)
    await _one_tick(poisoned)
    assert (await ticket_row(poisoned)).consecutive_failures == 1

    # `world.deps` is the un-archived original — the same tick, against a repo that answers.
    await _one_tick(world, world.deps)

    ticket = await ticket_row(world)
    assert ticket.consecutive_failures == 0
    assert ticket.quarantined_at is None
    assert ticket.status == TicketStatus.EXITED, "the tick it could not finish now finishes"


async def test_clearing_the_counter_writes_only_when_there_is_something_to_clear(
    world: World,
) -> None:
    """A healthy fleet is the common case: the reset must not add an UPDATE per ticket per tick
    just to re-assert a zero."""
    async with world.sessions() as session:
        healthy = await session.get(Ticket, world.ticket_id)
        assert healthy is not None and healthy.consecutive_failures == 0
        _clear_failures(healthy)
        assert healthy not in session.dirty

    async with world.sessions() as session:
        failing = await session.get(Ticket, world.ticket_id)
        assert failing is not None
        failing.consecutive_failures = 2

    async with world.sessions() as session:
        recovered = await session.get(Ticket, world.ticket_id)
        assert recovered is not None
        _clear_failures(recovered)
        assert recovered in session.dirty
        assert recovered.consecutive_failures == 0


async def test_record_failure_locks_the_row_so_two_overlapping_runners_do_not_double_count(
    world: World, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Reachable when a tick outlives `worker.lease_seconds` and another runner reclaims the
    ticket: two `one()` error handlers can then call `_record_failure` for the same ticket at
    once, each in its own session. `with_for_update=True` is what keeps them from both reading
    `limit - 1`: the second call's own SELECT waits for the first's transaction, so it reads the
    ALREADY-quarantined row and returns instead of quarantining a second time.

    Staged rather than `asyncio.gather`ed: the first call runs to completion, uncommitted, before
    the second even starts, so the second is guaranteed to arrive at the lock the first is
    already holding — a `gather` of both would deadlock, since neither commits until this test
    does.
    """
    exc = RuntimeError("boom")
    session_a = maker()
    session_b = maker()
    try:
        await _record_failure(session_a, world.ticket_id, exc, deps=world.deps, limit=1)

        task_b = asyncio.create_task(
            _record_failure(session_b, world.ticket_id, exc, deps=world.deps, limit=1)
        )
        # Give task_b's own SELECT (and, without the fix, its later flush) a chance to reach
        # session_a's held lock before it is released.
        await asyncio.sleep(0.1)
        await session_a.commit()
        await task_b
        await session_b.commit()
    finally:
        await session_a.close()
        await session_b.close()

    events = [row for row in await event_rows(world) if row.type == "ticket_quarantined"]
    assert len(events) == 1, "exactly one event however many runners raced to write it"
    ticket = await ticket_row(world)
    assert ticket.consecutive_failures == 1


async def test_a_bystander_already_near_quarantine_survives_this_modules_first_tick(
    world: World, maker: async_sessionmaker[AsyncSession]
) -> None:
    """What `_spare_the_bystanders_around_each_test` prevents: a ticket a PRIOR test module left one
    short of quarantine (`test_reconcile.py` first, via `--lf`, `-k`, or an explicit file list)
    would otherwise be pushed over by this module's own first tick — `world.deps.state_source`
    simply not recognising a foreign ticket's repo is enough to raise on it every tick, no
    archived projector required.

    Simulated in one test rather than two: the sweep must run BEFORE the foreign ticket's
    problem tick, and a real prior module's ticket cannot be planted before pytest has already
    run this module's own entry fixture for THIS test — so the explicit call below stands in for
    what that fixture does for every other test in this module, at the same point in the
    sequence: before any tick.
    """
    other = await _build_world(maker)
    async with other.sessions() as session:
        ticket = await session.get(Ticket, other.ticket_id)
        assert ticket is not None
        ticket.consecutive_failures = _AFTER - 1

    await _spare_the_bystanders(world)

    await tick(
        world.deps,
        sessions=world.sessions,
        settings=_settings(),
        owner=_OWNER,
        limit=50,
        ttl=_TTL,
    )

    async with other.sessions() as session:
        reloaded = await session.get(Ticket, other.ticket_id)
        assert reloaded is not None
        assert reloaded.consecutive_failures == 1, "one tick's worth, not the two it walked in with"
        assert reloaded.quarantined_at is None
        foreign_quarantines = (
            (
                await session.execute(
                    select(Event).where(
                        Event.ticket_id == other.ticket_id, Event.type == "ticket_quarantined"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert not foreign_quarantines


async def test_a_substrate_outage_is_said_once_and_never_quarantines(world: World) -> None:
    """E2B down for longer than the threshold: the ticket keeps being tried, nothing counts
    toward quarantine, and the outage is one event, not one per tick."""
    world.adapter.fail_spawn(SubstrateUnavailable("E2B could not create a sandbox: 503"))

    for _ in range(_AFTER + 2):
        await _one_tick(world)

    ticket = await ticket_row(world)
    assert ticket.quarantined_at is None and ticket.consecutive_failures == 0
    outages = [e for e in await event_rows(world) if e.type == EventType.SUBSTRATE_UNAVAILABLE]
    assert len(outages) == 1 and "503" in outages[0].details["error"]
    assert await step_runs(world) == [], "a spawn that never reached the substrate leaves no run"

    world.adapter.fail_spawn(None)  # type: ignore[arg-type]
    await _one_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING


async def test_a_spent_rate_limit_never_quarantines(world: World) -> None:
    """The limit is the connection's, shared by every ticket on it. Counted, one tenant's spent
    token would quarantine all of them inside an hour."""
    poisoned, archived, _ = await _poison(world, _SpentRateLimit)

    for _ in range(_AFTER + 2):
        await _one_tick(poisoned)

    ticket = await ticket_row(poisoned)
    assert ticket.quarantined_at is None and ticket.consecutive_failures == 0
    assert len(archived.calls) == _AFTER + 2
    assert await _leasable(poisoned)
