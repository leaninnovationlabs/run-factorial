"""L1r: the whole `claude_cli` adapter's interactive path on the real claude TUI, in a host tmux,
against `mock_anthropic.py` (decision 56). One scenario, its steps in order: the launch from the
seeds, a human's turn and answer through the relay, a worker restart, an interrupt and its
resume, the finish's pasted `/exit` with only a read-only viewer attached, a rework that
continues the session, and a host lost mid-turn. A subscription run repeats the launch and the
finish, and a third session, whose agent commits, must write nothing into the worker's HOME.

Opt-in, and never in CI: it needs the pinned CLI and tmux. It takes under half a minute.

    FACTORIAL_TUI_CLAUDE=<a claude PINNED_VERSION binary> \\
      uv run pytest -c pyproject.toml runtime/tests/test_claude_cli_interactive_local.py \\
      -q -p no:cacheprovider

The worker's environment is replaced, for the test, by PATH, a scratch HOME, LANG, the mock's
`ANTHROPIC_BASE_URL`, and made-up values of what a parent Claude Code session exports, which the
TUI's environment drops. The secrets are made up, so no real key and no real `~/.claude` is ever
in reach. `local_root` is named through /tmp, which macOS links to /private/tmp, so the seed must
trust the checkout's real path. The origin is a local bare repo, which `OriginCli` names in
place of GitHub.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from claude_tui_support import CLAUDE, DIALOGS, INPUT_MARK, PINNED_CLI, Mock, mock_api
from host_session_support import (
    REPO,
    OriginCli,
    bare_origin,
    git,
    run_dir,
    server_of,
    state_of,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import host_session as session
from runtime.adapters._cli import tmux
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.rework import REWORK_HEADER
from runtime.adapters._cli.session_state import LOG_FILE
from runtime.adapters.claude_cli import session as claude_session
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION
from runtime.adapters.claude_cli.cli import ENV_API_KEY, ENV_OAUTH_TOKEN
from runtime.adapters.claude_cli.transcript import Transcript
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

pytestmark = PINNED_CLI

TURN_WAIT = 60.0
POLL = 0.1
SETTLE = 0.5
API_KEY = "sk-ant-api03-l1r-made-up-key-0123456789abcdef"
REWORK_KEY = "sk-ant-api03-l1r-made-up-rework-key-zyxwvu98765"
OAUTH = "sk-ant-oat01-l1r-made-up-token-0123456789"
TOKEN = "ghs_l1rmadeuptoken"
BRANCH = "spec/12-l1r"
#: What a Claude Code session the worker runs under exports, with made-up values.
PARENT_SESSION = {
    "CLAUDECODE": "1",
    "AI_AGENT": "claude-code",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": "00000000-0000-4000-8000-000000000000",
    "CLAUDE_CODE_SSE_PORT": "1",
    "CLAUDE_CODE_MESSAGING_TOKEN": "made-up",
    "TMUX": "/tmp/not-a-server,1,0",
    "TMUX_PANE": "%0",
}


@dataclass
class Host:
    #: As the operator names it: through /tmp, a link on macOS.
    given: Path
    #: As the adapter resolves it.
    root: Path
    origin: Path
    mock: Mock
    #: Digits no other process's command line is likely to hold.
    mark: str

    def adapter(self, auth_mode: str = AUTH_MODE_API_KEY) -> ClaudeCliAdapter:
        """A worker's adapter: each call is one (re)started."""
        cli = OriginCli(Path(CLAUDE), self.origin)
        return ClaudeCliAdapter(local_root=self.given, cli=cli, auth_mode=auth_mode, poll=POLL)


@pytest.fixture
def mock() -> Iterator[Mock]:
    with mock_api() as api:
        yield api


@pytest.fixture
def host(mock: Mock, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    scratch = Path(tempfile.mkdtemp(prefix="f243r-"))
    given = Path(tempfile.mkdtemp(prefix="f243r-", dir="/tmp"))
    root = given.resolve()
    tools = {str(Path(found).parent) for found in map(shutil.which, ("tmux", "git")) if found}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    (scratch / "home").mkdir()
    environment = {
        "PATH": os.pathsep.join([*sorted(tools), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        "HOME": str(scratch / "home"),
        "LANG": "C.UTF-8",
        "ANTHROPIC_BASE_URL": mock.url,
        **PARENT_SESSION,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    mark = f"3{random.randrange(10**6):06d}"
    (given / "py").symlink_to(sys.executable)
    try:
        yield Host(given, root, bare_origin(scratch), mock, mark)
    finally:
        kill_servers(root)
        subprocess.run(["pkill", "-KILL", "-f", f"time.sleep[(]{mark}"], capture_output=True)
        shutil.rmtree(given, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


def kill_servers(root: Path) -> None:
    for socket in (root / tmux.SOCKETS_DIRNAME).glob("*"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)


def spec(marker: str, **overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(
            system=f"You are a test agent. SYS-{marker}",
            instructions=f"Reply with one word. TURN-{marker}",
        ),
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 50,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


async def at_prompt(
    adapter: ClaudeCliAdapter,
    host: Host,
    run_id: str,
    reason: IdleReason,
    *,
    ends: int = 0,
) -> None:
    """IDLE for `reason`, once the supervisor read `ends` turn ends from this segment's hooks."""

    async def there() -> bool:
        status = await adapter.status(run_id)
        why = state_of(host.root, run_id).failure_reason
        assert not status.is_terminal, f"run {run_id} ended {status}: {why}"
        if status is not RunStatus.IDLE:
            return False
        idle = (await adapter.session_state(run_id)).idle_reason is reason
        return idle and state_of(host.root, run_id).signals.hook_turn_ends >= ends

    try:
        await until(there, f"run {run_id} idle on {reason}", within=TURN_WAIT)
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{screen(host, run_id)}") from None


def screen(host: Host, run_id: str) -> str:
    socket = tmux.socket_path(host.root, run_id, "s", AssertionError)
    shown = subprocess.run(
        ["tmux", "-N", "-u", "-S", socket, "capture-pane", "-p", "-t", "=run:"],
        capture_output=True,
        text=True,
    )
    return shown.stdout.replace("\xa0", " ")


def hooks(host: Host, run_id: str) -> list[str]:
    path = run_dir(host.root, run_id) / session.SIGNALS_FILE
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    return [json.loads(line)["hook"].get("hook_event_name", "") for line in lines]


def translated(host: Host, run_id: str) -> list[dict[str, Any]]:
    lines = (run_dir(host.root, run_id) / LOG_FILE).read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def human_texts(host: Host, run_id: str) -> list[str]:
    return [
        " ".join(block["text"] for block in line["message"]["content"])
        for line in translated(host, run_id)
        if line["type"] == "user" and line.get("factorial_origin") == "human"
    ]


@asynccontextmanager
async def terminal(adapter: ClaudeCliAdapter, run_id: str) -> AsyncIterator[TerminalStream]:
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


async def say(adapter: ClaudeCliAdapter, host: Host, run_id: str, text: str) -> None:
    """A human types `text`, and presses Enter once all of it is drawn: compared without
    whitespace, since a long line wraps on the pane."""
    typed = unspaced(f"{INPUT_MARK} {text}")
    async with terminal(adapter, run_id) as tab:
        before = unspaced(screen(host, run_id)).count(typed)
        await tab.write(text.encode())

        async def drawn() -> bool:
            return unspaced(screen(host, run_id)).count(typed) > before

        await until(drawn, f"{text!r} on the input line")
        await tab.write(b"\r")


async def finish_watched(adapter: ClaudeCliAdapter, run_id: str) -> None:
    """`finish`, with only a read-only viewer attached: a `send-keys` would be refused."""
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


async def restarted(adapter: ClaudeCliAdapter) -> None:
    """A worker restart, in process: every supervisor stops where it stands, its lock let go."""
    tasks = list(adapter._supervisors.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def session_logs(host: Host, run_id: str) -> list[str]:
    logs = run_dir(host.root, run_id) / "config" / "projects" / "run"
    return sorted(path.name for path in logs.glob("*.jsonl"))


def reread(host: Host, run_id: str, session_id: str) -> Transcript:
    """The run's session log through a translator of its own, end to end: in the config home of
    the run whose workspace it is."""
    home = state_of(host.root, run_id).workspace_of or run_id
    config = run_dir(host.root, home) / "config"
    translator = Transcript(session_id, turn=True)
    for line in claude_session.session_log(config, session_id).read_text("utf-8").splitlines():
        translator.feed(line)
    translator.end_segment()
    return translator


def job(host: Host, n: int) -> str:
    """A job the agent backgrounds that ignores the hangup. Not an Apple binary such as
    `sleep`, whose environment macOS hides, so the adapter can find it there too; named by a
    short link, so the typed line stays one line whatever the checkout's path."""
    python = host.given / "py"
    return f"nohup {python} -c 'import time; time.sleep({host.mark}{n})' >/dev/null 2>&1 &"


def running(host: Host, n: int) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-f", f"time.sleep[(]{host.mark}{n}[)]"], capture_output=True, text=True
    )
    return [int(pid) for pid in found.stdout.split()]


async def left_behind(adapter: ClaudeCliAdapter, host: Host, run_id: str, n: int, ends: int) -> int:
    """The agent backgrounds job `n` through the Bash tool; its pid once it runs."""
    await say(adapter, host, run_id, f"BASHME {job(host, n)} EMHSAB")
    await at_prompt(adapter, host, run_id, IdleReason.TURN_END, ends=ends)
    await until(lambda: asyncio.to_thread(lambda: bool(running(host, n))), f"job {n}")
    [pid] = running(host, n)
    return pid


async def launched(adapter: ClaudeCliAdapter, host: Host, marker: str, **overrides: object) -> str:
    """Steps 1 and 2: a spawn that reaches its prompt with no dialog, on the files it was given."""
    assert await adapter.spawn_refusal() is None
    run_id = (await adapter.spawn(spec(marker, **overrides))).run_id
    await at_prompt(adapter, host, run_id, IdleReason.TURN_END, ends=1)
    assert not DIALOGS.search(screen(host, run_id)), screen(host, run_id)
    sid = state_of(host.root, run_id).session_id
    assert sid is not None
    first = next(r for r in host.mock.messages() if sid in json.dumps(r["body"].get("metadata")))
    assert f"TURN-{marker}" in json.dumps(first["body"]), "the @turn file was not inlined"
    assert f"SYS-{marker}" in json.dumps(first["body"]["system"])
    return run_id


async def finished(adapter: ClaudeCliAdapter, host: Host, run_id: str) -> None:
    """Step 7: the pasted `/exit` lands, so the CLI leaves by itself and writes its cost."""
    await finish_watched(adapter, run_id)
    result = await adapter.result(run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.SUCCEEDED, None)
    assert result.cost_usd is not None and result.cost_usd > 0
    assert result.stream_warnings == ()
    assert state_of(host.root, run_id).exit_code == 0, "the CLI left by the `/exit`"
    assert result.session_id is not None
    assert reread(host, run_id, result.session_id).missing() == ()


async def test_a_session_on_the_real_tui_from_spawn_to_rework_and_a_lost_host(host: Host) -> None:
    adapter = host.adapter()
    adapters = [adapter]
    try:
        # 1-2. The seeds trust the checkout's real path: no dialog, and the hooks fire.
        a = await launched(adapter, host, "A")
        requests = host.mock.requests()
        assert {r["x-api-key"] for r in requests} == {API_KEY}
        assert not any(r["authorization"] for r in requests)
        sid = state_of(host.root, a).session_id
        assert sid is not None

        # 3. A human's turn through the relay.
        await say(adapter, host, a, "Reply with one word. HUMAN-TURN")
        await at_prompt(adapter, host, a, IdleReason.TURN_END, ends=2)
        assert (await adapter.session_state(a)).last_input_at is not None

        async def mirrored() -> bool:
            return "Reply with one word. HUMAN-TURN" in human_texts(host, a)

        await until(mirrored, "the human's turn in the translated mirror")

        # 4. A question idles the session; the answer makes it active again.
        await say(adapter, host, a, "ASKME about the scope.")
        await at_prompt(adapter, host, a, IdleReason.QUESTION, ends=2)
        asked = await adapter.session_state(a)
        assert asked.question == "Include archived rows? [Active only | Active and archived]"

        async def menu() -> bool:
            return "Active and archived" in screen(host, a)

        await until(menu, "the question's menu")
        async with terminal(adapter, a) as tab:
            await tab.write(b"\r")
        await at_prompt(adapter, host, a, IdleReason.TURN_END, ends=3)
        events = hooks(host, a)
        assert events.index("PostToolUse") < len(events) - 1 - events[::-1].index("Stop")

        async def answer_mirrored() -> bool:
            return any("tool_use_result" in line for line in translated(host, a))

        await until(answer_mirrored, "the answer in the translated mirror")
        answered = [
            line["tool_use_result"] for line in translated(host, a) if "tool_use_result" in line
        ]
        assert answered == [{"answers": {"Include archived rows?": "Active only"}}]

        # The Bash tool runs each command in a session of its own, so a job the agent leaves is
        # outside the CLI's process group, which no hangup reaches.
        first_job = await left_behind(adapter, host, a, 1, ends=4)
        supervisor = adapter._attached(a)
        assert supervisor is not None and supervisor._pid is not None
        cli = supervisor._pid
        assert os.getsid(cli) == os.getpgid(cli) == cli, "the CLI leads the pane's session"
        assert os.getsid(first_job) != cli and os.getpgid(first_job) != cli

        # 5. A worker restart: a new adapter takes the session back, and repeats no line.
        await restarted(adapter)
        adapter = host.adapter()
        adapters.append(adapter)
        assert await adapter.status(a) is RunStatus.IDLE

        async def taken_back() -> bool:
            supervisor = adapter._attached(a)
            return supervisor is not None and supervisor._proven

        await until(taken_back, "the new supervisor to prove the server is the run's")
        assert await adapter.status(a) is RunStatus.IDLE
        lines = (run_dir(host.root, a) / LOG_FILE).read_text("utf-8").splitlines()
        assert len(lines) == len(set(lines))

        # 6. A rollover's interrupt, and its resume on the same session.
        await adapter.interrupt(a, ParkReason.ROLLOVER)
        assert await adapter.status(a) is RunStatus.INTERRUPTED
        assert running(host, 1) == [], "the interrupt left the agent's job running"
        secrets = {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)}
        await adapter.resume(a, None, secrets)
        await at_prompt(adapter, host, a, IdleReason.TURN_END)
        assert session_logs(host, a) == [f"{sid}.jsonl"]
        assert hooks(host, a)[-1] == "SessionStart"

        # The agent's branch, pushed; then a reviewer's commit on it.
        checkout = run_dir(host.root, a) / "worktree"
        git("checkout", "-q", "-b", BRANCH, cwd=checkout)
        (checkout / "spec.md").write_text("the spec\n")
        git("add", "-A", cwd=checkout)
        git("commit", "-q", "-m", "spec", cwd=checkout)
        git("push", "-q", "origin", BRANCH, cwd=checkout)
        review = host.origin.parent / "review"
        git("clone", "-q", "-b", BRANCH, str(host.origin), str(review), cwd=host.origin.parent)
        git("commit", "-q", "--allow-empty", "-m", "fix", cwd=review)
        git("push", "-q", "origin", BRANCH, cwd=review)
        head = git("rev-parse", "HEAD", cwd=review)

        # 7. The finish, with only a read-only viewer attached, ends the job left before it.
        await left_behind(adapter, host, a, 2, ends=1)
        await finished(adapter, host, a)
        assert running(host, 2) == [], "the finish left the agent's job running"
        parked = state_of(host.root, a)
        log_size = claude_session.session_log(checkout.parent / "config", sid).stat().st_size

        # 8. A rework continues the session in A's workspace, on a key minted for it.
        rework = spec(
            "B",
            resume_from=ResumeFrom(run_id=a, session_id=sid),
            secrets={ENV_API_KEY: SecretStr(REWORK_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
        )
        b = (await adapter.spawn(rework)).run_id
        state = await adapter.session_state(b)
        assert (state.session_id, state.log_offset) == (sid, log_size)
        assert log_size > 0
        assert not (run_dir(host.root, b) / "turns").exists()
        turn = (run_dir(host.root, a) / "turns" / f"{parked.segments}.md").read_text("utf-8")
        assert turn.startswith(REWORK_HEADER.format(branch=BRANCH, head=head)), turn[:300]
        await at_prompt(adapter, host, b, IdleReason.TURN_END, ends=1)
        assert not DIALOGS.search(screen(host, b)), screen(host, b)
        resumed = [r for r in host.mock.messages() if "Changes were requested" in json.dumps(r)]
        assert resumed and {r["x-api-key"] for r in resumed} == {REWORK_KEY}
        assert session_logs(host, a) == [f"{sid}.jsonl"]
        await finished(adapter, host, b)
        b_lines = translated(host, b)
        assert [line["type"] for line in b_lines].count("result") == 1
        a_ids = {
            line["message"]["id"] for line in translated(host, a) if line["type"] == "assistant"
        }
        assert not [line for line in b_lines if line.get("message", {}).get("id") in a_ids]

        # 9. The host goes mid-turn: the run fails, `host_lost`.
        c = await launched(adapter, host, "C")
        await say(adapter, host, c, "SLOWME")

        async def slow() -> bool:
            return any("SLOWME" in json.dumps(r["body"]) for r in host.mock.messages())

        await until(slow, "the slow request")
        await server_of(host.root, c).kill()
        await until(
            lambda: asyncio.to_thread(lambda: state_of(host.root, c).status.is_terminal),
            "the lost run's end",
        )
        result = await adapter.result(c)
        assert result.outcome is RunStatus.FAILED
        assert (result.failure_reason or "").startswith("host_lost"), result.failure_reason
    finally:
        kill_servers(host.root)
        for each in adapters:
            await each.drain()


async def test_a_subscription_session_launches_and_finishes_on_the_real_tui(host: Host) -> None:
    adapter = host.adapter(AUTH_MODE_SUBSCRIPTION)
    secrets = {ENV_OAUTH_TOKEN: SecretStr(OAUTH), ENV_GH_TOKEN: SecretStr(TOKEN)}
    try:
        run_id = await launched(adapter, host, "S", secrets=secrets)
        requests = host.mock.requests()
        assert requests and all(r["path"].startswith("/v1/messages") for r in requests)
        assert {r["authorization"] for r in requests} == {f"Bearer {OAUTH}"}
        assert not any(r["x-api-key"] for r in requests)
        await finished(adapter, host, run_id)
    finally:
        kill_servers(host.root)
        await adapter.drain()


async def test_a_session_writes_nothing_into_the_operators_home(host: Host) -> None:
    """claude writes into the HOME it runs with (its URL handler app, `gh`'s cache), so a
    session's HOME must be its own, not the worker's. The agent's git still commits, as the
    agent. No credential command runs here: the agent's git reads the host's own config files,
    whose helper may be a real keychain (`test_hostgit.py` proves the token's helper alone)."""
    home = Path(os.environ["HOME"])
    adapter = host.adapter()
    try:
        run_id = await launched(adapter, host, "H")
        await say(adapter, host, run_id, "BASHME git commit -q --allow-empty -m agent EMHSAB")
        await at_prompt(adapter, host, run_id, IdleReason.TURN_END, ends=2)
        await finished(adapter, host, run_id)
        left = await asyncio.to_thread(lambda: sorted(map(str, home.rglob("*"))))
        assert not left, f"the session wrote into the operator's HOME: {left}"
        checkout = run_dir(host.root, run_id) / "worktree"
        author = "factorial-agent <factorial-agent@users.noreply.github.com>"
        assert git("log", "-1", "--format=%s by %an <%ae>", cwd=checkout) == f"agent by {author}"
    finally:
        kill_servers(host.root)
        await adapter.drain()


async def test_a_session_reads_no_claude_md_from_above_its_worktree(host: Host) -> None:
    """claude reads the CLAUDE.md files of every ancestor of its cwd, and on a host those
    include `local_root`, which every run shares and any agent can write. The repo's own still
    reach the model, a nested one too once the agent reads a file beside it."""
    marker = f"MD{host.mark}"
    seed = host.origin.parent / "md-seed"
    git("clone", "-q", str(host.origin), str(seed), cwd=host.origin.parent)
    (seed / "sub").mkdir()
    for path, tag in {"CLAUDE.md": "REPO", "sub/CLAUDE.md": "NESTED", "sub/x.txt": "X"}.items():
        (seed / path).write_text(f"{marker}-{tag}\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "memory", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", cwd=seed)
    above = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/r.md")
    for folder in (host.root, host.root / "runs"):
        for name in above:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(f"{marker}-ABOVE\n")
    adapter = host.adapter()
    try:
        run_id = await launched(adapter, host, "M")
        nested = run_dir(host.root, run_id) / "worktree" / "sub" / "x.txt"
        read = json.dumps({"file_path": str(nested)})
        await say(adapter, host, run_id, f"TOOLME Read {read} EMLOOT")
        await at_prompt(adapter, host, run_id, IdleReason.TURN_END, ends=2)
        reached = set(re.findall(rf"{marker}-[A-Z]+", host.mock.log.read_text("utf-8")))
        assert f"{marker}-ABOVE" not in reached, "a CLAUDE.md above the worktree reached the model"
        assert reached == {f"{marker}-REPO", f"{marker}-NESTED", f"{marker}-X"}
        await adapter.finish(run_id)
    finally:
        kill_servers(host.root)
        await adapter.drain()
