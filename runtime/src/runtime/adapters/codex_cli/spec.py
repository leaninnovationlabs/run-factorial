"""RunSpec shaping: the prompt and `config.toml` documents, and the spawn-time refusals of
specs this adapter cannot honour."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters._cli.environment import (
    attachment_lines,
    config_lines,
    files_lines,
    refuse_model_alias,
    refuse_unverifiable_expectations,
)
from runtime.adapters.codex_cli.cli import (
    CONFIG_APPROVAL_POLICY,
    CONFIG_EXCLUDE_SLASH_TMP,
    CONFIG_EXCLUDE_TMPDIR,
    CONFIG_NETWORK_ACCESS,
    CONFIG_PROJECTS_TABLE,
    CONFIG_SANDBOX_MODE,
    CONFIG_TRUST_LEVEL,
    CONFIG_WEB_SEARCH,
    CONFIG_WORKSPACE_TABLE,
    CONFIG_WRITABLE_ROOTS,
    VALUE_APPROVAL_NEVER,
    VALUE_SANDBOX_WORKSPACE,
    VALUE_TRUST_UNTRUSTED,
    VALUE_WEB_SEARCH_DISABLED,
)
from runtime.contract import (
    McpServerSpec,
    McpTransport,
    RunSpec,
    SpawnError,
    SpawnReason,
)

#: The one HTTP auth shape codex 0.146.0 offers a hook for: `bearer_token_env_var` in
#: `config.toml`, read from the child's environment. Anything else in `headers` is refused.
_BEARER_HEADER: Final = "authorization"
_BEARER_PREFIX: Final = "Bearer "


#: The table every declared server renders under in `config.toml`.
MCP_TABLE: Final = "mcp_servers"


def bearer_env_var(server_name: str) -> str:
    """The environment variable one server's bearer token travels through — derived, so the
    TOML and the injected environment can never disagree on the name."""
    slug = re.sub(r"[^A-Za-z0-9]", "_", server_name).upper()
    return f"FACTORIAL_MCP_{slug}_BEARER"


def _toml_string(value: str) -> str:
    # A JSON string is a TOML basic string in all but two cases: TOML forbids a raw DEL, which
    # JSON leaves unescaped, and rejects the surrogate-pair escapes ASCII-only JSON gives a
    # character beyond the BMP.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _server_lines(
    server: McpServerSpec, secrets: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    lines = [f"[{MCP_TABLE}.{_toml_string(server.name)}]"]
    extra_env: dict[str, str] = {}
    if server.transport is McpTransport.STDIO:
        lines.append(f"command = {_toml_string(server.command or '')}")
        if server.args:
            rendered_args = ", ".join(_toml_string(arg) for arg in server.args)
            lines.append(f"args = [{rendered_args}]")
        env = server.rendered_env(secrets)
        if env:
            lines.append(f"[{MCP_TABLE}.{_toml_string(server.name)}.env]")
            lines += [f"{_toml_string(key)} = {_toml_string(value)}" for key, value in env.items()]
        return lines, extra_env

    if server.transport is not McpTransport.HTTP:
        raise SpawnError(
            f"MCP server {server.name!r} uses {server.transport.value} transport, which the "
            "pinned codex CLI does not offer: it supports only streamable HTTP for remote "
            "servers. Move the server to `http`, or run this agent on a runtime that supports it",
            # CONFIG: declared config this substrate cannot honour; a retry reads the same file.
            reason=SpawnReason.CONFIG,
        )

    lines.append(f"url = {_toml_string(server.url or '')}")
    headers = server.rendered_headers(secrets)
    for key, value in headers.items():
        if key.casefold() != _BEARER_HEADER or not value.startswith(_BEARER_PREFIX):
            raise SpawnError(
                f"MCP server {server.name!r} declares header {key!r}, but the pinned codex CLI "
                "authenticates a streamable-HTTP server only through `bearer_token_env_var`. The "
                "only header it can send is `Authorization: Bearer <token>`. Factorial doesn't "
                "drop the header, because the server would then run unauthenticated",
                reason=SpawnReason.CONFIG,
            )
        var = bearer_env_var(server.name)
        lines.append(f"bearer_token_env_var = {_toml_string(var)}")
        # The token travels through the environment, never through the TOML on disk.
        extra_env[var] = value.removeprefix(_BEARER_PREFIX)
    return lines, extra_env


def mcp_tables(
    servers: Sequence[McpServerSpec], secrets: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """The `[mcp_servers.*]` tables, plus the env vars the child must carry for them (bearer
    tokens, by indirection).

    `{secrets.KEY}` references are rendered against the same secrets the child process is given,
    and one nobody injected raises `SpawnError` before the file exists (contract.McpServerSpec).
    `tools_allow` narrowing is NOT enforced: 0.146.0 exposes no observed per-tool config surface,
    and the contract makes narrowing best-effort per substrate — server-level containment (only
    the servers in this file exist for the run) is what this adapter gives you.

    """
    lines: list[str] = []
    extra_env: dict[str, str] = {}
    for server in servers:
        server_lines, server_env = _server_lines(server, secrets)
        lines += [*server_lines, ""]
        extra_env |= server_env
    return lines, extra_env


def bedrock_provider_lines(
    *,
    provider: str,
    base_url: str,
    env_key: str,
    name: str = "Bedrock Grok 4.6 GovCloud",
) -> list[str]:
    """The custom provider block Codex needs to call GovCloud Bedrock `/openai/v1`."""
    return [
        f"model_provider = {_toml_string(provider)}",
        "",
        f"[model_providers.{provider}]",
        f"name = {_toml_string(name)}",
        f"base_url = {_toml_string(base_url)}",
        f"env_key = {_toml_string(env_key)}",
        f"wire_api = {_toml_string('responses')}",
        "supports_websockets = false",
        "",
    ]


def build_config_toml(
    servers: Sequence[McpServerSpec],
    secrets: Mapping[str, str],
    *,
    writable_root: Path,
    worktree: Path,
    bedrock: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """The per-run `$CODEX_HOME/config.toml` text, plus the env vars `mcp_tables` names.

    Always written, even with no MCP server declared: this file is the run's CONTAINMENT, the
    policy every TUI launch also passes as `-s`. `worktree`, the checkout's real path, is seeded
    `untrusted` (cli.py). `bedrock` adds the GovCloud provider table when this run is
    `codex_bedrock`; other modes omit it so OpenAI/Claude paths stay byte-for-byte.
    """
    lines: list[str] = []
    if bedrock:
        # Root keys must precede `[model_providers.*]` or TOML nests them in that table.
        lines += [
            f"{CONFIG_WEB_SEARCH} = {_toml_string(VALUE_WEB_SEARCH_DISABLED)}",
            "",
        ]
        lines += bedrock_provider_lines(
            provider=bedrock["provider"],
            base_url=bedrock["base_url"],
            env_key=bedrock["env_key"],
            name=bedrock.get("name", "Bedrock Grok 4.6 GovCloud"),
        )
    lines += [
        f"{CONFIG_SANDBOX_MODE} = {_toml_string(VALUE_SANDBOX_WORKSPACE)}",
        f"{CONFIG_APPROVAL_POLICY} = {_toml_string(VALUE_APPROVAL_NEVER)}",
        "",
        f"[{CONFIG_WORKSPACE_TABLE}]",
        f"{CONFIG_NETWORK_ACCESS} = true",
        f"{CONFIG_WRITABLE_ROOTS} = [{_toml_string(str(writable_root))}]",
        f"{CONFIG_EXCLUDE_TMPDIR} = true",
        f"{CONFIG_EXCLUDE_SLASH_TMP} = true",
        "",
        f"[{CONFIG_PROJECTS_TABLE}.{_toml_string(str(worktree))}]",
        f"{CONFIG_TRUST_LEVEL} = {_toml_string(VALUE_TRUST_UNTRUSTED)}",
        "",
    ]
    tables, extra_env = mcp_tables(servers, secrets)
    return "\n".join(lines + tables), extra_env


def build_prompt(spec: RunSpec, attachment_paths: Mapping[str, str] | None = None) -> str:
    """The first turn's document: the workspace, instructions, fenced inputs, and where each
    attached image was saved.

    Codex has no system-prompt flag (0.146.0), so the TUI takes the system prompt as config
    (`developer_instructions`). The workspace and files sections are adapter translation, not
    prompt authorship: the CLI has no `workspace` concept, so the branch the orchestrator will
    verify must reach the agent as text. Nothing about labels, promotion or merging appears here
    — that is the orchestrator's.
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
    refuse_unverifiable_expectations("CodexCliAdapter", spec)


def _reject_model_alias(model: str) -> None:
    refuse_model_alias(model, "gpt-5.1-codex")
