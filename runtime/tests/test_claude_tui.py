"""The claude TUI's launch documents: what reaches its prompt with no dialog, and nothing more
(decision 56). Pure: every path is a parameter, so these hold for E2B and a host alike."""

from __future__ import annotations

import json

from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION
from runtime.adapters.claude_cli.cli import (
    CLAUDE_JSON_FILE,
    ENV_AFK_COUNTDOWN,
    ENV_AFK_TIMEOUT,
    ENV_API_KEY,
    SETTINGS_FILE,
)
from runtime.adapters.claude_cli.tui import (
    pointer,
    resume_argv,
    seed_files,
    spawn_argv,
    tui_env,
    tui_flags,
)
from runtime.adapters.e2b.claude import PLACEHOLDER_API_KEY, PLACEHOLDER_OAUTH_TOKEN
from runtime.contract import AgentPrompts, McpServerSpec, McpTransport, RunSpec, Workspace

CONFIG = "/home/agent/.factorial"
WORKSPACE_REAL = "/home/agent/workspace"
SIGNAL = "/opt/factorial/bin/signal.sh"
SIGNALS = "/opt/factorial/run/signals.jsonl"
SYSTEM_PROMPT = f"{CONFIG}/system-prompt.md"
MCP = f"{CONFIG}/mcp.json"
TURN = f"{CONFIG}/turns/0.md"
IMAGES = (f"{CONFIG}/attachments/image-1.png", f"{CONFIG}/attachments/image-2.png")
SESSION = "5f0b1f2e-0000-4000-8000-00000000c1a0"

PLACEHOLDERS = {
    AUTH_MODE_API_KEY: PLACEHOLDER_API_KEY,
    AUTH_MODE_SUBSCRIPTION: PLACEHOLDER_OAUTH_TOKEN,
}


def make_spec() -> RunSpec:
    server = McpServerSpec(
        name="product-docs",
        transport=McpTransport.HTTP,
        url="https://mcp.example.com/docs",
        tools_allow=("search", "fetch_*"),
    )
    defaults: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 40,
        "mcp": (server,),
    }
    return RunSpec.model_validate(defaults)


def flags(spec: RunSpec) -> tuple[str, ...]:
    return tui_flags(spec, system_prompt_path=SYSTEM_PROMPT, mcp_path=MCP)


def seeds(auth_mode: str, workspace_real: str = WORKSPACE_REAL) -> dict[str, dict[str, object]]:
    files = seed_files(
        config_dir=CONFIG,
        workspace_real=workspace_real,
        signal_script=SIGNAL,
        auth_mode=auth_mode,
        credential=PLACEHOLDERS[auth_mode],
    )
    assert set(files) == {f"{CONFIG}/{CLAUDE_JSON_FILE}", f"{CONFIG}/{SETTINGS_FILE}"}
    return {path.rsplit("/", 1)[1]: json.loads(content) for path, content in files.items()}


def env(auth_mode: str) -> dict[str, str]:
    return tui_env(
        config_dir=CONFIG,
        signals_path=SIGNALS,
        auth_mode=auth_mode,
        credential=PLACEHOLDERS[auth_mode],
    )


def test_the_pointer_is_positional_after_double_dash_even_with_variadic_tool_patterns() -> None:
    """`--allowed-tools` takes every following word; a pointer after it would be swallowed as a
    tool pattern, and the TUI would sit at an empty prompt with no turn to run."""
    block = flags(make_spec())
    assert block[-3:] == (
        "--allowed-tools",
        "mcp__product-docs__search",
        "mcp__product-docs__fetch_*",
    )
    text = pointer(TURN, IMAGES)

    for argv in (
        spawn_argv(block, SESSION, text),
        resume_argv(block, SESSION, text),
    ):
        dash = argv.index("--")
        assert argv[dash + 1 :] == (text,)
        assert not any(arg.startswith("mcp__") for arg in argv[dash + 1 :])

    assert "--" not in resume_argv(block, SESSION, None)


def test_the_env_never_carries_an_auto_answer_timer() -> None:
    """Either timer answers an `AskUserQuestion` for the human (decision 56)."""
    for auth_mode in (AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION):
        child = env(auth_mode)
        assert not [key for key in child if key.startswith("CLAUDE_AFK_")]
        assert ENV_AFK_TIMEOUT not in child and ENV_AFK_COUNTDOWN not in child
        assert seeds(auth_mode)[SETTINGS_FILE]["askUserQuestionTimeout"] == "never"


def test_trust_is_seeded_on_the_exact_path_given_and_nowhere_else() -> None:
    """Trust on a parent would extend to every checkout under it; trust anywhere but the real
    path brings back the dialog, and no hook fires until it is answered."""
    real = "/private/var/agent/workspace"
    projects = seeds(AUTH_MODE_API_KEY, real)[CLAUDE_JSON_FILE]["projects"]

    assert projects == {real: {"hasTrustDialogAccepted": True}}


def test_the_key_approved_is_the_credentials_trimmed_tail_and_only_in_api_key_mode() -> None:
    """Any other approval brings the API-key dialog back, and no hook fires behind it. A
    subscription run carries no key, which would outrank its token (cli.py)."""
    keyed = seeds(AUTH_MODE_API_KEY)[CLAUDE_JSON_FILE]["customApiKeyResponses"]
    assert keyed == {"approved": [PLACEHOLDER_API_KEY.strip()[-20:]], "rejected": []}
    assert "customApiKeyResponses" not in seeds(AUTH_MODE_SUBSCRIPTION)[CLAUDE_JSON_FILE]
    assert ENV_API_KEY not in env(AUTH_MODE_SUBSCRIPTION)


def test_every_hook_event_names_the_signal_script_and_askuserquestion_is_matched() -> None:
    """A missing event is a state the adapter never learns of; an unmatched tool hook would
    signal a question on every tool call."""
    hooks = seeds(AUTH_MODE_API_KEY)[SETTINGS_FILE]["hooks"]
    assert isinstance(hooks, dict)

    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "StopFailure",
        "SessionEnd",
    }
    for event, groups in hooks.items():
        assert groups == [
            {
                **({"matcher": "AskUserQuestion"} if event.endswith("ToolUse") else {}),
                "hooks": [{"type": "command", "command": SIGNAL}],
            }
        ], event
