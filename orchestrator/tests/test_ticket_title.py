"""`ticket.title` — the observer's one copy of issue text, and when it is refreshed.

The factory board and the project ticket table identify a ticket by its number alone unless this
column is populated, so what matters here is not that a title is stored once but WHERE in the tick
it is written: before the parked early-return (so a blocked ticket still tracks a rename) and after
the `done` one (so a finished ticket keeps what was last observed rather than losing it).

The third ordering rule has no test of its own here because one already exists:
`test_reconcile_actions.py::test_action_spawn_is_idempotent_under_concurrency`. Writing the title
beside the `source.issue()` read instead of at the tick's exits makes it fail — the early
`UPDATE ticket` locks the row, two concurrent ticks queue instead of racing, and the second runs
the step a second time. `_observe_title`'s docstring carries the reasoning.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import Project, Ticket, TicketSource, TicketStatus
from orchestrator.worker.admission import InMemoryCandidates, admit_project
from tests.reconcile_world import (
    _build_world,
    run_tick,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _rename(world: object, title: str) -> None:
    """A human renaming the issue on GitHub — an OBSERVED change, so it mutates the fake's state
    directly rather than going through the projector."""
    world.github.issue = replace(world.github.issue, title=title)  # type: ignore[attr-defined]


async def test_admission_stores_the_issue_title(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Admission already holds the `IssueState` it admitted on, so the first title costs nothing."""
    world = await _build_world(maker, admit=False)
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})

    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        admitted = await admit_project(session, project, source, candidates, **world.observed_by)
        assert len(admitted) == 1

    async with world.sessions() as session:
        ticket = (
            await session.execute(select(Ticket).where(Ticket.project_id == world.project_id))
        ).scalar_one()
    assert ticket.title == "Add a widget"


async def test_a_renamed_issue_updates_the_stored_title(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The loop re-reads the issue every tick anyway; the title tracks it rather than freezing at
    admission, so a renamed issue stops showing its old name on the board."""
    world = await _build_world(maker)

    await run_tick(world)
    assert (await ticket_row(world)).title == "Add a widget"

    _rename(world, "Add a better widget")
    await run_tick(world)
    assert (await ticket_row(world)).title == "Add a better widget"


async def test_a_parked_ticket_still_tracks_its_title(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`reconcile_ticket` returns early for a blocked ticket whose parking label is still on the
    issue. The title write has to sit ABOVE that return — a ticket waiting on a human is exactly
    the one somebody is most likely to rename while they work out what it needs."""
    world = await _build_world(maker, labels=frozenset({"adlc:new", "adlc:blocked"}))
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        ticket.status = TicketStatus.BLOCKED

    _rename(world, "Renamed while parked")
    await run_tick(world)

    parked = await ticket_row(world)
    assert parked.status == TicketStatus.BLOCKED  # still parked; the tick did no other work
    assert parked.title == "Renamed while parked"


async def test_a_done_ticket_keeps_its_last_seen_title(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A finished ticket never reads GitHub again, so it keeps whatever was observed last. That is
    what lets the board's Done lane carry titles for its seven-day window without a round trip."""
    world = await _build_world(maker)
    await run_tick(world)
    assert (await ticket_row(world)).title == "Add a widget"

    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        ticket.status = TicketStatus.DONE

    _rename(world, "Renamed after it finished")
    await run_tick(world)

    assert (await ticket_row(world)).title == "Add a widget"
