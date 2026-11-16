"""A step that waits for a person's approval in the dashboard: `request_approval()` reads the
approval of the run being accepted, at the commit its branch ends on now, and the session waits
for it at its prompt, parked or not.

Each failure is silent: an approval of an older commit or an earlier attempt would accept work
nobody looked at, and a stop ended as a failure, or an approval left unread for the re-check's
interval, makes the person who approved wait on nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.adapters.state import StateSource
from orchestrator.api.services import controls
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.results import RequestResult
from orchestrator.db.models import Event, StepRun, StepRunStatus
from orchestrator.events import EventType
from orchestrator.github import LABEL_WAITING, BranchState, TreeEntry
from runtime.contract import AgentRuntimeError, IdleReason, ParkReason, RunResult, RunStatus
from tests.api_harness import build_app, client_for, make_settings
from tests.reconcile_world import (
    BOT_LOGIN,
    FakeAdapter,
    World,
    event_rows,
    event_types,
    interactive_world,
    run_tick,
    step_runs,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

REVIEWED_PLAN = {
    "postconditions": {"all": ['branch_exists("spec/{ticket}-*")', "request_approval()"]},
}


async def _approve(
    world: World,
    run: StepRun,
    head_sha: str,
    *,
    behind: timedelta = timedelta(0),
    at: datetime | None = None,
) -> None:
    """What the dashboard's Approve records. Written directly: this module tests the loop. Dated
    `at`, else on the world's clock, which the comments' times are on, less `behind`."""
    async with world.sessions() as session:
        session.add(
            Event(
                project_id=world.project_id,
                ticket_id=world.ticket_id,
                step_run_id=run.id,
                type=str(EventType.STEP_APPROVED),
                details={
                    "step": run.step_name,
                    "attempt": run.attempt,
                    "head_sha": head_sha,
                    "branch": f"spec/{world.issue_number}-widget",
                    "approved_by": "reviewer@example.com",
                    "requested_via": "dashboard",
                },
                created_at=at or world.clock.now - behind,
            )
        )
        await session.commit()


async def _idle_for_a_minute(world: World, adapter: FakeAdapter) -> None:
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    for _ in range(2):
        world.clock.advance(timedelta(seconds=30))
        await run_tick(world)


async def test_an_approval_of_the_runs_current_head_accepts_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    (run,) = await step_runs(world)
    await _approve(world, run, "sha-1")

    await _idle_for_a_minute(world, adapter)

    assert adapter.finish_calls == ["run-1"]
    (run, *_) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED


async def test_an_approval_of_an_older_commit_accepts_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    (run,) = await step_runs(world)
    await _approve(world, run, "sha-0")

    await _idle_for_a_minute(world, adapter)

    assert adapter.finish_calls == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.IDLE


async def test_an_approval_of_attempt_one_does_not_accept_attempt_two(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN)
    await run_tick(world)
    adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)
    world.clock.advance(timedelta(minutes=10))
    adapter.restart()
    await run_tick(world)
    first, second = await step_runs(world)
    assert second.attempt == 2
    world.github.deliver_spec(world.issue_number)
    await _approve(world, first, "sha-1")

    await _idle_for_a_minute(world, adapter)

    assert adapter.finish_calls == []
    (_, second) = await step_runs(world)
    assert second.status == StepRunStatus.IDLE


async def _idle_view(world: World, auth: dict[str, str]) -> dict[str, Any]:
    (run,) = await step_runs(world)
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.get(f"/api/step-runs/{run.id}/terminal", headers=auth)
    assert response.status_code == 200
    return response.json()["idle"]


def _stamp(world: World) -> str:
    """The world's clock as GitHub writes a comment's time."""
    return world.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _details(world: World, event_type: EventType) -> list[dict[str, object]]:
    return [dict(row.details) for row in await event_rows(world) if row.type == event_type]


async def _parked_for_review(world: World, adapter: FakeAdapter) -> StepRun:
    """The agent pushed its work and stopped with no question; nobody approved it in time."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    # The first parked tick re-checks a run it has never stamped.
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    return run


@pytest.mark.parametrize("reviewed", [True, False])
async def test_a_stop_with_no_question_waits_for_a_review_and_ends_without_one(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str], reviewed: bool
) -> None:
    """An agent that says its work is ready asks nothing. Ended and failed, the step spends a
    retry on work that only waited for a person to look at it. A step that waits for nobody
    still ends such a stop and retries, and the terminal panel says which it will be."""
    plan = REVIEWED_PLAN if reviewed else {"postconditions": 'branch_exists("spec/{ticket}-*")'}
    world, adapter = await interactive_world(maker, plan=plan)
    await run_tick(world)
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    assert (LABEL_WAITING in world.github.issue.labels) is reviewed
    assert (await _idle_view(world, auth))["at_deadline"] == ("park" if reviewed else "retry")

    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    if reviewed:
        assert adapter.interrupt_reasons == [ParkReason.IDLE]
        assert adapter.finish_calls == []
        assert [d["reason"] for d in await _details(world, EventType.AGENT_PARKED)] == ["idle"]
    else:
        assert adapter.interrupt_reasons == []
        assert adapter.finish_calls == ["run-1"]
        (finishing,) = await _details(world, EventType.AGENT_FINISHING)
        assert finishing["silent"] is True
    assert (EventType.POSTCONDITION_FAILED in await event_types(world)) is not reviewed


async def test_an_approval_of_a_parked_run_is_read_at_the_next_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The person approved and expects the ticket to move. Left to the re-check's interval, it
    sits for minutes after the one event that could change its verdict. Dated before the last
    re-check, as a database clock behind the worker's dates it, it is still new to the loop."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, bot_login=BOT_LOGIN)
    run = await _parked_for_review(world, adapter)
    world.clock.advance(timedelta(minutes=1))
    await _approve(world, run, "sha-1", behind=timedelta(hours=1))

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    (completed,) = await _details(world, EventType.AGENT_COMPLETED)
    assert completed["while_parked"] == "idle"


async def test_an_approval_the_recheck_could_not_judge_is_asked_again_a_minute_later(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marked read on a tick whose substrate read failed, the approval waits out the re-check's
    interval although nothing judged it. Asked on every tick, a substrate that stays down is read
    every thirty seconds for the whole retention."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, bot_login=BOT_LOGIN)
    run = await _parked_for_review(world, adapter)
    world.clock.advance(timedelta(minutes=1))
    await _approve(world, run, "sha-1")
    adapter.result_error = AgentRuntimeError("the substrate is down")
    reads: list[str] = []
    result = adapter.result

    async def counted(run_id: str) -> RunResult:
        reads.append(run_id)
        return await result(run_id)

    monkeypatch.setattr(adapter, "result", counted)

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    assert reads == ["run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED

    adapter.result_error = None
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert reads == ["run-1", "run-1"]
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED


async def test_an_approval_the_recheck_has_read_waits_out_the_interval(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An approval of an older commit leaves the run unmet. Re-read on every parked tick, each
    such run spends a GitHub read every thirty seconds for as long as it waits."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, bot_login=BOT_LOGIN)
    run = await _parked_for_review(world, adapter)
    world.clock.advance(timedelta(minutes=1))
    await _approve(world, run, "sha-0")
    reads = world.github.repo_reads

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    assert world.github.repo_reads == reads + 1
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    await run_tick(world)

    assert world.github.repo_reads == reads + 1
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED


DESIGN = "docs/12-widget-design.md"
#: Also a match of `docs/12-*-design.md`: read with `{slug}` as `*`, the Design entry is ambiguous.
EARLIER_DESIGN = "docs/12-widget-v1-design.md"
DESIGN_PREVIEW = {"files": [{"path": "docs/{ticket}-{slug}-design.md", "label": "Design"}]}


def _seed_main(world: World) -> None:
    world.github.branches.append(BranchState(name="main", sha="m0"))
    world.github.trees["m0"] = (TreeEntry(path="README.md", sha="r0", size=10),)


async def _records(world: World) -> list[dict[str, object]]:
    return await _details(world, EventType.RUN_OUTPUTS_OBSERVED)


async def _design_entry(world: World, auth: dict[str, str]) -> dict[str, Any]:
    (run, *_) = await step_runs(world)
    async with (
        world.sessions() as session,
        client_for(build_app(make_settings(), session)) as client,
    ):
        response = await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)
    assert response.status_code == 200
    (entry,) = response.json()["entries"]
    return entry


async def test_a_review_step_records_its_files_at_each_idle_spell_and_at_its_end(
    maker: async_sessionmaker[AsyncSession], auth: dict[str, str]
) -> None:
    """The person reads the step's files before pressing Approve, while the run is still open.
    Recorded only at the run's end, the Files view is empty exactly then, or shows a commit
    the agent has since pushed past. The run has delivered no branch yet: the record names the
    one branch it read, so `{slug}` resolves and the Design entry is not ambiguous."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, preview=DESIGN_PREVIEW)
    _seed_main(world)
    branch = f"spec/{world.issue_number}-widget"
    await run_tick(world)
    world.github.push_commit(branch, "s1", {DESIGN: "b1", EARLIER_DESIGN: "b0"}, parent="m0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)

    (first,) = await _records(world)
    assert (first["status"], first["commit"], first["slug"]) == ("observed", "s1", "widget")
    entry = await _design_entry(world, auth)
    assert (entry["status"], entry["path"], entry["commit"]) == ("published", DESIGN, "s1")

    adapter.go_active()
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    # The fake compares a commit with its parent only; the run's start is still `m0`.
    world.github.push_commit(branch, "s2", {DESIGN: "b2", EARLIER_DESIGN: "b0"}, parent="m0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)

    assert [r["commit"] for r in await _records(world)] == ["s1", "s2"]
    entry = await _design_entry(world, auth)
    assert (entry["status"], entry["commit"]) == ("published", "s2")

    (run,) = await step_runs(world)
    await _approve(world, run, "s2")
    for _ in range(2):
        world.clock.advance(timedelta(seconds=30))
        await run_tick(world)

    (run, *_) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert [r["commit"] for r in await _records(world)] == ["s1", "s2", "s2"]


async def test_a_spell_github_would_not_answer_for_is_asked_again_until_it_is_recorded(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The spell is announced once. Asked only then, one GitHub error leaves the Files view empty
    for the whole wait; asked on every tick after an answer, each waiting run spends a GitHub
    comparison every thirty seconds."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, preview=DESIGN_PREVIEW)
    _seed_main(world)
    await run_tick(world)
    world.github.push_commit(f"spec/{world.issue_number}-widget", "s1", {DESIGN: "b1"}, parent="m0")
    world.github.compare_error = RuntimeError("GitHub is down")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    assert await _records(world) == []

    world.github.compare_error = None
    for _ in range(2):
        world.clock.advance(timedelta(seconds=30))
        await run_tick(world)

    assert [r["commit"] for r in await _records(world)] == ["s1"]
    assert world.github.compare_calls == [("m0", "s1"), ("m0", "s1")]


async def test_a_step_that_awaits_no_review_records_its_files_only_at_its_end(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every other step keeps one record per run: a record per idle spell spends GitHub reads
    on a view nobody waits on."""
    plan = {"postconditions": 'branch_exists("never/{ticket}-*")'}
    world, adapter = await interactive_world(maker, plan=plan, preview=DESIGN_PREVIEW)
    _seed_main(world)
    await run_tick(world)
    world.github.push_commit(f"spec/{world.issue_number}-widget", "s1", {DESIGN: "b1"}, parent="m0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)

    assert await _records(world) == []

    adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)

    assert [r["commit"] for r in await _records(world)] == ["s1"]


@pytest.mark.parametrize("comment_first", [True, False])
async def test_a_comment_holds_a_parked_review_only_when_it_is_newer_than_the_approval(
    maker: async_sessionmaker[AsyncSession], comment_first: bool
) -> None:
    """A reviewer who comments and then approves has read their own comment. Held, the run waits
    for a release nobody means to give. A comment after the approval is a person's newer word:
    accepted over it, the ticket moves on before the agent reads it."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, bot_login=BOT_LOGIN)
    run = await _parked_for_review(world, adapter)
    world.clock.advance(timedelta(minutes=1))
    if comment_first:
        world.github.add_issue_comment("a-human", "Looks good.", _stamp(world))
        world.clock.advance(timedelta(minutes=1))
    await _approve(world, run, "sha-1")
    if not comment_first:
        world.clock.advance(timedelta(minutes=1))
        world.github.add_issue_comment("a-human", "Wait, one more thing.", _stamp(world))

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    (run,) = await step_runs(world)
    if comment_first:
        assert run.status == StepRunStatus.SUCCEEDED
    else:
        assert run.status == StepRunStatus.INTERRUPTED
        assert EventType.AGENT_COMPLETED not in await event_types(world)


async def _press(world: World, run: StepRun, head_sha: str) -> RequestResult:
    """The dashboard's Approve, through the service, against the fake forge's branches now."""

    async def observe() -> StateSource | None:
        return world.github.snapshot(world.github.repo, world.issue_number)

    async with world.sessions() as session:
        fresh = await session.get(StepRun, run.id)
        assert fresh is not None
        return await controls.request_approval(
            session,
            fresh,
            Actor.dashboard("reviewer@example.com"),
            head_sha=head_sha,
            observed=observe,
            # The world's clock is months behind the service's.
            retention=timedelta(days=3650),
        )


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


@pytest.mark.parametrize("pushed", [True, False])
async def test_a_park_names_approve_only_when_the_run_has_pushed(
    maker: async_sessionmaker[AsyncSession], pushed: bool
) -> None:
    """On a lane whose steps share a branch, the branch has a head before the run pushes. Told to
    read the work and press Approve, a person approves the earlier step's commit for a run that
    only asked a question or failed its turn."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, preview=DESIGN_PREVIEW)
    _seed_main(world)
    branch = f"spec/{world.issue_number}-widget"
    # The earlier step's push, where this run's spawn finds the branch.
    world.github.push_commit(branch, "s0", {DESIGN: "b0"}, parent="m0")
    await run_tick(world)
    if pushed:
        world.github.push_commit(branch, "s1", {DESIGN: "b1"}, parent="s0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    (comment,) = _comments(world)
    assert ("press Approve on the ticket's page in the dashboard" in comment) is pushed
    assert ("To ask for changes, answer here" in comment) is pushed
    assert ("To resume it, answer here" in comment) is not pushed


async def test_a_branch_that_moves_while_its_files_are_read_holds_approve_until_they_are_read(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A push, a bot or "Update branch" moves the branch while the person reads the Files view.
    Read once per idle spell, the view still shows the old commit while Approve records the new
    one, which the person never saw."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, preview=DESIGN_PREVIEW)
    _seed_main(world)
    branch = f"spec/{world.issue_number}-widget"
    await run_tick(world)
    world.github.push_commit(branch, "s1", {DESIGN: "b1"}, parent="m0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.github.push_commit(branch, "s2", {DESIGN: "b2"}, parent="m0")
    (run,) = await step_runs(world)

    with pytest.raises(Conflict) as refused:
        await _press(world, run, "s2")
    assert refused.value.code == "files_stale"

    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    assert [r["commit"] for r in await _records(world)] == ["s1", "s2"]
    compared = len(world.github.compare_calls)
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    # A branch that has not moved again is not read again.
    assert len(world.github.compare_calls) == compared

    assert (await _press(world, run, "s2")).status == "requested"


async def test_a_second_approval_after_a_comment_accepts_the_parked_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A comment after the approval holds the run. Pressed again by the same person at the same
    head and read as a double press, the approval is never renewed: the run stays parked and the
    dashboard says Approved by until someone presses Resume."""
    world, adapter = await interactive_world(maker, plan=REVIEWED_PLAN, bot_login=BOT_LOGIN)
    run = await _parked_for_review(world, adapter)
    now = datetime.now(UTC)
    await _approve(world, run, "sha-1", at=now - timedelta(minutes=2))
    world.github.add_issue_comment(
        "a-human", "One thing first.", (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED

    assert (await _press(world, run, "sha-1")).status == "requested"
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED


async def test_a_parked_runs_files_catch_up_with_its_branch_at_the_recheck(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A steady parked tick reads no forge. Never read again, a parked run whose branch moved is
    refused Approve as `files_stale` until someone resumes it."""
    world, adapter = await interactive_world(
        maker, plan=REVIEWED_PLAN, preview=DESIGN_PREVIEW, bot_login=BOT_LOGIN
    )
    _seed_main(world)
    branch = f"spec/{world.issue_number}-widget"
    await run_tick(world)
    world.github.push_commit(branch, "s1", {DESIGN: "b1"}, parent="m0")
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.INTERRUPTED
    world.github.push_commit(branch, "s2", {DESIGN: "b2"}, parent="m0")

    world.clock.advance(world.deps.sessions.parked_recheck)
    await run_tick(world)

    assert [r["commit"] for r in await _records(world)] == ["s1", "s2"]
    assert (await _press(world, run, "s2")).status == "requested"
