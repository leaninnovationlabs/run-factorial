"""A merging gate's conflict arm: it reads its pull request's mergeability, records and announces
a conflict once per head, and sends the work back to an agent step to fix it, by itself or on a
person's request. A send-back is never a reviewer's rejection."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.adapters.state import Mergeability
from orchestrator.config import AgentStep, ProcessConfig
from orchestrator.db.models import (
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.events.writer import append
from orchestrator.github import MergeRefused
from orchestrator.worker.gate_conflicts import AUTOMATIC_FIX_LIMIT
from orchestrator.worker.rework import _rejecting_gates
from orchestrator.worker.tick_context import _Tick
from tests.reconcile_world import (
    World,
    _build_world,
    _process,
    _process_with_gate,
    _process_with_spec_merge_gate,
    _process_without_gate_reject,
    advance_to_gate,
    entered_events,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def _process_with_on_conflict(name: str) -> ProcessConfig:
    return _process_with_gate(
        name,
        base=_process(name),
        onConflict={"rerunStep": "plan", "ticketLabel": "factorial:conflict-fix"},
    )


@pytest.fixture
async def world_with_on_conflict(maker: async_sessionmaker[AsyncSession]) -> World:
    return await _build_world(maker, config=_process_with_on_conflict(f"oc-{uuid4().hex[:8]}"))


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def _rows(world: World, event_type: EventType) -> list[Event]:
    return [row for row in await event_rows(world) if row.type == event_type]


async def _gate(world: World, name: str = "spec_gate") -> StepRun:
    return [run for run in await step_runs(world) if run.step_name == name][-1]


async def _request_fix(world: World) -> int:
    """What the dashboard's conflict-fix route records against the waiting gate run."""
    gate = await _gate(world)
    async with world.sessions() as session:
        event = await append(
            session,
            type=EventType.CONFLICT_FIX_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=gate.id,
            step=gate.step_name,
            attempt=gate.attempt,
            requested_by="reviewer@example.com",
            idempotency_key=uuid4().hex,
        )
        return event.id


def _later(world: World) -> None:
    """Past the mergeability memo, so the next tick reads the forge again."""
    world.clock.advance(timedelta(minutes=3))


def _becomes(world: World, state: Mergeability) -> None:
    """The forge's answer changes, and the gate's last reading is older than the memo keeps."""
    world.github.mergeability = state
    _later(world)


# ------------------------------------------------------------------------- observing and saying


async def test_a_conflict_is_recorded_and_commented_once_per_head(world: World) -> None:
    await advance_to_gate(world)
    world.projector.calls.clear()
    _becomes(world, Mergeability.CONFLICTING)

    await run_tick(world)
    _later(world)
    await run_tick(world)

    (recorded,) = await _rows(world, EventType.GATE_MERGEABILITY)
    assert recorded.details["state"] == "conflicting"
    assert recorded.details["head_sha"] == "sha-1"
    assert recorded.details["base_ref"] == "main"
    (comment,) = _comments(world)
    assert "https://github.test/pr/101" in comment
    assert "conflicts with `main`" in comment
    # No `onConflict`: nothing starts by itself, and the gate waits.
    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.WAITING_GATE
    assert (await _gate(world)).status == StepRunStatus.WAITING

    world.github.push(101, "sha-2")
    await run_tick(world)
    assert len(await _rows(world, EventType.GATE_MERGEABILITY)) == 2
    assert len(_comments(world)) == 2


async def test_unknown_is_neither_recorded_nor_acted_on(world_with_on_conflict: World) -> None:
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.projector.calls.clear()
    reads = world.github.mergeability_reads
    _becomes(world, Mergeability.UNKNOWN)

    await run_tick(world)
    await run_tick(world)

    assert await _rows(world, EventType.GATE_MERGEABILITY) == []
    assert _comments(world) == []
    assert (await _gate(world)).status == StepRunStatus.WAITING
    # Never kept: each tick asks again, inside the recheck window too.
    assert world.github.mergeability_reads == reads + 2


async def test_clean_is_recorded_only_after_a_conflict(world: World) -> None:
    await advance_to_gate(world)
    world.projector.calls.clear()
    await run_tick(world)
    assert await _rows(world, EventType.GATE_MERGEABILITY) == []

    _becomes(world, Mergeability.CONFLICTING)
    await run_tick(world)
    # A person merges `main` into the branch by hand: a new head, and it merges.
    world.github.push(101, "sha-2")
    world.github.mergeability = Mergeability.CLEAN
    await run_tick(world)

    states = [row.details["state"] for row in await _rows(world, EventType.GATE_MERGEABILITY)]
    assert states == ["conflicting", "clean"]
    assert len(_comments(world)) == 1


async def test_mergeability_is_read_once_inside_the_recheck_window(world: World) -> None:
    await advance_to_gate(world)
    reads = world.github.mergeability_reads
    _becomes(world, Mergeability.CONFLICTING)

    await run_tick(world)
    await run_tick(world)
    assert world.github.mergeability_reads == reads + 1

    _later(world)
    await run_tick(world)
    assert world.github.mergeability_reads == reads + 2

    # The memo lives as long as the worker: a gate run's entry goes when the run ends.
    gate = await _gate(world)
    assert gate.id in world.deps.mergeability_seen
    _becomes(world, Mergeability.CLEAN)
    world.github.approve("reviewer-1")
    await run_tick(world)
    assert (await _gate(world)).status == StepRunStatus.APPROVED
    assert gate.id not in world.deps.mergeability_seen


async def test_a_gate_that_does_not_merge_never_reads_mergeability(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker,
        config=_process_with_gate(
            f"nomerge-{uuid4().hex[:8]}",
            onApprove={"merge": False, "ticketLabel": "factorial:done"},
        ),
    )
    world.github.mergeability = Mergeability.CONFLICTING
    await advance_to_gate(world)
    await run_tick(world)

    assert world.github.mergeability_reads == 0
    assert await _rows(world, EventType.GATE_MERGEABILITY) == []


# ------------------------------------------------------------------------------ sending back


async def test_on_conflict_sends_the_gate_back_by_itself(world_with_on_conflict: World) -> None:
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.projector.calls.clear()
    _becomes(world, Mergeability.CONFLICTING)

    await run_tick(world)

    gate = await _gate(world)
    assert gate.status == StepRunStatus.REJECTED
    assert gate.ended_at is not None
    ticket = await ticket_row(world)
    assert ticket.current_step == "plan"
    assert ticket.status == TicketStatus.RUNNING
    (sent,) = await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)
    assert sent.step_run_id == gate.id
    assert {key: sent.details[key] for key in sent.details if key not in ("step", "attempt")} == {
        "pr_number": 101,
        "head_ref": "spec/12-widget",
        "base_ref": "main",
        "head_sha": "sha-1",
        "state": "conflicting",
        "target_step": "plan",
        "request_id": None,
        "requested_by": None,
    }
    assert "gate_rejected" not in await event_types(world)
    entered = (await entered_events(world))[-1]
    assert (entered["step"], entered["from_step"], entered["reason"]) == (
        "plan",
        "spec_gate",
        "conflict_fix",
    )
    # `onConflict`'s own outcome applies.
    assert world.github.issue.labels == frozenset({"factorial:conflict-fix"})


async def test_a_branch_only_behind_waits_for_the_button_even_with_on_conflict(
    world_with_on_conflict: World,
) -> None:
    """On a repository that requires up-to-date branches the forge often says "behind" only
    after an approval: an automatic start would discard that approval, again on every merge to
    the target."""
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.projector.calls.clear()
    _becomes(world, Mergeability.BEHIND)

    await run_tick(world)
    _later(world)
    await run_tick(world)

    assert (await _gate(world)).status == StepRunStatus.WAITING
    assert await _rows(world, EventType.GATE_CONFLICT_SENT_BACK) == []
    (recorded,) = await _rows(world, EventType.GATE_MERGEABILITY)
    assert recorded.details["state"] == "behind"
    (said,) = [body for body in _comments(world) if "is behind `main`" in body]
    assert "Press Update branch on the ticket page to send it to `plan`" in said
    assert "sends it to" not in said

    request_id = await _request_fix(world)
    await run_tick(world)

    assert (await _gate(world)).status == StepRunStatus.REJECTED
    (sent,) = await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)
    assert (sent.details["state"], sent.details["request_id"]) == ("behind", request_id)
    assert (await ticket_row(world)).current_step == "plan"


async def test_the_button_is_honoured_without_on_conflict(world: World) -> None:
    await advance_to_gate(world)
    _becomes(world, Mergeability.CONFLICTING)
    await run_tick(world)
    assert (await _gate(world)).status == StepRunStatus.WAITING

    request_id = await _request_fix(world)
    await run_tick(world)

    assert (await _gate(world)).status == StepRunStatus.REJECTED
    (sent,) = await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)
    assert sent.details["request_id"] == request_id
    assert sent.details["requested_by"] == "reviewer@example.com"
    assert sent.details["target_step"] == "plan"
    assert (await ticket_row(world)).current_step == "plan"
    assert "gate_rejected" not in await event_types(world)


async def test_the_button_is_refused_when_the_pull_request_is_mergeable(world: World) -> None:
    await advance_to_gate(world)
    request_id = await _request_fix(world)

    await run_tick(world)
    await run_tick(world)

    (refused,) = await _rows(world, EventType.CONFLICT_FIX_REFUSED)
    assert refused.details["request_id"] == request_id
    assert refused.details["reason"] == "mergeable"
    assert (await _gate(world)).status == StepRunStatus.WAITING


async def test_a_request_waits_while_mergeability_is_unknown(world: World) -> None:
    await advance_to_gate(world)
    world.github.mergeability = Mergeability.UNKNOWN
    request_id = await _request_fix(world)

    await run_tick(world)
    assert "conflict_fix_refused" not in await event_types(world)
    assert (await _gate(world)).status == StepRunStatus.WAITING

    world.github.mergeability = Mergeability.CONFLICTING
    await run_tick(world)
    (sent,) = await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)
    assert sent.details["request_id"] == request_id


@pytest.mark.parametrize("case", ["no_fix_step", "no_pull_request", "already_merged"])
async def test_a_request_the_gate_cannot_act_on_is_refused_with_its_reason(
    maker: async_sessionmaker[AsyncSession], case: str
) -> None:
    config = (
        _process_without_gate_reject(f"nofix-{uuid4().hex[:8]}")
        if case == "no_fix_step"
        else _process(f"refuse-{uuid4().hex[:8]}")
    )
    world = await _build_world(maker, config=config)
    await advance_to_gate(world)
    world.github.mergeability = Mergeability.CONFLICTING
    if case == "no_pull_request":
        world.github.close_pr_unmerged()
    elif case == "already_merged":
        world.github.merge_externally()
    request_id = await _request_fix(world)

    await run_tick(world)

    (refused,) = await _rows(world, EventType.CONFLICT_FIX_REFUSED)
    assert (refused.details["request_id"], refused.details["reason"]) == (request_id, case)
    assert await _rows(world, EventType.GATE_CONFLICT_SENT_BACK) == []


async def test_a_branch_name_a_command_line_acts_on_keeps_the_ticket_at_its_gate(
    world_with_on_conflict: World,
) -> None:
    """Git allows `$(...)` in a ref, and a fix run's agent writes the name on a command line."""
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.github.pulls = [
        replace(pr, head_ref="spec/12-a$(id)") if pr.head_ref == "spec/12-widget" else pr
        for pr in world.github.pulls
    ]
    _becomes(world, Mergeability.CONFLICTING)

    await run_tick(world)

    # `onConflict` is set, and still nothing is sent back: a person merges by hand.
    assert await _rows(world, EventType.GATE_CONFLICT_SENT_BACK) == []
    assert (await _gate(world)).status == StepRunStatus.WAITING
    (said,) = [body for body in _comments(world) if "conflicts with `main`" in body]
    assert "An agent cannot fix a branch whose name has characters a command line acts on" in said
    assert "Press Fix conflicts" not in said

    request_id = await _request_fix(world)
    await run_tick(world)

    (refused,) = await _rows(world, EventType.CONFLICT_FIX_REFUSED)
    assert (refused.details["request_id"], refused.details["reason"]) == (
        request_id,
        "unsafe_branch",
    )
    assert await _rows(world, EventType.GATE_CONFLICT_SENT_BACK) == []


async def test_a_request_left_on_an_earlier_gate_run_is_refused_as_newer_attempt(
    world: World,
) -> None:
    await advance_to_gate(world)
    request_id = await _request_fix(world)
    first = await _gate(world)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await session.execute(
            update(StepRun)
            .where(StepRun.id == first.id)
            .values(status=StepRunStatus.REJECTED, ended_at=first.started_at)
        )
        session.add(_gate_run(ticket, attempt=2, status=StepRunStatus.WAITING))

    await run_tick(world)

    (refused,) = await _rows(world, EventType.CONFLICT_FIX_REFUSED)
    assert (refused.details["request_id"], refused.details["reason"]) == (
        request_id,
        "newer_attempt",
    )


async def test_a_request_pending_when_the_approval_merges_is_refused_as_merged(
    world: World,
) -> None:
    """The gate never ticks again after it advances, so the approval answers the request."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    _becomes(world, Mergeability.UNKNOWN)
    request_id = await _request_fix(world)

    await run_tick(world)
    await run_tick(world)

    assert (await _gate(world)).status == StepRunStatus.APPROVED
    (refused,) = await _rows(world, EventType.CONFLICT_FIX_REFUSED)
    assert (refused.details["request_id"], refused.details["reason"]) == (
        request_id,
        "already_merged",
    )


# --------------------------------------------------------------------------- the budget


def _gate_run(ticket: Ticket, *, attempt: int, status: StepRunStatus) -> StepRun:
    return StepRun(
        ticket_id=ticket.id,
        step_name="spec_gate",
        kind=StepKind.HUMAN_GATE,
        attempt=attempt,
        status=status,
        generation=ticket.generation,
        started_at=None,
    )


async def _seed_gate_history(world: World, endings: list[str]) -> None:
    """Replace the waiting gate run with ended runs, one per ending, then a waiting one.

    `auto` is a send-back the loop started, `asked` one a person's request started, `approved`
    any other end."""
    first = await _gate(world)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        runs = [first]
        for attempt in range(2, len(endings) + 2):
            run = _gate_run(ticket, attempt=attempt, status=StepRunStatus.WAITING)
            session.add(run)
            runs.append(run)
        await session.flush()
        for run, ending in zip(runs[:-1], endings, strict=True):
            status = StepRunStatus.APPROVED if ending == "approved" else StepRunStatus.REJECTED
            await session.execute(
                update(StepRun).where(StepRun.id == run.id).values(status=status)
            )
            if ending == "approved":
                continue
            await append(
                session,
                type=EventType.GATE_CONFLICT_SENT_BACK,
                project_id=world.project_id,
                ticket_id=world.ticket_id,
                step_run_id=run.id,
                step="spec_gate",
                attempt=run.attempt,
                pr_number=101,
                head_ref="spec/12-widget",
                base_ref="main",
                head_sha="sha-0",
                state="conflicting",
                target_step="plan",
                request_id=None if ending == "auto" else 1,
                requested_by=None if ending == "auto" else "reviewer@example.com",
            )


async def test_automatic_send_back_stops_after_three_in_a_row(
    world_with_on_conflict: World,
) -> None:
    world = world_with_on_conflict
    await advance_to_gate(world)
    await _seed_gate_history(world, ["auto"] * AUTOMATIC_FIX_LIMIT)
    world.projector.calls.clear()
    world.github.mergeability = Mergeability.CONFLICTING

    await run_tick(world)
    _later(world)
    await run_tick(world)

    waiting = await _gate(world)
    assert waiting.attempt == AUTOMATIC_FIX_LIMIT + 1
    assert waiting.status == StepRunStatus.WAITING
    sent_back = {row.step_run_id for row in await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)}
    assert waiting.id not in sent_back
    limit = [body for body in _comments(world) if "3 times in a row" in body]
    assert len(limit) == 1
    assert "waits for a person" in limit[0]

    # The button is not limited.
    request_id = await _request_fix(world)
    await run_tick(world)
    assert (await _gate(world)).status == StepRunStatus.REJECTED
    sent = (await _rows(world, EventType.GATE_CONFLICT_SENT_BACK))[-1]
    assert (sent.step_run_id, sent.details["request_id"]) == (waiting.id, request_id)


@pytest.mark.parametrize("breaker", ["approved", "asked"])
async def test_a_run_that_ended_another_way_breaks_the_streak(
    world_with_on_conflict: World, breaker: str
) -> None:
    world = world_with_on_conflict
    await advance_to_gate(world)
    await _seed_gate_history(world, ["auto", breaker, "auto", "auto"])
    world.github.mergeability = Mergeability.CONFLICTING

    await run_tick(world)

    waiting = await _gate(world)
    assert waiting.status == StepRunStatus.REJECTED
    sent = (await _rows(world, EventType.GATE_CONFLICT_SENT_BACK))[-1]
    assert (sent.step_run_id, sent.details["request_id"]) == (waiting.id, None)


# ---------------------------------------------------------------------- approvals and merging


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("bound", [True, False])
async def test_no_automatic_fix_where_a_push_keeps_approvals(
    world_with_on_conflict: World, bound: bool, approved: bool
) -> None:
    """Where a push keeps an approval (GitLab), one given while an automatic fix ran would count
    on a merge nobody read, so the gate never starts a fix by itself, approved or not. Where a
    push voids it (GitHub), the fix starts by itself."""
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.github.approvals_bound_to_head = bound
    if approved:
        world.github.approve("reviewer-1")
    _becomes(world, Mergeability.CONFLICTING)
    world.projector.refuse_merge = MergeRefused(405, "conflict")

    await run_tick(world)

    gate = await _gate(world)
    if bound:
        assert gate.status == StepRunStatus.REJECTED
        assert 101 not in world.projector.merge_shas
    else:
        assert gate.status == StepRunStatus.WAITING
        assert await _rows(world, EventType.GATE_CONFLICT_SENT_BACK) == []
        held = "An approval can still count after a push on this forge, so the fix does not start"
        (said,) = [body for body in _comments(world) if held in body]
        assert "Fix conflicts" in said


async def test_the_button_sends_back_where_a_push_keeps_approvals(
    world_with_on_conflict: World,
) -> None:
    world = world_with_on_conflict
    await advance_to_gate(world)
    world.github.approvals_bound_to_head = False
    _becomes(world, Mergeability.CONFLICTING)
    await run_tick(world)
    assert (await _gate(world)).status == StepRunStatus.WAITING

    request_id = await _request_fix(world)
    await run_tick(world)

    assert (await _gate(world)).status == StepRunStatus.REJECTED
    (sent,) = await _rows(world, EventType.GATE_CONFLICT_SENT_BACK)
    assert sent.details["request_id"] == request_id
    assert (await ticket_row(world)).current_step == "plan"


async def test_an_approval_on_a_conflicting_head_says_the_conflict_not_a_promise(
    world: World,
) -> None:
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    _becomes(world, Mergeability.CONFLICTING)
    world.projector.refuse_merge = MergeRefused(405, "Pull Request is not mergeable")
    world.projector.calls.clear()

    await run_tick(world)

    (refused,) = await _rows(world, EventType.GATE_MERGE_REFUSED)
    assert refused.details["reason"] == "Pull Request is not mergeable"
    (said,) = [body for body in _comments(world) if "refused to merge" in body]
    assert "conflicts with `main`" in said
    assert "Fix conflicts" in said
    assert "as soon as" not in said

    # A refused merge forgets the reading, so the next tick asks the forge again.
    reads = world.github.mergeability_reads
    await run_tick(world)
    assert world.github.mergeability_reads == reads + 1


async def test_only_a_new_refusal_forgets_the_mergeability_reading(world: World) -> None:
    """The same refusal on every tick, a check still running, must not cost a forge read each."""
    await advance_to_gate(world)
    world.github.approve("reviewer-1")
    _becomes(world, Mergeability.CONFLICTING)
    world.projector.refuse_merge = MergeRefused(405, "Pull Request is not mergeable")
    await run_tick(world)
    await run_tick(world)
    reads = world.github.mergeability_reads

    await run_tick(world)
    assert world.github.mergeability_reads == reads

    world.projector.refuse_merge = MergeRefused(405, "Required status check is expected")
    await run_tick(world)
    await run_tick(world)
    assert world.github.mergeability_reads == reads + 1


# ------------------------------------------------------------------------ not a rejection


def _two_gates_sending_back_to_build(name: str) -> ProcessConfig:
    """`code_gate` and `spec_merge_gate` both send rework to `build`."""
    cfg = _process_with_spec_merge_gate(name).model_dump(mode="json", by_alias=True)
    merge_gate = next(step for step in cfg["steps"] if step["name"] == "spec_merge_gate")
    merge_gate["gate"]["onReject"] = {"rerunStep": "build"}
    return ProcessConfig.model_validate(cfg)


async def test_a_conflict_send_back_is_not_a_rejection_for_the_rework_window(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    config = _two_gates_sending_back_to_build(f"two-{uuid4().hex[:8]}")
    world = await _build_world(maker, config=config)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        for name, answer in (
            ("code_gate", EventType.GATE_REJECTED),
            ("spec_merge_gate", EventType.GATE_CONFLICT_SENT_BACK),
        ):
            run = StepRun(
                ticket_id=ticket.id,
                step_name=name,
                kind=StepKind.HUMAN_GATE,
                attempt=1,
                status=StepRunStatus.REJECTED,
                generation=ticket.generation,
            )
            session.add(run)
            await session.flush()
            await append(
                session,
                type=answer,
                project_id=world.project_id,
                ticket_id=ticket.id,
                step_run_id=run.id,
                step=name,
                attempt=1,
            )

    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        tick: Any = SimpleNamespace(session=session, ticket=ticket, config=config)
        build = config.step("build")
        assert isinstance(build, AgentStep)
        window = await _rejecting_gates(cast(_Tick, tick), build, attempt=2)

    assert [rejection.gate.name for rejection in window] == ["code_gate"]
