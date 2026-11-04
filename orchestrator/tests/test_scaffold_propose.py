import re

import pytest

from orchestrator.config import parse_process
from orchestrator.config.process_templates import PROCESS_TEMPLATES
from orchestrator.config.scaffold.assemble import assemble
from orchestrator.config.scaffold.propose import propose
from tests.scaffold_lanes import STACKED, UNSTACKED

#: Any unsubstituted `__X__` marker — the total check every proposed body must clear, regardless
#: of which specific markers a given file happens to name.
_RESIDUAL_MARKER = re.compile(r"__[A-Z_]+__")


def _propose(text, **kw):
    kw.setdefault("present_agents", ())
    kw.setdefault("present_skills", ())
    kw.setdefault("present_mcp", ())
    return propose(parse_process(text, origin="t"), **kw)


def test_every_file_of_a_fitting_variant_is_proposed():
    files = _propose(STACKED).files
    assert ".factorial/agents/plan-agent/stacked/agent.yaml" not in files
    assert ".factorial/agents/plan-agent/agent.yaml" in files
    assert ".factorial/agents/plan-agent/system-prompt.md" in files


def test_markers_are_substituted_in_the_proposed_body():
    body = _propose(UNSTACKED).files[".factorial/agents/plan-agent/instructions.md"].body
    assert "__" not in body
    assert "spec/<N>-<slug>" in body


def test_a_file_records_only_the_facts_it_used():
    """Per-file, so editing instructions.md does not go stale because a marker only
    system-prompt.md used has changed.

    Exact equality on two files whose marker sets genuinely differ — one that uses several,
    one that uses none — so a mutation that broadens `facts=used` to the whole fact set (as a
    reviewer's mutation of `add()` did) cannot slip past a merely-subset assertion the way the
    original version of this test did.
    """
    proposal = _propose(UNSTACKED)
    instructions = proposal.files[".factorial/agents/plan-agent/instructions.md"]
    assert instructions.facts == {
        "__BRANCH_PREFIX__": "spec",
        "__BASE_BRANCH__": "main",
        "__SPECS_DIR__": "specs",
    }
    # A speckit skill under plan-agent/base-target names no marker at all.
    skill = proposal.files[".factorial/agents/plan-agent/skills/speckit-specify/SKILL.md"]
    assert skill.facts == {}


def test_an_agent_already_in_the_repo_is_locked_not_proposed_for_writing():
    proposal = _propose(STACKED, present_agents=("plan-agent",))
    locked = [f for f in proposal.files.values() if f.component == "plan-agent"]
    assert locked and all(f.present_in_repo for f in locked)


def test_a_shared_skill_is_proposed_because_a_manifest_declares_it():
    files = _propose(STACKED).files
    assert ".factorial/skills/project-constitution/SKILL.md" in files


def test_a_shared_skill_already_in_the_repo_is_locked():
    proposal = _propose(STACKED, present_skills=("project-constitution",))
    assert proposal.files[".factorial/skills/project-constitution/SKILL.md"].present_in_repo


SPEC_PAGE = ".factorial/skills/spec-page/SKILL.md"


@pytest.mark.parametrize("lane", [UNSTACKED, STACKED], ids=["unstacked", "stacked"])
def test_the_spec_page_skill_arrives_declared_by_the_plan_agent(lane: str):
    """It asks for a page only when the run's `# Files` section lists a file labelled Summary, so
    a lane whose plan step declares none is asked for nothing."""
    files = _propose(lane).files
    assert SPEC_PAGE in files
    assert "- spec-page" in files[".factorial/agents/plan-agent/agent.yaml"].body


MCP_EXAMPLE = ".factorial/mcp/product-docs.yaml.example"


def test_a_repo_that_owns_the_plan_agent_is_still_offered_what_rides_with_it():
    """A folder half-written by hand gets the rest of the structure, not only a fresh one."""
    proposal = _propose(UNSTACKED, present_agents=("plan-agent",))
    for path in (SPEC_PAGE, MCP_EXAMPLE):
        assert not proposal.files[path].present_in_repo, path


def test_a_lane_that_names_no_plan_agent_is_offered_neither():
    lane = UNSTACKED.replace("agent: plan-agent", "agent: planner")
    files = _propose(lane).files
    assert SPEC_PAGE not in files and MCP_EXAMPLE not in files


def test_what_rides_with_the_plan_agent_is_locked_once_the_repo_has_it():
    proposal = _propose(UNSTACKED, present_skills=("spec-page",), present_mcp=("product-docs",))
    assert proposal.files[SPEC_PAGE].present_in_repo
    assert proposal.files[MCP_EXAMPLE].present_in_repo


def test_agent_local_skills_land_inside_the_agent_folder():
    files = _propose(UNSTACKED).files
    assert ".factorial/agents/plan-agent/skills/speckit-specify/SKILL.md" in files


def test_an_agent_with_no_catalog_entry_gets_no_files_and_no_offer():
    text = UNSTACKED.replace("agent: build-agent", "agent: db-migration-agent")
    proposal = _propose(text)
    assert not any(f.component == "db-migration-agent" for f in proposal.files.values())
    component = next(c for c in proposal.components if c.name == "db-migration-agent")
    assert component.variants == ()


def test_withheld_variant_is_reported_with_its_reason():
    proposal = _propose(UNSTACKED)
    build = next(c for c in proposal.components if c.name == "build-agent")
    stacked = next(v for v in build.variants if v.variant == "stacked")
    assert stacked.offered is False
    assert stacked.reason is not None
    assert "targets the base branch" in stacked.reason


def test_only_the_lane_matching_variant_contributes_a_body_when_both_are_offered():
    """The regression this whole ruling exists for: on the stacked lane, plan-agent's
    `base-target` variant also reads cleanly (its markers are a subset of what a stacked lane
    supplies), so both variants are `offered`. Only `stacked` is `default_on` here, and it must
    be the one whose body actually lands in `files` — `add()` keys on target path alone, so
    letting the non-matching variant contribute too would let it silently win the dict and hand
    the operator a base-target prompt under a config the UI shows as stacked."""
    proposal = _propose(STACKED)
    plan = next(c for c in proposal.components if c.name == "plan-agent")
    stacked = next(v for v in plan.variants if v.variant == "stacked")
    base_target = next(v for v in plan.variants if v.variant == "base-target")

    assert stacked.offered and stacked.default_on
    assert base_target.offered and not base_target.default_on
    assert base_target.paths == ()

    path = ".factorial/agents/plan-agent/agent.yaml"
    assert path in stacked.paths
    assert proposal.files[path].variant == "stacked"


@pytest.mark.parametrize("forge", ["github", "gitlab"])
@pytest.mark.parametrize("fixture", [STACKED, UNSTACKED], ids=["stacked", "unstacked"])
def test_no_body_carries_a_residual_marker(fixture, forge):
    """The total check: a shared component substitutes from `derive_lane_facts` alone, which
    once did not carry `__BASE_BRANCH__` — every one of the five shipped agent variants
    declares `project-constitution`, and its naming table names `__BASE_BRANCH__`, so every
    proposal on every lane shipped it verbatim until that fact came back. `variant_fit`'s
    missing-marker gate protects an agent's own files from this; nothing before this test
    checked a shared component's."""
    proposal = _propose(fixture, forge=forge)
    for path, file in proposal.files.items():
        leftover = _RESIDUAL_MARKER.findall(file.body)
        assert not leftover, f"{path} still contains unsubstituted marker(s): {leftover}"


# A stacked lane with a third step, naming `test-agent` — which ships only `base-target`. Not
# built by extending the shared `STACKED` fixture: several other tests key on that fixture's
# exact two-step shape (prefixes, `_only()` agreement), and this one only needs a third agent
# referenced, not a third step's facts to reconcile against the other two.
STACKED_WITH_TEST_AGENT = """\
specVersion: factorial/v1
kind: Process
name: sample-lane
version: 1
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: plan-agent
    next: build
    workspace:
      repo: "{repo}"
      base: main
      branch: "spec/{ticket}-{slug}"
    inputs:
      - file: "specs/{ticket}-*/spec.md"
    postconditions:
      all:
        - branch_exists("spec/{ticket}-*")
        - pr_open("spec/{ticket}-*", "main")
  - name: build
    kind: agent
    agent: build-agent
    next: test
    workspace:
      repo: "{repo}"
      base: main
      branch: "impl/{ticket}-{slug}"
    postconditions:
      all:
        - branch_exists("impl/{ticket}-*")
        - pr_open("impl/{ticket}-*", "spec/{ticket}-*")
  - name: test
    kind: agent
    agent: test-agent
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: "impl/{ticket}-{slug}"
terminal:
  step: done
"""


def test_test_agents_only_variant_is_withheld_on_a_stacked_lane():
    """`test-agent` ships `base-target` only (`CATALOG.yaml`). On a stacked lane that variant
    still reads cleanly — it names no stacked-only fact — so it is `offered`, but it is not
    `default_on`: the same lane-match rule that withholds an agent's non-matching variant
    applies here too, so it contributes no files rather than shipping a base-target prompt
    under a config the UI shows as stacked. This is the ruling's named consequence, made
    concrete rather than argued by inspection."""
    proposal = _propose(STACKED_WITH_TEST_AGENT)
    component = next(c for c in proposal.components if c.name == "test-agent")
    assert len(component.variants) == 1
    only = component.variants[0]
    assert only.variant == "base-target"
    assert only.offered is True
    assert only.default_on is False
    assert only.paths == ()
    assert not any(f.component == "test-agent" for f in proposal.files.values())


def _template_lanes(key: str) -> dict[str, str]:
    [template] = [t for t in PROCESS_TEMPLATES if t.key == key]
    return {lane.key: lane.text for lane in template.lanes}


_GH_COMMAND = re.compile(r"\bgh ")


@pytest.mark.parametrize("lane", ["primary", "simple"])
def test_gitlab_forge_gets_gitlab_bodies(lane: str):
    files = _propose(_template_lanes("simple-gitlab")[lane], forge="gitlab").files
    instructions = [f for path, f in files.items() if path.endswith("/instructions.md")]
    assert instructions
    for file in instructions:
        assert "glab" in file.body, file.path
        assert not [line for line in file.body.splitlines() if _GH_COMMAND.search(line)], file.path
    assert "merge request" in files[".factorial/skills/project-constitution/SKILL.md"].body


@pytest.mark.parametrize("lane", ["primary", "simple"])
def test_github_forge_never_gets_gitlab_bodies(lane: str):
    proposal = _propose(_template_lanes("simple")[lane])
    assert not [path for path, f in proposal.files.items() if "glab" in f.body]
    variants = [v.variant for c in proposal.components for v in c.variants]
    assert not [v for v in variants if v == "gitlab" or v.endswith("-gitlab")]


def test_gitlab_stacked_lane_gets_stubs_not_github_prompts():
    """GitLab ships base-target prompts only: a stacked GitLab lane must fall to the stub, never
    to the GitHub `stacked` prompts, which open pull requests with `gh`."""
    config = parse_process(STACKED, origin="t")
    proposal = propose(config, forge="gitlab")
    assert not [
        path for path, f in proposal.files.items() if f.component in {"plan-agent", "build-agent"}
    ]
    selected = {path: f.body for path, f in proposal.files.items()}
    bundle = assemble(config, STACKED, selected, proposal)
    assert {
        ".factorial/agents/plan-agent/agent.yaml",
        ".factorial/agents/build-agent/agent.yaml",
    } <= bundle.stub_paths
