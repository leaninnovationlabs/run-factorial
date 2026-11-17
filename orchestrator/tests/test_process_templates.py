"""The process templates the config editor offers.

The contract under test: the editor offers the sample templates and nothing else. The Simple
template's one lane, `primary`, parses to what docs/samples/factorial-simple's process of the
same name parses to, the Superpowers lane to docs/samples/factorial-superpowers, and a first
commit of each template's lane into a repo with no config, on GitHub or on GitLab, writes every
file its sample folder has for that lane, with the sample's text. The Simple sample keeps one
lane no template ships, `simple`, as the reference for the format; a first commit of it writes
the rest of that folder. Where the sample depends on something set up outside the repo — a
Runtime, an MCP server — the template carries it commented out, and each such example works once
it is turned on.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from orchestrator.config import (
    ConfigError,
    load_config_folder,
    parse_agent,
    parse_mcp_server,
    parse_named_process,
    parse_process,
)
from orchestrator.config.lint import lint_process
from orchestrator.config.models import AgentStep, GateStep, McpRefs
from orchestrator.config.process_templates import CONFIG_SHELLS, PROCESS_TEMPLATES
from orchestrator.config.scaffold.assemble import assemble
from orchestrator.config.scaffold.catalog import Forge
from orchestrator.config.scaffold.propose import propose
from orchestrator.config.scaffold.validate import validate_bundle
from tests.api_harness import authed_client_for, build_app, make_settings
from tests.paths import SAMPLE, SAMPLE_PROCESS

SIMPLE_TEMPLATE = PROCESS_TEMPLATES[0]
LANES = {lane.key: lane for lane in SIMPLE_TEMPLATE.lanes}
PRIMARY = LANES["primary"]
SUPERPOWERS = PROCESS_TEMPLATES[1]
SUPERPOWERS_LANES = {lane.key: lane for lane in SUPERPOWERS.lanes}


def sample_lane(key: str, *, root: Path = SAMPLE) -> Path:
    return root / "processes" / f"process-{key}.yaml"


#: The sample's own README explains the sample to this repo's readers. The seeded branch carries
#: a README of its own (`seed_config_branch`), so no commit writes this one.
NOT_SCAFFOLDED = frozenset({"README.md"})
SUPERPOWERS_SAMPLE = SAMPLE.parent / "factorial-superpowers"
MCP_SERVER = ".factorial/mcp/product-docs.yaml"
MCP_EXAMPLE = f"{MCP_SERVER}.example"
PLAN_AGENT = ".factorial/agents/plan-agent/agent.yaml"


def uncomment(text: str, key: str) -> str:
    """The text with every commented-out `<key>:` block turned on."""
    lines: list[str] = []
    indent: str | None = None
    for line in text.splitlines(keepends=True):
        opened = re.match(rf"^(\s*)# {key}:\s*$", line)
        if opened:
            indent = opened.group(1)
        elif indent is None or not line.startswith(f"{indent}#   "):
            indent = None
            lines.append(line)
            continue
        lines.append(line.replace("# ", "", 1))
    return "".join(lines)


def test_the_editor_offers_the_sample_templates_each_with_its_one_lane() -> None:
    """The Simple sample's `simple` lane is the reference for the format, and no template
    ships it."""
    assert [template.key for template in PROCESS_TEMPLATES] == ["simple", "superpowers"]
    for template, root, expected, reference_only in (
        (SIMPLE_TEMPLATE, SAMPLE, ["primary"], ["simple"]),
        (SUPERPOWERS, SUPERPOWERS_SAMPLE, ["superpowers"], []),
    ):
        lanes = [lane.key for lane in template.lanes]
        sample_lanes = sorted(
            path.name.removeprefix("process-").removesuffix(".yaml")
            for path in (root / "processes").glob("process-*.yaml")
        )
        assert lanes == expected
        assert sample_lanes == sorted(expected + reference_only)


def test_a_templates_lanes_are_the_process_files_in_its_folder(tmp_path, monkeypatch) -> None:
    """A template holds several lanes by holding several files: an import writes every one,
    and nothing in a registry entry has to list them."""
    from orchestrator.config import process_templates

    folder = tmp_path / "two" / "processes"
    folder.mkdir(parents=True)
    (folder / "process-hotfix.yaml").write_text("name: hotfix\n")
    (folder / "process-feature.yaml").write_text("name: feature\n")
    (folder / "notes.md").write_text("not a lane\n")
    monkeypatch.setattr(process_templates, "TEMPLATES_DIR", tmp_path)

    template = process_templates.ProcessTemplate(key="two", title="Two", description="")
    assert [(lane.key, lane.text) for lane in template.lanes] == [
        ("feature", "name: feature\n"),
        ("hotfix", "name: hotfix\n"),
    ]


LANE_CASES = [(LANES, SAMPLE, key) for key in sorted(LANES)] + [
    (SUPERPOWERS_LANES, SUPERPOWERS_SAMPLE, "superpowers")
]
LANE_CASE_IDS = [f"simple/{key}" for key in sorted(LANES)] + ["superpowers/superpowers"]


@pytest.mark.parametrize(("lanes", "root", "key"), LANE_CASES, ids=LANE_CASE_IDS)
def test_each_template_lane_is_the_sample_lane_naming_no_runtime(
    lanes: dict, root: Path, key: str
) -> None:
    """Comments differ, on purpose: the sample traces the orchestrator's code for this repo's
    readers. What a lane does may not — except that the template names no Runtime, which sync
    would refuse on a deployment without it, until its example is turned on."""
    text = lanes[key].text
    template = parse_process(text, origin="template")
    sample = parse_process(sample_lane(key, root=root).read_text(), origin="sample")
    assert template.runtime is None
    turned_on = parse_process(text.replace("\n# runtime: ", "\nruntime: "), origin="t")
    assert turned_on.model_dump(mode="json") == sample.model_dump(mode="json")


@pytest.mark.parametrize(
    ("lanes", "key"),
    [(lanes, key) for lanes, _, key in LANE_CASES],
    ids=LANE_CASE_IDS,
)
def test_each_template_lane_declares_its_own_name_and_lints_clean(lanes: dict, key: str) -> None:
    config = parse_named_process(lanes[key].text, key=key, origin="template")
    assert lint_process(config) == []


@pytest.mark.parametrize(
    ("lanes", "key"),
    [(lanes, key) for lanes, _, key in LANE_CASES],
    ids=LANE_CASE_IDS,
)
def test_every_merging_gate_names_an_agent_step_to_fix_a_conflict(lanes: dict, key: str) -> None:
    config = parse_named_process(lanes[key].text, key=key, origin="template")
    merging = [
        step.name
        for step in config.steps
        if isinstance(step, GateStep) and step.gate.on_approve.merge
    ]
    assert merging
    for name in merging:
        assert isinstance(config.conflict_fix_step(name), AgentStep), name


def test_the_template_names_no_version() -> None:
    """A snapshot is identified by its commit; `version:` parses and decides nothing."""
    assert "version" not in yaml.safe_load(PRIMARY.text)


def test_every_gate_lets_anyone_with_write_decide() -> None:
    """The editor asks no questions, so a lane keeps its template's choice."""
    gates = [
        step
        for step in parse_process(PRIMARY.text, origin="template").steps
        if isinstance(step, GateStep)
    ]
    assert gates
    assert all(gate.gate.approver_permission == "write" for gate in gates)


@pytest.mark.parametrize("which", ["template", "sample"])
def test_the_plan_step_shows_its_summary_and_spec_files_on_its_own_branch(which: str) -> None:
    """On from the first commit: nothing outside the repository is needed, and the plan agent
    declares the skill that writes the summary."""
    text = PRIMARY.text if which == "template" else SAMPLE_PROCESS.read_text()
    plan = parse_process(text, origin=which).step("plan")
    assert isinstance(plan, AgentStep)
    assert plan.preview is not None and plan.preview.page is None
    assert [(f.path, f.label) for f in plan.preview.files] == [
        ("specs/{ticket}-{slug}/summary.html", "Summary"),
        ("specs/{ticket}-{slug}/spec.md", "Spec"),
        ("specs/{ticket}-{slug}/plan.md", "Plan"),
        ("specs/{ticket}-{slug}/tasks.md", "Tasks"),
    ]
    assert all(f.path.startswith("specs/{ticket}-{slug}/") for f in plan.preview.files)


@pytest.mark.parametrize("which", ["template", "sample"])
def test_the_commented_app_preview_parses_once_turned_on(which: str) -> None:
    """An operator turns the build step's preview on by uncommenting it, so the block must be one
    the parser accepts as written."""
    text = PRIMARY.text if which == "template" else SAMPLE_PROCESS.read_text()
    build = parse_process(uncomment(text, "preview"), origin=which).step("build")
    assert isinstance(build, AgentStep)
    assert build.preview is not None and build.preview.services


def first_commit(text: str = PRIMARY.text, forge: Forge = "github"):
    """A lane with every file ticked by default, into a repo with no config yet, on a project
    whose issues live on `forge`."""
    config = parse_process(text, origin="template")
    proposal = propose(config, forge=forge)
    selected = {
        path: proposal.files[path].body
        for component in proposal.components
        for offer in component.variants
        if offer.offered and offer.default_on and not component.present_in_repo
        for path in offer.paths
    }
    return assemble(config, text, selected, proposal)


FORGES: tuple[Forge, ...] = ("github", "gitlab")
#: Each lane a first commit is tested with, as (the text committed, its sample folder, the lane,
#: the sample's files only another lane's commit writes). The Simple sample's `simple` lane has
#: no template, so its own text is committed: that holds its `test-agent` to the catalog's.
TEST_AGENT = frozenset(
    f"agents/test-agent/{name}" for name in ("agent.yaml", "instructions.md", "system-prompt.md")
)
FIRST_COMMITS = {
    "simple/primary": (PRIMARY.text, SAMPLE, "primary", TEST_AGENT),
    "simple/reference": (SAMPLE_PROCESS.read_text(), SAMPLE, "simple", frozenset()),
    "superpowers": (
        SUPERPOWERS_LANES["superpowers"].text,
        SUPERPOWERS_SAMPLE,
        "superpowers",
        frozenset(),
    ),
}
SAMPLE_CASES = [(*case, forge) for case in FIRST_COMMITS.values() for forge in FORGES]
SAMPLE_CASE_IDS = [f"{name}/{forge}" for name in FIRST_COMMITS for forge in FORGES]


def sample_files(root: Path, lane: str, others: frozenset[str]) -> dict[str, Path]:
    """The sample's files by the path a first commit of `lane` writes them at. The other lanes
    are files of their own, which their own commits write, as are the files in `others`."""
    other_lanes = {
        path.relative_to(root).as_posix()
        for path in (root / "processes").glob("process-*.yaml")
        if path != sample_lane(lane, root=root)
    }
    skipped = NOT_SCAFFOLDED | other_lanes | others
    return {
        f".factorial/{path.relative_to(root).as_posix()}": path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in skipped
    }


@pytest.mark.parametrize(
    ("text", "root", "lane", "others", "forge"), SAMPLE_CASES, ids=SAMPLE_CASE_IDS
)
def test_a_first_commit_writes_the_whole_sample_folder(
    text: str, root: Path, lane: str, others: frozenset[str], forge: Forge
) -> None:
    """The sample folder's paths, every agent with real files rather than a stub, on either
    forge — and the MCP server the sample defines as an inert example, which nothing selects."""
    bundle = first_commit(text, forge)
    loaded = validate_bundle(bundle)
    assert bundle.stub_paths == frozenset()
    assert loaded.mcp_servers == {}
    assert all(agent.mcp == McpRefs() for agent in loaded.agents.values())
    expected = set(sample_files(root, lane, others))
    if MCP_SERVER in expected:
        expected = expected - {MCP_SERVER} | {MCP_EXAMPLE}
    assert set(bundle.files) == expected


def _cut(pattern: str, text: str) -> str:
    cut, count = re.subn(pattern, "", text, flags=re.S)
    assert count == 1, f"the allowed difference {pattern!r} no longer matches"
    return cut


def as_sample(path: str, body: str) -> tuple[str, str]:
    """The one difference a sample may have from its first commit: it shows the MCP example
    turned on. The server file is `product-docs.yaml`, without its "AN EXAMPLE, AND INERT"
    paragraph; the plan agent's `mcp:` block is uncommented, without its "An example, and off"
    paragraph."""
    if path == MCP_EXAMPLE:
        return MCP_SERVER, _cut(r"# AN EXAMPLE, AND INERT\..*?\n#\n", body)
    if path == PLAN_AGENT:
        return path, uncomment(_cut(r"#\n# An example, and off:.*?(?=# mcp:)", body), "mcp")
    return path, body


@pytest.mark.parametrize(
    ("text", "root", "lane", "others", "forge"), SAMPLE_CASES, ids=SAMPLE_CASE_IDS
)
def test_first_commit_bodies_equal_the_sample(
    text: str, root: Path, lane: str, others: frozenset[str], forge: Forge
) -> None:
    """The samples are hand-kept copies of what a first commit writes, and drifted several
    releases behind before this test held them. Process files are compared parsed, above, since
    the sample's comments may say more."""
    bundle = first_commit(text, forge)
    sample = sample_files(root, lane, others)
    for path, body in bundle.files.items():
        if path.startswith(".factorial/processes/"):
            continue
        target, expected = as_sample(path, body)
        assert sample[target].read_text() == expected, target


def test_the_mcp_example_works_once_turned_on() -> None:
    """Rename the server file and uncomment the plan agent's `mcp:` block, as both say to."""
    bundle = first_commit()
    files = dict(bundle.files)
    files[MCP_SERVER] = files.pop(MCP_EXAMPLE)
    files[PLAN_AGENT] = uncomment(files[PLAN_AGENT], "mcp")
    loaded = validate_bundle(dataclasses.replace(bundle, files=files))
    assert loaded.agents["plan-agent"].mcp.shared == ("product-docs",)
    assert set(loaded.mcp_servers) == {"product-docs"}


def test_the_superpowers_sample_loads_and_lints_clean() -> None:
    """The lane is the reference for `request_approval()` and `checklist_done()`. The template
    tests compare it file by file, and only this one loads the folder as a whole."""
    config = load_config_folder(SUPERPOWERS_SAMPLE)
    assert set(config.processes) == {"superpowers"}
    lane = config.processes["superpowers"]
    assert {step.agent for step in lane.steps if isinstance(step, AgentStep)} <= set(config.agents)
    assert lint_process(lane) == []


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_templates_route_serves_the_whole_file() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        response = await client.get("/api/process-templates")
    assert response.status_code == 200
    templates = response.json()["templates"]
    assert [template["key"] for template in templates] == ["simple", "superpowers"]
    assert all(template["title"] and template["description"] for template in templates)
    assert [(lane["key"], lane["yaml"]) for lane in templates[0]["lanes"]] == [
        ("primary", PRIMARY.text)
    ]


def test_catalog_agents_are_the_manifests_a_first_commit_writes() -> None:
    from orchestrator.config import parse_agent
    from orchestrator.factorial.probe import catalog_agents

    config = parse_named_process(PRIMARY.text, key=PRIMARY.key, origin="template")
    proposal = propose(config)
    resolved = catalog_agents(config)

    assert [agent.name for agent in resolved] == ["build-agent", "plan-agent"]
    for agent in resolved:
        body = proposal.files[f".factorial/agents/{agent.name}/agent.yaml"].body
        assert agent.reason is None
        assert agent.manifest == parse_agent(body, origin="test")


@pytest.mark.asyncio(loop_scope="session")
async def test_the_templates_route_carries_content_and_agents() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        body = (await client.get("/api/process-templates")).json()

    [primary] = body["templates"][0]["lanes"]
    expected = parse_named_process(PRIMARY.text, key=PRIMARY.key, origin="t")
    assert primary["content"] == expected.model_dump(mode="json", by_alias=True)
    assert [agent["name"] for agent in primary["agents"]] == ["build-agent", "plan-agent"]
    assert all(agent["warning"] is None and agent["model"] for agent in primary["agents"])


@pytest.mark.parametrize(
    "text",
    [LANES["primary"].text, sample_lane("primary").read_text()],
    ids=["template", "sample"],
)
def test_primary_build_hands_the_ticket_to_code_review(text):
    steps = {s["name"]: s for s in yaml.safe_load(text)["steps"]}
    done = steps["build"]["onSuccess"]
    assert done["ticketLabel"] == "factorial:code-review"
    assert done["assign"] == ["{roles.code_reviewers}"]
    assert "testing" not in done["comment"]


# --- the shells a new agent, skill, MCP server or process starts from ---------------------------

EXAMPLE_START = "# --- example: uncomment and edit ---"
EXAMPLE_END = "# --- end example ---"


def named(kind: str, name: str) -> dict[str, str]:
    return {file: body.replace("__NAME__", name) for file, body in CONFIG_SHELLS[kind].items()}


def uncommented(text: str) -> str:
    """The shell with its example block turned on, as its own first lines tell the operator to."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line == EXAMPLE_START:
            inside = True
        elif line == EXAMPLE_END:
            inside = False
        elif inside:
            assert line == "#" or line.startswith("# "), line
            out.append(line[2:])
        else:
            out.append(line)
    assert not inside
    return "\n".join(out) + "\n"


def test_the_agent_shell_is_a_complete_agent_as_created() -> None:
    files = named("agent", "review-agent")
    manifest = parse_agent(files["agent.yaml"], origin="shell")
    assert set(files) == {"agent.yaml", "system-prompt.md", "instructions.md"}
    assert manifest.name == "review-agent"
    assert {manifest.prompts.system, manifest.prompts.instructions} <= set(files)
    assert [secret.key for secret in manifest.secrets] == ["GH_TOKEN"]
    assert manifest.limits.max_turns == 200


def test_the_agent_shell_prompt_keeps_the_prohibitions() -> None:
    """Merging is enforced; the rest is a prompt rule, so the sentence is the control."""
    prompt = CONFIG_SHELLS["agent"]["system-prompt.md"]
    for phrase in ("merge", "self-approve", "add labels", "push to `main`"):
        assert phrase in prompt


def test_the_skill_shell_names_itself_in_its_frontmatter() -> None:
    assert named("skill", "review-notes")["SKILL.md"].startswith("---\nname: review-notes\n")


def test_no_shell_keeps_its_marker_once_named() -> None:
    for kind in CONFIG_SHELLS:
        assert all("__NAME__" not in body for body in named(kind, "x").values())


def test_the_mcp_shell_is_refused_as_created_and_parses_once_its_example_is_uncommented() -> None:
    text = named("mcp", "tracker")["server.yaml"]
    with pytest.raises(ConfigError, match="transport"):
        parse_mcp_server(text, name="tracker", origin="shell")
    assert parse_mcp_server(uncommented(text), name="tracker", origin="shell").name == "tracker"


def test_the_process_shell_is_refused_as_created_and_parses_once_its_example_is_uncommented() -> (
    None
):
    text = named("process", "fast")["process.yaml"]
    with pytest.raises(ConfigError, match="steps"):
        parse_named_process(text, key="fast", origin="shell")
    config = parse_named_process(uncommented(text), key="fast", origin="shell")
    assert config.name == "fast"
    assert lint_process(config) == []


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_templates_route_serves_every_shell() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        response = await client.get("/api/process-templates")
    assert response.status_code == 200
    shells = response.json()["shells"]
    assert set(shells) == {"agent", "skill", "mcp", "process"}
    assert shells["agent"]["agent.yaml"] == CONFIG_SHELLS["agent"]["agent.yaml"]
