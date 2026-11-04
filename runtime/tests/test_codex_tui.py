"""The codex TUI's launch documents: what reaches its prompt with no dialog, trusts nothing in
the checkout, and puts no secret where `ps` or the config can show it (decision 56). Pure: every
path is a parameter, so these hold for E2B and a host alike."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
from host_session_support import (
    OPERATOR_ENV,
    SESSION_ENV,
    TOOL_ROOTS,
    plant_operator_toolchains,
)

from runtime.adapters._cli import session_signals
from runtime.adapters._cli.launch import SIGNAL_SH
from runtime.adapters.codex_cli import cli as codex_cli
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_BEDROCK,
    AUTH_MODE_SUBSCRIPTION,
)
from runtime.adapters.codex_cli.cli import (
    CONFIG_FILE,
    ENV_API_KEY,
    VALUE_SANDBOX_FULL,
    VALUE_SANDBOX_WORKSPACE,
)
from runtime.adapters.codex_cli.spec import build_config_toml
from runtime.adapters.codex_cli.tui import (
    AGENTS_MD_LINE,
    auth_document,
    hook_hash,
    pointer,
    resume_argv,
    signal_command,
    spawn_argv,
    thread_of,
    tui_config,
    tui_config_toml,
    tui_environment,
    tui_flags,
    turn_text,
)
from runtime.adapters.e2b.codex import _config as e2b_config
from runtime.contract import AgentPrompts, McpServerSpec, McpTransport, RunSpec, Workspace

# Pins the hook-hash formula. codex 0.159.0 recorded its own digests for a probe path that cannot be
# published, so these are the formula's output for a sample path, not codex's.
_PROBE = "/tmp/probes/live-codex"
_PROBE_COMMAND = (
    f'/bin/sh -c \'tr -d "\\n" >> "{_PROBE}/signals.jsonl"; echo >> "{_PROBE}/signals.jsonl"\''
)
PINNED_HOOK_HASHES = (
    (
        "session_start",
        _PROBE_COMMAND,
        "sha256:b7834621acb1cf775ba3effdb2f80458095755edba24f36cd8a615c7070e57c2",
    ),
    (
        "user_prompt_submit",
        _PROBE_COMMAND,
        "sha256:4412a56e56b4fd5bfaba5a67b03a8b934f1e2f925cc4781f99f1fe3a244f68e6",
    ),
    (
        "stop",
        _PROBE_COMMAND,
        "sha256:38a60da5b3d9deac823b82100ffc3898d8a465b596ed1686dae234c089e12c33",
    ),
)

BINARY = "/opt/codex/bin/codex"
MODEL = "gpt-5.3-codex"
ROOT = Path("/srv/factorial")
WORKTREE = "/srv/factorial/runs/r1/worktree"
CODEX_HOME = "/srv/factorial/runs/r1/config"
SIGNALS = "/srv/factorial/runs/r1/signals.jsonl"
SIGNAL = "/srv/factorial/bin/signal.sh"
TURN = "/srv/factorial/runs/r1/turns/0.md"
IMAGES = ("/srv/factorial/runs/r1/attachments/a.png", "/srv/factorial/runs/r1/attachments/b.png")
THREAD = "01a0f971-46a7-7d00-9e29-d969665667a5"
API_KEY = "sk-proj-really-secret-0123456789"
TOKEN = "ghs_really_secret_token"
DOCS_TOKEN = "docs-bearer-secret"
SYSTEM = "You are the plan agent. SYSTEM-MARKER"

EVENTS = {"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit", "stop": "Stop"}

MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.example.com/docs",
    headers={"Authorization": "Bearer {secrets.DOCS_TOKEN}"},
)
MCP_STDIO = McpServerSpec(
    name="local-index",
    transport=McpTransport.STDIO,
    command="npx",
    args=("-y", "@acme/index@1.2.3"),
    env={"INDEX_MODE": "fast"},
)


def make_spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system=SYSTEM, instructions="Write a spec."),
        "model": MODEL,
        "max_turns": 40,
    }
    return RunSpec.model_validate(fields | overrides)


SECRETS = {ENV_API_KEY: API_KEY, "GH_TOKEN": TOKEN, "DOCS_TOKEN": DOCS_TOKEN}


def host_config(
    servers: tuple[McpServerSpec, ...] = (MCP_STDIO, MCP_HTTP),
    *,
    developer_instructions: str | None = SYSTEM,
    workspace_real: str = WORKTREE,
    ask_tool: bool = False,
) -> tuple[str, dict[str, str]]:
    return tui_config_toml(
        servers,
        SECRETS,
        writable_root=ROOT,
        workspace_real=workspace_real,
        codex_home_real=CODEX_HOME,
        signal_command=signal_command(SIGNALS, SIGNAL),
        developer_instructions=developer_instructions,
        ask_tool=ask_tool,
    )


@pytest.mark.parametrize(("label", "command", "trusted"), PINNED_HOOK_HASHES)
def test_the_hook_hash_formula_is_stable(
    label: str, command: str, trusted: str
) -> None:
    assert hook_hash(label, command) == trusted


def test_the_seed_trusts_nothing_in_the_worktree_and_hashes_its_own_hooks() -> None:
    document, _ = host_config()
    seed = tomllib.loads(document)

    # Exactly the path given, and only it: codex matches the checkout's real path.
    assert seed["projects"] == {WORKTREE: {"trust_level": "untrusted"}}
    command = signal_command(SIGNALS, SIGNAL)
    assert seed["hooks"]["state"] == {
        f"{CODEX_HOME}/{CONFIG_FILE}:{label}:0:0": {"trusted_hash": hook_hash(label, command)}
        for label in EVENTS
    }
    for event in EVENTS.values():
        assert seed["hooks"][event] == [{"hooks": [{"type": "command", "command": command}]}]
    assert set(seed["hooks"]) == {*EVENTS.values(), "state"}
    assert seed["check_for_update_on_startup"] is False
    assert seed["cli_auth_credentials_store"] == "file"
    assert "notify" not in seed


def test_the_tui_config_keeps_the_headless_sandbox_policy_and_mcp_servers() -> None:
    # The last table of the headless document is a stdio server's `env`: a TUI key appended
    # after it would land inside that table instead of at the top.
    base, base_env = build_config_toml(
        (MCP_HTTP, MCP_STDIO), SECRETS, writable_root=ROOT, worktree=Path(WORKTREE)
    )
    document, env = host_config((MCP_HTTP, MCP_STDIO))
    headless, seed = tomllib.loads(base), tomllib.loads(document)
    assert {key: seed[key] for key in headless} == headless
    assert seed["sandbox_mode"] == VALUE_SANDBOX_WORKSPACE
    assert env == base_env

    e2b_base, _ = e2b_config((MCP_HTTP,))
    e2b_seed = tomllib.loads(
        tui_config(
            e2b_base,
            codex_home_real="/home/agent/.factorial",
            signal_command=signal_command("/opt/factorial/run/signals.jsonl", SIGNAL),
            developer_instructions=SYSTEM,
        )
    )
    e2b_headless = tomllib.loads(e2b_base)
    assert {key: e2b_seed[key] for key in e2b_headless} == e2b_headless
    assert e2b_seed["sandbox_mode"] == VALUE_SANDBOX_FULL


@pytest.mark.parametrize(
    "system",
    [
        'ends in a quote"""',
        "C:\\path\\with\\backslashes\\",
        "a bell \x07, an escape \x1b[0m, a delete \x7f and a tab\t",
        "lines\r\nand more lines\n",
        "non-ASCII: é, ü, 漢字 and an emoji 😀",
    ],
)
def test_developer_instructions_round_trip_any_system_prompt(system: str) -> None:
    document, _ = host_config(developer_instructions=system)
    assert tomllib.loads(document)["developer_instructions"] == system


def test_a_resume_seed_carries_no_developer_instructions() -> None:
    document, _ = host_config(developer_instructions=None)
    assert "developer_instructions" not in tomllib.loads(document)


@pytest.mark.parametrize("ask_tool", [True, False], ids=["on", "off"])
def test_the_ask_tool_is_the_one_config_switch_for_codexs_question_box(ask_tool: bool) -> None:
    """Off, codex refuses the tool in Default mode and the question comes back at the turn's
    end; on, the picker waits for a person until codex answers it alone. Either way the
    document must still parse with its stdio server's `env` as its last headless table."""
    document, _ = host_config((MCP_HTTP, MCP_STDIO), ask_tool=ask_tool)
    doc = tomllib.loads(document)
    if ask_tool:
        assert doc["features"] == {"default_mode_request_user_input": True}
        assert doc["suppress_unstable_features_warning"] is True
        # codex's own instructions send a blocking question to plain words; the agent's prompt
        # stays whole ahead of the line.
        assert doc["developer_instructions"].startswith(SYSTEM)
        assert doc["developer_instructions"].endswith(codex_cli.ASK_TOOL_INSTRUCTION)
    else:
        assert "features" not in doc
        assert "suppress_unstable_features_warning" not in doc
        assert doc["developer_instructions"] == SYSTEM
    assert doc["mcp_servers"]["local-index"]["env"] == {"INDEX_MODE": "fast"}


def test_spawn_argv_is_no_daemon_inline_and_puts_each_image_in_equals_form() -> None:
    flags = tui_flags(MODEL, worktree=WORKTREE, sandbox_mode=VALUE_SANDBOX_WORKSPACE)
    prompt = pointer(TURN)
    argv = spawn_argv(flags, IMAGES, prompt, binary=BINARY)
    assert argv == (
        BINARY,
        "--no-daemon",
        "--no-alt-screen",
        "-m",
        MODEL,
        "-C",
        WORKTREE,
        "-s",
        "workspace-write",
        "-a",
        "never",
        f"--image={IMAGES[0]}",
        f"--image={IMAGES[1]}",
        prompt,
    )
    # A bare `--image` takes every following value, the prompt included.
    assert not {"--image", "-i"} & set(argv)


def test_an_image_path_with_a_comma_is_refused() -> None:
    # `--image` splits its value on commas, so the path would arrive as two images.
    flags = tui_flags(MODEL, worktree=WORKTREE, sandbox_mode=VALUE_SANDBOX_WORKSPACE)
    with pytest.raises(ValueError, match="comma"):
        spawn_argv(flags, ("/srv/a,b.png",), pointer(TURN), binary=BINARY)


def test_resume_argv_names_the_thread_and_repeats_the_flags() -> None:
    flags = tui_flags(MODEL, worktree=WORKTREE, sandbox_mode=VALUE_SANDBOX_FULL)
    prompt = pointer(TURN)
    argv = resume_argv(flags, THREAD, IMAGES[:1], prompt, binary=BINARY)
    assert argv == (BINARY, "resume", *flags, f"--image={IMAGES[0]}", THREAD, prompt)
    assert argv[argv.index("-s") + 1] == VALUE_SANDBOX_FULL


def test_a_turnless_resume_has_no_prompt() -> None:
    flags = tui_flags(MODEL, worktree=WORKTREE, sandbox_mode=VALUE_SANDBOX_WORKSPACE)
    argv = resume_argv(flags, THREAD, (), None, binary=BINARY)
    assert argv == (BINARY, "resume", *flags, THREAD)


def test_a_pointer_refuses_whitespace_and_never_begins_with_a_dash() -> None:
    prompt = pointer(TURN)
    assert prompt == f"Read {TURN} and do what it says."
    assert not prompt.startswith("-")
    for bad in ("/srv/run s/turns/0.md", "/srv/run\t/turns/0.md", "/srv/run/turns/0.md\n"):
        with pytest.raises(ValueError, match="whitespace"):
            pointer(bad)
    with pytest.raises(ValueError, match="absolute"):
        pointer("-turns/0.md")


def test_no_secret_reaches_argv_the_hook_command_or_the_config() -> None:
    document, env = host_config((MCP_HTTP,))
    command = signal_command(SIGNALS, SIGNAL)
    flags = tui_flags(MODEL, worktree=WORKTREE, sandbox_mode=VALUE_SANDBOX_WORKSPACE)
    argvs = (
        spawn_argv(flags, IMAGES, pointer(TURN), binary=BINARY),
        resume_argv(flags, THREAD, IMAGES, pointer(TURN), binary=BINARY),
    )
    for secret in (API_KEY, TOKEN, DOCS_TOKEN):
        for argv in argvs:
            assert not any(secret in arg for arg in argv)
        assert secret not in command
        assert secret not in document
    # The MCP bearer rides in the environment, as headless; the key only in `auth.json`.
    assert DOCS_TOKEN in env.values()
    assert API_KEY in auth_document(AUTH_MODE_API_KEY, API_KEY)


def test_the_api_key_auth_document_is_apikey_shaped_and_subscription_passes_the_stored_login() -> (
    None
):
    assert json.loads(auth_document(AUTH_MODE_API_KEY, API_KEY)) == {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": API_KEY,
    }
    stored = '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}\n'
    assert auth_document(AUTH_MODE_SUBSCRIPTION, stored) == stored
    assert auth_document(AUTH_MODE_BEDROCK, "bedrock-api-key-x") == ""


@pytest.mark.parametrize("credential_less", [False, True])
def test_a_host_session_gets_the_operators_toolchain_roots_that_exist_and_no_credential_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential_less: bool
) -> None:
    expected = plant_operator_toolchains(tmp_path, monkeypatch)

    env = tui_environment(
        SECRETS,
        config_dir=Path(CODEX_HOME),
        home=Path(CODEX_HOME, "home"),
        worktree=Path(WORKTREE),
        git_dir=None,
        credential_less=credential_less,
        config_sha=None,
        mcp_env={},
        bot_login="factorial-app[bot]",
    )

    roots = {key: env[key] for key in TOOL_ROOTS if key in env}
    assert roots == ({} if credential_less else expected)
    assert not {"CARGO_HOME", "NPM_CONFIG_USERCONFIG", "GOPATH"} & env.keys()
    holders = (str(tmp_path / ".cargo"), str(tmp_path / ".npmrc"))
    assert not [value for value in env.values() if any(h in value for h in holders)]


def test_the_tui_env_keeps_an_allowlist_of_the_workers_and_carries_no_operator_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "TMUX": "/tmp/tmux-501/default,1,0",
        "TMUX_PANE": "%3",
        "CODEX_THREAD_ID": "outer",
        "CODEX_SANDBOX": "seatbelt",
        "CODEX_HOME": "/Users/operator/.codex",
        "HOME": "/Users/operator",
        "FACTORIAL_DATABASE_URL": "postgres://operator",
        "TERM": "screen",
        **OPERATOR_ENV,
        **SESSION_ENV,
        "ANTHROPIC_API_KEY": "sk-ant-operator",
    }.items():
        monkeypatch.setenv(key, value)
    config_dir, worktree, git_dir = Path(CODEX_HOME), Path(WORKTREE), Path("/srv/clone/.git")

    env = tui_environment(
        SECRETS,
        config_dir=config_dir,
        home=config_dir / "home",
        worktree=worktree,
        git_dir=git_dir,
        credential_less=False,
        config_sha="abc123",
        mcp_env={"FACTORIAL_MCP_PRODUCT_DOCS_BEARER": DOCS_TOKEN},
        bot_login="factorial-app[bot]",
    )
    assert not {"TMUX", "TMUX_PANE", "FACTORIAL_DATABASE_URL"} & set(env)
    assert {key for key in env if key.startswith("CODEX_")} == {"CODEX_HOME"}
    assert env["CODEX_HOME"] == CODEX_HOME
    assert env["HOME"] == f"{CODEX_HOME}/home"
    assert env["TERM"] == "xterm-256color"
    assert {key: env.get(key) for key in SESSION_ENV} == SESSION_ENV
    assert env["GIT_DIR"] == str(git_dir)
    assert env["GIT_WORK_TREE"] == WORKTREE
    assert env["GH_TOKEN"] == TOKEN
    assert env["FACTORIAL_CONFIG_SHA"] == "abc123"
    assert env["FACTORIAL_MCP_PRODUCT_DOCS_BEARER"] == DOCS_TOKEN
    assert not {*OPERATOR_ENV, "ANTHROPIC_API_KEY"} & env.keys()

    blind = tui_environment(
        SECRETS,
        config_dir=config_dir,
        home=config_dir / "home",
        worktree=worktree,
        git_dir=None,
        credential_less=True,
        config_sha=None,
        mcp_env={},
        bot_login="factorial-app[bot]",
    )
    assert "GH_TOKEN" not in blind
    assert not OPERATOR_ENV.keys() & blind.keys()
    assert blind["CODEX_HOME"] == CODEX_HOME
    assert blind["HOME"] == f"{CODEX_HOME}/home"
    for each in (env, blind):
        assert each["GIT_AUTHOR_NAME"] == each["GIT_COMMITTER_NAME"] == "factorial-app[bot]"
        assert each["GIT_COMMITTER_EMAIL"] == "factorial-app[bot]@users.noreply.github.com"


def test_codex_hook_names_equal_the_ones_session_signals_reads() -> None:
    read = (
        "HOOK_EVENT_NAME",
        "HOOK_SESSION_ID",
        "HOOK_SESSION_START",
        "HOOK_USER_PROMPT_SUBMIT",
        "HOOK_STOP",
        "HOOK_SOURCE",
        "LAUNCH_SOURCES",
        "HOOK_LAST_MESSAGE",
    )
    for name in read:
        assert getattr(codex_cli, name) == getattr(session_signals, name), name
    # Every hook installed is one the model reads.
    assert set(codex_cli.HOOK_LABELS) == {
        session_signals.HOOK_SESSION_START,
        session_signals.HOOK_USER_PROMPT_SUBMIT,
        session_signals.HOOK_STOP,
    }


def test_the_thread_id_is_read_from_a_rollout_name_and_nothing_else() -> None:
    name = f"rollout-2026-10-01T17-49-05-{THREAD}.jsonl"
    assert thread_of(name) == THREAD
    assert thread_of(f"{CODEX_HOME}/sessions/2026/10/01/{name}") == THREAD
    for other in (
        f"{name}.zst",
        f"rollout-2026-10-01T17-49-05-{THREAD.upper()}.jsonl",
        f"rollout-{THREAD}.jsonl",
        f"x-rollout-2026-10-01T17-49-05-{THREAD}.jsonl",
        f"rollout-2026-10-01T17-49-05-{THREAD}-{THREAD}.jsonl",
        f"/sessions/rollout-2026-10-01T17-49-05-{THREAD}.jsonl/history.jsonl",
    ):
        assert thread_of(other) is None, other


def test_the_turn_file_carries_no_system_prompt_and_asks_for_agents_md() -> None:
    text = turn_text(make_spec(), {})
    assert "SYSTEM-MARKER" not in text
    assert "Write a spec." in text
    assert text.endswith(AGENTS_MD_LINE)


def test_the_signal_command_carries_its_signals_path_without_the_env(tmp_path: Path) -> None:
    # codex runs a hook through `$SHELL -lc` with its own environment snapshot, not the TUI's:
    # the path has to ride in the command, and survive a space and a quote in it.
    script = tmp_path / "bin" / "signal.sh"
    script.parent.mkdir()
    script.write_text(SIGNAL_SH)
    script.chmod(0o755)
    signals = tmp_path / "it's a run" / "signals.jsonl"
    signals.parent.mkdir()
    command = signal_command(str(signals), str(script))
    assert shlex.split(command)[1:] == ["exec", str(script)]

    env = {k: v for k, v in os.environ.items() if k != "FACTORIAL_SIGNALS"}
    hook = {"hook_event_name": "Stop", "session_id": THREAD}
    subprocess.run(
        ["/bin/sh", "-c", command], input=json.dumps(hook), text=True, env=env, check=True
    )
    (line,) = signals.read_text().splitlines()
    assert json.loads(line)["hook"] == hook


def test_only_a_bedrock_run_loses_the_aws_variables_its_secrets_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An API-key run whose project happens to store a Bedrock bearer keeps an AWS profile its
    secrets carry; the auth mode decides, not a secret's name. The worker's own AWS variables
    reach no session either way: a session keeps only its allowlist of the worker's env."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAWORKER")
    secrets = {**SECRETS, "AWS_BEARER_TOKEN_BEDROCK": "bearer", "AWS_PROFILE": "project"}

    def env(*, bedrock: bool) -> dict[str, str]:
        return tui_environment(
            secrets,
            config_dir=Path(CODEX_HOME),
            home=Path(CODEX_HOME, "home"),
            worktree=Path(WORKTREE),
            git_dir=None,
            credential_less=False,
            config_sha=None,
            mcp_env={},
            bot_login="factorial-app[bot]",
            bedrock=bedrock,
        )

    assert env(bedrock=False)["AWS_PROFILE"] == "project"
    assert "AWS_PROFILE" not in env(bedrock=True)
    assert env(bedrock=True)["AWS_BEARER_TOKEN_BEDROCK"] == "bearer"
    assert "AWS_ACCESS_KEY_ID" not in env(bedrock=False) | env(bedrock=True)
