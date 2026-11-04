"""A gate's rework of an interactive step continues the previous attempt's session in its kept
workspace, or starts fresh and says why (decision 56).

Every failure here is silent. A rework that resumes what it should not continues a session on a
CLI that cannot read it, or in a sandbox the reaper is about to take; one that records the wrong
workspace lets the reaper take the sandbox the session lives in; one that escalates on a reaped
workspace blocks a ticket a fresh spawn would have carried on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.events.previews import PreviewLink, preview_links
from orchestrator.github import LABEL_WAITING
from orchestrator.worker import previews
from orchestrator.worker.context import SessionPolicy
from runtime.contract import (
    PREVIEW_CONTINUED,
    PREVIEW_RENEWAL,
    Artifact,
    ArtifactKind,
    IdleReason,
    ParkReason,
    PreviewPhase,
    PreviewState,
    PreviewUrl,
    ResumeFrom,
    RunHandle,
    RunSpec,
    RunStatus,
    SessionUnavailable,
)
from tests.reconcile_world import (
    SERVICES,
    FakeAdapter,
    InteractivePreviewAdapter,
    World,
    event_rows,
    event_types,
    interactive_world,
    plan_runs,
    run_tick,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_UNMOVED = "the pull request #101 still ends at sha-1, the commit the gate rejected"

ON = SessionPolicy(terminal_enabled=True)


async def _details(world: World, event_type: EventType) -> list[dict[str, Any]]:
    return [dict(row.details) for row in await event_rows(world) if row.type == event_type]


async def _rejected(world: World, adapter: FakeAdapter, **result: Any) -> None:
    """plan attempt 1 succeeds (its result carrying `result`), the gate opens and rejects; the
    rework is due on the next tick."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    adapter.complete(RunStatus.SUCCEEDED, **result)
    await run_tick(world)  # accept -> spec_gate
    await run_tick(world)  # the gate opens
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects -> plan
    adapter.restart()


async def test_an_interactive_rework_resumes_the_previous_attempts_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A second rework continues the same session in the same workspace: the reaper keeps a
    sandbox by the workspace its latest run names, and the first attempt's run is the
    sandbox."""
    world, adapter = await interactive_world(maker)
    await _rejected(world, adapter)
    adapter.log_end = 4096
    await run_tick(world)

    first, second = adapter.spawn_attempts
    assert first.resume_from is None
    assert second.resume_from == ResumeFrom(run_id="run-1", session_id="session-run-1")
    attempt_1, attempt_2 = await plan_runs(world)
    assert attempt_2.runtime_run_id == "run-2"
    spawned = await _details(world, EventType.AGENT_SPAWNED)
    assert spawned[0]["log_offset"] == 0
    assert spawned[1]["resumed_from"] == {
        "step_run_id": attempt_1.id,
        "run_id": "run-1",
        "attempt": 1,
    }
    assert spawned[1]["workspace_run_id"] == "run-1"
    assert spawned[1]["log_offset"] == 4096
    assert [row["session_id"] for row in await _details(world, EventType.AGENT_COMPLETED)] == [
        "session-run-1"
    ]

    # The rework pushes to the same PR and succeeds; the gate reopens and rejects again.
    world.github.pulls = [replace(pr, head_sha="sha-2") for pr in world.github.pulls]
    adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(
            Artifact(
                kind=ArtifactKind.BRANCH,
                locator={"branch": f"spec/{world.issue_number}-widget"},
            ),
        ),
    )
    await run_tick(world)
    await run_tick(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)
    adapter.restart()
    await run_tick(world)

    third = adapter.spawn_attempts[2]
    assert third.resume_from == ResumeFrom(run_id="run-2", session_id="session-run-1")
    spawned = await _details(world, EventType.AGENT_SPAWNED)
    assert spawned[2]["resumed_from"] == {
        "step_run_id": attempt_2.id,
        "run_id": "run-2",
        "attempt": 2,
    }
    assert spawned[2]["workspace_run_id"] == "run-1"
    assert EventType.SESSION_RESUME_SKIPPED not in await event_types(world)


async def test_an_interactive_rework_waits_at_its_prompt_until_the_rejected_pull_request_moves(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The agent ended its turn saying it would revise and push. Its branch and open PR held
    before it began, so finishing it there sent the unchanged PR back to the gate."""
    world, adapter = await interactive_world(maker)
    await _rejected(world, adapter)
    await run_tick(world)  # the rework continues the session
    adapter.go_idle(IdleReason.TURN_END, since=world.clock.now)
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.finish_calls == []
    (idle,) = await _details(world, EventType.AGENT_IDLE)
    assert idle["attempt"] == 2
    assert idle["unmet"] == [_UNMOVED]
    # Nothing asked and nobody engaged: the stop retries at its deadline, so nobody is asked to
    # look.
    assert LABEL_WAITING not in world.github.issue.labels
    *_, rework = await plan_runs(world)
    assert rework.status == StepRunStatus.IDLE

    world.github.push(101, "sha-2")
    world.clock.advance(timedelta(seconds=30))
    await run_tick(world)

    assert adapter.finish_calls == ["run-2"]


async def test_a_rework_parked_at_the_rejected_commit_says_so_on_the_ticket(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The agent ended its turn saying it would push: the comment is where the human learns the
    pull request has not moved."""
    world, adapter = await interactive_world(maker)
    await _rejected(world, adapter)
    await run_tick(world)
    adapter.go_idle(IdleReason.QUESTION, since=world.clock.now, question="Push now?")
    await run_tick(world)
    world.clock.advance(world.deps.sessions.idle_park_after)
    await run_tick(world)

    assert adapter.interrupt_reasons == [ParkReason.IDLE]
    (parked,) = [
        body for kind, body in world.projector.calls if kind == "comment" and "is paused" in body
    ]
    assert _UNMOVED in parked


async def _moved_runtime(world: World, adapter: FakeAdapter) -> dict[str, Any]:
    await _rejected(world, adapter)
    # The row deleted and recreated under the same slug: SET NULL on the old run.
    async with world.sessions() as session:
        await session.execute(
            update(StepRun).where(StepRun.ticket_id == world.ticket_id).values(runtime_id=None)
        )
    return {}


async def _repinned_cli(world: World, adapter: FakeAdapter) -> dict[str, Any]:
    await _rejected(world, adapter)
    return {"pinned_version": "fake-2"}


async def _past_retention(world: World, adapter: FakeAdapter) -> dict[str, Any]:
    await _rejected(world, adapter)
    # Retention is 2 h here: 61 minutes on, the reaper may take it within the hour's margin.
    world.clock.advance(timedelta(minutes=61))
    return {}


async def _no_session(world: World, adapter: FakeAdapter) -> dict[str, Any]:
    await _rejected(world, adapter, session_id=None)
    return {}


@pytest.mark.parametrize(
    ("setup", "sessions", "reason"),
    [
        pytest.param(setup, sessions, reason, id=reason)
        for setup, sessions, reason in (
            (_moved_runtime, ON, "runtime_changed"),
            (_repinned_cli, ON, "cli_version_changed"),
            (_past_retention, replace(ON, retention=timedelta(hours=2)), "past_retention"),
            (_no_session, ON, "no_session"),
        )
    ],
)
async def test_an_unresumable_rework_spawns_fresh_and_says_why(
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    setup: Any,
    sessions: SessionPolicy,
    reason: str,
) -> None:
    world, adapter = await interactive_world(maker, sessions=sessions)
    for name, value in (await setup(world, adapter)).items():
        monkeypatch.setattr(FakeAdapter, name, value)
    await run_tick(world)

    assert len(adapter.spawn_attempts) == 2
    assert adapter.spawn_attempts[1].resume_from is None
    attempt_1, _ = await plan_runs(world)
    (skipped,) = await _details(world, EventType.SESSION_RESUME_SKIPPED)
    assert skipped["reason"] == reason
    assert skipped["previous_step_run_id"] == attempt_1.id
    spawned = await _details(world, EventType.AGENT_SPAWNED)
    assert "resumed_from" not in spawned[1]
    assert spawned[1]["workspace_run_id"] == "run-2"


async def test_a_substrate_that_cannot_continue_the_session_respawns_fresh_on_the_same_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`SessionUnavailable` is a WORKSPACE refusal, which escalates anywhere else; here the
    session only made the rework cheaper, so the attempt starts fresh instead."""
    world, adapter = await interactive_world(maker)
    await _rejected(world, adapter)
    adapter.spawn_error_for_resume = SessionUnavailable("the sandbox was reaped")
    await run_tick(world)

    _, refused, fresh = adapter.spawn_attempts
    assert refused.resume_from is not None
    assert fresh.resume_from is None
    attempt_1, attempt_2 = await plan_runs(world)
    assert attempt_2.status == StepRunStatus.RUNNING
    assert attempt_2.runtime_run_id == "run-2"
    (skipped,) = await _details(world, EventType.SESSION_RESUME_SKIPPED)
    assert skipped == {
        "reason": "not_kept",
        "previous_step_run_id": attempt_1.id,
        "step": "plan",
        "attempt": 2,
    }
    spawned = await _details(world, EventType.AGENT_SPAWNED)
    assert "resumed_from" not in spawned[1]
    assert spawned[1]["workspace_run_id"] == "run-2"
    types = await event_types(world)
    assert EventType.AGENT_FAILED not in types
    assert EventType.ESCALATED not in types
    assert (await ticket_row(world)).status == TicketStatus.RUNNING


async def test_a_retry_after_unmet_postconditions_starts_a_fresh_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The session ended in work the predicates refused: a retry continuing it would carry the
    failed attempt's conversation into the one meant to start over."""
    world, adapter = await interactive_world(maker)
    await run_tick(world)
    adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # no branch, no PR: the run fails its postconditions
    world.clock.advance(timedelta(minutes=11))
    adapter.restart()
    await run_tick(world)

    _, retry = adapter.spawn_attempts
    assert retry.resume_from is None
    attempt_1, _ = await plan_runs(world)
    assert attempt_1.status == StepRunStatus.FAILED
    assert EventType.SESSION_RESUME_SKIPPED not in await event_types(world)


# --------------------------------------------------------------------------- a carried preview

_URL = "https://3000-sbx.e2b.app"


async def _press(world: World, run: StepRun, kind: EventType) -> None:
    """A person's Start or Stop, committed as the API commits it."""
    async with world.sessions() as session:
        await append(
            session,
            type=kind,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            requested_by="reviewer@example.com",
        )


async def _previewed_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, *, serving: bool
) -> tuple[World, InteractivePreviewAdapter]:
    """plan attempt 1's preview, started by a person beside its live session and serving when
    `serving` says, when the gate rejects it; the rework is due next tick."""
    # The test database is shared: another test's awake preview would count toward the limit.
    monkeypatch.setattr("orchestrator.worker.previews.MAX_AWAKE_WORKSPACES", 10**6)
    world, adapter = await interactive_world(
        maker, preview=dict(SERVICES), host=InteractivePreviewAdapter
    )
    assert isinstance(adapter, InteractivePreviewAdapter)
    await run_tick(world)  # plan attempt 1 spawns
    (attempt_1,) = await plan_runs(world)
    await _press(world, attempt_1, EventType.PREVIEW_REQUESTED)
    world.github.deliver_spec(world.issue_number)
    adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # the preview starts; accept -> spec_gate
    if serving:
        adapter.serve("run-1", _URL)
    await run_tick(world)  # the gate opens
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # the gate rejects -> plan
    adapter.restart()
    return world, adapter


async def _run_details(world: World, run: StepRun, kind: EventType) -> list[dict[str, Any]]:
    return [
        dict(row.details)
        for row in await event_rows(world)
        if row.step_run_id == run.id and row.type == kind
    ]


async def _stop_reasons(world: World, run: StepRun) -> list[str]:
    return [d["reason"] for d in await _run_details(world, run, EventType.PREVIEW_STOPPED)]


async def _links(world: World, run: StepRun) -> list[PreviewLink]:
    async with world.sessions() as session:
        return await preview_links(session, run)


def _ready(adapter: InteractivePreviewAdapter) -> PreviewState:
    """What the rework's run reads once run-1's preview moved to it."""
    return PreviewState(
        phase=PreviewPhase.READY,
        token=adapter.previews["run-1"].token,
        urls=(PreviewUrl(service="web", url=_URL),),
    )


def _stopped(adapter: InteractivePreviewAdapter, detail: str) -> PreviewState:
    return PreviewState(
        phase=PreviewPhase.STOPPED, token=adapter.previews["run-1"].token, detail=detail
    )


async def test_a_rework_carries_its_previous_attempts_ready_preview_over(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person reviewing keeps the page they were looking at, and each URL stays bound to the
    one run that serves it."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    adapter.adopted = {"run-1": _stopped(adapter, PREVIEW_CONTINUED), "run-2": _ready(adapter)}
    await run_tick(world)  # the rework continues the session

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["continued"]
    assert await _links(world, attempt_1) == []
    (started,) = await _run_details(world, attempt_2, EventType.PREVIEW_STARTED)
    assert started["carried_from"] == attempt_1.id
    assert started["services"] == ["web"]
    assert await _links(world, attempt_2) == [("web", _URL)]
    assert [run_id for run_id, _ in adapter.preview_starts] == ["run-1"], "carried, not restarted"
    # In the tick that carries it: the earlier run's idle close would otherwise still stand.
    assert [k for run_id, k in adapter.keep_untils if run_id == "run-2"] == [
        world.clock.now + timedelta(minutes=30)
    ]

    await run_tick(world)

    watched = [k for run_id, k in adapter.keep_untils if run_id == "run-2"]
    assert len(watched) == 2, "the next pass watches the carried preview"


async def test_a_preview_the_rework_could_not_carry_is_started_again_on_the_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewed at the adoption, as a sandbox with little of its window left is: the earlier run
    then reads `renewal`, which must not start a preview on a run a later attempt continues."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    adapter.adopted = {"run-1": _stopped(adapter, PREVIEW_RENEWAL)}
    await run_tick(world)  # the rework continues the session
    await run_tick(world)  # the loop's request is answered
    await run_tick(world)

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["continued"]
    requests = await _run_details(world, attempt_1, EventType.PREVIEW_REQUESTED)
    assert [d["requested_by"] for d in requests] == ["reviewer@example.com"]
    (request,) = await _run_details(world, attempt_2, EventType.PREVIEW_REQUESTED)
    assert request["requested_by"] == "factorial" and request["renewal"] is True
    assert [run_id for run_id, _ in adapter.preview_starts] == ["run-1", "run-2"]


async def test_a_preview_still_starting_when_the_rework_adopts_it_is_carried_once_ready(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substrate saw it ready before the loop recorded so on the earlier run."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=False)
    adapter.adopted = {"run-1": _stopped(adapter, PREVIEW_CONTINUED), "run-2": _ready(adapter)}
    await run_tick(world)  # the rework continues the session

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _run_details(world, attempt_1, EventType.PREVIEW_STARTED) == []
    assert await _stop_reasons(world, attempt_1) == ["continued"]
    (started,) = await _run_details(world, attempt_2, EventType.PREVIEW_STARTED)
    assert started["carried_from"] == attempt_1.id
    assert await _run_details(world, attempt_2, EventType.PREVIEW_REQUESTED) == []


async def test_a_preview_renewed_in_the_pass_before_its_rework_starts_again_on_the_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass answers a renewal it sees at once, so the adoption later in the same tick finds
    the loop's request and moves it. Answered a pass later, the request would land on a run a
    later attempt continues, and be refused there."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    renewed = _stopped(adapter, PREVIEW_RENEWAL)
    adapter.previews["run-1"] = renewed
    adapter.adopted = {"run-1": renewed}
    await run_tick(world)  # the pass sees the renewal; the rework continues the session
    await run_tick(world)  # the loop's request is answered
    await run_tick(world)

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["renewal", "continued"]
    (request,) = await _run_details(world, attempt_2, EventType.PREVIEW_REQUESTED)
    assert request["requested_by"] == "factorial" and request["renewal"] is True
    assert [run_id for run_id, _ in adapter.preview_starts] == ["run-1", "run-2"]


async def test_a_renewal_left_unanswered_when_the_rework_adopts_it_starts_again_on_the_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop's restart failed in the pass that saw the renewal: the adoption takes the
    renewal over, rather than leave it owed to a run a later attempt continues."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    renewed = _stopped(adapter, PREVIEW_RENEWAL)
    adapter.previews["run-1"] = renewed
    adapter.adopted = {"run-1": renewed}

    async def failing(*_: object) -> None:
        raise RuntimeError("the restart's write failed")

    monkeypatch.setattr("orchestrator.worker.previews._renew", failing)
    await run_tick(world)  # the pass sees the renewal and fails to answer it; the rework adopts
    await run_tick(world)  # the loop's request on the rework is answered

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["renewal", "continued"]
    (request,) = await _run_details(world, attempt_2, EventType.PREVIEW_REQUESTED)
    assert request["requested_by"] == "factorial" and request["renewal"] is True
    assert [run_id for run_id, _ in adapter.preview_starts] == ["run-1", "run-2"]


async def test_a_stop_pressed_while_the_rework_adopts_the_preview_stops_it_on_the_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Stop names the earlier run, which serves nothing once the substrate moved its
    preview: moved with it, the Stop reaches what the rework serves. Left on the earlier run, it
    stops nothing, and the preview serves on with no event to watch it by."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    adapter.adopted = {"run-1": _stopped(adapter, PREVIEW_CONTINUED), "run-2": _ready(adapter)}
    (attempt_1,) = await plan_runs(world)
    adopt = adapter.spawn

    async def spawn(run_spec: RunSpec) -> RunHandle:
        handle = await adopt(run_spec)
        await _press(world, attempt_1, EventType.PREVIEW_STOP_REQUESTED)
        return handle

    monkeypatch.setattr(adapter, "spawn", spawn)
    await run_tick(world)  # the rework continues the session
    await run_tick(world)  # the moved Stop is honoured

    _, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["continued"]
    (moved,) = await _run_details(world, attempt_2, EventType.PREVIEW_STOP_REQUESTED)
    assert moved["requested_by"] == "reviewer@example.com"
    assert moved["carried_from"] == attempt_1.id
    assert await _stop_reasons(world, attempt_2) == ["stopped by reviewer@example.com"]
    assert adapter.previews["run-2"].phase is PreviewPhase.STOPPED


async def test_a_carried_preview_whose_record_fails_is_still_watched_on_the_rework(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substrate moved it before the record failed: started again on the rework, rather
    than left serving there with no event to watch it by."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    adapter.adopted = {"run-1": _stopped(adapter, PREVIEW_CONTINUED), "run-2": _ready(adapter)}
    publish = previews._publish

    async def refusing(session: AsyncSession, run: StepRun, links: list[PreviewLink]) -> None:
        if links:
            raise ValueError("preview URL must not be on the dashboard's own origin")
        await publish(session, run, links)

    monkeypatch.setattr("orchestrator.worker.previews._publish", refusing)
    await run_tick(world)  # the rework continues the session
    await run_tick(world)  # the loop's request on the rework is answered

    attempt_1, attempt_2 = await plan_runs(world)
    assert await _stop_reasons(world, attempt_1) == ["continued"]
    assert await _run_details(world, attempt_2, EventType.PREVIEW_STARTED) == []
    (request,) = await _run_details(world, attempt_2, EventType.PREVIEW_REQUESTED)
    assert request["requested_by"] == "factorial" and request["renewal"] is True
    assert [run_id for run_id, _ in adapter.preview_starts] == ["run-1", "run-2"]


async def test_a_carry_that_cannot_read_the_earlier_run_leaves_the_rework_spawned(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rework is launched: an aborted transaction would roll its row back, and the next
    tick would spawn it again into the same workspace."""
    world, adapter = await _previewed_rework(maker, monkeypatch, serving=True)
    adopt, lifecycle = adapter.spawn, previews.preview_lifecycle
    adopted = False

    async def spawn(run_spec: RunSpec) -> RunHandle:
        nonlocal adopted
        handle = await adopt(run_spec)
        adopted = True
        return handle

    async def failing(session: AsyncSession, run: StepRun) -> Any:
        if adopted:
            await session.execute(text("select 1 / 0"))
        return await lifecycle(session, run)

    monkeypatch.setattr(adapter, "spawn", spawn)
    monkeypatch.setattr("orchestrator.worker.previews.preview_lifecycle", failing)
    await run_tick(world)  # the rework continues the session

    _, attempt_2 = await plan_runs(world)
    assert attempt_2.runtime_run_id == "run-2"
    assert attempt_2.status == StepRunStatus.RUNNING
    assert len(await _details(world, EventType.AGENT_SPAWNED)) == 2
