"""L1r: the whole `codex_cli` adapter on the real codex binary, against `mock_openai.py`. Its
interactive path, on the TUI in a host tmux (decision 56), is one scenario, its steps in order:
the launch from the seeds, a human's turn through the relay that commits and pushes inside the
narrowed sandbox, a failed turn, a worker restart, a rollover's interrupt and turnless resume,
the finish's pasted `/quit` with only a read-only viewer attached, a rework that continues the
thread on a pull request head shipping its own `.codex/` hooks, and a host lost mid-turn. Three
more sessions are a test each: a command's writes outside the worktree refused by the sandbox,
a cancel, which on Linux is shown to end the agent's command as well, and a skill in the
worker's `$HOME/.agents/skills` that must never reach the model.

Opt-in, and never in CI: it needs the pinned native binary and tmux. It takes about two
minutes, most of it codex's WebSocket retries before each launch's first turn. It runs on the
host, or inside the image under its seccomp profile (docs/local-container.md, "codex_cli in the
container").

    FACTORIAL_TUI_CODEX=<a native codex-cli PINNED_VERSION> \\
      uv run pytest -c pyproject.toml runtime/tests/test_codex_cli_interactive_local.py \\
      -q -p no:cacheprovider

The binary is `codex_tui_support.wrapper`, which a session's native check would refuse as a
script: that check is pointed at the binary the wrapper execs. The worker's environment is
replaced, for the test, by PATH, a scratch HOME and LANG, so no real key and no real `~/.codex`
is ever in reach. The origin is a local bare repo, which `CodexOriginCli` names in place of
GitHub for the host's git, and a `git daemon` serves for the agent's push: a push to a `file://`
origin would write outside the sandbox's one writable root, the clone's `.git`, as a push over
the network does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from codex_tui_support import (
    CODEX,
    DIALOGS,
    PINNED_CODEX,
    Mock,
    commit_and_push,
    mock_api,
    wrapper,
)
from host_session_support import (
    REPO,
    CodexOriginCli,
    bare_origin,
    git,
    run_dir,
    server_of,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import tmux
from runtime.adapters._cli.checkout import worktree_git_at
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.rework import REWORK_HEADER
from runtime.adapters._cli.session_state import LOG_FILE, HostRunState, read_state
from runtime.adapters.codex_cli import session as codex_session
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.cli import CONFIG_FILE, ENV_API_KEY, INPUT_MARK
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    ParkReason,
    ResumeFrom,
    RunSpec,
    RunStatus,
    Workspace,
)
from runtime.terminal import HostTmuxRelay, TerminalStream

pytestmark = PINNED_CODEX

MODEL = "gpt-5.3-codex"
TURN_WAIT = 90.0
POLL = 0.1
SETTLE = 0.5
API_KEY = "sk-proj-l1r-made-up-key-0123456789abcdef"
REWORK_KEY = "sk-proj-l1r-made-up-rework-key-zyxwvu98765"
TOKEN = "ghs_l1rmadeuptoken"
BRANCH = "spec/12-l1r"


@dataclass
class Host:
    root: Path
    scratch: Path
    origin: Path
    mock: Mock
    #: The `git daemon` URL of the origin, for the agent's push.
    served: str
    binary: Path

    def adapter(self) -> CodexCliAdapter:
        """A worker's adapter: each call is one (re)started."""
        cli = CodexOriginCli(self.binary, self.origin)
        return CodexCliAdapter(local_root=self.root, cli=cli, poll=POLL)

    @property
    def marks(self) -> Path:
        return self.scratch / "marks"


@pytest.fixture
def mock() -> Iterator[Mock]:
    with mock_api() as api:
        yield api


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def host(mock: Mock, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    scratch = Path(tempfile.mkdtemp(prefix="f243q-"))
    root = Path(tempfile.mkdtemp(prefix="f243q-", dir="/tmp")).resolve()
    tools = {str(Path(found).parent) for found in map(shutil.which, ("tmux", "git")) if found}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    (scratch / "home").mkdir()
    (scratch / "marks").mkdir()
    environment = {
        "PATH": os.pathsep.join([*sorted(tools), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        "HOME": str(scratch / "home"),
        "LANG": "C.UTF-8",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    binary = scratch / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(wrapper(mock.url))
    binary.chmod(0o755)
    real = codex_session._is_script
    monkeypatch.setattr(
        codex_session, "_is_script", lambda path: real(CODEX if path == str(binary) else path)
    )
    origin = bare_origin(scratch)
    port = _free_port()
    daemon = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            "--listen=127.0.0.1",
            f"--port={port}",
            f"--base-path={scratch}",
            "--export-all",
            "--enable=receive-pack",
            str(scratch),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield Host(root, scratch, origin, mock, f"git://127.0.0.1:{port}/origin.git", binary)
    finally:
        kill_servers(root)
        # `git daemon` runs `git-daemon` under it, which a kill of `git` alone leaves serving.
        os.killpg(daemon.pid, signal.SIGKILL)
        daemon.wait()
        subprocess.run(["pkill", "-KILL", "-f", str(root)], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


def kill_servers(root: Path) -> None:
    for each in (root / tmux.SOCKETS_DIRNAME).glob("*"):
        subprocess.run(["tmux", "-S", str(each), "kill-server"], capture_output=True)


def spec(marker: str, **overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(
            system=f"You are a test agent. SYS-{marker}",
            instructions=f"Reply with one word. TURN-{marker}",
        ),
        "model": MODEL,
        "max_turns": 50,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


def state_of(host: Host, run_id: str) -> HostRunState:
    return read_state(run_dir(host.root, run_id), run_id)


async def at_prompt(
    adapter: CodexCliAdapter, host: Host, run_id: str, reason: IdleReason, *, prompts: int = 0
) -> None:
    """IDLE for `reason`, once this segment's signals counted `prompts` prompts."""

    async def there() -> bool:
        status = await adapter.status(run_id)
        why = state_of(host, run_id).failure_reason
        assert not status.is_terminal, f"run {run_id} ended {status}: {why}"
        if status is not RunStatus.IDLE:
            return False
        idle = (await adapter.session_state(run_id)).idle_reason is reason
        return idle and state_of(host, run_id).signals.prompts >= prompts

    try:
        await until(there, f"run {run_id} idle on {reason}", within=TURN_WAIT)
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{screen(host, run_id)}") from None


def screen(host: Host, run_id: str) -> str:
    path = tmux.socket_path(host.root, run_id, "s", AssertionError)
    shown = subprocess.run(
        ["tmux", "-N", "-u", "-S", path, "capture-pane", "-p", "-t", "=run:"],
        capture_output=True,
        text=True,
    )
    return shown.stdout.replace("\xa0", " ")


def translated(host: Host, run_id: str) -> list[dict[str, Any]]:
    lines = (run_dir(host.root, run_id) / LOG_FILE).read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def user_items(host: Host, run_id: str) -> list[tuple[str | None, str]]:
    return [
        (line.get("factorial_origin"), line["item"].get("text", ""))
        for line in translated(host, run_id)
        if line["type"] == "item.completed" and line["item"]["type"] == "user_message"
    ]


@asynccontextmanager
async def terminal(adapter: CodexCliAdapter, run_id: str) -> AsyncIterator[TerminalStream]:
    """A writable Terminal tab on the run. A key sent in the instant the client attaches is
    lost, so the human takes a moment first."""
    target = await adapter.terminal_target(run_id)
    stream = await HostTmuxRelay().attach(
        target, writable=True, cols=200, rows=50, on_output=lambda _: None
    )
    try:
        await asyncio.sleep(SETTLE)
        yield stream
    finally:
        await stream.close()


def unspaced(text: str) -> str:
    return "".join(text.split())


async def say(adapter: CodexCliAdapter, host: Host, run_id: str, text: str) -> None:
    """A human types `text`, and presses Enter once all of it is drawn and a moment has passed:
    codex takes an Enter inside a burst of keys for a newline."""
    async with terminal(adapter, run_id) as tab:
        await draft(tab, host, run_id, text)
        await asyncio.sleep(SETTLE)
        await tab.write(b"\r")


async def draft(tab: TerminalStream, host: Host, run_id: str, text: str) -> None:
    typed = unspaced(f"{INPUT_MARK} {text}")
    before = unspaced(screen(host, run_id)).count(typed)
    await tab.write(text.encode())

    async def drawn() -> bool:
        return unspaced(screen(host, run_id)).count(typed) > before

    await until(drawn, f"{text!r} on the input line")


async def restarted(adapter: CodexCliAdapter) -> None:
    """A worker restart, in process: every supervisor stops where it stands, its lock let go."""
    tasks = list(adapter._supervisors.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def rollouts(host: Host, run_id: str) -> list[Path]:
    home = state_of(host, run_id).workspace_of or run_id
    return sorted((run_dir(host.root, home) / "config").glob("sessions/*/*/*/rollout-*.jsonl"))


def pushed(host: Host) -> str:
    return git("rev-parse", f"refs/heads/{BRANCH}", cwd=host.origin)


def head_of(host: Host, run_id: str) -> str:
    git_at = worktree_git_at(host.root, REPO, Path(state_of(host, run_id).worktree))
    assert git_at is not None
    return git(*git_at, "rev-parse", "HEAD", cwd=host.root)


async def launched(adapter: CodexCliAdapter, host: Host, marker: str) -> str:
    """Steps 1 and 2: a spawn that reaches its prompt with no dialog, its system prompt the
    model's developer message."""
    assert await adapter.spawn_refusal() is None
    run_id = (await adapter.spawn(spec(marker))).run_id
    await at_prompt(adapter, host, run_id, IdleReason.TURN_END, prompts=1)
    assert not DIALOGS.search(screen(host, run_id)), screen(host, run_id)
    turn = run_dir(host.root, run_id) / "turns" / "0.md"
    [first, *_] = host.mock.carrying(f"Read {turn} and do what it says.")
    developer = [
        item
        for item in first["body"]["input"]
        if item.get("role") == "developer" and f"SYS-{marker}" in json.dumps(item)
    ]
    assert developer, "the system prompt is not the model's developer message"
    return run_id


async def finished(adapter: CodexCliAdapter, host: Host, run_id: str) -> None:
    """Step 7: with a draft left on the input line, Ctrl-C only empties it, so the pasted
    `/quit` must land, with only a read-only viewer attached, for the CLI to leave by itself."""
    async with terminal(adapter, run_id) as tab:
        await draft(tab, host, run_id, "a thought left half typed")
    viewer = await HostTmuxRelay().attach(
        await adapter.terminal_target(run_id),
        writable=False,
        cols=200,
        rows=50,
        on_output=lambda _: None,
    )
    try:
        await adapter.finish(run_id)
    finally:
        await viewer.close()
    result = await adapter.result(run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.SUCCEEDED, None)
    assert result.cost_usd is None
    assert result.input_tokens is not None and result.input_tokens > 0
    assert result.stream_warnings == ()
    assert state_of(host, run_id).exit_code == 0, "the CLI left by the `/quit`"


def ship_codex_config(host: Host, marker: str) -> tuple[str, str]:
    """A reviewer's commit on the pull request's branch that adds a `.codex/` with hooks and
    developer instructions, as a repository may ship. Each leaves a mark if codex takes it up: a
    file, or the marker in a request. Returns the head and the marker."""
    review = host.scratch / "review"
    git("clone", "-q", "-b", BRANCH, str(host.origin), str(review), cwd=host.scratch)
    folder = review / ".codex"
    folder.mkdir()
    touch = f"touch {host.marks / 'repo-hook'}"
    hooks = "".join(
        f'[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = "command"\ncommand = "{touch}"\n'
        for event in ("SessionStart", "UserPromptSubmit", "Stop")
    )
    (folder / "config.toml").write_text(f'developer_instructions = "{marker}"\n\n{hooks}')
    hooked = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{touch}-json"}]}]}}
    (folder / "hooks.json").write_text(json.dumps(hooked))
    git("add", "-A", cwd=review)
    git("commit", "-q", "-m", "review", cwd=review)
    git("push", "-q", "origin", BRANCH, cwd=review)
    return git("rev-parse", "HEAD", cwd=review), marker


async def test_a_codex_session_on_the_real_tui_from_spawn_to_rework_and_a_lost_host(
    host: Host,
) -> None:
    adapter = host.adapter()
    adapters = [adapter]
    try:
        # 1-2. The refusal passes the pinned binary; the seeds take the consent screen alone.
        a = await launched(adapter, host, "A")
        auth = {r["authorization"] for r in host.mock.main()}
        assert auth == {f"Bearer {API_KEY}"}
        thread = state_of(host, a).session_id
        assert thread is not None
        [log] = rollouts(host, a)
        config = tomllib.loads((run_dir(host.root, a) / "config" / CONFIG_FILE).read_text())
        clone_git = str(host.root / "repos" / REPO / ".git")
        assert config["sandbox_workspace_write"]["writable_roots"] == [clone_git]

        # 3. A human's turn through the relay: the agent commits and pushes from its sandbox.
        await say(adapter, host, a, commit_and_push(host.served, BRANCH, 1))
        await at_prompt(adapter, host, a, IdleReason.TURN_END, prompts=2)
        assert (await adapter.session_state(a)).last_input_at is not None

        async def human_mirrored() -> bool:
            return any(origin == "human" for origin, _ in user_items(host, a))

        await until(human_mirrored, "the human's turn in the translated mirror")
        assert pushed(host) == head_of(host, a), screen(host, a)

        # 4. A failed turn idles the session on its error.
        await say(adapter, host, a, "FAILME please")
        await at_prompt(adapter, host, a, IdleReason.TURN_ERROR, prompts=3)

        # 5. A worker restart: a new adapter takes the spawn's server back by its turn file.
        await restarted(adapter)
        adapter = host.adapter()
        adapters.append(adapter)
        assert await adapter.status(a) is RunStatus.IDLE

        async def taken_back() -> bool:
            supervisor = adapter._attached(a)
            return supervisor is not None and supervisor._proven

        await until(taken_back, "the new supervisor to prove the server is the run's")
        await asyncio.sleep(SETTLE)
        assert await adapter.status(a) is RunStatus.IDLE
        items = [json.dumps(line) for line in translated(host, a)]
        items = [line for line in items if '"item.completed"' in line]
        assert len(items) == len(set(items))

        # 6. A rollover's interrupt, and its turnless resume on the same rollout.
        await adapter.interrupt(a, ParkReason.ROLLOVER)
        assert await adapter.status(a) is RunStatus.INTERRUPTED
        secrets = {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)}
        await adapter.resume(a, None, secrets)
        await at_prompt(adapter, host, a, IdleReason.TURN_ERROR)
        assert rollouts(host, a) == [log]
        assert state_of(host, a).session_id == thread
        await say(adapter, host, a, "Reply with one word. RESUMED")
        await at_prompt(adapter, host, a, IdleReason.TURN_END, prompts=1)

        # 7. The finish's pasted `/quit`, under a read-only viewer.
        head, repo_marker = ship_codex_config(host, f"REPO-{uuid.uuid4().hex[:8]}")
        await finished(adapter, host, a)
        log_size = log.stat().st_size
        segments = state_of(host, a).segments

        # 8. A rework continues the thread in A's workspace, at the pull request's head.
        rework = spec(
            "B",
            resume_from=ResumeFrom(run_id=a, session_id=thread),
            secrets={ENV_API_KEY: SecretStr(REWORK_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
        )
        b = (await adapter.spawn(rework)).run_id
        state = await adapter.session_state(b)
        assert (state.session_id, state.log_offset) == (thread, log_size)
        assert log_size > 0
        turn_file = run_dir(host.root, a) / "turns" / f"{segments}.md"
        turn = turn_file.read_text("utf-8")
        assert turn.startswith(REWORK_HEADER.format(branch=BRANCH, head=head)), turn[:300]
        await at_prompt(adapter, host, b, IdleReason.TURN_END, prompts=1)
        assert rollouts(host, a) == [log]
        resumed = host.mock.carrying(f"Read {turn_file} and do what it says.")
        assert resumed and {r["authorization"] for r in resumed} == {f"Bearer {REWORK_KEY}"}
        config = tomllib.loads((run_dir(host.root, a) / "config" / CONFIG_FILE).read_text())
        assert config["sandbox_workspace_write"]["writable_roots"] == [clone_git]

        # 10. The pull request's `.codex/` reached B's checkout, and codex took none of it.
        assert (Path(state_of(host, b).worktree) / ".codex" / "config.toml").is_file()
        assert not DIALOGS.search(screen(host, b)), screen(host, b)
        await say(adapter, host, b, commit_and_push(host.served, BRANCH, 2))
        await at_prompt(adapter, host, b, IdleReason.TURN_END, prompts=2)
        assert pushed(host) == head_of(host, b), screen(host, b)
        assert list(host.marks.iterdir()) == [], "a hook the checkout shipped ran"
        assert not host.mock.carrying(repo_marker), "the checkout's instructions were loaded"

        await finished(adapter, host, b)
        b_users = user_items(host, b)
        assert [origin for origin, _ in b_users] == ["orchestrator", "human"]
        assert list(host.marks.iterdir()) == []

        # 9. The host goes mid-turn: the run fails, `host_lost`.
        c = await launched(adapter, host, "C")
        await say(adapter, host, c, "SLOWME")
        await until(lambda: asyncio.to_thread(lambda: bool(host.mock.carrying("SLOWME"))), "slow")
        await server_of(host.root, c).kill()
        await until(
            lambda: asyncio.to_thread(lambda: state_of(host, c).status.is_terminal),
            "the lost run's end",
            within=TURN_WAIT,
        )
        result = await adapter.result(c)
        assert result.outcome is RunStatus.FAILED
        assert (result.failure_reason or "").startswith("host_lost"), result.failure_reason
    finally:
        kill_servers(host.root)
        for each in adapters:
            await each.drain()


# ---------------------------------------------------------------- the sandbox, and a cancel


def sleep_mark() -> str:
    return f"7{uuid.uuid4().int % 10**6:06d}"


def sleeping(mark: str) -> bool:
    found = subprocess.run(["pgrep", "-f", f"sleep {mark}"], capture_output=True, text=True)
    return bool(found.stdout.strip())


def unsleep(mark: str) -> None:
    """Not under the scratch root, so the fixture's own sweep would miss it."""
    subprocess.run(["pkill", "-KILL", "-f", f"sleep {mark}"], capture_output=True)


async def started(mark: str) -> None:
    await until(lambda: asyncio.to_thread(sleeping, mark), f"sleep {mark}", within=TURN_WAIT)


async def ended_on_linux(mark: str) -> None:
    """On Linux the command dies with bubblewrap's PID namespace. On macOS `/bin/sleep` outlives
    the run: codex starts it in a session of its own, and the sweep by environment cannot read
    an Apple binary's (`_cli/escaped.py`)."""
    if sys.platform != "linux":
        return

    async def gone() -> bool:
        return not await asyncio.to_thread(sleeping, mark)

    await until(gone, f"sleep {mark} to end")


async def test_a_session_cannot_write_outside_its_worktree(host: Host) -> None:
    adapter = host.adapter()
    targets = [host.scratch / "escape", host.root / "runs" / "escape"]
    # Split in the prompt and whole only in the command's output, so the mock proves it ran.
    script = "; ".join(f"touch {t} 2>/dev/null && echo WRO''TE || echo BLOCK''ED" for t in targets)
    try:
        run_id = await launched(adapter, host, "S2")
        await say(adapter, host, run_id, f"RUNME {script}")
        await at_prompt(adapter, host, run_id, IdleReason.TURN_END, prompts=2)
        assert not host.mock.carrying("WROTE"), "a write outside the worktree went through"
        assert host.mock.carrying("BLOCKED"), "the command never ran"
        assert not [t for t in targets if t.exists()]
        await adapter.finish(run_id)
    finally:
        kill_servers(host.root)
        await adapter.drain()


async def test_cancelling_a_session_ends_the_agents_command(host: Host) -> None:
    adapter = host.adapter()
    mark = sleep_mark()
    try:
        run_id = await launched(adapter, host, "S3")
        await say(adapter, host, run_id, f"RUNME sleep {mark}")
        await started(mark)
        await adapter.cancel(run_id)

        async def cancelled() -> bool:
            return await adapter.status(run_id) is RunStatus.CANCELLED

        await until(cancelled, f"run {run_id} cancelled", within=60)
        # The mock's command call yields after two seconds: a run left going hands its output on.
        assert not host.mock.carrying('"function_call_output"'), "the run went on past the cancel"
        await ended_on_linux(mark)
    finally:
        await asyncio.to_thread(unsleep, mark)
        kill_servers(host.root)
        await adapter.drain()


async def test_a_session_reads_no_skill_from_the_operators_home(host: Host) -> None:
    """codex loads `$HOME/.agents/skills` into the developer message whatever `CODEX_HOME`
    says, so a session's HOME must be its own, not the worker's."""
    marker = f"OPERATOR-{uuid.uuid4().hex[:8]}"
    skill = Path(os.environ["HOME"]) / ".agents" / "skills" / marker.lower() / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {marker.lower()}\ndescription: {marker}\n---\n{marker}\n")
    adapter = host.adapter()
    try:
        run_id = await launched(adapter, host, "S4")
        carried = [r["path"] for r in host.mock.requests() if marker in json.dumps(r["body"])]
        assert not carried, f"the operator's skill {marker} reached the model: {carried}"
        await adapter.finish(run_id)
    finally:
        kill_servers(host.root)
        await adapter.drain()
