"""ADLC config subsystem tests — the canonical sample plus the rejections that matter.

No database, no network: everything here is YAML text and Pydantic. The sample under
`docs/samples/factorial-simple/` is the format reference, so "the sample still loads" is the
regression test that keeps the models and the documented format from drifting apart.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from orchestrator.config import (
    ACTION_NAMES,
    PREDICATE_ARITY,
    ActionStep,
    AgentManifest,
    AgentStep,
    AllOf,
    Condition,
    ConfigError,
    FileInput,
    GateStep,
    Not,
    Outcome,
    PredicateCall,
    PreviewFile,
    PreviewPage,
    ProcessConfig,
    SourceInput,
    TemplateError,
    expand,
    expand_principals,
    expand_strict,
    load_agent,
    load_config_folder,
    load_process,
    load_processes,
    parse_agent,
    parse_mcp_server,
    parse_process,
)
from orchestrator.config.conditions import iter_conjunctive
from orchestrator.worker.templates import _expected_artifacts
from runtime.contract import ArtifactKind
from tests.paths import REPO_ROOT, SAMPLE, SAMPLE_PROCESS
from tests.reconcile_world import _process

#: A process this repo OWNS, describing the stacked lane: implementation stacks on an open spec
#: PR and exactly one pull request per ticket reaches the default branch. It used to be a byte
#: copy of one target repository's live file, with a test diffing the two — which made this
#: suite go red on a commit in a different repository, and bound it to one candidate when the
#: factory is meant to run several (PR #93).
STACKED_FIXTURE = REPO_ROOT / "orchestrator" / "tests" / "fixtures" / "stacked-lane"
STACKED_PROCESS = STACKED_FIXTURE / "processes" / "process-stacked-lane.yaml"

_CONDITION = TypeAdapter[Condition](Condition)


@pytest.fixture(scope="module")
def sample_process() -> ProcessConfig:
    return load_process(SAMPLE_PROCESS)


@pytest.fixture(scope="module")
def stacked_process() -> ProcessConfig:
    return load_process(STACKED_PROCESS)


@pytest.fixture(scope="module")
def sample_raw() -> dict[str, Any]:
    return yaml.safe_load(SAMPLE_PROCESS.read_text(encoding="utf-8"))


def _mutated(raw: dict[str, Any], mutate: Any) -> dict[str, Any]:
    clone = copy.deepcopy(raw)
    mutate(clone)
    return clone


# ------------------------------------------------------------------------- policy


def test_branch_protection_policy_defaults_to_advisory(sample_process: ProcessConfig) -> None:
    """A process that says nothing gets the behaviour that existed before the field did.
    Silence must never become stricter than it was, or adding the field to the models would
    change what every already-pinned process means."""
    assert sample_process.policy.branch_protection == "advisory"


@pytest.mark.parametrize("level", ["required", "advisory", "ignored"])
def test_branch_protection_policy_accepts_each_level(level: str) -> None:
    raw = yaml.safe_load(SAMPLE_PROCESS.read_text())
    raw["policy"] = {"branchProtection": level}

    assert ProcessConfig.model_validate(raw).policy.branch_protection == level


def test_branch_protection_policy_rejects_anything_else() -> None:
    """A closed set, checked at parse time. A typo like `enforced` must fail where the file is
    read, not resolve to some default and silently stop meaning what it says."""
    raw = yaml.safe_load(SAMPLE_PROCESS.read_text())
    raw["policy"] = {"branchProtection": "enforced"}

    with pytest.raises(ValidationError):
        ProcessConfig.model_validate(raw)


# ------------------------------------------------------------------------ runtime


def test_the_sample_names_the_fleet_runtime(sample_process: ProcessConfig) -> None:
    """The reference copy names the fleet's Claude runtime (decision 36)."""
    assert sample_process.runtime == "claude-api"


@pytest.mark.parametrize("slug", ["codex-prod", "a", "c1", "team-a-codex-2", "a" * 63])
def test_runtime_accepts_a_slug(sample_raw: dict[str, Any], slug: str) -> None:
    raw = _mutated(sample_raw, lambda r: r.__setitem__("runtime", slug))
    assert ProcessConfig.model_validate(raw).runtime == slug


@pytest.mark.parametrize(
    "bad",
    ["", "Codex", "-codex", "has_underscore", "has space", "with.dot", "a/b", "a" * 64],
)
def test_runtime_rejects_a_bad_slug(sample_raw: dict[str, Any], bad: str) -> None:
    """The slug is a Runtime row's key, so it fails where the file is read — and the message
    names the field, because a slug is not obviously a slug when it is one word in a YAML file."""
    raw = _mutated(sample_raw, lambda r: r.__setitem__("runtime", bad))
    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(raw)
    assert f"Runtime {bad!r} is not a slug" in str(excinfo.value)


def test_runtime_round_trips_through_json(sample_raw: dict[str, Any]) -> None:
    """The `process_version.content` snapshot path carries the key."""
    raw = _mutated(sample_raw, lambda r: r.__setitem__("runtime", "codex-prod"))
    process = ProcessConfig.model_validate(raw)
    dumped = process.model_dump(mode="json", by_alias=True)
    assert dumped["runtime"] == "codex-prod"
    assert ProcessConfig.model_validate(dumped) == process


def test_snapshot_without_runtime_still_parses(sample_process: ProcessConfig) -> None:
    """A snapshot written before the field existed has no `runtime` key at all, and is
    re-parsed on every tick — so it must still parse, for the worker to refuse it by name."""
    dumped = sample_process.model_dump(mode="json", by_alias=True)
    del dumped["runtime"]
    assert ProcessConfig.model_validate(dumped).runtime is None


# ----------------------------------------------------------------- the canonical sample


def test_sample_folder_loads() -> None:
    config = load_config_folder(SAMPLE)

    assert set(config.processes) == {"primary", "simple"}
    process = config.processes["simple"]
    assert process.name == "simple"
    assert process.step_names == (
        "plan",
        "spec_review_gate",
        "build",
        "test",
        "code_review_gate",
    )
    assert set(config.agents) == {"plan-agent", "build-agent", "test-agent"}
    # Folder name == the `agent:` name a step references.
    assert {s.agent for s in process.steps if isinstance(s, AgentStep)} == set(config.agents)


def test_sample_agent_step_shape(sample_process: ProcessConfig) -> None:
    """The PARSER's output types, read off the documented sample.

    What is asserted is that an agent step parses into the right structures — an `AgentStep` with
    typed inputs, a condition tree of `PredicateCall`s, an `Outcome`. What is NOT asserted is
    which agent runs, which step follows, or what a label is called: those are facts about one
    project's lane, and this factory is meant to run several. Restating them here binds the suite
    to a single candidate and turns every lane edit into a test edit — the tax that made
    `test_config.py` need three hand-edits in one day.

    The claims the literals were standing in for are enforced where they belong. That the graph
    is closed and terminates is validated by the PARSER, for every project rather than for the two
    that happen to have fixtures here — a dangling `next`, an unreachable terminal and a bad
    `rerunStep` are all `ConfigError` at load. That every role reference resolves is a test,
    because that one is only checked at run time.
    """
    plan = sample_process.step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.agent, "an agent step must name an agent"
    # Inputs parse into the closed set's types rather than staying strings.
    assert all(isinstance(i, SourceInput | FileInput) for i in plan.inputs)
    assert plan.on_success is not None
    assert plan.on_success.ticket_label is not None

    build = sample_process.step("build")
    assert isinstance(build, AgentStep)
    # A `file:` input is the shape a bare `source:` string is NOT — both must parse, and the
    # sample is where the format reference exercises the second one.
    assert any(isinstance(i, FileInput) for i in build.inputs)
    assert build.postconditions is not None
    # Postconditions parse into predicate CALLS, not free text: the closed library is what makes
    # them executable, and a string that merely looks like one would silently never be evaluated.
    assert all(isinstance(c, PredicateCall) for c in iter_conjunctive(build.postconditions))


def test_sample_rework_preconditions_carry_no_branch_guard(sample_process: ProcessConfig) -> None:
    """`plan` and `build` used to each carry a `not: branch_exists(...)` conjunct that
    made a rework triggered by their own gate's `onReject.rerunStep` un-restartable — retired for
    both (see `process.yaml`'s own precondition-site comments for the full trace of what the
    guard protected and what protects it now). One conjunct is left on each.

    Whole-object equality against `AllOf`, deliberately not the conjunctive walk (which only reads
    conjunctive positions and would silently skip a `not: branch_exists(...)` child if the guard
    ever came back — exactly the case this test exists to catch)."""
    plan = sample_process.step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.preconditions == AllOf(all=(PredicateCall(name="ticket_has_assignee", args=()),))

    build = sample_process.step("build")
    assert isinstance(build, AgentStep)
    assert build.preconditions == AllOf(
        all=(PredicateCall(name="pr_merged", args=("spec/{ticket}-*", "main")),)
    )


def test_sample_test_step_shape(sample_process: ProcessConfig) -> None:
    """The decoupled test step, between `build` and `code_review_gate`."""
    test_step = sample_process.step("test")
    assert isinstance(test_step, AgentStep)
    assert test_step.agent == "test-agent"
    assert test_step.next == "code_review_gate"
    # This step pushes nothing (README's seam note). `base` is a plain, always-resolvable
    # starting point — nothing reads it for this step (the test agent discovers the real impl
    # branch itself via `gh`); `branch` DOES matter, since `{pr_url}` resolution keys off it.
    assert test_step.workspace.base == "main"
    assert test_step.workspace.branch == "impl/{ticket}-{slug}"
    assert test_step.postconditions is not None
    assert list(iter_conjunctive(test_step.postconditions)) == [
        PredicateCall(name="pr_comment_contains", args=("impl/{ticket}-*", "## Test Verdict")),
    ]
    assert test_step.on_success is not None
    assert test_step.on_success.ticket_label == "factorial:code-review"
    assert test_step.on_success.assign == ("{roles.code_reviewers}",)


def test_sample_gate_wiring(sample_process: ProcessConfig) -> None:
    gate_step = sample_process.step("spec_review_gate")
    assert isinstance(gate_step, GateStep)
    gate = gate_step.gate
    assert gate.type == "pr_approval"
    assert (gate.pr.head, gate.pr.base) == ("spec/{ticket}-*", "main")
    assert gate.approvers == ("{roles.spec_reviewers}",)
    assert gate.waiting is not None and gate.waiting.ticket_label == "factorial:spec-review"
    assert gate.on_approve.merge is True
    assert gate.on_approve.ticket_label == "factorial:ready-for-build"
    assert gate.on_reject is not None
    assert gate.on_reject.rerun_step == "plan"
    assert gate.on_reject.inputs == (SourceInput(source="pr_review_comments"),)
    assert gate_step.next == "build"

    assert sample_process.terminal.step == "done"
    assert sample_process.terminal.actions.close_ticket is True


def test_sample_code_review_gate_rerun_retraverses_test(sample_process: ProcessConfig) -> None:
    """A GRAPH-SHAPE claim: `code_review_gate.onReject` reruns `build`, not `test` — and because
    `build.next` is `test`, the config graph routes a rerun of build back through test before
    `code_review_gate` again.

    `build`'s own precondition once carried `not: branch_exists("impl/{ticket}-*")`, true only
    before the FIRST attempt's branch existed — once it did, `_start_attempt`'s precondition
    guard declined the rerun silently, every tick, rather than actually reaching `test` a second
    time. That guard is retired (see `process.yaml`'s own precondition-site comment for the full
    trace of what it protected and what protects it now), and the LIVE claim is proven for real:
    `test_reconcile.test_build_rework_reaches_test_and_code_review_gate_again` drives the actual
    reconcile loop, against real Postgres, through build -> test -> code_review_gate a second time.
    `test_reconcile._process_with_a_branch_guard` still exercises the general wedge machinery
    the retired guard used to trigger, kept for that purpose even though no shipped config
    carries the guard anymore."""
    code_review_gate = sample_process.step("code_review_gate")
    assert isinstance(code_review_gate, GateStep)
    assert code_review_gate.gate.on_reject is not None
    assert code_review_gate.gate.on_reject.rerun_step == "build"

    build = sample_process.step("build")
    assert isinstance(build, AgentStep)
    assert build.next == "test"

    test_step = sample_process.step("test")
    assert isinstance(test_step, AgentStep)
    assert test_step.next == "code_review_gate"


def test_sample_durations_parse(sample_process: ProcessConfig) -> None:
    assert sample_process.defaults.timeout == timedelta(hours=1)
    assert sample_process.defaults.retry is not None
    assert sample_process.defaults.retry.max_attempts == 2
    assert sample_process.defaults.retry.backoff == timedelta(minutes=10)


def test_sample_agent_manifest() -> None:
    manifest = load_agent(SAMPLE / "agents" / "plan-agent" / "agent.yaml")
    assert manifest.name == "plan-agent"
    assert manifest.model == "claude-opus-5-5"
    assert manifest.limits.max_duration is None
    assert manifest.limits.max_turns == 200
    assert manifest.skills.shared == ("project-constitution", "spec-page")
    assert [s.key for s in manifest.secrets] == ["GH_TOKEN"]
    assert manifest.runtime is None
    # No `permissions:` key in the sample — absent means "the default profile", not "nothing"
    # (orchestrator.factorial.permission_policy), and that distinction is why this is `None`, not an
    # empty `Permissions()`.
    assert manifest.permissions is None


def test_sample_test_agent_manifest() -> None:
    """`test-agent` follows the same runtime-neutral shape as its siblings."""
    manifest = load_agent(SAMPLE / "agents" / "test-agent" / "agent.yaml")
    assert manifest.name == "test-agent"
    assert manifest.model == "claude-opus-5-5"
    assert manifest.limits.max_duration is None
    assert manifest.limits.max_turns == 200
    assert manifest.skills.shared == ("project-constitution",)
    assert [s.key for s in manifest.secrets] == ["GH_TOKEN"]
    assert manifest.runtime is None
    assert manifest.permissions is None


def test_a_manifests_runtime_block_is_ignored() -> None:
    """The process's Runtime says which CLI runs an agent; a manifest that still declares one
    parses, and the block is not kept."""
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw["runtime"] = {"kind": "codex"}
    assert "runtime" not in AgentManifest.model_validate(raw).model_dump()


# --------------------------------------------------------------- the stacked lane


def test_stacked_process_loads() -> None:
    """The stacked lane round-trips through the real parser.

    A genuinely different graph from `simple`: implementation stacks on an OPEN spec
    PR, so
    an impl PR targets `spec/*` and exactly one pull request per ticket reaches the default
    branch. Worth its own parse test rather than riding on the sample's.

    Asserts properties, never literals. Step names and version numbers used to be restated here
    from another repository's file, which meant editing this suite whenever someone edited that
    one — twice in a single day, neither time revealing anything about the orchestrator.
    """
    config = load_process(STACKED_PROCESS)

    assert config.terminal.actions.close_ticket is True


def _principal_lists(config: ProcessConfig) -> list[tuple[str, Sequence[str]]]:
    """Every `assign:` / `approvers:` list in a config, with where it came from."""
    found: list[tuple[str, Sequence[str]]] = []

    def outcome(where: str, out: Outcome | None) -> None:
        if out is not None and out.assign is not None:
            found.append((where, out.assign))

    outcome("terminal.actions", config.terminal.actions)
    if config.defaults.on_failure is not None:
        outcome("defaults.onFailure.escalate", config.defaults.on_failure.escalate)
    for step in config.steps:
        if isinstance(step, GateStep):
            found.append((f"{step.name}.gate.approvers", step.gate.approvers))
            outcome(f"{step.name}.gate.waiting", step.gate.waiting)
            outcome(f"{step.name}.gate.onApprove", step.gate.on_approve)
            outcome(f"{step.name}.gate.onReject", step.gate.on_reject)
        else:
            outcome(f"{step.name}.onSuccess", getattr(step, "on_success", None))
            on_failure = getattr(step, "on_failure", None)
            if on_failure is not None:
                outcome(f"{step.name}.onFailure.escalate", on_failure.escalate)
    return found


@pytest.mark.parametrize("fixture", ["sample_process", "stacked_process"])
def test_every_role_reference_in_a_process_resolves(fixture: str, request: Any) -> None:
    """A `{roles.x}` naming no declared role is a typo that reaches production.

    `expand_principals` raises `TemplateError` for an unknown role, but it raises at RUN time —
    when a gate is being projected for a real ticket. So the failure lands mid-flight on a live
    ticket rather than when the config was written, and the person who sees it is not the person
    who made the typo.

    Asserted over every principal list a config has, for both fixtures, because this is a
    property of a well-formed process rather than a fact about one project's lane. A factory that
    runs several projects gets this check for each of them by naming the fixture, not by growing
    a new suite of shape assertions.
    """
    config: ProcessConfig = request.getfixturevalue(fixture)
    lists = _principal_lists(config)
    assert lists, "a process with no assignees or approvers routes work to nobody"

    for where, entries in lists:
        try:
            expand_principals(entries, config.roles, {"ticket_owner": "operator"})
        except TemplateError as exc:  # pragma: no cover - the assertion carries the diagnosis
            pytest.fail(f"{where}: {exc}")


def test_stacked_gate_one_approves_without_merging(stacked_process: ProcessConfig) -> None:
    """The single most load-bearing difference from the sample.

    `simple`'s `spec_review_gate.onApprove.merge` is `true`; here it must be `false`.
    The spec
    branch is the base `build`'s own PR targets, so merging at gate 1 would delete it and
    `build`'s `pr_open("impl/…", "spec/…")` postcondition could never hold.
    """
    spec_review_gate = stacked_process.step("spec_review_gate")
    assert isinstance(spec_review_gate, GateStep)
    gate = spec_review_gate.gate
    assert (gate.pr.head, gate.pr.base) == ("spec/{ticket}-*", "main")
    assert spec_review_gate.gate.on_approve.merge is False
    assert spec_review_gate.gate.on_reject is not None
    assert spec_review_gate.gate.on_reject.rerun_step == "plan"
    assert spec_review_gate.gate.on_reject.inputs == (SourceInput(source="pr_review_comments"),)


def test_stacked_build_waits_on_an_open_spec_pr_not_a_merged_one(
    stacked_process: ProcessConfig,
) -> None:
    """The precondition inversion the factory lane turns on.

    The sample waits for `pr_merged("spec/…", "main")`. Here the spec never merges before build
    — gate 1 does not merge it — so build waits on the spec PR being OPEN instead. Whole-object
    equality against `AllOf`, deliberately not the conjunctive walk: a `pr_merged` conjunct
    sneaking back in is precisely what this pins against, and the walk reads past wrappers.
    """
    build = stacked_process.step("build")
    assert isinstance(build, AgentStep)
    assert build.preconditions == AllOf(
        all=(PredicateCall(name="pr_open", args=("spec/{ticket}-*", "main")),)
    )
    # The impl PR's base is the spec branch, globbed — `pr_open` globs base symmetrically with
    # head (`predicates/library.py`), so the plan agent's own slug choice is matched.
    assert build.postconditions is not None
    assert list(iter_conjunctive(build.postconditions)) == [
        PredicateCall(name="branch_exists", args=("impl/{ticket}-*",)),
        PredicateCall(name="pr_open", args=("impl/{ticket}-*", "spec/{ticket}-*")),
    ]
    # `build.next` is deliberately not asserted: which step follows is a property of someone
    # else's lane, and `test_a_process_graph_is_closed_and_terminates` already proves it resolves.


def test_stacked_code_review_gate_merges_into_the_spec_branch(
    stacked_process: ProcessConfig,
) -> None:
    """`merge: true`, but the merge lands on the spec branch — nothing reaches `main` here."""
    code_review_gate = stacked_process.step("code_review_gate")
    assert isinstance(code_review_gate, GateStep)
    assert (code_review_gate.gate.pr.head, code_review_gate.gate.pr.base) == (
        "impl/{ticket}-*",
        "spec/{ticket}-*",
    )
    assert code_review_gate.gate.on_approve.merge is True
    assert code_review_gate.gate.on_reject is not None
    assert code_review_gate.gate.on_reject.rerun_step == "build"
    assert code_review_gate.next == "spec_merge_gate"


def test_stacked_gate_two_is_the_only_edge_to_main(stacked_process: ProcessConfig) -> None:
    """The factory's gate 2, expressed as a second `pr_approval` gate.

    The format has exactly one gate type, and the spec PR's approval-and-merge to `main` IS
    gate 2 — so a second human gate is what the format can express, and it is honest rather than
    a workaround. "The merged implementation must be in the spec PR before gate 2" is NOT a
    condition expressible here; it holds structurally, because this step has exactly one inbound
    edge (`code_review_gate.next`) and arriving over it means the merge happened.

    Pinned as a whole-graph claim: exactly one step merges to `main`, and it is this one.
    """
    gate_two = stacked_process.step("spec_merge_gate")
    assert isinstance(gate_two, GateStep)
    assert (gate_two.gate.pr.head, gate_two.gate.pr.base) == ("spec/{ticket}-*", "main")
    assert gate_two.gate.on_approve.merge is True
    assert gate_two.next == stacked_process.terminal.step

    merging_to_main = [
        step.name
        for step in stacked_process.steps
        if isinstance(step, GateStep) and step.gate.on_approve.merge and step.gate.pr.base == "main"
    ]
    assert merging_to_main == ["spec_merge_gate"]

    # Exactly one inbound edge, which is what makes the ordering claim above structural rather
    # than merely intended. Computed from the whole graph, not asserted about
    # `code_review_gate` alone:
    # a future step pointing `next` or a rework edge here would break this, which is the point.
    inbound = [
        step.name
        for step in stacked_process.steps
        if step.next == "spec_merge_gate"
        or (
            isinstance(step, GateStep)
            and step.gate.on_reject is not None
            and step.gate.on_reject.rerun_step == "spec_merge_gate"
        )
    ]
    assert inbound == ["code_review_gate"]


def test_stacked_gate_two_has_no_rework_edge(stacked_process: ProcessConfig) -> None:
    """`spec_merge_gate` must carry NO `onReject` — a rejection blocks rather than loops.

    Not an omission. The original reason was a live engine defect: `pr_approved`
    (`predicates/library.py`) once filtered on neither state nor merge — it globbed
    head and base and returned true if ANY matching PR carried an approval. Once `code_review_gate`
    approved and merged impl PR #A, that PR was frozen merged-AND-approved, so a
    `rerunStep: build` edge here would have let a rework open an unreviewed #B, have the code
    review gate
    re-satisfied by #A's stale approval, and have `_find_pr` (prefer open, then newest) merge
    #B: the human code review skipped on every traversal after the first.

    A second failure sat on the same edge: a gate-2 reviewer who CLOSES the spec PR makes
    `build`'s `pr_open("spec/…", "main")` precondition false forever, and the rework declines
    silently with no event.

    So the rejection falls to `worker/gates._reject`'s no-`onReject` arm — BLOCKED, the
    blocked label, and an `ESCALATED` event, all visible. That arm ALSO calls
    `_apply_outcome`, so a declared `defaults.onFailure.escalate` contributes its comment and
    reassignment on this path too, not just the label.

    Gate-approval freshness has since landed: `pr_approved` now reads only OPEN pull requests,
    so #A's frozen approval can no longer re-satisfy a gate and the engine-side reason for this
    omission is gone. That closes exactly the frozen-MERGED-PR strand above — NOT the residual
    race, which hopes for the same "engine-side gate-approval freshness": that race is gate 1's
    approval read as live on the same STILL-OPEN spec PR inside GitHub's dismiss_stale_reviews
    propagation window, and an open-only filter changes nothing there; it remains open. Re-adding
    the edge is therefore now POSSIBLE for the strand this test pins, but the closed-spec-PR
    strand above is untouched by it and needs its own answer first. Until then this test pins the
    no-`onReject` shape.
    """
    gate_two = stacked_process.step("spec_merge_gate")
    assert isinstance(gate_two, GateStep)
    assert gate_two.gate.on_reject is None

    # `code_review_gate`'s own rework edge stays — on that path nothing merged, so no frozen
    # approval
    # exists and the loop is safe.
    code_review_gate = stacked_process.step("code_review_gate")
    assert isinstance(code_review_gate, GateStep)
    assert code_review_gate.gate.on_reject is not None
    assert code_review_gate.gate.on_reject.rerun_step == "build"


# ---------------------------------------------------- postcondition -> artifact derivation


def test_sample_build_derives_branch_and_pr_only(sample_process: ProcessConfig) -> None:
    """`ci_green` left `build`'s postconditions, but it was never derivable in the
    first place (`_DERIVABLE_PREDICATES` has no `ci_green` entry) — this pins that the move did
    not change what `build` derives, only what it no longer waits on itself."""
    build = sample_process.step("build")
    assert isinstance(build, AgentStep)
    expectations = _expected_artifacts(build, {"ticket": "12"})
    assert [(item.kind, dict(item.selector)) for item in expectations] == [
        (ArtifactKind.BRANCH, {"head_glob": "impl/12-*"}),
        (ArtifactKind.PR, {"head_glob": "impl/12-*"}),
    ]


def test_sample_test_step_derives_only_the_comment(sample_process: ProcessConfig) -> None:
    """`ci_green` is not derivable (no BRANCH/PR expectation from it — correct, it names no
    delivery); `pr_comment_contains` derives the COMMENT expectation carrying the marker and the
    impl PR's head glob — the artifact-expectation machinery closes end to end."""
    test_step = sample_process.step("test")
    assert isinstance(test_step, AgentStep)
    expectations = _expected_artifacts(test_step, {"ticket": "12"})
    assert [(item.kind, dict(item.selector)) for item in expectations] == [
        (
            ArtifactKind.COMMENT,
            {"marker": "## Test Verdict", "where": "pr", "head_glob": "impl/12-*"},
        )
    ]


# ------------------------------------------------------------------------------- kind: action


def _process_with_action_step(action: str = "fix_the_contract") -> dict[str, Any]:
    return {
        "specVersion": "adlc/v1",
        "kind": "Process",
        "name": "action-test",
        "version": 1,
        "entry": {"when": 'ticket_label("adlc:new")', "step": "fix"},
        "steps": [
            {
                "name": "fix",
                "kind": "action",
                "description": "Fix the executable contract",
                "action": action,
                "next": "done",
                "onSuccess": {"ticketLabel": "adlc:fixed"},
            }
        ],
        "terminal": {"step": "done"},
    }


def test_action_step_parses() -> None:
    process = ProcessConfig.model_validate(_process_with_action_step())
    step = process.step("fix")
    assert isinstance(step, ActionStep)
    assert step.kind == "action"
    assert step.action == "fix_the_contract"
    assert step.next == "done"
    assert step.description == "Fix the executable contract"
    assert step.on_success is not None
    assert step.on_success.ticket_label == "adlc:fixed"


def test_unregistered_action_name_fails_at_parse() -> None:
    """Mirrors `test_unknown_predicate_names_the_allowed_set`: config referencing an
    unregistered action must fail here, not at dispatch (`orchestrator.actions.registry`'s own
    import-time assert is the OTHER half of this same two-table discipline)."""
    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_process_with_action_step(action="deploy"))
    message = str(excinfo.value)
    assert "Unknown action 'deploy'" in message
    assert "fix_the_contract" in message


def test_action_registry_is_declared_once() -> None:
    assert frozenset({"fix_the_contract"}) == ACTION_NAMES


# ------------------------------------------------------------------ specVersion (issue #138)


@pytest.mark.parametrize("spec", ["adlc/v1", "factorial/v1"])
def test_every_kind_accepts_both_spec_versions(spec: str) -> None:
    """Both names parse, and neither is translated on the way in.

    `process_version.content` is append-only and re-parsed on every tick
    (`worker/tick_context.py::_load_pinned_config`), so every snapshot written before the rename
    says `adlc/v1` for ever. Narrowing this literal would fail the tick of every live ticket.
    """
    process = ProcessConfig.model_validate(
        _process_with_action_step() | {"specVersion": spec},
    )
    assert process.spec_version == spec

    manifest = yaml.safe_load((SAMPLE / "agents" / "plan-agent" / "agent.yaml").read_text())
    agent = AgentManifest.model_validate(manifest | {"specVersion": spec})
    assert agent.spec_version == spec

    server = parse_mcp_server(
        f"specVersion: {spec}\n"
        "kind: McpServer\n"
        "name: product-docs\n"
        "transport: http\n"
        "http:\n"
        "  url: https://docs.example.com/mcp\n",
        name="product-docs",
        origin="test:product-docs.yaml",
    )
    assert server.spec_version == spec


def test_an_unknown_spec_version_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        ProcessConfig.model_validate(_process_with_action_step() | {"specVersion": "adlc/v2"})


def test_step_union_discriminates_all_three_kinds() -> None:
    """`Step` still discriminates correctly with a third member in the union — an agent, an
    action, and a human gate in the SAME process, each parsing to its own model."""
    process = ProcessConfig.model_validate(
        {
            "specVersion": "adlc/v1",
            "kind": "Process",
            "name": "three-kinds",
            "version": 1,
            "entry": {"when": 'ticket_label("adlc:new")', "step": "plan"},
            "steps": [
                {
                    "name": "plan",
                    "kind": "agent",
                    "agent": "plan-agent",
                    "next": "fix",
                    "workspace": {
                        "repo": "{repo}",
                        "base": "main",
                        "branch": "spec/{ticket}-{slug}",
                    },
                },
                {
                    "name": "fix",
                    "kind": "action",
                    "action": "fix_the_contract",
                    "next": "gate",
                },
                {
                    "name": "gate",
                    "kind": "human_gate",
                    "next": "done",
                    "gate": {
                        "type": "pr_approval",
                        "pr": {"head": "spec/{ticket}-*", "base": "main"},
                        "onApprove": {},
                    },
                },
            ],
            "terminal": {"step": "done"},
        }
    )
    # Also proves graph reachability walks THROUGH an action step: `done` is only reachable via
    # plan -> fix -> gate, so a successful parse here means `_reachable` treated `fix`'s `next`
    # like every other step's, not like `GateStep`'s isinstance-special-cased `on_reject` edge.
    kinds = {step.name: type(step).__name__ for step in process.steps}
    assert kinds == {"plan": "AgentStep", "fix": "ActionStep", "gate": "GateStep"}


def test_action_step_next_must_exist() -> None:
    raw = _process_with_action_step()
    raw["steps"][0]["next"] = "nowhere"
    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(raw)
    assert "Step 'fix': next 'nowhere' is not a step." in str(excinfo.value)


# ------------------------------------------------------- snapshot round-trip (JSONB path)


def test_process_round_trips_through_json(sample_process: ProcessConfig) -> None:
    """This is the `process_version.content` snapshot path — it must be lossless."""
    dumped = sample_process.model_dump(mode="json", by_alias=True)
    assert ProcessConfig.model_validate(dumped) == sample_process
    # Serialized back to the YAML-ish shape, not to internal field names.
    assert dumped["defaults"]["retry"] == {"maxAttempts": 2, "backoff": "10m"}
    assert dumped["entry"]["when"] == {
        "all": ['ticket_label("factorial:new")', "ticket_has_assignee()"]
    }
    assert dumped["steps"][0]["inputs"] == ["ticket_body", "ticket_comments"]


def test_duration_serializes_compactly() -> None:
    process = load_process(SAMPLE_PROCESS)
    dumped = process.model_dump(mode="json", by_alias=True)
    assert dumped["defaults"]["timeout"] == "1h"


def test_manifest_permissions_round_trip_through_json() -> None:
    """`permissions:` stays snake_case in `agent.yaml` (unlike `process.yaml`'s camelCase), so
    this is the same lossless snapshot path `test_process_round_trips_through_json` proves for
    `ProcessConfig` — no alias to lose."""
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw["permissions"] = {"contents": "read", "checks": "write"}
    manifest = AgentManifest.model_validate(raw)

    assert manifest.permissions is not None
    assert manifest.permissions.contents == "read"
    assert manifest.permissions.checks == "write"
    assert manifest.permissions.issues is None

    dumped = manifest.model_dump(mode="json", by_alias=True)
    assert dumped["permissions"] == {
        "contents": "read",
        "issues": None,
        "pull_requests": None,
        "metadata": None,
        "checks": "write",
        "actions": None,
    }
    assert AgentManifest.model_validate(dumped) == manifest


# ------------------------------------------------------------------- condition parsing


@pytest.mark.parametrize(
    ("source", "name", "args"),
    [
        ('ticket_label("adlc:new")', "ticket_label", ("adlc:new",)),
        ("ticket_has_assignee()", "ticket_has_assignee", ()),
        ('branch_exists("spec/{ticket}-*")', "branch_exists", ("spec/{ticket}-*",)),
        (
            'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")',
            "file_exists",
            ("specs/{ticket}-*/spec.md", "spec/{ticket}-*"),
        ),
        ('pr_open("spec/{ticket}-*", "main")', "pr_open", ("spec/{ticket}-*", "main")),
        ('pr_merged("spec/{ticket}-*", "main")', "pr_merged", ("spec/{ticket}-*", "main")),
        ('pr_approved("a", "main")', "pr_approved", ("a", "main")),
        ('pr_approved("a", "main", "reviewers")', "pr_approved", ("a", "main", "reviewers")),
        ('ci_green("impl/{ticket}-*")', "ci_green", ("impl/{ticket}-*",)),
        ("ticket_label('single-quoted')", "ticket_label", ("single-quoted",)),
    ],
)
def test_predicate_call_shapes(source: str, name: str, args: tuple[str, ...]) -> None:
    call = _CONDITION.validate_python(source)
    assert call == PredicateCall(name=name, args=args)


def test_composition_and_not_nesting() -> None:
    node = _CONDITION.validate_python(
        {"all": [{"not": 'branch_exists("spec/1-*")'}, "ticket_has_assignee()"]}
    )
    assert node == AllOf(
        all=(
            Not(**{"not": PredicateCall(name="branch_exists", args=("spec/1-*",))}),
            PredicateCall(name="ticket_has_assignee"),
        )
    )
    assert _CONDITION.dump_python(node, mode="json", by_alias=True) == {
        "all": [{"not": 'branch_exists("spec/1-*")'}, "ticket_has_assignee()"]
    }


def test_predicate_registry_is_the_closed_library() -> None:
    # `orchestrator.predicates` imports this table; design.md §6 is the source.
    assert set(PREDICATE_ARITY) == {
        "branch_exists",
        "file_exists",
        "pr_open",
        "pr_merged",
        "pr_approved",
        "pr_changes_requested",
        "ci_green",
        "ticket_label",
        "ticket_has_assignee",
        "issue_comment_contains",
        "pr_comment_contains",
        "request_approval",
        "checklist_done",
    }
    assert PREDICATE_ARITY["pr_approved"] == (2, 3)


@pytest.mark.parametrize(
    "source",
    ['issue_comment_contains("")', 'pr_comment_contains("spec/1-*", "")'],
)
def test_comment_predicates_reject_an_empty_marker(source: str) -> None:
    """Mirrors `runtime.contract.ExpectedArtifact`'s own refusal of an empty `marker`: caught here
    too, at config load, so a broken postcondition fails once instead of wedging every spawn of
    the step it is on (`worker.templates._expected_artifacts` would otherwise try to build an
    `ExpectedArtifact` selector pydantic itself refuses, every single tick)."""
    with pytest.raises(ValidationError) as excinfo:
        _CONDITION.validate_python(source)
    assert "non-empty" in str(excinfo.value)


def test_unknown_predicate_names_the_allowed_set() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _CONDITION.validate_python('pr_reviewed("a")')
    message = str(excinfo.value)
    assert "Unknown predicate 'pr_reviewed'" in message
    assert "branch_exists/1" in message
    assert "ticket_has_assignee/0" in message


def test_wrong_arity_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _CONDITION.validate_python('file_exists("specs/1/spec.md")')
    assert "'file_exists' takes 2 argument(s), but" in str(excinfo.value)
    assert "passes 1." in str(excinfo.value)


@pytest.mark.parametrize(
    "source",
    [
        "branch_exists(main)",  # unquoted argument
        'branch_exists("a"',  # unterminated call
        'branch_exists("a) ',  # unterminated literal
        'all(branch_exists("a"))',  # nesting is not a call
        'branch_exists("a") or pr_open("a", "b")',  # not an expression language
        '"just a string"',
    ],
)
def test_malformed_call_strings_are_rejected(source: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _CONDITION.validate_python(source)
    assert "Malformed predicate call" in str(excinfo.value) or "Unknown predicate" in str(
        excinfo.value
    )


# --------------------------------------------------------------------------- rejections


def test_next_pointing_at_a_missing_step(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["next"] = "review"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "Step 'plan': next 'review' is not a step." in str(excinfo.value)


def test_duplicate_step_name(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][2]["name"] = "plan"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "Duplicate step name 'plan'." in str(excinfo.value)


def test_unreachable_terminal(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][3]["next"] = "plan"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "Terminal step 'done' is unreachable" in str(excinfo.value)


def test_rerun_step_must_exist(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][1]["gate"]["onReject"]["rerunStep"] = "replan"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "Step 'spec_review_gate': gate.onReject.rerunStep 'replan' is not a step." in str(
        excinfo.value
    )


def _review_in_precondition(raw: dict[str, Any]) -> None:
    raw["steps"][0]["preconditions"] = {"all": ["ticket_has_assignee()", "request_approval()"]}


def _review_in_check_step(raw: dict[str, Any]) -> None:
    raw["steps"][0]["next"] = "wait"
    raw["steps"].insert(
        1,
        {
            "name": "wait",
            "kind": "check",
            "next": "spec_review_gate",
            "conditions": "request_approval()",
        },
    )


def _review_in_entry(raw: dict[str, Any]) -> None:
    raw["entry"]["when"] = {"all": ['ticket_label("factorial:new")', "request_approval()"]}


def _review_negated(raw: dict[str, Any]) -> None:
    raw["steps"][0]["postconditions"]["all"].append({"not": "request_approval()"})


@pytest.mark.parametrize(
    ("mutate", "where"),
    [
        (_review_in_precondition, "Step 'plan'"),
        (_review_in_check_step, "Step 'wait'"),
        (_review_in_entry, "entry.when"),
        (_review_negated, "Step 'plan'"),
    ],
)
def test_request_approval_only_loads_in_an_agent_steps_postconditions(
    sample_raw: dict[str, Any], mutate: Any, where: str
) -> None:
    """Only acceptance feeds the predicate its facts, so anywhere else it would wait for ever,
    and under `not` an approval would fail the step it approved."""
    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    expected = f"{where}: request_approval() may appear only in an agent step's postconditions"
    assert expected in str(excinfo.value)


# ------------------------------------------------------------------------- previews


def test_preview_is_absent_unless_declared(sample_process: ProcessConfig) -> None:
    """The sample's plan step declares its files; every other step's preview is left commented."""
    for step in sample_process.steps:
        preview = getattr(step, "preview", None)
        if step.name == "plan":
            assert preview is not None and len(preview.files) == 4
        else:
            assert preview is None, step.name


def test_preview_is_step_based_and_survives_the_snapshot_round_trip(
    sample_raw: dict[str, Any],
) -> None:
    """Nothing about a preview is tied to a step called `build`: `test` opts in viewer-only and
    `plan` with its own gate, and the pinned snapshot keeps both."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"enabled": True, "reviewGate": "spec_review_gate"}
        raw["steps"][3]["preview"] = {"enabled": True}

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    snapshot = config.model_dump(mode="json", by_alias=True)

    unserved = {
        "setup": [],
        "services": [],
        "env": {},
        "stopAfter": "30m",
        "page": None,
        "files": [],
    }
    assert snapshot["steps"][0]["preview"] == {
        "enabled": True,
        "reviewGate": "spec_review_gate",
        **unserved,
    }
    assert snapshot["steps"][3]["preview"] == {"enabled": True, "reviewGate": None, **unserved}
    assert ProcessConfig.model_validate(snapshot) == config


@pytest.mark.parametrize(
    ("step", "gate", "expected"),
    [
        (2, "test", "preview.reviewGate 'test' is not a human_gate step"),
        (3, "code_review_gate", "must send rework back to 'test'. Its gate.onReject.rerunStep is "),
        (2, "spec_review_gate", "preview.reviewGate 'spec_review_gate' is not reached from"),
    ],
)
def test_review_gate_must_rerun_the_previewed_step_after_it(
    sample_raw: dict[str, Any], step: int, gate: str, expected: str
) -> None:
    """The change request rejects the gate and the rework has to land on the step whose preview
    was reviewed. The third case makes spec_review_gate rerun `build`, so only the `next` walk
    can refuse it: the gate comes before `build`, and would never see its output."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][step]["preview"] = {"reviewGate": gate}
        raw["steps"][1]["gate"]["onReject"]["rerunStep"] = "build" if step == 2 else "plan"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert expected in str(excinfo.value)


_SERVED: dict[str, Any] = {
    "setup": ["npm ci"],
    "services": [
        {"name": "web", "command": "npm run dev -- --port 3000", "port": 3000},
        {
            "name": "api",
            "command": "uv run uvicorn app:app --port 8000",
            "port": 8000,
            "ready": {"path": "/health", "timeout": "60s"},
        },
    ],
    "env": {"ADMIN_PASSWORD": "{secrets.PREVIEW_ADMIN_PASSWORD}"},
    "stopAfter": "20m",
}


def test_a_runtime_served_preview_parses_and_survives_the_snapshot(
    sample_raw: dict[str, Any],
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][2]["preview"] = {"reviewGate": "code_review_gate", **_SERVED}

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    build = config.step("build")
    assert isinstance(build, AgentStep)
    preview = build.preview
    assert preview is not None and preview.served_by_runtime
    assert [s.name for s in preview.services] == ["web", "api"]
    assert preview.services[0].ready.path == "/"
    assert preview.services[1].ready.timeout == timedelta(seconds=60)
    assert preview.stop_after == timedelta(minutes=20)
    snapshot = config.model_dump(mode="json", by_alias=True)
    assert ProcessConfig.model_validate(snapshot) == config


_WEB = {"name": "web", "command": "a", "port": 3000}


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        (
            {"services": [_WEB, {"name": "web", "command": "b", "port": 3001}]},
            "Preview service names repeat.",
        ),
        (
            {"services": [_WEB, {"name": "api", "command": "b", "port": 3000}]},
            "Preview service ports repeat.",
        ),
        ({"services": [{**_WEB, "name": "Web"}]}, "String should match pattern"),
        ({"services": [{**_WEB, "port": 80}]}, "greater than or equal to 1024"),
        (
            {"services": [{**_WEB, "port": 49983}]},
            "Port 49983 is reserved for the sandbox's control plane.",
        ),
        ({"setup": ["npm ci"]}, "need services"),
        ({"env": {"A": "b"}}, "need services"),
        ({"stopAfter": "10m"}, "need services"),
        ({"services": [_WEB], "env": {"PORT": "1"}}, "is reserved"),
        ({"services": [_WEB], "env": {"lower": "1"}}, "is not an environment variable name"),
        ({"services": [_WEB], "stopAfter": "2h"}, "from 1m to 55m"),
        (
            {"services": [_WEB], "env": {"TOKEN": "Bearer {secrets.MCP_DOCS_TOKEN}"}},
            "may reference only PREVIEW_ secrets",
        ),
        ({"services": [{**_WEB, "ready": {"path": "health"}}]}, "must start with /"),
        ({"services": [{**_WEB, "ready": {"timeout": "2h"}}]}, "from 1s to 30m"),
    ],
)
def test_a_runtime_served_preview_refuses_what_it_cannot_serve(
    sample_raw: dict[str, Any], patch: dict[str, Any], expected: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][2]["preview"] = patch

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert expected in str(excinfo.value)


def test_only_an_agent_step_can_serve_its_own_preview() -> None:
    raw = _process_with_action_step()
    raw["steps"][0]["preview"] = {"services": [_WEB]}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(raw)
    assert "preview.services needs an agent step" in str(excinfo.value)


def test_an_action_step_still_takes_a_producer_published_preview() -> None:
    raw = _process_with_action_step()
    raw["steps"][0]["preview"] = {"enabled": True}

    step = ProcessConfig.model_validate(raw).step("fix")
    assert isinstance(step, ActionStep)
    assert step.preview is not None and not step.preview.served_by_runtime


_PAGE: dict[str, str] = {"branch": "pages/{ticket}-{slug}", "path": "summary.html"}


def test_a_page_parses_and_survives_the_snapshot(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"reviewGate": "spec_review_gate", "page": _PAGE}

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    plan = config.step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None
    assert plan.preview.page == PreviewPage(branch="pages/{ticket}-{slug}", path="summary.html")
    assert not plan.preview.served_by_runtime
    snapshot = config.model_dump(mode="json", by_alias=True)
    assert snapshot["steps"][0]["preview"]["page"] == _PAGE
    assert ProcessConfig.model_validate(snapshot) == config


def test_a_page_never_rides_on_the_steps_own_branch(sample_raw: dict[str, Any]) -> None:
    """On the step's own branch the page would merge with the spec and stay in the repository
    later agents read, describing a design that may never be built."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {
            "page": {"branch": "spec/{ticket}-{slug}", "path": "specs/{ticket}-{slug}/summary.html"}
        }

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert (
        "Step 'plan': preview.page.branch 'spec/{ticket}-{slug}' is the step's own "
        "workspace.branch. A page lives on a branch of its own and never merges."
    ) in str(excinfo.value)


def test_a_snapshot_written_before_pages_still_parses(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {
            "enabled": True,
            "reviewGate": "spec_review_gate",
            "setup": [],
            "services": [],
            "env": {},
            "stopAfter": "30m",
        }

    plan = ProcessConfig.model_validate(_mutated(sample_raw, mutate)).step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None
    assert plan.preview.page is None


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ({"branch": "pages/summary", "path": "summary.html"}, "must contain {ticket}"),
        (
            {"branch": "pages/{ticket}-{repo}", "path": "summary.html"},
            "only {ticket} and {slug}, not {repo}",
        ),
        ({"branch": "pages/{ticket}", "path": "/summary.html"}, "inside the repository"),
        ({"branch": "pages/{ticket}", "path": "docs/../summary.html"}, "inside the repository"),
        ({"branch": "pages/{ticket}", "path": "docs//summary.html"}, "inside the repository"),
        ({"branch": "pages/{ticket}", "path": "summary page.html"}, "letters, digits"),
        ({"branch": "pages/{ticket}'x", "path": "summary.html"}, "letters, digits"),
        ({"branch": "pages/{ticket}-$HOME", "path": "summary.html"}, "letters, digits"),
        ({"branch": "pages/{ticket}-{1..3}", "path": "summary.html"}, "letters, digits"),
        ({"branch": "pages/{ticket}-{", "path": "summary.html"}, "letters, digits"),
        ({"branch": "-{ticket}", "path": "summary.html"}, "starting with '-'"),
        ({"branch": "pages/{ticket}", "path": "-x.html"}, "starting with '-'"),
        ({"branch": "pages/{ticket}/", "path": "summary.html"}, "must not end in '/'"),
        ({"branch": "pages/{ticket}-{slug{ticket}}", "path": "summary.html"}, "letters, digits"),
        ({"branch": "pages/{ticket}", "path": "{slug{ticket}}.html"}, "letters, digits"),
    ],
)
def test_a_page_refuses_what_it_cannot_publish_safely(
    sample_raw: dict[str, Any], page: dict[str, str], expected: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"page": page}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert expected in str(excinfo.value)


@pytest.mark.parametrize(
    "path", ["summary.md", "docs/SUMMARY.MD", "notes.txt", "summary", "specs/{ticket}-{slug}.md"]
)
def test_a_page_may_be_any_file(sample_raw: dict[str, Any], path: str) -> None:
    """The API decides how each extension is served (api/page_formats.py); the parser only keeps
    the path safe to publish."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"page": {"branch": "pages/{ticket}-{slug}", "path": path}}

    plan = ProcessConfig.model_validate(_mutated(sample_raw, mutate)).step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None and plan.preview.page
    assert plan.preview.page.path == path


@pytest.mark.parametrize(
    ("step", "branch", "other", "other_branch"),
    [
        (0, "spec/{ticket}-{slug}-page", "plan", "spec/{ticket}-{slug}"),
        (0, "spec/{ticket}-foo", "plan", "spec/{ticket}-{slug}"),
        (0, "impl/{ticket}-{slug}-x", "build", "impl/{ticket}-{slug}"),
        (0, "impl/{ticket}-{slug}", "build", "impl/{ticket}-{slug}"),
        (2, "spec/{ticket}-{slug}-page", "plan", "spec/{ticket}-{slug}"),
    ],
)
def test_a_page_branch_is_named_apart_from_every_step_branch(
    sample_raw: dict[str, Any], step: int, branch: str, other: str, other_branch: str
) -> None:
    """Both globs read each placeholder as `*`: a name that extends or specialises a step branch
    would show up in that step's delivery probe and make its `{slug}` ambiguous."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][step]["preview"] = {"page": {"branch": branch, "path": "summary.html"}}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    name = sample_raw["steps"][step]["name"]
    assert (
        f"Step {name!r}: preview.page.branch {branch!r} overlaps step {other!r}'s "
        f"workspace.branch {other_branch!r}. A page branch must be named apart from every "
        "step branch."
    ) in str(excinfo.value)


@pytest.mark.parametrize("branch", ["pages/{ticket}-{slug}", "pages/{ticket}", "page-{ticket}"])
def test_a_page_branch_apart_from_every_step_branch_is_accepted(
    sample_raw: dict[str, Any], branch: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"page": {"branch": branch, "path": "summary.html"}}

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    plan = config.step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None and plan.preview.page
    assert plan.preview.page.branch == branch


def test_a_page_and_services_cannot_share_a_preview(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][2]["preview"] = {"page": _PAGE, "services": [_WEB]}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "preview.page and preview.services cannot be combined" in str(excinfo.value)


def test_a_page_never_goes_to_the_branch_work_merges_into(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][2]["workspace"]["base"] = "spec/{ticket}-{slug}"
        raw["steps"][2]["preview"] = {
            "page": {"branch": "spec/{ticket}-{slug}", "path": "summary.html"}
        }

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "preview.page.branch must not be workspace.base" in str(excinfo.value)


def test_only_an_agent_step_pushes_a_page() -> None:
    raw = _process_with_action_step()
    raw["steps"][0]["preview"] = {"page": _PAGE}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(raw)
    assert "preview.page needs an agent step" in str(excinfo.value)


_FILES: list[dict[str, str]] = [
    {"path": "specs/{ticket}-{slug}/spec.md", "label": "Spec"},
    {"path": "specs/{ticket}-{slug}/plan.md"},
]


def test_declared_files_parse_and_survive_the_snapshot(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"page": _PAGE, "files": _FILES}

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    plan = config.step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None
    assert plan.preview.files == (
        PreviewFile(path="specs/{ticket}-{slug}/spec.md", label="Spec"),
        PreviewFile(path="specs/{ticket}-{slug}/plan.md", label=None),
    )
    snapshot = config.model_dump(mode="json", by_alias=True)
    assert ProcessConfig.model_validate(snapshot) == config


def test_a_snapshot_written_before_files_still_parses(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"page": _PAGE}

    plan = ProcessConfig.model_validate(_mutated(sample_raw, mutate)).step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None
    assert plan.preview.files == ()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("specs/{repo}/spec.md", "only {ticket} and {slug}, not {repo}"),
        ("/spec.md", "inside the repository"),
        ("specs/../spec.md", "inside the repository"),
        ("specs//spec.md", "inside the repository"),
        ("specs/?/spec.md", "letters, digits"),
        ("specs/a**/spec.md", '"**" must be a whole segment'),
        ("./specs/spec.md", 'must not have a "." segment'),
        ("specs/./*.md", 'must not have a "." segment'),
        ("specs/-x/spec.md", "starting with '-'"),
    ],
)
def test_a_declared_file_refuses_an_unsafe_path(
    sample_raw: dict[str, Any], path: str, expected: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"files": [{"path": path}]}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "preview.files.path" in str(excinfo.value)
    assert expected in str(excinfo.value)


def test_a_declared_pattern_parses_with_its_label(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {
            "files": [
                {"path": "reports/{ticket}/*.html", "label": "Reports"},
                {"path": "docs/{ticket}/**/*.md", "label": "Docs"},
                {"path": "more/{ticket}/*.md", "label": "Reports"},
            ]
        }

    plan = ProcessConfig.model_validate(_mutated(sample_raw, mutate)).step("plan")
    assert isinstance(plan, AgentStep) and plan.preview is not None
    assert [declared.is_pattern for declared in plan.preview.files] == [True, True, True]
    assert not PreviewFile(path="specs/{ticket}-{slug}/spec.md").is_pattern


def test_a_declared_pattern_needs_a_label(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"files": [{"path": "reports/{ticket}/*.html"}]}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "'reports/{ticket}/*.html' is a pattern, so it needs a label" in str(excinfo.value)


def test_a_retired_page_still_refuses_a_star() -> None:
    with pytest.raises(ValidationError, match="letters, digits"):
        PreviewPage(branch="pages/{ticket}", path="*.html")


def test_declared_files_may_not_repeat(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["preview"] = {"files": [_FILES[0], {"path": _FILES[0]["path"]}]}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "preview.files paths repeat" in str(excinfo.value)


def test_declared_files_need_an_agent_step() -> None:
    raw = _process_with_action_step()
    raw["steps"][0]["preview"] = {"files": _FILES}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(raw)
    assert (
        f"Step {raw['steps'][0]['name']!r}: preview.files needs an agent step, because only "
        "an agent run's branch is read."
    ) in str(excinfo.value)


def test_declared_files_need_a_ticket_in_the_step_branch(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["workspace"]["branch"] = "spec/{slug}"
        raw["steps"][0]["preview"] = {"files": _FILES}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert (
        "Step 'plan': preview.files needs {ticket} in workspace.branch. Without it, the branch "
        "could be another ticket's."
    ) in str(excinfo.value)


def test_typoed_key_is_not_ignored(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["onSucess"] = {"ticketLabel": "oops"}

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    message = str(excinfo.value)
    assert "onSucess" in message
    assert "Extra inputs are not permitted" in message


def test_bare_input_must_be_in_the_closed_set(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["inputs"] = ["ticket_history"]

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "ticket_body" in str(excinfo.value)


def test_bad_duration(sample_raw: dict[str, Any]) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["defaults"]["timeout"] = "1 hour"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "Invalid duration '1 hour'" in str(excinfo.value)


def test_workspace_repo_rejects_a_malformed_literal(sample_raw: dict[str, Any]) -> None:
    """`repo: widgets` (no `owner/name` slash) parsed fine before this
    validator and only failed deep inside a spawn as a bare `split_repo` `ValueError` the
    permanent-failure classification does not name — an invisible spin. Caught at PARSE time now,
    so a config shaped like this can never be admitted at all."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["workspace"]["repo"] = "widgets"

    with pytest.raises(ValidationError) as excinfo:
        ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    assert "workspace.repo 'widgets' must be 'owner/name'" in str(excinfo.value)


def test_workspace_repo_tolerates_the_unexpanded_placeholder(
    sample_process: ProcessConfig,
) -> None:
    """`{repo}` (the sample's own canonical form) carries no `/` either, but it is a template —
    `sample_process` parsing at all (the module-scoped fixture every other sample test already
    depends on) is the proof the validator does not reject it."""
    plan = sample_process.step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.workspace.repo == "{repo}"


def test_workspace_repo_accepts_a_literal_owner_slash_name(sample_raw: dict[str, Any]) -> None:
    """A literal, already-resolved `owner/name` (never templated) is legitimate too — the
    validator checks SHAPE, not "must be a template"."""

    def mutate(raw: dict[str, Any]) -> None:
        raw["steps"][0]["workspace"]["repo"] = "acme/widgets"

    config = ProcessConfig.model_validate(_mutated(sample_raw, mutate))
    plan = config.step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.workspace.repo == "acme/widgets"


def test_size_alias_model_id_is_rejected() -> None:
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw["model"] = "sonnet"
    with pytest.raises(ValidationError) as excinfo:
        AgentManifest.model_validate(raw)
    message = str(excinfo.value)
    assert "size alias" in message
    assert "claude-haiku-4-5-20251001" in message  # the error shows what a real id looks like


@pytest.mark.parametrize("field", ["skills", "mcp"])
def test_a_name_cannot_be_declared_in_both_scopes(field: str) -> None:
    """A model rule, so the builder's disk loader and sync from GitHub both refuse it."""
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw[field] = {"local": ["dup"], "shared": ["dup"]}
    with pytest.raises(ValidationError, match="unique across scopes"):
        AgentManifest.model_validate(raw)


def test_manifest_permissions_never_declares_workflows() -> None:
    """Banned in the pydantic model itself, independent of any project's ceiling (design.md
    §7.2) — `extra="forbid"` on `Permissions` (no `workflows` field exists to hold it) is what
    makes this a parse-time rejection rather than something a ceiling has to catch later."""
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw["permissions"] = {"workflows": "write"}
    with pytest.raises(ValidationError) as excinfo:
        AgentManifest.model_validate(raw)
    assert "workflows" in str(excinfo.value)


def test_manifest_permissions_rejects_an_unknown_scope() -> None:
    raw = yaml.safe_load((SAMPLE / "agents" / "build-agent" / "agent.yaml").read_text())
    raw["permissions"] = {"deployments": "write"}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(raw)


def test_config_error_names_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "process.yaml"
    bad.write_text("specVersion: adlc/v1\nkind: Process\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_process(bad)
    assert str(bad) in str(excinfo.value)
    assert excinfo.value.path == bad


def test_an_alias_bomb_is_refused_before_it_expands() -> None:
    """Nine levels of nine aliases is under 1 KB of text and 387 million strings expanded: the
    API parses unsaved text for any member, so expanding it would stop every tenant's loop."""
    lines = ['a0: &a0 ["x"]']
    for level in range(1, 10):
        lines.append(f"a{level}: &a{level} [" + ", ".join([f"*a{level - 1}"] * 9) + "]")
    with pytest.raises(ConfigError, match="anchors and aliases are not supported"):
        parse_process("\n".join(lines), origin="bomb")


# ----------------------------------------------------------- the processes/ folder (§4)


def _lane(root: Path, key: str, *, name: str | None = None) -> Path:
    """One `processes/process-<key>.yaml`, copied from the sample under a chosen `name:`."""
    folder = root / "processes"
    folder.mkdir(parents=True, exist_ok=True)
    text = SAMPLE_PROCESS.read_text(encoding="utf-8").replace(
        "name: simple", f"name: {name if name is not None else key}"
    )
    path = folder / f"process-{key}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_stem_minus_the_prefix_is_the_lane_key(tmp_path: Path) -> None:
    root = tmp_path / ".factorial"
    _lane(root, "hotfix")
    _lane(root, "docs-only")
    assert sorted(load_processes(root)) == ["docs-only", "hotfix"]


def test_a_stem_that_does_not_match_the_name_is_refused(tmp_path: Path) -> None:
    """The key is what a `factorial:process:<key>` label selects, so a mismatch would offer a
    lane under a name nothing in the repo answers to."""
    root = tmp_path / ".factorial"
    _lane(root, "hotfix", name="urgent")
    with pytest.raises(ConfigError, match=r"does not match its file process\-hotfix\.yaml"):
        load_processes(root)


def test_a_lane_named_default_is_refused(tmp_path: Path) -> None:
    """The default is a property of the project, not of a file: a lane called `default` would
    put a second, contradictory answer in the repo."""
    root = tmp_path / ".factorial"
    _lane(root, "default")
    with pytest.raises(ConfigError, match="reserved"):
        load_processes(root)


def test_a_lane_key_that_is_not_a_slug_is_refused(tmp_path: Path) -> None:
    """The key becomes a path segment and a label, so it is a slug — checked at the loader
    boundary and never on `ProcessConfig`, which re-parses pinned snapshots every tick."""
    root = tmp_path / ".factorial"
    _lane(root, "Hot Fix")
    with pytest.raises(ConfigError, match="is not a slug"):
        load_processes(root)


def test_a_file_that_is_not_a_process_file_is_refused_not_skipped(tmp_path: Path) -> None:
    root = tmp_path / ".factorial"
    _lane(root, "hotfix")
    (root / "processes" / "hotfix.yaml").write_text("x: 1", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"'hotfix\.yaml' is not one"):
        load_processes(root)


def test_a_repo_carrying_both_layouts_is_refused(tmp_path: Path) -> None:
    """Silently preferring one would leave an operator editing a file nothing reads."""
    root = tmp_path / ".factorial"
    (root / "agents").mkdir(parents=True)
    _lane(root, "hotfix")
    (root / "process.yaml").write_text(SAMPLE_PROCESS.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="both layouts"):
        load_config_folder(root)


def test_the_legacy_root_file_keeps_whatever_name_it_carries(tmp_path: Path) -> None:
    """Identity rules guard a NEW lane; a repo that predates the folder is not asked to rename
    its process, and there is nothing to migrate a pinned snapshot to."""
    root = tmp_path / ".factorial"
    (root / "agents").mkdir(parents=True)
    (root / "process.yaml").write_text(
        SAMPLE_PROCESS.read_text(encoding="utf-8").replace("name: simple", "name: Legacy Lane"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Step\\(s\\) reference agents"):
        load_config_folder(root)  # the name was accepted; only the agents are missing


def test_missing_agent_manifest_is_reported(tmp_path: Path) -> None:
    root = tmp_path / ".adlc"
    (root / "agents").mkdir(parents=True)
    (root / "process.yaml").write_text(SAMPLE_PROCESS.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config_folder(root)
    assert "plan (agent plan-agent)" in str(excinfo.value)


def test_folder_name_must_match_manifest_name(tmp_path: Path) -> None:
    root = tmp_path / ".adlc"
    folder = root / "agents" / "planner"
    folder.mkdir(parents=True)
    (root / "process.yaml").write_text(SAMPLE_PROCESS.read_text(encoding="utf-8"), encoding="utf-8")
    source = SAMPLE / "agents" / "plan-agent"
    (folder / "agent.yaml").write_text(
        (source / "agent.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for name in ("system-prompt.md", "instructions.md"):
        (folder / name).write_text("x", encoding="utf-8")
    for skill in ("speckit-specify", "speckit-clarify", "speckit-tasks"):
        (folder / "skills" / skill).mkdir(parents=True)
        (folder / "skills" / skill / "SKILL.md").write_text("x", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config_folder(root)
    assert "does not match its folder 'planner'" in str(excinfo.value)


def test_declared_prompt_paths_must_exist(tmp_path: Path) -> None:
    folder = tmp_path / "plan-agent"
    folder.mkdir()
    (folder / "agent.yaml").write_text(
        (SAMPLE / "agents" / "plan-agent" / "agent.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_agent(folder / "agent.yaml")
    assert "prompts.system points at missing file 'system-prompt.md'" in str(excinfo.value)


# ------------------------------------------------------------------- template expansion


def test_expand_leaves_unknown_placeholders_alone() -> None:
    branch = expand("spec/{ticket}-{slug}", {"ticket": "42"})
    assert branch == "spec/42-{slug}"  # {slug} is chosen by the agent; predicates glob it
    # Not str.format: JSON braces survive untouched.
    assert expand('{"k": 1} for {ticket}', {"ticket": "42"}) == '{"k": 1} for 42'


def test_expand_strict_raises_on_unknown_placeholder() -> None:
    with pytest.raises(TemplateError) as excinfo:
        expand_strict("spec/{ticket}-{slug}", {"ticket": "42"})
    assert "slug" in str(excinfo.value)


def test_expand_failure_comment_placeholders(sample_process: ProcessConfig) -> None:
    assert sample_process.defaults.on_failure is not None
    comment = sample_process.defaults.on_failure.escalate.comment
    assert comment is not None
    rendered = expand_strict(
        comment, {"step": "plan", "attempt": "2", "failure_summary": "Ran past its 1-hour limit"}
    )
    assert "Factorial stopped at plan after 2 attempt(s): Ran past its 1-hour limit." in rendered


def test_expand_principals_resolves_roles_through_templates(
    sample_process: ProcessConfig,
) -> None:
    plan = sample_process.step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.on_success is not None and plan.on_success.assign is not None
    assignees = expand_principals(
        plan.on_success.assign, sample_process.roles, {"ticket_owner": "operator"}
    )
    assert assignees == ("operator",)


def test_expand_principals_dedupes_and_preserves_order() -> None:
    resolved = expand_principals(
        ["teammate", "{roles.reviewers}", "teammate"],
        {"reviewers": ("{ticket_owner}", "teammate-2"), "unused": ("teammate-3",)},
        {"ticket_owner": "teammate"},
    )
    assert resolved == ("teammate", "teammate-2")


def test_expand_principals_rejects_role_cycles() -> None:
    with pytest.raises(TemplateError) as excinfo:
        expand_principals(["{roles.a}"], {"a": ("{roles.b}",), "b": ("{roles.a}",)}, {})
    assert "Role reference cycle: a -> b -> a." in str(excinfo.value)


def test_expand_principals_rejects_unknown_role() -> None:
    with pytest.raises(TemplateError) as excinfo:
        expand_principals(["{roles.nobody}"], {"reviewers": ("x",)}, {})
    assert "Unknown role 'nobody'." in str(excinfo.value)


# ------------------------------------------------------- parsing from text (repo-backed config)
#
# Config really lives in the target repo at a pinned commit sha, so the parsers must work on text
# with no filesystem behind it. `origin` is `owner/name@<sha7>:<path>` there.

REPO_ORIGIN = "acme/widgets@a1b2c3d:.factorial/process.yaml"


def test_parse_process_matches_the_disk_loader(sample_process: ProcessConfig) -> None:
    text = SAMPLE_PROCESS.read_text(encoding="utf-8")
    assert parse_process(text, origin=REPO_ORIGIN) == sample_process


def test_parse_agent_matches_the_disk_loader() -> None:
    path = SAMPLE / "agents" / "plan-agent" / "agent.yaml"
    parsed = parse_agent(
        path.read_text(encoding="utf-8"),
        origin="acme/widgets@a1b2c3d:.factorial/agents/plan-agent/agent.yaml",
    )
    assert parsed == load_agent(path)


def test_parse_error_names_the_repo_origin() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_process("specVersion: adlc/v1\nkind: Process\n", origin=REPO_ORIGIN)
    assert str(excinfo.value).startswith(f"{REPO_ORIGIN}: ")
    assert excinfo.value.origin == REPO_ORIGIN
    assert excinfo.value.path == REPO_ORIGIN


def test_parse_reports_malformed_yaml_against_the_origin() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_process("steps: [\n", origin=REPO_ORIGIN)
    assert REPO_ORIGIN in str(excinfo.value)
    assert "not valid YAML" in str(excinfo.value)


def test_a_yaml_error_gives_line_and_column_without_the_stream_name() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_agent("name: [\n", origin="x")
    assert caught.value.detail.startswith("The file is not valid YAML at line 2, column 1: ")
    assert "<unicode string>" not in caught.value.detail


def test_a_file_of_comments_says_so_instead_of_naming_a_model() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_agent("# to be filled in\n", origin="x")
    assert caught.value.detail == "The file is empty or holds only comments."


def test_parse_agent_does_not_require_prompt_files_to_exist() -> None:
    """There is no filesystem behind a repo-backed manifest; the resolver proves the prompts
    exist by fetching them at the pinned sha, which is a stronger check than a path test."""
    text = (SAMPLE / "agents" / "plan-agent" / "agent.yaml").read_text(encoding="utf-8")
    manifest = parse_agent(
        text, origin="acme/widgets@a1b2c3d:.factorial/agents/plan-agent/agent.yaml"
    )
    assert manifest.prompts.system == "system-prompt.md"
    assert manifest.skills.local  # declared, unverified — deliberately


def test_config_error_from_disk_still_carries_a_path(tmp_path: Path) -> None:
    """The widened origin must not downgrade what existing disk callers get back."""
    bad = tmp_path / "agent.yaml"
    bad.write_text("specVersion: adlc/v1\nkind: Agent\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_agent(bad)
    assert isinstance(excinfo.value.path, Path)
    assert excinfo.value.origin == str(bad)


# --------------------------------------------------------------- gate.approverPermission


def _gate_config(**gate: Any) -> dict[str, Any]:
    cfg = _process("p").model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["gate"].update(gate)
    return cfg


def _spec_gate(config: ProcessConfig) -> GateStep:
    step = config.steps[1]
    assert isinstance(step, GateStep)
    return step


@pytest.mark.parametrize("level", ["write", "maintain", "admin"])
def test_a_gate_may_name_a_permission_that_decides_it(level: str) -> None:
    config = ProcessConfig.model_validate(_gate_config(approverPermission=level))
    assert _spec_gate(config).gate.approver_permission == level


def test_the_permission_is_optional_and_unset_by_default() -> None:
    raw = _gate_config()
    raw["steps"][1]["gate"].pop("approverPermission", None)
    assert _spec_gate(ProcessConfig.model_validate(raw)).gate.approver_permission is None


def test_a_snapshot_pinned_before_the_key_reparses_unchanged() -> None:
    raw = _gate_config()
    raw["steps"][1]["gate"].pop("approverPermission", None)
    config = ProcessConfig.model_validate(raw)
    assert ProcessConfig.model_validate(config.model_dump(mode="json", by_alias=True)) == config


@pytest.mark.parametrize("level", ["read", "triage", "push", "none", "Write"])
def test_an_unknown_permission_level_is_refused(level: str) -> None:
    with pytest.raises(ValidationError, match="approverPermission"):
        ProcessConfig.model_validate(_gate_config(approverPermission=level))


def _gate_rerunning(kind: str | None) -> ProcessConfig:
    """`_process("proc")` whose `spec_gate` reruns a step of `kind` (a check step inserted
    between `plan` and the gate), or has no `onReject` at all when `kind` is None."""
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    gate = raw["steps"][1]["gate"]
    if kind is None:
        del gate["onReject"]
    elif kind == "check":
        raw["steps"][0]["next"] = "wait_ci"
        raw["steps"].insert(
            1,
            {
                "name": "wait_ci",
                "kind": "check",
                "next": "spec_gate",
                "conditions": 'ci_green("spec/{ticket}-*")',
            },
        )
        gate["onReject"]["rerunStep"] = "wait_ci"
    return ProcessConfig.model_validate(raw)


def test_a_gate_reruns_the_agent_step_its_onreject_names() -> None:
    step = _process("proc").rerun_agent_step("spec_gate")

    assert step is not None and step.name == "plan"


def test_nothing_reruns_from_a_step_that_is_not_a_gate() -> None:
    config = _process("proc")

    assert config.rerun_agent_step("plan") is None
    assert config.rerun_agent_step(None) is None
    assert config.rerun_agent_step("no_such_step") is None


def test_a_gate_without_onreject_reruns_nothing() -> None:
    assert _gate_rerunning(None).rerun_agent_step("spec_gate") is None


def test_a_gate_whose_rerun_step_is_no_agent_reruns_nothing() -> None:
    assert _gate_rerunning("check").rerun_agent_step("spec_gate") is None


def _stacked_raw() -> dict[str, Any]:
    """The stacked lane cut to its four steps: a non-merging gate, `build`, a merging gate with
    `onReject`, and a merging gate with none."""
    raw = yaml.safe_load(STACKED_PROCESS.read_text(encoding="utf-8"))
    raw["steps"] = [step for step in raw["steps"] if step["name"] != "plan"]
    raw["entry"]["step"] = "spec_review_gate"
    by_name = {step["name"]: step for step in raw["steps"]}
    del by_name["spec_review_gate"]["gate"]["onReject"]
    by_name["spec_merge_gate"]["gate"].pop("onReject", None)
    return raw


def _stacked_gate(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in raw["steps"] if step["name"] == name)["gate"]


def test_on_conflict_parses_on_a_merging_gate() -> None:
    raw = _stacked_raw()
    _stacked_gate(raw, "spec_merge_gate")["onConflict"] = {"rerunStep": "build"}
    config = ProcessConfig.model_validate(raw)
    gate = config.step("spec_merge_gate")
    assert isinstance(gate, GateStep)
    assert gate.gate.on_conflict is not None
    assert gate.gate.on_conflict.rerun_step == "build"


def test_on_conflict_is_refused_on_a_gate_that_does_not_merge() -> None:
    raw = _stacked_raw()
    _stacked_gate(raw, "spec_review_gate")["onConflict"] = {"rerunStep": "build"}
    with pytest.raises(ValidationError, match=r"onApprove\.merge"):
        ProcessConfig.model_validate(raw)


def test_on_conflict_must_name_an_agent_step() -> None:
    raw = _stacked_raw()
    _stacked_gate(raw, "spec_merge_gate")["onConflict"] = {"rerunStep": "code_review_gate"}
    with pytest.raises(ValidationError, match="agent step"):
        ProcessConfig.model_validate(raw)


def test_on_conflict_adds_no_edge() -> None:
    raw = _stacked_raw()
    raw["steps"].append(
        {
            "name": "stray",
            "kind": "agent",
            "agent": "build-agent",
            "next": "done",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "x/{ticket}"},
            "preconditions": "ticket_has_assignee()",
        }
    )
    _stacked_gate(raw, "spec_merge_gate")["onConflict"] = {"rerunStep": "stray"}
    with pytest.raises(ValidationError, match="unreachable"):
        ProcessConfig.model_validate(raw)


def test_conflict_fix_step_prefers_on_conflict_then_on_reject_then_the_earlier_gate() -> None:
    raw = _stacked_raw()
    del _stacked_gate(raw, "spec_merge_gate")["onConflict"]
    stacked = ProcessConfig.model_validate(raw)
    fix = stacked.conflict_fix_step("spec_merge_gate")
    assert fix is not None and fix.name == "build"
    fix = stacked.conflict_fix_step("code_review_gate")
    assert fix is not None and fix.name == "build"

    _stacked_gate(raw, "code_review_gate")["onConflict"] = {"rerunStep": "other"}
    raw["steps"].insert(
        0,
        {
            "name": "other",
            "kind": "agent",
            "agent": "build-agent",
            "next": "spec_review_gate",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "o/{ticket}"},
            "preconditions": "ticket_has_assignee()",
        },
    )
    raw["entry"]["step"] = "other"
    _stacked_gate(raw, "spec_review_gate")["onReject"] = {"rerunStep": "other"}
    explicit = ProcessConfig.model_validate(raw)
    fix = explicit.conflict_fix_step("code_review_gate")
    assert fix is not None and fix.name == "other"
    fix = explicit.conflict_fix_step("spec_merge_gate")
    assert fix is not None and fix.name == "build"


def test_conflict_fix_step_is_none_when_no_gate_sends_work_back() -> None:
    raw = _stacked_raw()
    del _stacked_gate(raw, "code_review_gate")["onReject"]
    del _stacked_gate(raw, "spec_merge_gate")["onConflict"]
    config = ProcessConfig.model_validate(raw)
    assert config.conflict_fix_step("spec_merge_gate") is None
    assert config.conflict_fix_step("build") is None
    assert config.conflict_fix_step("no_such_step") is None


def test_a_manifest_that_still_declares_max_duration_parses() -> None:
    text = (SAMPLE / "agents" / "plan-agent" / "agent.yaml").read_text(encoding="utf-8")
    text = text.replace("limits:\n", "limits:\n  max_duration: 1h\n", 1)
    manifest = parse_agent(text, origin="acme/widgets@a1b2c3d:.factorial/agents/plan/agent.yaml")
    assert manifest.limits.max_duration == timedelta(hours=1)
    assert manifest.limits.max_turns == 200
