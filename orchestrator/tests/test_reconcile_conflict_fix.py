"""A conflict fix: inside the window a merging gate's send-back opens, the fix step's attempt is
a fresh session on the pull request's own branch, told the orchestrator's instructions, and its
retry budget counts from the send-back. It is accepted when the pull request's head moved and it
merges cleanly, and the ticket goes straight back to the gate that sent it.

Every failure here is silent. A fix that resumes the step's kept session, or checks out the
step's own branch, works on the wrong branch in a stacked lane; one that keeps the step's own
instructions builds again; one that counts the step's earlier attempts escalates a fix that
never got its budget; one judged by the step's own postconditions can never pass in a stacked
lane; and one accepted onward to `build.next` reopens a gate that already merged."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.adapters.state import Mergeability
from orchestrator.config import AgentStep, ProcessConfig
from orchestrator.db.models import StepKind, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType
from orchestrator.events.writer import append
from orchestrator.github import BranchState, PullRequestState
from orchestrator.worker.gate_conflicts import AUTOMATIC_FIX_LIMIT
from orchestrator.worker.rework import ConflictFix, _conflict_fix
from orchestrator.worker.tick_context import _load_pinned_config, _Tick
from runtime.contract import RunSpec, RunStatus
from tests.reconcile_world import (
    RESOLVED_AGENT,
    World,
    _build_world,
    _process_with_spec_merge_gate,
    _remove_blocked_label,
    entered_events,
    event_rows,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_BACKOFF_PASSED = timedelta(minutes=11)


def _stacked_lane(name: str, *, build_on_start: dict[str, str] | None = None) -> ProcessConfig:
    """The stacked lane: `build` works on `impl/...` stacked on the spec branch, and
    `spec_merge_gate`, which merges the spec pull request into `main`, sends a conflict back to
    `build`."""
    return ProcessConfig.model_validate(
        {
            "specVersion": "factorial/v1",
            "kind": "Process",
            "name": name,
            "version": 1,
            "defaults": {"timeout": "1h", "retry": {"maxAttempts": 2, "backoff": "10m"}},
            "entry": {
                "when": {"all": ['ticket_label("factorial:new")', "ticket_has_assignee()"]},
                "step": "plan",
            },
            "steps": [
                {
                    "name": "plan",
                    "kind": "agent",
                    "agent": "plan-agent",
                    "next": "spec_review_gate",
                    "workspace": {
                        "repo": "{repo}",
                        "base": "main",
                        "branch": "spec/{ticket}-{slug}",
                    },
                    "postconditions": 'pr_open("spec/{ticket}-*", "main")',
                },
                {
                    "name": "spec_review_gate",
                    "kind": "human_gate",
                    "next": "build",
                    "gate": {
                        "type": "pr_approval",
                        "pr": {"head": "spec/{ticket}-*", "base": "main"},
                        "approvers": ["reviewer-1"],
                        "onApprove": {"merge": False},
                    },
                },
                {
                    "name": "build",
                    "kind": "agent",
                    "agent": "plan-agent",
                    "next": "code_review_gate",
                    "workspace": {
                        "repo": "{repo}",
                        "base": "spec/{ticket}-{slug}",
                        "branch": "impl/{ticket}-{slug}",
                    },
                    "inputs": ["ticket_body"],
                    "retry": {"maxAttempts": 3, "backoff": "10m"},
                    "postconditions": 'pr_open("impl/{ticket}-*", "spec/{ticket}-*")',
                    **({"onStart": build_on_start} if build_on_start else {}),
                },
                {
                    "name": "code_review_gate",
                    "kind": "human_gate",
                    "next": "spec_merge_gate",
                    "gate": {
                        "type": "pr_approval",
                        "pr": {"head": "impl/{ticket}-*", "base": "spec/{ticket}-*"},
                        "approvers": ["reviewer-1"],
                        "onApprove": {"merge": True},
                        "onReject": {"rerunStep": "build"},
                    },
                },
                {
                    "name": "spec_merge_gate",
                    "kind": "human_gate",
                    "next": "done",
                    "gate": {
                        "type": "pr_approval",
                        "pr": {"head": "spec/{ticket}-*", "base": "main"},
                        "approvers": ["reviewer-2"],
                        "onApprove": {"merge": True},
                        "onConflict": {"rerunStep": "build"},
                    },
                },
            ],
            "terminal": {"step": "done", "actions": {"closeTicket": True}},
        }
    )


@pytest.fixture
async def stacked_world(maker: async_sessionmaker[AsyncSession]) -> World:
    return await _build_world(maker, config=_stacked_lane(f"stacked-{uuid4().hex[:8]}"))


async def _until(world: World, done: Callable[[], Awaitable[bool]], ticks: int = 4) -> None:
    for _ in range(ticks):
        if await done():
            return
        await run_tick(world)
    assert await done()


def _at(
    world: World, step: str, status: TicketStatus | None = None
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        ticket = await ticket_row(world)
        return ticket.current_step == step and (status is None or ticket.status == status)

    return check


def _spawned(world: World, count: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return len(world.adapter.spawns) >= count

    return check


def _deliver_impl(world: World) -> None:
    world.github.branches.append(BranchState(name="impl/12-login", sha="sha-impl"))
    world.github.pulls.append(
        PullRequestState(
            number=102,
            head_ref="impl/12-login",
            base_ref="spec/12-widget",
            head_sha="sha-impl",
            state="open",
            merged=False,
            url="https://github.test/pr/102",
        )
    )


async def _drive_build(world: World, *, failures: int = 0) -> None:
    """plan delivers the spec PR and is approved, then `build` fails `failures` times before an
    attempt delivers the impl PR; the ticket ends waiting at `code_review_gate`."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await _until(world, _at(world, "spec_review_gate", TicketStatus.WAITING_GATE))
    world.github.approve("reviewer-1")
    world.adapter.restart()
    await _until(world, _spawned(world, 2))
    for failed in range(failures):
        world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
        await run_tick(world)
        world.clock.advance(_BACKOFF_PASSED)
        world.adapter.restart()
        await _until(world, _spawned(world, 3 + failed))
    _deliver_impl(world)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await _until(world, _at(world, "code_review_gate", TicketStatus.WAITING_GATE))


async def _send_back(
    world: World, *, failures: int = 0, state: Mergeability = Mergeability.CONFLICTING
) -> None:
    """The impl PR merges into the spec branch at `code_review_gate`, then `spec_merge_gate`
    finds the spec PR conflicting with `main` and sends it back to `build`; a PR only behind
    `main` goes back when a person presses the button."""
    await _drive_build(world, failures=failures)
    world.github.approve("reviewer-1")
    await _until(world, _at(world, "spec_merge_gate", TicketStatus.WAITING_GATE))
    world.github.mergeability = state
    # Past the gate's mergeability memo, so the next tick reads the forge again.
    world.clock.advance(timedelta(minutes=3))
    world.adapter.restart()
    if state == Mergeability.BEHIND:
        await run_tick(world)
        await _request_fix(world)
    await _until(world, _at(world, "build"))


async def _fix_spec(
    world: World, *, failures: int = 0, state: Mergeability = Mergeability.CONFLICTING
) -> tuple[RunSpec, RunSpec]:
    """The last normal `build` spawn and the fix's."""
    await _send_back(world, failures=failures, state=state)
    spawns = len(world.adapter.spawns)
    await _until(world, _spawned(world, spawns + 1))
    return world.adapter.spawns[-2], world.adapter.spawns[-1]


async def _finish_fix(
    world: World, *, head: str | None = "sha-2", state: Mergeability = Mergeability.CLEAN
) -> None:
    """The fix run ends SUCCEEDED, having moved the spec PR's head to `head` (None: not at all),
    and the forge then answers `state`."""
    if head is not None:
        world.github.push(101, head)
    world.github.mergeability = state
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)


async def _builds(world: World) -> list[StepRun]:
    return [run for run in await step_runs(world) if run.step_name == "build"]


async def _gate_runs(world: World) -> list[StepRun]:
    return [run for run in await step_runs(world) if run.step_name == "spec_merge_gate"]


def _pr_comments(world: World, number: int = 101) -> list[str]:
    return [body for on, body in world.projector.comments if on == number]


async def _failed_fix(world: World) -> StepRun:
    """The fix run, FAILED with a retry scheduled, and nothing said on the pull request."""
    (*_, fix) = await _builds(world)
    assert fix.status == StepRunStatus.FAILED
    ticket = await ticket_row(world)
    assert (ticket.current_step, ticket.status) == ("build", TicketStatus.RUNNING)
    types = [row.type for row in await event_rows(world) if row.step_run_id == fix.id]
    assert EventType.RETRY_SCHEDULED in types
    assert EventType.STEP_COMPLETED not in types
    assert _pr_comments(world) == []
    return fix


async def _summary_of(world: World, fix: StepRun) -> str:
    (failed,) = [
        row
        for row in await event_rows(world)
        if row.type == EventType.AGENT_FAILED and row.step_run_id == fix.id
    ]
    return str(failed.details["failure_summary"])


# --------------------------------------------------------------------------- the fix's spec


async def test_a_fix_run_works_on_the_pull_request_branch(stacked_world: World) -> None:
    world = stacked_world
    build, fix = await _fix_spec(world)

    # The send-back named the spec PR, not `build`'s own impl PR.
    assert build.workspace.branch.startswith("impl/12-")
    assert fix.workspace.base == fix.workspace.branch == "spec/12-widget"
    # `build`'s session is still kept, and a rework would continue it.
    assert world.adapter.session.session_id is not None
    assert fix.resume_from is None
    assert build.inputs != ()
    assert fix.inputs == ()
    assert fix.expected_artifacts == ()
    assert "Do not rebase, amend or force-push" in fix.prompts.instructions
    assert "#101" in fix.prompts.instructions
    assert "conflicts with `main`" in fix.prompts.instructions
    assert "`git push origin HEAD:spec/12-widget`" in fix.prompts.instructions
    # The run replaces the agent's own instructions, which may be the only place they say it.
    assert (
        "Never merge a pull request, never approve one, never add or remove a label, and never "
        "push to `main` or to any branch other than `spec/12-widget`."
    ) in fix.prompts.instructions
    assert fix.prompts.system == RESOLVED_AGENT.system_prompt
    spawned = [row for row in await event_rows(world) if row.type == EventType.AGENT_SPAWNED]
    assert "resumed_from" not in spawned[-1].details


@pytest.mark.parametrize(
    ("state", "said"),
    [
        (
            Mergeability.CONFLICTING,
            "Conflict fix starting (attempt 2): an agent merges `main` into `spec/12-widget` "
            "and resolves the conflicts.",
        ),
        (
            Mergeability.BEHIND,
            "Branch update starting (attempt 2): an agent merges `main` into `spec/12-widget`.",
        ),
    ],
)
async def test_a_fix_run_says_what_it_does_in_place_of_the_steps_start_comment(
    maker: async_sessionmaker[AsyncSession], state: Mergeability, said: str
) -> None:
    lane = _stacked_lane(
        f"stacked-{uuid4().hex[:8]}",
        build_on_start={"comment": "Build agent starting (attempt {attempt})."},
    )
    world = await _build_world(maker, config=lane)
    await _fix_spec(world, state=state)

    said_on_ticket = [body for _, body in world.projector.comments]
    assert said_on_ticket.count("Build agent starting (attempt 1).") == 1
    assert "Build agent starting (attempt 2)." not in said_on_ticket
    assert said_on_ticket.count(said) == 1


async def test_a_fix_run_keeps_the_steps_runtime_model_skills_and_mcp(
    stacked_world: World,
) -> None:
    build, fix = await _fix_spec(stacked_world)

    assert fix.agent == build.agent
    assert fix.model == build.model
    assert fix.max_turns == build.max_turns
    assert fix.runtime_options == build.runtime_options
    assert fix.skills == build.skills
    assert fix.mcp == build.mcp
    assert fix.prompts.system == build.prompts.system
    assert fix.workspace.repo == build.workspace.repo


async def test_a_failed_fix_retries_on_a_budget_counted_from_the_send_back(
    stacked_world: World,
) -> None:
    """`build` spent 2 of its 3 attempts before the gate; the fix still gets all 3."""
    world = stacked_world
    await _fix_spec(world, failures=1)
    builds = [run for run in await step_runs(world) if run.step_name == "build"]
    assert [run.attempt for run in builds] == [1, 2, 3]

    for _ in range(2):
        world.adapter.complete(RunStatus.FAILED, failure_reason="merge failed")
        await run_tick(world)
        assert (await ticket_row(world)).status != TicketStatus.BLOCKED
        world.clock.advance(_BACKOFF_PASSED)
        world.adapter.restart()
        spawns = len(world.adapter.spawns)
        await _until(world, _spawned(world, spawns + 1))
        # The window stays open through retries of a failed fix.
        retried = world.adapter.spawns[-1]
        assert retried.workspace.branch == "spec/12-widget"
        assert retried.resume_from is None
        assert "Do not rebase, amend or force-push" in retried.prompts.instructions

    world.adapter.complete(RunStatus.FAILED, failure_reason="merge failed")
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    escalated = [row for row in await event_rows(world) if row.type == EventType.ESCALATED]
    assert [row.details["attempt"] for row in escalated] == [5]


# --------------------------------------------------------------------------- the window


async def test_two_rejected_gates_are_told_apart(maker: async_sessionmaker[AsyncSession]) -> None:
    """A reviewer's rejection at one gate and a conflict send-back at another both name `build`:
    only the send-back opens the conflict window."""
    cfg = _process_with_spec_merge_gate(f"two-{uuid4().hex[:8]}").model_dump(
        mode="json", by_alias=True
    )
    merge_gate = next(step for step in cfg["steps"] if step["name"] == "spec_merge_gate")
    merge_gate["gate"]["onReject"] = {"rerunStep": "build"}
    config = ProcessConfig.model_validate(cfg)
    world = await _build_world(maker, config=config)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        sent_back: dict[str, Any] = {
            "pr_number": 101,
            "head_ref": "impl/12-widget",
            "base_ref": "main",
            "head_sha": "sha-1",
            "state": "behind",
            "target_step": "build",
            "request_id": None,
            "requested_by": None,
        }
        for name, answer, details in (
            ("code_gate", EventType.GATE_REJECTED, {}),
            ("spec_merge_gate", EventType.GATE_CONFLICT_SENT_BACK, sent_back),
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
                **details,
            )

    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        tick: Any = SimpleNamespace(session=session, ticket=ticket, config=config)
        build = config.step("build")
        assert isinstance(build, AgentStep)
        fix = await _conflict_fix(cast(_Tick, tick), build)

    assert fix is not None
    assert fix.gate.name == "spec_merge_gate"
    assert (fix.pr_number, fix.head_ref, fix.base_ref, fix.head_sha, fix.state) == (
        101,
        "impl/12-widget",
        "main",
        "sha-1",
        Mergeability.BEHIND,
    )


# --------------------------------------------------------------------------- acceptance

_CONFLICT_NOTE = (
    "An agent merged `main` into this branch and resolved the conflicts. The lane's test and "
    "review steps did not run again. Read the merge commit before you approve."
)
_BEHIND_NOTE = (
    "An agent merged `main` into this branch to bring it up to date. The lane's test and review "
    "steps did not run again."
)


async def _stacked_world_with(
    maker: async_sessionmaker[AsyncSession],
    *,
    build: dict[str, Any] | None = None,
    merge_gate: dict[str, Any] | None = None,
) -> World:
    """The stacked lane with `build` and `spec_merge_gate.gate` extended."""
    cfg = _stacked_lane(f"stacked-{uuid4().hex[:8]}").model_dump(mode="json", by_alias=True)
    steps = {step["name"]: step for step in cfg["steps"]}
    steps["build"].update(build or {})
    steps["spec_merge_gate"]["gate"].update(merge_gate or {})
    return await _build_world(maker, config=ProcessConfig.model_validate(cfg))


async def _back_at_the_gate(world: World) -> None:
    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_merge_gate"
    entered = await entered_events(world)
    assert (entered[-1]["from_step"], entered[-1]["reason"]) == ("build", "conflict_fixed")
    # Straight back to the gate that sent it, never through `build.next`.
    assert [e["step"] for e in entered].count("code_review_gate") == 1


async def test_a_fix_is_accepted_when_the_head_moved_and_it_merges_cleanly(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _fix_spec(world)
    # An approval of the head the gate sent back, which must not carry over to the fix's push.
    world.github.approve("reviewer-2")

    await _finish_fix(world)

    await _back_at_the_gate(world)
    (*_, fix) = await _builds(world)
    assert fix.status == StepRunStatus.SUCCEEDED
    completed = [
        row
        for row in await event_rows(world)
        if row.type == EventType.STEP_COMPLETED and row.step_run_id == fix.id
    ]
    assert len(completed) == 1

    await run_tick(world)
    (*_, sent_back, reopened) = await _gate_runs(world)
    assert sent_back.status == StepRunStatus.REJECTED
    assert reopened.status == StepRunStatus.WAITING
    assert 101 not in world.projector.merge_shas


async def test_a_fix_that_did_not_move_the_head_fails_with_words(stacked_world: World) -> None:
    world = stacked_world
    await _fix_spec(world)

    await _finish_fix(world, head=None)

    summary = await _summary_of(world, await _failed_fix(world))
    assert "#101" in summary and "`main`" in summary


async def test_a_fix_that_still_conflicts_fails_and_retries(stacked_world: World) -> None:
    world = stacked_world
    await _fix_spec(world)

    await _finish_fix(world, state=Mergeability.CONFLICTING)

    summary = await _summary_of(world, await _failed_fix(world))
    assert "pull request #101 still conflicts with `main`" in summary
    world.clock.advance(_BACKOFF_PASSED)
    world.adapter.restart()
    spawns = len(world.adapter.spawns)
    await _until(world, _spawned(world, spawns + 1))
    assert world.adapter.spawns[-1].workspace.branch == "spec/12-widget"
    await _finish_fix(world, head="sha-3")
    await _back_at_the_gate(world)


async def test_unknown_after_the_push_keeps_the_run_and_asks_again(stacked_world: World) -> None:
    world = stacked_world
    await _fix_spec(world)

    await _finish_fix(world, state=Mergeability.UNKNOWN)

    (*_, fix) = await _builds(world)
    assert fix.status == StepRunStatus.RUNNING
    assert (await ticket_row(world)).current_step == "build"
    decided = {EventType.AGENT_FAILED, EventType.POSTCONDITION_FAILED, EventType.STEP_COMPLETED}
    assert not [r for r in await event_rows(world) if r.step_run_id == fix.id and r.type in decided]
    assert _pr_comments(world) == []

    world.github.mergeability = Mergeability.CLEAN
    await run_tick(world)
    await _back_at_the_gate(world)


async def test_a_push_the_pull_request_does_not_show_yet_is_waited_for(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _fix_spec(world)
    world.github.branches = [
        replace(b, sha="sha-2") if b.name == "spec/12-widget" else b for b in world.github.branches
    ]

    await _finish_fix(world, head=None)

    (*_, fix) = await _builds(world)
    assert fix.status == StepRunStatus.RUNNING
    world.github.push(101, "sha-2")
    await run_tick(world)
    await _back_at_the_gate(world)


async def _ended_by_a_person(world: World, *, merged: bool, approved: bool = False) -> StepRun:
    """While the fix runs, a person resolves the pull request in GitHub (`approved`: reviewer-2
    approves that head) and merges it, or closes it unmerged; then the fix run ends. Nothing was
    left to fix: the run completes with no note, and the ticket is back on the gate."""
    await _fix_spec(world)
    world.github.push(101, "sha-by-hand")
    if approved:
        world.github.approve("reviewer-2")
    world.github.pulls = [
        replace(pr, state="closed", merged=merged) if pr.number == 101 else pr
        for pr in world.github.pulls
    ]

    await _finish_fix(world, head=None)

    (*_, fix) = await _builds(world)
    assert fix.status == StepRunStatus.SUCCEEDED
    types = [row.type for row in await event_rows(world) if row.step_run_id == fix.id]
    assert EventType.STEP_COMPLETED in types
    assert EventType.RETRY_SCHEDULED not in types
    assert _pr_comments(world) == []
    await _back_at_the_gate(world)
    return fix


async def _conflict_send_backs(world: World) -> int:
    return len(
        [r for r in await event_rows(world) if r.type == EventType.GATE_CONFLICT_SENT_BACK]
    )


async def test_a_fix_whose_pull_request_a_person_merged_returns_to_the_gate(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _ended_by_a_person(world, merged=True)

    await run_tick(world)

    (*_, reopened) = await _gate_runs(world)
    assert reopened.status == StepRunStatus.WAITING
    assert await _conflict_send_backs(world) == 1
    assert _pr_comments(world) == []


async def test_a_merged_pull_request_approved_on_its_head_advances_the_gate(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _ended_by_a_person(world, merged=True, approved=True)

    await run_tick(world)

    (*_, reopened) = await _gate_runs(world)
    assert reopened.status == StepRunStatus.APPROVED
    assert 101 not in world.projector.merge_shas
    assert await _conflict_send_backs(world) == 1


async def test_a_fix_whose_pull_request_a_person_closed_returns_to_the_gate(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _ended_by_a_person(world, merged=False)

    await run_tick(world)

    (*_, reopened) = await _gate_runs(world)
    assert reopened.status == StepRunStatus.WAITING
    held = [r for r in await event_rows(world) if r.type == EventType.GATE_PR_CLOSED]
    assert [(r.step_run_id, r.details["pr_number"]) for r in held] == [(reopened.id, 101)]
    assert await _conflict_send_backs(world) == 1


async def _returned_without_a_run(world: World, builds: int) -> None:
    """Back at the gate with no fix run started and nothing said on the pull request, which is no
    longer open."""
    await _back_at_the_gate(world)
    assert len(await _builds(world)) == builds
    assert _pr_comments(world) == []


def _end_pull_request(world: World, *, merged: bool) -> None:
    world.github.pulls = [
        replace(pr, state="closed", merged=merged) if pr.number == 101 else pr
        for pr in world.github.pulls
    ]


async def _retry_due(world: World) -> int:
    """A failed fix whose retry is now due; the number of `build` runs so far."""
    await _fix_spec(world)
    await _finish_fix(world, head=None)
    await _failed_fix(world)
    world.clock.advance(_BACKOFF_PASSED)
    world.adapter.restart()
    return len(await _builds(world))


@pytest.mark.parametrize("merged", [True, False])
async def test_a_pull_request_ended_before_the_retry_returns_to_the_gate(
    stacked_world: World, merged: bool
) -> None:
    world = stacked_world
    builds = await _retry_due(world)
    spawns = len(world.adapter.spawns)
    _end_pull_request(world, merged=merged)

    await run_tick(world)

    await _returned_without_a_run(world, builds)
    assert len(world.adapter.spawns) == spawns
    # The gate's own rules for a merged or closed pull request decide; no second send-back.
    await run_tick(world)
    assert (await _gate_runs(world))[-1].status != StepRunStatus.REJECTED
    assert await _conflict_send_backs(world) == 1


async def _blocked_fix(world: World) -> tuple[int, int]:
    """A fix that failed until it escalated; the `build` runs and the comments so far."""
    await _fix_spec(world)
    for _ in range(2):
        world.adapter.complete(RunStatus.FAILED, failure_reason="merge failed")
        await run_tick(world)
        world.clock.advance(_BACKOFF_PASSED)
        world.adapter.restart()
        spawns = len(world.adapter.spawns)
        await _until(world, _spawned(world, spawns + 1))
    world.adapter.complete(RunStatus.FAILED, failure_reason="merge failed")
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    return len(await _builds(world)), len(world.projector.comments)


async def test_an_unblock_before_the_forge_judged_a_hand_push_promises_nothing(
    stacked_world: World,
) -> None:
    world = stacked_world
    builds, said_before = await _blocked_fix(world)
    spawns = len(world.adapter.spawns)

    world.github.push(101, "sha-by-hand")
    world.github.mergeability = Mergeability.UNKNOWN
    _remove_blocked_label(world)
    await run_tick(world)

    said = [body for _, body in world.projector.comments[said_before:]]
    assert said == [
        "Unblocked: the forge has not said yet whether the pull request still needs a fix. "
        "`build` (attempt 5) runs only if it does."
    ]
    ticket = await ticket_row(world)
    assert (ticket.current_step, ticket.status) == ("build", TicketStatus.RUNNING)
    assert len(world.adapter.spawns) == spawns

    # The failed run's backoff still gates the start, and with it the next look at the forge.
    world.github.mergeability = Mergeability.CLEAN
    world.clock.advance(_BACKOFF_PASSED)
    await _until(world, _at(world, "spec_merge_gate"))
    assert len(await _builds(world)) == builds
    assert len(world.adapter.spawns) == spawns


async def test_an_unblock_after_a_hand_merge_says_the_ticket_goes_back_to_its_gate(
    stacked_world: World,
) -> None:
    """The acknowledgment must not promise a retry that the same tick decides against."""
    world = stacked_world
    builds, said_before = await _blocked_fix(world)

    _end_pull_request(world, merged=True)
    _remove_blocked_label(world)
    await run_tick(world)

    said = [body for _, body in world.projector.comments[said_before:]]
    assert said == [
        "Unblocked: the pull request needs no fix now, so `build` does not run again and the "
        "ticket goes back to `spec_merge_gate`."
    ]
    await _returned_without_a_run(world, builds)


async def test_a_merged_pull_request_returns_to_the_gate_past_an_unmet_precondition(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`build` requires the spec pull request open: without the check first, the ticket would
    sit on `build` in silence."""
    world = await _stacked_world_with(
        maker, build={"preconditions": 'pr_open("spec/{ticket}-*", "main")'}
    )
    builds = await _retry_due(world)
    _end_pull_request(world, merged=True)

    await run_tick(world)

    await _returned_without_a_run(world, builds)


_CHANGED_NOTE = (
    "This branch changed after the gate sent it back, and it now merges with `main`. The lane's "
    "test and review steps did not run again. Read the new commits before you approve."
)


async def test_a_moved_clean_head_returns_to_the_gate_and_says_it_changed(
    stacked_world: World,
) -> None:
    """The branch merges cleanly before a fix starts: a person's push, or an earlier attempt's
    that ended before it was accepted. No agent starts, and the approver is told what is true
    either way."""
    world = stacked_world
    await _send_back(world)
    builds = len(await _builds(world))
    spawns = len(world.adapter.spawns)
    world.github.push(101, "sha-by-hand")
    world.github.mergeability = Mergeability.CLEAN

    await run_tick(world)

    await _back_at_the_gate(world)
    assert len(await _builds(world)) == builds
    assert len(world.adapter.spawns) == spawns
    await run_tick(world)
    assert (await _gate_runs(world))[-1].status == StepRunStatus.WAITING
    assert _pr_comments(world) == [_CHANGED_NOTE]


async def test_a_moved_head_the_forge_has_not_judged_starts_nothing(stacked_world: World) -> None:
    world = stacked_world
    await _send_back(world)
    world.github.push(101, "sha-by-hand")
    world.github.mergeability = Mergeability.UNKNOWN
    spawns = len(world.adapter.spawns)
    events = len(await event_rows(world))

    await run_tick(world)

    assert len(world.adapter.spawns) == spawns
    assert len(await event_rows(world)) == events
    ticket = await ticket_row(world)
    assert (ticket.current_step, ticket.status) == ("build", TicketStatus.RUNNING)

    world.github.mergeability = Mergeability.CLEAN
    await run_tick(world)
    await _back_at_the_gate(world)


async def test_an_unmoved_head_starts_the_fix_without_asking_mergeability(
    stacked_world: World,
) -> None:
    """The head the gate sent back is still there: the fix starts, even on a CLEAN reading."""
    world = stacked_world
    await _send_back(world)
    world.github.mergeability = Mergeability.CLEAN
    reads = world.github.mergeability_reads
    spawns = len(world.adapter.spawns)

    await run_tick(world)

    assert len(world.adapter.spawns) == spawns + 1
    assert world.adapter.spawns[-1].workspace.branch == "spec/12-widget"
    assert world.github.mergeability_reads == reads


async def test_a_retry_after_the_pull_request_merged_returns_to_the_gate(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _fix_spec(world)
    await _finish_fix(world, head=None)
    fix = await _failed_fix(world)
    builds = len(await _builds(world))
    _end_pull_request(world, merged=True)
    async with world.sessions() as session:
        request = await append(
            session,
            type=EventType.STEP_RETRY_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=fix.id,
            step=fix.step_name,
            attempt=fix.attempt,
            next_attempt=fix.attempt + 1,
            requested_by="operator@example.com",
            requested_via="dashboard",
        )

    await run_tick(world)

    await _returned_without_a_run(world, builds)
    (answer,) = [r for r in await event_rows(world) if r.type == EventType.STEP_RETRY_REFUSED]
    assert (answer.details["request_id"], answer.details["reason"]) == (
        request.id,
        "conflict_fixed",
    )


async def test_the_steps_own_postconditions_are_not_asked_in_the_window(
    stacked_world: World,
) -> None:
    world = stacked_world
    await _fix_spec(world)
    # `build`'s `pr_open("impl/12-*", "spec/12-*")` cannot hold: `code_review_gate` merged it.
    (impl,) = [pr for pr in world.github.pulls if pr.number == 102]
    assert impl.state == "closed"

    await _finish_fix(world)

    await _back_at_the_gate(world)
    failed = [r for r in await event_rows(world) if r.type == EventType.POSTCONDITION_FAILED]
    assert failed == []


async def test_on_success_is_not_applied_after_a_fix(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _stacked_world_with(
        maker,
        build={
            "onSuccess": {
                "ticketLabel": "factorial:code-review",
                "assign": ["reviewer-3"],
                "comment": "Build ready: {pr_url}",
            }
        },
        merge_gate={"onConflict": {"rerunStep": "build", "assign": ["reviewer-2"]}},
    )
    await _fix_spec(world)
    assert world.github.issue.assignees == ("reviewer-2",)
    world.projector.calls.clear()

    await _finish_fix(world)

    await _back_at_the_gate(world)
    assert "factorial:code-review" not in world.github.issue.labels
    assert world.github.issue.assignees == ("reviewer-2",)
    said = [body for kind, body in world.projector.calls if kind == "comment"]
    assert not [body for body in said if body.startswith("Build ready")]


@pytest.mark.parametrize(
    ("state", "note"),
    [(Mergeability.CONFLICTING, _CONFLICT_NOTE), (Mergeability.BEHIND, _BEHIND_NOTE)],
)
async def test_an_accepted_fix_tells_the_approver_to_read_the_merge(
    stacked_world: World, state: Mergeability, note: str
) -> None:
    world = stacked_world
    await _fix_spec(world, state=state)

    await _finish_fix(world)
    await run_tick(world)

    await _back_at_the_gate(world)
    assert _pr_comments(world) == [note]


async def _window(world: World) -> ConflictFix | None:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        config, *_ = await _load_pinned_config(session, ticket.process_version_id)
        tick: Any = SimpleNamespace(session=session, ticket=ticket, config=config)
        build = config.step("build")
        assert isinstance(build, AgentStep)
        return await _conflict_fix(cast(_Tick, tick), build)


async def test_the_window_closes_when_the_gate_opens_its_next_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _stacked_world_with(maker, merge_gate={"onReject": {"rerunStep": "build"}})
    await _fix_spec(world)
    assert await _window(world) is not None
    await _finish_fix(world)
    await run_tick(world)
    assert (await _gate_runs(world))[-1].status == StepRunStatus.WAITING
    assert await _window(world) is None

    world.github.request_changes("reviewer-2", "rename the flag")
    world.adapter.restart()
    spawns = len(world.adapter.spawns)
    await _until(world, _spawned(world, spawns + 1))

    # A reviewer's rejection there is a normal rework of `build`, on its own branch.
    rework = world.adapter.spawns[-1]
    assert rework.workspace.branch.startswith("impl/12-")
    assert "Do not rebase, amend or force-push" not in rework.prompts.instructions
    entered = await entered_events(world)
    assert (entered[-1]["step"], entered[-1]["reason"]) == ("build", "reject_rerun")


async def _request_fix(world: World) -> int:
    """What the dashboard's conflict-fix route records against the waiting gate run."""
    (*_, gate) = await _gate_runs(world)
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


async def test_automatic_fixes_stop_after_three_in_a_row(stacked_world: World) -> None:
    """Live, round after round: the pull request conflicts again after every accepted fix."""
    world = stacked_world
    await _fix_spec(world)
    for head in ("sha-2", "sha-3"):
        await _finish_fix(world, head=head)
        await _back_at_the_gate(world)
        world.github.mergeability = Mergeability.CONFLICTING
        world.adapter.restart()
        spawns = len(world.adapter.spawns)
        await _until(world, _spawned(world, spawns + 1))
        assert world.adapter.spawns[-1].workspace.branch == "spec/12-widget"
    await _finish_fix(world, head="sha-4")
    world.projector.calls.clear()
    world.github.mergeability = Mergeability.CONFLICTING

    await run_tick(world)

    (*_, waiting) = await _gate_runs(world)
    assert (waiting.attempt, waiting.status) == (AUTOMATIC_FIX_LIMIT + 1, StepRunStatus.WAITING)
    sent = [r for r in await event_rows(world) if r.type == EventType.GATE_CONFLICT_SENT_BACK]
    assert [r.details["request_id"] for r in sent] == [None] * AUTOMATIC_FIX_LIMIT
    said = [body for kind, body in world.projector.calls if kind == "comment"]
    limit = [body for body in said if "3 times in a row" in body]
    assert len(limit) == 1 and "waits for a person" in limit[0]

    # The button is not limited.
    request_id = await _request_fix(world)
    await run_tick(world)
    (*_, honoured) = await _gate_runs(world)
    assert (honoured.id, honoured.status) == (waiting.id, StepRunStatus.REJECTED)
    last = [r for r in await event_rows(world) if r.type == EventType.GATE_CONFLICT_SENT_BACK][-1]
    assert last.details["request_id"] == request_id
    assert (await ticket_row(world)).current_step == "build"


async def test_check_now_accepts_a_failed_fix_once_it_merges_cleanly(stacked_world: World) -> None:
    """Force complete reads the same verdict: a fix the pull request now bears out is accepted
    back to the gate, with the approver's note."""
    world = stacked_world
    await _fix_spec(world)
    await _finish_fix(world, state=Mergeability.CONFLICTING)
    fix = await _failed_fix(world)
    world.github.mergeability = Mergeability.CLEAN
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.COMPLETION_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=fix.id,
            step=fix.step_name,
            attempt=fix.attempt,
            allow_unmet=False,
            seen_failures=[],
            requested_by="op@example.com",
            requested_via="dashboard",
        )

    await run_tick(world)

    await _back_at_the_gate(world)
    (*_, accepted) = await _builds(world)
    assert (accepted.id, accepted.status) == (fix.id, StepRunStatus.SUCCEEDED)
    assert _pr_comments(world) == [_CONFLICT_NOTE]
