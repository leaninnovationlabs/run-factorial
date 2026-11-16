import pytest
import yaml

from orchestrator.config import ConfigError, parse_process
from orchestrator.config.scaffold.assemble import (
    EditConflict,
    FileDelete,
    FileEdit,
    SelectionError,
    assemble,
    process_bundle_path,
)
from orchestrator.config.scaffold.propose import propose
from orchestrator.config.scaffold.validate import validate_bundle
from tests.scaffold_lanes import UNSTACKED


def _setup(text=UNSTACKED, **kw):
    config = parse_process(text, origin="t")
    return config, propose(config, **kw)


def _default_selection(proposal):
    """Every file of every ticked-by-default variant — what the UI opens on."""
    wanted = {
        path
        for component in proposal.components
        for offer in component.variants
        if offer.offered and offer.default_on and not component.present_in_repo
        for path in offer.paths
    }
    return {path: proposal.files[path].body for path in wanted}


def test_the_default_selection_assembles_and_validates():
    config, proposal = _setup()
    bundle = assemble(config, UNSTACKED, _default_selection(proposal), proposal)
    validate_bundle(bundle)


def test_the_process_file_is_written_under_the_folder_layout():
    """One layout is written (decision 37); the path comes from the config's own `name:`."""
    config, proposal = _setup()
    bundle = assemble(config, UNSTACKED, _default_selection(proposal), proposal)
    assert ".factorial/process.yaml" not in bundle.files
    assert bundle.files[".factorial/processes/process-sample-lane.yaml"] == UNSTACKED


def test_a_submitted_process_file_body_is_refused():
    """Otherwise the committed YAML could differ from the config that was parsed, version
    checked, and used to compute the legal path set."""
    config, proposal = _setup()
    path = process_bundle_path(config.name)
    with pytest.raises(SelectionError, match=r"process-sample-lane\.yaml"):
        assemble(config, UNSTACKED, {path: "evil: true"}, proposal)


def test_a_process_name_that_would_walk_out_of_the_config_dir_is_refused():
    """The write site proves its own path segment: the loader's slug rule is stricter and the
    route applies it, but this is where the segment is built."""
    with pytest.raises(SelectionError, match="single file name"):
        process_bundle_path("../../.github/workflows/ci")


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "README.md",
        ".factorial/agents/plan-agent/../../.github/workflows/ci.yml",
        ".factorial/agents/nope/agent.yaml",
    ],
)
def test_a_path_outside_the_proposal_is_refused(path):
    config, proposal = _setup()
    with pytest.raises(SelectionError, match="not proposed"):
        assemble(config, UNSTACKED, {path: "x"}, proposal)


def test_a_file_the_repo_already_has_is_refused():
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(SelectionError, match="already"):
        assemble(
            config,
            UNSTACKED,
            {".factorial/agents/plan-agent/agent.yaml": "x"},
            proposal,
        )


def test_an_edited_body_is_committed_verbatim():
    config, proposal = _setup()
    selection = _default_selection(proposal)
    selection[".factorial/agents/plan-agent/instructions.md"] = "# mine\n"
    bundle = assemble(config, UNSTACKED, selection, proposal)
    assert bundle.files[".factorial/agents/plan-agent/instructions.md"] == "# mine\n"


def test_selecting_nothing_still_stubs_every_referenced_agent():
    """Why not `assert "specVersion" not in ...`: the shipped stub
    (`templates/stub/agent.yaml`) names every required key, `specVersion` included, in a
    comment — by design, so an operator has the shape in front of them.
    A raw substring check can't tell that stub apart from a real manifest that also happens to
    mention `specVersion` in a docstring; `yaml.safe_load(...) is None` is the "comments
    only" proof that actually distinguishes a stub from a template.
    """
    config, proposal = _setup()
    bundle = assemble(config, UNSTACKED, {}, proposal)
    for name in ("plan-agent", "build-agent"):
        path = f".factorial/agents/{name}/agent.yaml"
        assert path in bundle.stub_paths
        assert yaml.safe_load(bundle.files[path]) is None
    validate_bundle(bundle)


def test_a_manifest_without_the_prompt_it_names_fails_validation():
    """The gate that makes per-file choice safe: the loader resolves prompts.system, so
    unticking system-prompt.md while keeping agent.yaml cannot reach a pull request."""
    config, proposal = _setup()
    selection = _default_selection(proposal)
    del selection[".factorial/agents/plan-agent/system-prompt.md"]
    bundle = assemble(config, UNSTACKED, selection, proposal)
    with pytest.raises(ConfigError):
        validate_bundle(bundle)


def test_a_manifest_without_the_shared_skill_it_declares_fails_validation():
    config, proposal = _setup()
    selection = _default_selection(proposal)
    del selection[".factorial/skills/project-constitution/SKILL.md"]
    bundle = assemble(config, UNSTACKED, selection, proposal)
    with pytest.raises(ConfigError):
        validate_bundle(bundle)


def test_a_manifest_without_a_local_skill_it_declares_fails_validation():
    config, proposal = _setup()
    selection = _default_selection(proposal)
    dropped = [p for p in selection if "/skills/speckit-" in p]
    assert dropped, "plan-agent must ship local skills for this test to mean anything"
    for path in dropped:
        del selection[path]
    bundle = assemble(config, UNSTACKED, selection, proposal)
    with pytest.raises(ConfigError):
        validate_bundle(bundle)


# --- presence for an uncatalogued agent, and unsafe agent names -----------------------------------

#: A single agent step naming an agent the catalog has never heard of. Deliberately one step
#: only, so an assertion that `stub_paths` ends up empty means what it says — a second,
#: catalogued step would earn its own stub and muddy the assertion.
UNCATALOGUED = """\
specVersion: factorial/v1
kind: Process
name: single
version: 1
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: migration-planner
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: "spec/{ticket}-{slug}"
terminal:
  step: done
"""


def test_a_present_agent_absent_from_the_catalog_gets_no_stub():
    """The regression this guards: `propose()` emits no `FileProposal` at all for an agent with
    no catalog entry (only a `ComponentProposal`), so `proposal.files` alone under-reports
    presence for exactly the agents the catalog cannot template. Trusting it here would stub a
    comments-only manifest over the operator's real one."""
    config, proposal = _setup(UNCATALOGUED, present_agents=("migration-planner",))
    bundle = assemble(config, UNCATALOGUED, {}, proposal)
    assert ".factorial/agents/migration-planner/agent.yaml" not in bundle.files
    assert not bundle.stub_paths


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../..",
        "a/b",
        "plan-agent/../../.github/workflows",
    ],
)
def test_an_unsafe_agent_name_is_refused(name):
    """`assemble()` builds the stub path from `AgentStep.agent` itself rather than looking it up
    (`config/models.py`'s `agent` field carries no pattern), so this is the one path the
    server-computed-proposal membership guarantee does not reach."""
    text = UNSTACKED.replace("plan-agent", name)
    config = parse_process(text, origin="t")
    proposal = propose(config)
    with pytest.raises(SelectionError, match="folder name"):
        assemble(config, text, {}, proposal)


PROMPT = ".factorial/agents/plan-agent/system-prompt.md"
BLOB = "b" * 40


def test_an_edit_to_a_file_on_the_branch_is_written():
    config, proposal = _setup(present_agents=("plan-agent",))
    bundle = assemble(
        config,
        UNSTACKED,
        {},
        proposal,
        edits=[FileEdit(PROMPT, "# mine\n", BLOB)],
        repo_blobs={PROMPT: BLOB},
    )
    assert bundle.files[PROMPT] == "# mine\n"


def test_an_edit_whose_file_moved_on_the_branch_is_a_conflict():
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(EditConflict, match=r"system-prompt\.md"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            edits=[FileEdit(PROMPT, "x", BLOB)],
            repo_blobs={PROMPT: "c" * 40},
        )


@pytest.mark.parametrize(
    "path", [".factorial/agents/plan-agent/extra.md", ".github/workflows/ci.yml"]
)
def test_an_edit_to_a_path_the_branch_does_not_hold_is_refused(path):
    config, proposal = _setup()
    # `.github/...` is in the blobs on purpose: membership alone must not admit a path outside
    # the config folder.
    with pytest.raises(SelectionError, match="not a file on the config branch"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            edits=[FileEdit(path, "x", BLOB)],
            repo_blobs={".github/workflows/ci.yml": BLOB},
        )


def test_the_lanes_own_process_file_is_refused_as_an_edit():
    config, proposal = _setup()
    path = process_bundle_path(config.name)
    with pytest.raises(SelectionError, match="submitted config text"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            edits=[FileEdit(path, "x", BLOB)],
            repo_blobs={path: BLOB},
        )


def test_another_lanes_process_file_can_be_edited():
    config, proposal = _setup()
    other = ".factorial/processes/process-other.yaml"
    bundle = assemble(
        config,
        UNSTACKED,
        {},
        proposal,
        edits=[FileEdit(other, "name: other\n", BLOB)],
        repo_blobs={other: BLOB},
    )
    assert bundle.files[other] == "name: other\n"


def test_a_path_sent_as_both_a_pick_and_an_edit_is_refused():
    config, proposal = _setup()
    path = next(p for p, f in proposal.files.items() if not f.present_in_repo)
    with pytest.raises(SelectionError, match="twice"):
        assemble(
            config,
            UNSTACKED,
            {path: "x"},
            proposal,
            edits=[FileEdit(path, "y", BLOB)],
            repo_blobs={path: BLOB},
        )


def test_a_created_file_under_the_config_folder_is_written():
    config, proposal = _setup(present_agents=("plan-agent",))
    path = ".factorial/notes/runbook.md"
    bundle = assemble(
        config, UNSTACKED, {}, proposal, created={path: "# runbook\n"}, repo_blobs={PROMPT: BLOB}
    )
    assert bundle.files[path] == "# runbook\n"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "README.md",
        ".factorial",
        ".factorial/",
        ".factorial/notes/../../.github/workflows/ci.yml",
        ".factorial/notes/./x.md",
        ".factorial/.hidden/x.md",
        ".factorial/notes//x.md",
        ".factorial/notes/x y.md",
    ],
)
def test_a_created_path_that_is_not_safe_under_the_config_folder_is_refused(path):
    config, proposal = _setup()
    with pytest.raises(SelectionError, match="cannot be created"):
        assemble(config, UNSTACKED, {}, proposal, created={path: "x"})


@pytest.mark.parametrize(
    "path",
    [
        PROMPT,  # the file itself
        ".factorial/agents/plan-agent/system-prompt.md/x.md",  # under a file
        ".factorial/agents/plan-agent",  # over a folder the branch has
    ],
)
def test_a_create_never_lands_on_or_around_a_file_the_branch_has(path):
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(SelectionError, match="already has a file"):
        assemble(config, UNSTACKED, {}, proposal, created={path: "x"}, repo_blobs={PROMPT: BLOB})


def test_a_created_path_the_catalog_offers_is_refused():
    config, proposal = _setup()
    path = next(p for p, f in proposal.files.items() if not f.present_in_repo)
    with pytest.raises(SelectionError, match="offered by the catalog"):
        assemble(config, UNSTACKED, {}, proposal, created={path: "x"})


def test_the_lanes_own_process_file_is_refused_as_a_created_file():
    config, proposal = _setup()
    path = process_bundle_path(config.name)
    with pytest.raises(SelectionError, match="submitted config text"):
        assemble(config, UNSTACKED, {}, proposal, created={path: "x"})


def test_a_path_sent_as_both_an_edit_and_a_created_file_is_refused():
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(SelectionError, match="twice"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            edits=[FileEdit(PROMPT, "y", BLOB)],
            created={PROMPT: "x"},
            repo_blobs={PROMPT: BLOB},
        )


OTHER_LANE = ".factorial/processes/process-other.yaml"


def test_a_deleted_file_leaves_the_bundle_and_is_named_in_deleted():
    config, proposal = _setup(present_agents=("plan-agent",))
    bundle = assemble(
        config,
        UNSTACKED,
        {},
        proposal,
        deleted=[FileDelete(PROMPT, BLOB)],
        repo_blobs={PROMPT: BLOB},
    )
    assert bundle.deleted == frozenset({PROMPT})
    assert PROMPT not in bundle.files


def test_a_delete_whose_file_moved_on_the_branch_is_a_conflict():
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(EditConflict, match=r"system-prompt\.md"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            deleted=[FileDelete(PROMPT, BLOB)],
            repo_blobs={PROMPT: "c" * 40},
        )


def test_a_delete_outside_the_config_folder_is_refused():
    config, proposal = _setup()
    workflow = ".github/workflows/ci.yml"
    # In the blobs on purpose: membership alone must not admit a path outside the config folder.
    with pytest.raises(SelectionError, match=r"not under \.factorial/"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            deleted=[FileDelete(workflow, BLOB)],
            repo_blobs={workflow: BLOB},
        )


def test_a_path_both_edited_and_deleted_is_refused():
    config, proposal = _setup(present_agents=("plan-agent",))
    with pytest.raises(SelectionError, match="both edited and deleted"):
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            edits=[FileEdit(PROMPT, "x", BLOB)],
            deleted=[FileDelete(PROMPT, BLOB)],
            repo_blobs={PROMPT: BLOB},
        )


def test_a_deleted_path_the_branch_no_longer_has_is_skipped():
    """A retried commit finds its deletion already made, and must stay a no-op."""
    config, proposal = _setup()
    bundle = assemble(
        config, UNSTACKED, {}, proposal, deleted=[FileDelete(PROMPT, BLOB)], repo_blobs={}
    )
    assert bundle.deleted == frozenset()


def test_deleting_the_lanes_own_process_file_writes_no_process_and_no_stub():
    config, proposal = _setup()
    own = process_bundle_path(config.name)
    bundle = assemble(
        config,
        UNSTACKED,
        {},
        proposal,
        deleted=[FileDelete(own, BLOB)],
        repo_blobs={own: BLOB, OTHER_LANE: BLOB},
    )
    assert bundle.deleted == frozenset({own})
    assert bundle.files == {}
    assert bundle.stub_paths == frozenset()


def test_a_retried_delete_of_the_lanes_own_process_writes_nothing():
    """The first try landed, so the branch no longer has the file. The retry must not put the
    process back: it writes nothing and removes nothing, which the commit reads as a no-op."""
    config, proposal = _setup()
    own = process_bundle_path(config.name)
    bundle = assemble(
        config,
        UNSTACKED,
        {},
        proposal,
        deleted=[FileDelete(own, BLOB)],
        repo_blobs={OTHER_LANE: BLOB},
    )
    assert bundle.files == {}
    assert bundle.deleted == frozenset()
    assert bundle.stub_paths == frozenset()


def test_a_commit_that_deletes_its_own_process_adds_no_files_for_it():
    config, proposal = _setup()
    own = process_bundle_path(config.name)
    with pytest.raises(SelectionError, match="cannot add files for that process"):
        assemble(
            config,
            UNSTACKED,
            _default_selection(proposal),
            proposal,
            deleted=[FileDelete(own, BLOB)],
            repo_blobs={own: BLOB, OTHER_LANE: BLOB},
        )


def test_deleting_the_only_process_is_refused():
    config, proposal = _setup()
    own = process_bundle_path(config.name)
    with pytest.raises(SelectionError) as caught:
        assemble(
            config,
            UNSTACKED,
            {},
            proposal,
            deleted=[FileDelete(own, BLOB)],
            repo_blobs={own: BLOB},
        )
    assert caught.value.detail == (
        "A project keeps at least one process. Add another process before you delete this one."
    )
