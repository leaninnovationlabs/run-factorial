"""The declared files of a run, selected from its recorded footprint (tech-stack decision 71): one
pure function, so the API's answer and the page route's check cannot disagree."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import PreviewFile
from orchestrator.db.models import StepKind, StepRun, StepRunStatus
from orchestrator.events import EventType, append
from orchestrator.events.outputs import (
    SelectedFile,
    files_on_branch,
    outputs_observation,
    select_files,
)
from tests.api_harness import seed_project, seed_ticket

SPEC = PreviewFile(path="specs/{ticket}-{slug}/spec.md", label="Spec")
PLAN = PreviewFile(path="specs/{ticket}-{slug}/plan.md", label="Plan")
TASKS = PreviewFile(path="specs/{ticket}-{slug}/tasks.md")
REPORTS = PreviewFile(path="reports/{ticket}/*.html", label="Reports")


def _observed(*files: dict[str, Any], slug: str | None = "widget") -> dict[str, Any]:
    return {
        "status": "observed",
        "branch": "spec/12-widget",
        "commit": "c1",
        "start": "m0",
        "start_at_spawn": False,
        "slug": slug,
        "cut_off": False,
        "files": list(files),
    }


def _file(path: str, blob: str | None = "b1", changed: bool | None = True) -> dict[str, Any]:
    return {"path": path, "blob": blob, "size": 100, "changed": changed}


def _published(declared: PreviewFile, label: str, path: str, blob: str) -> SelectedFile:
    return SelectedFile(
        key=declared.path,
        label=label,
        status="published",
        path=path,
        blob=blob,
        commit="c1",
        size=100,
        changed=True,
        matches=(),
        reason=None,
    )


def _matched(path: str, label: str, blob: str, total: int) -> SelectedFile:
    return SelectedFile(
        key=f"{REPORTS.path}#{path}",
        label=label,
        status="published",
        path=path,
        blob=blob,
        commit="c1",
        size=100,
        changed=True,
        matches=(),
        reason=None,
        group="Reports",
        group_total=total,
        folder="reports/12",
    )


def test_each_declared_file_is_selected_in_declaration_order() -> None:
    observed = _observed(
        _file("specs/12-widget/spec.md", "b1"),
        _file("notes.txt", "b2"),
        _file("specs/12-widget/plan.md", "b3"),
    )

    assert select_files([SPEC, PLAN], observed, ticket="12") == [
        _published(SPEC, "Spec", "specs/12-widget/spec.md", "b1"),
        _published(PLAN, "Plan", "specs/12-widget/plan.md", "b3"),
    ]


def test_a_declared_file_the_step_did_not_produce_is_missing() -> None:
    (plan,) = select_files([PLAN], _observed(_file("specs/12-widget/spec.md")), ticket="12")

    assert (plan.status, plan.path, plan.blob) == ("missing", None, None)
    assert plan.reason == "The attempt left no specs/12-widget/plan.md on spec/12-widget."


def test_a_declared_file_removed_by_the_step_is_missing_with_a_reason() -> None:
    observed = _observed(_file("specs/12-widget/spec.md", blob=None))

    (spec,) = select_files([SPEC], observed, ticket="12")

    assert (spec.status, spec.path, spec.reason) == (
        "missing",
        "specs/12-widget/spec.md",
        "This attempt removed it.",
    )


def test_a_declared_file_removed_by_an_earlier_attempt_says_so() -> None:
    observed = _observed(_file("specs/12-widget/spec.md", blob=None, changed=False))

    (spec,) = select_files([SPEC], observed, ticket="12")

    assert (spec.status, spec.reason) == (
        "missing",
        "It was not on the branch at this attempt's end.",
    )


def test_a_declared_file_absent_from_a_cut_off_record_is_not_called_missing() -> None:
    observed = {**_observed(_file("gen/1.txt")), "cut_off": True}

    (spec,) = select_files([SPEC], observed, ticket="12")

    assert (spec.status, spec.reason) == (
        "lookup_failed",
        "This attempt changed more than 300 files, so this file may not be listed.",
    )


def test_an_unresolved_slug_matches_by_glob_and_two_matches_are_ambiguous() -> None:
    one = _observed(_file("specs/12-a/spec.md", "ba"), slug=None)
    two = _observed(_file("specs/12-a/spec.md"), _file("specs/12-b/spec.md"), slug=None)

    (found,) = select_files([SPEC], one, ticket="12")
    (unsure,) = select_files([SPEC], two, ticket="12")

    assert (found.status, found.path, found.blob) == ("published", "specs/12-a/spec.md", "ba")
    assert (unsure.status, unsure.blob) == ("ambiguous", None)
    assert unsure.matches == ("specs/12-a/spec.md", "specs/12-b/spec.md")


def test_nothing_observed_yet_is_pending() -> None:
    (spec,) = select_files([SPEC], None, ticket="12")
    (reports,) = select_files([REPORTS], None, ticket="12")

    assert (spec.status, spec.key, spec.label, spec.path) == ("pending", SPEC.path, "Spec", None)
    assert (reports.status, reports.key, reports.label, reports.group) == (
        "pending",
        REPORTS.path,
        "Reports",
        "Reports",
    )


def test_a_failed_or_ambiguous_record_reaches_every_entry() -> None:
    failed = {"status": "lookup_failed", "reason": "GitHub is down"}
    unsure = {"status": "ambiguous", "matches": ["spec/12-a", "spec/12-b"]}

    assert [
        (f.status, f.reason) for f in select_files([SPEC, PLAN, REPORTS], failed, ticket="12")
    ] == [
        ("lookup_failed", "GitHub is down"),
        ("lookup_failed", "GitHub is down"),
        ("lookup_failed", "GitHub is down"),
    ]
    assert [(f.status, f.matches) for f in select_files([SPEC, REPORTS], unsure, ticket="12")] == [
        ("ambiguous", ("spec/12-a", "spec/12-b")),
        ("ambiguous", ("spec/12-a", "spec/12-b")),
    ]
    (reports,) = select_files([REPORTS], failed, ticket="12")
    assert (reports.key, reports.label, reports.group) == (REPORTS.path, "Reports", "Reports")


def test_no_branch_reads_as_missing() -> None:
    no_branch = {"status": "no_branch", "branch": "spec/12-*"}
    spec, reports = select_files([SPEC, REPORTS], no_branch, ticket="12")

    assert (spec.status, spec.reason) == (
        "missing",
        "The agent didn't push a branch starting with spec/12-.",
    )
    assert (reports.status, reports.reason, reports.key, reports.group) == (
        "missing",
        "The agent didn't push a branch starting with spec/12-.",
        REPORTS.path,
        "Reports",
    )


def test_a_label_defaults_to_the_file_name() -> None:
    (tasks,) = select_files([TASKS], None, ticket="12")

    assert tasks.label == "tasks.md"


def test_a_pattern_shows_each_matching_file_by_path_named_below_its_folder() -> None:
    observed = _observed(
        _file("specs/12-widget/spec.md", "b0"),
        _file("reports/12/lint.html", "b2"),
        _file("notes.txt", "b3"),
        _file("reports/12/archive/old.html", "b4"),
        _file("reports/12/coverage.html", "b1"),
    )

    assert select_files([SPEC, REPORTS], observed, ticket="12") == [
        _published(SPEC, "Spec", "specs/12-widget/spec.md", "b0"),
        _matched("reports/12/coverage.html", "coverage.html", "b1", 2),
        _matched("reports/12/lint.html", "lint.html", "b2", 2),
    ]


def test_a_double_star_reaches_into_subfolders() -> None:
    docs = PreviewFile(path="docs/{ticket}/**/*", label="Docs")
    observed = _observed(
        _file("docs/12/notes.md"), _file("docs/12/b/index.html"), _file("docs/12/a/index.html")
    )

    shown = select_files([docs], observed, ticket="12")

    assert [(f.label, f.folder) for f in shown] == [
        ("a/index.html", "docs/12"),
        ("b/index.html", "docs/12"),
        ("notes.md", "docs/12"),
    ]


def test_a_literal_declared_after_a_pattern_still_shows_its_file() -> None:
    wide = PreviewFile(path="specs/{ticket}-{slug}/*.md", label="Docs")
    observed = _observed(_file("specs/12-widget/spec.md", "b1"), _file("specs/12-widget/extra.md"))

    extra, spec = select_files([wide, SPEC], observed, ticket="12")

    assert (extra.path, extra.group, extra.group_total) == ("specs/12-widget/extra.md", "Docs", 1)
    assert spec == _published(SPEC, "Spec", "specs/12-widget/spec.md", "b1")


def test_a_later_pattern_does_not_repeat_an_earlier_patterns_file() -> None:
    everything = PreviewFile(path="reports/**", label="All")
    observed = _observed(_file("reports/12/a.html"), _file("reports/x.txt"))

    shown = select_files([REPORTS, everything], observed, ticket="12")

    assert [(f.group, f.path, f.label) for f in shown] == [
        ("Reports", "reports/12/a.html", "a.html"),
        ("All", "reports/x.txt", "x.txt"),
    ]


def test_a_pattern_whose_every_file_another_entry_shows_adds_nothing() -> None:
    docs = PreviewFile(path="specs/{ticket}-{slug}/*.md", label="Docs")
    observed = _observed(_file("specs/12-widget/spec.md", "b1"))

    assert select_files([SPEC, docs], observed, ticket="12") == [
        _published(SPEC, "Spec", "specs/12-widget/spec.md", "b1")
    ]


def test_a_pattern_skips_a_file_the_step_removed() -> None:
    observed = _observed(_file("reports/12/gone.html", blob=None), _file("reports/12/a.html"))

    (shown,) = select_files([REPORTS], observed, ticket="12")

    assert (shown.path, shown.group_total) == ("reports/12/a.html", 1)


def test_a_pattern_that_matches_nothing_is_one_missing_entry() -> None:
    observed = _observed(_file("notes.txt"))

    assert select_files([REPORTS], observed, ticket="12") == [
        SelectedFile(
            key=REPORTS.path,
            label="Reports",
            status="missing",
            path=None,
            blob=None,
            commit=None,
            size=None,
            changed=None,
            matches=(),
            reason="The attempt left no file matching reports/12/*.html on spec/12-widget.",
            group="Reports",
            group_total=None,
            folder="reports/12",
        )
    ]


def test_a_pattern_that_matches_nothing_in_a_cut_off_record_is_not_called_missing() -> None:
    observed = {**_observed(_file("gen/1.txt")), "cut_off": True}

    (reports,) = select_files([REPORTS], observed, ticket="12")

    assert (reports.status, reports.reason) == (
        "lookup_failed",
        "This attempt changed more than 300 files, so a file matching reports/12/*.html may "
        "not be listed.",
    )


def test_a_cut_off_record_still_shows_the_matches_it_holds() -> None:
    observed = {**_observed(_file("reports/12/a.html", "b1")), "cut_off": True}

    assert select_files([REPORTS], observed, ticket="12") == [
        _matched("reports/12/a.html", "a.html", "b1", 1)
    ]


def test_a_pattern_keeps_its_first_fifty_matches_by_path_and_says_how_many_it_matched() -> None:
    observed = _observed(*(_file(f"reports/12/f{n:02}.html") for n in reversed(range(60))))

    shown = select_files([REPORTS], observed, ticket="12")

    assert [f.label for f in shown] == [f"f{n:02}.html" for n in range(50)]
    assert {f.group_total for f in shown} == {60}


def test_a_file_an_earlier_pattern_cut_off_still_shows_under_a_later_pattern() -> None:
    everything = PreviewFile(path="reports/**", label="All")
    late = PreviewFile(path="reports/{ticket}/f5*.html", label="Late")
    observed = _observed(*(_file(f"reports/12/f{n:02}.html") for n in range(60)))

    shown = select_files([everything, late], observed, ticket="12")

    assert [f.label for f in shown if f.group == "All"] == [f"12/f{n:02}.html" for n in range(50)]
    assert [f.label for f in shown if f.group == "Late"] == [f"f{n}.html" for n in range(50, 60)]


def test_a_matched_name_may_repeat_another_entrys_label() -> None:
    readme = PreviewFile(path="README.md")
    out = PreviewFile(path="out/*.md", label="Out")
    observed = _observed(_file("README.md"), _file("out/README.md"))

    shown = select_files([readme, out], observed, ticket="12")

    assert [f.label for f in shown] == ["README.md", "README.md"]
    assert len({f.key for f in shown}) == 2


def test_a_matched_name_does_not_change_when_another_file_matches() -> None:
    docs = PreviewFile(path="docs/{ticket}/**/*", label="Docs")

    (alone,) = select_files([docs], _observed(_file("docs/12/a/index.html")), ticket="12")
    first, _ = select_files(
        [docs],
        _observed(_file("docs/12/a/index.html"), _file("docs/12/b/index.html")),
        ticket="12",
    )

    assert (alone.key, alone.label) == (first.key, first.label)
    assert first.label == "a/index.html"


def test_a_pattern_whose_slug_the_run_never_named_shows_every_match() -> None:
    docs = PreviewFile(path="specs/{ticket}-{slug}/*.md", label="Docs")
    observed = _observed(_file("specs/12-a/x.md"), _file("specs/12-b/y.md"), slug=None)

    shown = select_files([docs], observed, ticket="12")

    assert [(f.status, f.label) for f in shown] == [
        ("published", "12-a/x.md"),
        ("published", "12-b/y.md"),
    ]


def test_files_on_branch_lists_the_records_paths_still_there() -> None:
    observed = _observed(_file("b.md"), _file("a.md"), _file("gone.md", blob=None))

    assert files_on_branch(observed) == ["a.md", "b.md"]
    assert files_on_branch(None) == []
    assert files_on_branch({"status": "no_branch", "branch": "spec/12-*"}) == []


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_latest_observation_of_a_run_is_its_record(async_session: AsyncSession) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="plan")
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=StepRunStatus.SUCCEEDED,
        runtime_run_id="run-1",
    )
    async_session.add(run)
    await async_session.flush()
    assert await outputs_observation(async_session, run) is None
    for status in ("lookup_failed", "observed"):
        await append(
            async_session,
            type=EventType.RUN_OUTPUTS_OBSERVED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=run.id,
            status=status,
        )

    seen = await outputs_observation(async_session, run)

    assert seen is not None and seen["status"] == "observed"
