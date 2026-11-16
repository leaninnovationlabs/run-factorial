"""A step's declared files in the loop (tech-stack decision 71): the spawn records where the step's
branches stood, and when the run ends the worker records every file the step changed there."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import StepRunStatus
from orchestrator.events import EventType, append
from orchestrator.events.outputs import heads_by_name
from orchestrator.github import BranchState, TreeEntry
from orchestrator.github.contents import COMPARE_FILES_MAX, ContentsNotFound
from orchestrator.worker.outputs import FOOTPRINT_MAX, _capped
from runtime.contract import Artifact, ArtifactKind, RunStatus, ShownFile
from tests.reconcile_world import _build_world, _process, event_rows, run_tick, step_runs

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

FILES: list[dict[str, str]] = [
    {"path": "specs/{ticket}-{slug}/spec.md", "label": "Spec"},
    {"path": "specs/{ticket}-{slug}/plan.md", "label": "Plan"},
    {"path": "specs/{ticket}-{slug}/tasks.md", "label": "Tasks"},
]


def filed_process(files: list[dict[str, str]] = FILES) -> ProcessConfig:
    """`_process("proc")` with `plan` declaring its three spec files."""
    raw: dict[str, Any] = _process("proc").model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {"files": files}
    return ProcessConfig.model_validate(raw)


async def _spawned(world: Any) -> list[dict[str, Any]]:
    return [
        event.details
        for event in await event_rows(world)
        if event.type == EventType.AGENT_SPAWNED
    ]


# ------------------------------------------------------------------- what the run is told


async def test_the_run_is_told_each_declared_file_with_its_label(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    files = [{"path": "specs/{ticket}-{slug}/summary.html", "label": "Summary"}, *FILES[:1]]
    files.append({"path": "specs/{ticket}-{slug}/tasks.md"})
    world = await _build_world(maker, config=filed_process(files))

    await run_tick(world)

    (spec,) = world.adapter.spawns
    assert spec.shown_files == (
        ShownFile(path="specs/12-{slug}/summary.html", label="Summary"),
        ShownFile(path="specs/12-{slug}/spec.md", label="Spec"),
        ShownFile(path="specs/12-{slug}/tasks.md", label="tasks.md"),
    )
    # A file is read from GitHub, never from the run: it needs no kept sandbox.
    assert spec.public is False


async def test_a_step_that_declares_no_files_tells_the_run_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)

    await run_tick(world)

    assert world.adapter.spawns[0].shown_files == ()


async def test_a_disabled_preview_tells_the_run_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    raw: dict[str, Any] = _process("proc").model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {"enabled": False, "files": FILES}
    world = await _build_world(maker, config=ProcessConfig.model_validate(raw))

    await run_tick(world)

    assert world.adapter.spawns[0].shown_files == ()


async def test_a_retired_page_tells_the_run_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A ticket pinned with `preview.page` keeps parsing it, and its runs are told nothing of it."""
    raw: dict[str, Any] = _process("proc").model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {"page": {"branch": "pages/{ticket}-{slug}", "path": "s.html"}}
    world = await _build_world(maker, config=ProcessConfig.model_validate(raw))

    await run_tick(world)

    assert world.adapter.spawns[0].shown_files == ()


# ------------------------------------------------------------------------ the heads at spawn


async def test_a_spawn_records_the_heads_of_the_branches_it_may_push(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A branch name is data: one the event guard would read as a credential field (`key`) still
    records, and the spawn happens once (ticket 205 looped on `...-authorization-record`)."""
    world = await _build_world(maker, config=filed_process())
    world.github.branches.append(BranchState(name="spec/12-api-key-rotation", sha="s0"))
    world.github.branches.append(BranchState(name="impl/12-widget", sha="i0"))

    await run_tick(world)

    (spawned,) = await _spawned(world)
    assert spawned["branch_heads"] == [{"name": "spec/12-api-key-rotation", "sha": "s0"}]
    assert len(world.adapter.spawns) == 1


def test_spawn_heads_read_in_either_shape() -> None:
    """Events written before the list shape keep their name -> sha map for ever."""
    assert heads_by_name([{"name": "a", "sha": "1"}]) == {"a": "1"}
    assert heads_by_name({"a": "1"}) == {"a": "1"}
    assert heads_by_name(None) == {}


async def test_a_spawn_with_no_branch_yet_records_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=filed_process())

    await run_tick(world)

    (spawned,) = await _spawned(world)
    assert spawned["branch_heads"] == []


async def test_a_step_that_declares_no_files_records_no_heads(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    world.github.branches.append(BranchState(name="spec/12-widget", sha="s0"))

    await run_tick(world)

    (spawned,) = await _spawned(world)
    assert "branch_heads" not in spawned


# ------------------------------------------------------------------------ the record at the end

SPEC = "specs/12-widget/spec.md"
PLAN = "specs/12-widget/plan.md"
TASKS = "specs/12-widget/tasks.md"
NOTES = "notes.txt"


def _seed_main(world: Any) -> None:
    world.github.branches.append(BranchState(name="main", sha="m0"))
    world.github.trees["m0"] = (TreeEntry(path="README.md", sha="r0", size=10),)


def _file(
    path: str, blob: str | None, changed: bool | None, size: int | None = 100
) -> dict[str, Any]:
    return {"path": path, "blob": blob, "size": size, "changed": changed}


async def _outputs(world: Any) -> list[tuple[int | None, dict[str, Any]]]:
    return [
        (event.step_run_id, event.details)
        for event in await event_rows(world)
        if event.type == EventType.RUN_OUTPUTS_OBSERVED
    ]


async def _end(
    world: Any,
    outcome: RunStatus = RunStatus.FAILED,
    delivered: tuple[str, ...] = ("spec/12-widget",),
) -> None:
    """The attempt already spawned ends, reporting `delivered` as its branches."""
    artifacts = tuple(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": b}) for b in delivered)
    world.adapter.complete(outcome, artifacts=artifacts)
    await run_tick(world)


async def _next_attempt(world: Any) -> None:
    """Attempt 1 failed; wait out the backoff and spawn attempt 2."""
    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)


async def _filed_world(maker: async_sessionmaker[AsyncSession]) -> Any:
    world = await _build_world(maker, config=filed_process())
    _seed_main(world)
    return world


async def test_a_first_attempt_records_what_it_added_to_a_new_branch(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit(
        "spec/12-widget", "s1", {SPEC: "b1", PLAN: "b2", TASKS: "b3", NOTES: "b4"}, parent="m0"
    )

    await _end(world)
    await run_tick(world)  # a later tick must not record the same run again

    plan = (await step_runs(world))[0]
    assert await _outputs(world) == [
        (
            plan.id,
            {
                "step": "plan",
                "attempt": 1,
                "status": "observed",
                "branch": "spec/12-widget",
                "commit": "s1",
                "start": "m0",
                "start_at_spawn": False,
                "slug": "widget",
                "cut_off": False,
                "files": [
                    _file(SPEC, "b1", True),
                    _file(PLAN, "b2", True),
                    _file(TASKS, "b3", True),
                    _file(NOTES, "b4", True),
                ],
            },
        )
    ]


async def test_a_rework_keeps_the_files_it_did_not_touch(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit(
        "spec/12-widget", "s1", {SPEC: "b1", PLAN: "b2", TASKS: "b3", NOTES: "b4"}, parent="m0"
    )
    await _end(world)
    await _next_attempt(world)
    world.github.push_commit("spec/12-widget", "s2", {TASKS: "b5"}, parent="s1")

    await _end(world)

    second = (await step_runs(world))[1]
    details = dict(await _outputs(world))[second.id]
    assert (details["start"], details["start_at_spawn"], details["commit"]) == ("s1", True, "s2")
    assert details["files"] == [
        _file(SPEC, "b1", False),
        _file(PLAN, "b2", False),
        _file(TASKS, "b5", True),
        _file(NOTES, "b4", False),
    ]


async def test_a_step_on_another_step_s_branch_records_only_its_own_commits(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
    await run_tick(world)  # spawns with the branch already at s1
    world.github.push_commit("spec/12-widget", "s2", {"reports/12.md": "b9"}, parent="s1")

    await _end(world)

    ((_, details),) = await _outputs(world)
    assert details["files"] == [_file("reports/12.md", "b9", True)]


async def test_a_run_that_pushed_nothing_records_no_files(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
    await run_tick(world)

    await _end(world)

    ((_, details),) = await _outputs(world)
    assert (details["status"], details["commit"], details["files"]) == ("observed", "s1", [])


async def test_no_branch_is_recorded_as_no_branch(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)

    await _end(world, delivered=())

    ((_, details),) = await _outputs(world)
    assert details == {"step": "plan", "attempt": 1, "status": "no_branch", "branch": "spec/12-*"}


async def test_two_branches_are_recorded_as_ambiguous(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-b", "sb", {SPEC: "bb"}, parent="m0")
    world.github.push_commit("spec/12-a", "sa", {SPEC: "ba"}, parent="m0")

    await _end(world, delivered=())

    ((_, details),) = await _outputs(world)
    assert (details["status"], details["matches"]) == ("ambiguous", ["spec/12-a", "spec/12-b"])


async def test_a_cut_off_comparison_still_finds_a_declared_file(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    many = {f"gen/{n}.txt": f"g{n}" for n in range(COMPARE_FILES_MAX)}
    world.github.push_commit("spec/12-widget", "s1", {**many, SPEC: "b1"}, parent="m0")
    listed = world.github.comparisons[("m0", "s1")]
    world.github.comparisons[("m0", "s1")] = replace(
        listed, files=tuple(f for f in listed.files if f.path != SPEC)
    )

    await _end(world)

    ((_, details),) = await _outputs(world)
    assert details["cut_off"] is True
    assert len(details["files"]) == FOOTPRINT_MAX
    assert _file(SPEC, "b1", None) in details["files"]


async def test_a_timed_out_run_is_recorded_before_its_ticket_can_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")

    world.clock.advance(timedelta(hours=2))  # past `defaults.timeout: 1h`
    await run_tick(world)

    plan = (await step_runs(world))[0]
    assert plan.status == StepRunStatus.TIMEOUT
    ((run_id, details),) = await _outputs(world)
    assert run_id == plan.id
    # No slug of its own, but the glob finds the one branch.
    assert (details["status"], details["branch"]) == ("observed", "spec/12-widget")
    assert details["files"] == [_file(SPEC, "b1", True)]


async def test_an_interrupted_run_is_not_recorded_until_it_ends(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
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
    await run_tick(world)

    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.INTERRUPTED
    assert await _outputs(world) == []


async def test_a_github_error_is_retried_on_a_later_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
    world.github.compare_error = RuntimeError("GitHub is down")

    await _end(world)
    assert await _outputs(world) == []

    world.github.compare_error = None
    await run_tick(world)
    await run_tick(world)  # once recorded, never asked for again

    ((_, details),) = await _outputs(world)
    assert details["status"] == "observed"


async def test_a_look_up_still_failing_when_the_next_attempt_exists_is_given_up(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """By then the branch may hold the newer attempt's push: a late look-up could credit the older
    run with the newer one's files."""
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
    world.github.compare_error = RuntimeError("GitHub is down")
    await _end(world)
    await _next_attempt(world)

    await run_tick(world)

    first = (await step_runs(world))[0]
    assert dict(await _outputs(world))[first.id] == {
        "step": "plan",
        "attempt": 1,
        "status": "lookup_failed",
        "reason": "GitHub could not be read before the step's next attempt started",
    }


async def test_a_step_without_declared_files_records_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker)
    _seed_main(world)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")

    await _end(world)
    await run_tick(world)

    assert await _outputs(world) == []
    assert world.github.compare_calls == []


async def test_a_comparison_github_refuses_is_recorded_not_asked_again(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A 404 or 422 is GitHub's answer, not an outage: asking on every tick would never end for a
    run whose step has moved on."""
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1"}, parent="m0")
    world.github.compare_error = ContentsNotFound("acme cannot compare m0...s1")

    await _end(world)
    await run_tick(world)

    ((_, details),) = await _outputs(world)
    assert details == {
        "step": "plan",
        "attempt": 1,
        "status": "lookup_failed",
        "commit": "s1",
        "reason": "acme cannot compare m0...s1",
    }
    assert len(world.github.compare_calls) == 1


async def test_a_rework_on_a_new_branch_carries_nothing_from_the_old_one(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1", NOTES: "b4"}, parent="m0")
    await _end(world)
    await _next_attempt(world)
    world.github.push_commit("spec/12-gadget", "g1", {"specs/12-gadget/spec.md": "b7"}, parent="m0")

    await _end(world, delivered=("spec/12-gadget",))

    second = (await step_runs(world))[1]
    details = dict(await _outputs(world))[second.id]
    assert details["branch"] == "spec/12-gadget"
    assert details["files"] == [_file("specs/12-gadget/spec.md", "b7", True)]


async def test_an_earlier_attempt_with_two_records_carries_only_its_latest(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A step that awaits review records a run at each idle spell and at its end. Read from every
    record, a file the attempt wrote and then deleted stays in the next attempt's footprint."""
    world = await _filed_world(maker)
    await run_tick(world)
    world.github.push_commit("spec/12-widget", "s1", {SPEC: "b1", NOTES: "b4"}, parent="m0")
    await _end(world)
    world.github.push_commit("spec/12-widget", "s2", {NOTES: None}, parent="s1")
    first = (await step_runs(world))[0]
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.RUN_OUTPUTS_OBSERVED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=first.id,
            step="plan",
            attempt=1,
            status="observed",
            branch="spec/12-widget",
            commit="s2",
            start="m0",
            start_at_spawn=False,
            slug="widget",
            cut_off=False,
            files=[_file(SPEC, "b1", True)],
        )
        await session.commit()
    await _next_attempt(world)
    world.github.push_commit("spec/12-widget", "s3", {TASKS: "b5"}, parent="s2")

    await _end(world)

    second = (await step_runs(world))[1]
    details = dict(await _outputs(world))[second.id]
    assert details["files"] == [_file(SPEC, "b1", False), _file(TASKS, "b5", True)]


def test_a_full_record_keeps_a_patterns_matches_before_undeclared_files() -> None:
    gen = [f"gen/{n}.txt" for n in range(FOOTPRINT_MAX - 1)]
    paths = [*gen, "reports/12/a.html", "reports/12/sub/b.html", "reports/12/b.html", SPEC]
    kept = _capped(paths, {SPEC}, ["reports/12/*.html"])
    assert kept == [*gen[: FOOTPRINT_MAX - 3], "reports/12/a.html", "reports/12/b.html", SPEC]


def test_a_record_under_the_limit_keeps_every_path() -> None:
    paths = ["a.txt", "reports/12/a.html"]
    assert _capped(paths, set(), ["reports/12/*.html"]) == paths
