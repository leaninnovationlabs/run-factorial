"""Loading a target repo's `.factorial/` folder — from disk, or from YAML text pulled out of GitHub.

`.factorial/` content is repo-controlled input, so it is read by a safe loader that also refuses
anchors and aliases: a few kilobytes of nested aliases expand to gigabytes, and the API parses
unsaved text for any member. Every failure is re-raised as a `ConfigError` naming the offending
file.

The parse half is source-agnostic because config is pinned to a commit sha and the repo-backed
resolver has no filesystem: `parse_process` / `parse_agent` take text plus an `origin` string,
and `load_process` / `load_agent` are the disk wrappers.

A project may run several ADLC lanes, one file each under `processes/`, and a ticket picks one at
admission. The lane key is the file stem minus its `process-` prefix, checked against the file's
own `name:`. The identity rules for a key live here, at the boundary, and never on
`ProcessConfig` — `check_process_key` says why.

Prompt and skill contents are never read here — the disk loader only checks that declared paths
exist, and the repo-backed resolver proves the same thing by fetching each file at the pin.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from orchestrator.config.models import AgentManifest, AgentStep, McpServerManifest, ProcessConfig

#: The legacy single-process layout. Still read, for ever; never written again.
PROCESS_FILE = "process.yaml"
#: One YAML document per lane, at `<root>/processes/process-<key>.yaml`. The stem minus the
#: prefix IS the lane key, checked against the file's own `name:` — the rule `load_agent`
#: already applies to `agents/<name>/agent.yaml`.
PROCESSES_DIR = "processes"
PROCESS_PREFIX = "process-"
PROCESS_SUFFIX = ".yaml"
#: The default is a property of the project, held in `project.settings` where no agent can reach
#: it (design.md §7.5). A lane called `default` would put a second, contradictory answer in the
#: repo, and would read wrong everywhere the lane's name is shown.
RESERVED_PROCESS_KEYS: Final[frozenset[str]] = frozenset({"default"})
#: A lane key becomes a path segment and a `factorial:process:<key>` label, so it is a slug.
#: Enforced HERE and never on `ProcessConfig`: the model re-parses pinned snapshots every tick,
#: so a pattern on the model would fail an old non-slug snapshot into quarantine (decision 31).
PROCESS_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

AGENTS_DIR = "agents"
AGENT_FILE = "agent.yaml"
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
#: One YAML document per server, at `<root>/mcp/<name>.yaml` (shared) or
#: `agents/<agent>/mcp/<name>.yaml` (local).
MCP_DIR = "mcp"
MCP_SUFFIX = ".yaml"
#: An example server the catalog ships, which nothing reads: renaming it to `.yaml` is what
#: makes it a server, so an example can never select one by accident.
MCP_EXAMPLE_SUFFIX = ".yaml.example"


#: Far above any real config file; checked before parsing, so the parser never sees more.
MAX_CONFIG_CHARS: Final = 128 * 1024


class ConfigError(Exception):
    """A `.factorial/` file is missing, unparseable, or invalid.

    `origin` is the display string. `path` keeps the caller's original object — a `Path` on
    disk, or the `owner/name@<sha7>:<path>` string a repo-backed caller passed in.
    """

    def __init__(self, path: Path | str, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.origin = str(path)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AdlcConfig:
    """A whole `.factorial/` folder: every process lane plus the agent manifests they reference."""

    #: `{lane key: config}`. A repo on the legacy layout has exactly one entry, under the root
    #: `process.yaml`'s own `name:`.
    processes: dict[str, ProcessConfig]
    agents: dict[str, AgentManifest]
    #: The shared `<root>/mcp/` catalog, by name. Agent-local servers are validated in place and
    #: not collected here — they belong to one agent and nothing else may reference them.
    mcp_servers: dict[str, McpServerManifest] = field(default_factory=dict)


class _AliasRefused(yaml.YAMLError):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


class _NoAliasLoader(yaml.SafeLoader):
    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _AliasRefused(event.start_mark.line + 1)
        return super().compose_node(parent, index)


def _parse_yaml(text: str, origin: Path | str) -> Any:
    if len(text) > MAX_CONFIG_CHARS:
        raise ConfigError(origin, f"The file is larger than {MAX_CONFIG_CHARS // 1024} KiB.")
    loader = _NoAliasLoader(text)
    try:
        return loader.get_single_data()
    except _AliasRefused as exc:
        raise ConfigError(
            origin,
            f"YAML anchors and aliases are not supported (line {exc.line}). "
            "Write each value out in full.",
        ) from None
    except yaml.YAMLError as exc:
        raise ConfigError(origin, _yaml_problem(exc)) from exc
    finally:
        loader.dispose()


def _yaml_problem(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is None or not problem:
        return f"The file is not valid YAML: {exc}"
    where = f"line {mark.line + 1}, column {mark.column + 1}"
    return f"The file is not valid YAML at {where}: {problem}."


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(path, "File not found.")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(path, f"Factorial couldn't read the file: {exc}") from exc


def _read_yaml(path: Path) -> Any:
    return _parse_yaml(_read_text(path), path)


def _invalid(what: str, exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False):
        msg = error["msg"].removeprefix("Value error, ")
        msg = msg if msg.endswith((".", "?", "!")) else f"{msg}."
        where = ".".join(str(part) for part in error["loc"])
        lines.append(f"{where}: {msg}" if where else msg)
    return f"{what}:\n" + "\n".join(lines)


# --- source-agnostic parsing --------------------------------------------------------------------
#
# `origin` only ever names the offending file in an error. Disk callers pass a `Path`; the
# repo-backed resolver passes `owner/name@<sha7>:<path>`, so a log line says which commit the bad
# config came from.


_EMPTY: Final = "The file is empty or holds only comments."


def _validate_process(data: Any, origin: Path | str) -> ProcessConfig:
    if data is None:
        raise ConfigError(origin, _EMPTY)
    try:
        return ProcessConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(origin, _invalid("The process config is invalid", exc)) from exc


def _validate_agent(data: Any, origin: Path | str) -> AgentManifest:
    if data is None:
        raise ConfigError(origin, _EMPTY)
    try:
        return AgentManifest.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(origin, _invalid("The agent manifest is invalid", exc)) from exc


def _validate_mcp_server(data: Any, origin: Path | str) -> McpServerManifest:
    if data is None:
        raise ConfigError(origin, _EMPTY)
    try:
        return McpServerManifest.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(origin, _invalid("The MCP server manifest is invalid", exc)) from exc


def process_file_name(key: str) -> str:
    """The file one lane lives in, under `processes/`."""
    return f"{PROCESS_PREFIX}{key}{PROCESS_SUFFIX}"


def check_process_key(key: str, origin: Path | str) -> None:
    """The identity rules a NEW lane must satisfy — slug shape, and not the reserved name.

    Called wherever a lane key is established: the `processes/` loader below, and every route
    that registers a process. Deliberately not called when re-parsing a stored snapshot or when
    reading the legacy root `process.yaml`, which keeps whatever name it was written with
    (design.md §4) — an old identity is history, and history is not validated.
    """
    if key in RESERVED_PROCESS_KEYS:
        raise ConfigError(
            origin,
            f"Process name {key!r} is reserved. The default process is chosen in the project's "
            "settings, not by a file in the repository.",
        )
    if not PROCESS_KEY_PATTERN.match(key):
        raise ConfigError(
            origin,
            f"Process name {key!r} is not a slug. Use lowercase letters, digits and dashes, "
            "starting and ending with a letter or digit.",
        )


def parse_process(text: str, *, origin: str) -> ProcessConfig:
    """Validate `process.yaml` text."""
    return _validate_process(_parse_yaml(text, origin), origin)


def dump_config_yaml(data: Mapping[str, Any]) -> str:
    """The one emitter for config YAML — every emission formats identically or not at all."""
    return yaml.safe_dump(dict(data), sort_keys=False, width=100, allow_unicode=True)


def parse_named_process(text: str, *, key: str, origin: str) -> ProcessConfig:
    """Validate one `processes/process-<key>.yaml`. `key` is the file stem minus its prefix and
    must equal the config's `name:` — the key is what a `factorial:process:<key>` label selects,
    so a mismatch would offer a lane under a name nothing in the repo answers to."""
    check_process_key(key, origin)
    config = _validate_process(_parse_yaml(text, origin), origin)
    if config.name != key:
        raise ConfigError(
            origin,
            f"Process name {config.name!r} does not match its file {process_file_name(key)}.",
        )
    return config


def parse_agent(text: str, *, origin: str) -> AgentManifest:
    """Validate `agent.yaml` text.

    Does not check that declared prompt and skill paths resolve — there is no filesystem behind
    repo-backed config; the resolver fetches each file at the pinned sha and a missing one
    surfaces there as a real 404.
    """
    return _validate_agent(_parse_yaml(text, origin), origin)


def parse_mcp_server(text: str, *, name: str, origin: str) -> McpServerManifest:
    """Validate one `mcp/<name>.yaml`. `name` is the file stem and must equal the manifest's
    `name:` — an agent references a server by that name, so a mismatch resolves nowhere."""
    manifest = _validate_mcp_server(_parse_yaml(text, origin), origin)
    if manifest.name != name:
        raise ConfigError(
            origin,
            f"MCP server name {manifest.name!r} does not match its file {name}{MCP_SUFFIX}.",
        )
    return manifest


# --- disk loading -------------------------------------------------------------------------------


def load_process(path: Path) -> ProcessConfig:
    return _validate_process(_read_yaml(path), path)


def load_processes(root: Path) -> dict[str, ProcessConfig]:
    """Every `<root>/processes/process-<key>.yaml`, by key. No folder means none, not an error —
    a repo on the legacy layout has its one process at the root instead.

    Anything else in the folder is refused rather than ignored, the same "unknown keys are fatal"
    posture the rest of `.factorial/` takes: a lane an operator believes they added, silently
    skipped because of a typo in its filename, is exactly the failure the strictness prevents.
    """
    folder = root / PROCESSES_DIR
    if not folder.is_dir():
        return {}
    processes: dict[str, ProcessConfig] = {}
    for entry in sorted(folder.iterdir()):
        name = entry.name
        matches = name.startswith(PROCESS_PREFIX) and name.endswith(PROCESS_SUFFIX)
        if not (entry.is_file() and matches):
            raise ConfigError(
                entry,
                f"{PROCESSES_DIR}/ holds one process per file, named "
                f"{PROCESS_PREFIX}<name>{PROCESS_SUFFIX}, and {name!r} is not one.",
            )
        key = name.removeprefix(PROCESS_PREFIX).removesuffix(PROCESS_SUFFIX)
        processes[key] = parse_named_process(_read_text(entry), key=key, origin=str(entry))
    return processes


def load_agent(path: Path) -> AgentManifest:
    """Parse and validate an `agents/<name>/agent.yaml`, checking its declared paths exist."""
    manifest = _validate_agent(_read_yaml(path), path)

    folder = path.parent
    for label, prompt in (
        ("prompts.system", manifest.prompts.system),
        ("prompts.instructions", manifest.prompts.instructions),
    ):
        if not (folder / prompt).is_file():
            raise ConfigError(path, f"{label} points at missing file {prompt!r}.")
    for skill in manifest.skills.local:
        if not (folder / SKILLS_DIR / skill / SKILL_FILE).is_file():
            raise ConfigError(
                path, f"Local skill {skill!r} has no {SKILLS_DIR}/{skill}/{SKILL_FILE}."
            )
    for server in manifest.mcp.local:
        # Parsed, not merely existence-checked: an unparseable server must fail here, not at a
        # spawn an hour later.
        load_mcp_server(folder / MCP_DIR / f"{server}{MCP_SUFFIX}")
    return manifest


def load_mcp_server(path: Path) -> McpServerManifest:
    """Parse and validate one `mcp/<name>.yaml` on disk; the file stem is the server name."""
    return parse_mcp_server(
        _read_text(path), name=path.name.removesuffix(MCP_SUFFIX), origin=str(path)
    )


def load_shared_mcp_servers(root: Path) -> dict[str, McpServerManifest]:
    """Every `<root>/mcp/*.yaml`, by name. A missing folder means no servers, not an error."""
    mcp_dir = root / MCP_DIR
    if not mcp_dir.is_dir():
        return {}
    servers: dict[str, McpServerManifest] = {}
    for file in sorted(mcp_dir.glob(f"*{MCP_SUFFIX}")):
        manifest = load_mcp_server(file)
        servers[manifest.name] = manifest
    return servers


def load_config_folder(root: Path) -> AdlcConfig:
    """Load every process lane plus every `<root>/agents/<name>/agent.yaml`.

    Both layouts are read: `<root>/processes/process-<key>.yaml` is what the tooling writes now,
    and the legacy `<root>/process.yaml` keeps working for ever, under whatever `name:` it
    carries. A repo holding BOTH is refused, naming the file to delete — silently preferring one
    would leave an operator editing a file nothing reads.
    """
    processes = load_processes(root)
    legacy = root / PROCESS_FILE
    if processes:
        if legacy.is_file():
            raise ConfigError(
                legacy,
                f"This repository has both layouts: {PROCESSES_DIR}/ holds "
                f"{', '.join(sorted(processes))}, and {PROCESS_FILE} is still here. Delete "
                f"{PROCESS_FILE}. The {PROCESSES_DIR}/ folder replaces it.",
            )
    else:
        one = load_process(legacy)
        processes = {one.name: one}
    mcp_servers = load_shared_mcp_servers(root)

    agents: dict[str, AgentManifest] = {}
    agents_dir = root / AGENTS_DIR
    folders = sorted(p for p in agents_dir.iterdir() if p.is_dir()) if agents_dir.is_dir() else []
    for folder in folders:
        manifest_path = folder / AGENT_FILE
        if not manifest_path.is_file():
            continue  # a folder without a manifest is not an agent (e.g. scratch dirs)
        manifest = load_agent(manifest_path)
        # The folder name is the registry name that `process.yaml` steps reference.
        if manifest.name != folder.name:
            raise ConfigError(
                manifest_path,
                f"Manifest name {manifest.name!r} does not match its folder {folder.name!r}.",
            )
        for skill in manifest.skills.shared:
            if not (root / SKILLS_DIR / skill / SKILL_FILE).is_file():
                raise ConfigError(
                    manifest_path,
                    f"Shared skill {skill!r} has no {SKILLS_DIR}/{skill}/{SKILL_FILE} at the "
                    f"{root.name} root.",
                )
        for server in manifest.mcp.shared:
            if server not in mcp_servers:
                raise ConfigError(
                    manifest_path,
                    f"Shared MCP server {server!r} has no {MCP_DIR}/{server}{MCP_SUFFIX} at the "
                    f"{root.name} root.",
                )
        agents[manifest.name] = manifest

    for key, process in sorted(processes.items()):
        missing = [
            f"{step.name} (agent {step.agent})"
            for step in process.steps
            if isinstance(step, AgentStep) and step.agent not in agents
        ]
        if missing:
            raise ConfigError(
                root / PROCESSES_DIR / process_file_name(key)
                if (root / PROCESSES_DIR).is_dir()
                else root / PROCESS_FILE,
                f"Step(s) reference agents with no manifest under {AGENTS_DIR}/: "
                + ", ".join(missing)
                + ".",
            )

    return AdlcConfig(processes=processes, agents=agents, mcp_servers=mcp_servers)
