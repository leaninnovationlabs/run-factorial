"""The claude_cli adapter's interactive path on the host (decision 56): the TUI's environment,
whether the host may run a session at all, a session run end to end, the spawn's own properties
(its credentials, MCP servers, prompt files and images), and a session taken back after a worker
restart.

The refusal tests run stand-in `claude` and `tmux` scripts, so they need neither on the machine.
The session tests run `fake_claude_tui.py` in a real tmux, against a local bare origin, with roots
from `mkdtemp(dir="/tmp")`: pytest's tmp_path on macOS is too long for a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from host_session_support import (
    FAKE_TUI,
    OPERATOR_ENV,
    REPO,
    SESSION_ENV,
    TOOL_ROOTS,
    OriginCli,
    bare_origin,
    edit_state,
    fake_tui_settings_reach_sessions,  # noqa: F401
    git,
    leave_job,
    plant_operator_toolchains,
    reaches,
    run_dir,
    server_of,
    short_root,
    state_of,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import escaped, tmux
from runtime.adapters._cli import host_session as session
from runtime.adapters._cli.environment import ENV_CONFIG_SHA, ENV_GH_TOKEN
from runtime.adapters._cli.launch import ENV_FILE, ENV_SIGNALS
from runtime.adapters._cli.mirror import TRANSLATOR_CHANGED
from runtime.adapters._cli.paths import delete_credentials
from runtime.adapters._cli.probes import ENV_BOT_LOGIN
from runtime.adapters._cli.process import CommandResult
from runtime.adapters._cli.session_state import (
    LOG_FILE,
    STATE_FILE,
    HostRunState,
    read_state,
    write_state,
)
from runtime.adapters._cli.tmux import TmuxServer
from runtime.adapters.claude_cli import session as claude_session
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_SUBSCRIPTION,
    SECRET_OAUTH_TOKEN,
)
from runtime.adapters.claude_cli.cli import (
    API_KEY_TAIL,
    CLAUDE_JSON_FILE,
    CLAUDE_JSON_KEY_RESPONSES,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_CLASSIC_RENDERER,
    ENV_CONFIG_DIR,
    ENV_DISABLE_AUTOUPDATER,
    ENV_OAUTH_TOKEN,
    MCP_CONFIG_FILE,
    PINNED_VERSION,
)
from runtime.adapters.claude_cli.environment import _tui_credential, _tui_environment
from runtime.adapters.claude_cli.stream import NO_TURN
from runtime.adapters.claude_cli.transcript import TRANSCRIPT_VERSION
from runtime.adapters.claude_cli.tui import seed_files
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    AgentRuntimeError,
    AgentSkill,
    IdleReason,
    McpServerSpec,
    McpTransport,
    MissingAttachment,
    ParkReason,
    RunAttachment,
    RunSpec,
    RunStatus,
    RunUsage,
    SkillScope,
    SpawnError,
    SpawnReason,
    Workspace,
)
from runtime.terminal import HostTmuxRelay

API_KEY = "sk-ant-api03-the-stored-key-0123456789abcdef"
HOST_KEY = "sk-ant-api03-the-host-exported-key-zyxwvu"
OAUTH = "sk-ant-oat01-the-stored-token"
TOKEN = "ghs_mintedtoken"
SHA = "c0ffee" * 6 + "abcd"
BOT = "factorial-app[bot]"
CONFIG = Path("/r/runs/x/config")
SIGNALS = Path("/r/runs/x/signals.jsonl")
#: What a Claude Code session the worker runs under exports, as seen from one.
PARENT_SESSION = (
    "CLAUDECODE",
    "AI_AGENT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)


def tui_environment(
    secrets: dict[str, str], *, auth_mode: str = AUTH_MODE_API_KEY, credential_less: bool = False
) -> dict[str, str]:
    return _tui_environment(
        secrets,
        config_dir=CONFIG,
        home=CONFIG / "home",
        signals_path=SIGNALS,
        auth_mode=auth_mode,
        credential=_tui_credential(secrets, auth_mode),
        credential_less=credential_less,
        config_sha=SHA,
        bot_login=BOT,
    )


def test_the_tui_env_is_no_factorial_variable_of_the_workers_and_then_tui_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORIAL_MASTER_KEY", "master")
    monkeypatch.setenv(ENV_SIGNALS, "/elsewhere/signals.jsonl")
    monkeypatch.setenv(ENV_CONFIG_SHA, "0" * 40)

    env = tui_environment({ENV_GH_TOKEN: TOKEN, ENV_API_KEY: API_KEY})

    assert {key for key in env if key.startswith("FACTORIAL_")} == {ENV_SIGNALS, ENV_CONFIG_SHA}
    assert env[ENV_SIGNALS] == str(SIGNALS)
    assert env[ENV_CONFIG_SHA] == SHA
    assert env[ENV_GH_TOKEN] == TOKEN
    assert "GIT_CONFIG_KEY_0" in env
    assert env[ENV_CLASSIC_RENDERER] == "1"


@pytest.mark.parametrize("credential_less", [False, True])
def test_a_parent_sessions_afk_timer_or_tmux_variable_never_reaches_the_tui(
    monkeypatch: pytest.MonkeyPatch, credential_less: bool
) -> None:
    """A stored secret is the operator's choice and stays, as headless, but for an AFK timer,
    which would answer the human's question from any source."""
    planted = {*PARENT_SESSION, "TMUX", "TMUX_PANE", "CLAUDE_AFK_TIMEOUT_MS", "CLAUDE_AFK_X"}
    for key in planted:
        monkeypatch.setenv(key, "1")
    secrets = {
        ENV_API_KEY: API_KEY,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
        "CLAUDE_AFK_COUNTDOWN_MS": "1",
    }

    env = tui_environment(secrets, credential_less=credential_less)

    assert not (planted | {"CLAUDE_AFK_COUNTDOWN_MS"}) & env.keys()
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


@pytest.mark.parametrize("credential_less", [False, True])
def test_the_tui_env_carries_none_of_the_operators_credentials_and_names_the_agent_to_git(
    monkeypatch: pytest.MonkeyPatch, credential_less: bool
) -> None:
    """A host session keeps an allowlist of the worker's environment: a denylist of what an
    operator's shell holds is never complete. Its HOME is its own, and the commit identity a
    `.gitconfig` gave comes by env."""
    for key, value in {
        **OPERATOR_ENV,
        **SESSION_ENV,
        "HOME": "/Users/operator",
        "GIT_AUTHOR_NAME": "Operator",
        ENV_BASE_URL: "http://gateway",
        "OPENAI_API_KEY": "sk-operator",
    }.items():
        monkeypatch.setenv(key, value)

    env = tui_environment({ENV_API_KEY: API_KEY}, credential_less=credential_less)

    assert not {*OPERATOR_ENV, "OPENAI_API_KEY"} & env.keys()
    if not credential_less:
        assert {key: env.get(key) for key in SESSION_ENV} == SESSION_ENV
        assert env[ENV_BASE_URL] == "http://gateway"
    assert env["HOME"] == f"{CONFIG}/home"
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == BOT
    email = f"{BOT}@users.noreply.github.com"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == email


@pytest.mark.parametrize("credential_less", [False, True])
def test_a_host_session_gets_the_operators_toolchain_roots_that_exist_and_no_credential_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential_less: bool
) -> None:
    """The private HOME hides rustup's toolchains, uv's Pythons and the package caches; a root
    that does not exist would make a tool build one there, and CARGO_HOME holds cargo's tokens."""
    expected = plant_operator_toolchains(tmp_path, monkeypatch)

    env = tui_environment({ENV_API_KEY: API_KEY}, credential_less=credential_less)

    roots = {key: env[key] for key in TOOL_ROOTS if key in env}
    assert roots == ({} if credential_less else expected)
    assert not {"CARGO_HOME", "NPM_CONFIG_USERCONFIG", "GOPATH"} & env.keys()
    holders = (str(tmp_path / ".cargo"), str(tmp_path / ".npmrc"))
    assert not [value for value in env.values() if any(h in value for h in holders)]


@pytest.mark.parametrize("credential_less", [False, True])
def test_a_subscription_tui_env_carries_no_api_key_even_one_the_host_exports(
    monkeypatch: pytest.MonkeyPatch, credential_less: bool
) -> None:
    monkeypatch.setenv(ENV_API_KEY, HOST_KEY)

    env = tui_environment(
        {ENV_API_KEY: API_KEY, ENV_OAUTH_TOKEN: OAUTH},
        auth_mode=AUTH_MODE_SUBSCRIPTION,
        credential_less=credential_less,
    )

    assert ENV_API_KEY not in env
    assert env[ENV_OAUTH_TOKEN] == OAUTH


def test_a_credential_less_tui_env_carries_no_github_token_or_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_GH_TOKEN, "ghs_host")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_host")

    env = tui_environment({ENV_GH_TOKEN: TOKEN, ENV_API_KEY: API_KEY}, credential_less=True)

    assert not [key for key in env if key.startswith(("GH_", "GITHUB", "GIT_CONFIG"))]
    assert TOKEN not in env.values()


def test_the_key_the_tui_sees_is_the_one_its_seed_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tail that does not match the key opens the "use this API key?" dialog, and no hook
    fires behind a dialog: the session would sit RUNNING for ever."""
    monkeypatch.setenv(ENV_API_KEY, HOST_KEY)
    secrets = {ENV_API_KEY: API_KEY}
    credential = _tui_credential(secrets, AUTH_MODE_API_KEY)

    env = tui_environment(secrets)
    seeds = seed_files(
        config_dir=str(CONFIG),
        workspace_real="/r/runs/x/worktree",
        signal_script="/r/bin/signal.sh",
        auth_mode=AUTH_MODE_API_KEY,
        credential=credential,
    )

    approved = json.loads(seeds[f"{CONFIG}/{CLAUDE_JSON_FILE}"])[CLAUDE_JSON_KEY_RESPONSES]
    assert env[ENV_API_KEY] == API_KEY
    assert approved["approved"] == [env[ENV_API_KEY][-API_KEY_TAIL:]]


# ------------------------------------------------------------------ spawn_refusal


@pytest.fixture
def root() -> Iterator[Path]:
    yield from short_root("f243r-")


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


class Host:
    """A PATH holding a stand-in `claude` and `tmux`; the claude logs each run to `calls`."""

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = folder / "bin"
        self.calls = folder / "calls"
        #: The environment the last `claude` ran with.
        self.env = folder / "env"
        self.claude(f"{PINNED_VERSION} (Claude Code)")
        self.tmux("tmux 3.5a")
        monkeypatch.setenv("PATH", str(self.bin))

    def claude(self, version: str, *, at: Path | None = None, status: int = 0) -> Path:
        return _script(
            at or self.bin / "claude",
            f"echo run >> '{self.calls}'\n/usr/bin/env > '{self.env}'\n"
            f"echo '{version}'\nexit {status}",
        )

    def tmux(self, version: str, *, status: int = 0) -> None:
        _script(self.bin / "tmux", f"echo '{version}'\nexit {status}")

    def runs(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("reported", "refused"),
    [
        (f"{PINNED_VERSION} (Claude Code)", False),
        ("2.1.285 (Claude Code)", True),
        (f"1{PINNED_VERSION} (Claude Code)", True),
    ],
)
async def test_interactive_is_refused_unless_the_installed_claude_is_exactly_the_pin(
    host: Host, root: Path, reported: str, refused: bool
) -> None:
    host.claude(reported)

    refusal = await ClaudeCliAdapter(local_root=root).spawn_refusal()

    if not refused:
        assert refusal is None
        return
    assert refusal is not None
    assert repr(reported) in refusal
    assert f"install claude {PINNED_VERSION}" in refusal


async def test_the_version_check_reads_the_binary_the_adapter_would_run(
    host: Host, root: Path, tmp_path: Path
) -> None:
    other = host.claude("2.1.285 (Claude Code)", at=tmp_path / "other" / "claude")

    refusal = await ClaudeCliAdapter(binary=str(other), local_root=root).spawn_refusal()

    assert refusal is not None
    assert str(other) in refusal


@pytest.mark.parametrize(
    ("tmux", "named"),
    [
        (None, "tmux is not on the worker's PATH"),
        ("tmux 3.1c", "is 3.1,"),
        ("tmux master", "names no version"),
    ],
)
async def test_interactive_is_refused_without_tmux_3_2(
    host: Host, root: Path, tmux: str | None, named: str
) -> None:
    if tmux is None:
        (host.bin / "tmux").unlink()
    else:
        host.tmux(tmux)

    refusal = await ClaudeCliAdapter(local_root=root).spawn_refusal()

    assert refusal is not None
    assert named in refusal
    assert "tmux 3.2 or later" in refusal


@pytest.mark.parametrize(
    ("leaf", "named"),
    [("x" * 90, "too long for a tmux socket"), ("with space", "contains whitespace")],
)
async def test_interactive_is_refused_on_a_root_that_cannot_hold_a_socket_or_holds_whitespace(
    host: Host, root: Path, leaf: str, named: str
) -> None:
    refusal = await ClaudeCliAdapter(local_root=root / leaf).spawn_refusal()

    assert refusal is not None
    assert named in refusal
    assert "move local_root" in refusal


async def test_a_version_probe_that_cannot_run_raises_adapter_unavailable_not_a_refusal(
    host: Host, root: Path
) -> None:
    host.claude("", status=1)
    with pytest.raises(AdapterUnavailable):
        await ClaudeCliAdapter(local_root=root).spawn_refusal()

    (host.bin / "claude").unlink()
    with pytest.raises(AdapterUnavailable):
        await ClaudeCliAdapter(local_root=root).spawn_refusal()

    host.claude(f"{PINNED_VERSION} (Claude Code)")
    host.tmux("tmux 3.5a", status=1)
    with pytest.raises(AdapterUnavailable):
        await ClaudeCliAdapter(local_root=root).spawn_refusal()


async def test_a_version_probe_that_hangs_is_killed_and_raises_adapter_unavailable(
    host: Host, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(host.bin / "claude", "exec /bin/sleep 30")
    monkeypatch.setattr(session, "PROBE_TIMEOUT", 0.5)

    with pytest.raises(AdapterUnavailable, match="did not answer"):
        await asyncio.wait_for(ClaudeCliAdapter(local_root=root).spawn_refusal(), 10)


async def test_the_version_probe_gets_none_of_the_workers_environment(
    host: Host, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORIAL_MASTER_KEY", "master")
    monkeypatch.setenv(ENV_API_KEY, HOST_KEY)

    assert await ClaudeCliAdapter(local_root=root).spawn_refusal() is None

    seen = dict(line.split("=", 1) for line in host.env.read_text().splitlines())
    assert not [key for key in seen if key.startswith("FACTORIAL_")]
    assert ENV_API_KEY not in seen
    assert seen["CLAUDE_CONFIG_DIR"].startswith(f"{root}/")


async def test_a_passing_check_is_cached_until_the_binary_changes(host: Host, root: Path) -> None:
    adapter = ClaudeCliAdapter(local_root=root)

    assert await adapter.spawn_refusal() is None
    assert await adapter.spawn_refusal() is None
    assert host.runs() == 1

    claude = host.bin / "claude"
    stat = claude.stat()
    os.utime(claude, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert await adapter.spawn_refusal() is None
    assert host.runs() == 2

    host.claude("2.2.0 (Claude Code)")
    assert await adapter.spawn_refusal() is not None
    assert await adapter.spawn_refusal() is not None
    assert host.runs() == 4


# ------------------------------------------------------------------ a session in tmux

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

DOCS_TOKEN = "docs-token-held-only-by-mcp-json"
MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)
SOCKET = re.compile(r"/t/[0-9a-f]{12}-s")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return bare_origin(tmp_path)


@pytest.fixture
def claude(tmp_path: Path) -> Path:
    """The fake TUI behind an absolute path, as `shutil.which` resolves the real one."""
    return _script(tmp_path / "bin" / "claude", f'exec "{sys.executable}" "{FAKE_TUI}" "$@"')


class Clock:
    def __init__(self) -> None:
        self.ahead = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.ahead


def session_adapter(
    root: Path,
    claude: Path,
    origin: Path,
    *,
    now: Callable[[], datetime] | None = None,
    auth_mode: str = AUTH_MODE_API_KEY,
    cli: OriginCli | None = None,
) -> ClaudeCliAdapter:
    cli = cli or OriginCli(claude, origin)
    return ClaudeCliAdapter(
        local_root=root, cli=cli, now=now or Clock(), poll=0.05, auth_mode=auth_mode
    )


def session_spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You plan.", instructions="Write the spec."),
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 10,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


async def typed(root: Path, run_id: str, text: str) -> None:
    assert await server_of(root, run_id).type_text(text)


async def ended(adapter: ClaudeCliAdapter, root: Path, run_id: str) -> None:
    """The test's own end of a session: its server killed, its supervisor drained."""
    await server_of(root, run_id).kill()
    await adapter.drain()


def files_holding(root: Path, value: str) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and value.encode() in path.read_bytes()
    }


@needs_tmux
async def test_an_interactive_spawn_launches_the_tui_in_its_own_tmux_server_under_local_root(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    for planted in ("FACTORIAL_MASTER_KEY", "TMUX", "TMUX_PANE", "CLAUDECODE"):
        monkeypatch.setenv(planted, "planted")
    monkeypatch.setenv("CLAUDE_AFK_TIMEOUT_MS", "1")
    monkeypatch.setenv("SOME_WORKER_SETTING", "dropped")
    adapter = session_adapter(root, claude, origin)
    secrets = {
        ENV_API_KEY: SecretStr(API_KEY),
        ENV_GH_TOKEN: SecretStr(TOKEN),
        "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
    }

    handle = await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))
    run_id = handle.run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        state = read_state(run_dir(root, run_id), run_id)
        target = await adapter.terminal_target(run_id)
        assert (target.substrate, target.session) == ("host", "run")
        assert target.socket.startswith(f"{root}/t/") and SOCKET.search(target.socket)
        shown = await server_of(root, run_id).display("#{pane_current_path}")
        assert shown.stdout.strip() == state.workspace_real == state.worktree

        seen = json.loads(dump.read_text())
        argv = seen["argv"][1:]
        assert argv[argv.index("--session-id") + 1] == state.session_id
        assert argv[-2] == "--" and argv[-1].startswith("Read @")
        env = seen["env"]
        assert {key for key in env if key.startswith("FACTORIAL_")} == {ENV_SIGNALS}
        assert not {"TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_AFK_TIMEOUT_MS"} & env.keys()
        assert (env[ENV_API_KEY], env[ENV_GH_TOKEN]) == (API_KEY, TOKEN)
        assert "SOME_WORKER_SETTING" not in env
        assert env[ENV_DISABLE_AUTOUPDATER] == "1"
        assert env[ENV_CONFIG_DIR] == str(run_dir(root, run_id) / "config")
        home = run_dir(root, run_id) / "config" / "home"
        assert env["HOME"] == str(home) and home.is_dir()
        helpers = [
            (env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"])
            for n in range(int(env["GIT_CONFIG_COUNT"]))
            if env[f"GIT_CONFIG_KEY_{n}"].startswith("credential.")
        ]
        assert helpers[0] == ("credential.helper", ""), "no helper from a file is asked"
        assert len(helpers) == 2, "the token reaches git by an env-only helper"
        assert "$GH_TOKEN" in helpers[1][1]

        config = run_dir(root, run_id) / "config"
        assert not (config / ENV_FILE).exists(), "the pane deletes its env file"
        seed = json.loads((config / CLAUDE_JSON_FILE).read_text())
        assert seed[CLAUDE_JSON_KEY_RESPONSES]["approved"] == [env[ENV_API_KEY][-API_KEY_TAIL:]]

        ps = ["ps", "-axww", "-o", "args="]
        processes = await asyncio.to_thread(subprocess.run, ps, capture_output=True, text=True)
        environment = await server_of(root, run_id).run("show-environment", "-g")
        for secret in (API_KEY, TOKEN, DOCS_TOKEN):
            assert secret not in processes.stdout
            assert secret not in environment.stdout
        assert files_holding(root, API_KEY[-API_KEY_TAIL:]) == {
            f"runs/{run_id}/config/{CLAUDE_JSON_FILE}"
        }
        assert files_holding(root, DOCS_TOKEN) == {f"runs/{run_id}/config/{MCP_CONFIG_FILE}"}
        assert files_holding(root, TOKEN) == set()
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_stop_then_question_then_human_turn_reach_session_state(
    root: Path, claude: Path, origin: Path
) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        fresh = await adapter.session_state(run_id)
        assert fresh.log_offset == 0 and fresh.session_id, "answered as soon as spawn returns"
        await reaches(adapter, run_id, RunStatus.IDLE)
        stopped = await adapter.session_state(run_id)
        assert stopped.idle_reason is IdleReason.TURN_END and stopped.last_input_at is None
        assert stopped.session_deadline is None and stopped.session_id == fresh.session_id

        await typed(root, run_id, "ASKME\r")

        async def asking() -> bool:
            return (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

        await until(asking, "a question")
        asked = await adapter.session_state(run_id)
        assert asked.question == "Include archived rows? [Active only | Active and archived]"
        assert await adapter.status(run_id) is RunStatus.IDLE

        await typed(root, run_id, "Active only\r")
        await typed(root, run_id, "SLOWME\r")

        async def working_on_it() -> bool:
            # The answer's reply is counted once the log's turn end follows it, which can come
            # a poll after the run reads RUNNING on the answer itself.
            state = await adapter.session_state(run_id)
            return state.turns == 3 and state.idle_reason is None

        await until(working_on_it, "the human's turn under way, the answer's counted")
        assert await adapter.status(run_id) is RunStatus.RUNNING
        assert (await adapter.session_state(run_id)).last_input_at is not None

        async def origins() -> list[str | None]:
            lines = [json.loads(line) async for line in adapter.logs(run_id)]
            return [line.get("factorial_origin") for line in lines if line["type"] == "user"]

        async def mirrored() -> bool:
            return len(await origins()) == 4

        await until(mirrored, "the human's turn in the mirror")
        assert await origins() == ["orchestrator", "human", None, "human"]
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_turn_cap_hangs_up_and_fails_the_run(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "turns=3")
    adapter = session_adapter(root, claude, origin)
    secrets = {ENV_API_KEY: SecretStr(API_KEY), "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    spec = session_spec(max_turns=2, secrets=secrets, mcp=(MCP_HTTP,))
    run_id = (await adapter.spawn(spec)).run_id
    try:
        await reaches(adapter, run_id, RunStatus.FAILED)

        result = await adapter.result(run_id)
        assert "Stopped after 2 turns" in (result.failure_reason or "")
        assert result.session_id is not None
        assert result.cost_usd is not None, "hung up: the CLI writes its cost on a SIGHUP"
        assert await server_of(root, run_id).pane() is None
        config = run_dir(root, run_id) / "config"
        assert (config / "settings.json").exists()
        credentials = (ENV_FILE, CLAUDE_JSON_FILE, MCP_CONFIG_FILE)
        left = [name for name in credentials if (config / name).exists()]
        assert not left, "every credential file goes at the end"
        assert (run_dir(root, run_id) / "worktree").is_dir(), "the workspace is kept"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_line_over_the_ceiling_fails_the_run_rather_than_stalling(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent can write its own log: a line without end would double the read until the
    worker ran out of memory."""
    monkeypatch.setattr(session, "READ_LIMIT", 4096)
    monkeypatch.setattr(session, "MAX_LINE", 16384)
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        state = read_state(run_dir(root, run_id), run_id)
        assert state.session_id is not None
        log = claude_session.session_log(run_dir(root, run_id) / "config", state.session_id)
        with log.open("ab") as handle:
            handle.write(b"x" * 100_000)
        await reaches(adapter, run_id, RunStatus.FAILED)

        result = await adapter.result(run_id)
        assert result.failure_reason == "the session log carried a line over 16 MiB"
        assert await server_of(root, run_id).pane() is None, "hung up"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_session_state_reads_the_state_file_and_never_tmux(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker asks it every tick: a tmux that hangs must not hang the tick."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        released = asyncio.Event()
        real = TmuxServer.run

        async def held(server: TmuxServer, *args: str, start: bool = False) -> CommandResult:
            await released.wait()
            return await real(server, *args, start=start)

        try:
            with monkeypatch.context() as patched:
                patched.setattr(TmuxServer, "run", held)
                state = await asyncio.wait_for(adapter.session_state(run_id), 2)
                await asyncio.wait_for(adapter.terminal_target(run_id), 2)
        finally:
            released.set()  # the supervisor's own poll may be held meanwhile
        assert state.idle_reason is IdleReason.TURN_END
    finally:
        await ended(adapter, root, run_id)


def test_a_legacy_headless_state_file_still_reads(tmp_path: Path) -> None:
    """A run from before sessions, read once after the deploy."""
    legacy = {
        "run_id": "GH-12-spec-agent-0a1b2c3d",
        "status": "succeeded",
        "agent": "spec-agent",
        "ticket": "GH-12",
        "repo": REPO,
        "branch": "spec/12-{slug}",
        "worktree": "/r/runs/x/worktree",
        "started_at": "2026-09-01T10:00:00Z",
        "deadline": "2026-09-01T11:00:00Z",
        "pid": 4242,
        "session_id": "s-1",
        "turns": 3,
        "resume_args": ["--model", "claude-haiku-4-5-20251001"],
        "mcp": [],
    }
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / STATE_FILE).write_text(json.dumps(legacy))

    state = read_state(folder, "x")

    assert state.status is RunStatus.SUCCEEDED
    assert state.signals.idle_reason is None and state.stopping is None


@needs_tmux
async def test_a_live_run_from_before_every_run_was_a_session_fails_rather_than_runs_for_ever(
    root: Path,
) -> None:
    run_id = "GH-12-spec-agent-0a1b2c3d"
    folder = root / "runs" / run_id
    folder.mkdir(parents=True)
    now = datetime.now(UTC)
    legacy = HostRunState(
        run_id=run_id,
        status=RunStatus.RUNNING,
        agent="spec-agent",
        ticket="GH-12",
        repo=REPO,
        branch="spec/12-{slug}",
        worktree=str(folder / "worktree"),
        started_at=now,
        session_id="s-1",
    )
    (folder / STATE_FILE).write_text(legacy.model_dump_json())
    adapter = ClaudeCliAdapter(local_root=root)

    assert await adapter.status(run_id) is RunStatus.FAILED
    assert "never began a session" in ((await adapter.result(run_id)).failure_reason or "")


# ------------------------------------------------------------------ finish, stop and resume

NEW_KEY = "sk-ant-api03-the-key-minted-for-the-resume-9876543210"
NEW_TOKEN = "ghs_mintedagainforresume"
BRANCH = "spec/12-widget"
PNG = b"\x89PNG\r\n\x1a\n-not-really"


def fresh_secrets() -> dict[str, SecretStr]:
    return {ENV_API_KEY: SecretStr(NEW_KEY), ENV_GH_TOKEN: SecretStr(NEW_TOKEN)}


def worktree_git(root: Path, run_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=run_dir(root, run_id) / "worktree", capture_output=True, text=True
    )


def credentials_left(root: Path, run_id: str) -> list[str]:
    config = run_dir(root, run_id) / "config"
    return [
        name for name in (ENV_FILE, CLAUDE_JSON_FILE, MCP_CONFIG_FILE) if (config / name).exists()
    ]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int) -> bool:
    return not alive(pid)


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The file the fake appends every byte typed into it to."""
    keys = tmp_path / "keys"
    monkeypatch.setenv("FAKE_TUI_KEYS", str(keys))
    return keys


def typed_into(keys: Path) -> bytes:
    return keys.read_bytes() if keys.exists() else b""


@needs_tmux
async def test_finish_types_exit_through_paste_buffer_and_succeeds_with_this_segments_cost(
    root: Path, claude: Path, origin: Path
) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        target = await adapter.terminal_target(run_id)
        viewer = await HostTmuxRelay().attach(
            target, writable=False, cols=80, rows=24, on_output=lambda _: None
        )
        try:
            await adapter.finish(run_id)
            assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        finally:
            await viewer.close()

        result = await adapter.result(run_id)
        assert result.cost_usd == Decimal("0.001")
        assert result.stream_warnings == ()
        assert state_of(root, run_id).exit_code == 0, "the CLI left by its own /exit"
        before = (run_dir(root, run_id) / STATE_FILE).read_bytes()
        await adapter.finish(run_id)
        assert (run_dir(root, run_id) / STATE_FILE).read_bytes() == before
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.skipif(
    not sys.platform.startswith("linux") and sys.platform != "darwin",
    reason="reads process environments on Linux and macOS only",
)
async def test_the_end_of_a_session_ends_the_job_its_agent_left_outside_its_group(
    root: Path, claude: Path, origin: Path
) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    config = str(run_dir(root, run_id) / "config")
    jobs = [leave_job(config), leave_job(f"{config}-not-this-run")]
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.finish(run_id)
        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        assert await asyncio.to_thread(jobs[0].wait, 5) == -signal.SIGTERM
        assert jobs[1].poll() is None, "another run's job is left alone"
    finally:
        for job in jobs:
            job.kill()
            job.wait()
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.skipif(
    not sys.platform.startswith("linux") and sys.platform != "darwin",
    reason="reads process environments on Linux and macOS only",
)
async def test_reaping_a_finished_session_ends_a_job_that_outlived_it(
    root: Path, claude: Path, origin: Path
) -> None:
    """One the run's own end missed, such as a worker that died before it."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.finish(run_id)
        job = leave_job(str(run_dir(root, run_id) / "config"))
        try:
            later = session_adapter(
                root, claude, origin, now=lambda: datetime.now(UTC) + timedelta(days=30)
            )
            assert await later.reap(set(), retain=None) == (run_id,)
            assert await asyncio.to_thread(job.wait, 5) == -signal.SIGTERM
        finally:
            job.kill()
            job.wait()
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_on_a_question_asked_in_words_types_exit(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "ask_words")
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

        await adapter.finish(run_id)

        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        assert state_of(root, run_id).exit_code == 0, "a plain prompt is quit, not hung up"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_on_a_question_hangs_up_rather_than_typing_into_the_dialog(
    root: Path, claude: Path, origin: Path, recorded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "ask")
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

        await adapter.finish(run_id)

        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        assert typed_into(recorded) == b""
        assert state_of(root, run_id).exit_code is None, "hung up, not exited"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_on_a_running_session_sends_nothing(
    root: Path, claude: Path, origin: Path, recorded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "slow")
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:

        async def working() -> bool:
            return state_of(root, run_id).signals.prompts == 1

        await until(working, "the first turn under way")

        await adapter.finish(run_id)

        assert await adapter.status(run_id) is RunStatus.RUNNING
        assert typed_into(recorded) == b""
        assert not state_of(root, run_id).finishing
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("ended_by", ["finish", "the session itself"])
async def test_a_failed_last_turn_fails_only_a_session_that_ended_itself(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch, ended_by: str
) -> None:
    """The worker finishes a session only once its postconditions hold: how its last turn went
    is then a warning. A session that left on its own after a failed turn did not deliver."""
    monkeypatch.setenv("FAKE_TUI", "fail_turn")
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_ERROR

        if ended_by == "finish":
            await adapter.finish(run_id)
        else:
            await typed(root, run_id, "/exit\r")
            await adapter.drain()

        result = await adapter.result(run_id)
        if ended_by == "finish":
            assert result.outcome is RunStatus.SUCCEEDED
            assert any("finished after its last turn failed" in w for w in result.stream_warnings)
        else:
            assert result.outcome is RunStatus.FAILED
            assert result.failure_reason is not None
            assert "500 overloaded" in result.failure_reason
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("mode", ["hide_input", "deaf_exit"])
async def test_an_exit_that_does_not_land_falls_back_to_a_hangup(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """`hide_input`: the input line never reads `/exit`, so Enter is never pressed. `deaf_exit`:
    Enter is pressed and the CLI stays."""
    monkeypatch.setenv("FAKE_TUI", mode)
    monkeypatch.setattr(session, "EXIT_WAIT", 1.0)
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)

        await asyncio.wait_for(adapter.finish(run_id), 30)

        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        assert state_of(root, run_id).exit_code is None, "hung up, not exited"
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_closes_a_dialog_a_human_left_open_without_applying_it(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = tmp_path / "applied"
    monkeypatch.setenv("FAKE_TUI_APPLIED", str(applied))
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await typed(root, run_id, "/model\r")

        async def open_() -> bool:
            return "Select model" in (await server_of(root, run_id).capture() or "")

        await until(open_, "the picker open")

        await adapter.finish(run_id)

        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        assert not applied.exists(), "the Enter must not apply the picker's choice"
        assert state_of(root, run_id).exit_code == 0
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("reason", list(ParkReason))
async def test_interrupt_parks_with_its_reason_keeps_the_branch_and_deletes_every_credential(
    root: Path, claude: Path, origin: Path, reason: ParkReason
) -> None:
    adapter = session_adapter(root, claude, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0

        await adapter.interrupt(run_id, reason)

        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        result = await adapter.result(run_id)
        assert result.failure_reason == session._PARKED[reason]
        assert result.cost_usd == Decimal("0.001"), "hung up: the CLI writes its cost"
        assert credentials_left(root, run_id) == []
        shown = worktree_git(root, run_id, "symbolic-ref", "--short", "HEAD")
        assert shown.stdout.strip() == BRANCH
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_on_a_parked_run_releases_its_branch_and_keeps_its_result(
    root: Path, claude: Path, origin: Path
) -> None:
    """A park accepted without a resume: its worktree must not keep the branch a later fresh
    attempt checks out."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0
        await adapter.interrupt(run_id, ParkReason.IDLE)
        parked = await adapter.result(run_id)

        await adapter.finish(run_id)
        await adapter.finish(run_id)  # idempotent

        assert worktree_git(root, run_id, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        assert await adapter.result(run_id) == parked
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_cancel_hangs_up_and_detaches_head(root: Path, claude: Path, origin: Path) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0

        await adapter.cancel(run_id)

        result = await adapter.result(run_id)
        assert (result.outcome, result.failure_reason) == (
            RunStatus.CANCELLED,
            "cancelled by the operator",
        )
        assert worktree_git(root, run_id, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert credentials_left(root, run_id) == []
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("end", ["interrupt", "finish"])
async def test_an_end_also_ends_what_the_agent_left_in_the_clis_process_group(
    root: Path,
    claude: Path,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end: str,
) -> None:
    """A job that ignores SIGHUP outlives the CLI and keeps its environment, the key included:
    a hangup, and the CLI's own `/exit` alike, end its process group."""
    job = tmp_path / "job"
    monkeypatch.setenv("FAKE_TUI", "job")
    monkeypatch.setenv("FAKE_TUI_JOB", str(job))
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    pid = 0
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        pid = int(job.read_text())
        assert alive(pid)

        if end == "interrupt":
            await adapter.interrupt(run_id)
        else:
            await adapter.finish(run_id)
            assert state_of(root, run_id).exit_code == 0, "the CLI left by its own /exit"

        assert (await adapter.status(run_id)).is_terminal
        await until(lambda: gone(pid), "the job ended", within=5)
    finally:
        if pid and alive(pid):
            os.kill(pid, 9)
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_stop_tmux_refuses_while_the_pane_lives_fails_at_once(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was ended, so the worker is told now and asks again, rather than after the
    two minutes a stop that may have landed is waited for."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)

        async def refused(server: TmuxServer) -> CommandResult:
            return CommandResult(("tmux", "kill-server"), 1, "", "refused")

        with monkeypatch.context() as patched:
            patched.setattr(TmuxServer, "kill", refused)
            with pytest.raises(AdapterUnavailable):
                await asyncio.wait_for(adapter.interrupt(run_id), 5)
        assert not (await adapter.status(run_id)).is_terminal
        assert state_of(root, run_id).stopping is RunStatus.INTERRUPTED

        await adapter.interrupt(run_id)
        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_turnless_resume_relaunches_the_same_session_at_its_prompt_with_fresh_credentials(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    probed: list[str | None] = []

    async def discover(state: HostRunState, env: Mapping[str, str]) -> tuple[()]:
        probed.append(env.get(ENV_GH_TOKEN))
        return ()

    monkeypatch.setattr(adapter, "_discover_artifacts", discover)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        target = await adapter.terminal_target(run_id)
        first = await server_of(root, run_id).display("#{pid}")
        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
        parked = state_of(root, run_id)

        handle = await adapter.resume(run_id, None, fresh_secrets())

        assert (handle.run_id, handle.status) == (run_id, RunStatus.RUNNING)
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert await adapter.terminal_target(run_id) == target
        again = await server_of(root, run_id).display("#{pid}")
        assert again.stdout.strip() != first.stdout.strip(), "a new server"
        argv = json.loads(dump.read_text())["argv"][1:]
        assert argv[argv.index("--resume") + 1] == parked.session_id
        assert "--" not in argv and not any(arg.startswith("Read @") for arg in argv)
        config = run_dir(root, run_id) / "config"
        seed = json.loads((config / CLAUDE_JSON_FILE).read_text())
        assert seed[CLAUDE_JSON_KEY_RESPONSES]["approved"] == [NEW_KEY[-API_KEY_TAIL:]]
        resumed = state_of(root, run_id)
        assert resumed.segment_log_start == parked.log_read > 0
        assert (await adapter.session_state(run_id)).session_id == parked.session_id

        await adapter.finish(run_id)
        result = await adapter.result(run_id)
        assert result.outcome is RunStatus.SUCCEEDED
        assert result.cost_usd == Decimal("0.001"), "the resumed segment cost nothing more"
        assert probed == [TOKEN, NEW_TOKEN], "discovery uses the token the resume was handed"
    finally:
        await ended(adapter, root, run_id)


RUN_BOT = "acme-factorial[bot]"


def _identity(env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        env[key]
        for key in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        )
    )


RUN_IDENTITY = (RUN_BOT, f"{RUN_BOT}@users.noreply.github.com") * 2


@needs_tmux
async def test_the_commit_identity_uses_the_run_s_bot_login(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    secrets = {
        ENV_API_KEY: SecretStr(API_KEY),
        ENV_GH_TOKEN: SecretStr(TOKEN),
        ENV_BOT_LOGIN: SecretStr(RUN_BOT),
    }

    run_id = (await adapter.spawn(session_spec(secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert _identity(json.loads(dump.read_text())["env"]) == RUN_IDENTITY
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_resumed_session_keeps_the_run_s_bot_login(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login the resume's secrets leave out is the one the run kept, for its commits and
    for the comment fence its delivery is probed with."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    fenced: list[str] = []

    async def discover(state: HostRunState, env: Mapping[str, str]) -> tuple[()]:
        fenced.append(env.get(ENV_BOT_LOGIN) or state.bot_login)
        return ()

    monkeypatch.setattr(adapter, "_discover_artifacts", discover)
    secrets = {
        ENV_API_KEY: SecretStr(API_KEY),
        ENV_GH_TOKEN: SecretStr(TOKEN),
        ENV_BOT_LOGIN: SecretStr(RUN_BOT),
    }
    run_id = (await adapter.spawn(session_spec(secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
        dump.unlink()

        await adapter.resume(run_id, None, fresh_secrets())

        await reaches(adapter, run_id, RunStatus.IDLE)
        assert _identity(json.loads(dump.read_text())["env"]) == RUN_IDENTITY
        await adapter.finish(run_id)
        await adapter.result(run_id)
        assert fenced[-1] == RUN_BOT
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_resume_turn_with_images_points_at_them(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    image = RunAttachment(
        name="screenshot-1",
        media_type="image/png",
        content=PNG,
        source="ticket_comments",
        origin="https://example.com/s.png",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)

        await adapter.resume(run_id, "Match the screenshot.", fresh_secrets(), (image,))

        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        turn = here / "turns" / "1.md"
        assert turn.stat().st_mode & 0o777 == 0o600
        assert turn.read_text().startswith("Match the screenshot.\n")
        pointer = json.loads(dump.read_text())["argv"][-1]
        assert f"@{turn}" in pointer
        assert f"@{here}/attachments/" in pointer
        assert state_of(root, run_id).segment_turn
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_resume_ends_the_inspect_shell_before_it_writes_a_credential(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = session_adapter(root, claude, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    seen: list[list[str]] = []
    real = adapter._end_inspect

    async def end_inspect(ended_id: str) -> None:
        seen.append(credentials_left(root, ended_id))
        await real(ended_id)

    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        monkeypatch.setattr(adapter, "_end_inspect", end_inspect)

        await adapter.resume(run_id, None, {**fresh_secrets(), **secrets})

        await reaches(adapter, run_id, RunStatus.IDLE)
        assert seen == [[]]
        assert credentials_left(root, run_id) == [CLAUDE_JSON_FILE, MCP_CONFIG_FILE]
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_resume_that_cannot_start_tmux_deletes_what_it_wrote_and_stays_interrupted(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = session_adapter(root, claude, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)

        async def refused(server: TmuxServer, cwd: Path, argv: list[str]) -> CommandResult:
            return CommandResult(("tmux",), 1, "", "no server")

        monkeypatch.setattr(TmuxServer, "start", refused)
        with pytest.raises(AdapterUnavailable):
            await adapter.resume(run_id, "Go on.", {**fresh_secrets(), **secrets})

        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        assert credentials_left(root, run_id) == []
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_symlink_the_agent_plants_is_replaced_by_a_resume_never_written_through(
    root: Path, claude: Path, origin: Path, tmp_path: Path
) -> None:
    """The agent writes the run area as the worker's own user: a link it leaves where a resume
    writes a credential would otherwise carry the credential, or the conf, wherever it points."""
    adapter = session_adapter(root, claude, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        here = run_dir(root, run_id)
        planted = [
            here / "config" / name
            for name in (CLAUDE_JSON_FILE, MCP_CONFIG_FILE, "settings.json", ENV_FILE)
        ]
        planted += [here / "turns" / "1.md", root / tmux.CONF_FILE, root / tmux.SESSION_CONF_FILE]
        targets = tmp_path / "targets"
        targets.mkdir()
        for n, link in enumerate(planted):
            target = targets / str(n)
            target.write_text("the agent's\n")
            link.unlink(missing_ok=True)
            link.symlink_to(target)

        await adapter.resume(run_id, "Go on.", {**fresh_secrets(), **secrets})

        await reaches(adapter, run_id, RunStatus.IDLE)
        for n, link in enumerate(planted):
            assert (targets / str(n)).read_text() == "the agent's\n", link
            assert not link.is_symlink(), link
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_turnless_resume_whose_human_turn_got_no_answer_still_warns_of_it(
    root: Path, claude: Path, origin: Path
) -> None:
    """A turnless segment owes no turn only while nobody typed one."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
        await adapter.resume(run_id, None, fresh_secrets())
        await reaches(adapter, run_id, RunStatus.IDLE)

        await typed(root, run_id, "SLOWME\r")
        await reaches(adapter, run_id, RunStatus.RUNNING)
        await typed(root, run_id, "/exit\r")
        await adapter.drain()

        result = await adapter.result(run_id)
        assert (result.outcome, state_of(root, run_id).exit_code) == (RunStatus.INTERRUPTED, 0)
        assert NO_TURN in result.stream_warnings
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_session_a_person_quits_parks_and_resumes_in_its_workspace(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TUI never exits on its own, so a clean exit nobody asked for is a person's quit: the
    run parks with its branch kept, and a resume brings the session back where it was."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0
        launched_in = json.loads(dump.read_text())["cwd"]

        await typed(root, run_id, "/exit\r")
        await adapter.drain()

        parked = state_of(root, run_id)
        assert (parked.status, parked.park_reason, parked.exit_code) == (
            RunStatus.INTERRUPTED,
            ParkReason.EXITED,
            0,
        )
        result = await adapter.result(run_id)
        assert result.park_reason is ParkReason.EXITED
        assert result.failure_reason == session._PARKED[ParkReason.EXITED]
        shown = worktree_git(root, run_id, "symbolic-ref", "--short", "HEAD")
        assert shown.stdout.strip() == BRANCH

        handle = await adapter.resume(run_id, None, fresh_secrets())

        assert (handle.run_id, handle.status) == (run_id, RunStatus.RUNNING)
        await reaches(adapter, run_id, RunStatus.IDLE)
        relaunch = json.loads(dump.read_text())
        assert "--resume" in relaunch["argv"]
        assert relaunch["cwd"] == launched_in
        assert state_of(root, run_id).worktree == parked.worktree
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("verb", ["finish", "interrupt"])
async def test_a_run_that_ends_while_a_stop_reads_its_state_stays_ended(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """The human's own `/exit` lands as the worker sends its stop: a state read before the run
    was finalised must never be written back over its result."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    real = session.read_state
    held = threading.Event()

    def read_then_hold(folder: Path, read_id: str) -> HostRunState:
        state = real(folder, read_id)
        if threading.current_thread() is not threading.main_thread() and not held.is_set():
            held.set()
            for _ in range(200):
                if real(folder, read_id).status.is_terminal:
                    break
                time.sleep(0.05)
        return state

    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        monkeypatch.setattr(session, "read_state", read_then_hold)
        await typed(root, run_id, "/exit\r")

        with contextlib.suppress(AgentRuntimeError):
            await getattr(adapter, verb)(run_id)
        await adapter.drain()

        assert state_of(root, run_id).status.is_terminal
        assert (await adapter.result(run_id)).session_id is not None
    finally:
        await ended(adapter, root, run_id)


class DeadPane(TmuxServer):
    """A server whose one pane is in the state the test sets, and whose kill always lands."""

    shown: tmux.PaneState | None = None

    async def pane(self) -> tmux.PaneState | None:
        return type(self).shown

    async def kill(self) -> CommandResult:
        return CommandResult(("tmux", "kill-server"), 0, "", "")


@pytest.mark.parametrize(
    ("shown", "seen", "signalled"),
    [
        (tmux.PaneState(dead=True, status=0, pid=4242), 4242, True),
        (tmux.PaneState(dead=True, status=0, pid=4242), None, False),
        (tmux.PaneState(dead=True, status=0, pid=4242), 4243, False),
        (tmux.PaneState(dead=False, status=None, pid=1), 1, False),
        (tmux.PaneState(dead=False, status=None, pid=0), None, False),
    ],
)
async def test_a_hangup_signals_only_a_process_group_this_worker_saw_live(
    monkeypatch: pytest.MonkeyPatch, shown: tmux.PaneState, seen: int | None, signalled: bool
) -> None:
    """A dead pane's pid is free for any process to take, and `killpg` of 0 or 1 reaches the
    worker's own group or every process it may signal."""
    sent: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(session.os, "killpg", killpg)
    monkeypatch.setattr(session, "GRACE", 0.2)
    DeadPane.shown = shown
    server = DeadPane("tmux", "/tmp/x", Path("/tmp/c"), Path("/tmp"), "run")

    await session._hangup(server, seen=seen)

    assert bool(sent) is signalled
    assert all(pgid == 4242 for pgid, _ in sent)


def test_deleting_credentials_also_deletes_a_half_written_one(tmp_path: Path) -> None:
    """A worker killed inside `write_new` leaves its temporary file, every secret in it."""
    run = tmp_path / "runs" / "x"
    config = run / "config"
    config.mkdir(parents=True)
    left = [f".{ENV_FILE}.0a1b2c3d", f".{CLAUDE_JSON_FILE}.0a1b2c3d", MCP_CONFIG_FILE]
    for name in [*left, ".settings.json.0a1b2c3d"]:
        (config / name).write_text(API_KEY)

    delete_credentials(tmp_path, run, claude_session.SESSION_CREDENTIALS)

    assert sorted(path.name for path in config.iterdir()) == [".settings.json.0a1b2c3d"]


# ------------------------------------------------------------------ a worker restart

DRIVER = Path(__file__).with_name("host_session_driver.py")


async def restarted(adapter: ClaudeCliAdapter) -> None:
    """A worker restart, in process: every supervisor stops where it stands, its lock let go."""
    tasks = list(adapter._supervisors.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def socket_left(root: Path, run_id: str) -> bool:
    return os.path.lexists(server_of(root, run_id).socket)


def mirrored(root: Path, run_id: str) -> list[str]:
    return (run_dir(root, run_id) / LOG_FILE).read_text().splitlines()


def said(lines: list[str], text: str) -> int:
    """How many mirrored assistant lines carry `text`."""
    return sum(1 for line in lines if json.loads(line).get("type") == "assistant" and text in line)


async def idle_after(adapter: ClaudeCliAdapter, root: Path, run_id: str, prompts: int) -> None:
    async def answered() -> bool:
        status = await adapter.status(run_id)
        return status is RunStatus.IDLE and state_of(root, run_id).signals.prompts == prompts

    await until(answered, f"turn {prompts} answered")


@pytest.fixture
def decoy() -> Iterator[subprocess.Popen[bytes]]:
    """A process in a group of its own, standing for whatever holds a recorded pid by now."""
    process = subprocess.Popen(["/bin/sleep", "1000"], start_new_session=True)
    yield process
    process.kill()
    process.wait()


@needs_tmux
async def test_a_new_adapter_re_adopts_a_live_session_without_repeating_a_line(
    root: Path, claude: Path, origin: Path
) -> None:
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    second = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        session_id = state_of(root, run_id).session_id
        await restarted(first)

        assert await second.status(run_id) is RunStatus.IDLE
        await typed(root, run_id, "the second turn\r")
        await idle_after(second, root, run_id, 2)
        await second.finish(run_id)

        result = await second.result(run_id)
        assert (result.outcome, result.session_id) == (RunStatus.SUCCEEDED, session_id)
        lines = mirrored(root, run_id)
        assert len(lines) == len(set(lines))
        assert (said(lines, "call 1 for: Read @"), said(lines, "the second turn")) == (1, 1)
        assert [json.loads(line)["type"] for line in lines].count("result") == 1
        assert result.cost_usd == Decimal("0.002")
    finally:
        await ended(second, root, run_id)


@needs_tmux
async def test_live_usage_survives_a_worker_restart(root: Path, claude: Path, origin: Path) -> None:
    """The state file carries the figure the worker prices while the session runs; a new worker
    reads it before its first poll, and goes on without counting a message twice."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    second = session_adapter(root, claude, origin)

    async def counted(messages: int) -> None:
        usage = RunUsage(input_tokens=10 * messages, output_tokens=10 * messages)

        async def reached() -> bool:
            return (await second.session_state(run_id)).usage == usage

        # A message is counted once a later line closes it, which can follow the Stop hook.
        await until(reached, f"{messages} messages counted")

    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await counted(1)
        await restarted(first)

        assert (await second.session_state(run_id)).usage == RunUsage(
            input_tokens=10, output_tokens=10
        )
        assert await second.status(run_id) is RunStatus.IDLE
        await typed(root, run_id, "the second turn\r")
        await idle_after(second, root, run_id, 2)
        await counted(2)
        await second.interrupt(run_id)
        await second.resume(run_id, "a third turn", fresh_secrets())
        await reaches(second, run_id, RunStatus.IDLE)
        await counted(3)
    finally:
        await ended(second, root, run_id)


@needs_tmux
async def test_a_worker_killed_mid_session_leaves_tmux_running_and_the_next_one_re_adopts(
    root: Path, claude: Path, origin: Path
) -> None:
    driver = await asyncio.create_subprocess_exec(
        sys.executable,
        str(DRIVER),
        "serve",
        str(root),
        "--claude",
        str(claude),
        "--origin",
        str(origin),
        stdout=subprocess.PIPE,
    )
    assert driver.stdout is not None
    run_id = (await asyncio.wait_for(driver.stdout.readline(), 30)).decode().strip()
    adapter = session_adapter(root, claude, origin)
    try:

        async def idle() -> bool:
            return state_of(root, run_id).status is RunStatus.IDLE

        await until(idle, "the driver's run idle")
        assert await adapter.status(run_id) is RunStatus.IDLE
        assert adapter._attached(run_id) is None, "the driver's supervisor holds the run"
        before = await server_of(root, run_id).pane()
        assert before is not None and not before.dead

        driver.kill()
        await driver.wait()

        assert await server_of(root, run_id).pane() == before, "tmux outlives the worker"
        assert await adapter.status(run_id) is RunStatus.IDLE
        assert adapter._attached(run_id) is not None, "taken back"
        await adapter.finish(run_id)
        assert (await adapter.result(run_id)).outcome is RunStatus.SUCCEEDED
    finally:
        if driver.returncode is None:
            driver.kill()
            await driver.wait()
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("restart", [False, True])
async def test_a_lost_tmux_server_fails_the_run_host_lost_and_keeps_its_workspace(
    root: Path, claude: Path, origin: Path, restart: bool
) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0
        if restart:
            await restarted(adapter)
            adapter = session_adapter(root, claude, origin)

        await server_of(root, run_id).kill()
        await adapter.status(run_id)
        await adapter.drain()

        result = await adapter.result(run_id)
        assert result.outcome is RunStatus.FAILED
        assert result.failure_reason is not None
        assert result.failure_reason.startswith("host_lost")
        assert (run_dir(root, run_id) / "worktree").is_dir()
        assert credentials_left(root, run_id) == []
        assert worktree_git(root, run_id, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert json.loads(mirrored(root, run_id)[-1])["type"] == "result"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize(
    ("in_flight", "outcome"),
    [
        ({"finishing": True}, RunStatus.SUCCEEDED),
        (
            {"stopping": RunStatus.INTERRUPTED, "park_reason": ParkReason.ROLLOVER},
            RunStatus.INTERRUPTED,
        ),
        ({"stopping": RunStatus.CANCELLED}, RunStatus.CANCELLED),
    ],
)
async def test_a_stop_in_flight_when_the_server_died_keeps_its_outcome(
    root: Path, claude: Path, origin: Path, in_flight: dict[str, object], outcome: RunStatus
) -> None:
    """The worker persisted the stop, then died with the host before the hangup landed."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    adapter = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        edit_state(root, run_id, **in_flight)
        await server_of(root, run_id).kill()

        await adapter.status(run_id)
        await adapter.drain()

        assert (await adapter.result(run_id)).outcome is outcome
        assert credentials_left(root, run_id) == []
        if outcome is RunStatus.INTERRUPTED:
            await adapter.resume(run_id, None, fresh_secrets())
            await reaches(adapter, run_id, RunStatus.IDLE)
            assert await server_of(root, run_id).pane() is not None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_one_supervisor_reads_a_session_across_adapters(
    root: Path, claude: Path, origin: Path
) -> None:
    """Two adapters on one root, as two runtime rows would make: the lock leaves the second
    reading the state the first writes. The driver test above holds it across processes."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    second = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)

        assert await second.status(run_id) is RunStatus.IDLE
        assert second._attached(run_id) is None
        await typed(root, run_id, "the second turn\r")
        await idle_after(second, root, run_id, 2)
        await first.finish(run_id)

        lines = mirrored(root, run_id)
        assert len(lines) == len(set(lines))
        assert said(lines, "the second turn") == 1
        assert await second.status(run_id) is RunStatus.SUCCEEDED
    finally:
        await ended(first, root, run_id)
        await second.drain()


@needs_tmux
async def test_a_torn_poll_is_recovered_by_taking_the_session_back_without_a_duplicate(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirrors were appended, then the state that counts them could not be written."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        torn: list[int] = []

        def fails_once(folder: Path, state: HostRunState) -> None:
            # Under load the prompt hook's line can be polled alone, before the turn's log lines:
            # tearing that save would leave the translated mirror with nothing to duplicate.
            if not torn and state.offset > state_of(root, run_id).offset:
                torn.append(state.offset)
                raise OSError("no space left on device")
            write_state(folder, state)

        with monkeypatch.context() as patched:
            patched.setattr(session, "_write_state", fails_once)
            await typed(root, run_id, "the second turn\r")

            async def tore() -> bool:
                return adapter._attached(run_id) is None

            await until(tore, "the poll torn")
        assert (run_dir(root, run_id) / LOG_FILE).stat().st_size > state_of(root, run_id).offset

        await idle_after(adapter, root, run_id, 2)
        await adapter.finish(run_id)

        lines = mirrored(root, run_id)
        assert len(lines) == len(set(lines))
        assert said(lines, "the second turn") == 1
        assert (await adapter.result(run_id)).outcome is RunStatus.SUCCEEDED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_translator_change_restarts_the_mirror_at_the_raw_offset(
    root: Path, claude: Path, origin: Path
) -> None:
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    adapter = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        edit_state(root, run_id, transcript_version=0)
        before = mirrored(root, run_id)

        assert await adapter.status(run_id) is RunStatus.IDLE
        assert mirrored(root, run_id) == before, "nothing replayed"
        assert TRANSLATOR_CHANGED in state_of(root, run_id).stream_warnings
        assert state_of(root, run_id).transcript_version == TRANSCRIPT_VERSION
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_server_on_the_runs_socket_that_is_not_its_launch_is_lost_not_adopted(
    root: Path, claude: Path, origin: Path
) -> None:
    """The socket is keyed by the run, but any process of the worker's user can serve on it."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    adapter = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        server = server_of(root, run_id)
        await server.kill()
        assert (await server.start(root, ["/bin/sleep", "1000"])).ok

        await adapter.status(run_id)
        await asyncio.wait_for(adapter.drain(), 20)

        result = await adapter.result(run_id)
        assert result.failure_reason is not None
        assert result.failure_reason.startswith("host_lost")
    finally:
        await server_of(root, run_id).kill()
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_pane_that_died_while_no_worker_ran_never_has_its_recorded_pid_signalled(
    root: Path,
    claude: Path,
    origin: Path,
    decoy: subprocess.Popen[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI exited between workers, so its pid is free: a supervisor taken back never saw it
    live, and the group that holds it by now is someone else's."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    adapter = session_adapter(root, claude, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        real = TmuxServer.pane

        async def died(self: TmuxServer) -> tmux.PaneState | None:
            shown = await real(self)
            return shown and tmux.PaneState(dead=True, status=0, pid=decoy.pid)

        monkeypatch.setattr(TmuxServer, "pane", died)

        await adapter.status(run_id)
        await asyncio.wait_for(adapter.drain(), 20)

        assert state_of(root, run_id).status.is_terminal
        assert decoy.poll() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_startup_sweep_ends_a_finished_runs_stray_server_and_deletes_its_credentials(
    root: Path, claude: Path, origin: Path, decoy: subprocess.Popen[bytes]
) -> None:
    first = session_adapter(root, claude, origin)
    stray = (await first.spawn(session_spec())).run_id
    live = (await first.spawn(session_spec())).run_id
    try:
        await reaches(first, stray, RunStatus.IDLE)
        await reaches(first, live, RunStatus.IDLE)
        await restarted(first)
        edit_state(root, stray, status=RunStatus.CANCELLED)
        assert credentials_left(root, stray) == [CLAUDE_JSON_FILE]

        assert session_adapter(root, claude, origin).sweep_credentials() == 1

        assert await server_of(root, stray).pane() is None
        assert not socket_left(root, stray), "no socket left to visit again"
        assert credentials_left(root, stray) == []
        assert decoy.poll() is None
        pane = await server_of(root, live).pane()
        assert pane is not None and not pane.dead
        assert credentials_left(root, live) == [CLAUDE_JSON_FILE]
    finally:
        for run_id in (stray, live):
            await ended(first, root, run_id)


@needs_tmux
async def test_the_reaper_removes_a_sessions_raw_mirrors_with_its_config_home(
    root: Path, claude: Path, origin: Path
) -> None:
    clock = Clock()
    adapter = session_adapter(root, claude, origin, now=clock)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.finish(run_id)
        folder = run_dir(root, run_id)
        raw = ("session.jsonl", "signals.mirror.jsonl", "signals.jsonl")
        assert all((folder / name).exists() for name in raw)
        clock.ahead = timedelta(hours=1)

        assert await adapter.reap((), retain=None) == (run_id,)

        assert [name for name in raw if (folder / name).exists()] == []
        assert (folder / LOG_FILE).exists() and (folder / STATE_FILE).exists()
        assert not socket_left(root, run_id)
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_startup_sweep_ends_a_session_whose_spawn_died_before_its_state(
    root: Path, claude: Path, origin: Path
) -> None:
    """The worker died between the server's start and the first state write: a CLI holding the
    key, under a run directory nothing else would ever read."""
    first = session_adapter(root, claude, origin)
    run_id = (await first.spawn(session_spec())).run_id
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        (run_dir(root, run_id) / STATE_FILE).unlink()
        assert credentials_left(root, run_id) == [CLAUDE_JSON_FILE]

        assert session_adapter(root, claude, origin).sweep_credentials() == 1

        assert await server_of(root, run_id).pane() is None
        assert not socket_left(root, run_id)
        assert credentials_left(root, run_id) == []
    finally:
        await server_of(root, run_id).kill()


# ------------------------------------------------------------------ spawn properties, on a session

INDEX_TOKEN = "index-token"
MCP_DOCS = MCP_HTTP.model_copy(update={"tools_allow": ("search_docs",)})
MCP_STDIO = McpServerSpec(
    name="repo-index",
    transport=McpTransport.STDIO,
    command="npx",
    args=("-y", "@acme/repo-index-mcp@1.4.2"),
    env={"REPO_INDEX_TOKEN": "{secrets.REPO_INDEX_TOKEN}"},
)
MCP_SECRETS = {
    "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
    "REPO_INDEX_TOKEN": SecretStr(INDEX_TOKEN),
}


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


async def test_no_local_root_refuses_a_session(
    claude: Path, origin: Path, tmp_path: Path
) -> None:
    """The guard that makes the blast radius knowable. No default, ever."""
    adapter = ClaudeCliAdapter(cli=OriginCli(claude, origin))
    before = sorted(os.listdir(tmp_path))

    with pytest.raises(AdapterUnavailable, match="LOCAL_ROOT"):
        await adapter.spawn(session_spec())

    assert sorted(os.listdir(tmp_path)) == before, "refused before anything was created"


@needs_tmux
async def test_a_ticket_key_cannot_escape_local_root_on_a_session(
    root: Path, claude: Path, origin: Path
) -> None:
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec(ticket="../../etc/passwd"))).run_id
    try:
        assert run_dir(root, run_id).is_dir()
        assert run_dir(root, run_id).resolve().parent == root / "runs"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_minted_token_reaches_the_adapters_git_by_env_never_argv(
    root: Path, claude: Path, origin: Path
) -> None:
    """The clone, the fetch and the delivery probes are argv a process table shows anyone on
    the host, so the token goes only by the env-only credential helper."""
    cli = OriginCli(claude, origin)
    adapter = session_adapter(root, claude, origin, cli=cli)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.cancel(run_id)
    finally:
        await ended(adapter, root, run_id)

    assert any(command[0] == cli.git_bin for command in cli.commands)
    assert [command for command in cli.commands if any(TOKEN in arg for arg in command)] == []


async def test_a_host_exported_key_never_stands_in_for_the_store_on_a_session(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_API_KEY, HOST_KEY)

    with pytest.raises(SpawnError) as raised:
        await session_adapter(root, claude, origin).spawn(
            session_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)})
        )

    assert raised.value.reason is SpawnReason.CREDENTIAL


@needs_tmux
@pytest.mark.parametrize("servers", ["none", "both"])
async def test_a_sessions_mcp_config_is_private_outside_the_worktree_and_strict(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    servers: str,
) -> None:
    """`--strict-mcp-config` alone means no server: the target repo's own `.mcp.json` never
    applies. The file holds rendered credentials, and the worktree is a checkout the agent
    pushes. `--resume` restores only the conversation, so a resume names every flag again."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    adapter = session_adapter(root, claude, origin)
    mcp = (MCP_DOCS, MCP_STDIO) if servers == "both" else ()
    secrets = {**session_spec().secrets, **MCP_SECRETS}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=mcp))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        argv = json.loads(dump.read_text())["argv"][1:]
        assert "--strict-mcp-config" in argv
        if servers == "none":
            assert "--mcp-config" not in argv
        else:
            path = argv[argv.index("--mcp-config") + 1]
            assert os.stat(path).st_mode & 0o777 == 0o600
            assert not path.startswith(f"{state_of(root, run_id).worktree}/")
            config = _read_json(path)["mcpServers"]
            assert config["product-docs"] == {
                "type": "http",
                "url": "https://mcp.internal.example.com/product-docs/mcp",
                "headers": {"Authorization": f"Bearer {DOCS_TOKEN}"},
            }
            assert config["repo-index"] == {
                "command": "npx",
                "args": ["-y", "@acme/repo-index-mcp@1.4.2"],
                "env": {"REPO_INDEX_TOKEN": INDEX_TOKEN},
            }
            allowed = argv[argv.index("--allowed-tools") + 1 :]
            assert "mcp__product-docs__search_docs" in allowed
            assert "mcp__repo-index" in allowed
            assert DOCS_TOKEN not in " ".join(argv)
            assert INDEX_TOKEN not in " ".join(argv)

        await adapter.interrupt(run_id, ParkReason.USER)
        await adapter.resume(run_id, None, {**fresh_secrets(), **MCP_SECRETS})
        await reaches(adapter, run_id, RunStatus.IDLE)

        resumed = json.loads(dump.read_text())["argv"][1:]
        assert "--resume" in resumed
        repeated = ["--model", "--system-prompt-file", "--strict-mcp-config"]
        if servers == "both":
            repeated.append("--mcp-config")
            assert _read_json(resumed[resumed.index("--mcp-config") + 1])["mcpServers"]
        for flag in repeated:
            assert flag in resumed, f"{flag} missing from the resume argv"
            if flag != "--strict-mcp-config":
                assert resumed[resumed.index(flag) + 1] == argv[argv.index(flag) + 1]
    finally:
        await ended(adapter, root, run_id)


async def test_an_unstorable_mcp_secret_refuses_a_session_before_anything_exists(
    root: Path, claude: Path, origin: Path
) -> None:
    """An `Authorization: Bearer {secrets.X}` header going out unrendered is worse than none."""
    with pytest.raises(SpawnError, match="PRODUCT_DOCS_TOKEN"):
        await session_adapter(root, claude, origin).spawn(session_spec(mcp=(MCP_HTTP,)))

    assert os.listdir(root) == []


@needs_tmux
async def test_a_sessions_prompt_and_skills_land_outside_the_worktree(
    root: Path, claude: Path, origin: Path
) -> None:
    """A SKILL.md inside the checkout is something the agent can commit into the target repo."""
    adapter = session_adapter(root, claude, origin)
    skill = AgentSkill(name="spec-format", content="# Spec format\n", scope=SkillScope.SHARED)
    run_id = (await adapter.spawn(session_spec(skills=(skill,)))).run_id
    try:
        here = run_dir(root, run_id)
        assert (here / "system-prompt.md").read_text() == "You plan."
        assert (here / "config" / "skills" / "spec-format" / "SKILL.md").exists()
        assert not list((here / "worktree").rglob("SKILL.md"))
    finally:
        await ended(adapter, root, run_id)


def _pin_config(origin: Path, folder: Path) -> str:
    """A commit no branch holds, as a pinned config commit can be. Returns its sha."""
    work = folder / "pinned"
    git("clone", str(origin), str(work), cwd=folder)
    (work / ".factorial").mkdir()
    (work / ".factorial" / "process.yaml").write_text("specVersion: factorial/v1\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "config", cwd=work)
    git("push", "origin", "HEAD:refs/pinned/config", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


@needs_tmux
@pytest.mark.parametrize("pinned", [True, False])
async def test_a_pinned_config_commit_is_exported_and_named_in_the_sessions_turn(
    root: Path,
    claude: Path,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned: bool,
) -> None:
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    sha = _pin_config(origin, tmp_path) if pinned else None
    workspace = Workspace(repo=REPO, base="main", branch="spec/12-{slug}", config_sha=sha)
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec(workspace=workspace))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        env = json.loads(dump.read_text())["env"]
        turn = (run_dir(root, run_id) / "turns" / "0.md").read_text()
        if sha is None:
            assert ENV_CONFIG_SHA not in env
            assert "git show " not in turn
        else:
            assert env[ENV_CONFIG_SHA] == sha
            assert f"git show {sha}:.factorial/" in turn
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_no_secret_is_left_in_the_run_directory_after_a_session_ends(
    root: Path, claude: Path, origin: Path
) -> None:
    adapter = session_adapter(root, claude, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.finish(run_id)
        assert await adapter.status(run_id) is RunStatus.SUCCEEDED

        for value in (API_KEY[-API_KEY_TAIL:], TOKEN, DOCS_TOKEN):
            assert files_holding(root, value) == set()
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_credential_none_session_receives_no_github_credential_even_one_the_host_exports(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge over the host cannot delete a host-exported GH_TOKEN: the environment is
    constructed. The assertion names keys only, never the values it guards."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv(ENV_GH_TOKEN, "ghs_host_exported_token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_host_exported_token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.https://github.com.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!echo password=leaked")
    workspace = Workspace(repo=REPO, base="main", branch="impl/12-{slug}", credential="none")
    # The constructed environment drops the host's FAKE_TUI_DUMP too; a declared secret stays.
    secrets = {**session_spec().secrets, "FAKE_TUI_DUMP": SecretStr(str(dump))}
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec(workspace=workspace, secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        env = json.loads(dump.read_text())["env"]
        leaked = sorted(
            key
            for key in env
            if key in {ENV_GH_TOKEN, "GITHUB_TOKEN"} or key.startswith("GIT_CONFIG")
        )
        assert leaked == []
        assert env[ENV_API_KEY] == API_KEY
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_subscription_session_carries_the_token_and_never_the_key_across_a_resume(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set ANTHROPIC_API_KEY outranks the token in the CLI, so a key from any source must be
    absent, not blank: the host's export, the project store's, on the spawn and the resume."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    monkeypatch.setenv(ENV_API_KEY, HOST_KEY)
    adapter = session_adapter(root, claude, origin, auth_mode=AUTH_MODE_SUBSCRIPTION)
    secrets = {**session_spec().secrets, SECRET_OAUTH_TOKEN: SecretStr(OAUTH)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        env = json.loads(dump.read_text())["env"]
        assert env[ENV_OAUTH_TOKEN] == OAUTH
        assert ENV_API_KEY not in env

        await adapter.interrupt(run_id)
        resumed = "sk-ant-oat01-resumed"
        await adapter.resume(
            run_id, None, {**fresh_secrets(), SECRET_OAUTH_TOKEN: SecretStr(resumed)}
        )
        await reaches(adapter, run_id, RunStatus.IDLE)

        env = json.loads(dump.read_text())["env"]
        assert env[ENV_OAUTH_TOKEN] == resumed
        assert ENV_API_KEY not in env
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_subscription_session_refuses_without_the_token_on_spawn_and_resume(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store, and only the store: a host-exported token and the other mode's key are both
    present here and neither counts. The message names the whole fix."""
    monkeypatch.setenv(ENV_OAUTH_TOKEN, "sk-ant-oat01-from-the-host")
    adapter = session_adapter(root, claude, origin, auth_mode=AUTH_MODE_SUBSCRIPTION)

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec())
    assert raised.value.reason is SpawnReason.CREDENTIAL
    for named in (SECRET_OAUTH_TOKEN, "Runtime page", "claude setup-token"):
        assert named in str(raised.value)

    secrets = {**session_spec().secrets, SECRET_OAUTH_TOKEN: SecretStr(OAUTH)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)

        with pytest.raises(SpawnError) as raised:
            await adapter.resume(run_id, None, fresh_secrets())
        assert raised.value.reason is SpawnReason.CREDENTIAL
        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_turn_cap_holds_across_a_session_resume(
    root: Path, claude: Path, origin: Path
) -> None:
    """Counting only the resumed segment would let a run escape its cap by being stopped and
    started."""
    adapter = session_adapter(root, claude, origin)
    run_id = (await adapter.spawn(session_spec(max_turns=2))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        await adapter.resume(run_id, "go on", fresh_secrets())
        await idle_after(adapter, root, run_id, 1)

        await typed(root, run_id, "more\r")
        await reaches(adapter, run_id, RunStatus.FAILED)

        result = await adapter.result(run_id)
        assert "Stopped after 2 turns" in (result.failure_reason or "")
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_session_spawn_attaches_its_images_from_outside_the_worktree_and_an_end_removes_it(
    root: Path, claude: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screenshot can hold what the ticket should not keep on the host."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    image = RunAttachment(
        name="image-1",
        media_type="image/png",
        content=PNG,
        source="ticket_body",
        origin="https://example.com/s.png",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    missing = MissingAttachment(
        source="ticket_comments",
        origin="https://github.com/user-attachments/files/1/spec.pdf",
        reason="not an image",
    )
    adapter = session_adapter(root, claude, origin)
    spec = session_spec(attachments=(image,), missing_attachments=(missing,))
    run_id = (await adapter.spawn(spec)).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        assert f"@{here}/attachments/" in json.loads(dump.read_text())["argv"][-1]
        [saved] = (here / "attachments").iterdir()
        assert saved.read_bytes() == PNG
        assert files_holding(here / "worktree", "-not-really") == set()
        turn = (here / "turns" / "0.md").read_text()
        assert f"{missing.origin} (ticket_comments): not an image" in turn

        await adapter.finish(run_id)

        assert not (here / "attachments").exists()
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_startup_sweep_reads_the_process_table_once_for_every_ended_session_home(
    root: Path, claude: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run dirs stay for ever, so each startup must not scan the table once per run; and a home
    the reaper removed holds nothing to look for."""
    adapter = session_adapter(root, claude, origin)
    runs = [(await adapter.spawn(session_spec())).run_id for _ in range(3)]
    try:
        for run_id in runs:
            await reaches(adapter, run_id, RunStatus.IDLE)
            await adapter.finish(run_id)
        shutil.rmtree(run_dir(root, runs[2]) / "config")
        asked: list[list[str]] = []

        def marked_any(key: str, values: Iterable[str]) -> dict[str, set[int]]:
            asked.append(list(values))
            return {value: set() for value in asked[-1]}

        monkeypatch.setattr(escaped, "marked_any", marked_any)

        await asyncio.to_thread(session_adapter(root, claude, origin).sweep_credentials)

        assert [sorted(values) for values in asked] == [
            sorted(str(run_dir(root, run_id) / "config") for run_id in runs[:2])
        ]
    finally:
        for run_id in runs:
            await server_of(root, run_id).kill()
        await adapter.drain()
