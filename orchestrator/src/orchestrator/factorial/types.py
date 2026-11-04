"""What a resolved agent definition is, once its repo files have actually been read.

`AgentManifest` names files; these carry their contents, fetched at one concrete commit sha, so
a pin governs behaviour rather than a YAML file (`agent.pinned_commit_sha`).

Not `runtime.contract`'s `AgentPrompts` / `AgentSkill` despite the close shapes: this layer must
not depend on the runtime contract. The worker owns the mapping, which is the seam that lets a
second substrate land without touching the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config import AgentManifest, McpServerManifest

__all__ = ["ResolvedAgent", "ResolvedMcpServer", "ResolvedSkill"]


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    name: str
    content: str  # the SKILL.md text, frontmatter included
    scope: str  # "local" (this agent's skills/) | "shared" (the .factorial-level skills/)


@dataclass(frozen=True, slots=True)
class ResolvedMcpServer:
    """One `mcp/<name>.yaml`, parsed at the agent's pinned sha.

    Structure rather than text, so the parsed manifest travels whole: the worker translates it
    onto the runtime contract, after the project's operator allowlist has cleared it (§7.5).
    """

    name: str
    manifest: McpServerManifest
    scope: str  # "local" (this agent's mcp/) | "shared" (the .factorial-level mcp/)


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    name: str
    repo: str
    #: Always a concrete 40-char sha — never a branch. Two runs of one pin read identical bytes.
    commit_sha: str
    manifest: AgentManifest
    system_prompt: str
    instructions: str
    skills: tuple[ResolvedSkill, ...]
    mcp_servers: tuple[ResolvedMcpServer, ...] = ()

    def secret_keys(self) -> tuple[str, ...]:
        """Every secret key this run must resolve: the agent's own declarations plus the ones its
        MCP servers reference through `{secrets.KEY}`. First-seen order, deduplicated.

        The adapter, not the orchestrator, renders MCP header and env values, so a key nobody
        resolved would reach the substrate as a literal `{secrets.X}`.
        """
        seen: dict[str, None] = {}
        for decl in self.manifest.secrets:
            seen.setdefault(decl.key, None)
        for server in self.mcp_servers:
            for key in server.manifest.secret_refs():
                seen.setdefault(key, None)
        return tuple(seen)

    def env_secret_keys(self) -> tuple[str, ...]:
        """The declared keys the agent reads itself. A key an MCP server references reaches the
        run as that server's header, so a sandbox needs no other way to deliver it."""
        referenced = {key for server in self.mcp_servers for key in server.manifest.secret_refs()}
        return tuple(decl.key for decl in self.manifest.secrets if decl.key not in referenced)
