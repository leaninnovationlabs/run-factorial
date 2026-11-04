"""RunSpec shaping: the prompt and MCP-config documents, and the spawn-time refusals of specs
this adapter cannot honour."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from runtime.adapters._cli.environment import (
    attachment_lines,
    config_lines,
    files_lines,
    refuse_model_alias,
    refuse_unverifiable_expectations,
)
from runtime.adapters.claude_cli.cli import (
    MCP_KEY_ARGS,
    MCP_KEY_COMMAND,
    MCP_KEY_ENV,
    MCP_KEY_HEADERS,
    MCP_KEY_TYPE,
    MCP_KEY_URL,
    MCP_ROOT_KEY,
)
from runtime.contract import (
    McpServerSpec,
    McpTransport,
    RunSpec,
)


def build_mcp_config(
    servers: Sequence[McpServerSpec], secrets: Mapping[str, str]
) -> dict[str, Any]:
    """`RunSpec.mcp` -> the CLI's `--mcp-config` document.

    `{secrets.KEY}` references are rendered against the same secrets the child process is given,
    and one nobody injected raises `SpawnError` before the file exists (contract.McpServerSpec).
    """
    entries: dict[str, Any] = {}
    for server in servers:
        if server.transport is McpTransport.STDIO:
            entry: dict[str, Any] = {MCP_KEY_COMMAND: server.command}
            if server.args:
                entry[MCP_KEY_ARGS] = list(server.args)
            env = server.rendered_env(secrets)
            if env:
                entry[MCP_KEY_ENV] = env
        else:
            entry = {MCP_KEY_TYPE: server.transport.value, MCP_KEY_URL: server.url}
            headers = server.rendered_headers(secrets)
            if headers:
                entry[MCP_KEY_HEADERS] = headers
        entries[server.name] = entry
    return {MCP_ROOT_KEY: entries}


def mcp_tool_patterns(spec: RunSpec) -> tuple[str, ...]:
    """`--allowed-tools` entries for the declared servers: `mcp__<server>__<tool-pattern>`.

    A server with no `tools_allow` yields the bare `mcp__<server>`, which is how the CLI spells
    "every tool this server exposes". See `FLAG_ALLOWED_TOOLS` for how much this actually binds.
    """
    patterns: list[str] = []
    for server in spec.mcp:
        if server.tools_allow:
            patterns += [f"mcp__{server.name}__{tool}" for tool in server.tools_allow]
        else:
            patterns.append(f"mcp__{server.name}")
    return tuple(patterns)


def build_prompt(spec: RunSpec, attachment_paths: Mapping[str, str] | None = None) -> str:
    """The first turn's text: the manifest's instructions, the resolved inputs, the workspace,
    and where each attached image was saved (`attachment_paths`, name -> path).

    The workspace, files and attachment sections are adapter translation, not prompt authorship:
    the CLI has no `workspace` concept, so the branch the orchestrator will verify must reach the
    agent as text. Nothing about labels, promotion or merging appears here — that is the
    orchestrator's.
    """
    sections = [
        "# Workspace",
        f"- repo: {spec.workspace.repo}",
        f"- base branch: {spec.workspace.base}",
        f"- branch to push: {spec.workspace.branch}",
        *config_lines(spec.workspace),
        "",
        "`{slug}` in the branch name is yours to choose; keep the rest exactly as written. You "
        "are in a checkout of the repo at the base branch. Push the branch and open the PR "
        "yourself (`gh` is authenticated).",
    ]
    sections += files_lines(spec)
    if spec.prompts.instructions.strip():
        sections += ["", "# Instructions", spec.prompts.instructions.strip()]
    if spec.inputs:
        sections += ["", "# Inputs"]
        for item in spec.inputs:
            # Fenced, because an input is untrusted text (a ticket body) and must read as data.
            sections += [f"## {item.name}", "```", item.value, "```"]
    sections += attachment_lines(spec.attachments, spec.missing_attachments, attachment_paths or {})
    return "\n".join(sections)


def _reject_unverifiable_expectations(spec: RunSpec) -> None:
    refuse_unverifiable_expectations("ClaudeCliAdapter", spec)


def _reject_model_alias(model: str) -> None:
    refuse_model_alias(model, "claude-haiku-4-5")
