"""Parked tickets read from the listing, not one by one (decision 30).

Every test counts `world.github.issue_reads`: a parked ticket served from its listing costs
zero, a ticket in transition costs exactly one, and a failed listing costs what it cost before
this decision — never a wrong transition.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from orchestrator.db.models import StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github import LABEL_BLOCKED, LABEL_EXIT, LABEL_INTERRUPTED, LABEL_WAITING
from orchestrator.worker.leases import release
from orchestrator.worker.loop import tick
from tests.reconcile_world import (
    _LEASE_OWNER,
    World,
    _escalate_the_plan_step,
    add_label,
    close_issue,
    event_types,
    exited_via_label,
    remove_label,
    request,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_an_exited_ticket_is_served_from_the_listing(world: World) -> None:
    """The whole point: parked ticks cost one listing, not one read per ticket."""
    await exited_via_label(world)
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)
    await run_tick(world, listings=True)

    assert world.github.issue_reads == reads_before, "a parked ticket never reads its issue"
    assert world.github.listing_calls == [(world.repo, LABEL_EXIT)] * 2
    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_a_removed_label_misses_the_listing_and_the_read_decides(world: World) -> None:
    """Absence from the listing is a transition, and the per-ticket read is what settles it —
    one read, once, then the ticket is no longer parked."""
    await exited_via_label(world)
    remove_label(world, LABEL_EXIT)
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert world.github.issue_reads == reads_before + 1
    assert EventType.TICKET_REENTERED in await event_types(world)


async def test_a_failed_listing_costs_a_read_and_changes_nothing(world: World) -> None:
    """None from the listing means "pay today's price": the ticket reads for itself and stays
    parked. A truncated or failed page must never look like a label that came off."""
    await exited_via_label(world)
    world.github.listing_unavailable = True
    reads_before = world.github.issue_reads
    events_before = len(await event_types(world))

    await run_tick(world, listings=True)

    assert world.github.issue_reads == reads_before + 1
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert len(await event_types(world)) == events_before


async def test_a_blocked_ticket_under_a_closed_issue_exits_after_one_confirming_read(
    world: World,
) -> None:
    """The ledger's own case: the `state=all` listing found the closed state beside the label,
    and the ticket's own read confirmed it before the exit — one call for the transition, where
    the steady park costs none."""
    await _escalate_the_plan_step(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    close_issue(world)
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert world.github.issue_reads == reads_before + 1
    assert world.github.listing_calls == [(world.repo, LABEL_BLOCKED)]


async def test_a_reopened_issue_is_not_exited_on_a_stale_listing(world: World) -> None:
    """The listing was taken earlier in the tick and says closed; the issue has been reopened
    since. The confirming read wins, the ticket stays blocked, and the gesture nobody made is
    not taken for them."""
    await _escalate_the_plan_step(world)
    world.github.listing_issue = replace(world.github.issue, state="closed")
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert world.github.issue_reads == reads_before + 1
    assert EventType.TICKET_EXITED not in await event_types(world)


async def test_a_re_entry_is_not_refused_on_a_stale_closed(world: World) -> None:
    """Same window, the other decision that reads state: a dashboard re-entry must not be
    refused — and the gesture consumed — because a listing from earlier in the tick still
    says closed."""
    await exited_via_label(world)
    world.github.listing_issue = replace(world.github.issue, state="closed")
    await request(world, EventType.REENTRY_REQUESTED)
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert world.github.issue_reads == reads_before + 1
    assert EventType.REENTRY_REFUSED not in await event_types(world)


async def test_a_write_reads_fresh_so_a_label_added_mid_tick_survives(world: World) -> None:
    """`_project` replaces the whole label set. The listing was taken earlier in the tick; a
    human has since added a label of their own and the exit label. The exit must be computed
    from the listing (fine) but written from a fresh read, or the human's label is deleted."""
    await _escalate_the_plan_step(world)
    stale = world.github.issue  # what the listing saw: blocked, no priority label
    add_label(world, "priority:high")
    add_label(world, LABEL_EXIT)
    world.github.listing_issue = replace(stale, labels=stale.labels | {LABEL_EXIT})

    await run_tick(world, listings=True)

    assert (await ticket_row(world)).status == TicketStatus.EXITED
    assert "priority:high" in world.github.issue.labels, "a human's label must survive the write"


async def test_a_closed_issue_that_exited_still_parks_at_zero_reads(world: World) -> None:
    """The confirming read is a transition's cost, not the park's: the exited early return sits
    above it and never reads state, however the ticket got there."""
    await running_run(world)
    close_issue(world)
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)
    await run_tick(world, listings=True)

    assert world.github.issue_reads == reads_before, "a parked ticket never reads its issue"


async def _interrupted(world: World) -> None:
    run = await running_run(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            requested_via="dashboard",
        )
        await session.commit()
    await run_tick(world)
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.INTERRUPTED


async def test_an_interrupted_ticket_is_found_in_the_second_listing(world: World) -> None:
    """The blocked listing misses, the interrupted one hits, and the ticket still reads nothing
    for itself."""
    await _interrupted(world)
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)

    assert world.github.issue_reads == reads_before
    assert world.github.listing_calls == [
        (world.repo, LABEL_BLOCKED),
        (world.repo, LABEL_INTERRUPTED),
    ]
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


@pytest.mark.parametrize("listings", [False, True])
async def test_a_ticket_parked_by_factorial_waiting_stays_parked(
    world: World, listings: bool
) -> None:
    """An idle park holds the ticket with `factorial:waiting` alone. A parking set or listing
    that misses it reads the ticket as released, and every idle-parked ticket resumes on the
    next tick; through the listing, a miss still parks but pays a read per ticket per tick."""
    await _interrupted(world)
    remove_label(world, LABEL_INTERRUPTED)
    add_label(world, LABEL_WAITING)
    reads_before = world.github.issue_reads
    writes_before = len(world.projector.calls)

    await run_tick(world, listings=listings)

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert world.adapter.resumed == []
    assert len(world.adapter.spawns) == 1
    assert len(world.projector.calls) == writes_before
    if listings:
        assert world.github.issue_reads == reads_before, "served from the waiting listing"


async def test_a_live_ticket_never_consults_a_listing(world: World) -> None:
    """Running and gated tickets read for themselves, as before: only parked statuses have
    parking labels."""
    reads_before = world.github.issue_reads

    await run_tick(world, listings=True)  # spawns attempt 1

    assert world.github.listing_calls == []
    assert world.github.issue_reads == reads_before + 1


async def test_the_tick_builds_the_cache_from_the_parked_source(world: World) -> None:
    """`loop.tick` is where the cache is born, one per tick; `parked=None` is today's loop."""
    await exited_via_label(world)
    reads_before = world.github.issue_reads

    # `run_tick` (above) never releases its own lease — one grant is meant to cover every
    # `run_tick` call in a test (reconcile_world.py's `_ensure_leased`). A real previous tick
    # would have released it on the way out, so free it here before proving `tick` itself can
    # claim and reconcile this ticket.
    async with world.sessions() as session:
        await release(session, world.ticket_id, _LEASE_OWNER)

    reconciled = await tick(
        world.deps,
        sessions=world.sessions,
        owner="runner-parked",
        limit=50,
        ttl=timedelta(seconds=60),
        parked=lambda _client: world.github,
    )

    # `>=`, not `==`: the session-scoped DB carries other tests' unleased tickets too
    # (test_reconcile.py's own tick() tests use the same convention, same reason). They belong
    # to repos `world.deps.state_source` does not know, so they fail fast in `snapshot()` and
    # never touch `world.github` — the two assertions below stay exact.
    assert reconciled >= 1
    assert world.github.issue_reads == reads_before
    assert world.github.listing_calls == [(world.repo, LABEL_EXIT)]
