"""The refresh route, and the wake every dashboard request now sends.

Refresh asks the worker for one pass over one ticket and waits for it (tech-stack decision 4). The
API only asks and waits: a stand-in worker here claims and releases the ticket the way a real
refresh pass would, and the worker's half is `test_refresh.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api import wakes
from orchestrator.db.models import Event, StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.worker import leases
from tests.api_harness import (
    WEBHOOK_SECRET,
    build_app,
    client_for,
    make_settings,
    seed_connected,
    seed_lane_head,
    seed_project,
    seed_ticket,
)
from tests.wake_probe import listening, notify

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _stand_in_pass(
    received: asyncio.Queue[str], maker: async_sessionmaker[AsyncSession]
) -> None:
    """What a worker's refresh pass does to the lease, without the reconcile: claim the ticket
    the wake names, after the wake's time, then release it."""
    wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))
    async with maker() as session:
        claimed = await leases.acquire_lease(
            session,
            wake["ticket_id"],
            "stand-in/refresh",
            timedelta(seconds=60),
            requested_at=datetime.fromisoformat(wake["requested_at"]),
        )
        assert claimed is not None
        await session.commit()
    async with maker() as session:
        assert await leases.release(session, wake["ticket_id"], "stand-in/refresh")
        await session.commit()


async def _events(session: AsyncSession, ticket_id: int) -> list[Event]:
    return list(
        (await session.execute(select(Event).where(Event.ticket_id == ticket_id))).scalars().all()
    )


async def _next_wake(received: asyncio.Queue[str], database_url: str) -> str:
    """The first notification after whatever the test just did. The sentinel sent last means
    "nothing" arrives as the sentinel, instead of as a wait that proves nothing."""
    await notify(database_url, "sentinel")
    return await asyncio.wait_for(received.get(), timeout=5)


async def test_refreshing_an_unknown_ticket_is_404(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post("/api/tickets/999999999/refresh", headers=auth)
    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found."


@pytest.mark.parametrize(
    ("state", "code"),
    [("done", "ticket_done"), ("quarantined", "quarantined"), ("failing", "failing")],
)
async def test_refresh_is_refused_where_a_pass_would_not_answer_it(
    async_session: AsyncSession,
    auth: dict[str, str],
    migrated_database: str,
    state: str,
    code: str,
) -> None:
    """Done and quarantined tickets are never leased. A failing one would be, but a pass there
    counts toward quarantine (decision 31), and clicking must not be a way to spend that."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    if state == "done":
        ticket.status = TicketStatus.DONE
    elif state == "quarantined":
        ticket.quarantined_at = datetime.now(UTC)
    else:
        ticket.consecutive_failures = 2
    await async_session.flush()

    async with listening(migrated_database) as received:
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        assert await _next_wake(received, migrated_database) == "sentinel"

    assert response.status_code == 409
    assert response.json()["code"] == code
    if state == "failing":
        assert response.json()["detail"] == (
            "The last 2 checks of this ticket failed. It is checked again automatically."
        )


async def test_with_no_worker_the_refresh_gives_up_with_202_and_writes_nothing(
    async_session: AsyncSession, auth: dict[str, str], migrated_database: str
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    before = (ticket.current_step, ticket.status, ticket.lease_owner, ticket.last_leased_at)

    async with listening(migrated_database) as received:
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))

    assert response.status_code == 202
    body = response.json()
    assert body["landed"] is False
    assert body["ticket"]["id"] == ticket.id
    assert wake == {"ticket_id": ticket.id, "requested_at": wake["requested_at"]}
    assert datetime.fromisoformat(wake["requested_at"]) == datetime.fromisoformat(
        body["requested_at"]
    )
    # A wake, not a write: no event, and no ticket column moved.
    assert await _events(async_session, ticket.id) == []
    reloaded = (
        await async_session.execute(select(Ticket).where(Ticket.id == ticket.id))
    ).scalar_one()
    await async_session.refresh(reloaded)
    after = (reloaded.current_step, reloaded.status, reloaded.lease_owner, reloaded.last_leased_at)
    assert after == before


async def test_refresh_answers_200_once_a_pass_after_the_request_has_released_the_ticket(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Room for the stand-in on a slow runner; the route answers as soon as the pass lands.
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 5.0)
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with listening(migrated_database) as received:
        worker = asyncio.create_task(_stand_in_pass(received, maker))
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        await asyncio.wait_for(worker, timeout=5)

    assert response.status_code == 200
    body = response.json()
    assert body["landed"] is True
    assert body["ticket"]["id"] == ticket.id


async def test_refresh_stops_waiting_once_no_pass_will_come(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet pass that finished the ticket as the click landed: nothing will lease it again,
    so the request has nothing left to wait for."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 10.0)
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with listening(migrated_database) as received:

        async def fleet_finishes_it() -> None:
            wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))
            async with maker() as session:
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == wake["ticket_id"])
                    .values(status=TicketStatus.DONE)
                )
                await session.commit()

        fleet = asyncio.create_task(fleet_finishes_it())
        started = asyncio.get_running_loop().time()
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        waited = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(fleet, timeout=5)

    assert response.status_code == 202
    assert response.json()["ticket"]["status"] == TicketStatus.DONE
    assert waited < 5


async def test_a_pass_that_finished_the_ticket_has_landed_before_it_releases(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass commits `done` (or a quarantine) and only then releases, in its own transaction —
    after a GitHub comment, for a quarantine. Seen in between, the ticket is gone AND this pass
    claimed it after the request: that pass is the answer, not a reason to give up."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 5.0)
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    finished = asyncio.Event()

    async with listening(migrated_database) as received:

        async def pass_that_finishes_it() -> None:
            wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))
            async with maker() as session:
                assert await leases.acquire_lease(
                    session,
                    wake["ticket_id"],
                    "stand-in/refresh",
                    timedelta(seconds=60),
                    requested_at=datetime.fromisoformat(wake["requested_at"]),
                )
                await session.commit()
            async with maker() as session:
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == wake["ticket_id"])
                    .values(status=TicketStatus.DONE)
                )
                await session.commit()
            await finished.wait()
            async with maker() as session:
                await leases.release(session, wake["ticket_id"], "stand-in/refresh")
                await session.commit()

        worker = asyncio.create_task(pass_that_finishes_it())
        try:
            async with client_for(build_app(make_settings(), async_session)) as client:
                response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        finally:
            finished.set()
            await asyncio.wait_for(worker, timeout=5)

    assert response.status_code == 200
    assert response.json()["landed"] is True


async def test_a_fleet_claim_after_the_pass_does_not_hide_that_it_landed(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fleet tick claims every ticket up front and holds each until its turn. Claimed again
    right after the woken pass released it, the ticket is never seen free — but a newer claim
    than the one seen after the request means that one was released."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 5.0)
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    fleet_done = asyncio.Event()

    async with listening(migrated_database) as received:

        async def pass_then_fleet_claim() -> None:
            wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))
            async with maker() as session:
                assert await leases.acquire_lease(
                    session,
                    wake["ticket_id"],
                    "stand-in/refresh",
                    timedelta(seconds=60),
                    requested_at=datetime.fromisoformat(wake["requested_at"]),
                )
                await session.commit()
            await asyncio.sleep(0.3)  # long enough for the route to see this pass hold it
            # Released and re-claimed by the fleet in one transaction: never seen free.
            async with maker() as session:
                assert await leases.release(session, wake["ticket_id"], "stand-in/refresh")
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == wake["ticket_id"])
                    .values(
                        lease_owner="stand-in-fleet",
                        lease_expires_at=func.now() + timedelta(seconds=60),
                        last_leased_at=func.clock_timestamp(),
                    )
                )
                await session.commit()
            await fleet_done.wait()
            async with maker() as session:
                await leases.release(session, wake["ticket_id"], "stand-in-fleet")
                await session.commit()

        worker = asyncio.create_task(pass_then_fleet_claim())
        try:
            async with client_for(build_app(make_settings(), async_session)) as client:
                response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        finally:
            fleet_done.set()
            await asyncio.wait_for(worker, timeout=5)

    assert response.status_code == 200
    assert response.json()["landed"] is True


async def test_a_pass_still_holding_the_ticket_has_not_landed(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    """Claimed after the click but not released: its reconcile may still be mid-flight, so the
    page would show the state from before it."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)

    async with listening(migrated_database) as received:

        async def stand_in_worker() -> None:
            wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))
            async with maker() as session:
                assert await leases.acquire_lease(
                    session,
                    wake["ticket_id"],
                    "stand-in/refresh",
                    timedelta(seconds=60),
                    requested_at=datetime.fromisoformat(wake["requested_at"]),
                )
                await session.commit()

        worker = asyncio.create_task(stand_in_worker())
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(f"/api/tickets/{ticket.id}/refresh", headers=auth)
        await asyncio.wait_for(worker, timeout=5)

    assert response.status_code == 202
    assert response.json()["landed"] is False


# --------------------------------------------------------------------------- the other requests


async def _exit_request(session: AsyncSession) -> tuple[str, int]:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    return f"/api/tickets/{ticket.id}/exit", ticket.id


async def _reentry_request(session: AsyncSession) -> tuple[str, int]:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, status=TicketStatus.EXITED)
    return f"/api/tickets/{ticket.id}/reenter", ticket.id


async def _requeue_request(session: AsyncSession) -> tuple[str, int]:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    ticket.quarantined_at = datetime.now(UTC)
    ticket.consecutive_failures = 20
    await session.flush()
    return f"/api/tickets/{ticket.id}/requeue", ticket.id


async def _interrupt_request(session: AsyncSession) -> tuple[str, int]:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=StepRunStatus.RUNNING,
    )
    session.add(run)
    await session.flush()
    return f"/api/step-runs/{run.id}/interrupt", ticket.id


async def _restart_request(session: AsyncSession) -> tuple[str, int]:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    await seed_lane_head(session, ticket)
    return f"/api/tickets/{ticket.id}/restart", ticket.id


@pytest.mark.parametrize(
    "seed",
    [_exit_request, _reentry_request, _requeue_request, _interrupt_request, _restart_request],
)
async def test_every_dashboard_request_wakes_the_worker_for_its_ticket(
    async_session: AsyncSession, auth: dict[str, str], migrated_database: str, seed: object
) -> None:
    """Each of these used to wait for the next interval tick to be read."""
    path, ticket_id = await seed(async_session)  # type: ignore[operator]

    async with listening(migrated_database) as received:
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(path, headers=auth)
        wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))

    assert response.status_code == 202
    assert wake["ticket_id"] == ticket_id


@pytest.mark.usefixtures("master_key")
async def test_a_webhook_wakes_the_whole_fleet(
    async_session: AsyncSession, migrated_database: str
) -> None:
    """The one wake that names no ticket: the worker reads anything without a `ticket_id` as a
    fleet wake, so the webhook's payload must never grow one."""
    project = await seed_connected(async_session)
    body = json.dumps({"repository": {"full_name": project.github_repo}}).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    async with listening(migrated_database) as received:
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(
                f"/webhooks/github/projects/{project.id}",
                content=body,
                headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
            )
        wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))

    assert response.status_code == 202
    assert wake == {"project_id": project.id, "repo": project.github_repo}


async def test_the_dashboard_refresh_wakes_the_whole_fleet_and_records_nothing(
    async_session: AsyncSession, auth: dict[str, str], migrated_database: str
) -> None:
    """The project pages' refresh: the webhook's wake, so admission runs now. It must name no
    ticket, or the worker would read it as one ticket's refresh and never admit."""
    events_before = await async_session.scalar(select(func.count()).select_from(Event))

    async with listening(migrated_database) as received:
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post("/api/orgs/default/refresh", headers=auth)
        wake = json.loads(await asyncio.wait_for(received.get(), timeout=5))

    assert response.status_code == 202
    assert datetime.fromisoformat(response.json()["requested_at"]).tzinfo is not None
    assert wake == {}
    assert await async_session.scalar(select(func.count()).select_from(Event)) == events_before


@pytest.mark.parametrize(
    "seed",
    [_exit_request, _reentry_request, _requeue_request, _interrupt_request, _restart_request],
)
async def test_a_request_is_committed_before_its_wake(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    seed: object,
) -> None:
    """The wake's time is what a pass must claim after. Stamped before the request committed, a
    pass could claim after it and still not see the request."""
    path, ticket_id = await seed(async_session)  # type: ignore[operator]
    visible_at_wake: list[int] = []
    real_wake = wakes.wake_ticket

    async def checking_wake(session: AsyncSession, woken: int) -> datetime:
        async with maker() as other:
            count = await other.scalar(
                select(func.count()).select_from(Event).where(Event.ticket_id == woken)
            )
        visible_at_wake.append(int(count or 0))
        return await real_wake(session, woken)

    monkeypatch.setattr(wakes, "wake_ticket", checking_wake)
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(path, headers=auth)

    assert response.status_code == 202
    assert visible_at_wake == [1], "the request's event must be committed when the wake is sent"
    assert ticket_id


@pytest.mark.parametrize(
    "seed", [_exit_request, _reentry_request, _interrupt_request, _restart_request]
)
async def test_a_request_on_a_failing_ticket_waits_for_the_interval(
    async_session: AsyncSession,
    auth: dict[str, str],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    seed: object,
) -> None:
    """A pass there counts toward quarantine (decision 31), which is why refresh refuses such a
    ticket; the other requests are recorded but must not buy that pass either — and with no pass
    coming, they answer at once rather than wait for one."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 10.0)
    path, ticket_id = await seed(async_session)  # type: ignore[operator]
    ticket = (
        await async_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    ticket.consecutive_failures = 3
    await async_session.flush()

    clock = asyncio.get_running_loop()
    async with listening(migrated_database) as received:
        started = clock.time()
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(path, headers=auth)
        answered = clock.time() - started
        assert await _next_wake(received, migrated_database) == "sentinel"

    assert response.status_code == 202
    assert response.json()["landed"] is False
    assert answered < 2
    assert await _events(async_session, ticket_id) != []


@pytest.mark.parametrize(
    "seed",
    [_exit_request, _reentry_request, _requeue_request, _interrupt_request, _restart_request],
)
async def test_a_request_answers_once_its_pass_has_landed(
    async_session: AsyncSession,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    seed: object,
) -> None:
    """The page refetches the ticket when a request answers. Answered at the 202, the refetch
    read the ticket from before the pass: on the canary, exit landed 1.6 s after the click and
    the page showed it only on its next poll."""
    # Room for the stand-in on a slow runner; the route answers as soon as the pass lands.
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 5.0)
    path, _ = await seed(async_session)  # type: ignore[operator]

    async with listening(migrated_database) as received:
        worker = asyncio.create_task(_stand_in_pass(received, maker))
        async with client_for(build_app(make_settings(), async_session)) as client:
            response = await client.post(path, headers=auth)
        await asyncio.wait_for(worker, timeout=5)

    assert response.status_code == 202
    assert response.json()["landed"] is True
