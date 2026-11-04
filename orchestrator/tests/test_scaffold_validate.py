"""Validation runs the production loader against the bundle, with stubs stood in for.

A stub is comments-only and cannot parse; omitting it instead would trip the loader's
"step references an agent with no manifest" check. Neither failure is real, so validation
substitutes a synthetic manifest and leaves the stub body unread. A shared skill the target
repo already has is stood in the same way, for the same reason.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from orchestrator.config import ConfigError, parse_process
from orchestrator.config.process_templates import PROCESS_TEMPLATES
from orchestrator.config.scaffold import ScaffoldBundle
from orchestrator.config.scaffold.assemble import assemble
from orchestrator.config.scaffold.propose import propose
from orchestrator.config.scaffold.validate import validate_bundle


def lane(name: str) -> str:
    """The Simple template's process file, declaring lane `name`."""
    [text] = [lane.text for lane in PROCESS_TEMPLATES[0].lanes if lane.key == "simple"]
    return text.replace("\nname: simple\n", f"\nname: {name}\n")


def bundle_for(
    text: str,
    present: tuple[str, ...] = (),
    allow: bool = True,
    present_skills: tuple[str, ...] = (),
) -> ScaffoldBundle:
    """`allow=False` mirrors unticking every offered file — the operator consents to nothing,
    and `assemble` stubs every referenced agent in its place."""
    config = parse_process(text, origin="test")
    proposal = propose(config, present_agents=present, present_skills=present_skills)
    wanted = {
        path
        for component in proposal.components
        for offer in component.variants
        if allow and offer.offered and offer.default_on and not component.present_in_repo
        for path in offer.paths
    }
    selected = {path: proposal.files[path].body for path in wanted}
    return assemble(config, text, selected, proposal)


def test_a_template_bundle_validates() -> None:
    validate_bundle(bundle_for(lane("p")))


def test_a_stub_bundle_validates_without_parsing_the_stub() -> None:
    validate_bundle(bundle_for(lane("p"), allow=False))


def test_a_broken_template_manifest_is_caught() -> None:
    bundle = bundle_for(lane("p"))
    files = dict(bundle.files)
    body = files[".factorial/agents/plan-agent/agent.yaml"]
    files[".factorial/agents/plan-agent/agent.yaml"] = re.sub(
        r"(?m)^model: .*$", "model: sonnet", body
    )
    with pytest.raises(ConfigError, match="size alias"):
        validate_bundle(dataclasses.replace(bundle, files=files))


def test_a_missing_prompt_file_is_caught() -> None:
    """The loader's declared-path check is exactly what a hand-built bundle would get wrong."""
    bundle = bundle_for(lane("p"))
    files = {
        k: v
        for k, v in bundle.files.items()
        if k != ".factorial/agents/plan-agent/system-prompt.md"
    }
    with pytest.raises(ConfigError, match=r"prompts\.system"):
        validate_bundle(dataclasses.replace(bundle, files=files))


def test_a_present_agent_is_stood_in_for_too() -> None:
    """`present_agents` files are not in the bundle, but the process still references them."""
    bundle = bundle_for(lane("p"), present=("plan-agent",))
    validate_bundle(bundle)


def test_an_unparseable_process_is_caught() -> None:
    bundle = bundle_for(lane("p"))
    files = dict(bundle.files)
    files[".factorial/process.yaml"] = "specVersion: adlc/v1\nkind: Process\n"
    with pytest.raises(ConfigError):
        validate_bundle(dataclasses.replace(bundle, files=files))


def test_a_shared_skill_already_in_the_repo_is_stood_in_for() -> None:
    """The bundle deliberately omits it, but the manifests it does carry declare it — without a
    stand-in the loader reports a missing skill for config that is in fact complete."""
    bundle = bundle_for(lane("p"), present_skills=("project-constitution",))
    assert ".factorial/skills/project-constitution/SKILL.md" not in bundle.files
    validate_bundle(bundle)


def test_a_declared_skill_that_ships_nowhere_is_caught() -> None:
    """The stand-in covers a skill the repo has, never one nothing anywhere provides — a manifest
    naming a skill no repo file backs is a 404 an hour into the run."""
    bundle = bundle_for(lane("p"))
    files = dict(bundle.files)
    files[".factorial/agents/plan-agent/agent.yaml"] = files[
        ".factorial/agents/plan-agent/agent.yaml"
    ].replace("- project-constitution", "- nonexistent-policy")
    with pytest.raises(ConfigError, match="nonexistent-policy"):
        validate_bundle(dataclasses.replace(bundle, files=files))


def test_an_edited_manifest_resolves_the_prompts_the_branch_already_has() -> None:
    """Without the branch's folder the edited manifest's prompts exist nowhere; with it, the loader
    reads the tree the commit will actually produce."""
    config = parse_process(lane("p"), origin="test")
    agent = ".factorial/agents/plan-agent/"
    on_branch = {p: f.body for p, f in propose(config).files.items() if p.startswith(agent)}
    manifest = f"{agent}agent.yaml"
    bundle = bundle_for(lane("p"), present=("plan-agent",))
    edited = dataclasses.replace(bundle, files={**bundle.files, manifest: on_branch[manifest]})
    with pytest.raises(ConfigError, match=r"missing file 'system-prompt\.md'"):
        validate_bundle(edited)
    validate_bundle(edited, base_files=on_branch)


def test_an_edited_other_lane_naming_an_unknown_agent_is_refused() -> None:
    """Against the branch, the loader sees every lane on it; an agent no file provides must fail
    rather than be stood in for, or sync would refuse the commit after it landed."""
    other = re.sub(r"(agent: )\S+", r"\1ghost-agent", lane("other"))
    assert "ghost-agent" in other
    base = {".factorial/processes/process-other.yaml": other}
    with pytest.raises(ConfigError, match="ghost-agent"):
        validate_bundle(bundle_for(lane("p")), base_files=base)
