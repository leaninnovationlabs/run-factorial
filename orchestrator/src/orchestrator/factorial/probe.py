"""Which referenced agents cannot produce a runnable definition at a given sha.

`registry.upsert_agent_rows` creates an `agent` row for every agent a process names without ever
reading the manifest, so a 201 from `POST /process-versions/sync` proves the process parsed and
nothing more — the break surfaces an hour later at the first spawn. This is the read that
notices, and both `sync` and the readiness ledger use it so they cannot disagree.

Absent and present-but-unparseable are deliberately one verdict. Scaffolding writes a
comments-only `agent.yaml` for an agent the factory has no template for, and an unfilled stub is
the likelier of the two failures precisely because the file's existence looks like completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from orchestrator.config import AgentManifest, AgentStep, ConfigError, ProcessConfig, parse_agent
from orchestrator.config.loader import AGENT_FILE, AGENTS_DIR
from orchestrator.config.scaffold import CONFIG_DIR
from orchestrator.config.scaffold.catalog import Forge
from orchestrator.config.scaffold.propose import propose

if TYPE_CHECKING:
    from orchestrator.factorial.source import RepoConfigSource

__all__ = [
    "AgentGap",
    "AgentResolution",
    "agent_gaps",
    "agent_names",
    "catalog_agents",
    "resolve_agents",
]

#: These lines land in a readiness `detail` and in a sync response, next to other prose. A
#: manifest's `ValidationError` runs to thousands of characters, which would bury them.
_MAX_REASON = 200


@dataclass(frozen=True, slots=True)
class AgentGap:
    """A referenced agent with no runnable definition, and the first line of why."""

    name: str
    reason: str

    def line(self) -> str:
        return f"{self.name}: {self.reason}"


class _Source(Protocol):
    def agent_manifest_path(self, name: str) -> str: ...

    async def load_agent(self, repo: str, manifest_path: str, sha: str) -> object: ...


@dataclass(frozen=True, slots=True)
class AgentResolution:
    """A referenced agent's manifest at a sha, or, when it has none, the first line of why."""

    name: str
    manifest: AgentManifest | None
    reason: str | None = None


def agent_names(config: ProcessConfig) -> list[str]:
    return sorted({step.agent for step in config.steps if isinstance(step, AgentStep)})


def _brief(exc: Exception) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:_MAX_REASON]


async def agent_gaps(source: _Source, repo: str, sha: str, config: ProcessConfig) -> list[AgentGap]:
    """One entry per referenced agent that does not resolve at `sha`; empty means all sound.

    Structured rather than preformatted because the two readers want different things from it:
    a sync response prints every gap as a line, while the config explorer hangs each one on the
    manifest node it belongs to and never shows the others.
    """
    gaps: list[AgentGap] = []
    for name in agent_names(config):
        try:
            await source.load_agent(repo, source.agent_manifest_path(name), sha)
        except Exception as exc:  # absent, unparseable or unreadable — all the same verdict
            gaps.append(AgentGap(name=name, reason=_brief(exc)))
    return gaps


async def resolve_agents(
    source: RepoConfigSource, repo: str, sha: str, config: ProcessConfig
) -> list[AgentResolution]:
    """Every referenced agent, with its manifest when it resolves at `sha`.

    The same read and verdict as `agent_gaps`, so the process-config section cannot disagree with
    the warning the explorer hangs on a manifest.
    """
    resolved: list[AgentResolution] = []
    for name in agent_names(config):
        try:
            agent = await source.load_agent(repo, source.agent_manifest_path(name), sha)
        except Exception as exc:
            resolved.append(AgentResolution(name=name, manifest=None, reason=_brief(exc)))
        else:
            resolved.append(AgentResolution(name=name, manifest=agent.manifest))
    return resolved


def catalog_agents(config: ProcessConfig, *, forge: Forge = "github") -> list[AgentResolution]:
    """`resolve_agents`' verdict read from the catalog instead of a repo: the manifest a first
    commit of `config` would write for each agent it names on a `forge` project. Reads only
    package data."""
    files = propose(config, forge=forge).files
    resolved: list[AgentResolution] = []
    for name in agent_names(config):
        proposed = files.get(f"{CONFIG_DIR}/{AGENTS_DIR}/{name}/{AGENT_FILE}")
        if proposed is None:
            resolved.append(AgentResolution(name=name, manifest=None, reason="not in the catalog"))
            continue
        try:
            manifest = parse_agent(proposed.body, origin=f"catalog {name}")
        except ConfigError as exc:
            resolved.append(AgentResolution(name=name, manifest=None, reason=exc.detail))
        else:
            resolved.append(AgentResolution(name=name, manifest=manifest))
    return resolved
