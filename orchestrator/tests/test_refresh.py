"""Refresh: the dashboard asks the worker to reconcile one ticket now (tech-stack decision 4).

The worker's half against real Postgres — the claim stamp, the one-ticket lease, the listener, and
the runner that serves a request. The route's half is `test_routes_refresh.py`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api import wakes
from orchestrator.db.models import Project, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import WAKE_LISTENER, wake_admission, wake_fleet, wake_ticket
from orchestrator.settings import Settings, WorkerSettings
from orchestrator.worker import leases, loop
from orchestrator.worker.loop import (
    ADMISSION_QUEUE_PER_PROJECT,
    _Admissions,
    _refresh,
    _Refreshes,
    _requested_admission,
    _requested_ticket,
    run_forever,
)
from runtime.contract import RunStatus
from tests.api_harness import build_app, client_for, make_settings
from tests.reconcile_world import (
    World,
    _build_world,
    _fail_observation,
    _grant_lease,
    step_runs,
    ticket_row,
)
from tests.wake_probe import listening, notify

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_TTL = timedelta(seconds=300)


async def _db_now(session: AsyncSession) -> datetime:
    return (await session.execute(select(func.statement_timestamp()))).scalar_one()


_UNUSED_URL = "postgresql+asyncpg://unused@localhost:1/unused"


def _settings(name: str, *, database_url: str = _UNUSED_URL) -> Settings:
    # A long interval: anything that happens inside a test's few seconds was not the interval tick.
    # No gap: a woken pass runs at once.
    return Settings(
        database_url=database_url,
        worker=WorkerSettings(
            poll_interval_seconds=60, min_pass_gap_seconds=0, name=name, batch_size=50
        ),
    )


async def _until(check: Callable[[], Awaitable[bool]], *, within: float = 5.0) -> None:
    """A bounded poll instead of a sleep-and-hope."""
    for _ in range(int(within / 0.05)):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _refresh_now(world: World, name: str, requested_at: datetime) -> None:
    await asyncio.wait_for(
        _refresh(
            world.ticket_id,
            requested_at,
            world.deps,
            scope=world.sessions,
            settings=_settings(name),
            slots=asyncio.Semaphore(1),
            stopping=asyncio.Event(),
        ),
        timeout=5,
    )


async def _claim_everything(session: AsyncSession, owner: str) -> list[int]:
    """What `loop.tick` does: claim until a claim comes back short. The database is shared by the
    whole session, so a single `LIMIT` claim may never reach this test's ticket."""
    claimed: list[int] = []
    while True:
        batch = await leases.acquire_leases(session, owner, 50, _TTL)
        claimed.extend(ticket.id for ticket in batch)
        if len(batch) < 50:
            return claimed


# --------------------------------------------------------------------------- the claim stamp


async def test_a_claim_stamps_last_leased_at_and_renew_and_release_leave_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The stamp is the landing signal a refresh waits on, so only a claim may move it: a renew
    or a release that moved it would read as a pass that started after the request."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        before = await _db_now(session)
    async with world.sessions() as session:
        claimed = await _claim_everything(session, "runner-stamp")
    assert world.ticket_id in claimed

    stamped = (await ticket_row(world)).last_leased_at
    assert stamped is not None
    assert stamped > before

    async with world.sessions() as session:
        assert await leases.renew(session, world.ticket_id, "runner-stamp", _TTL) is True
    async with world.sessions() as session:
        for ticket_id in claimed:
            await leases.release(session, ticket_id, "runner-stamp")
    assert (await ticket_row(world)).last_leased_at == stamped


async def test_a_long_exited_ticket_gets_a_fleet_pass_only_now_and_then_but_any_wake(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every closed issue comes to rest as EXITED, so passing them all every tick grew each pass
    with history. Re-entry still has its way back: a label removal or the dashboard wakes the
    ticket by id, and the fleet pass still looks now and then."""
    world = await _build_world(maker)
    hour_ago = text("now() - interval '1 hour'")

    async def settle(*, last_leased: str) -> None:
        async with world.sessions() as session:
            await session.execute(
                update(Ticket)
                .where(Ticket.id == world.ticket_id)
                .values(
                    status=TicketStatus.EXITED,
                    closed_at=hour_ago,
                    last_leased_at=text(f"now() - interval '{last_leased}'"),
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

    async def fleet_claims() -> bool:
        async with world.sessions() as session:
            claimed = await _claim_everything(session, "runner-exited")
        async with world.sessions() as session:
            for ticket_id in claimed:
                await leases.release(session, ticket_id, "runner-exited")
        return world.ticket_id in claimed

    await settle(last_leased="1 minute")
    assert not await fleet_claims()
    async with world.sessions() as session:
        requested_at = await _db_now(session)
    async with world.sessions() as session:
        assert await leases.acquire_lease(
            session, world.ticket_id, "runner-wake", _TTL, requested_at=requested_at
        )

    await settle(last_leased="11 minutes")
    assert await fleet_claims()


# --------------------------------------------------------------------------- one-ticket lease


async def test_acquire_lease_claims_that_ticket_and_no_other(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await _build_world(maker)
    bystander = await _build_world(maker)
    async with wanted.sessions() as session:
        requested_at = await _db_now(session)

    async with wanted.sessions() as session:
        claimed = await leases.acquire_lease(
            session, wanted.ticket_id, "runner-one", _TTL, requested_at=requested_at
        )

    assert claimed is not None
    assert claimed.id == wanted.ticket_id
    row = await ticket_row(wanted)
    assert row.lease_owner == "runner-one"
    assert row.last_leased_at is not None
    assert row.last_leased_at > requested_at
    assert (await ticket_row(bystander)).lease_owner is None


async def test_acquire_lease_declines_a_ticket_a_pass_claimed_after_the_request(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A fleet tick that reached the ticket after the click has already answered it; a second
    pass would read GitHub again for nothing."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        requested_at = await _db_now(session)
    async with world.sessions() as session:
        assert await leases.acquire_lease(
            session, world.ticket_id, "runner-fleet", _TTL, requested_at=requested_at
        )
    async with world.sessions() as session:
        assert await leases.release(session, world.ticket_id, "runner-fleet") is True

    async with world.sessions() as session:
        again = await leases.acquire_lease(
            session, world.ticket_id, "runner-refresh", _TTL, requested_at=requested_at
        )
    assert again is None

    # A request made after that pass claimed is a new question, and gets its own pass.
    async with world.sessions() as session:
        later = await _db_now(session)
    async with world.sessions() as session:
        assert await leases.acquire_lease(
            session, world.ticket_id, "runner-refresh", _TTL, requested_at=later
        )


@pytest.mark.parametrize("state", ["held", "done", "quarantined"])
async def test_acquire_lease_declines_what_acquire_leases_declines(
    maker: async_sessionmaker[AsyncSession], state: str
) -> None:
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        if state == "held":
            await _grant_lease(session, ticket, owner="someone-else", ttl=_TTL)
        elif state == "done":
            ticket.status = TicketStatus.DONE
        else:
            ticket.quarantined_at = datetime.now(UTC)
    async with world.sessions() as session:
        requested_at = await _db_now(session)

    async with world.sessions() as session:
        claimed = await leases.acquire_lease(
            session, world.ticket_id, "runner-refresh", _TTL, requested_at=requested_at
        )
    assert claimed is None


async def test_acquire_lease_reclaims_an_expired_lease(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    async with world.sessions() as session:
        await session.execute(
            update(Ticket)
            .where(Ticket.id == world.ticket_id)
            .values(lease_owner="died-mid-tick", lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    async with world.sessions() as session:
        requested_at = await _db_now(session)

    async with world.sessions() as session:
        claimed = await leases.acquire_lease(
            session, world.ticket_id, "runner-refresh", _TTL, requested_at=requested_at
        )
    assert claimed is not None


# --------------------------------------------------------------------------- the wake


async def test_wake_ticket_notifies_at_commit_with_the_ticket_and_the_request_time(
    maker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    world = await _build_world(maker)
    async with listening(migrated_database) as received:
        async with world.sessions() as session:
            requested_at = await wake_ticket(session, world.ticket_id)
            assert received.empty()  # queued on the transaction, not sent yet
        payload = await asyncio.wait_for(received.get(), timeout=5)

    assert json.loads(payload) == {
        "ticket_id": world.ticket_id,
        "requested_at": requested_at.isoformat(),
    }


async def test_wake_ticket_in_a_rolled_back_transaction_wakes_nothing(
    maker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """A route that refuses after queuing the wake must not start a pass it refused."""
    world = await _build_world(maker)
    async with listening(migrated_database) as received:
        session = maker()
        try:
            await wake_ticket(session, world.ticket_id)
            await session.rollback()
        finally:
            await session.close()
        await notify(migrated_database, "sentinel")
        assert await asyncio.wait_for(received.get(), timeout=5) == "sentinel"


# --------------------------------------------------------------------------- what the worker hears

_AT = "2026-09-13T10:00:00.123456+00:00"


async def test_a_ticket_request_names_the_ticket_and_its_database_time() -> None:
    payload = json.dumps({"ticket_id": 7, "requested_at": _AT})
    assert _requested_ticket(payload) == (7, datetime(2026, 9, 13, 10, 0, 0, 123456, tzinfo=UTC))


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"project_id": 1, "repo": "acme/app"}),  # the webhook's
        "not json",
        "[7]",
        json.dumps({"ticket_id": True, "requested_at": _AT}),
        json.dumps({"ticket_id": "7", "requested_at": _AT}),
        json.dumps({"ticket_id": 7}),
        json.dumps({"ticket_id": 7, "requested_at": "yesterday"}),
        json.dumps({"ticket_id": 7, "requested_at": "2026-09-13T10:00:00"}),
    ],
)
async def test_anything_else_is_a_fleet_wake(payload: str) -> None:
    """Every notification woke the fleet before a payload could name a ticket, so reading an
    unexpected one that way loses nothing."""
    assert _requested_ticket(payload) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (json.dumps({"admit": 3, "issue": "246"}), (3, 246)),
        (json.dumps({"admit": True, "issue": "246"}), None),
        (json.dumps({"admit": 3, "issue": 246}), None),
        (json.dumps({"admit": 3, "issue": "24a"}), None),
        (json.dumps({"admit": 3, "issue": "²"}), None),
        (json.dumps({"admit": 3, "issue": "1" * 13}), None),
        (json.dumps({"admit": 3}), None),
        (json.dumps({"project_id": 1, "repo": "acme/app"}), None),
        ("not json", None),
    ],
)
async def test_an_admission_request_names_a_project_and_an_issue(
    payload: str, expected: tuple[int, int] | None
) -> None:
    """Anything malformed is not an admission request, and so falls through to a fleet wake."""
    assert _requested_admission(payload) == expected


async def test_requests_for_an_issue_merge_and_a_flooded_project_is_capped() -> None:
    """A request past the cap says so: the listener then wakes the fleet, as every such
    delivery did before, rather than leave the issue to the poll interval."""
    admissions = _Admissions()
    assert admissions.add(3, 246)
    assert admissions.add(3, 246)
    queued = [admissions.add(4, n) for n in range(1000, 1000 + 2 * ADMISSION_QUEUE_PER_PROJECT)]
    assert list(admissions.pending[3]) == [246]
    assert len(admissions.pending[4]) == ADMISSION_QUEUE_PER_PROJECT
    assert queued == [True] * ADMISSION_QUEUE_PER_PROJECT + [False] * ADMISSION_QUEUE_PER_PROJECT
    assert admissions.arrived.is_set()


async def test_a_request_that_lands_mid_check_runs_again_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`opened` and `labeled` arrive a second apart. A check that read the issue before the
    label landed declines it, and if the `labeled` request were folded into that check the issue
    would wait for the fleet pass: the very delay #476 removes."""
    admissions = _Admissions()
    calls: list[tuple[int, int]] = []
    first_running = asyncio.Event()
    release_first = asyncio.Event()

    async def check(project_id: int, number: int, *_: Any, **__: Any) -> None:
        calls.append((project_id, number))
        if len(calls) == 1:
            first_running.set()
            await release_first.wait()

    monkeypatch.setattr(loop, "_admit_reported", check)
    stopping = asyncio.Event()
    serving = asyncio.create_task(
        loop._serve_admissions(
            cast(Any, None), admissions, _Refreshes(), stopping, scope=cast(Any, None)
        )
    )
    try:
        admissions.add(3, 246)
        await asyncio.wait_for(first_running.wait(), timeout=5)
        admissions.add(3, 246)
        release_first.set()

        async def checked_twice() -> bool:
            return len(calls) == 2

        await _until(checked_twice)
    finally:
        stopping.set()
        await asyncio.wait_for(serving, timeout=5)
    assert calls == [(3, 246), (3, 246)]


async def test_repeat_requests_for_a_ticket_keep_the_newest_time() -> None:
    early = datetime(2026, 9, 13, 10, tzinfo=UTC)
    late = datetime(2026, 9, 13, 11, tzinfo=UTC)
    refreshes = _Refreshes()
    refreshes.add(7, late)
    refreshes.add(7, early)
    assert refreshes.pending == {7: late}
    assert refreshes.arrived.is_set()


# --------------------------------------------------------------------------- one refresh pass


async def test_a_refresh_waits_out_a_lease_another_pass_holds(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet tick leases every ticket before it reconciles any, so a click mid-tick usually
    finds the ticket held — by a pass whose read may predate the click."""
    monkeypatch.setattr(loop, "_REFRESH_RETRY_SECONDS", 0.05)
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _grant_lease(session, ticket, owner="runner-fleet", ttl=_TTL)
    async with world.sessions() as session:
        requested_at = await _db_now(session)

    pending = asyncio.create_task(
        _refresh(
            world.ticket_id,
            requested_at,
            world.deps,
            scope=world.sessions,
            settings=_settings("runner-wait"),
            slots=asyncio.Semaphore(1),
            stopping=asyncio.Event(),
        )
    )
    await asyncio.sleep(0.3)
    assert not pending.done()
    assert await step_runs(world) == []

    async with world.sessions() as session:
        assert await leases.release(session, world.ticket_id, "runner-fleet") is True
    await asyncio.wait_for(pending, timeout=5)

    assert [run.step_name for run in await step_runs(world)] == ["plan"]
    assert (await ticket_row(world)).lease_owner is None


async def test_a_refresh_a_later_pass_already_answered_does_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    async with world.sessions() as session:
        requested_at = await _db_now(session)
    async with world.sessions() as session:
        assert await leases.acquire_lease(
            session, world.ticket_id, "runner-fleet", _TTL, requested_at=requested_at
        )
    async with world.sessions() as session:
        assert await leases.release(session, world.ticket_id, "runner-fleet") is True

    await _refresh_now(world, "runner-answered", requested_at)

    assert await step_runs(world) == []


async def test_a_refresh_taken_up_after_a_stop_claims_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """SIGTERM can land while a refresh waits for a slot; the fleet tick finishes only what it
    already holds, and a refresh must not start new work either."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        requested_at = await _db_now(session)
    stopping = asyncio.Event()
    stopping.set()

    await asyncio.wait_for(
        _refresh(
            world.ticket_id,
            requested_at,
            world.deps,
            scope=world.sessions,
            settings=_settings("runner-stopped"),
            slots=asyncio.Semaphore(1),
            stopping=stopping,
        ),
        timeout=5,
    )

    assert await step_runs(world) == []
    assert (await ticket_row(world)).last_leased_at is None


async def test_a_failed_attempt_does_not_strand_a_newer_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that arrives mid-pass waits for that pass's task; if the pass raises, the task
    must still take the newer request up rather than exit with it queued."""
    calls: list[datetime] = []
    first_started = asyncio.Event()
    fail_first = asyncio.Event()

    async def fake_refresh(_ticket_id: int, requested_at: datetime, *_: Any, **__: Any) -> None:
        calls.append(requested_at)
        if len(calls) == 1:
            first_started.set()
            await fail_first.wait()
            raise ConnectionError("dropped mid-pass")

    monkeypatch.setattr(loop, "_refresh", fake_refresh)
    refreshes = _Refreshes()
    stop = asyncio.Event()
    runner = asyncio.create_task(
        loop._serve_refreshes(
            cast(Any, None),
            refreshes,
            stop,
            scope=cast(Any, None),
            settings=_settings("runner-strand"),
        )
    )
    early = datetime(2026, 9, 13, 10, tzinfo=UTC)
    late = datetime(2026, 9, 13, 11, tzinfo=UTC)
    try:
        refreshes.add(7, early)
        await asyncio.wait_for(first_started.wait(), timeout=5)
        refreshes.add(7, late)
        # Let the dispatcher see the second request while the first is still in flight, so it
        # leaves it to the task already serving ticket 7.
        await asyncio.sleep(0.1)
        fail_first.set()

        async def both_served() -> bool:
            return len(calls) == 2

        await _until(both_served)
    finally:
        stop.set()
        await asyncio.wait_for(runner, timeout=5)
    assert calls == [early, late]


async def test_a_tick_whose_claim_fails_midway_releases_what_it_claimed(
    world: World, maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier batches are committed before a later claim raises. Left leased, they would sit out
    `lease_seconds` — and every refresh on them would wait and give up."""
    real = leases.acquire_leases
    claims = 0

    async def second_claim_fails(*args: Any, **kwargs: Any) -> Any:
        nonlocal claims
        claims += 1
        if claims == 1:
            return await real(*args, **kwargs)
        raise ConnectionError("dropped mid-claim")

    monkeypatch.setattr(loop, "acquire_leases", second_claim_fails)
    with pytest.raises(ConnectionError):
        await loop.tick(
            world.deps, sessions=world.sessions, owner="runner-claim-fails", limit=1, ttl=_TTL
        )

    async with maker() as session:
        stranded = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.lease_owner == "runner-claim-fails")
        )
    assert stranded == 0


async def test_a_refresh_that_raises_counts_toward_quarantine_and_releases(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The same per-ticket unit the tick runs: a refresh is not a way around decision 31."""
    world = await _build_world(maker)
    _fail_observation(world, "issue")
    async with world.sessions() as session:
        requested_at = await _db_now(session)

    await _refresh_now(world, "runner-raises", requested_at)

    row = await ticket_row(world)
    assert row.consecutive_failures == 1
    assert row.lease_owner is None


# --------------------------------------------------------------------------- end to end


async def _listener_up(maker: async_sessionmaker[AsyncSession]) -> bool:
    async with maker() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": WAKE_LISTENER},
        )
    return bool(count)


async def test_a_refresh_reconciles_its_ticket_without_a_fleet_tick(
    world: World,
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline: the run finishes on the fake substrate, a refresh names the ticket, and the
    ticket takes its next transition while the fleet's interval tick is a minute away."""
    ticks = 0
    real_tick = loop.tick

    async def counting_tick(*args: Any, **kwargs: Any) -> int:
        nonlocal ticks
        ticks += 1
        return await real_tick(*args, **kwargs)

    monkeypatch.setattr(loop, "tick", counting_tick)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=_settings("runner-refresh-e2e", database_url=migrated_database),
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:

        async def plan_spawned() -> bool:
            return [run.step_name for run in await step_runs(world)] == ["plan"]

        await _until(plan_spawned)  # the startup tick
        await _until(lambda: _listener_up(maker))

        world.github.deliver_spec(world.issue_number)
        world.adapter.complete(RunStatus.SUCCEEDED)
        async with world.sessions() as session:
            await wake_ticket(session, world.ticket_id)

        async def plan_accepted() -> bool:
            return [run.status for run in await step_runs(world)] == [StepRunStatus.SUCCEEDED]

        await _until(plan_accepted)
        assert ticks == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert (await ticket_row(world)).lease_owner is None


async def test_a_webhook_for_a_new_issue_admits_and_starts_it_inside_the_fleet_gap(
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#476. Production spaces fleet passes 30 s apart and polls every 30 s, so a fleet wake for
    a new issue saved only a pass's length. The issue's own wake must admit it and spawn its
    first step within seconds, with no second fleet tick."""
    world = await _build_world(maker, admit=False)
    async with world.sessions() as session:
        await session.execute(
            update(Project).where(Project.id == world.project_id).values(active=True)
        )
    ticks = 0
    real_tick = loop.tick

    async def counting_tick(*args: Any, **kwargs: Any) -> int:
        nonlocal ticks
        ticks += 1
        return await real_tick(*args, **kwargs)

    monkeypatch.setattr(loop, "tick", counting_tick)
    settings = Settings(
        database_url=migrated_database,
        worker=WorkerSettings(
            poll_interval_seconds=60, min_pass_gap_seconds=30, name="runner-admit", batch_size=50
        ),
    )
    stop = asyncio.Event()
    # No candidate source: the startup tick lists nothing, so only the wake can admit.
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=settings,
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:
        await _until(lambda: _listener_up(maker))
        await _until(lambda: _ticked(ticks))
        async with world.sessions() as session:
            await wake_admission(
                session, project_id=world.project_id, issue=str(world.issue_number)
            )

        async def plan_spawned() -> bool:
            async with world.sessions() as session:
                steps = (
                    await session.execute(
                        select(StepRun.step_name)
                        .join(Ticket, Ticket.id == StepRun.ticket_id)
                        .where(Ticket.project_id == world.project_id)
                    )
                ).scalars()
                return list(steps) == ["plan"]

        await _until(plan_spawned)
        assert ticks == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def _ticked(ticks: int) -> bool:
    return ticks >= 1


async def test_an_exit_from_the_dashboard_answers_once_the_worker_has_exited_the_ticket(
    world: World,
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    """Route and worker together, as the canary ran them: the request wakes the ticket, the
    worker's pass cancels the run and parks the ticket, and only then does the request answer —
    so the page's refetch reads `exited`, not the ticket from before the pass."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 10.0)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=_settings("runner-exit-e2e", database_url=migrated_database),
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:

        async def plan_running() -> bool:
            return [run.status for run in await step_runs(world)] == [StepRunStatus.RUNNING]

        await _until(plan_running)  # the startup tick
        await _until(lambda: _listener_up(maker))

        async with (
            maker() as session,
            client_for(build_app(make_settings(), session)) as client,
        ):
            response = await client.post(f"/api/tickets/{world.ticket_id}/exit", headers=auth)

        assert response.status_code == 202
        assert response.json() == {"status": "requested", "landed": True}
        assert (await ticket_row(world)).status == TicketStatus.EXITED
        assert [run.status for run in await step_runs(world)] == [StepRunStatus.CANCELLED]
        assert world.adapter.cancelled
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


# --------------------------------------------------------------------------- the listener


async def _listener_pids(maker: async_sessionmaker[AsyncSession]) -> set[int]:
    async with maker() as session:
        rows = await session.execute(
            text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": WAKE_LISTENER},
        )
    return {row.pid for row in rows}


async def test_a_killed_listener_reconnects_catches_up_and_serves_refreshes(
    world: World,
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Postgres restart, or anything else that drops the connection, used to end wakes until
    the worker restarted. The NOTIFYs sent while it was down are gone, so one fleet pass follows
    the reconnect."""
    monkeypatch.setattr(loop, "_LISTEN_RETRY_SECONDS", 0.05)
    ticks = 0
    real_tick = loop.tick

    async def counting_tick(*args: Any, **kwargs: Any) -> int:
        nonlocal ticks
        ticks += 1
        return await real_tick(*args, **kwargs)

    monkeypatch.setattr(loop, "tick", counting_tick)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=_settings("runner-reconnect", database_url=migrated_database),
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:

        async def plan_spawned() -> bool:
            return [run.step_name for run in await step_runs(world)] == ["plan"]

        await _until(plan_spawned)
        await _until(lambda: _listener_up(maker))
        (killed,) = await _listener_pids(maker)
        async with maker() as session:
            await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": killed})

        async def replaced() -> bool:
            pids = await _listener_pids(maker)
            return bool(pids) and killed not in pids

        await _until(replaced)

        async def caught_up() -> bool:
            return ticks == 2

        await _until(caught_up)

        world.github.deliver_spec(world.issue_number)
        world.adapter.complete(RunStatus.SUCCEEDED)
        async with world.sessions() as session:
            await wake_ticket(session, world.ticket_id)

        async def plan_accepted() -> bool:
            return [run.status for run in await step_runs(world)] == [StepRunStatus.SUCCEEDED]

        await _until(plan_accepted)
        assert ticks == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_a_listener_whose_connection_keeps_dropping_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `idle_session_timeout` below the poll interval, say. Each reconnect also wakes the
    fleet, so reconnecting at once would run fleet ticks back to back."""
    monkeypatch.setattr(loop, "_LISTEN_RETRY_SECONDS", 0.05)
    connects = 0

    class DropsAtOnce:
        def __init__(self) -> None:
            self._on_lost: Callable[[Any], None] = lambda _connection: None

        def add_termination_listener(self, callback: Callable[[Any], None]) -> None:
            self._on_lost = callback

        async def add_listener(self, *_: Any) -> None:
            return None

        async def execute(self, *_: Any) -> None:
            asyncio.get_running_loop().call_soon(self._on_lost, self)

        async def close(self) -> None:
            return None

        def is_closed(self) -> bool:
            return True

        def terminate(self) -> None:
            return None

    async def connect(*_: Any, **__: Any) -> DropsAtOnce:
        nonlocal connects
        connects += 1
        return DropsAtOnce()

    monkeypatch.setattr(asyncpg, "connect", connect)
    stop = asyncio.Event()
    listener = asyncio.create_task(
        loop._listen_for_wakeups(_settings("runner-flap"), asyncio.Event(), _Refreshes(), stop)
    )
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(listener, timeout=5)

    # 0.05 + 0.1 + 0.2 s of backoff fits about four connects into half a second.
    assert connects <= 6


async def test_a_burst_of_fleet_wakes_runs_at_most_one_pass_per_gap(
    world: World,
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every webhook delivery wakes the whole fleet, and each pass reads every ticket from GitHub:
    a burst run back to back can spend the App's rate limit and trip `quarantine_after`."""
    gap = 0.5
    # Timers may fire a clock resolution early.
    floor = gap - 0.01
    clock = asyncio.get_running_loop()
    starts: list[float] = []

    async def timed_tick(*args: Any, **kwargs: Any) -> int:
        starts.append(clock.time())
        return 0

    monkeypatch.setattr(loop, "tick", timed_tick)
    settings = Settings(
        database_url=migrated_database,
        worker=WorkerSettings(
            poll_interval_seconds=60, min_pass_gap_seconds=gap, name="runner-burst", batch_size=50
        ),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=settings,
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:
        await _until(lambda: _listener_up(maker))
        for _ in range(10):
            async with world.sessions() as session:
                await wake_fleet(session, repo=world.repo)
            await asyncio.sleep(0.02)
        await asyncio.sleep(3 * gap)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    between = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert len(starts) >= 2, "the burst woke nothing"
    assert all(seconds >= floor for seconds in between), between
