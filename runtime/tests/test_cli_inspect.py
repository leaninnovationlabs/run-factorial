"""Open session on the host adapters (decision 57), against real tmux and the fake TUIs.

The view is the run's own CLI resumed on its session, for a person to read: what these tests
hold is what it is launched with (no GitHub token, no MCP server, nothing typed), where it may
open, and that a resume ends it before a credential file is written again. Every root sits
under `mkdtemp(dir="/tmp")`, since pytest's tmp_path on macOS is too long for a tmux socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from host_session_support import (
    REPO,
    CodexOriginCli,
    OriginCli,
    bare_origin,
    fake_tui_settings_reach_sessions,  # noqa: F401
    reaches,
    run_dir,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import host_session, session_state
from runtime.adapters._cli.inspect import RECORD_FILE, SESSION, VIEW_ENV_FILE, _Record
from runtime.adapters._cli.process import CommandResult
from runtime.adapters._cli.tmux import SOCKETS_DIRNAME
from runtime.adapters.claude_cli import session as claude_session
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.cli import FLAG_SETTING_SOURCES
from runtime.adapters.claude_cli.session import SESSION_CREDENTIALS as CLAUDE_CREDENTIALS
from runtime.adapters.claude_cli.tui import view_flags
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.cli import CONFIG_FILE, SUBCMD_SANDBOX, _Cli
from runtime.adapters.codex_cli.session import SESSION_CREDENTIALS as CODEX_CREDENTIALS
from runtime.contract import (
    AgentPrompts,
    InspectLoginRefused,
    InspectNoSession,
    InspectPhase,
    InspectState,
    InspectUnavailable,
    McpServerSpec,
    McpTransport,
    RunSpec,
    RunStatus,
    TerminalTarget,
    UnknownRunError,
    Workspace,
)
from runtime.terminal import HostTmuxRelay

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux"),
]

Kind = Literal["claude_cli", "codex_cli"]
KINDS: tuple[Kind, ...] = ("claude_cli", "codex_cli")
LIMIT = timedelta(minutes=55)
RUN_ID = "GH-12-plan-agent-0a1b2c3d"
#: In the worker's environment for every test: none of it may reach the view.
WORKER_SECRETS = {
    "FACTORIAL_MASTER_KEY": "factorial-master-key-must-not-reach-the-view",
    "FACTORIAL_DATABASE_URL": "postgresql://factorial:secret@db/factorial",
    "GH_TOKEN": "ghs_hostTokenMustNotReachTheView000",
    "GITHUB_TOKEN": "ghs_hostGithubTokenMustNotReachTheView",
}
MODEL_KEY: dict[Kind, str] = {"claude_cli": "ANTHROPIC_API_KEY", "codex_cli": "OPENAI_API_KEY"}
SPAWN_TOKEN = "ghs_theSpawnsMintedToken"
VIEW_TOKEN = "ghs_handedToTheViewMustNotReachIt"
DOCS_TOKEN = "docs-token-held-only-by-the-session"
MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)
FAKES: dict[Kind, str] = {"claude_cli": "fake_claude_tui.py", "codex_cli": "fake_codex_tui.py"}
CREDENTIALS: dict[Kind, tuple[str, ...]] = {
    "claude_cli": CLAUDE_CREDENTIALS,
    "codex_cli": CODEX_CREDENTIALS,
}


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for key, value in WORKER_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TMUX", raising=False)
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix="f243i-", dir="/tmp")))
    yield path
    for socket in (path / SOCKETS_DIRNAME).glob("*"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return bare_origin(tmp_path)


def credentials(kind: Kind) -> dict[str, SecretStr]:
    """What the worker hands a view: the model credential, and here a GitHub token too."""
    return {
        MODEL_KEY[kind]: SecretStr(f"sk-the-view-key-for-{kind}-0123456789"),
        "GH_TOKEN": SecretStr(VIEW_TOKEN),
    }


class SandboxStartsCli(_Cli):
    """The real seam, except that codex's sandbox check passes with no `codex` on PATH."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if SUBCMD_SANDBOX in args:
            return CommandResult((program, *args), 0, "", "")
        return await super().run(
            program,
            *args,
            env=env,
            cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


def adapter_for(root: Path, kind: Kind) -> ClaudeCliAdapter | CodexCliAdapter:
    if kind == "claude_cli":
        return ClaudeCliAdapter(local_root=root)
    return CodexCliAdapter(local_root=root, cli=SandboxStartsCli())


def session_adapter(
    root: Path, kind: Kind, origin: Path, tmp_path: Path
) -> ClaudeCliAdapter | CodexCliAdapter:
    """The CLI is the kind's fake TUI behind an absolute path, as `shutil.which` resolves it."""
    cli = tmp_path / "bin" / kind
    cli.parent.mkdir(exist_ok=True)
    fake = Path(__file__).with_name(FAKES[kind])
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    cli.chmod(0o755)
    if kind == "claude_cli":
        return ClaudeCliAdapter(local_root=root, cli=OriginCli(cli, origin), poll=0.05)
    return CodexCliAdapter(local_root=root, cli=CodexOriginCli(cli, origin), poll=0.05)


def session_spec(kind: Kind) -> RunSpec:
    model = "claude-haiku-4-5-20251001" if kind == "claude_cli" else "gpt-5.3-codex"
    return RunSpec(
        agent="spec-agent",
        ticket="GH-12",
        workspace=Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        prompts=AgentPrompts(system="You plan.", instructions="Write the spec."),
        model=model,
        max_turns=10,
        secrets={
            MODEL_KEY[kind]: SecretStr(f"sk-the-spawns-key-for-{kind}-0123456789"),
            "GH_TOKEN": SecretStr(SPAWN_TOKEN),
            "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
        },
        mcp=(MCP_HTTP,),
    )


async def kept_session(
    root: Path, kind: Kind, origin: Path, tmp_path: Path, *, parked: bool = False
) -> tuple[ClaudeCliAdapter | CodexCliAdapter, str]:
    """A session run with an MCP server, ended as the worker ends one: finished, or parked."""
    adapter = session_adapter(root, kind, origin, tmp_path)
    run_id = (await adapter.spawn(session_spec(kind))).run_id
    await reaches(adapter, run_id, RunStatus.IDLE)
    if parked:
        await adapter.interrupt(run_id)
    else:
        await adapter.finish(run_id)
    assert (await adapter.status(run_id)).is_terminal
    return adapter, run_id


def kept_run(
    root: Path,
    kind: Kind,
    *,
    before_sessions: bool = False,
    session_id: str | None = "session-1",
    status: RunStatus = RunStatus.SUCCEEDED,
    worktree: str | None = None,
) -> Path:
    """A run's state and its workspace as the adapter leaves them at its end, with no session
    behind them: enough for a refusal. `before_sessions` writes it as a run from before every
    run was a session did, which never resolved its checkout for a TUI."""
    folder = root / "runs" / RUN_ID
    tree = folder / "worktree"
    tree.mkdir(parents=True, exist_ok=True)
    (folder / "config").mkdir(exist_ok=True)
    now = datetime(2026, 10, 2, tzinfo=UTC)
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "status": status,
        "agent": "plan-agent",
        "ticket": "GH-12",
        "repo": REPO,
        "branch": "spec/12-x",
        "worktree": worktree or str(tree),
        "started_at": now,
        "session_id": session_id,
        "mcp": (),
        "workspace_real": None if before_sessions else str(tree),
    }
    model = "" if kind == "claude_cli" else "gpt-5"
    session_state.write_state(folder, session_state.HostRunState(model=model, **fields))
    return tree


def tmux(target: TerminalTarget, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-N", "-S", target.socket, *args], capture_output=True, text=True, check=False
    )


def opened(state: InspectState) -> TerminalTarget:
    assert state.phase is InspectPhase.OPEN and state.target is not None, state
    return state.target


def credentials_left(root: Path, kind: Kind, run_id: str) -> list[str]:
    config = run_dir(root, run_id) / "config"
    return [name for name in CREDENTIALS[kind] if (config / name).exists()]


def screen(target: TerminalTarget) -> str:
    return tmux(target, "capture-pane", "-p", "-t", f"={SESSION}:").stdout


async def at_its_prompt(
    adapter: ClaudeCliAdapter | CodexCliAdapter, target: TerminalTarget
) -> None:
    async def there() -> bool:
        return host_session._at_prompt(screen(target), adapter._tui)

    await until(there, "the view at its session's prompt", within=5)


def session_id_of(root: Path, kind: Kind, run_id: str) -> str | None:
    return session_state.read_state(run_dir(root, run_id), run_id).session_id


@pytest.mark.parametrize("kind", KINDS)
async def test_open_session_resumes_the_run_with_no_github_token_and_no_mcp(
    root: Path, kind: Kind, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session ran with a GitHub token and an MCP server; its view gets neither, from the
    credentials it is handed or from the worker's own environment."""
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))

    opened(
        await adapter.inspect_start(run_id, token="1", limit=LIMIT, credentials=credentials(kind))
    )

    async def dumped() -> bool:
        return await asyncio.to_thread(dump.exists)

    await until(dumped, "the view's CLI started")
    seen = json.loads(dump.read_text())
    env: dict[str, str] = seen["env"]
    assert not [key for key in env if key.startswith("GH_") or "GITHUB" in key]
    assert not [key for key, held in env.items() if "credential." in held], "nor git's helper"
    for value in (VIEW_TOKEN, SPAWN_TOKEN, DOCS_TOKEN, *WORKER_SECRETS.values()):
        assert not [key for key, held in env.items() if value in held], value
    assert seen["cwd"] == str(run_dir(root, run_id) / "worktree")
    argv: list[str] = seen["argv"][1:]
    session_id = session_id_of(root, kind, run_id)
    config = run_dir(root, run_id) / "config"
    if kind == "claude_cli":
        assert argv[:2] == ["--resume", session_id]
        assert "--mcp-config" not in argv and "--strict-mcp-config" in argv
        assert not (config / "mcp.json").exists()
    else:
        assert (argv[0], argv[-1]) == ("resume", session_id)
        written = (config / CONFIG_FILE).read_text()
        assert "mcp_servers" not in written and DOCS_TOKEN not in written
    assert await adapter.status(run_id) is RunStatus.SUCCEEDED

    await adapter.inspect_stop(run_id)
    assert credentials_left(root, kind, run_id) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_views_git_runs_no_hook_or_fsmonitor_the_checkout_names(
    root: Path, kind: Kind, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view runs beside the model key in a worktree whose git config the agent wrote: its
    env turns hooks and fsmonitor off above that config (`view_git_env`)."""
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))

    opened(
        await adapter.inspect_start(run_id, token="1", limit=LIMIT, credentials=credentials(kind))
    )

    async def dumped() -> bool:
        return await asyncio.to_thread(dump.exists)

    await until(dumped, "the view's CLI started")
    env: dict[str, str] = json.loads(dump.read_text())["env"]
    entries = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env.get("GIT_CONFIG_COUNT", "0")))
    ]
    assert entries[-2:] == [("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null")]
    await adapter.inspect_stop(run_id)


@pytest.mark.parametrize("kind", KINDS)
async def test_nothing_is_typed_into_a_view_but_codexs_consent_is_taken(
    root: Path, kind: Kind, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex asks about the folder at every resume; nobody may type into a view, so the adapter
    takes it, or the viewer stares at a dialog."""
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    keys = tmp_path / "keys"
    monkeypatch.setenv("FAKE_TUI_KEYS", str(keys))

    target = opened(
        await adapter.inspect_start(run_id, token="1", limit=LIMIT, credentials=credentials(kind))
    )

    await at_its_prompt(adapter, target)
    assert "Open restricted" not in screen(target)
    assert not keys.exists() or keys.read_bytes() == b""
    await adapter.inspect_stop(run_id)


async def test_a_view_leaves_the_session_log_as_the_session_left_it(
    root: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude writes its cost to the log on its way out, a view's too, and not at once: a
    resume would read what a view wrote there as its session's."""
    adapter, run_id = await kept_session(root, "claude_cli", origin, tmp_path)
    monkeypatch.setenv("FAKE_TUI", "slow_exit")
    session_id = session_id_of(root, "claude_cli", run_id)
    assert session_id is not None
    session_log = claude_session.session_log(run_dir(root, run_id) / "config", session_id)
    before = session_log.read_bytes()
    target = opened(
        await adapter.inspect_start(
            run_id, token="1", limit=LIMIT, credentials=credentials("claude_cli")
        )
    )
    await at_its_prompt(adapter, target)
    pid = int(tmux(target, "display-message", "-p", "-t", f"={SESSION}:", "#{pane_pid}").stdout)

    await adapter.inspect_stop(run_id)

    async def exited() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    await until(exited, "the view's CLI gone", within=5)
    assert session_log.read_bytes() == before


async def test_a_view_names_no_mcp_config_whatever_the_state_file_says() -> None:
    tampered = session_state.HostRunState(
        run_id=RUN_ID,
        status=RunStatus.SUCCEEDED,
        agent="plan-agent",
        ticket="GH-12",
        repo=REPO,
        branch="spec/12-x",
        worktree="/w",
        started_at=datetime(2026, 10, 2, tzinfo=UTC),
        resume_args=(
            "--model",
            "m",
            "--mcp-config=/a.json",
            "--mcp-config",
            "/b.json",
            "/c.json",
            "--allowed-tools",
            "mcp__x",
        ),
    )

    flags = claude_session.ClaudeTui().view_flags(tampered, worktree=Path("/w"))

    assert flags[:4] == ("--model", "m", "--allowed-tools", "mcp__x")
    assert "--mcp-config" not in flags
    assert "--strict-mcp-config" in flags


async def test_a_view_loads_the_user_settings_alone() -> None:
    """A view runs beside the model key: no hook a checkout ships may run in it."""
    flags = view_flags(("--model", "claude-sonnet-5-5", "--mcp-config", "/x/mcp.json"))

    at = flags.index(FLAG_SETTING_SOURCES)
    assert flags[at + 1] == "user"
    assert "--mcp-config" not in flags


async def test_a_view_on_a_refused_login_fails_naming_the_credential(
    root: Path, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, run_id = await kept_session(root, "codex_cli", origin, tmp_path)
    monkeypatch.setenv("FAKE_TUI", "sign_in")
    monkeypatch.setattr(host_session, "CONSENT_WAIT", 1.0)

    with pytest.raises(InspectLoginRefused, match="store a fresh one"):
        await adapter.inspect_start(
            run_id, token="1", limit=LIMIT, credentials=credentials("codex_cli")
        )

    assert credentials_left(root, "codex_cli", run_id) == []
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.STOPPED
    assert not list((root / SOCKETS_DIRNAME).glob("*-i"))


@pytest.mark.parametrize("kind", KINDS)
async def test_the_startup_sweep_deletes_the_env_file_a_view_left(root: Path, kind: Kind) -> None:
    """A worker restart under an open view, as a deploy is, leaves its env file and the key in
    it."""
    left = kept_run(root, kind).parent / "config" / VIEW_ENV_FILE
    left.write_text("export KEY=sk-left-by-a-view\n")

    adapter_for(root, kind).sweep_credentials()

    assert not left.exists()


async def test_a_launch_its_worker_never_finished_ends_at_its_limit(root: Path) -> None:
    """Its worker died with the record STARTING: a status read past the limit ends it, and cuts
    back what the view's CLI wrote to the log."""
    folder = kept_run(root, "claude_cli").parent
    session_log = claude_session.session_log(folder / "config", "session-1")
    session_log.parent.mkdir(parents=True)
    session_log.write_text("kept\nthe view's\n")
    started = datetime.now(UTC) - LIMIT - timedelta(minutes=2)
    record = _Record(
        token="r1",
        phase=InspectPhase.STARTING,
        started_at=started,
        deadline=started + LIMIT,
        log_size=len("kept\n"),
    )
    (folder / RECORD_FILE).write_text(record.model_dump_json())

    ended = await adapter_for(root, "claude_cli").inspect_status(RUN_ID)

    assert (ended.phase, ended.detail) == (InspectPhase.STOPPED, "stopped at its limit")
    assert session_log.read_text() == "kept\n"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("before_sessions", "session_id"),
    [(True, "session-1"), (False, None)],
    ids=["from before every run was a session", "no session"],
)
async def test_a_run_with_no_session_to_resume_is_refused_a_view(
    root: Path, kind: Kind, before_sessions: bool, session_id: str | None
) -> None:
    kept_run(root, kind, before_sessions=before_sessions, session_id=session_id)

    with pytest.raises(InspectNoSession):
        await adapter_for(root, kind).inspect_start(
            RUN_ID, token="r1", limit=LIMIT, credentials=credentials(kind)
        )
    assert not (root / SOCKETS_DIRNAME).exists(), "refused before any tmux server existed"


@pytest.mark.parametrize("kind", KINDS)
async def test_inspect_start_is_idempotent_and_a_relay_attaches_to_its_target(
    root: Path, kind: Kind, origin: Path, tmp_path: Path
) -> None:
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    first = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=credentials(kind)
    )
    target = opened(first)
    server = tmux(target, "display-message", "-p", "-t", f"={SESSION}:", "#{pid}").stdout

    again = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=credentials(kind)
    )

    assert again.started_at == first.started_at and again.token == "r1"
    assert tmux(target, "display-message", "-p", "-t", f"={SESSION}:", "#{pid}").stdout == server
    assert TerminalTarget.model_validate(target.model_dump()) == target
    stream = await HostTmuxRelay().attach(
        target, writable=False, cols=80, rows=24, on_output=lambda _: None
    )
    try:
        assert (await adapter.inspect_status(run_id)).clients == 1
    finally:
        await stream.close()
    assert (await adapter.inspect_status(run_id)).clients == 0
    await adapter.inspect_stop(run_id)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "case", ["running", "foreign worktree", "linked worktree", "gone", "linked config home"]
)
async def test_a_view_opens_only_in_a_finished_runs_own_workspace(
    root: Path, kind: Kind, case: str
) -> None:
    """The state file names the worktree, and on a host the agent can write that file."""
    outside = root / "outside"
    outside.mkdir()
    if case == "running":
        kept_run(root, kind, status=RunStatus.RUNNING)
    elif case == "foreign worktree":
        kept_run(root, kind, worktree=str(outside))
    elif case == "linked config home":
        config = kept_run(root, kind).parent / "config"
        config.rmdir()
        config.symlink_to(outside)
    else:
        tree = kept_run(root, kind)
        shutil.rmtree(tree)
        if case == "linked worktree":
            tree.symlink_to(outside)

    error = UnknownRunError if case in ("gone", "linked config home") else InspectUnavailable
    with pytest.raises(error):
        await adapter_for(root, kind).inspect_start(
            RUN_ID, token="r1", limit=LIMIT, credentials=credentials(kind)
        )
    assert not (root / SOCKETS_DIRNAME).exists(), "refused before any tmux server existed"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_start_with_the_same_token_is_refused_once_the_run_is_live_again(
    root: Path, kind: Kind, origin: Path, tmp_path: Path
) -> None:
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    opened(
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=credentials(kind))
    )
    folder = run_dir(root, run_id)
    state = session_state.read_state(folder, run_id)
    session_state.write_state(folder, state.model_copy(update={"status": RunStatus.RUNNING}))

    with pytest.raises(InspectUnavailable, match="still running"):
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=credentials(kind))
    await adapter.inspect_stop(run_id)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_view_whose_cli_exits_is_stopped_and_a_stop_is_idempotent(
    root: Path, kind: Kind, origin: Path, tmp_path: Path
) -> None:
    adapter, run_id = await kept_session(root, kind, origin, tmp_path)
    target = opened(
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=credentials(kind))
    )

    pid = tmux(target, "display-message", "-p", "-t", f"={SESSION}:", "#{pane_pid}").stdout
    os.kill(int(pid), signal.SIGKILL)
    for _ in range(100):
        if tmux(target, "has-session", "-t", f"={SESSION}").returncode != 0:
            break
        await asyncio.sleep(0.05)
    exited = await adapter.inspect_status(run_id)

    assert (exited.phase, exited.detail) == (InspectPhase.STOPPED, "the session exited")
    assert credentials_left(root, kind, run_id) == []
    reopened = opened(
        await adapter.inspect_start(run_id, token="r2", limit=LIMIT, credentials=credentials(kind))
    )
    await adapter.inspect_stop(run_id)
    await adapter.inspect_stop(run_id)
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.STOPPED
    assert tmux(reopened, "has-session", "-t", f"={SESSION}").returncode != 0
    assert not await asyncio.to_thread(Path(reopened.socket).exists)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "unrecorded"])
async def test_a_resume_ends_the_view_before_it_writes_credentials(
    root: Path,
    kind: Kind,
    origin: Path,
    tmp_path: Path,
    recorded: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the record says: a worker can die between a view's start and its record."""
    adapter, run_id = await kept_session(root, kind, origin, tmp_path, parked=True)
    target = opened(
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=credentials(kind))
    )
    if not recorded:
        await asyncio.to_thread((run_dir(root, run_id) / RECORD_FILE).unlink)
    open_at_write: list[bool] = []
    real = host_session._write_launch

    def write_launch(*args: Any, **kwargs: Any) -> None:
        open_at_write.append(tmux(target, "has-session", "-t", f"={SESSION}").returncode == 0)
        real(*args, **kwargs)

    monkeypatch.setattr(host_session, "_write_launch", write_launch)

    await adapter.resume(run_id, None, session_spec(kind).secrets)
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert open_at_write == [False]
        assert (await adapter.inspect_status(run_id)).phase is InspectPhase.STOPPED
    finally:
        await adapter.cancel(run_id)
        await adapter.drain()
