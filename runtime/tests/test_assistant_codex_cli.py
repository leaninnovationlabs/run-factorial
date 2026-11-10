"""Assistant Codex CLI backend: config and event mapping."""

from __future__ import annotations

import tomllib

import pytest

from runtime.adapters.codex_cli.auth import AUTH_MODE_BEDROCK, bedrock_provider_config
from runtime.adapters.codex_cli.events import AgentMessage, TurnCompleted, TurnStarted
from runtime.assistant.adapters.codex_cli.config import (
    ENV_TOOL_SOCKET,
    SERVER_NAME,
    build_assistant_config,
)
from runtime.assistant.adapters.codex_cli.events import EventMapper
from runtime.assistant.adapters.codex_cli.tool_socket import _MAX_SUN_PATH, socket_path_for_turn
from runtime.assistant.contract import TurnDone, TurnStop


def test_tool_socket_path_fits_macos_sun_path_limit() -> None:
    path = socket_path_for_turn("999999")
    assert len(path.as_posix().encode()) < _MAX_SUN_PATH


def test_assistant_config_includes_bedrock_and_factorial_mcp(tmp_path) -> None:
    worktree = tmp_path / "conv-1"
    worktree.mkdir()
    document, _ = build_assistant_config(
        worktree=worktree,
        writable_root=worktree,
        mcp_command=("/usr/bin/python3", "-m", "orchestrator.assistant.mcp_bridge"),
        tool_socket=tmp_path / "t1.sock",
        auth_mode=AUTH_MODE_BEDROCK,
        bedrock_region="us-east-1",
    )
    parsed = tomllib.loads(document)
    assert parsed["web_search"] == "disabled"
    assert parsed["model_provider"] == bedrock_provider_config("us-east-1")["provider"]
    assert parsed["mcp_servers"][SERVER_NAME]["command"] == "/usr/bin/python3"
    assert "mcp_bridge" in document


def test_the_bridge_gets_the_tool_socket_from_its_own_env_table(tmp_path) -> None:
    """Codex starts a stdio MCP server with an allowlist of its own env (HOME, PATH and a few
    more) plus the server's `env` table. A socket path set only on the Codex child never reaches
    the bridge, the bridge exits at start, and the turn runs with no factorial tools."""
    socket = tmp_path / "t1.sock"
    document, _ = build_assistant_config(
        worktree=tmp_path,
        writable_root=tmp_path,
        mcp_command=("/usr/bin/python3", "-m", "orchestrator.assistant.mcp_bridge"),
        tool_socket=socket,
        auth_mode="api_key",
        bedrock_region=None,
    )
    server = tomllib.loads(document)["mcp_servers"][SERVER_NAME]
    assert server.get("env") == {ENV_TOOL_SOCKET: str(socket)}


def test_codex_runs_a_factorial_tool_without_asking(tmp_path) -> None:
    """Codex asks to approve an MCP tool not annotated read-only, and with `approval_policy`
    never it refuses the call before the bridge sees it: the model reads nothing. Every factorial
    tool is a read or a proposal the person decides, as on the Claude path."""
    document, _ = build_assistant_config(
        worktree=tmp_path,
        writable_root=tmp_path,
        mcp_command=("/usr/bin/python3", "-m", "orchestrator.assistant.mcp_bridge"),
        tool_socket=tmp_path / "t1.sock",
        auth_mode="api_key",
        bedrock_region=None,
    )
    server = tomllib.loads(document)["mcp_servers"][SERVER_NAME]
    assert server.get("default_tools_approval_mode") == "approve"


def test_codex_waits_for_the_bridge_before_the_model_sees_its_tools(tmp_path) -> None:
    """Codex gives a server that is not required one second to start before it lists the tools
    for the model, then leaves it out: a bridge slower than that, on a busy host, gives the
    model no tools. Required, Codex waits for it and fails the turn if it never starts."""
    document, _ = build_assistant_config(
        worktree=tmp_path,
        writable_root=tmp_path,
        mcp_command=("/usr/bin/python3", "-m", "orchestrator.assistant.mcp_bridge"),
        tool_socket=tmp_path / "t1.sock",
        auth_mode=AUTH_MODE_BEDROCK,
        bedrock_region="us-east-1",
    )
    server = tomllib.loads(document)["mcp_servers"][SERVER_NAME]
    assert server.get("required") is True


@pytest.mark.parametrize("auth_mode", ["api_key", AUTH_MODE_BEDROCK])
def test_an_assistant_turn_gets_no_shell_and_no_network(tmp_path, auth_mode: str) -> None:
    """The child runs as the assistant's user, who can read the API token and the database URL,
    and on Fargate no sandbox starts around a command: the shell itself must be gone. Each key
    sits at the top level, not in the Bedrock provider's table."""
    document, _ = build_assistant_config(
        worktree=tmp_path,
        writable_root=tmp_path,
        mcp_command=("/usr/bin/python3", "-m", "orchestrator.assistant.mcp_bridge"),
        tool_socket=tmp_path / "t1.sock",
        auth_mode=auth_mode,
        bedrock_region="us-east-1",
    )
    parsed = tomllib.loads(document)
    assert parsed["features"] == dict.fromkeys(
        ("shell_tool", "unified_exec", "view_image", "multi_agent"), False
    )
    assert parsed["web_search"] == "disabled"
    assert parsed["sandbox_mode"] == "workspace-write"
    assert parsed["sandbox_workspace_write"]["network_access"] is False


def test_event_mapper_emits_text_and_turn_done() -> None:
    mapper = EventMapper(max_steps=5)
    events = mapper.map(AgentMessage(text="Hello"))
    assert len(events) == 1
    assert events[0].text == "Hello"  # type: ignore[attr-defined]
    done = mapper.map(
        TurnCompleted(
            input_tokens=10,
            cached_input_tokens=2,
            cache_write_input_tokens=0,
            output_tokens=3,
        )
    )
    assert any(isinstance(e, TurnDone) and e.stop == TurnStop.END for e in done)


def test_event_mapper_enforces_max_steps() -> None:
    mapper = EventMapper(max_steps=1)
    assert mapper.map(TurnStarted()) == []
    second = mapper.map(TurnStarted())
    assert second == [TurnDone(stop=TurnStop.STEPS)]
