"""Proving a bundle before the App commits it (design.md §7.7: never propose what sync rejects).

The bundle is written to a temp directory and handed to `load_config_folder`, the disk loader
`docs/bootstrap.md`'s one-liner uses. It is stricter than sync, which reads through
`factorial/source.py`, defers agents to spawn and reports an agent gap only as a warning. An
in-memory reimplementation would be a second opinion that could drift; this one cannot.

Two kinds of agent have no parseable manifest in the bundle and are stood in for rather than read:
a comments-only stub, and an agent that already exists in the target repo (whose files the bundle
deliberately does not carry). Omitting either would fail the loader's "step references an agent
with no manifest" check — a failure about this function, not about the operator's config.

A shared skill already in the target repo is stood in the same way and for the same reason: the
bundle does not rewrite it, but the loader resolves every `skills.shared` name against the folder
in front of it, so its absence here would read as a defect in config that is in fact complete. The
bundle names those skills, so a manifest declaring one that exists nowhere still fails.

A shared MCP server already in the target repo gets the same treatment for the same reason: the
loader resolves every `mcp.shared` name against `.factorial/mcp/`, and the bundle does not rewrite a
server the repo already owns.

A commit that edits files the target repo already has is validated against the repo too: the route
passes every path in the config branch's folder as `base_files` — the text of each file the loader
parses, and an empty stand-in for the rest, whose existence is all it checks — and the bundle is
written over it, so the loader reads the tree the commit will produce. Without it, an edited
manifest would name prompts that exist nowhere in front of the loader. A file the commit deletes
is left out of that tree, so a manifest that still names it fails here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path

from orchestrator.config.loader import (
    AGENT_FILE,
    AGENTS_DIR,
    MCP_DIR,
    SKILL_FILE,
    SKILLS_DIR,
    AdlcConfig,
    ConfigError,
    load_config_folder,
    load_processes,
)
from orchestrator.config.models import AgentStep
from orchestrator.config.scaffold import CONFIG_DIR, ScaffoldBundle

__all__ = ["validate_bundle"]

#: A stubbed manifest's own prompts, left unwritten (`agent.yaml` is written separately above) —
#: the loader still resolves `prompts.system`/`prompts.instructions` against real files.
_STUB_PROMPTS = ("system-prompt.md", "instructions.md")

#: Written only inside the temp directory, never proposed to a repo. Minimal on purpose: its job
#: is to let the loader check everything *else* about the process.
_PLACEHOLDER_MANIFEST = """\
specVersion: factorial/v1
kind: Agent
name: {name}
prompts:
  system: system-prompt.md
  instructions: instructions.md
model: claude-sonnet-5-5
limits:
  max_turns: 200
"""

#: Written only inside the temp directory, never proposed to a repo. Unlike a skill stand-in, this
#: must parse as a valid `McpServerManifest` — `load_shared_mcp_servers` validates every file under
#: `mcp/`, not merely checks that it exists.
_PLACEHOLDER_MCP = """\
specVersion: factorial/v1
kind: McpServer
name: {name}
transport: http
http:
  url: https://example.invalid/{name}
required: false
"""


def validate_bundle(
    bundle: ScaffoldBundle, base_files: Mapping[str, str] | None = None
) -> AdlcConfig:
    """Raise `ConfigError` if the orchestrator would refuse this bundle at sync; otherwise return
    the folder as the loader read it."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return _load(Path(tmp), bundle, base_files)
        except ConfigError as exc:
            # The loader names the temp copy it read; the operator knows the repository's path.
            raise ConfigError(str(exc.path).removeprefix(f"{tmp}/"), exc.detail) from exc


def _load(root: Path, bundle: ScaffoldBundle, base_files: Mapping[str, str] | None) -> AdlcConfig:
    for path, content in (base_files or {}).items():
        if not path.startswith(f"{CONFIG_DIR}/") or path in bundle.deleted:
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for path, content in bundle.files.items():
        if path in bundle.stub_paths:
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    config_root = root / CONFIG_DIR
    # Read the processes with the same loader the folder check below uses, so the stand-in
    # loop and the real check cannot disagree about which processes the tree contains.
    agents = {
        step.agent
        for process in load_processes(config_root).values()
        for step in process.steps
        if isinstance(step, AgentStep)
    }
    for agent in sorted(agents):
        manifest = config_root / AGENTS_DIR / agent / AGENT_FILE
        if manifest.is_file():
            continue
        # Against the branch every agent a lane names already has its real manifest, so a
        # missing one is a defect in an edited lane, not a gap to stand in for.
        if base_files is not None and f"{CONFIG_DIR}/{AGENTS_DIR}/{agent}/{AGENT_FILE}" not in (
            bundle.stub_paths
        ):
            continue
        folder = config_root / AGENTS_DIR / agent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / AGENT_FILE).write_text(_PLACEHOLDER_MANIFEST.format(name=agent))
        for prompt in _STUB_PROMPTS:
            (folder / prompt).write_text("")

    # Named explicitly, never inferred from what the manifests declare: a stand-in for every
    # undeclared-and-absent skill would also absorb a manifest naming a skill nothing
    # provides, which is a real defect and must still fail here.
    for name in sorted(bundle.present_skills):
        skill = config_root / SKILLS_DIR / name / SKILL_FILE
        # A skill the commit deletes is gone from the tree it produces, so nothing stands in.
        if skill.is_file() or f"{CONFIG_DIR}/{SKILLS_DIR}/{name}/{SKILL_FILE}" in bundle.deleted:
            continue
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("")

    # Same reasoning as the skill loop above, and for the same defect this guards against:
    # a stand-in for every undeclared-and-absent server would also absorb a manifest naming
    # a server nothing provides, which must still fail here.
    for name in sorted(bundle.present_mcp):
        server = config_root / MCP_DIR / f"{name}.yaml"
        if server.is_file() or f"{CONFIG_DIR}/{MCP_DIR}/{name}.yaml" in bundle.deleted:
            continue
        server.parent.mkdir(parents=True, exist_ok=True)
        server.write_text(_PLACEHOLDER_MCP.format(name=name))

    return load_config_folder(config_root)
