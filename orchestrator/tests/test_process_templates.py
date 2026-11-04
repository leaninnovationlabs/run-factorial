"""The process templates the config editor offers.

The contract under test: the editor offers the sample templates and nothing else. Each GitHub
lane parses to what docs/samples/factorial-simple's process of the same name parses to, each
GitLab lane to docs/samples/factorial-gitlab, and a first commit of each template's Simple lane
into a repo with no config writes every file its sample folder has but the other lanes', with
the sample's text. Where the sample depends on something set up outside the repo — a Runtime,
an MCP server — the template carries it commented out, and each such example works once it is
turned on.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from orchestrator.config import parse_named_process, parse_process
from orchestrator.config.lint import lint_process
from orchestrator.config.models import AgentStep, GateStep, McpRefs
from orchestrator.config.process_templates import PROCESS_TEMPLATES, ProcessTemplate
from orchestrator.config.scaffold.assemble import assemble
from orchestrator.config.scaffold.catalog import TEMPLATE_ROOT, Forge
from orchestrator.config.scaffold.propose import propose
from orchestrator.config.scaffold.validate import validate_bundle
from tests.api_harness import authed_client_for, build_app, make_settings
from tests.paths import GITLAB_SAMPLE, SAMPLE, SAMPLE_PROCESS

LANES = {lane.key: lane for lane in PROCESS_TEMPLATES[0].lanes}
SIMPLE = LANES["simple"]
GITLAB = PROCESS_TEMPLATES[1]
GITLAB_LANES = {lane.key: lane for lane in GITLAB.lanes}


def sample_lane(key: str, *, root: Path = SAMPLE) -> Path:
    return root / "processes" / f"process-{key}.yaml"


#: The sample's own README explains the sample to this repo's readers. The seeded branch carries
#: a README of its own (`seed_config_branch`), so no commit writes this one.
NOT_SCAFFOLDED = frozenset({"README.md"})
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


def test_the_editor_offers_the_sample_templates_with_every_lane_they_have() -> None:
    assert [template.key for template in PROCESS_TEMPLATES] == ["simple", "simple-gitlab"]
    for template, root, expected in (
        (PROCESS_TEMPLATES[0], SAMPLE, ["primary", "simple"]),
        (GITLAB, GITLAB_SAMPLE, ["primary", "simple"]),
    ):
        lanes = [lane.key for lane in template.lanes]
        sample_lanes = sorted(
            path.name.removeprefix("process-").removesuffix(".yaml")
            for path in (root / "processes").glob("process-*.yaml")
        )
        assert lanes == sample_lanes == expected


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

    template = process_templates.ProcessTemplate(
        key="two", title="Two", description="", forge="github"
    )
    assert [(lane.key, lane.text) for lane in template.lanes] == [
        ("feature", "name: feature\n"),
        ("hotfix", "name: hotfix\n"),
    ]


LANE_CASES = [(LANES, SAMPLE, key) for key in sorted(LANES)] + [
    (GITLAB_LANES, GITLAB_SAMPLE, key) for key in sorted(GITLAB_LANES)
]
LANE_CASE_IDS = [f"simple/{key}" for key in sorted(LANES)] + [
    f"simple-gitlab/{key}" for key in sorted(GITLAB_LANES)
]


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


def test_the_template_names_no_version() -> None:
    """A snapshot is identified by its commit; `version:` parses and decides nothing."""
    assert "version" not in yaml.safe_load(SIMPLE.text)


def test_every_gate_lets_anyone_with_write_decide() -> None:
    """The editor asks no questions, so a lane keeps its template's choice."""
    gates = [
        step
        for step in parse_process(SIMPLE.text, origin="template").steps
        if isinstance(step, GateStep)
    ]
    assert gates
    assert all(gate.gate.approver_permission == "write" for gate in gates)


@pytest.mark.parametrize("which", ["template", "sample"])
def test_the_plan_step_shows_its_summary_and_spec_files_on_its_own_branch(which: str) -> None:
    """On from the first commit: nothing outside the repository is needed, and the plan agent
    declares the skill that writes the summary."""
    text = SIMPLE.text if which == "template" else SAMPLE_PROCESS.read_text()
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
    text = SIMPLE.text if which == "template" else SAMPLE_PROCESS.read_text()
    build = parse_process(uncomment(text, "preview"), origin=which).step("build")
    assert isinstance(build, AgentStep)
    assert build.preview is not None and build.preview.services


def first_commit(template: ProcessTemplate = PROCESS_TEMPLATES[0], forge: Forge = "github"):
    """The template's Simple lane with every file ticked by default, into a repo with no config
    yet, on a project whose issues live on `forge`."""
    text = {lane.key: lane for lane in template.lanes}["simple"].text
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


SAMPLE_CASES = [(PROCESS_TEMPLATES[0], SAMPLE), (GITLAB, GITLAB_SAMPLE)]
SAMPLE_CASE_IDS = [template.key for template, _ in SAMPLE_CASES]


def sample_files(root: Path, template: ProcessTemplate) -> dict[str, Path]:
    """The sample's files by the path a first commit writes them at. The other lanes are files
    of their own, which their own commits write."""
    other_lanes = {
        f"processes/process-{lane.key}.yaml" for lane in template.lanes if lane.key != "simple"
    }
    return {
        f".factorial/{path.relative_to(root).as_posix()}": path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in NOT_SCAFFOLDED | other_lanes
    }


@pytest.mark.parametrize(("template", "root"), SAMPLE_CASES, ids=SAMPLE_CASE_IDS)
def test_a_first_commit_writes_the_whole_sample_folder(
    template: ProcessTemplate, root: Path
) -> None:
    """The sample folder's paths, every agent with real files rather than a stub — and the MCP
    server the sample defines as an inert example, which nothing selects."""
    bundle = first_commit(template, template.forge)
    loaded = validate_bundle(bundle)
    assert bundle.stub_paths == frozenset()
    assert loaded.mcp_servers == {}
    assert all(agent.mcp == McpRefs() for agent in loaded.agents.values())
    assert set(bundle.files) == set(sample_files(root, template)) - {MCP_SERVER} | {MCP_EXAMPLE}


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


@pytest.mark.parametrize(("template", "root"), SAMPLE_CASES, ids=SAMPLE_CASE_IDS)
def test_first_commit_bodies_equal_the_sample(template: ProcessTemplate, root: Path) -> None:
    """The samples are hand-kept copies of what a first commit writes, and drifted several
    releases behind before this test held them. Process files are compared parsed, above, since
    the sample's comments may say more."""
    bundle = first_commit(template, template.forge)
    sample = sample_files(root, template)
    for path, body in bundle.files.items():
        if path.startswith(".factorial/processes/"):
            continue
        target, expected = as_sample(path, body)
        assert sample[target].read_text() == expected, target


def test_the_gitlab_plan_agent_skills_are_the_github_ones() -> None:
    """The speckit skills name no forge, so the GitLab plan agent ships copies of the GitHub
    one's; a fix to one must reach the other."""
    agent = TEMPLATE_ROOT / "agents" / "plan-agent"
    github, gitlab = agent / "base-target" / "skills", agent / "base-target-gitlab" / "skills"
    files = sorted(path.relative_to(github) for path in github.rglob("*") if path.is_file())
    assert files == sorted(path.relative_to(gitlab) for path in gitlab.rglob("*") if path.is_file())
    for relative in files:
        assert (gitlab / relative).read_bytes() == (github / relative).read_bytes(), relative


def test_the_mcp_example_works_once_turned_on() -> None:
    """Rename the server file and uncomment the plan agent's `mcp:` block, as both say to."""
    bundle = first_commit()
    files = dict(bundle.files)
    files[MCP_SERVER] = files.pop(MCP_EXAMPLE)
    files[PLAN_AGENT] = uncomment(files[PLAN_AGENT], "mcp")
    loaded = validate_bundle(dataclasses.replace(bundle, files=files))
    assert loaded.agents["plan-agent"].mcp.shared == ("product-docs",)
    assert set(loaded.mcp_servers) == {"product-docs"}


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_templates_route_serves_the_whole_file() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        response = await client.get("/api/process-templates")
    assert response.status_code == 200
    templates = response.json()["templates"]
    assert [template["key"] for template in templates] == ["simple", "simple-gitlab"]
    assert all(template["title"] and template["description"] for template in templates)
    assert [(lane["key"], lane["yaml"]) for lane in templates[0]["lanes"]] == [
        (key, LANES[key].text) for key in ("primary", "simple")
    ]
    assert [(lane["key"], lane["yaml"]) for lane in templates[1]["lanes"]] == [
        (key, GITLAB_LANES[key].text) for key in ("primary", "simple")
    ]


def test_catalog_agents_are_the_manifests_a_first_commit_writes() -> None:
    from orchestrator.config import parse_agent
    from orchestrator.factorial.probe import catalog_agents

    config = parse_named_process(SIMPLE.text, key=SIMPLE.key, origin="template")
    proposal = propose(config)
    resolved = catalog_agents(config)

    assert [agent.name for agent in resolved] == ["build-agent", "plan-agent", "test-agent"]
    for agent in resolved:
        body = proposal.files[f".factorial/agents/{agent.name}/agent.yaml"].body
        assert agent.reason is None
        assert agent.manifest == parse_agent(body, origin="test")


@pytest.mark.asyncio(loop_scope="session")
async def test_the_templates_route_carries_content_and_agents() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        body = (await client.get("/api/process-templates")).json()

    [simple] = [lane for lane in body["templates"][0]["lanes"] if lane["key"] == "simple"]
    expected = parse_named_process(SIMPLE.text, key=SIMPLE.key, origin="t")
    assert simple["content"] == expected.model_dump(mode="json", by_alias=True)
    assert [agent["name"] for agent in simple["agents"]] == [
        "build-agent",
        "plan-agent",
        "test-agent",
    ]
    assert all(agent["warning"] is None and agent["model"] for agent in simple["agents"])


@pytest.mark.asyncio(loop_scope="session")
async def test_gallery_gitlab_template_resolves_gitlab_agents(monkeypatch) -> None:
    """The gallery previews the manifests an import would write, so a GitLab template's lanes
    resolve against the GitLab agents."""
    from orchestrator.api.routes import admin
    from orchestrator.factorial.probe import catalog_agents

    assert [template.forge for template in PROCESS_TEMPLATES] == ["github", "gitlab"]
    asked: list[tuple[str, str]] = []

    def spy(config, *, forge: Forge = "github"):
        asked.append((config.name, forge))
        return catalog_agents(config, forge=forge)

    monkeypatch.setattr(admin, "catalog_agents", spy)
    async with authed_client_for(build_app(make_settings(), None)) as client:
        body = (await client.get("/api/process-templates")).json()

    [gitlab] = [template for template in body["templates"] if template["key"] == "simple-gitlab"]
    for lane in gitlab["lanes"]:
        assert lane["agents"]
        assert all(agent["warning"] is None and agent["model"] for agent in lane["agents"])
    assert sorted(asked) == sorted(
        [(lane.key, "github") for lane in LANES.values()]
        + [(lane.key, "gitlab") for lane in GITLAB_LANES.values()]
    )
    for lane in GITLAB_LANES.values():
        config = parse_named_process(lane.text, key=lane.key, origin="t")
        build = catalog_agents(config, forge="gitlab")
        files = propose(config, forge="gitlab").files
        assert "merge request" in files[".factorial/agents/build-agent/agent.yaml"].body
        assert all(agent.manifest is not None for agent in build)


@pytest.mark.parametrize(
    "text",
    [
        LANES["primary"].text,
        GITLAB_LANES["primary"].text,
        sample_lane("primary").read_text(),
        sample_lane("primary", root=GITLAB_SAMPLE).read_text(),
    ],
    ids=["template", "gitlab-template", "sample", "gitlab-sample"],
)
def test_primary_build_hands_the_ticket_to_code_review(text):
    steps = {s["name"]: s for s in yaml.safe_load(text)["steps"]}
    done = steps["build"]["onSuccess"]
    assert done["ticketLabel"] == "factorial:code-review"
    assert done["assign"] == ["{roles.code_reviewers}"]
    assert "testing" not in done["comment"]
