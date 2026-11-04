from pathlib import Path
from typing import Any, cast

import pytest
from fake_toolbox import ECHO, FakeToolbox
from test_assistant_contract import _spec

from runtime.assistant.adapters.claude_sdk.launcher import (
    ENV_AUTH_MODE,
    ENV_REAL_CLI,
    child_environment,
)
from runtime.assistant.adapters.claude_sdk.options import (
    SERVER,
    build_options,
    sdk_tools,
    tool_server,
)


def test_options_switch_off_everything_but_the_turns_tools(tmp_path):
    store = object()
    opts = build_options(
        _spec(),
        FakeToolbox(),
        cwd=tmp_path / "c",
        config_dir=tmp_path / "cfg",
        store=store,
        cli_path=Path("/venv/bin/factorial-assistant-cli"),
        real_cli=Path("/sdk/_bundled/claude"),
    )
    assert opts.tools == []
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.permission_mode == "dontAsk"
    assert opts.allowed_tools == ["mcp__factorial__*"]
    servers = cast(dict[str, Any], opts.mcp_servers)
    assert set(servers) == {"factorial"}
    assert servers["factorial"]["type"] == "sdk"
    assert opts.model == "claude-opus-5-5" and opts.max_turns == 25
    assert opts.effort == "medium" and opts.max_budget_usd == 1.0
    assert opts.system_prompt == "be brief"
    assert opts.include_partial_messages is True
    assert opts.cwd == tmp_path / "c"
    assert opts.cli_path == "/venv/bin/factorial-assistant-cli"
    assert opts.session_store is store and opts.resume is None
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert opts.env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert opts.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert opts.env["DISABLE_AUTOUPDATER"] == "1"
    assert opts.env["ENABLE_TOOL_SEARCH"] == "false"
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in opts.env
    assert opts.env["FACTORIAL_ASSISTANT_REAL_CLI"] == "/sdk/_bundled/claude"
    assert opts.env["FACTORIAL_ASSISTANT_AUTH_MODE"] == "api_key"
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in opts.env


@pytest.mark.parametrize("auth_mode", ["api_key", "subscription"])
def test_every_variable_the_adapter_sets_reaches_the_child(tmp_path, auth_mode):
    opts = build_options(
        _spec(auth_mode=auth_mode),
        FakeToolbox(),
        cwd=tmp_path,
        config_dir=tmp_path,
        store=object(),
        cli_path=Path("/l"),
        real_cli=Path("/r"),
    )
    expected = {k: v for k, v in opts.env.items() if k not in (ENV_REAL_CLI, ENV_AUTH_MODE)}
    assert child_environment(opts.env) == expected


def test_a_subscription_turn_carries_the_token_and_no_key(tmp_path):
    opts = build_options(
        _spec(auth_mode="subscription"),
        FakeToolbox(),
        cwd=tmp_path,
        config_dir=tmp_path,
        store=object(),
        cli_path=Path("/l"),
        real_cli=Path("/r"),
    )
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-test"
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert opts.env["FACTORIAL_ASSISTANT_AUTH_MODE"] == "subscription"


def test_a_resumed_turn_names_its_session(tmp_path):
    opts = build_options(
        _spec(resume_session="5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a"),
        FakeToolbox(),
        cwd=tmp_path,
        config_dir=tmp_path,
        store=object(),
        cli_path=Path("/l"),
        real_cli=Path("/r"),
    )
    assert opts.resume == "5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a"


async def test_each_definition_becomes_an_sdk_tool_that_calls_the_toolbox() -> None:
    toolbox = FakeToolbox()
    [tool] = sdk_tools(toolbox)
    assert (tool.name, tool.description, tool.input_schema) == (
        "factorial_echo",
        ECHO.description,
        ECHO.input_schema,
    )
    assert tool.annotations is not None
    assert tool.annotations.title == "Echo"
    assert (tool.annotations.read_only_hint, tool.annotations.destructive_hint) == (True, False)
    assert await tool.handler({"text": "hi"}) == {
        "content": [{"type": "text", "text": '{"text": "hi"}'}],
        "is_error": False,
    }
    assert toolbox.calls == [("factorial_echo", {"text": "hi"})]


async def test_a_refused_call_reaches_the_model_as_an_error_result() -> None:
    [tool] = sdk_tools(FakeToolbox(ok=False))
    assert (await tool.handler({"text": "hi"}))["is_error"] is True


def test_the_server_is_in_process_and_named_for_the_allowed_tools() -> None:
    server = tool_server(FakeToolbox())
    assert (server["type"], server["name"]) == ("sdk", SERVER)
    assert SERVER == "factorial"
