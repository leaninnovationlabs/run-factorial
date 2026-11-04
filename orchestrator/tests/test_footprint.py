"""What a restart or a redo removes from GitHub: no database, no network — `footprint` is pure."""

from __future__ import annotations

from orchestrator.config import ProcessConfig
from orchestrator.github.state import BranchState, PullRequestState
from orchestrator.worker.footprint import footprint
from orchestrator.worker.redos import discard_set, redo_target
from tests.reconcile_world import (
    _process,
    _process_with_a_build_step,
    _process_with_test_and_code_gate,
    _process_without_gate_reject,
)

VALUES = {"ticket": "12", "repo": "acme/widgets", "ticket_owner": "operator"}


def _branch(name: str) -> BranchState:
    return BranchState(name=name, sha="sha")


def _pr(
    number: int, head: str, base: str = "main", *, state: str = "open", merged: bool = False
) -> PullRequestState:
    return PullRequestState(
        number=number,
        head_ref=head,
        base_ref=base,
        head_sha="sha",
        state=state,
        merged=merged,
        url=f"https://github.test/pr/{number}",
    )


def test_the_branch_templates_name_the_tickets_branches_and_nothing_else() -> None:
    """The template match is what catches a branch an interrupted or cancelled run pushed: such a
    run records no artifact. Another ticket's branch and an unrelated one are never touched."""
    found = footprint(
        _process_with_a_build_step("p"),
        VALUES,
        [_branch(n) for n in ("spec/12-widget", "impl/12-widget", "spec/13-other", "feature/12")],
        [],
    )
    assert found.branches == ("impl/12-widget", "spec/12-widget")


def test_a_template_without_the_ticket_cannot_be_attributed() -> None:
    cfg = _process("p").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "spec/{slug}"
    found = footprint(ProcessConfig.model_validate(cfg), VALUES, [_branch("spec/x")], [])
    assert found.branches == ()


def test_open_prs_close_and_merged_ones_are_judged_by_their_base() -> None:
    """An impl PR merged into the ticket's own spec branch is the ticket's business; a PR merged
    into `main` is work that left the ticket, so a fresh start would duplicate it. A merged PR
    whose head GitHub auto-deleted still counts."""
    found = footprint(
        _process_with_a_build_step("p"),
        VALUES,
        [_branch("spec/12-widget"), _branch("impl/12-widget")],
        [
            _pr(1, "spec/12-widget"),
            _pr(2, "impl/12-widget", "spec/12-widget", state="closed", merged=True),
            _pr(3, "spec/12-old", state="closed"),
            _pr(4, "spec/12-gone", state="closed", merged=True),
        ],
    )
    assert [pr.number for pr in found.close] == [1]
    assert [pr.number for pr in found.merged_outside] == [4]


def _ticketless(name: str) -> ProcessConfig:
    """A legal lane whose plan step names no ticket: neither its branch template nor its
    postconditions' globs carry `{ticket}`."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "spec/{slug}"
    cfg["steps"][0]["postconditions"] = {
        "all": ['branch_exists("spec/*")', 'pr_open("spec/*", "main")']
    }
    cfg["steps"][1]["gate"]["pr"]["head"] = "spec/*"
    return ProcessConfig.model_validate(cfg)


def test_a_ticketless_glob_names_nothing_of_another_tickets() -> None:
    """The delivery probe records every branch and pull request its glob matches; under a glob
    without `{ticket}` that is every ticket's, so neither the glob nor what it recorded names the
    ticket's work. A restart must not close or delete another ticket's."""
    found = footprint(
        _ticketless("p"),
        VALUES,
        [_branch("spec/gadget")],
        [_pr(8, "spec/gadget")],
    )
    assert (found.branches, found.close) == ((), ())


def test_a_postcondition_glob_with_the_ticket_names_its_branches() -> None:
    """The postconditions are what a fresh generation would find already satisfied, so a
    `{ticket}`-bearing glob there attributes a branch the workspace template cannot."""
    cfg = _process("p").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "spec/{slug}"
    found = footprint(
        ProcessConfig.model_validate(cfg),
        VALUES,
        [_branch("spec/12-widget"), _branch("spec/13-other")],
        [],
    )
    assert found.branches == ("spec/12-widget",)


def test_a_step_scoped_footprint_keeps_what_a_kept_step_shares() -> None:
    """Redo `test` in the full graph: its branch is `build`'s too, so nothing goes."""
    found = footprint(
        _process_with_test_and_code_gate("p"),
        VALUES,
        [_branch("spec/12-w"), _branch("impl/12-w")],
        [_pr(5, "impl/12-w")],
        steps=("test", "code_gate"),
    )
    assert (found.branches, found.close, found.kept) == ((), (), ("impl/12-w",))


def test_a_step_scoped_footprint_names_only_the_discarded_steps_work() -> None:
    found = footprint(
        _process_with_test_and_code_gate("p"),
        VALUES,
        [_branch("spec/12-w"), _branch("impl/12-w")],
        [_pr(5, "impl/12-w"), _pr(4, "spec/12-w", state="closed", merged=True)],
        steps=("build", "test", "code_gate"),
    )
    assert found.branches == ("impl/12-w",) and [p.number for p in found.close] == [5]
    assert found.merged == ()  # spec's merged PR is a kept step's


def test_a_kept_ticketless_template_keeps_what_a_discarded_glob_also_names() -> None:
    """Kept `plan` names its branch `spec/{slug}`; discarded `build` reads `spec/{ticket}-*`.
    The kept template has no `{ticket}`, but it still claims the branch, so the redo keeps it."""
    cfg = _process_with_a_build_step("p").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "spec/{slug}"
    cfg["steps"][0]["postconditions"] = {"all": ['branch_exists("spec/*")']}
    cfg["steps"][1]["gate"]["pr"]["head"] = "spec/*"
    cfg["steps"][2]["postconditions"]["all"].append(
        'pr_comment_contains("spec/{ticket}-*", "## Build")'
    )
    found = footprint(
        ProcessConfig.model_validate(cfg),
        VALUES,
        [_branch("spec/12-w"), _branch("impl/12-w")],
        [],
        steps=("build",),
    )
    assert (found.branches, found.kept) == (("impl/12-w",), ("spec/12-w",))


def test_redo_targets() -> None:
    cfg = _process_with_test_and_code_gate("p")
    assert redo_target(cfg, "test", "running") == "test"
    assert redo_target(cfg, "code_gate", "waiting_gate") == "build"  # onReject.rerunStep
    assert redo_target(cfg, "code_gate", "blocked") is None  # a gate that is not waiting
    assert redo_target(cfg, "done", "running") is None
    # The branch fallback: no `onReject`, so the agent step whose branch the gate reviews.
    assert redo_target(_process_without_gate_reject("p"), "spec_gate", "waiting_gate") == "plan"
    assert discard_set(cfg, "build") == ("build", "test", "code_gate")

    ambiguous = cfg.model_dump(mode="json", by_alias=True)
    del ambiguous["steps"][4]["gate"]["onReject"]  # `build` and `test` both push `impl/`
    assert redo_target(ProcessConfig.model_validate(ambiguous), "code_gate", "waiting_gate") is None

    # Only `build` pushes what `spec_gate` reviews, but `build` comes after the gate: redoing it
    # would leave the gate WAITING and current while the ticket moved on, which wedges it.
    downstream = cfg.model_dump(mode="json", by_alias=True)
    del downstream["steps"][1]["gate"]["onReject"]
    downstream["steps"][1]["gate"]["pr"]["head"] = "impl/{ticket}-*"
    downstream["steps"][3]["workspace"]["branch"] = "qa/{ticket}-{slug}"
    assert (
        redo_target(ProcessConfig.model_validate(downstream), "spec_gate", "waiting_gate") is None
    )
