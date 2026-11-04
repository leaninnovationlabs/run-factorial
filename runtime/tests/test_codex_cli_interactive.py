"""The codex_cli adapter's interactive path on the host (decision 56): the refusal with codex's
profile, the run state a session adds to, a session run end to end, the spawn's own properties
(its credentials, skills, MCP servers and images), and the sandbox check every launch asks first.
The claude profile's finish, as the extraction of the host session from claude_cli left it.

The refusal tests run stand-in `codex` and `tmux` scripts. The session tests run
`fake_codex_tui.py` in a real tmux, against a local bare origin, with roots from
`mkdtemp(dir="/tmp")`: pytest's tmp_path on macOS is too long for a socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from host_session_support import (
    REPO,
    CodexOriginCli,
    bare_origin,
    fake_tui_settings_reach_sessions,  # noqa: F401
    git,
    reaches,
    run_dir,
    server_of,
    short_root,
    to_origin,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import host_session as session
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.host_session import HostSession
from runtime.adapters._cli.launch import ENV_FILE, launch_prefix
from runtime.adapters._cli.process import CommandResult
from runtime.adapters._cli.session_state import (
    LOG_FILE,
    STATE_FILE,
    HostRunState,
    read_state,
    write_state,
)
from runtime.adapters._cli.tmux import PaneState, TmuxServer
from runtime.adapters.claude_cli.session import ClaudeTui
from runtime.adapters.codex_cli import session as codex_session
from runtime.adapters.codex_cli import spec as codex_spec
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_SUBSCRIPTION,
    SECRET_AUTH_JSON,
)
from runtime.adapters.codex_cli.cli import (
    AUTH_FILE,
    CONFIG_FILE,
    ENV_API_KEY,
    ENV_CODEX_HOME,
    PINNED_VERSION,
    SUBCMD_SANDBOX,
    VALUE_TRUST_UNTRUSTED,
    _Cli,
)
from runtime.adapters.codex_cli.session import CodexTui
from runtime.adapters.codex_cli.spec import bearer_env_var
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    AgentRuntimeError,
    AgentSkill,
    ArtifactKind,
    IdleReason,
    InspectUnavailable,
    McpServerSpec,
    McpTransport,
    ParkReason,
    ResumeFrom,
    RunAttachment,
    RunSpec,
    RunStatus,
    SkillScope,
    SpawnError,
    SpawnReason,
    Workspace,
)
from runtime.terminal import HostTmuxRelay

PIN = f"codex-cli {PINNED_VERSION}"


class CodexHost(HostSession):
    """The host session with codex's profile, as `CodexCliAdapter` will mix it in."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cli = _Cli()
        self._tui = CodexTui()


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Host:
    """A PATH holding a stand-in `codex` and `tmux`; the codex logs each run to `calls` and the
    environment it ran with to `env`."""

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = folder / "bin"
        self.calls = folder / "calls"
        self.env = folder / "env"
        self.codex(PIN)
        _script(self.bin / "tmux", "#!/bin/sh\necho 'tmux 3.5a'\n")
        monkeypatch.setenv("PATH", str(self.bin))

    def codex(self, version: str, *, status: int = 0) -> Path:
        return _script(
            self.bin / "codex",
            f"#!/bin/sh\necho run >> '{self.calls}'\n/usr/bin/env > '{self.env}'\n"
            f"echo '{version}'\nexit {status}\n",
        )

    def runs(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


@pytest.fixture
def root() -> Iterator[Path]:
    yield from short_root("f243x-")


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stand-in counts as the native binary: a test cannot build one."""
    monkeypatch.setattr(codex_session, "_is_script", lambda binary: False)


@pytest.mark.usefixtures("native")
@pytest.mark.parametrize(
    ("reported", "refused"),
    [
        (PIN, False),
        ("codex-cli 0.159.1", True),
        (f"codex-cli 1{PINNED_VERSION}", True),
        (f"{PIN}-alpha", True),
        (f"{PIN}.1", True),
    ],
)
async def test_interactive_codex_is_refused_unless_the_installed_codex_is_exactly_the_pin(
    host: Host, root: Path, reported: str, refused: bool
) -> None:
    host.codex(reported)

    refusal = await CodexHost(root).spawn_refusal()

    if not refused:
        assert refusal is None
        return
    assert refusal is not None
    assert repr(reported) in refusal
    assert PIN in refusal


async def test_interactive_codex_is_refused_on_npms_node_wrapper(host: Host, root: Path) -> None:
    _script(host.bin / "codex", f"#!/usr/bin/env node\nconsole.log({PIN!r});\n")

    refusal = await CodexHost(root).spawn_refusal()

    assert refusal is not None
    assert "is a script, as npm's @openai/codex is" in refusal


@pytest.mark.usefixtures("native")
async def test_a_codex_version_probe_runs_with_an_empty_environment_and_a_failure_is_unavailable(
    host: Host, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORIAL_MASTER_KEY", "master")
    monkeypatch.setenv(ENV_API_KEY, "sk-the-workers-own-key")

    assert await CodexHost(root).spawn_refusal() is None
    seen = dict(line.split("=", 1) for line in host.env.read_text().splitlines())
    assert set(seen) <= {"PWD", "SHLVL", "_"}, "only what sh sets itself"

    host.codex(PIN, status=1)
    with pytest.raises(AdapterUnavailable):
        await CodexHost(root).spawn_refusal()


@pytest.mark.usefixtures("native")
async def test_a_passing_codex_check_is_cached_until_the_binary_changes(
    host: Host, root: Path
) -> None:
    adapter = CodexHost(root)

    assert await adapter.spawn_refusal() is None
    assert await adapter.spawn_refusal() is None
    assert host.runs() == 1

    codex = host.bin / "codex"
    stat = codex.stat()
    os.utime(codex, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert await adapter.spawn_refusal() is None
    assert host.runs() == 2

    host.codex("codex-cli 0.160.0")
    assert await adapter.spawn_refusal() is not None
    assert await adapter.spawn_refusal() is not None
    assert host.runs() == 4


def test_a_legacy_headless_codex_state_file_still_reads(tmp_path: Path) -> None:
    """As the codex adapter wrote it before sessions, read once after the deploy."""
    legacy = {
        "run_id": "GH-7-impl-agent-0a1b2c3d",
        "status": "interrupted",
        "agent": "impl-agent",
        "ticket": "GH-7",
        "repo": "acme/widgets",
        "branch": "impl/7-{slug}",
        "worktree": "/r/runs/GH-7-impl-agent-0a1b2c3d/worktree",
        "started_at": "2026-09-01T10:00:00Z",
        "deadline": "2026-09-01T11:00:00Z",
        "pid": 4242,
        "session_id": "019a0fd7-7d2e-7c41-9a55-3b1c2d3e4f50",
        "model": "gpt-5.3-codex",
        "max_turns": None,
        "turns": 2,
        "exit_code": None,
        "summary": "",
        "failure_reason": None,
        "cost_usd": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_input_tokens": None,
        "output_tokens": None,
        "artifacts": [],
        "stream_warnings": [],
        "expected": [],
        "blind": False,
        "config_sha": None,
        "git_dir": "/r/repos/acme/widgets/.git/worktrees/worktree",
        "mcp": [],
    }
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / STATE_FILE).write_text(json.dumps(legacy))

    state = read_state(folder, "x")

    assert state.status is RunStatus.INTERRUPTED
    assert state.session_log is None and state.resume_args == ()


class Pane(TmuxServer):
    """A TUI idle at its input line, which leaves once Enter lands on the quit command."""

    typed: list[tuple[str, bool, bool]]
    left = False

    # Python 3.13's frozen-slots `__setattr__` calls super() on the class before slots were
    # added, so it refuses even a subclass's own attributes; 3.14 fixed that.
    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)

    async def type_text(self, data: str, *, bracketed: bool = False, key: bool = False) -> bool:
        self.typed.append((data, bracketed, key))
        self.left = data == "\r"
        return True

    async def capture(self) -> str | None:
        return "\u276f /exit\n"

    async def pane(self) -> PaneState | None:
        return PaneState(dead=self.left, status=0, pid=4242)


async def test_the_claude_host_session_is_unchanged_by_the_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude's finish types what it typed before the profile: Esc, Ctrl-C, a bracketed
    `/exit`, and Enter only once the input line reads it."""
    monkeypatch.setattr(session, "KEY_GAP", 0.0)
    monkeypatch.setattr(session, "DRAW_WAIT", 0.0)
    server = Pane("tmux", "/tmp/x", Path("/tmp/c"), Path("/tmp"), "run")
    server.typed = []

    assert await session._type_exit(server, ClaudeTui())

    assert server.typed == [
        ("\x1b", False, True),
        ("\x03", False, True),
        ("/exit", True, False),
        ("\r", False, True),
    ]


# ------------------------------------------------------------------ discovery


def _rollout(home: Path, thread: str, first: dict[str, object] | None = None) -> Path:
    path = home / "sessions" / "2026" / "10" / "02" / f"rollout-2026-10-02T12-00-00-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = first or {"type": "session_meta", "payload": {"id": thread}}
    path.write_text(json.dumps(meta) + "\n")
    return path


THREAD = "01a0fd77-250b-7800-82c6-e6f8b2077805"
OTHER = "01a0fd77-0000-7000-8000-000000000000"


def test_codex_discovery_takes_the_one_rollout_its_launch_began_and_names_its_thread(
    tmp_path: Path,
) -> None:
    """The thread a resume addresses comes from here: a wrong one continues another session."""
    tui = CodexTui()
    since = time.time() - 1
    assert tui.discover(tmp_path, since) is None

    stale = _rollout(tmp_path, OTHER)
    os.utime(stale, (since - 60, since - 60))
    elsewhere = tmp_path / "elsewhere"
    linked = _rollout(elsewhere, OTHER)
    os.symlink(
        linked,
        tmp_path
        / "sessions"
        / "2026"
        / "10"
        / "02"
        / linked.name.replace(OTHER, "01a0fd77-1111-7000-8000-000000000000"),
    )
    path = _rollout(tmp_path, THREAD)
    assert tui.discover(tmp_path, since) == (THREAD, path)

    _rollout(tmp_path, "01a0fd77-2222-7000-8000-000000000000")
    with pytest.raises(SpawnError, match="2 rollouts"):
        tui.discover(tmp_path, since)


def test_codex_discovery_refuses_a_rollout_whose_first_line_names_another_thread(
    tmp_path: Path,
) -> None:
    _rollout(tmp_path, THREAD, {"type": "session_meta", "payload": {"id": OTHER}})

    with pytest.raises(SpawnError, match="does not open on its own thread"):
        CodexTui().discover(tmp_path, time.time() - 1)


# ------------------------------------------------------------------ a session in tmux

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
FAKE_TUI = Path(__file__).with_name("fake_codex_tui.py")

API_KEY = "sk-proj-the-stored-openai-key-0123456789abcdef"
TOKEN = "ghs_mintedtoken"
NEW_KEY = "sk-proj-the-key-minted-for-the-resume-9876543210"
NEW_TOKEN = "ghs_mintedagainforresume"
DOCS_TOKEN = "docs-token-held-only-by-the-environment"
MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)
SOCKET = re.compile(r"/t/[0-9a-f]{12}-s")
BRANCH = "spec/12-widget"
PNG = b"\x89PNG\r\n\x1a\n-not-really"
SESSION_CREDENTIALS = (AUTH_FILE, CONFIG_FILE, ENV_FILE)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return bare_origin(tmp_path)


@pytest.fixture
def codex(tmp_path: Path) -> Path:
    """The fake TUI behind an absolute path, as `shutil.which` resolves the real one."""
    return _script(
        tmp_path / "bin" / "codex", f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TUI}" "$@"\n'
    )


class Clock:
    def __init__(self) -> None:
        self.ahead = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.ahead


def session_adapter(
    root: Path,
    codex: Path,
    origin: Path,
    *,
    now: Callable[[], datetime] | None = None,
    auth_mode: str = AUTH_MODE_API_KEY,
    subscription_auth_file: Path | None = None,
    cli: CodexOriginCli | None = None,
    ask_tool: bool = False,
) -> CodexCliAdapter:
    return CodexCliAdapter(
        local_root=root,
        cli=cli or CodexOriginCli(codex, origin),
        now=now or Clock(),
        poll=0.05,
        auth_mode=auth_mode,
        subscription_auth_file=subscription_auth_file,
        ask_tool=ask_tool,
    )


def session_spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You plan.", instructions="Write the spec."),
        "model": "gpt-5.3-codex",
        "max_turns": 10,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


def fresh_secrets() -> dict[str, SecretStr]:
    return {ENV_API_KEY: SecretStr(NEW_KEY), ENV_GH_TOKEN: SecretStr(NEW_TOKEN)}


def state_of(root: Path, run_id: str) -> HostRunState:
    return read_state(run_dir(root, run_id), run_id)


def edit_state(root: Path, run_id: str, **fields: object) -> None:
    write_state(run_dir(root, run_id), state_of(root, run_id).model_copy(update=fields))


def config_of(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "config"


def credentials_left(root: Path, run_id: str) -> list[str]:
    return [name for name in SESSION_CREDENTIALS if (config_of(root, run_id) / name).exists()]


def worktree_git(root: Path, run_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    state = state_of(root, run_id)
    assert state.git_dir is not None
    at = ["--git-dir", state.git_dir, "--work-tree", state.worktree]
    return subprocess.run(["git", *at, *to_origin(), *args], capture_output=True, text=True)


def dumped(dump: Path) -> dict[str, Any]:
    return json.loads(dump.read_text())


def argv_of(dump: Path) -> list[str]:
    return dumped(dump)["argv"][1:]


async def typed(root: Path, run_id: str, text: str) -> None:
    assert await server_of(root, run_id).type_text(text)


async def ended(adapter: CodexCliAdapter, root: Path, run_id: str) -> None:
    """The test's own end of a session: its server killed, its supervisor drained."""
    await server_of(root, run_id).kill()
    await adapter.drain()


def files_holding(root: Path, value: str) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and value.encode() in path.read_bytes()
    }


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int) -> bool:
    return not alive(pid)


@pytest.fixture
def dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The file the fake writes its argv, cwd and environment to at each launch."""
    path = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(path))
    return path


@needs_tmux
async def test_an_interactive_codex_spawn_runs_the_tui_in_its_own_tmux_server_and_names_its_thread(
    root: Path, codex: Path, origin: Path, dump: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for planted in ("FACTORIAL_MASTER_KEY", "TMUX", "TMUX_PANE", "CODEX_THREAD_ID"):
        monkeypatch.setenv(planted, "planted")
    adapter = session_adapter(root, codex, origin)
    secrets = {
        ENV_API_KEY: SecretStr(API_KEY),
        ENV_GH_TOKEN: SecretStr(TOKEN),
        "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
    }

    handle = await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))
    run_id = handle.run_id
    try:
        fresh = await adapter.session_state(run_id)
        state = state_of(root, run_id)
        assert state.session_log is not None
        assert fresh.session_id == state.session_id == Path(state.session_log).stem[-36:]
        target = await adapter.terminal_target(run_id)
        assert (target.substrate, target.session) == ("host", "run")
        assert target.socket.startswith(f"{root}/t/") and SOCKET.search(target.socket)
        await reaches(adapter, run_id, RunStatus.IDLE)

        argv = argv_of(dump)
        worktree = state.worktree
        assert argv[:-1] == [
            "--no-daemon",
            "--no-alt-screen",
            "-m",
            "gpt-5.3-codex",
            "-C",
            worktree,
            "-s",
            "workspace-write",
            "-a",
            "never",
        ]
        assert argv[-1] == f"Read {run_dir(root, run_id)}/turns/0.md and do what it says."
        env = dumped(dump)["env"]
        assert {key for key in env if key.startswith("FACTORIAL_")} <= {
            "FACTORIAL_MCP_PRODUCT_DOCS_BEARER"
        }
        assert not {"TMUX", "TMUX_PANE", "CODEX_THREAD_ID"} & env.keys()
        assert (env[ENV_GH_TOKEN], env["CODEX_HOME"]) == (TOKEN, str(config_of(root, run_id)))
        home = config_of(root, run_id) / "home"
        assert env["HOME"] == str(home) and home.is_dir() and not any(home.iterdir())
        assert not (config_of(root, run_id) / ENV_FILE).exists(), "the pane deletes its env file"

        ps = ["ps", "-axww", "-o", "args="]
        processes = await asyncio.to_thread(subprocess.run, ps, capture_output=True, text=True)
        environment = await server_of(root, run_id).run("show-environment", "-g")
        for secret in (API_KEY, TOKEN, DOCS_TOKEN):
            assert secret not in processes.stdout
            assert secret not in environment.stdout
        assert files_holding(root, API_KEY) == {f"runs/{run_id}/config/{AUTH_FILE}"}
        assert files_holding(root, TOKEN) == set()
        assert files_holding(root, DOCS_TOKEN) == set()
        for credential in (AUTH_FILE, CONFIG_FILE):
            assert (config_of(root, run_id) / credential).stat().st_mode & 0o777 == 0o600
    finally:
        await ended(adapter, root, run_id)


def _commit_repo_hooks(origin: Path, folder: Path) -> None:
    """A `.codex/config.toml` on `main` declaring hooks, as a repository may ship one."""
    work = folder / "hooked"
    git("clone", str(origin), str(work), cwd=folder)
    (work / ".codex").mkdir()
    (work / ".codex" / "config.toml").write_text(
        '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\n'
        'command = "touch /tmp/repo-hook-ran"\n'
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "true"\n'
    )
    git("add", "-A", cwd=work)
    git("commit", "-m", "hooks", cwd=work)
    git("push", "origin", "HEAD:main", cwd=work)


@needs_tmux
@pytest.mark.parametrize("seed", [VALUE_TRUST_UNTRUSTED, "trusted"])
async def test_a_repo_codex_hook_does_not_block_the_fakes_startup_because_the_seed_trusts_nothing(
    root: Path,
    codex: Path,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: str,
) -> None:
    """Trusted, the checkout's hooks bring up "Hooks need review", which no consent takes: the
    spawn fails naming it, and nothing it wrote is left."""
    _commit_repo_hooks(origin, tmp_path)
    monkeypatch.setattr(codex_spec, "VALUE_TRUST_UNTRUSTED", seed)
    monkeypatch.setattr(session, "CONSENT_WAIT", 2.0)
    adapter = session_adapter(root, codex, origin)

    if seed == VALUE_TRUST_UNTRUSTED:
        run_id = (await adapter.spawn(session_spec())).run_id
        try:
            await reaches(adapter, run_id, RunStatus.IDLE)
            assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_END
        finally:
            await ended(adapter, root, run_id)
        return

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec())
    assert raised.value.reason is SpawnReason.INFRASTRUCTURE
    assert "put up no consent screen" in str(raised.value)
    assert "'Hooks need review'" in str(raised.value)
    [run] = (root / "runs").iterdir()
    assert credentials_left(root, run.name) == []
    assert await server_of(root, run.name).pane() is None


@needs_tmux
async def test_a_codex_spawn_that_never_writes_a_rollout_fails_with_its_screen_and_no_credential(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "no_rollout")
    monkeypatch.setattr(session, "SESSION_ID_WAIT", 1.0)
    adapter = session_adapter(root, codex, origin)

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec())

    # A sign-in screen is the stored login refused: no retry fixes it (sandbox #203).
    assert raised.value.reason is SpawnReason.CREDENTIAL
    assert "began no session log within 1s" in str(raised.value)
    assert "'Sign in with ChatGPT to continue'" in str(raised.value)
    assert str(raised.value).startswith(
        "The Codex CLI refused the runtime's stored login. Store a fresh one on the Runtime page."
    )
    [run] = (root / "runs").iterdir()
    assert credentials_left(root, run.name) == []
    assert not (run / STATE_FILE).exists()
    assert await server_of(root, run.name).pane() is None


@needs_tmux
async def test_stop_hook_then_failed_turn_then_human_input_reach_session_state(
    root: Path, codex: Path, origin: Path
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        stopped = await adapter.session_state(run_id)
        assert stopped.idle_reason is IdleReason.TURN_END and stopped.last_input_at is None
        assert stopped.session_deadline is None

        await typed(root, run_id, "FAILME\r")

        async def failed() -> bool:
            return (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_ERROR

        await until(failed, "the failed turn, from the rollout")
        assert (await adapter.session_state(run_id)).last_input_at is not None
        assert await adapter.status(run_id) is RunStatus.IDLE

        await typed(root, run_id, "SLOWME\r")
        await reaches(adapter, run_id, RunStatus.RUNNING)

        async def origins() -> list[str | None]:
            lines = [json.loads(line) async for line in adapter.logs(run_id)]
            items = [line["item"] for line in lines if line.get("type") == "item.completed"]
            return (
                [
                    line.get("factorial_origin")
                    for line in lines
                    if line.get("type") == "item.completed"
                    and line["item"]["type"] == "user_message"
                ]
                if items
                else []
            )

        async def mirrored() -> bool:
            return len(await origins()) == 3

        await until(mirrored, "the human's turns in the mirror")
        assert await origins() == ["orchestrator", "human", "human"]
    finally:
        await ended(adapter, root, run_id)


async def asked(adapter: CodexCliAdapter, root: Path, run_id: str) -> None:
    """Type the turn whose reply is a `request_user_input` call, and wait for its rollout line."""
    await reaches(adapter, run_id, RunStatus.IDLE)
    await typed(root, run_id, "ASKME\r")

    async def called() -> bool:
        log = state_of(root, run_id).session_log
        return log is not None and "request_user_input" in await asyncio.to_thread(
            Path(log).read_text
        )

    await until(called, "the question call in the rollout")


@needs_tmux
@pytest.mark.parametrize("ask_tool", [True, False], ids=["on", "off"])
async def test_a_codex_question_call_idles_the_session_only_with_the_ask_tool_on(
    root: Path, codex: Path, origin: Path, ask_tool: bool
) -> None:
    """On, the picker waits for a person, and the expiry is when codex answers it itself; off,
    codex refuses the call and the turn runs on."""
    adapter = session_adapter(root, codex, origin, ask_tool=ask_tool)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await asked(adapter, root, run_id)
        if ask_tool:

            async def asking() -> bool:
                return (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

            await until(asking, "the session idle on the question")
            state = await adapter.session_state(run_id)
            assert state.question == "Which database? [Postgres | SQLite]"
            assert state.idle_since is not None
            assert state.question_expires_at == state.idle_since + timedelta(seconds=120)
        else:
            await asyncio.sleep(0.05 * 3)
            assert await adapter.status(run_id) is RunStatus.RUNNING
            assert (await adapter.session_state(run_id)).question_expires_at is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_finish_on_a_codex_tool_question_hangs_up(
    root: Path, codex: Path, origin: Path
) -> None:
    """The picker takes typed text for an answer, so a quit typed there would answer it."""
    adapter = session_adapter(root, codex, origin, ask_tool=True)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await asked(adapter, root, run_id)

        async def asking() -> bool:
            return (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

        await until(asking, "the session idle on the question")
        prompts = state_of(root, run_id).signals.prompts
        await adapter.finish(run_id)

        assert state_of(root, run_id).signals.prompts == prompts, "the quit went in as a turn"
        assert state_of(root, run_id).exit_code is None, "hung up, not exited"
        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_resume_clears_a_codex_tool_question(
    root: Path, codex: Path, origin: Path
) -> None:
    """The park's hang-up took the picker with it: a resumed session still reporting the
    question would be parked again at once for a question nobody can see."""
    adapter = session_adapter(root, codex, origin, ask_tool=True)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await asked(adapter, root, run_id)

        async def asking() -> bool:
            return (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

        await until(asking, "the session idle on the question")
        await adapter.interrupt(run_id, ParkReason.IDLE)
        await adapter.resume(run_id, None, fresh_secrets())
        await reaches(adapter, run_id, RunStatus.IDLE)

        state = await adapter.session_state(run_id)
        assert state.idle_reason is not IdleReason.QUESTION
        assert state.question_expires_at is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_codex_turn_cap_hangs_up_and_fails_the_run(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "turns=3")
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec(max_turns=2))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.FAILED)

        result = await adapter.result(run_id)
        assert "Stopped after 2 turns" in (result.failure_reason or "")
        assert await server_of(root, run_id).pane() is None
        assert credentials_left(root, run_id) == [], "every credential file goes at the end"
        assert (run_dir(root, run_id) / "worktree").is_dir(), "the workspace is kept"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("draft", ["", "half a thought"])
async def test_codex_finish_quits_through_paste_buffer_under_a_watcher_with_its_tokens(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch, draft: str
) -> None:
    """Ctrl-C quits at an empty input line; on a draft it only empties it, and the pasted
    `/quit` is Entered once the input line reads it."""
    monkeypatch.setattr(session, "CLEAR_WAIT", 1.0)
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        if draft:
            await typed(root, run_id, draft)
        target = await adapter.terminal_target(run_id)
        viewer = await HostTmuxRelay().attach(
            target, writable=False, cols=80, rows=24, on_output=lambda _: None
        )
        try:
            await adapter.finish(run_id)
            prompts = state_of(root, run_id).signals.prompts
            assert prompts == 1, "the quit went in as a turn of its own"
            assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        finally:
            await viewer.close()

        result = await adapter.result(run_id)
        assert result.cost_usd is None
        assert (result.input_tokens, result.output_tokens) == (1000, 7)
        assert result.stream_warnings == ()
        assert result.summary.startswith("done: Read ")
        assert state_of(root, run_id).exit_code == 0, "the CLI left on its own"
        assert credentials_left(root, run_id) == []
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_codex_quit_that_does_not_land_falls_back_to_a_hangup(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TUI", "deaf_quit")
    monkeypatch.setattr(session, "CLEAR_WAIT", 0.5)
    monkeypatch.setattr(session, "EXIT_WAIT", 1.0)
    adapter = session_adapter(root, codex, origin)
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
async def test_a_codex_session_a_person_quits_parks_as_exited(
    root: Path, codex: Path, origin: Path
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)

        await typed(root, run_id, "/quit\r")
        await adapter.drain()

        state = state_of(root, run_id)
        assert (state.status, state.park_reason, state.exit_code) == (
            RunStatus.INTERRUPTED,
            ParkReason.EXITED,
            0,
        )
        assert (await adapter.result(run_id)).park_reason is ParkReason.EXITED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("reason", list(ParkReason))
async def test_codex_interrupt_parks_with_its_reason_keeps_the_branch_and_deletes_credentials(
    root: Path, codex: Path, origin: Path, reason: ParkReason
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0

        await adapter.interrupt(run_id, reason)

        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        result = await adapter.result(run_id)
        assert result.failure_reason == session._PARKED[reason]
        assert credentials_left(root, run_id) == []
        shown = worktree_git(root, run_id, "symbolic-ref", "--short", "HEAD")
        assert shown.stdout.strip() == BRANCH
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize("end", ["interrupt", "finish"])
async def test_a_codex_hangup_also_ends_a_command_the_agent_left_in_its_own_session(
    root: Path,
    codex: Path,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end: str,
) -> None:
    """codex starts each command in a session of its own, which the CLI's process group and its
    hangup do not reach. This one carries none of the run's environment, so no marker finds it:
    only the CLI's descendants, read while it runs, do."""
    child = tmp_path / "child"
    monkeypatch.setenv("FAKE_TUI", "spawn_child")
    monkeypatch.setenv("FAKE_TUI_CHILD", str(child))
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    pid = 0
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        pid = int(child.read_text())
        assert alive(pid) and os.getpgid(pid) == pid

        if end == "interrupt":
            await adapter.interrupt(run_id)
        else:
            await adapter.finish(run_id)
            assert state_of(root, run_id).exit_code == 0, "the CLI left by its own quit"

        assert (await adapter.status(run_id)).is_terminal
        await until(lambda: gone(pid), "the command ended", within=5)
    finally:
        if pid and alive(pid):
            os.kill(pid, signal.SIGKILL)
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_turnless_codex_resume_relaunches_its_thread_at_its_prompt_with_fresh_credentials(
    root: Path, codex: Path, origin: Path, dump: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = session_adapter(root, codex, origin)
    probed: list[str | None] = []

    async def discover(state: HostRunState, env: Mapping[str, str]) -> tuple[()]:
        probed.append(env.get(ENV_GH_TOKEN))
        return ()

    monkeypatch.setattr(adapter, "_discover_artifacts", discover)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        target = await adapter.terminal_target(run_id)
        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
        parked = state_of(root, run_id)
        assert parked.session_id is not None

        handle = await adapter.resume(run_id, None, fresh_secrets())

        assert (handle.run_id, handle.status) == (run_id, RunStatus.RUNNING)
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_END
        assert await adapter.terminal_target(run_id) == target
        argv = argv_of(dump)
        assert argv[0] == "resume" and argv[-1] == parked.session_id
        assert not any(arg.startswith("Read ") for arg in argv)
        auth = json.loads((config_of(root, run_id) / AUTH_FILE).read_text())
        assert auth[ENV_API_KEY] == NEW_KEY
        resumed = state_of(root, run_id)
        assert resumed.session_log == parked.session_log
        assert resumed.segment_log_start == parked.log_read > 0

        await adapter.finish(run_id)
        result = await adapter.result(run_id)
        assert result.outcome is RunStatus.SUCCEEDED
        assert result.input_tokens == 1000, "the resumed segment's tokens are its own: none"
        assert result.stream_warnings == ()
        assert probed == [TOKEN, NEW_TOKEN], "discovery uses the token the resume was handed"
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_codex_resume_turn_attaches_images_and_points_at_its_turn_file(
    root: Path, codex: Path, origin: Path, dump: Path
) -> None:
    adapter = session_adapter(root, codex, origin)
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
        thread = state_of(root, run_id).session_id
        assert thread is not None
        await adapter.interrupt(run_id)

        await adapter.resume(run_id, "Match the screenshot.", fresh_secrets(), (image,))

        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        turn = here / "turns" / "1.md"
        assert turn.stat().st_mode & 0o777 == 0o600
        assert turn.read_text().startswith("Match the screenshot.\n")
        argv = argv_of(dump)
        [attached] = [arg for arg in argv if arg.startswith("--image=")]
        assert attached.startswith(f"--image={here}/attachments/")
        assert argv[-2:] == [thread, f"Read {turn} and do what it says."]
        assert argv.index(attached) < argv.index(thread)
        assert state_of(root, run_id).segment_turn
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_codex_resume_ends_the_inspect_shell_before_it_writes_a_credential(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    seen: list[list[str]] = []
    real = adapter._end_inspect

    async def end_inspect(ended_id: str) -> None:
        seen.append(credentials_left(root, ended_id))
        await real(ended_id)

    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        monkeypatch.setattr(adapter, "_end_inspect", end_inspect)

        await adapter.resume(run_id, None, fresh_secrets())

        await reaches(adapter, run_id, RunStatus.IDLE)
        assert seen == [[]]
        assert credentials_left(root, run_id) == [AUTH_FILE, CONFIG_FILE]
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_codex_sandbox_writes_only_the_worktree_and_its_clones_git_dir(
    root: Path, codex: Path, origin: Path, dump: Path
) -> None:
    """Every file a resume trusts sits elsewhere under `local_root`: the state, the config
    home, the hooks' script. So flags planted in the state file are never read back, and a
    resume launches inside the same sandbox as the spawn."""
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        planted = ("-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox")
        edit_state(root, run_id, resume_args=planted)

        await adapter.resume(run_id, None, fresh_secrets())

        await reaches(adapter, run_id, RunStatus.IDLE)
        argv = argv_of(dump)
        assert not set(planted[1:]) & set(argv)
        assert argv[argv.index("-s") + 1] == "workspace-write"
        config = tomllib.loads((config_of(root, run_id) / CONFIG_FILE).read_text())
        assert (config["sandbox_mode"], config["approval_policy"]) == ("workspace-write", "never")
        workspace = config["sandbox_workspace_write"]
        assert workspace["writable_roots"] == [str(root / "repos" / REPO / ".git")]
        # The agent must push; `$TMPDIR` and `/tmp` are writable by default, a scratch every
        # run on the host would share.
        assert workspace["network_access"] is True
        assert workspace["exclude_tmpdir_env_var"] is workspace["exclude_slash_tmp"] is True
        state = state_of(root, run_id)
        reach = [Path(workspace["writable_roots"][0]), Path(state.worktree)]
        for trusted in (run_dir(root, run_id) / STATE_FILE, config_of(root, run_id), root / "bin"):
            assert not any(trusted.is_relative_to(path) for path in reach), trusted
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.parametrize(
    ("planted", "refusal"),
    [
        ({"session_id": "--dangerously-bypass-approvals-and-sandbox"}, "rollout"),
        ({"session_log": "/etc/hosts"}, "rollout"),
        ({"git_dir": "/"}, "administrative directory in a clone"),
        # A run from before every run was a session never resolved a checkout for a TUI.
        ({"workspace_real": None}, "no terminal session to resume"),
    ],
    ids=["thread-as-a-flag", "log-elsewhere", "git-dir-root", "from-before-sessions"],
)
async def test_a_codex_resume_refuses_a_state_that_names_what_its_session_never_had(
    root: Path, codex: Path, origin: Path, planted: dict[str, str], refusal: str
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        edit_state(root, run_id, **planted)

        with pytest.raises(AgentRuntimeError, match=refusal):
            await adapter.resume(run_id, None, fresh_secrets())

        assert state_of(root, run_id).status is RunStatus.INTERRUPTED
        assert credentials_left(root, run_id) == []
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


# ------------------------------------------------------------------ a worker restart


async def restarted(adapter: CodexCliAdapter) -> None:
    """A worker restart, in process: every supervisor stops where it stands, its lock let go."""
    tasks = list(adapter._supervisors.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def mirrored(root: Path, run_id: str) -> list[str]:
    return (run_dir(root, run_id) / LOG_FILE).read_text().splitlines()


def socket_left(root: Path, run_id: str) -> bool:
    return os.path.lexists(server_of(root, run_id).socket)


@pytest.fixture
def decoy() -> Iterator[subprocess.Popen[bytes]]:
    """A process in a group of its own, standing for whatever holds a recorded pid by now."""
    process = subprocess.Popen(["/bin/sleep", "1000"], start_new_session=True)
    yield process
    process.kill()
    process.wait()


@needs_tmux
@pytest.mark.parametrize("segment", ["spawn", "turnless resume", "rework"])
async def test_a_new_adapter_re_adopts_a_live_codex_session_without_repeating_a_line(
    root: Path, codex: Path, origin: Path, segment: str
) -> None:
    """A spawn's pane names no thread, which codex makes as it starts: the server is taken back
    by the launch's turn file, a resume's also by its thread, and a rework's by both, its pane
    launched from the config home of the run whose workspace it continues."""
    first = session_adapter(root, codex, origin)
    run_id = (await first.spawn(session_spec())).run_id
    second = session_adapter(root, codex, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        thread = state_of(root, run_id).session_id
        if segment == "turnless resume":
            await first.interrupt(run_id, ParkReason.ROLLOVER)
            await first.resume(run_id, None, fresh_secrets())
            await reaches(first, run_id, RunStatus.IDLE)
        elif segment == "rework":
            assert thread is not None
            identity = ("-c", "user.name=t", "-c", "user.email=t@t")
            for args in (
                ("checkout", "-b", BRANCH),
                ("commit", "--allow-empty", "-m", "spec"),
                ("push", "origin", BRANCH),
            ):
                assert worktree_git(root, run_id, *identity, *args).returncode == 0, args
            await first.finish(run_id)
            rework = session_spec(
                resume_from=ResumeFrom(run_id=run_id, session_id=thread), secrets=fresh_secrets()
            )
            run_id = (await first.spawn(rework)).run_id
            await reaches(first, run_id, RunStatus.IDLE)
            assert state_of(root, run_id).workspace_of is not None
        await restarted(first)

        assert await second.status(run_id) is RunStatus.IDLE

        async def taken_back() -> bool:
            supervisor = second._attached(run_id)
            return supervisor is not None and supervisor._proven

        await until(taken_back, "the new supervisor to prove the server is the run's")
        await typed(root, run_id, "the second turn\r")

        prompts = 1 if segment == "turnless resume" else 2

        async def answered() -> bool:
            status = await second.status(run_id)
            return status is RunStatus.IDLE and state_of(root, run_id).signals.prompts == prompts

        await until(answered, "the second turn answered")
        await second.finish(run_id)

        result = await second.result(run_id)
        assert (result.outcome, result.session_id) == (RunStatus.SUCCEEDED, thread)
        assert result.stream_warnings == ()
        lines = mirrored(root, run_id)
        items = [line for line in lines if json.loads(line)["type"] == "item.completed"]
        assert len(items) == len(set(items)) == 4
        kinds = [json.loads(line)["type"] for line in lines]
        assert (kinds.count("turn.started"), kinds.count("turn.completed")) == (2, 2)
        assert sum("done: the second turn" in line for line in lines) == 1
    finally:
        await ended(second, root, run_id)


@needs_tmux
@pytest.mark.parametrize("lost", ["killed", "killed after a restart", "served by another launch"])
async def test_a_lost_tmux_server_fails_the_codex_run_host_lost_and_keeps_its_workspace(
    root: Path, codex: Path, origin: Path, lost: str
) -> None:
    """The socket is keyed by the run, but any process of the worker's user can serve on it: a
    launch of the same thread is not the spawn's, whose pane holds its turn file."""
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert worktree_git(root, run_id, "checkout", "-b", BRANCH).returncode == 0
        if lost != "killed":
            await restarted(adapter)
            adapter = session_adapter(root, codex, origin)

        server = server_of(root, run_id)
        await server.kill()
        if lost == "served by another launch":
            state = state_of(root, run_id)
            assert state.session_id is not None
            env_file = config_of(root, run_id) / ENV_FILE
            impostor = [*launch_prefix(root, env_file), str(codex), "resume"]
            assert (await server.start(root, [*impostor, state.session_id])).ok
        await adapter.status(run_id)
        await asyncio.wait_for(adapter.drain(), 30)

        result = await adapter.result(run_id)
        assert result.outcome is RunStatus.FAILED
        assert (result.failure_reason or "").startswith("host_lost"), result.failure_reason
        assert (run_dir(root, run_id) / "worktree").is_dir()
        assert credentials_left(root, run_id) == []
        assert worktree_git(root, run_id, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert json.loads(mirrored(root, run_id)[-1])["type"] == "turn.completed"
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
async def test_a_codex_stop_in_flight_when_the_server_died_keeps_its_outcome(
    root: Path, codex: Path, origin: Path, in_flight: dict[str, object], outcome: RunStatus
) -> None:
    """The worker persisted the stop, then died with the host before the hangup landed."""
    first = session_adapter(root, codex, origin)
    run_id = (await first.spawn(session_spec())).run_id
    adapter = session_adapter(root, codex, origin)
    try:
        await reaches(first, run_id, RunStatus.IDLE)
        await restarted(first)
        edit_state(root, run_id, **in_flight)
        await server_of(root, run_id).kill()

        await adapter.status(run_id)
        await asyncio.wait_for(adapter.drain(), 30)

        assert (await adapter.result(run_id)).outcome is outcome
        assert credentials_left(root, run_id) == []
        if outcome is RunStatus.INTERRUPTED:
            await adapter.resume(run_id, None, fresh_secrets())
            await reaches(adapter, run_id, RunStatus.IDLE)
            assert await server_of(root, run_id).pane() is not None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_startup_sweep_ends_a_finished_codex_runs_stray_server_and_deletes_credentials(
    root: Path, codex: Path, origin: Path, decoy: subprocess.Popen[bytes]
) -> None:
    first = session_adapter(root, codex, origin)
    stray = (await first.spawn(session_spec())).run_id
    live = (await first.spawn(session_spec())).run_id
    try:
        await reaches(first, stray, RunStatus.IDLE)
        await reaches(first, live, RunStatus.IDLE)
        await restarted(first)
        edit_state(root, stray, status=RunStatus.CANCELLED)
        assert credentials_left(root, stray) == [AUTH_FILE, CONFIG_FILE]

        assert session_adapter(root, codex, origin).sweep_credentials() == 2

        assert await server_of(root, stray).pane() is None
        assert not socket_left(root, stray), "no socket left to visit again"
        assert credentials_left(root, stray) == []
        assert decoy.poll() is None
        pane = await server_of(root, live).pane()
        assert pane is not None and not pane.dead
        assert credentials_left(root, live) == [AUTH_FILE, CONFIG_FILE]
    finally:
        for run_id in (stray, live):
            await ended(first, root, run_id)


# ------------------------------------------------------------------ spawn properties, on a session


async def test_no_local_root_refuses_a_codex_session(codex: Path, origin: Path) -> None:
    """The guard that makes the blast radius knowable. No default, ever (decision 34)."""
    adapter = CodexCliAdapter(cli=CodexOriginCli(codex, origin))

    with pytest.raises(AdapterUnavailable, match="LOCAL_ROOT"):
        await adapter.spawn(session_spec())


async def test_a_codex_session_without_the_stored_key_is_refused_even_with_one_on_the_host(
    root: Path, codex: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store, not the environment: a run's fresh CODEX_HOME sees no operator login, and a
    host fallback is what hid an empty secret table on claude_cli."""
    monkeypatch.setenv(ENV_API_KEY, "sk-proj-exported-on-the-host")

    with pytest.raises(SpawnError) as raised:
        await session_adapter(root, codex, origin).spawn(
            session_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)})
        )

    assert raised.value.reason is SpawnReason.CREDENTIAL


def _operator_login(folder: Path, token: str) -> Path:
    path = folder / "operator" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token}}))
    return path


@needs_tmux
async def test_a_subscription_session_seeds_its_home_from_the_operators_login(
    root: Path, codex: Path, origin: Path, tmp_path: Path
) -> None:
    """A subscription is a refreshable token bundle, not a key: the run's home gets a copy of
    the operator's login, and no OPENAI_API_KEY is needed anywhere."""
    login = _operator_login(tmp_path, "from-the-host")
    adapter = session_adapter(
        root, codex, origin, auth_mode=AUTH_MODE_SUBSCRIPTION, subscription_auth_file=login
    )
    run_id = (await adapter.spawn(session_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)}))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        seeded = config_of(root, run_id) / AUTH_FILE
        assert seeded.read_text() == login.read_text()
        assert seeded.stat().st_mode & 0o777 == 0o600
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_stored_subscription_beats_the_operators_file_on_a_session(
    root: Path, codex: Path, origin: Path, tmp_path: Path
) -> None:
    """The login travels as a project secret, per-project and encrypted at rest; the host file
    is the developer-machine fallback, not the source of record."""
    login = _operator_login(tmp_path, "from-the-host")
    stored = '{"auth_mode": "chatgpt", "tokens": {"access_token": "from-the-store"}}'
    adapter = session_adapter(
        root, codex, origin, auth_mode=AUTH_MODE_SUBSCRIPTION, subscription_auth_file=login
    )
    secrets = {ENV_GH_TOKEN: SecretStr(TOKEN), SECRET_AUTH_JSON: SecretStr(stored)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert (config_of(root, run_id) / AUTH_FILE).read_text() == stored
    finally:
        await ended(adapter, root, run_id)


async def test_a_subscription_session_refuses_when_the_operator_is_not_logged_in(
    root: Path, codex: Path, origin: Path, tmp_path: Path
) -> None:
    """It names `codex login`, the developer-machine fix, and the secret, the stored-login one."""
    adapter = session_adapter(
        root,
        codex,
        origin,
        auth_mode=AUTH_MODE_SUBSCRIPTION,
        subscription_auth_file=tmp_path / "operator" / "auth.json",
    )

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)}))

    assert raised.value.reason is SpawnReason.CREDENTIAL
    assert "codex login" in str(raised.value)
    assert SECRET_AUTH_JSON in str(raised.value)


@needs_tmux
async def test_a_codex_sessions_skills_lie_under_its_home_never_the_worktree(
    root: Path, codex: Path, origin: Path
) -> None:
    adapter = session_adapter(root, codex, origin)
    skill = AgentSkill(name="spec-format", content="# Spec format\n", scope=SkillScope.SHARED)
    run_id = (await adapter.spawn(session_spec(skills=(skill,)))).run_id
    try:
        here = run_dir(root, run_id)
        laid = config_of(root, run_id) / "skills" / "spec-format" / "SKILL.md"
        assert laid.read_text() == "# Spec format\n"
        assert not list((here / "worktree").rglob("SKILL.md"))
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_finished_codex_session_reports_the_head_of_its_pointerless_worktree(
    root: Path, codex: Path, origin: Path
) -> None:
    """codex protects the cwd's `.git` and the `gitdir:` a pointer file names, read-only: so the
    pointer goes, and the host probe reads HEAD through the worktree's administrative
    directory."""
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        identity = ("-c", "user.name=t", "-c", "user.email=t@t")
        made = worktree_git(root, run_id, *identity, "commit", "--allow-empty", "-m", "work")
        assert made.returncode == 0, made.stderr
        head = worktree_git(root, run_id, "rev-parse", "HEAD").stdout.strip()

        await adapter.finish(run_id)

        assert not (run_dir(root, run_id) / "worktree" / ".git").exists()
        result = await adapter.result(run_id)
        commits = [a.locator for a in result.artifacts if a.kind is ArtifactKind.COMMIT]
        assert commits == [{"sha": head}]
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_credential_none_codex_session_gets_no_github_credential_even_one_the_host_exports(
    root: Path, codex: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec(workspace=workspace, secrets=secrets))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        env = dumped(dump)["env"]
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
async def test_a_codex_resume_renders_its_mcp_servers_again_with_their_bearer(
    root: Path, codex: Path, origin: Path, dump: Path
) -> None:
    """`config.toml` goes with the login at every end. Without the bearer variables a resumed
    session would reach its servers unauthenticated; the token itself never enters the TOML."""
    adapter = session_adapter(root, codex, origin)
    secrets = {**session_spec().secrets, "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN)}
    run_id = (await adapter.spawn(session_spec(secrets=secrets, mcp=(MCP_HTTP,)))).run_id
    fresh_docs = "docs-token-minted-again-for-the-resume"
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        assert not (config_of(root, run_id) / CONFIG_FILE).exists()

        await adapter.resume(
            run_id, None, {**fresh_secrets(), "PRODUCT_DOCS_TOKEN": SecretStr(fresh_docs)}
        )
        await reaches(adapter, run_id, RunStatus.IDLE)

        assert dumped(dump)["env"][bearer_env_var("product-docs")] == fresh_docs
        config = (config_of(root, run_id) / CONFIG_FILE).read_text()
        assert "product-docs" in config
        assert fresh_docs not in config and DOCS_TOKEN not in config
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_codex_session_spawn_attaches_its_images_and_an_end_removes_them(
    root: Path, codex: Path, origin: Path, dump: Path
) -> None:
    """Saved beside the worktree, never in it, and gone at the end: a screenshot can hold what
    the ticket should not keep on the host."""
    image = RunAttachment(
        name="image-1",
        media_type="image/png",
        content=PNG,
        source="ticket_body",
        origin="https://example.com/s.png",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec(attachments=(image,)))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        [attached] = [arg for arg in argv_of(dump) if arg.startswith("--image=")]
        assert attached == f"--image={here}/attachments/image-1.png"
        assert (here / "attachments" / "image-1.png").read_bytes() == PNG
        assert files_holding(here / "worktree", "-not-really") == set()

        await adapter.finish(run_id)

        assert not (here / "attachments").exists()
    finally:
        await ended(adapter, root, run_id)


# ------------------------------------------------------------------ the sandbox check

#: As observed in the image under Docker's default seccomp profile: 206 characters.
BWRAP = (
    "bwrap: No permissions to create a new namespace, likely because the kernel does not allow "
    "non-privileged user namespaces. On e.g. debian this can be enabled with "
    "'sysctl kernel.unprivileged_userns_clone=1'."
)


class SandboxCli(CodexOriginCli):
    """The origin's git, and `codex sandbox ...` answered with `sandbox_result`."""

    def __init__(self, codex: Path, origin: Path) -> None:
        super().__init__(codex, origin)
        self.commands: list[tuple[str, ...]] = []
        self.command_envs: list[Mapping[str, str] | None] = []
        self.sandbox_result: tuple[int, str] = (0, "")

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        self.commands.append((program, *args))
        self.command_envs.append(env)
        if program == self.agent_bin and SUBCMD_SANDBOX in args:
            returncode, stderr = self.sandbox_result
            return CommandResult((program, *args), returncode, "", stderr)
        return await super().run(
            program, *args, env=env, cwd=cwd,
            inherit_host_env=inherit_host_env,
        )

    def checks(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if SUBCMD_SANDBOX in command]


def _no_run_dirs(root: Path) -> bool:
    return not any(path.is_dir() for path in (root / "runs").glob("*"))


@pytest.fixture
def refusing(codex: Path, origin: Path) -> SandboxCli:
    cli = SandboxCli(codex, origin)
    cli.sandbox_result = (1, BWRAP)
    return cli


async def test_a_sandbox_that_cannot_start_refuses_the_spawn_for_good(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    adapter = session_adapter(root, codex, origin, cli=refusing)

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec())

    assert raised.value.reason is SpawnReason.CONFIG
    assert "docker/seccomp.json" in str(raised.value)
    assert BWRAP[:40] in str(raised.value)
    assert _no_run_dirs(root)


async def test_the_refusal_quotes_bwraps_whole_line_and_one_full_stop(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    adapter = session_adapter(root, codex, origin, cli=refusing)

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(session_spec())

    message = str(raised.value)
    assert BWRAP.removesuffix(".") in message
    assert ".." not in message


async def test_a_rework_is_refused_before_it_adopts_anything(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    """Adopting first would raise about the unknown run instead, and can stop a ghost run."""
    adapter = session_adapter(root, codex, origin, cli=refusing)
    spec = session_spec(resume_from=ResumeFrom(run_id="gone", session_id="t"))

    with pytest.raises(SpawnError) as raised:
        await adapter.spawn(spec)

    assert raised.value.reason is SpawnReason.CONFIG
    assert "docker/seccomp.json" in str(raised.value)


@needs_tmux
async def test_resume_refuses_while_the_sandbox_cannot_start_and_keeps_the_run_parked(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    adapter = session_adapter(root, codex, origin)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.interrupt(run_id)
        # A restart: a new adapter on the same root, on a machine whose sandbox cannot start.
        restarted = session_adapter(root, codex, origin, cli=refusing)

        with pytest.raises(SpawnError) as raised:
            await restarted.resume(run_id, None, fresh_secrets())

        assert raised.value.reason is SpawnReason.CONFIG
        assert state_of(root, run_id).status is RunStatus.INTERRUPTED
        assert await server_of(root, run_id).pane() is None
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_passing_sandbox_check_runs_once_per_adapter(
    root: Path, codex: Path, origin: Path
) -> None:
    cli = SandboxCli(codex, origin)
    adapter = session_adapter(root, codex, origin, cli=cli)
    runs = [(await adapter.spawn(session_spec())).run_id for _ in range(2)]
    try:
        for run_id in runs:
            await reaches(adapter, run_id, RunStatus.IDLE)
        assert len(cli.checks()) == 1
    finally:
        for run_id in runs:
            await server_of(root, run_id).kill()
        await adapter.drain()


@needs_tmux
async def test_a_refused_sandbox_check_is_asked_again(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    """A refusal can be fixed while the worker runs, so only a pass is remembered."""
    adapter = session_adapter(root, codex, origin, cli=refusing)
    with pytest.raises(SpawnError):
        await adapter.spawn(session_spec())

    refusing.sandbox_result = (0, "")
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert len(refusing.checks()) == 2
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_the_sandbox_check_holds_no_run_secret_and_removes_its_scratch_codex_home(
    root: Path, codex: Path, origin: Path
) -> None:
    cli = SandboxCli(codex, origin)
    adapter = session_adapter(root, codex, origin, cli=cli)
    run_id = (await adapter.spawn(session_spec())).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        [index] = [i for i, command in enumerate(cli.commands) if SUBCMD_SANDBOX in command]
        assert cli.commands[index] == (
            cli.agent_bin,
            "sandbox",
            "-c",
            'sandbox_mode="workspace-write"',
            "--",
            "true",
        )
        env = cli.command_envs[index]
        assert env is not None and set(env) == {ENV_CODEX_HOME}
        scratch = Path(env[ENV_CODEX_HOME])
        assert scratch.parent == root
        assert scratch.name not in os.listdir(root), "the scratch home is removed"
    finally:
        await ended(adapter, root, run_id)


async def test_the_session_view_refuses_while_the_sandbox_cannot_start(
    root: Path, codex: Path, origin: Path, refusing: SandboxCli
) -> None:
    adapter = session_adapter(root, codex, origin, cli=refusing)

    with pytest.raises(InspectUnavailable, match=r"docker/seccomp\.json"):
        await adapter.inspect_start("any", token="t", limit=timedelta(minutes=5), credentials={})
