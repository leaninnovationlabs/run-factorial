"""The `ClaudeAgentOptions` for one chat turn: every capability off except the turn's own tools
(tech-stack.md decision 63).

- `tools=[]` removes every built-in tool: no shell, files, web or subagents.
- `setting_sources=[]` loads no CLAUDE.md and no user or project settings.
- `strict_mcp_config` makes the in-process `factorial` server the whole server list: no
  `.mcp.json`, user, plugin or claude.ai connector servers.
- `allowed_tools` with `permission_mode="dontAsk"` denies anything outside that server without a
  prompt nobody could answer.
- The env flags turn off auto memory, CLAUDE.md discovery, nonessential traffic (telemetry,
  error reports, update checks), the auto-updater and tool search (its placeholder tool would
  join the turn's tools). The local transcript stays on: the SDK's session store mirrors it, and
  it lands in the per-turn config dir, which the backend deletes after the turn.

Pure, so a test reads every field.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import (
    ClaudeAgentOptions,
    McpSdkServerConfig,
    SdkMcpTool,
    ToolAnnotations,
    create_sdk_mcp_server,
)

from runtime.adapters._claude.credentials import credential_environment
from runtime.adapters._claude.flags import ENV_NONESSENTIAL, ENV_TOOL_SEARCH
from runtime.assistant.adapters.claude_sdk.launcher import ENV_AUTH_MODE, ENV_REAL_CLI
from runtime.assistant.contract import ChatToolbox, ChatTurnSpec

__all__ = [
    "SERVER",
    "build_options",
    "sdk_tools",
    "tool_server",
]

#: The in-process server's name: the model sees each tool as `mcp__factorial__<name>`.
SERVER: Final = "factorial"


def build_options(
    spec: ChatTurnSpec,
    tools: ChatToolbox,
    *,
    cwd: Path,
    config_dir: Path,
    store: Any,
    cli_path: Path,
    real_cli: Path,
) -> ClaudeAgentOptions:
    env = credential_environment(
        {
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            ENV_NONESSENTIAL: "1",
            "DISABLE_AUTOUPDATER": "1",
            ENV_TOOL_SEARCH: "false",
            ENV_REAL_CLI: str(real_cli),
            ENV_AUTH_MODE: spec.auth_mode,
        },
        auth_mode=spec.auth_mode,
        credential=spec.credential.get_secret_value(),
    )
    return ClaudeAgentOptions(
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers={SERVER: tool_server(tools)},
        allowed_tools=[f"mcp__{SERVER}__*"],
        permission_mode="dontAsk",
        system_prompt=spec.system_prompt,
        model=spec.model,
        effort=spec.effort,
        max_turns=spec.limits.max_steps,
        max_budget_usd=float(spec.limits.max_budget_usd),
        include_partial_messages=True,
        cwd=cwd,
        env=env,
        cli_path=str(cli_path),
        session_store=store,
        resume=spec.resume_session,
    )


def sdk_tools(tools: ChatToolbox) -> list[SdkMcpTool[Any]]:
    """Each definition as an SDK tool whose handler calls `tools.call` in this process."""
    return [
        SdkMcpTool(
            name=definition.name,
            description=definition.description,
            input_schema=dict(definition.input_schema),
            handler=_handler(tools, definition.name),
            annotations=ToolAnnotations(
                title=definition.title,
                readOnlyHint=definition.read_only,
                destructiveHint=definition.destructive,
            ),
        )
        for definition in tools.definitions()
    ]


def _handler(
    tools: ChatToolbox, name: str
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handle(arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = await tools.call(name, arguments)
        return {"content": [{"type": "text", "text": outcome.text}], "is_error": not outcome.ok}

    return handle


def tool_server(tools: ChatToolbox) -> McpSdkServerConfig:
    """The toolbox as the SDK's in-process MCP server. The CLI child calls a tool over its stdio
    control channel and the call runs in this process: no network, and no bearer anywhere."""
    return create_sdk_mcp_server(SERVER, tools=sdk_tools(tools))
