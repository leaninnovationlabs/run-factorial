"""Per-turn Codex config for the in-app assistant — containment plus one MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from runtime.adapters.codex_cli.auth import AUTH_MODE_BEDROCK, bedrock_provider_config
from runtime.adapters.codex_cli.cli import (
    CONFIG_APPROVAL_POLICY,
    CONFIG_EXCLUDE_SLASH_TMP,
    CONFIG_EXCLUDE_TMPDIR,
    CONFIG_FEATURES,
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
from runtime.adapters.codex_cli.spec import MCP_TABLE, _toml_string, bedrock_provider_lines

__all__ = ["ENV_TOOL_SOCKET", "SERVER_NAME", "TOOLS_OFF", "build_assistant_config"]

SERVER_NAME: Final = "factorial"
ENV_TOOL_SOCKET: Final = "FACTORIAL_ASSISTANT_TOOL_SOCKET"
CONFIG_TOOLS_APPROVAL: Final = "default_tools_approval_mode"
VALUE_TOOLS_APPROVE: Final = "approve"
CONFIG_REQUIRED: Final = "required"
#: `shell_tool` alone leaves no command tool in 0.164.0; `unified_exec` is off too, so a release
#: that stops reading it as a sub-switch still offers none.
TOOLS_OFF: Final = ("shell_tool", "unified_exec", "view_image", "multi_agent")


def build_assistant_config(
    *,
    worktree: Path,
    writable_root: Path,
    mcp_command: tuple[str, ...],
    tool_socket: Path,
    auth_mode: str,
    bedrock_region: str | None,
    bedrock_base_url: str | None = None,
) -> tuple[str, dict[str, str]]:
    """`config.toml` text and extra env for the Codex child.

    `bedrock_base_url` is where Codex sends its model requests in place of Bedrock's own
    address: the turn's relay (`relay.py`).

    The model gets the factorial MCP server and no tool of Codex's own that reaches the machine:
    no shell, no image reader, no sub-agents, no web search. The child runs as the assistant's
    user, who can read the API token and the database URL (design.md §7.18), and a sandbox is
    no answer where none can start, as on Fargate. Each name is a 0.164.0 `[features]` key;
    `codex features list` shows them. The event mapper's refusal of a command stays behind it.
    """
    lines = [
        f"{CONFIG_SANDBOX_MODE} = {_toml_string(VALUE_SANDBOX_WORKSPACE)}",
        f"{CONFIG_APPROVAL_POLICY} = {_toml_string(VALUE_APPROVAL_NEVER)}",
        f"{CONFIG_WEB_SEARCH} = {_toml_string(VALUE_WEB_SEARCH_DISABLED)}",
        "",
    ]
    if auth_mode == AUTH_MODE_BEDROCK:
        bedrock = bedrock_provider_config(bedrock_region)
        lines += bedrock_provider_lines(
            provider=bedrock["provider"],
            base_url=bedrock_base_url or bedrock["base_url"],
            env_key=bedrock["env_key"],
            name=bedrock.get("name", "Bedrock Grok 4.6"),
        )
    lines += [
        f"[{CONFIG_FEATURES}]",
        *(f"{feature} = false" for feature in TOOLS_OFF),
        "",
        f"[{CONFIG_WORKSPACE_TABLE}]",
        f"{CONFIG_NETWORK_ACCESS} = false",
        f"{CONFIG_WRITABLE_ROOTS} = [{_toml_string(str(writable_root))}]",
        f"{CONFIG_EXCLUDE_TMPDIR} = true",
        f"{CONFIG_EXCLUDE_SLASH_TMP} = true",
        "",
        f"[{CONFIG_PROJECTS_TABLE}.{_toml_string(str(worktree))}]",
        f"{CONFIG_TRUST_LEVEL} = {_toml_string(VALUE_TRUST_UNTRUSTED)}",
        "",
    ]
    mcp_lines = _mcp_server_lines(mcp_command, tool_socket)
    return "\n".join(lines + mcp_lines), {}


def _mcp_server_lines(command: tuple[str, ...], tool_socket: Path) -> list[str]:
    if not command:
        raise ValueError("mcp_command is required for the assistant Codex backend")
    binary, *args = command
    table = f"{MCP_TABLE}.{_toml_string(SERVER_NAME)}"
    lines = [f"[{table}]", f"command = {_toml_string(binary)}"]
    if args:
        rendered = ", ".join(_toml_string(arg) for arg in args)
        lines.append(f"args = [{rendered}]")
    # Codex asks to approve a tool not annotated read-only, and `approval_policy = never` turns
    # that ask into a refusal. Every factorial tool is a read or a proposal the person decides,
    # as the Claude path's `allowed_tools` already says.
    lines.append(f"{CONFIG_TOOLS_APPROVAL} = {_toml_string(VALUE_TOOLS_APPROVE)}")
    # Not required, a server gets one second to start before Codex lists the model's tools
    # without it. Required, Codex waits for it, and fails the turn if it never starts.
    lines.append(f"{CONFIG_REQUIRED} = true")
    # Codex hands a stdio server a few of its own variables (HOME, PATH and the like) and this
    # table, never the rest of its env: a socket path set only on the Codex child is lost.
    lines += [
        "",
        f"[{table}.env]",
        f"{_toml_string(ENV_TOOL_SOCKET)} = {_toml_string(str(tool_socket))}",
        "",
    ]
    return lines
