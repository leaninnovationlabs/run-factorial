"""Interrupting a run: the request the API records, and what the loop does with it.

The behaviours here are the ones that would regress silently. An interrupt that retried, or that
fell back to `cancel`, would both look like they worked — the run stops either way. What differs
is whether the workspace survives and whether the loop starts the work again ten minutes later,
and neither is visible without asking (decision 26).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github import LABEL_INTERRUPTED
from orchestrator.github.media import MediaTransient
from runtime.contract import RunStatus
from tests.reconcile_world import (
    BOT_LOGIN,
    FakeAdapter,
    World,
    _build_world,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _running_run(world: World) -> object:
    """Spawn, so there is a RUNNING row for a human to stop."""
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    return run


async def _request_interrupt(world: World, run: object) -> None:
    """What the API route records. Written here directly so this module tests the LOOP's half."""
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,  # type: ignore[attr-defined]
            requested_via="dashboard",
        )
        await session.commit()


async def test_an_interrupt_request_stops_the_run_and_parks_the_ticket(world: World) -> None:
    """The whole point: the substrate is told to stop, and the ticket waits for a human rather
    than for a backoff."""
    run = await _running_run(world)
    await _request_interrupt(world, run)

    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.INTERRUPTED
    assert world.adapter.interrupted == [row.runtime_run_id]
    assert not world.adapter.cancelled, "interrupt must not fall through to cancel"

    # BLOCKED is what keeps the retry path away from it and what `_grant_attempt` later releases.
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert EventType.AGENT_INTERRUPTED in await event_types(world)


async def test_an_interrupted_step_is_not_retried(world: World) -> None:
    """`INTERRUPTED` is deliberately absent from `_FAILED_STATUSES`. A human who stopped a run has
    not experienced a failure the step should recover from, and starting it again ten minutes
    later is the precise opposite of what they asked for."""
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)

    # Several more ticks: a retry, if one were owed, would fire on one of them.
    await run_tick(world)
    await run_tick(world)

    rows = await step_runs(world)
    assert len(rows) == 1, "an interrupted step must not start a second attempt"
    assert rows[0].status == StepRunStatus.INTERRUPTED


async def test_the_request_is_honoured_once_however_many_ticks_pass(world: World) -> None:
    """`pending` is the absence of the answering event, because an append-only log cannot mark a
    request done in place. Without that, every tick would re-interrupt an already-stopped run."""
    run = await _running_run(world)
    await _request_interrupt(world, run)

    await run_tick(world)
    await run_tick(world)
    await run_tick(world)

    assert len(world.adapter.interrupted) == 1


async def test_a_request_the_run_finished_ahead_of_is_answered_not_dropped(world: World) -> None:
    """The race a human cannot see: the agent reached the end inside the same tick gap the stop
    was pressed in. Nothing is left to interrupt, and an unanswered request reads on the page as
    one the loop ignored — while the ticket carries on through the steps after it."""
    run = await _running_run(world)
    await _request_interrupt(world, run)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)

    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.SUCCEEDED
    assert not world.adapter.interrupted, "a run that already ended has nothing to stop"
    assert "interrupt_overtaken" in await event_types(world)


async def test_an_interrupted_ticket_stays_parked_until_the_label_is_removed(
    world: World,
) -> None:
    """The gate that keeps a parked ticket parked keys on a LABEL being present, not on the
    ticket's status alone. An interrupt that parked with a label the gate does not recognise
    would release itself on the very next tick — the loop would see BLOCKED with the awaited
    label absent and read that as "a human already removed it".
    """
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED

    # Ticks while the human has NOT yet acted. The label is still on the issue.
    await run_tick(world)
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED, (
        "an interrupted ticket must wait for a human, not release itself"
    )
    assert EventType.UNBLOCKED not in await event_types(world)


async def test_a_parked_ticket_still_says_which_step_it_stopped_at(world: World) -> None:
    """`factorial:interrupted` is a STATUS label beside the step label, not a step label itself.

    Without an explicit flag the projector's only route is its generic `factorial:`-prefix branch,
    which reads "interrupted" as a step NAME and writes `factorial:interrupted` INSTEAD of
    `factorial:plan`.
    The label lands either way and the ticket stays parked either way, so nothing fails — it just
    stops answering the first question a human asks a parked ticket, which is where it stopped.
    """
    run = await _running_run(world)
    await _request_interrupt(world, run)

    await run_tick(world)

    labels = set(world.github.issue.labels)
    assert LABEL_INTERRUPTED in labels
    assert "factorial:plan" in labels, "the step label must survive alongside the status label"

async def test_the_apps_own_comments_are_not_read_back_as_steering(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A resume must not hand the agent the orchestrator's own parking comment.

    The cutoff is by TIME, so everything posted while the ticket sat parked qualifies — including
    the "run interrupted" notice the projector itself wrote. Fed back, the machine presents its
    own output to the agent as human instruction. The predicates already fence on `bot_login`
    for this reason (`issue_comment_contains`); steering needs the same fence.
    """
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)

    # After the interrupt — the cutoff is by time, so both qualify on that axis.
    after = "2099-01-01T00:00:00Z"
    world.github.add_issue_comment(BOT_LOGIN, "Run interrupted from the dashboard.", after)
    world.github.add_issue_comment("a-human", "Use ISO 8601, not a locale string.", after)
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )

    await run_tick(world)

    resumed = [e for e in await event_rows(world) if e.type == EventType.AGENT_RESUMED]
    assert resumed, "the ticket should have resumed once the parking label was gone"
    assert resumed[-1].details["steered_by"] == ["a-human"]


async def test_a_resumed_run_gets_its_timeout_from_the_resume(world: World) -> None:
    """A step timeout bounds working time, not time parked. Counted from the spawn, a ticket
    parked longer than the timeout was cancelled on its first poll after the resume — and on a
    substrate that keeps the workspace in the run, the cancel destroys it."""
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)

    world.clock.advance(timedelta(hours=2))  # parked, past the 1h step timeout
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )
    await run_tick(world)  # resumes
    await run_tick(world)  # polls the resumed run

    assert world.adapter.cancelled == [], "a resumed run must not inherit the parked hours"
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING

    world.clock.advance(timedelta(minutes=61))
    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.TIMEOUT, "the timeout still bounds the resumed segment"


async def test_each_pause_is_kept_out_of_the_runs_active_time(world: World) -> None:
    """A resume keeps `started_at` and clears `ended_at`, so the run's span counts every hour it
    sat parked. Each pause adds to `parked_seconds`: one that replaced the total would drop the
    earlier pauses from every duration on the dashboard, with no error."""
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)
    world.clock.advance(timedelta(hours=2))
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )
    await run_tick(world)

    world.clock.advance(timedelta(minutes=10))
    await _request_interrupt(world, run)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )
    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING
    assert row.parked_seconds == 2 * 3600 + 30 * 60


_AFTER = "2099-01-01T00:00:00Z"
_UUID = "11111111-2222-4333-8444-555555555555"


async def _parked_with_steering(world: World, body: str) -> None:
    run = await _running_run(world)
    await _request_interrupt(world, run)
    await run_tick(world)
    world.github.add_issue_comment("a-human", body, _AFTER)
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )


async def test_a_steering_comments_image_reaches_the_resumed_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    await _parked_with_steering(world, f'Match this.\n<img src="{world.media.image(_UUID)}" />')

    await run_tick(world)

    ((attachments, missing),) = world.adapter.resumed_attachments
    assert [a.name for a in attachments] == ["resume1-image-1"]
    assert attachments[0].source == "steering"
    assert missing == ()
    resumed = [e for e in await event_rows(world) if e.type == EventType.AGENT_RESUMED]
    assert resumed[-1].details["attachments"][0]["name"] == "resume1-image-1"


async def test_a_resume_with_no_images_passes_none(maker: async_sessionmaker[AsyncSession]) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    await _parked_with_steering(world, "Use ISO 8601.")

    await run_tick(world)

    assert world.adapter.resumed_attachments == [((), ())]
    assert world.media.calls == []


async def test_a_transient_image_failure_retries_the_release_not_reparks(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    await _parked_with_steering(world, f"Match ![x]({world.media.image(_UUID)})")
    world.media.error = MediaTransient("503")

    with pytest.raises(MediaTransient):
        await run_tick(world)
    assert world.adapter.resumed == []

    world.media.error = None
    await run_tick(world)
    assert len(world.adapter.resumed) == 1


class _CannotTakeImages(FakeAdapter):
    supports_attachments: ClassVar[bool] = False


async def test_a_substrate_that_cannot_take_images_reparks_and_says_why(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, adapter_cls=_CannotTakeImages, bot_login=BOT_LOGIN)
    await _parked_with_steering(world, f"Match ![x]({world.media.image(_UUID)})")

    await run_tick(world)

    assert world.adapter.resumed == []
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert any(
        kind == "comment" and "can't pass on the images in the comments" in body
        for kind, body in world.projector.calls
    )


async def test_a_second_resume_numbers_its_images_after_the_first(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    await _parked_with_steering(world, f"Match ![a]({world.media.image(_UUID)})")
    await run_tick(world)

    (run,) = await step_runs(world)
    await _request_interrupt(world, run)
    await run_tick(world)
    world.github.add_issue_comment(
        "a-human", f"Now this ![b]({world.media.image(_UUID[:-1] + '6')})", _AFTER
    )
    world.github.issue = replace(
        world.github.issue,
        labels=frozenset(world.github.issue.labels) - {LABEL_INTERRUPTED},
    )
    await run_tick(world)

    first, second = world.adapter.resumed_attachments
    assert all(a.name.startswith("resume1-image-") for a in first[0])
    names = [a.name for a in second[0]]
    assert "resume2-image-1" in names
    assert all(n.startswith("resume2-image-") for n in names)
