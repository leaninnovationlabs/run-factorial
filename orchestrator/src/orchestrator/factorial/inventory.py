"""The `.factorial/` folder as the configuration explorer shows it: every file at a pinned sha,
what each one is, whether the repo has since moved it, and which manifests do not resolve.

The pinned sha is the subject, because the pin is what tickets actually run (design.md §2.8).
The config branch's head is read only to answer "has this moved since", and that answer degrades to
`unknown` rather than failing the listing — an explorer that will not open because GitHub is
slow is worse than one that cannot say whether a file drifted.

A file the head has but the pin does not is still listed, marked `added`. Hiding it would be its
own confusion: an operator who just committed config would find the file they wrote missing,
with nothing on screen to explain that they have not synced a version yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from orchestrator.config import ProcessConfig
from orchestrator.config.loader import (
    AGENT_FILE,
    AGENTS_DIR,
    MCP_DIR,
    MCP_SUFFIX,
    PROCESS_FILE,
    PROCESS_PREFIX,
    PROCESS_SUFFIX,
    PROCESSES_DIR,
)
from orchestrator.config.loader import SKILLS_DIR as SKILL_DIR
from orchestrator.factorial.probe import agent_gaps
from orchestrator.factorial.source import RepoConfigSource

__all__ = ["ConfigFile", "ConfigInventory", "FileKind", "config_inventory"]

#: `same` and `differs` are only ever asserted from two blob shas actually compared; everything
#: the head could not be read for is `unknown`, never an optimistic `same`.
Drift = Literal["same", "differs", "added", "removed", "unknown"]

#: What the explorer draws a file as. Derived from position alone (`classify`), so a folder the
#: factory does not know about still lists — as `other` — rather than disappearing.
FileKind = Literal["process", "agent", "prompt", "skill", "mcp", "other"]

_AGENT_MANIFEST: Final = re.compile(rf"^[^/]+/{AGENTS_DIR}/(?P<name>[^/]+)/{AGENT_FILE}$")


@dataclass(frozen=True, slots=True)
class ConfigFile:
    path: str
    kind: FileKind
    drift: Drift
    #: Why this file cannot be used, if it cannot. Only agent manifests carry one today.
    warning: str | None = None
    #: The blob at the pinned commit; `None` for a file only the head has.
    sha: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigInventory:
    config_dir: str
    pinned_sha: str
    #: `None` when the head could not be read; every file's drift is then `unknown`.
    head_sha: str | None
    files: tuple[ConfigFile, ...]


def classify(path: str, *, root: str) -> FileKind:
    """What kind of config file a path is, from its position under the config folder alone."""
    rest = path[len(root) + 1 :] if path.startswith(f"{root}/") else path
    if rest == PROCESS_FILE:
        return "process"
    segments = rest.split("/")
    if (
        len(segments) == 2
        and segments[0] == PROCESSES_DIR
        and segments[1].startswith(PROCESS_PREFIX)
        and segments[1].endswith(PROCESS_SUFFIX)
    ):
        return "process"
    if segments[0] == AGENTS_DIR:
        if len(segments) == 3 and segments[2] == AGENT_FILE:
            return "agent"
        # Everything else an agent folder holds is prose the agent reads: both prompts, and any
        # skill or MCP file it keeps locally.
        return "skill" if SKILL_DIR in segments else "mcp" if MCP_DIR in segments else "prompt"
    if segments[0] == SKILL_DIR:
        return "skill"
    if segments[0] == MCP_DIR and rest.endswith(MCP_SUFFIX):
        return "mcp"
    return "other"


async def config_inventory(
    source: RepoConfigSource,
    repo: str,
    *,
    pinned_sha: str,
    config: ProcessConfig | None,
) -> ConfigInventory:
    """List the config folder at `pinned_sha`, with drift against the `factorial` head and
    per-manifest warnings for the agents `config` references.

    `config` is the pinned snapshot's parse. Without it the agent warnings are omitted rather
    than guessed: which agents matter is a property of the process, and a manifest nothing
    references is not a gap.
    """
    root = await source.resolve_dir(repo, pinned_sha)
    pinned = {entry.path: entry.sha for entry in await source.list_files(repo, pinned_sha)}

    head_sha, head = await _head(source, repo, pinned_sha=pinned_sha, pinned=pinned)
    warnings = await _manifest_warnings(source, repo, sha=pinned_sha, config=config)

    files: list[ConfigFile] = []
    for path in sorted(set(pinned) | set(head or {})):
        name = _agent_name(path)
        files.append(
            ConfigFile(
                path=path,
                kind=classify(path, root=root),
                drift=_drift(path, pinned=pinned, head=head),
                # Only against the pinned listing: a manifest the head added has not been probed,
                # and reporting the pinned agent's gap on it would blame the wrong file.
                warning=warnings.get(name) if name and path in pinned else None,
                sha=pinned.get(path),
            )
        )
    return ConfigInventory(
        config_dir=root, pinned_sha=pinned_sha, head_sha=head_sha, files=tuple(files)
    )


async def _head(
    source: RepoConfigSource, repo: str, *, pinned_sha: str, pinned: dict[str, str]
) -> tuple[str | None, dict[str, str] | None]:
    """`(head sha, its files)`, or `(None, None)` when the head cannot be read.

    A head that resolves to the pinned sha needs no second listing: the commit is the same one,
    so its own listing is the answer already in hand.
    """
    try:
        head_sha = await source.resolve_ref(repo, None)
        if head_sha == pinned_sha:
            return head_sha, pinned
        return head_sha, {e.path: e.sha for e in await source.list_files(repo, head_sha)}
    except Exception:  # truncated tree, outage, deleted branch — the listing still stands
        return None, None


def _drift(path: str, *, pinned: dict[str, str], head: dict[str, str] | None) -> Drift:
    if head is None:
        return "unknown"
    if path not in head:
        return "removed"
    if path not in pinned:
        return "added"
    return "same" if pinned[path] == head[path] else "differs"


def _agent_name(path: str) -> str | None:
    """The agent a manifest path belongs to, or `None` if the path is not a manifest."""
    match = _AGENT_MANIFEST.match(path)
    return match.group("name") if match else None


async def _manifest_warnings(
    source: RepoConfigSource, repo: str, *, sha: str, config: ProcessConfig | None
) -> dict[str, str]:
    """Agent name -> why its definition does not resolve at `sha`.

    Degrades to no warnings on a GitHub failure: this is a probe, and a probe that cannot
    read must not turn into an assertion that something is broken.
    """
    if config is None:
        return {}
    try:
        return {gap.name: gap.reason for gap in await agent_gaps(source, repo, sha, config)}
    except Exception:
        return {}
