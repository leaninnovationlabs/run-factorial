"""An interactive run's life on the worker: an IDLE session is finished at most once when its
postconditions hold, held with `agent_idle` and `factorial:waiting` when they do not, and never
cancelled by the step deadline while it waits (decision 56).

Every failure here is silent. A second `finish` or a deadline cancel ends a session a human is
still answering, and a missing waiting label leaves a ticket that looks busy while it waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import request_resume
from orchestrator.api.services.errors import Conflict
from orchestrator.db.models import Event, StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.types import RequestedVia
from orchestrator.github import LABEL_EXIT, LABEL_INTERRUPTED, LABEL_WAITING, step_label
from orchestrator.github.state import CheckRunState
from orchestrator.worker.context import SessionPolicy
from orchestrator.worker.interrupts import _EXITED_OPENING, _EXITED_TURN
from orchestrator.worker.sessions import EXIT_GRACE
from runtime.contract import (
    AgentRuntimeError,
    IdleReason,
    ParkReason,
    ResumeFrom,
    RunHandle,
    RunStatus,
    RunUsage,
    SubstrateUnavailable,
    UnknownRunError,
)
from tests.api_harness import build_app, client_for, make_settings
from tests.reconcile_world import (
    BOT_LOGIN,
    FakeAdapter,
    World,
    _fail_observation,
    add_label,
    event_rows,
    event_types,
    interactive_world,
    remove_label,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

PLAN_LABEL = step_label("plan")


async def _details(world: World, event_type: EventType) -> list[dict[str, object]]:
    return [dict(row.details) for row in await event_rows(world) if row.type == event_type]


def _label_writes(world: World) -> list[frozenset[str]]:
    return [labels for kind, labels in world.projector.calls if kind == "set_labels"]


async def test_idle_with_postconditions_met_finishes_exactly_once_then_accepts(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    adapter.finish_settles = False

    for _ in range(2):
        world.clock.advance(timedelta(seconds=30))
        await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    adapter.finish_settles = True
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    types = await event_types(world)
    assert types.count(EventType.AGENT_FINISHING) == 1
    assert types.index(EventType.AGENT_FINISHING) < types.index(EventType.AGENT_COMPLETED)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_a_live_session_shows_tokens_and_an_estimated_cost_that_the_end_replaces(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Claude's CLI writes its cost only at exit, so a session's spend was invisible until its
    end. While it runs the row carries its tokens so far at list price, labelled an estimate;
    the end's measured figure replaces it."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    so_far = RunUsage(
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        cache_write_input_tokens=100_000,
        output_tokens=100_000,
    )
    adapter._session_with(usage=so_far)

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert (run.input_tokens, run.cached_input_tokens, run.output_tokens) == (
        1_000_000,
        400_000,
        100_000,
    )
    # claude-haiku-4-5 per million: 500k fresh x $1, 400k read x $0.10, 100k written x $1.25,
    # 100k out x $5.
    assert (run.cost_usd, run.cost_source) == (Decimal("1.165"), "estimated")

    adapter._session_with(usage=so_far.model_copy(update={"output_tokens": 200_000}))
    await run_tick(world)
    (run,) = await step_runs(world)
    assert (run.output_tokens, run.cost_usd) == (200_000, Decimal("1.665"))

    world.github.deliver_spec(world.issue_number)
    adapter.complete(
        RunStatus.SUCCEEDED,
        cost_usd=Decimal("1.5"),
        input_tokens=1_100_000,
        cached_input_tokens=400_000,
        output_tokens=210_000,
    )
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert (run.cost_usd, run.cost_source) == (Decimal("1.5"), "reported")
    assert (run.input_tokens, run.output_tokens) == (1_100_000, 210_000)


async def test_recent_writer_input_defers_finish(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A human typing into the terminal is still talking to the agent: met postconditions do not
    end the session under them. Once the grace passes, the finish lands and the run is accepted
    on the same tick."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    adapter.typed(at=world.clock.now - timedelta(seconds=30))

    await run_tick(world)

    assert adapter.finish_calls == []
    assert EventType.AGENT_FINISHING not in await event_types(world)

    world.clock.advance(timedelta(seconds=31))
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert EventType.AGENT_COMPLETED in await event_types(world)


async def test_a_finish_that_reads_back_running_is_not_deadline_cancelled(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Active 0-50, idle with a human typing until 65, finished at 66: the read after the finish
    is still the idle spell. Measured from the stretch before it, the deadline passed at 60."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)

    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    adapter.typed(at=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=15))
    adapter.typed(at=world.clock.now)
    await run_tick(world)
    assert adapter.finish_calls == []

    world.clock.advance(timedelta(minutes=1))
    adapter.finish_reports_running = True
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.cancelled == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.cancelled == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert await _details(world, EventType.AGENT_ACTIVE) == [
        {"step": "plan", "attempt": 1, "active_at": world.clock.now.isoformat()}
    ]


async def test_an_idle_status_whose_session_has_gone_active_is_read_as_running(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A turn that starts between `status()` and `session_state()`: the session is active, so
    nothing is finished or announced, and the idle spell it left ends."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Ship it?")
    await run_tick(world)

    world.github.deliver_spec(world.issue_number)
    adapter.go_active()
    adapter.status_value = RunStatus.IDLE
    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)

    assert adapter.finish_calls == []
    types = await event_types(world)
    assert types.count(EventType.AGENT_IDLE) == 1
    assert EventType.AGENT_FINISHING not in types
    assert types.count(EventType.AGENT_ACTIVE) == 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert LABEL_WAITING not in world.github.issue.labels


async def test_a_finish_that_never_lands_is_sent_once_more_and_no_more(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A spell being finished is not waiting on a human, whatever the postconditions say now; and
    a finish that never lands is re-sent once, `idle_park_after` later, then left alone."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    adapter.finish_settles = False
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]

    world.github.pulls = []
    park_after = world.deps.sessions.idle_park_after
    world.clock.advance(park_after - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    world.clock.advance(park_after - timedelta(seconds=1))
    await run_tick(world)

    assert adapter.finish_calls == ["run-1", "run-1"]
    finishing = await _details(world, EventType.AGENT_FINISHING)
    assert [row.get("retry") for row in finishing] == [None, True]
    assert EventType.AGENT_IDLE not in await event_types(world)
    assert LABEL_WAITING not in world.github.issue.labels


async def test_idle_with_postconditions_unmet_waits_and_says_so_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    idle_since = world.clock.now
    adapter.go_idle(IdleReason.QUESTION, since=idle_since, question="Postgres or SQLite?")

    for _ in range(3):
        world.clock.advance(timedelta(seconds=30))
        await run_tick(world)

    assert await _details(world, EventType.AGENT_IDLE) == [
        {
            "step": "plan",
            "attempt": 1,
            "reason": "question",
            "question": "Postgres or SQLite?",
            "idle_since": idle_since.isoformat(),
            "unmet": ['branch_exists("spec/12-*") = false'],
            "needs": ["a branch starting with spec/12-"],
        }
    ]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    # The only cover for the projection's waiting branch: the step label stays beside it.
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels
    assert sum(LABEL_WAITING in labels for labels in _label_writes(world)) == 1
    assert adapter.finish_calls == []
    assert adapter.cancelled == []
    assert adapter.interrupted == []


async def test_an_idle_run_is_never_deadline_cancelled(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(
        maker, sessions=SessionPolicy(terminal_enabled=True, idle_park_after=timedelta(hours=3))
    )
    await run_tick(world)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)

    world.clock.advance(timedelta(hours=2))
    await run_tick(world)

    assert adapter.cancelled == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE


async def test_going_active_after_idle_rebases_the_deadline_and_drops_waiting(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The step's 1h timeout counts the active stretch that began at 70 minutes, not the spawn:
    the idle 20 minutes are not working time."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)

    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    assert LABEL_WAITING in world.github.issue.labels

    world.clock.advance(timedelta(minutes=20))
    adapter.go_active()
    await run_tick(world)

    assert await _details(world, EventType.AGENT_ACTIVE) == [
        {"step": "plan", "attempt": 1, "active_at": world.clock.now.isoformat()}
    ]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert LABEL_WAITING not in world.github.issue.labels
    assert PLAN_LABEL in world.github.issue.labels

    world.clock.advance(timedelta(minutes=30))
    await run_tick(world)
    assert adapter.cancelled == []

    world.clock.advance(timedelta(minutes=31))
    await run_tick(world)
    assert adapter.cancelled == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.TIMEOUT
    assert (await event_types(world)).count(EventType.AGENT_ACTIVE) == 1


async def test_an_exit_cancels_an_idle_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An idle session still holds its credentials: left alone it would outlive the exit."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Ship it?")
    await run_tick(world)

    add_label(world, LABEL_EXIT)
    await run_tick(world)

    assert adapter.cancelled == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.CANCELLED
    assert (await ticket_row(world)).status == TicketStatus.EXITED


async def test_an_idle_run_can_be_interrupted(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Ship it?")
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    async with maker() as session, client_for(build_app(make_settings(), session)) as client:
        response = await client.post(f"/api/step-runs/{run.id}/interrupt", headers=auth)
    assert response.status_code == 202

    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.USER]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


_BEFORE = "2020-01-01T00:00:00Z"
_AFTER = "2099-01-01T00:00:00Z"
_QUESTION = "Postgres or SQLite?\n```\nnot a fence\n```"


async def _idle_parked(world: World, adapter: FakeAdapter) -> None:
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)


async def test_an_idle_session_parks_after_ten_minutes_with_one_comment_and_no_retry(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Parked, not failed: a retry would start the work again while the human is still deciding,
    and a second comment per tick would bury the question it exists to ask."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=10) - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    for _ in range(3):
        world.clock.advance(timedelta(hours=2))
        await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels
    assert LABEL_INTERRUPTED not in world.github.issue.labels
    (comment,) = _comments(world)
    assert f"````\n{_QUESTION}\n````" in comment
    assert await _details(world, EventType.AGENT_PARKED) == [
        {"step": "plan", "attempt": 1, "run_id": "run-1", "reason": "idle", "at_prompt": True}
    ]
    assert EventType.AGENT_INTERRUPTED not in await event_types(world)
    assert len(adapter.spawns) == 1


async def test_a_session_parked_on_a_failed_turn_says_why_rather_than_that_nothing_was_asked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rejected credential leaves the CLI at its prompt, and once parked nothing else on the
    ticket shows the error: the comment is where the human learns the key needs replacing."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    error = "Failed to authenticate. API Error: 401 Invalid bearer token"
    adapter.go_idle(IdleReason.TURN_ERROR, since=world.clock.now, turn_error=error)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    (comment,) = _comments(world)
    assert f"Its last reply failed:\n\n```\n{error}\n```" in comment
    assert "A person worked with this session" not in comment
    (idle,) = await _details(world, EventType.AGENT_IDLE)
    assert idle["turn_error"] == error


async def _parked_view(world: World, auth: dict[str, str]) -> dict[str, object]:
    (run,) = await step_runs(world)
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)
    assert response.status_code == 200
    return response.json()["parked"]


async def test_the_drawer_of_a_parked_session_carries_why_its_stretch_went_idle(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The failed turn and the postconditions still unmet are the stretch's own: a session
    resumed since, and parked again over a question, must not show the older stretch's error."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    error = "Failed to authenticate. API Error: 401 Invalid bearer token"
    adapter.go_idle(IdleReason.TURN_ERROR, since=world.clock.now, turn_error=error)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    parked = await _parked_view(world, auth)
    assert parked["turn_error"] == error
    assert parked["unmet"] == ['branch_exists("spec/12-*") = false']

    remove_label(world, LABEL_WAITING)
    await run_tick(world)
    adapter.go_active()
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    parked = await _parked_view(world, auth)
    assert parked["question"] == _QUESTION
    assert parked["turn_error"] is None


async def test_the_drawer_of_a_finishing_park_does_not_show_what_was_unmet_before_it(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The spell went idle with the spec missing, then the spec landed and the loop sent finish:
    that park is a finish nobody honoured, so "not accepted yet" would say the opposite."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    adapter.go_idle(IdleReason.TURN_ERROR, since=world.clock.now, turn_error="401")
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.finish_settles = False
    park_after = world.deps.sessions.idle_park_after
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    world.clock.advance(park_after)
    await run_tick(world)
    world.clock.advance(park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert EventType.AGENT_FINISHING in await event_types(world)
    parked = await _parked_view(world, auth)
    assert parked["turn_error"] is None
    assert parked["unmet"] is None


async def test_the_drawer_of_an_exit_park_carries_what_is_unmet(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """A quit mid-turn leaves no idle spell to read: the park's own record is the only place
    that says what the person who quit it has left to do."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _typed_and_quit(world, adapter, at_prompt=False)
    assert EventType.AGENT_IDLE not in await event_types(world)

    parked = await _parked_view(world, auth)
    assert parked["reason"] == "exited"
    assert parked["unmet"] == [_UNMET]
    assert parked["needs"] == [_NEED]


async def test_a_finish_that_never_lands_parks_once_its_resend_has_waited_too(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Without the park, a session that never honours `finish` holds its credentials at its
    prompt for ever, and nothing tells a human the ticket stopped moving."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    adapter.finish_settles = False
    await run_tick(world)
    park_after = world.deps.sessions.idle_park_after
    world.clock.advance(park_after)
    await run_tick(world)
    assert adapter.finish_calls == ["run-1", "run-1"]

    world.clock.advance(park_after - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert adapter.finish_calls == ["run-1", "run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert LABEL_WAITING in world.github.issue.labels
    assert len(_comments(world)) == 1
    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == ["idle"]


async def test_releasing_a_waiting_park_feeds_only_comments_posted_since_the_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The cutoff is the park. Without it the whole ticket history, the issue body's discussion
    included, is replayed to the agent as what the human just said."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    world.github.add_issue_comment("a-human", "An old remark about MySQL.", _BEFORE)
    await _idle_parked(world, adapter)
    world.github.add_issue_comment("a-human", "Postgres, please.", _AFTER)

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    ((run_id, turn),) = adapter.resumed
    assert run_id == "run-1"
    assert turn is not None
    assert "Postgres, please." in turn
    assert "MySQL" not in turn
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "label"
    assert resumed["comment_count"] == 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert PLAN_LABEL in world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels


async def test_a_waiting_park_released_with_no_comments_resumes_at_the_prompt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An interactive session comes back at its prompt with nothing typed: a canned "continue"
    would answer the agent's question for the human."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    assert adapter.resumed == [("run-1", None)]

    # Back at the prompt it parked at, it waits afresh rather than parking again at once.
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE, ParkReason.IDLE]
    assert sum("is paused" in body for body in _comments(world)) == 2


async def test_a_refused_waiting_resume_reparks_under_factorial_waiting(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Re-parked under `factorial:interrupted`, an idle park would read as a human's stop, and
    the dashboard would offer the wrong release."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    adapter.resume_error = AgentRuntimeError("the session is gone")

    remove_label(world, LABEL_WAITING)
    await run_tick(world)
    await run_tick(world)

    assert len(adapter.resumed) == 1
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels
    assert LABEL_INTERRUPTED not in world.github.issue.labels
    assert any("Couldn't resume" in body for body in _comments(world))


async def test_an_answer_left_while_the_session_waited_reaches_the_resumed_turn(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A human who answers on the ticket before the park is the reply the release exists to
    deliver: cut off at the park, it is dropped and the agent asks again."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    world.github.add_issue_comment("a-human", "An old remark about MySQL.", _BEFORE)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    answered = (world.clock.now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    world.github.add_issue_comment("a-human", "Postgres, please.", answered)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert "Postgres, please." in turn
    assert "MySQL" not in turn


async def test_a_human_typing_at_the_terminal_defers_the_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Typing is answering: a park in mid-sentence ends the session under the human's hands."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)

    world.clock.advance(timedelta(minutes=9, seconds=40))
    adapter.typed(at=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(seconds=20))
    await run_tick(world)
    world.clock.advance(timedelta(minutes=9, seconds=39))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]


async def test_a_park_whose_tick_rolled_back_parks_again_rather_than_failing(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session was interrupted but the record of it was lost. Read as a run that ended
    INTERRUPTED, it fails, and nothing retries or releases it: the ticket sits for ever."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)

    async def outage(issue_number: int, body: str) -> None:
        raise RuntimeError("GitHub answered 502")

    with monkeypatch.context() as patched:
        patched.setattr(world.projector, "comment", outage)
        with pytest.raises(RuntimeError, match="502"):
            await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE

    await run_tick(world)
    world.clock.advance(timedelta(hours=2))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert LABEL_WAITING in world.github.issue.labels
    (comment,) = _comments(world)
    assert f"````\n{_QUESTION}\n````" in comment
    types = await event_types(world)
    assert types.count(EventType.AGENT_PARKED) == 1
    assert EventType.AGENT_FAILED not in types
    assert len(adapter.spawns) == 1


async def test_an_unreachable_substrate_retries_the_release_rather_than_reparking(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A passing outage is not a refusal: re-parked, the ticket waits on a human to remove the
    label a second time for a fault that has already cleared."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")

    remove_label(world, LABEL_WAITING)
    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert not any(body.startswith("Couldn't resume") for body in _comments(world))

    adapter.resume_error = None
    await run_tick(world)
    assert len(adapter.resumed) == 2
    assert (await ticket_row(world)).status == TicketStatus.RUNNING


async def test_a_lost_stop_on_an_idle_session_is_recorded_as_the_humans(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken for an idle park, a human's stop answers to the wrong label, and the request it
    never answered stops the session again the moment it is resumed."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    (run,) = await step_runs(world)
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

    async def outage(issue_number: int, body: str) -> None:
        raise RuntimeError("GitHub answered 502")

    with monkeypatch.context() as patched:
        patched.setattr(world.projector, "comment", outage)
        with pytest.raises(RuntimeError, match="502"):
            await run_tick(world)
    await run_tick(world)

    types = await event_types(world)
    assert types.count(EventType.AGENT_INTERRUPTED) == 1
    assert EventType.AGENT_PARKED not in types
    assert adapter.interrupt_reasons == [ParkReason.USER, ParkReason.USER]
    assert LABEL_INTERRUPTED in world.github.issue.labels


async def test_a_forged_idle_since_does_not_replay_the_tickets_history(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`idle_since` is the agent's to write. Trusted as the steering cutoff, a 1970 one hands the
    whole ticket's discussion to the resumed turn as what the human just said."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    world.github.add_issue_comment("a-human", "An old remark about MySQL.", _BEFORE)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=datetime(1970, 1, 1, tzinfo=UTC), question="?")
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    world.github.add_issue_comment("a-human", "Postgres, please.", _AFTER)

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert "Postgres, please." in turn
    assert "MySQL" not in turn


async def test_a_stop_requested_as_the_session_parked_does_not_stop_it_once_resumed(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stop recorded after the parking tick read its requests is overtaken by the park. Left
    pending, it stops the session the moment a human releases it."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    (run,) = await step_runs(world)
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

    remove_label(world, LABEL_WAITING)
    await run_tick(world)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert (await event_types(world)).count(EventType.INTERRUPT_OVERTAKEN) == 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE
    assert (await ticket_row(world)).status == TicketStatus.RUNNING


async def _parked_reasons(world: World) -> list[object]:
    return [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)]


async def test_a_rollover_at_turn_end_parks_and_resumes_on_the_same_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Past 55 minutes on one token, the next turn end is where the session gets a fresh one,
    invisibly. Resumed on a later tick, the token expires under the session; parked with a
    comment, a human is asked to release what needs no human."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    await run_tick(world)
    world.clock.advance(timedelta(minutes=26))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert adapter.resumed == [("run-1", None)]
    assert len(world.minted) == 2
    types = await event_types(world)
    assert types.index(EventType.AGENT_PARKED) < types.index(EventType.AGENT_RESUMED)
    assert await _details(world, EventType.AGENT_PARKED) == [
        {
            "step": "plan",
            "attempt": 1,
            "run_id": "run-1",
            "reason": "rollover",
            "mint_age_seconds": 56 * 60,
            "at_prompt": True,
        }
    ]
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "rollover"
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE
    # A rollover waits on the substrate, not on a person: no pause to keep out of its time.
    assert run.parked_seconds == 0
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world) == []
    assert not any(LABEL_INTERRUPTED in labels for labels in _label_writes(world))
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels

    # The same spell on a fresh token: neither announced again nor rolled over again.
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert (await event_types(world)).count(EventType.AGENT_IDLE) == 1


async def test_a_busy_session_rolls_over_hard_at_58_minutes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A session that never reaches a turn end still needs its token before the hour. The
    rollover continues the active stretch: re-based, a busy run would never time out."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=57))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(minutes=1))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    ((run_id, turn),) = adapter.resumed
    assert run_id == "run-1"
    # Stopped mid-turn, it must be told to carry on: at a silent prompt it would wait on a human.
    assert turn
    assert len(world.minted) == 2
    (parked,) = await _details(world, EventType.AGENT_PARKED)
    assert parked["at_prompt"] is False
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert (await ticket_row(world)).status == TicketStatus.RUNNING

    world.clock.advance(timedelta(minutes=3))
    await run_tick(world)
    assert adapter.cancelled == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.TIMEOUT


async def test_a_stop_pressed_during_a_rollover_still_stops_the_session(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollover parks for no human, so nothing it does answers a human's stop. Answered as
    overtaken, a Stop pressed between its interrupt and its resume is dropped and the session
    runs on."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    (run,) = await step_runs(world)
    resume = adapter.resume

    async def resume_while_a_human_presses_stop(*args: Any, **kwargs: Any) -> RunHandle:
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
        return await resume(*args, **kwargs)

    monkeypatch.setattr(adapter, "resume", resume_while_a_human_presses_stop)
    world.clock.advance(timedelta(minutes=58))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    monkeypatch.undo()

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER, ParkReason.USER]
    assert EventType.INTERRUPT_OVERTAKEN not in await event_types(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


async def test_the_session_deadline_brings_the_rollover_forward(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A substrate session that ends before the token does would take the session with it."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.set_deadline(world.clock.now + timedelta(minutes=40))
    world.clock.advance(timedelta(minutes=30))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=5) - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    # Resumed under the same cap, idle or busy: another rollover cannot move the cap, and each
    # would mint.
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    adapter.go_active()
    world.clock.advance(timedelta(minutes=3))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert len(world.minted) == 2


async def test_a_refused_rollover_resume_parks_the_ticket_until_a_human_releases_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Interrupted and refused, the session is stopped whatever happens next: left alone, the
    ticket reads busy while nothing runs; retried, the step redoes the kept work."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    adapter.resume_error = AgentRuntimeError("the session is gone")
    world.clock.advance(timedelta(minutes=58))
    await run_tick(world)
    for _ in range(2):
        world.clock.advance(timedelta(hours=1))
        await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert len(adapter.resumed) == 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_INTERRUPTED} <= world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels
    (comment,) = _comments(world)
    assert "the session is gone" in comment
    assert comment.endswith(f"Remove the `{LABEL_INTERRUPTED}` label to resume it.")
    assert await _parked_reasons(world) == ["rollover"]
    assert len(adapter.spawns) == 1

    adapter.resume_error = None
    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    assert len(adapter.resumed) == 2
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    # Parked by the refused rollover until the human's release two hours later: a pause a
    # person held, whatever parked it.
    (run,) = await step_runs(world)
    assert run.parked_seconds == 2 * 3600


@pytest.mark.parametrize("held", ["idle", "busy", "first-idle-poll"])
async def test_a_rollover_whose_resume_raised_resumes_on_the_next_tick(
    maker: async_sessionmaker[AsyncSession], held: str
) -> None:
    """A passing fault after the interrupt rolls the tick back and leaves the session stopped
    with no record of it. Read as an ended run, INTERRUPTED fails a step nothing retries; read
    as an idle park, it asks a human to release a session that only needed a token. The row
    cannot say whether the session was mid-turn or waiting on a human (a first idle poll that
    rolled back still reads RUNNING), so the turn must not tell a waiting session to carry on."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    if held == "idle":
        adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=8))
    if held == "first-idle-poll":
        adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")

    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == (StepRunStatus.IDLE if held == "idle" else StepRunStatus.RUNNING)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    adapter.resume_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert len(adapter.resumed) == 2
    turn = adapter.resumed[-1][1]
    assert turn is not None
    assert "waiting on a human" in turn
    assert await _parked_reasons(world) == ["rollover"]
    assert [row["via"] for row in await _details(world, EventType.AGENT_RESUMED)] == ["rollover"]
    (run,) = await step_runs(world)
    assert run.status == (StepRunStatus.RUNNING if held == "busy" else StepRunStatus.IDLE)
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world) == []
    assert EventType.AGENT_FAILED not in await event_types(world)


async def test_a_lost_rollover_the_cap_brought_forward_is_resumed_not_idle_parked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The token is only 35 minutes old, but the spell is 5: the log says no idle park was due,
    so the stop was the rollover's, and nobody should be asked to release it."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.set_deadline(world.clock.now + timedelta(minutes=40))
    world.clock.advance(timedelta(minutes=30))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=5))
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")
    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)

    adapter.resume_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert await _parked_reasons(world) == ["rollover"]
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert LABEL_WAITING in world.github.issue.labels
    assert _comments(world) == []


async def test_a_lost_rollover_over_an_answered_question_is_not_idle_parked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The question is 18 minutes old, but a human has been typing the answer: the log cannot
    see that, so it reads as a due idle park. Parked, the session waits on a question already
    being answered."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=40))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=9))
    adapter.typed(at=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=9))
    adapter.typed(at=world.clock.now)
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")
    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    adapter.resume_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert await _parked_reasons(world) == ["rollover"]
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world) == []


async def test_a_human_typing_at_the_prompt_holds_off_the_turn_end_rollover(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rollover restarts the session under the human's hands. Typing holds it off until the
    mid-turn time, which the token cannot wait past."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)

    world.clock.advance(timedelta(minutes=6))
    adapter.typed(at=world.clock.now - timedelta(seconds=10))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(minutes=2))
    adapter.typed(at=world.clock.now - timedelta(seconds=10))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]


async def test_an_idle_park_due_with_the_rollover_parks_and_mints_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Parked for a human, the session gets a fresh token when it is released. Rolled over
    first, it mints a token only to be interrupted again on the same tick."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=10))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert len(world.minted) == 1


_ANSWER = "Postgres 16, on the shared cluster."


async def _press_resume(
    world: World, auth: dict[str, str], body: dict[str, object]
) -> dict[str, object]:
    (run, *_) = [r for r in await step_runs(world) if r.status == StepRunStatus.INTERRUPTED]
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.post(
            f"/api/step-runs/{run.id}/resume-session", headers=auth, json=body
        )
    assert response.status_code == 202
    return response.json()


async def _requests(world: World) -> list[tuple[int, dict[str, object]]]:
    return [
        (row.id, dict(row.details))
        for row in await event_rows(world)
        if row.type == EventType.SESSION_RESUME_REQUESTED
    ]


async def test_resume_session_records_one_request_per_park(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """One request per park: a double click that recorded two would release the session once
    and leave the second pending, to release the NEXT park with a stale answer."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)

    assert (await _press_resume(world, auth, {"answer": _ANSWER}))["status"] == "requested"
    second = await _press_resume(world, auth, {"answer": "A second thought."})
    assert second["status"] == "pending"

    ((_, details),) = await _requests(world)
    assert details == {
        "by": "api-token",
        "requested_via": "dashboard",
        "has_answer": True,
        "answer": _ANSWER,
        "step": "plan",
        "attempt": 1,
    }


async def test_a_dashboard_resume_releases_a_waiting_park_with_the_answer_as_the_turn(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The label is still on the issue: the request alone releases the park, and the answer
    reaches the agent as the turn — and is copied nowhere else, so the log keeps one record of
    whatever a human typed."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.add_issue_comment("a-human", "Keep the pool small.", _AFTER)

    await _press_resume(world, auth, {"answer": _ANSWER})
    assert LABEL_WAITING in world.github.issue.labels
    await run_tick(world)

    ((run_id, turn),) = adapter.resumed
    assert run_id == "run-1"
    assert turn is not None
    assert f"api-token answered from the dashboard:\n{_ANSWER}" in turn
    assert "Keep the pool small." in turn
    ((request_id, _),) = await _requests(world)
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "dashboard"
    assert resumed["request_id"] == request_id
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert PLAN_LABEL in world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels
    elsewhere = [
        row.type
        for row in await event_rows(world)
        if row.id != request_id and _ANSWER in str(row.details)
    ]
    assert elsewhere == []
    assert not any(_ANSWER in body for body in _comments(world))
    await run_tick(world)
    assert len(adapter.resumed) == 1


async def test_a_resume_pressed_and_a_label_removed_release_the_park_once(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """Both gestures, one tick: the label release would resume without the answer, and then the
    request — answered by that resume — would never deliver it."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    await _press_resume(world, auth, {"answer": _ANSWER})
    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert _ANSWER in turn
    ((request_id, _),) = await _requests(world)
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "dashboard"
    assert resumed["request_id"] == request_id
    assert PLAN_LABEL in world.github.issue.labels


async def test_a_refused_dashboard_resume_is_answered_once(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """Unanswered, a refused request stays pending: every tick would try the resume again and
    post another "could not resume"."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    adapter.resume_error = AgentRuntimeError("the session is gone")

    await _press_resume(world, auth, {})
    await run_tick(world)
    await run_tick(world)

    assert len(adapter.resumed) == 1
    ((request_id, details),) = await _requests(world)
    assert details["has_answer"] is False
    assert "answer" not in details
    (refused,) = await _details(world, EventType.SESSION_RESUME_REFUSED)
    assert refused["request_id"] == request_id
    assert "the session is gone" in str(refused["reason"])
    assert sum("Couldn't resume" in body for body in _comments(world)) == 1
    assert EventType.AGENT_RESUMED not in await event_types(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels
    assert LABEL_INTERRUPTED not in world.github.issue.labels


async def test_a_resume_request_older_than_the_park_releases_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A request that lost a race with a label release is left pending behind the resume it
    raced. Taken at face value, it would release the run's NEXT park the moment it happened,
    with an answer to a question nobody has read yet."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    (run,) = await step_runs(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.SESSION_RESUME_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            by="api-token",
            has_answer=True,
            answer=_ANSWER,
            step="plan",
            attempt=1,
        )
        await session.commit()
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    await run_tick(world)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert adapter.resumed == []
    ((request_id, _),) = await _requests(world)
    (refused,) = await _details(world, EventType.SESSION_RESUME_REFUSED)
    assert refused["request_id"] == request_id
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_WAITING} <= world.github.issue.labels
    assert len(_comments(world)) == 1


# --------------------------------------------------------------------------- the terminal target


async def test_an_interactive_spawn_records_where_its_terminal_is(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The gateway reads where to attach only from the log (decision 57): without the key a live
    session has no terminal."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    (spawned,) = await _details(world, EventType.AGENT_SPAWNED)
    assert spawned["terminal"] == (await adapter.terminal_target("run-1")).model_dump()


async def test_with_the_terminal_switch_off_a_label_release_still_resumes_a_parked_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The switch gates a browser's attach and typing only: a run is a session either way, so
    it parks, and removing its label resumes it."""
    world, adapter = await interactive_world(maker, sessions=SessionPolicy())
    await _idle_parked(world, adapter)

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    assert len(adapter.resumed) == 1
    assert (await step_runs(world))[-1].status == StepRunStatus.RUNNING


async def test_a_resumed_session_records_its_terminal_again(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A resume can land the session somewhere new, and the gateway reads the latest target: one
    the resume did not record leaves the tab attaching to where the session was before."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)

    remove_label(world, LABEL_WAITING)
    await run_tick(world)

    assert adapter.resumed == [("run-1", None)]
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["terminal"] == (await adapter.terminal_target("run-1")).model_dump()


# ------------------------------------------------------------------- typing through the gateway

_DASHBOARD = "https://factorial.example.com"


async def _gateway_wrote(
    world: World, event_type: EventType, at: datetime, **details: object
) -> None:
    """What the terminal gateway appends for the run, at `at`: its rows carry the database's
    clock, which the loop compares with its own."""
    (run, *_) = await step_runs(world)
    async with world.sessions() as session:
        session.add(
            Event(
                project_id=world.project_id,
                ticket_id=world.ticket_id,
                step_run_id=run.id,
                type=str(event_type),
                details={"step": "plan", "attempt": 1, "by": "a@example.com", **details},
                created_at=at,
            )
        )
        await session.commit()


async def test_typing_in_the_terminal_without_submitting_defers_finish(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The session reports no input of its own here: only the gateway saw the human typing. A
    finish under them ends the session mid-sentence."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await _gateway_wrote(
        world, EventType.TERMINAL_INPUT, world.clock.now - timedelta(seconds=30), bytes=12
    )

    await run_tick(world)

    assert adapter.finish_calls == []
    assert EventType.AGENT_FINISHING not in await event_types(world)

    world.clock.advance(timedelta(seconds=31))
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED


async def test_typing_defers_the_idle_park(maker: async_sessionmaker[AsyncSession]) -> None:
    """Typing into the terminal is answering, and the park clock restarts from the last burst."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)

    world.clock.advance(timedelta(minutes=9, seconds=40))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=40)
    world.clock.advance(timedelta(seconds=20))
    await run_tick(world)
    world.clock.advance(timedelta(minutes=9, seconds=39))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]


async def test_a_watcher_never_defers_finish_or_the_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Watching is not answering: a session someone keeps open in a tab would otherwise never
    finish and never park."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)

    world.clock.advance(timedelta(minutes=9, seconds=40))
    await _gateway_wrote(
        world, EventType.TERMINAL_ATTACHED, world.clock.now, writable=False, target="session"
    )
    world.clock.advance(timedelta(seconds=20))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]

    finishing, finishing_adapter = await interactive_world(maker)
    await run_tick(finishing)
    finishing.github.deliver_spec(finishing.issue_number)
    finishing_adapter.go_idle(IdleReason.TURN_END, since=finishing.clock.now)
    await _gateway_wrote(
        finishing,
        EventType.TERMINAL_DETACHED,
        finishing.clock.now - timedelta(seconds=5),
        seconds=300.0,
        code=1000,
    )
    await run_tick(finishing)
    assert finishing_adapter.finish_calls == ["run-1"]


async def test_the_dashboard_says_when_the_loop_will_park_a_session_someone_typed_into(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The waiting banner's countdown is `idle.parks_at`: one the typing did not push back tells
    the human the session parks while it is still waiting for them."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=4))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
    await run_tick(world)

    (run,) = await step_runs(world)
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)
    assert response.status_code == 200
    parks_at = datetime.fromisoformat(response.json()["idle"]["parks_at"])
    assert parks_at == world.clock.now + world.deps.sessions.idle_park_after
    assert response.json()["idle"]["at_deadline"] == "park"

    world.clock.now = parks_at - timedelta(seconds=1)
    await run_tick(world)
    assert adapter.interrupt_reasons == []
    world.clock.now = parks_at
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]


_SELF_ANSWERED = "Postgres or SQLite?"


async def _asked_with_expiry(world: World, adapter: FakeAdapter) -> None:
    """Idle on a question the CLI answers itself in two minutes, as codex's question box is."""
    adapter.go_idle(
        IdleReason.QUESTION,
        since=world.clock.now,
        question=_SELF_ANSWERED,
        question_expires_at=world.clock.now + timedelta(seconds=120),
    )


async def test_a_question_the_cli_would_answer_itself_parks_at_the_first_tick_that_sees_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Codex answers its picker with nothing two minutes on: waiting the usual ten would hand
    the agent an empty answer and leave the person a question already gone."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    await _asked_with_expiry(world, adapter)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert LABEL_WAITING in world.github.issue.labels
    (comment,) = _comments(world)
    assert _SELF_ANSWERED in comment


async def test_typing_since_the_question_holds_its_early_park_to_the_idle_deadline(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Someone typing is answering the picker: parking would hang up the answer mid-word."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    await _asked_with_expiry(world, adapter)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]


async def _idle_view(world: World, auth: dict[str, str]) -> dict[str, Any]:
    (run,) = await step_runs(world)
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)
    assert response.status_code == 200
    return response.json()["idle"]


async def test_the_dashboard_says_a_codex_question_parks_at_once(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """A countdown of ten minutes on a question codex answers in two tells the person they
    have time they do not have. The worker parks such a spell in the tick that records it, so
    the spell is written here as that tick would have left it had its park not happened."""
    world, _ = await interactive_world(maker)
    await run_tick(world)
    since = world.clock.now
    (run,) = await step_runs(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.AGENT_IDLE,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step="plan",
            attempt=1,
            reason=IdleReason.QUESTION,
            idle_since=since.isoformat(),
            question=_SELF_ANSWERED,
            question_expires_at=(since + timedelta(seconds=120)).isoformat(),
        )
        row = await session.get(StepRun, run.id)
        assert row is not None
        row.status = StepRunStatus.IDLE
        await session.commit()

    assert datetime.fromisoformat((await _idle_view(world, auth))["parks_at"]) == since

    world.clock.advance(timedelta(seconds=30))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
    parks_at = datetime.fromisoformat((await _idle_view(world, auth))["parks_at"])
    assert parks_at == world.clock.now + world.deps.sessions.idle_park_after


async def test_the_idle_park_comment_links_the_terminal_and_names_resume(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The comment is where the human learns the agent is waiting: it names both ways back, and
    the link opens the run's terminal on the dashboard."""
    world, adapter = await interactive_world(
        maker, sessions=SessionPolicy(terminal_enabled=True, dashboard_url=_DASHBOARD)
    )
    await _idle_parked(world, adapter)

    (run,) = await step_runs(world)
    (comment,) = _comments(world)
    assert f"({_DASHBOARD}/orgs/default/tickets/{world.ticket_id}?terminal={run.id})" in comment
    assert "press Resume" in comment
    assert f"remove the `{LABEL_WAITING}` label" in comment


@pytest.mark.parametrize(
    "dashboard_url", ["", "factorial.example.com", "https://factorial.example.com/#/home"]
)
async def test_an_idle_park_comment_links_nothing_without_an_absolute_dashboard_url(
    maker: async_sessionmaker[AsyncSession], dashboard_url: str
) -> None:
    """A relative link on GitHub resolves against the repository, and leads nowhere."""
    world, adapter = await interactive_world(
        maker, sessions=SessionPolicy(terminal_enabled=True, dashboard_url=dashboard_url)
    )
    await _idle_parked(world, adapter)

    (comment,) = _comments(world)
    assert "?terminal=" not in comment
    assert "](" not in comment
    assert "press Resume" in comment
    assert f"remove the `{LABEL_WAITING}` label" in comment


async def _finishing_with_typing(world: World, adapter: FakeAdapter) -> None:
    """Met postconditions, a finish that never settles and has been sent twice, and a human
    typing every five minutes, so neither the park nor the finish ever goes through."""
    world.github.deliver_spec(world.issue_number)
    adapter.finish_settles = False
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=10))
    await run_tick(world)
    assert adapter.finish_calls == ["run-1", "run-1"]


async def test_a_resent_finish_held_by_typing_still_rolls_over(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Typing holds a re-sent finish at its prompt, and that hold must not outlive the token:
    past the hour the session works on with a dead credential, and the substrate's cap ends it
    with nothing parked."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    minted_at = world.clock.now
    await _finishing_with_typing(world, adapter)

    while world.clock.now + timedelta(minutes=5) < minted_at + timedelta(minutes=58):
        world.clock.advance(timedelta(minutes=5))
        await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
        await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.now = minted_at + timedelta(minutes=58)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert await _parked_reasons(world) == ["rollover"]
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world) == []


async def test_a_finish_in_flight_rolls_over_at_the_mid_turn_time(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A finish sent at 57 minutes is re-sent only at 67: waiting for it, the token expires."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=57))
    world.github.deliver_spec(world.issue_number)
    adapter.finish_settles = False
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]

    world.clock.advance(timedelta(minutes=1))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert adapter.finish_calls == ["run-1"]


async def test_a_lost_rollover_over_a_finish_someone_is_typing_into_is_not_idle_parked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The re-sent finish is 28 minutes old, but a human typed three minutes ago: the stop was
    the cap's rollover, and parked idle the session waits on a human who was still there."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.set_deadline(world.clock.now + timedelta(minutes=40))
    await _finishing_with_typing(world, adapter)
    for _ in range(5):
        world.clock.advance(timedelta(minutes=5))
        await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
        await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(minutes=3))
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")
    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    adapter.resume_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    assert await _parked_reasons(world) == ["rollover"]
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world) == []


def _ceiling(ceiling: timedelta, **kw: Any) -> SessionPolicy:
    return SessionPolicy(terminal_enabled=True, session_ceiling=ceiling, **kw)


async def test_a_session_alive_past_its_ceiling_parks_for_a_human(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """At 58 minutes the mid-turn rollover is due too, and it must not mint: a session at its
    ceiling given a fresh token has another hour the ceiling exists to refuse it."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=58))
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=57))
    await run_tick(world)
    assert adapter.interrupt_reasons == []

    world.clock.advance(timedelta(minutes=1))
    await run_tick(world)
    for _ in range(2):
        world.clock.advance(timedelta(hours=1))
        await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.CEILING]
    assert adapter.resumed == []
    assert len(world.minted) == 1
    assert adapter.cancelled == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_INTERRUPTED} <= world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels
    (comment,) = _comments(world)
    assert "58 minutes" in comment
    assert "Press Resume on the run's terminal in the dashboard to resume it" in comment
    assert await _details(world, EventType.AGENT_PARKED) == [
        {
            "step": "plan",
            "attempt": 1,
            "run_id": "run-1",
            "reason": "ceiling",
            "live_seconds": 3480,
            "at_prompt": False,
        }
    ]


async def test_forged_activity_never_moves_the_ceiling(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The agent can write the hook lines that say a turn ended and a prompt was submitted. Each
    pair restarts the active-time deadline, and every 55 minutes a rollover mints it a fresh
    token: a ceiling that counted from reported activity would move with them, and the session
    would hold a live token for ever."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(hours=3))
    )
    await run_tick(world)
    spawned = world.clock.now
    while ParkReason.CEILING not in adapter.interrupt_reasons:
        assert world.clock.now - spawned < timedelta(hours=3, minutes=5), "outlived its ceiling"
        world.clock.advance(timedelta(minutes=4))
        adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
        await run_tick(world)
        world.clock.advance(timedelta(minutes=1))
        adapter.go_active()
        await run_tick(world)

    assert adapter.interrupt_reasons.count(ParkReason.ROLLOVER) == 3
    assert (await event_types(world)).count(EventType.AGENT_ACTIVE) >= 30
    assert adapter.cancelled == []
    (*_, parked) = await _details(world, EventType.AGENT_PARKED)
    assert parked["reason"] == "ceiling"
    live = parked["live_seconds"]
    assert isinstance(live, int)
    assert timedelta(hours=3) <= timedelta(seconds=live) < timedelta(hours=3, minutes=5)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert LABEL_INTERRUPTED in world.github.issue.labels


async def test_a_dashboard_resume_starts_a_new_ceiling_window_and_a_rollover_does_not(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """Only an operator's Resume starts a window: a rollover resumes on the loop's own say, and
    counted, it would carry a session past its ceiling an hour at a time."""
    world, adapter = await interactive_world(
        maker,
        bot_login=BOT_LOGIN,
        sessions=_ceiling(timedelta(hours=2), idle_park_after=timedelta(hours=5)),
    )
    await run_tick(world)
    spawned = world.clock.now
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Which table?")
    for at in (30, 60, 90):
        world.clock.now = spawned + timedelta(minutes=at)
        await run_tick(world)
    world.clock.now = spawned + timedelta(hours=2) - timedelta(seconds=1)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER, ParkReason.ROLLOVER]

    world.clock.now = spawned + timedelta(hours=2)
    await run_tick(world)
    assert adapter.interrupt_reasons[-1] == ParkReason.CEILING

    world.clock.now = spawned + timedelta(hours=2, minutes=30)
    await _press_resume(world, auth, {})
    await run_tick(world)
    (*_, released) = await _details(world, EventType.AGENT_RESUMED)
    assert released["via"] == "dashboard"
    released_at = world.clock.now
    for at in (30, 60, 90):
        world.clock.now = released_at + timedelta(minutes=at)
        await run_tick(world)
    world.clock.now = released_at + timedelta(hours=2) - timedelta(seconds=1)
    await run_tick(world)
    assert adapter.interrupt_reasons.count(ParkReason.CEILING) == 1
    assert adapter.interrupt_reasons[-1] == ParkReason.ROLLOVER

    world.clock.now = released_at + timedelta(hours=2)
    await run_tick(world)
    assert adapter.interrupt_reasons.count(ParkReason.CEILING) == 2
    assert [
        row["live_seconds"]
        for row in await _details(world, EventType.AGENT_PARKED)
        if row["reason"] == "ceiling"
    ] == [7200, 7200]


async def test_an_mcp_resume_cannot_lift_a_ceiling_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The ceiling bounds a session's spend because only a person lifts it (decision 56). A model
    holding an MCP token could press Resume at every park, so the MCP surface is refused and
    records nothing; the dashboard's Resume still releases it."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=30))
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]

    (parked,) = await step_runs(world)
    async with world.sessions() as session:
        run = await session.get(StepRun, parked.id)
        assert run is not None
        with pytest.raises(Conflict, match="session time limit"):
            await request_resume(
                session, run, Actor(by="ops@example.com", via=RequestedVia.MCP), answer=None
            )
    assert await _details(world, EventType.SESSION_RESUME_REQUESTED) == []

    async with world.sessions() as session:
        run = await session.get(StepRun, parked.id)
        assert run is not None
        result = await request_resume(
            session, run, Actor.dashboard("ops@example.com"), answer=None
        )
    assert result.status == "requested"


async def test_an_earned_finish_wins_over_the_ceiling(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Parked at the turn end its postconditions were met on, the work is done and a human is
    asked to resume a session with nothing left to do."""
    world, adapter = await interactive_world(maker, sessions=_ceiling(timedelta(minutes=30)))
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.interrupt_reasons == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert (await ticket_row(world)).current_step == "spec_gate"


async def test_a_label_release_of_an_idle_park_does_not_open_a_ceiling_window(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Any token in the repository can remove `factorial:waiting`: counted, a parked agent's
    leftover process could release its own idle park and buy the session a fresh window."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(hours=2))
    )
    await run_tick(world)
    spawned = world.clock.now
    world.clock.now = spawned + timedelta(minutes=20)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    world.clock.now = spawned + timedelta(minutes=30)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]

    world.clock.now = spawned + timedelta(minutes=40)
    remove_label(world, LABEL_WAITING)
    await run_tick(world)
    (released,) = await _details(world, EventType.AGENT_RESUMED)
    assert released["via"] == "label"
    while ParkReason.CEILING not in adapter.interrupt_reasons:
        assert world.clock.now - spawned < timedelta(hours=2, minutes=5), "the release moved it"
        world.clock.advance(timedelta(minutes=4))
        adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
        await run_tick(world)
        world.clock.advance(timedelta(minutes=1))
        adapter.go_active()
        await run_tick(world)

    (*_, parked) = await _details(world, EventType.AGENT_PARKED)
    assert parked["reason"] == "ceiling"


async def test_a_label_release_of_a_ceiling_park_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Any token in the repository can remove a label, the parked agent's own included: released
    by one, a session would open itself a new window, and the ceiling would bound nothing."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=50))
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    await run_tick(world)

    assert adapter.resumed == []
    assert len(world.minted) == 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_INTERRUPTED} <= world.github.issue.labels
    (_, refusal) = _comments(world)
    assert "neither removing the `factorial:interrupted` label nor re-entering" in refusal


async def test_an_answer_posted_before_a_ceiling_park_reaches_the_resumed_turn(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """A ceiling can land while the agent waits on a question someone already answered on the
    ticket: cut off at the park, the answer is dropped and the agent asks again."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=30))
    )
    await run_tick(world)
    spawned = world.clock.now
    world.clock.now = spawned + timedelta(minutes=25)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question=_QUESTION)
    await run_tick(world)
    answered = (spawned + timedelta(minutes=27)).isoformat().replace("+00:00", "Z")
    world.github.add_issue_comment("a-human", "Postgres, please.", answered)
    world.clock.now = spawned + timedelta(minutes=30)
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]

    await _press_resume(world, auth, {})
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert "Postgres, please." in turn


async def test_a_ceiling_park_resumed_with_nothing_said_is_told_to_carry_on(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """Stopped mid-turn and resumed at a silent prompt, the session waits on a human for work it
    was in the middle of, and idle-parks ten minutes later."""
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=50))
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]

    await _press_resume(world, auth, {})
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert "continue it" in turn
    assert "ask your question again" in turn


# ------------------------------------------------------------- a stop with nobody to wait for


async def _silent_stop(world: World, adapter: FakeAdapter) -> None:
    """Idle at a plain turn end with nothing delivered, and wait out the idle deadline."""
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)


async def test_an_unattended_stop_with_nothing_asked_finishes_once_and_retries(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Parked, a silent stop waits for a person nobody told to look: no retry, no escalation,
    and a ticket that sits under `factorial:waiting` until someone notices."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    # Nobody is asked to look at a stop that will end and retry.
    assert LABEL_WAITING not in world.github.issue.labels
    assert PLAN_LABEL in world.github.issue.labels
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.interrupt_reasons == []
    (finishing,) = await _details(world, EventType.AGENT_FINISHING)
    assert finishing["silent"] is True
    assert finishing["unmet"] == ['branch_exists("spec/12-*") = false']
    types = await event_types(world)
    assert EventType.POSTCONDITION_FAILED in types
    assert EventType.AGENT_PARKED not in types
    assert LABEL_WAITING not in world.github.issue.labels

    world.clock.advance(timedelta(minutes=10))
    await run_tick(world)
    (first, *_) = await step_runs(world)
    assert first.status == StepRunStatus.FAILED
    assert len(adapter.spawns) == 2


async def test_typing_into_the_terminal_makes_an_unmet_stop_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person who typed into the session is working with it: retried, their conversation is
    thrown away under them."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=5)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    assert LABEL_WAITING in world.github.issue.labels
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert adapter.finish_calls == []
    assert "A person worked with this session, so it waits for them." in _comments(world)[0]


async def test_a_rollover_is_not_a_person_working_with_the_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rollover's resume is the loop's own: counted, every session that ran an hour would
    park its silent stop for a person."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=58))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]
    await _silent_stop(world, adapter)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]


async def test_a_watcher_or_a_seat_take_is_not_a_person_working_with_the_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Watching is not working with it: a tab left open would turn every silent stop into a
    park."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    await _gateway_wrote(
        world, EventType.TERMINAL_ATTACHED, world.clock.now, writable=False, target="session"
    )
    await _gateway_wrote(world, EventType.TERMINAL_SEAT_TAKEN, world.clock.now, forced=False)
    await _silent_stop(world, adapter)

    assert adapter.finish_calls == ["run-1"]
    assert EventType.AGENT_PARKED not in await event_types(world)


async def test_input_only_the_session_reports_does_not_make_it_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The session's own input report is the agent's to write: trusted as engagement, an agent
    could turn its every silent stop into a park nothing retries."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    idle_at = world.clock.now
    adapter.go_idle(IdleReason.TURN_END, since=idle_at)
    await run_tick(world)
    adapter.typed(at=idle_at + timedelta(minutes=3))
    world.clock.now = idle_at + timedelta(minutes=13)
    await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.interrupt_reasons == []


async def test_working_with_one_attempt_does_not_make_the_next_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A retry starts the work afresh, with nobody at it: carried over, one person's typing
    would park every later attempt's silent stop."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=5)
    adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=10))
    adapter.restart()
    await run_tick(world)
    assert len(adapter.spawns) == 2

    await _silent_stop(world, adapter)

    assert adapter.finish_calls == ["run-2"]
    assert adapter.interrupt_reasons == []


@pytest.mark.parametrize("release", ["label", "dashboard", "mcp"])
async def test_each_human_release_makes_an_unmet_stop_park(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str], release: str
) -> None:
    """Whoever released a park is waiting on the session they released: its next silent stop,
    retried, ends the conversation they came back for."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    assert adapter.interrupt_reasons == [ParkReason.IDLE]

    if release == "label":
        remove_label(world, LABEL_WAITING)
    elif release == "dashboard":
        await _press_resume(world, auth, {})
    else:
        (parked,) = await step_runs(world)
        async with world.sessions() as session:
            run = await session.get(StepRun, parked.id)
            assert run is not None
            await request_resume(
                session, run, Actor(by="ops@example.com", via=RequestedVia.MCP), answer=None
            )
    await run_tick(world)
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed.get("via", "label") == release
    adapter.go_active()
    await run_tick(world)
    await _silent_stop(world, adapter)

    assert adapter.interrupt_reasons == [ParkReason.IDLE, ParkReason.IDLE]
    assert adapter.finish_calls == []


async def test_a_lost_rollover_over_an_unattended_turn_end_resumes_rather_than_parks(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The spell is 15 minutes old by the log, which cannot see the input the session reported
    at 29, so the stop reads as a due idle park. But a silent stop never parks: the stop was
    the cap's rollover, and parked, it waits on a person for nothing."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    spawned = world.clock.now
    adapter.set_deadline(spawned + timedelta(minutes=40))
    world.clock.now = spawned + timedelta(minutes=20)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.now = spawned + timedelta(minutes=29)
    adapter.typed(at=world.clock.now)
    await run_tick(world)
    world.clock.now = spawned + timedelta(minutes=35)
    adapter.resume_error = SubstrateUnavailable("E2B answered 503")
    with pytest.raises(SubstrateUnavailable):
        await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    adapter.resume_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert await _parked_reasons(world) == ["rollover"]
    assert _comments(world) == []
    assert (await ticket_row(world)).status == TicketStatus.RUNNING


async def test_a_silent_finish_that_never_lands_is_cancelled_and_retried(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Parked, it would say its postconditions hold, which they do not, and wait on a person
    for a run that has nothing to ask: the step timeout's cancel and fail lets it retry."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.finish_settles = False
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    for _ in range(3):
        world.clock.advance(world.deps.sessions.idle_park_after)
        await run_tick(world)

    assert adapter.finish_calls == ["run-1", "run-1"]
    assert adapter.cancelled == ["run-1"]
    assert adapter.interrupt_reasons == []
    (failed,) = await _details(world, EventType.AGENT_FAILED)
    assert failed["reason"] == "finish_unsettled"
    assert EventType.AGENT_PARKED not in await event_types(world)
    assert not any("is paused" in body for body in _comments(world))


async def test_typing_after_a_silent_finish_parks_rather_than_retries(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person who took up the session while it was being ended is working with it: cancelled
    and retried, their conversation is thrown away under them."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.finish_settles = False
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    park_after = world.deps.sessions.idle_park_after
    world.clock.advance(park_after)
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]

    world.clock.advance(timedelta(minutes=1))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=5)
    for _ in range(2):
        world.clock.advance(park_after)
        await run_tick(world)

    assert adapter.finish_calls == ["run-1"]
    assert adapter.cancelled == []
    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert EventType.AGENT_FAILED not in await event_types(world)
    (comment,) = _comments(world)
    assert "A person worked with this session, so it waits for them." in comment


async def test_a_rollover_over_a_question_still_parks_with_the_question(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A session resumed for its token comes back at a plain prompt, its question no longer on
    screen. Read from that prompt alone, the stop is silent: the question is lost and the work
    the person was asked about starts again."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Push now?")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.ROLLOVER]

    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.finish_calls == []
    assert adapter.interrupt_reasons[-1] == ParkReason.IDLE
    assert "Push now?" in _comments(world)[0]


# ------------------------------------------------------------- a stop while CI still runs

_CI_PLAN: dict[str, Any] = {
    "postconditions": {
        "all": [
            'branch_exists("spec/{ticket}-*")',
            'pr_open("spec/{ticket}-*", "main")',
            'ci_green("spec/{ticket}-*")',
        ]
    }
}


def _ci_running(world: World) -> None:
    world.github.deliver_spec(world.issue_number)
    world.github.check_runs_by_sha["sha-1"] = (
        CheckRunState(name="ci", status="in_progress", conclusion=None),
    )


async def test_an_idle_session_waits_out_running_ci_and_its_clock_starts_when_ci_settles(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An agent that pushed and went quiet has done its part until CI answers: ended before
    then, its run fails on a check that has not finished and the work starts again."""
    world, adapter = await interactive_world(maker, plan=_CI_PLAN)
    await run_tick(world)
    _ci_running(world)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=25))
    await run_tick(world)

    assert adapter.finish_calls == []
    assert adapter.interrupt_reasons == []
    assert PLAN_LABEL in world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels

    world.github.check_runs_by_sha["sha-1"] = (
        CheckRunState(
            name="ci", status="completed", conclusion="failure", completed_at=world.clock.now
        ),
    )
    park_after = world.deps.sessions.idle_park_after
    world.clock.advance(park_after - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.finish_calls == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]
    (finishing,) = await _details(world, EventType.AGENT_FINISHING)
    assert finishing["silent"] is True
    assert finishing["unmet"] == ['ci_green("spec/12-*") = false']


async def test_ci_that_never_settles_holds_a_session_no_longer_than_the_step_timeout(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A check stuck queued would otherwise hold a silent session at its prompt for good, with
    nobody told to look."""
    world, adapter = await interactive_world(maker, plan={**_CI_PLAN, "timeout": "30m"})
    await run_tick(world)
    spawned = world.clock.now
    _ci_running(world)
    adapter.go_idle(IdleReason.TURN_END, since=spawned)
    await run_tick(world)
    world.clock.now = spawned + timedelta(minutes=30) - timedelta(seconds=1)
    await run_tick(world)
    assert adapter.finish_calls == []

    world.clock.now = spawned + timedelta(minutes=30)
    await run_tick(world)
    assert adapter.finish_calls == ["run-1"]


async def test_a_question_asked_while_ci_runs_parks_at_idle_park_after(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The CI hold is for a stop nobody waits on. Held, a question waits unasked behind a slow
    check while the person who could answer it is never told."""
    world, adapter = await interactive_world(maker, plan=_CI_PLAN)
    await run_tick(world)
    _ci_running(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Merge base?")
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    assert adapter.finish_calls == []
    assert "Merge base?" in _comments(world)[0]


async def test_the_drawer_says_whether_the_deadline_parks_or_retries(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The countdown alone tells a person the session will wait for them, when it will end
    and retry."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    assert (await _idle_view(world, auth))["at_deadline"] == "retry"

    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=3)
    assert (await _idle_view(world, auth))["at_deadline"] == "park"


async def _recheck_due(world: World, *, listings: bool = False) -> None:
    world.clock.advance(world.deps.sessions.parked_recheck)
    await run_tick(world, listings=listings)


async def _parks(world: World) -> list[tuple[object, object]]:
    return [(d["reason"], d["at_prompt"]) for d in await _details(world, EventType.AGENT_PARKED)]


async def _completed(world: World) -> list[dict[str, object]]:
    return await _details(world, EventType.AGENT_COMPLETED)


async def test_a_recheck_accepts_an_idle_park_whose_pr_opened_with_no_resume_and_no_mint(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Finished work waits for nobody: the agent pushed and opened its PR, then asked. A resume
    would mint and wake a session only to end it, and the projection must write labels from a
    fresh read, not the listing's, or it drops the one a human added since."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    parked_at = world.clock.now
    world.github.deliver_spec(world.issue_number)
    world.github.listing_issue = world.github.issue
    add_label(world, "keep-me")

    await _recheck_due(world, listings=True)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert run.ended_at == parked_at
    assert run.failure_reason is None
    ticket = await ticket_row(world)
    assert (ticket.status, ticket.current_step) == (TicketStatus.RUNNING, "spec_gate")
    assert adapter.resumed == []
    assert adapter.finish_calls == ["run-1"], "the park ends: a host's holds the branch"
    assert len(world.minted) == 1
    (completed,) = await _completed(world)
    assert completed["while_parked"] == "idle"
    assert completed["session_id"] == "session-run-1"
    assert LABEL_WAITING not in world.github.issue.labels
    assert "keep-me" in world.github.issue.labels
    assert len([c for c in _comments(world) if "accepted while paused" in c]) == 1


async def test_a_recheck_of_an_unmet_park_reads_github_once_per_interval_and_writes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every parked session is read on every tick: unthrottled, a few dozen of them spend the
    App's hourly budget on questions nobody has answered."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    reads = world.github.repo_reads
    events = await event_types(world)
    calls = list(world.projector.calls)

    await run_tick(world)
    assert world.github.repo_reads == reads + 1
    world.clock.advance(world.deps.sessions.parked_recheck - timedelta(seconds=1))
    await run_tick(world)
    await run_tick(world)
    assert world.github.repo_reads == reads + 1
    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert world.github.repo_reads == reads + 2

    assert await event_types(world) == events
    assert world.projector.calls == calls
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED


async def test_a_recheck_never_accepts_a_run_a_person_stopped(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person stopped the run to redirect it. Accepted, the ticket moves on with the work
    they meant to change."""
    world, _ = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    (run,) = await step_runs(world)
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
    reads = world.github.repo_reads
    world.github.deliver_spec(world.issue_number)

    await _recheck_due(world)
    await _recheck_due(world)

    assert EventType.AGENT_COMPLETED not in await event_types(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert world.github.repo_reads == reads


async def test_a_recheck_never_accepts_over_a_stop_the_park_overtook(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person pressed Stop as the session parked idle, so no `agent_interrupted` answers it and
    the park reads `idle`. Accepted, the ticket moves on over the stop they asked for."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    (run,) = await step_runs(world)
    # Recorded after the park's tick read the requests, as a press during that tick is.
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
    assert await _parks(world) == [("idle", True)]
    reads = world.github.repo_reads
    world.github.deliver_spec(world.issue_number)

    await _recheck_due(world)
    await _recheck_due(world)

    assert EventType.AGENT_COMPLETED not in await event_types(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert world.github.repo_reads == reads


async def test_a_recheck_holds_while_a_persons_answer_is_newer_than_the_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person answering on the ticket expects the session to read it. Accepted, the answer
    reaches nobody and the ticket moves on without it."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    world.github.add_issue_comment("a-human", "Use Postgres.", _AFTER)

    await _recheck_due(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert EventType.AGENT_COMPLETED not in await event_types(world)
    assert LABEL_WAITING in world.github.issue.labels

    remove_label(world, LABEL_WAITING)
    await run_tick(world)
    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert "Use Postgres." in turn


async def test_a_recheck_accepts_a_ceiling_park_taken_at_the_prompt_with_no_mint(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A ceiling park at the prompt is a finished turn like an idle one: only a dashboard Resume
    releases it, so without the re-check finished work waits for an operator."""
    world, adapter = await interactive_world(
        maker,
        bot_login=BOT_LOGIN,
        sessions=_ceiling(timedelta(minutes=58), idle_park_after=timedelta(hours=2)),
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=50))
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(timedelta(minutes=8))
    await run_tick(world)
    assert await _parks(world) == [("ceiling", True)]

    world.github.deliver_spec(world.issue_number)
    await _recheck_due(world)

    (completed,) = await _completed(world)
    assert completed["while_parked"] == "ceiling"
    assert adapter.resumed == []
    assert len(world.minted) == 1
    assert LABEL_INTERRUPTED not in world.github.issue.labels


async def test_a_recheck_never_accepts_a_ceiling_park_taken_mid_turn_even_with_its_checks_met(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Mid-turn, a PR can be open with CI green and half the turn undone: the postconditions
    alone cannot say the work is finished."""
    world, _ = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(minutes=58))
    )
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.clock.advance(timedelta(minutes=58))
    await run_tick(world)
    assert await _parks(world) == [("ceiling", False)]

    await _recheck_due(world)
    await _recheck_due(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert EventType.AGENT_COMPLETED not in await event_types(world)
    assert LABEL_INTERRUPTED in world.github.issue.labels


async def test_an_unreadable_recheck_changes_nothing_and_waits_out_the_interval(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An unreadable verdict is no verdict, and the tick must not raise over it. Re-read on the
    next tick, an outage costs the full price on every parked session."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    events = await event_types(world)
    calls = list(world.projector.calls)
    readable = world.project_github.state_source
    _fail_observation(world, "branches")

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert await event_types(world) == events
    assert world.projector.calls == calls

    world.use_github(state_source=readable)
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED

    await _recheck_due(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED


async def test_a_label_removed_as_the_recheck_comes_due_resumes_the_session_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The person's release is the newer word: the session they released must continue, not
    end under them."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    remove_label(world, LABEL_WAITING)

    await _recheck_due(world)

    assert len(adapter.resumed) == 1
    assert not any("while_parked" in d for d in await _completed(world))
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING


async def test_a_recheck_whose_run_state_is_gone_leaves_the_park_alone(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run whose local state is gone cannot be accepted, and must not raise: a raising tick
    every interval quarantines the ticket."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    adapter.result_error = UnknownRunError("no local run run-1")
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    events = await event_types(world)
    reads = world.github.repo_reads

    await _recheck_due(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert await event_types(world) == events
    assert world.github.repo_reads == reads


async def test_no_recheck_runs_past_the_workspace_retention(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Past retention the workspace is no longer kept, and a park left for weeks would be read
    every interval for ever."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    reads = world.github.repo_reads

    world.clock.advance(world.deps.sessions.retention + timedelta(seconds=1))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert world.github.repo_reads == reads


async def test_a_rework_after_a_recheck_accept_resumes_the_parked_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run accepted while parked still carries its session, so a rework continues it rather
    than starting the work over."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await _idle_parked(world, adapter)
    world.github.deliver_spec(world.issue_number)
    await _recheck_due(world)
    await run_tick(world)

    world.github.request_changes("reviewer-1")
    await run_tick(world)
    adapter.restart()
    await run_tick(world)

    assert adapter.spawn_attempts[1].resume_from == ResumeFrom(
        run_id="run-1", session_id="session-run-1"
    )
    assert EventType.SESSION_RESUME_SKIPPED not in await event_types(world)


# ------------------------------------------------------------- a person's quit from the terminal

_UNMET = 'branch_exists("spec/12-*") = false'
_NEED = "a branch starting with spec/12-"


async def _typed_and_quit(world: World, adapter: FakeAdapter, *, at_prompt: bool) -> None:
    await run_tick(world)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=6)
    if at_prompt:
        adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
        await run_tick(world)
        (run,) = await step_runs(world)
        assert run.status == StepRunStatus.IDLE
    adapter.quit_by_person()
    await run_tick(world)


async def _assert_exit_parked(world: World, adapter: FakeAdapter, *, at_prompt: bool) -> None:
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert LABEL_WAITING not in world.github.issue.labels
    (comment,) = _comments(world)
    assert "a person ended its session from the terminal" in comment
    assert f"still missing: {_NEED}" in comment
    assert await _details(world, EventType.AGENT_PARKED) == [
        {
            "step": "plan",
            "attempt": 1,
            "run_id": "run-1",
            "reason": "exited",
            "at_prompt": at_prompt,
            "unmet": [_UNMET],
            "needs": [_NEED],
        }
    ]
    types = await event_types(world)
    assert EventType.RETRY_SCHEDULED not in types
    assert EventType.POSTCONDITION_FAILED not in types
    assert adapter.finish_calls == []
    assert adapter.resumed == []
    assert len(adapter.spawns) == 1


async def test_a_session_a_person_quits_parks_under_interrupted_with_no_retry(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`/exit` typed by the person working with the session: failed and retried, their quit
    costs an attempt and a backoff, and the conversation they meant to come back to."""
    world, adapter = await interactive_world(maker)
    await _typed_and_quit(world, adapter, at_prompt=True)

    await _assert_exit_parked(world, adapter, at_prompt=True)
    (comment,) = _comments(world)
    assert comment == (
        "`plan` (attempt 1) is paused: a person ended its session from the terminal before it "
        f"was accepted (still missing: {_NEED}).\n\nThe session and its workspace are kept. "
        f"To resume it, remove the `{LABEL_INTERRUPTED}` label or press Resume on the run's "
        "terminal in the dashboard. The agent reads the comments posted since the session last "
        "started working."
    )


async def test_a_quit_mid_turn_parks_with_at_prompt_false(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Ctrl+C twice while the agent works: `at_prompt` is the park's truthful record, for the
    event log and the dashboard, of a session stopped mid-turn."""
    world, adapter = await interactive_world(maker)
    await _typed_and_quit(world, adapter, at_prompt=False)

    await _assert_exit_parked(world, adapter, at_prompt=False)


async def test_a_quit_seen_before_its_typing_is_recorded_waits_for_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The gateway records a burst seconds after its first key, and the CLI is gone within a
    second of the Enter: judged at once, a person's quit reads as nobody's and is retried."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    events = await event_types(world)
    adapter.quit_by_person()

    await run_tick(world)

    assert await event_types(world) == events
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert adapter.finish_calls == [] and adapter.resumed == []
    assert _comments(world) == []

    world.clock.advance(timedelta(seconds=5))
    await _gateway_wrote(
        world, EventType.TERMINAL_INPUT, world.clock.now - timedelta(seconds=3), bytes=6
    )
    await run_tick(world)

    await _assert_exit_parked(world, adapter, at_prompt=False)


async def test_an_unattended_quit_fails_on_its_postconditions_and_retries(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody typed, or the gateway was down: parked, the run waits for a person nobody told to
    look. Judged at the first sight, a person's keys still in flight are lost."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    events = await event_types(world)
    adapter.quit_by_person()
    await run_tick(world)
    world.clock.advance(EXIT_GRACE - timedelta(seconds=1))
    await run_tick(world)

    assert await event_types(world) == events
    assert adapter.finish_calls == []

    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)

    types = await event_types(world)
    assert types[-4:] == [
        EventType.AGENT_COMPLETED,
        EventType.POSTCONDITION_FAILED,
        EventType.AGENT_FAILED,
        EventType.RETRY_SCHEDULED,
    ]
    assert EventType.AGENT_PARKED not in types
    assert adapter.finish_calls == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert LABEL_INTERRUPTED not in world.github.issue.labels


async def test_a_quit_after_its_postconditions_hold_is_accepted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The work is done: held for the grace or parked, an accepted run waits on nothing. Judged
    as a clean end, it keeps what its result measured: the cost, the warnings, the session a
    rework continues."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.quit_by_person(cost_usd=Decimal("1.5"), stream_warnings=("no cost-state line",))

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert run.failure_reason is None
    assert run.cost_usd == Decimal("1.5")
    assert (await ticket_row(world)).current_step == "spec_gate"
    assert adapter.finish_calls == ["run-1"]
    assert EventType.AGENT_PARKED not in await event_types(world)
    (completed,) = await _details(world, EventType.AGENT_COMPLETED)
    assert completed["stream_warnings"] == ["no cost-state line"]
    assert completed["session_id"] == "session-run-1"


async def test_a_quit_with_an_interrupt_pending_is_the_persons_interrupt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Recorded as an exit park, the request stays unanswered and stops the session again the
    moment it is resumed."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    (run,) = await step_runs(world)
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
    adapter.quit_by_person()

    await run_tick(world)

    types = await event_types(world)
    assert EventType.AGENT_INTERRUPTED in types
    assert EventType.AGENT_PARKED not in types
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert LABEL_INTERRUPTED in world.github.issue.labels


async def test_a_quit_racing_a_finish_the_worker_sent_is_never_parked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The loop asked this session to end. On a host the finish ends the park too, its HEAD
    detached, so a park here would resume a session on no branch."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.finish_settles = False
    await _silent_stop(world, adapter)
    assert adapter.finish_calls == ["run-1"]
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=6)
    adapter.quit_by_person()

    await run_tick(world)

    types = await event_types(world)
    assert EventType.AGENT_PARKED not in types
    assert types[-3:] == [
        EventType.POSTCONDITION_FAILED,
        EventType.AGENT_FAILED,
        EventType.RETRY_SCHEDULED,
    ]
    assert LABEL_INTERRUPTED not in world.github.issue.labels


async def test_a_quit_with_no_typing_in_its_stretch_is_judged_as_before_even_after_a_release(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A release, or typing in an earlier stretch, does not say who ended this one: parked, the
    run would wait for a person nobody told to look."""
    world, adapter = await _exit_parked(maker)
    world.clock.advance(timedelta(minutes=1))
    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    assert len(adapter.resumed) == 1
    world.clock.advance(timedelta(minutes=1))
    adapter.quit_by_person()
    await run_tick(world)
    world.clock.advance(EXIT_GRACE)
    await run_tick(world)

    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == ["exited"]
    assert (await event_types(world))[-4:] == [
        EventType.AGENT_COMPLETED,
        EventType.POSTCONDITION_FAILED,
        EventType.AGENT_FAILED,
        EventType.RETRY_SCHEDULED,
    ]
    assert adapter.finish_calls == ["run-1"]


def _ci_settled(world: World, conclusion: str) -> None:
    world.github.check_runs_by_sha["sha-1"] = (
        CheckRunState(
            name="ci", status="completed", conclusion=conclusion, completed_at=world.clock.now
        ),
    )


async def _typed_and_quit_with_ci_running(world: World, adapter: FakeAdapter) -> None:
    await run_tick(world)
    _ci_running(world)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=6)
    adapter.quit_by_person()
    await run_tick(world)


async def _assert_held(world: World, adapter: FakeAdapter) -> None:
    assert EventType.AGENT_PARKED not in await event_types(world)
    assert adapter.finish_calls == []
    assert LABEL_INTERRUPTED not in world.github.issue.labels
    assert _comments(world) == []


async def test_an_engaged_quit_with_ci_pending_waits_and_is_accepted_when_ci_goes_green(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A person who pushed and quit left the rest to CI: parked before it answers, a run whose
    checks then pass would wait on a person to release work that was already done."""
    world, adapter = await interactive_world(maker, plan=_CI_PLAN)
    await _typed_and_quit_with_ci_running(world, adapter)
    world.clock.advance(timedelta(minutes=5))
    await run_tick(world)
    await _assert_held(world, adapter)

    _ci_settled(world, "success")
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert (await ticket_row(world)).current_step == "spec_gate"
    assert adapter.finish_calls == ["run-1"]
    assert EventType.AGENT_PARKED not in await event_types(world)


async def test_an_engaged_quit_with_ci_pending_parks_when_ci_fails(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker, plan=_CI_PLAN)
    await _typed_and_quit_with_ci_running(world, adapter)
    await _assert_held(world, adapter)

    _ci_settled(world, "failure")
    await run_tick(world)

    assert await _details(world, EventType.AGENT_PARKED) == [
        {
            "step": "plan",
            "attempt": 1,
            "run_id": "run-1",
            "reason": "exited",
            "at_prompt": False,
            "unmet": ['ci_green("spec/12-*") = false'],
            "needs": ["passing CI on a branch starting with spec/12-"],
        }
    ]
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert adapter.finish_calls == []
    assert EventType.RETRY_SCHEDULED not in await event_types(world)


async def test_an_engaged_quit_parks_once_the_step_timeout_passes_with_ci_still_pending(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A check stuck queued would otherwise hold a person's quit for good, with nothing on the
    ticket to say it stopped."""
    world, adapter = await interactive_world(maker, plan={**_CI_PLAN, "timeout": "30m"})
    spawned = world.clock.now
    await _typed_and_quit_with_ci_running(world, adapter)
    world.clock.now = spawned + timedelta(minutes=30) - timedelta(seconds=1)
    await run_tick(world)
    await _assert_held(world, adapter)

    world.clock.now = spawned + timedelta(minutes=30)
    await run_tick(world)

    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == ["exited"]
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert adapter.finish_calls == []


# ------------------------------------- releasing the park a person's quit left


async def _exit_parked(
    maker: async_sessionmaker[AsyncSession], **kw: Any
) -> tuple[World, FakeAdapter]:
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN, **kw)
    await _typed_and_quit(world, adapter, at_prompt=True)
    await _assert_exit_parked(world, adapter, at_prompt=True)
    return world, adapter


async def test_removing_the_label_resumes_a_session_a_person_quit_on_the_same_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _exit_parked(maker)

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)

    assert adapter.resumed == [("run-1", _EXITED_TURN)]
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "label"
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert run.attempt == 1
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert len(adapter.spawns) == 1
    assert EventType.RETRY_SCHEDULED not in await event_types(world)


async def test_an_exit_park_resumes_with_the_comments_posted_since(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _exit_parked(maker)
    world.github.add_issue_comment("a-human", "An old remark about MySQL.", _BEFORE)
    world.github.add_issue_comment("a-human", "Postgres, please.", _AFTER)

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert turn.startswith(_EXITED_OPENING)
    assert "Postgres, please." in turn
    assert "MySQL" not in turn


def _github_time(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


async def test_a_comment_posted_between_a_quit_and_its_park_reaches_the_resumed_turn(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The park waits for the gateway to record the keys, or for CI to answer: a person who
    said on the ticket why they quit, in that gap, said it to the resumed turn."""
    world, adapter = await interactive_world(maker, bot_login=BOT_LOGIN)
    await run_tick(world)
    spawned = world.clock.now
    world.github.add_issue_comment(
        "a-human", "An old remark about MySQL.", _github_time(spawned - timedelta(minutes=1))
    )
    adapter.quit_by_person()
    await run_tick(world)
    world.github.add_issue_comment(
        "a-human", "Postgres, please.", _github_time(spawned + timedelta(seconds=3))
    )
    world.clock.advance(timedelta(seconds=5))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, spawned + timedelta(seconds=2), bytes=6)
    await run_tick(world)
    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == ["exited"]

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)

    ((_, turn),) = adapter.resumed
    assert turn is not None
    assert turn.startswith(_EXITED_OPENING)
    assert "Postgres, please." in turn
    assert "MySQL" not in turn


async def test_an_mcp_resume_releases_an_exit_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _exit_parked(maker)
    (parked,) = await step_runs(world)
    async with world.sessions() as session:
        run = await session.get(StepRun, parked.id)
        assert run is not None
        result = await request_resume(
            session, run, Actor(by="ops@example.com", via=RequestedVia.MCP), answer=None
        )
    assert result.status == "requested"

    await run_tick(world)

    assert adapter.resumed == [("run-1", _EXITED_TURN)]
    (resumed,) = await _details(world, EventType.AGENT_RESUMED)
    assert resumed["via"] == "mcp"
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING


async def test_a_label_release_of_an_exit_park_does_not_open_a_ceiling_window(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(hours=2))
    )
    await run_tick(world)
    spawned = world.clock.now
    world.clock.now = spawned + timedelta(minutes=20)
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=6)
    adapter.quit_by_person()
    await run_tick(world)
    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == ["exited"]

    world.clock.now = spawned + timedelta(minutes=40)
    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    assert len(adapter.resumed) == 1
    while ParkReason.CEILING not in adapter.interrupt_reasons:
        assert world.clock.now - spawned < timedelta(hours=2, minutes=5), "the release moved it"
        world.clock.advance(timedelta(minutes=4))
        adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
        await run_tick(world)
        world.clock.advance(timedelta(minutes=1))
        adapter.go_active()
        await run_tick(world)

    (*_, parked) = await _details(world, EventType.AGENT_PARKED)
    assert parked["reason"] == "ceiling"


async def test_a_recheck_never_accepts_an_exit_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, _ = await _exit_parked(maker)
    reads = world.github.repo_reads
    world.github.deliver_spec(world.issue_number)

    await _recheck_due(world)
    await _recheck_due(world)

    assert EventType.AGENT_COMPLETED not in await event_types(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert world.github.repo_reads == reads


async def test_a_resumed_session_quit_again_parks_again_on_the_same_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(
        maker, bot_login=BOT_LOGIN, sessions=_ceiling(timedelta(hours=2))
    )
    spawned = world.clock.now
    await _typed_and_quit(world, adapter, at_prompt=True)
    await _assert_exit_parked(world, adapter, at_prompt=True)

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    assert len(adapter.resumed) == 1
    world.clock.advance(timedelta(minutes=1))
    await _gateway_wrote(world, EventType.TERMINAL_INPUT, world.clock.now, bytes=6)
    adapter.quit_by_person()
    await run_tick(world)

    assert [row["reason"] for row in await _details(world, EventType.AGENT_PARKED)] == [
        "exited",
        "exited",
    ]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    assert run.attempt == 1
    assert LABEL_INTERRUPTED in world.github.issue.labels
    assert EventType.RETRY_SCHEDULED not in await event_types(world)
    assert len(adapter.spawns) == 1

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    while ParkReason.CEILING not in adapter.interrupt_reasons:
        assert world.clock.now - spawned < timedelta(hours=2, minutes=5), "the release moved it"
        world.clock.advance(timedelta(minutes=4))
        adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
        await run_tick(world)
        world.clock.advance(timedelta(minutes=1))
        adapter.go_active()
        await run_tick(world)


async def test_a_refused_resume_of_an_exit_park_reparks_under_interrupted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _exit_parked(maker)
    adapter.resume_error = UnknownRunError("no local run run-1")

    remove_label(world, LABEL_INTERRUPTED)
    await run_tick(world)
    await run_tick(world)

    assert len(adapter.resumed) == 1
    assert len(adapter.spawns) == 1
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert {PLAN_LABEL, LABEL_INTERRUPTED} <= world.github.issue.labels
    assert any("Couldn't resume" in body for body in _comments(world))
