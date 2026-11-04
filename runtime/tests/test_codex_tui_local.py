"""L1: the real codex TUI in a host tmux, launched from `codex_cli/tui.py`'s documents against
`mock_openai.py` (decision 56). It proves the codex facts slice 6 builds on, and with
`FACTORIAL_TUI_CODEX_CAPTURE=1` it writes fixtures under `fixtures/codex-<PINNED_VERSION>/`; the
tracked ones are 0.159.0's.

Opt-in, and never in CI: it needs the pinned native binary and tmux. It takes under six
minutes: two of them codex's own wait before it answers its question box alone, most of the
rest its WebSocket retries before each launch's first turn falls back to HTTPS. `-s` shows what
the steps that record rather than assert observed (`FINDING` lines).

    FACTORIAL_TUI_CODEX=<a native codex-cli 0.164.0> \\
      uv run pytest -c pyproject.toml runtime/tests/test_codex_tui_local.py -q -p no:cacheprovider

One thread runs through the module's tests in file order; a failure leaves the later ones
meaningless. Every file lives under one `mkdtemp(dir="/var/tmp")` addressed through a symlink
(macOS's `/var`, or on Linux one beside it), so the hooks' trust, keyed on CODEX_HOME's real
path, is proven. HOME and CODEX_HOME are scratch, the key is fake, and the CLI runs under
`env -i` with only the variables named here. It runs on the host, or inside the image under its
seccomp profile (docs/local-container.md, "codex_cli in the container").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fcntl import ioctl
from pathlib import Path
from typing import Any

import pytest
from codex_tui_support import DIALOGS, PINNED_CODEX, Mock, mock_api, wrapper

from runtime.adapters._cli import tmux
from runtime.adapters._cli.environment import worktree_env
from runtime.adapters._cli.host_session import GRACE
from runtime.adapters._cli.launch import (
    ENV_SIGNALS,
    SIGNAL_SH,
    launch_argv,
    write_env_file,
    write_launch_files,
)
from runtime.adapters._cli.session_signals import SessionSignals
from runtime.adapters.codex_cli.auth import AUTH_MODE_API_KEY
from runtime.adapters.codex_cli.cli import (
    INPUT_MARK,
    PINNED_VERSION,
    QUIT_COMMAND,
    VALUE_SANDBOX_WORKSPACE,
)
from runtime.adapters.codex_cli.tui import (
    auth_document,
    pointer,
    resume_argv,
    signal_command,
    spawn_argv,
    thread_of,
    tui_config_toml,
    tui_flags,
    turn_text,
)
from runtime.contract import AgentPrompts, IdleReason, RunSpec, Workspace

CAPTURE = os.environ.get("FACTORIAL_TUI_CODEX_CAPTURE") == "1"
FIXTURES = Path(__file__).parent / "fixtures" / f"codex-{PINNED_VERSION}"
MODEL = "gpt-5.3-codex"
FAKE_KEY = "sk-fake-l1-codex"
TURN_WAIT = 90.0
#: §0.6: spawn waits this long for the rollout that names the thread.
SESSION_ID_WAIT = 30.0
#: Long enough for a hook that is going to fire to have fired.
SETTLE = 3.0
#: The consent screen's choice for a folder seeded `untrusted`.
RESTRICTED = "Open restricted"
#: The system prompt's bulk: no character JSON escapes, so a fixture can swap it for a token.
FILLER = "filler " * 30_000


pytestmark = PINNED_CODEX


def finding(name: str, value: Any) -> None:
    print(f"FINDING {name}: {json.dumps(value, default=str)}", file=sys.stderr)


def _png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    side = 64
    raw = b"".join(b"\x00" + bytes(rgb) * side for _ in range(side))
    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Watcher:
    """A read-only tmux client on a pty, as a browser's watcher is: with it the only client,
    tmux refuses `send-keys`, so only a paste reaches the TUI."""

    def __init__(self, socket: str) -> None:
        master, slave = pty.openpty()
        ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        self.master = master
        self.proc = subprocess.Popen(
            [
                shutil.which("tmux") or "tmux",
                "-N",
                "-u",
                "-S",
                socket,
                "attach-session",
                "-t",
                "=run",
                "-r",
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "TERM": "xterm-256color"},
        )
        os.close(slave)
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.drain.start()

    def _drain(self) -> None:
        with contextlib.suppress(OSError):
            while os.read(self.master, 65536):
                pass

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()
        with contextlib.suppress(OSError):
            os.close(self.master)


@dataclass
class Codex:
    """The scratch run: its files, its tmux server, the thread, and what each step saw."""

    root: Path
    mock: Mock
    signals: SessionSignals = field(default_factory=SessionSignals)
    thread: str = ""
    rollout: Path | None = None
    signals_read: int = 0
    #: Named (rollout lines, signals lines) at each step's end, for the fixtures' index.
    checkpoints: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: Each launch, and when it started.
    launches: list[tuple[str, float]] = field(default_factory=list)
    #: Where the question-box launches begin, and their steps' ends, for the `ask` fixtures.
    ask_start: tuple[int, int] | None = None
    ask_checkpoints: dict[str, tuple[int, int]] = field(default_factory=dict)
    watcher: Watcher | None = None

    def __post_init__(self) -> None:
        self.real = Path(os.path.realpath(self.root))
        socket = tmux.socket_path(self.root, "l1-codex", "s", RuntimeError)
        self.server = tmux.TmuxServer(
            binary=shutil.which("tmux") or "tmux",
            socket=socket,
            conf=self.root / tmux.SESSION_CONF_FILE,
            home=self.root / "home",
            session="run",
        )

    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def home(self) -> Path:
        return self.path("codex")

    @property
    def worktree(self) -> Path:
        return self.path("wt")

    @property
    def wrapper(self) -> str:
        return str(self.path("bin/codex"))

    def setup(self) -> None:
        for name in ("home", "codex", "wt", "run", "run/turns", "run/attachments", "marks"):
            self.path(name).mkdir()
        tmux.prepare(self.root)
        git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", "--bare", str(self.path("git"))], env=git_env)
        self.path("run/signals.jsonl").touch(mode=0o600)
        skill = self.home / "skills" / SKILL
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {SKILL}\ndescription: An L1 skill that does nothing.\n---\nNothing.\n",
            "utf-8",
        )
        write_repo_config(self.worktree, self.path("marks"))

    def seed(self, developer_instructions: str | None, *, ask_tool: bool = False) -> None:
        document, _ = tui_config_toml(
            (),
            {},
            writable_root=self.real,
            workspace_real=os.path.realpath(self.worktree),
            codex_home_real=os.path.realpath(self.home),
            signal_command=signal_command(
                str(self.path("run/signals.jsonl")), str(self.path("bin/signal.sh"))
            ),
            developer_instructions=developer_instructions,
            ask_tool=ask_tool,
        )
        (self.home / "config.toml").write_text(document, "utf-8")
        auth = self.home / "auth.json"
        auth.write_text(auth_document(AUTH_MODE_API_KEY, FAKE_KEY), "utf-8")
        auth.chmod(0o600)

    def env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.path("home")),
            "USER": "agent",
            "LANG": "C.UTF-8",
            "SHELL": "/bin/bash",
            **worktree_env(self.worktree, self.path("git")),
            "CODEX_HOME": str(self.home),
            "TERM": "xterm-256color",
        }

    def flags(self) -> tuple[str, ...]:
        return tui_flags(MODEL, worktree=str(self.worktree), sandbox_mode=VALUE_SANDBOX_WORKSPACE)

    def sync(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def launch(self, name: str, argv: tuple[str, ...], *, turn: bool) -> None:
        """A segment, as the adapter starts one: `launch.sh` reads the environment from a file
        it deletes, and execs the CLI as the pane's process. The binary is the wrapper that
        points codex at the mock."""
        self.sync(self.server.kill())
        write_launch_files(self.root, {"signal.sh": SIGNAL_SH, "codex": wrapper(self.mock.url)})
        env_file = self.path("run/launch.env")
        env_file.unlink(missing_ok=True)
        write_env_file(env_file, self.env())
        self.feed()
        self.signals.start_segment(turn=turn)
        self.launches.append((name, time.time()))
        started = self.sync(
            self.server.start(self.worktree, launch_argv(self.root, env_file, argv))
        )
        assert started.ok, started.stderr
        self.open_restricted()

    def open_restricted(self) -> None:
        """Every launch in a folder not seeded `trusted` asks for consent first. For an
        `untrusted` one it offers "Open restricted", already selected, which keeps the trust as
        it is; Enter takes it."""
        self.wait("the folder consent", lambda: RESTRICTED in self.pane(), timeout=20)
        time.sleep(0.3)
        self.key("\r")
        self.wait("the consent to close", lambda: RESTRICTED not in self.pane(), timeout=10)

    def pane(self) -> str:
        return self.sync(self.server.capture()) or ""

    def wait(self, what: str, done: Callable[[], bool], timeout: float = TURN_WAIT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done():
                return
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}\npane:\n{self.pane()}")

    def paste(self, text: str) -> None:
        assert self.sync(self.server.type_text(text, bracketed=True))

    def key(self, key: str) -> None:
        assert self.sync(self.server.type_text(key, key=True))

    def send(self, text: str) -> None:
        """Type a turn: its text as one paste, then Enter as a paste of its own after a gap,
        since codex takes an Enter inside a paste burst for a newline."""
        self.paste(text)
        self.wait(f"{text!r} in the input line", lambda: f"{INPUT_MARK} {text}" in self.pane())
        time.sleep(0.5)
        self.key("\r")

    def quit(self) -> tuple[int | None, str]:
        """`finish`: Ctrl-C, which quits an idle TUI outright and only empties a draft; then,
        if it still runs, the pasted `/quit` and Enter alone. The pane's exit status, and which
        of the two ended it."""
        self.key("\x03")
        with contextlib.suppress(AssertionError):
            self.wait("Ctrl-C to quit", lambda: self.dead() is not None, timeout=3)
            return self.dead(), "ctrl-c"
        self.paste(QUIT_COMMAND)
        time.sleep(0.5)
        lines = [line.strip() for line in self.pane().splitlines()]
        assert f"{INPUT_MARK} {QUIT_COMMAND}" in lines, self.pane()
        self.key("\r")
        self.wait("the TUI to exit", lambda: self.dead() is not None, timeout=10)
        return self.dead(), QUIT_COMMAND

    def dead(self) -> int | None:
        pane = self.sync(self.server.pane())
        return pane.status if pane is not None and pane.dead else None

    def pane_pid(self) -> int:
        pane = self.sync(self.server.pane())
        assert pane is not None
        return pane.pid

    def hooks(self, event: str | None = None) -> list[dict[str, Any]]:
        lines = self.path("run/signals.jsonl").read_text("utf-8").splitlines()
        hooks = [json.loads(line)["hook"] for line in lines]
        return [h for h in hooks if event is None or h.get("hook_event_name") == event]

    def wait_hooks(self, event: str, count: int) -> None:
        self.wait(f"{count} {event} hook(s)", lambda: len(self.hooks(event)) >= count)

    def rollouts(self) -> list[Path]:
        return sorted(self.home.glob("sessions/*/*/*/rollout-*"))

    def lines(self) -> list[dict[str, Any]]:
        assert self.rollout is not None
        return [json.loads(line) for line in self.rollout.read_text("utf-8").splitlines()]

    def events(self, kind: str, since: int = 0) -> list[dict[str, Any]]:
        return [
            line["payload"]
            for line in self.lines()[since:]
            if line["type"] == "event_msg" and line["payload"].get("type") == kind
        ]

    def items(self, kind: str, since: int = 0) -> list[dict[str, Any]]:
        return [
            e["item"] for e in self.events("item_completed", since) if e["item"]["type"] == kind
        ]

    def feed(self) -> None:
        """Signals lines to `SessionSignals`, as the supervisor feeds them, for this thread."""
        raw = self.path("run/signals.jsonl").read_bytes()[self.signals_read :]
        complete = raw[: raw.rfind(b"\n") + 1]
        self.signals_read += len(complete)
        for line in complete.decode().splitlines():
            self.signals.feed(line, observed_at=datetime.now(UTC), session_id=self.thread)

    def position(self) -> tuple[int, int]:
        signals = len(self.path("run/signals.jsonl").read_text("utf-8").splitlines())
        return len(self.lines()), signals

    def checkpoint(self, name: str) -> None:
        self.checkpoints[name] = self.position()

    def responses(self, kind: str, since: int = 0) -> list[dict[str, Any]]:
        return [
            line["payload"]
            for line in self.lines()[since:]
            if line["type"] == "response_item" and line["payload"].get("type") == kind
        ]

    def turn_file(self, n: int, spec: RunSpec) -> Path:
        path = self.path(f"run/turns/{n}.md")
        path.write_text(turn_text(spec, {}), "utf-8")
        return path

    def close(self) -> None:
        if self.watcher is not None:
            self.watcher.close()
        with contextlib.suppress(Exception):
            self.sync(self.server.kill())


SKILL = f"l1-skill-{uuid.uuid4().hex[:8]}"
SYSTEM_MARKER = f"SYS-{uuid.uuid4().hex[:8]}"
#: Large, and the marker last, so a truncated prompt is caught.
SYSTEM_PROMPT = f"You are a test agent.\n{FILLER}\n{SYSTEM_MARKER}"
REPO_MARK = f"REPO-{uuid.uuid4().hex[:8]}"


def write_repo_config(worktree: Path, marks: Path) -> None:
    """What a target repo may ship in `.codex/`: a hook, an MCP server, a sandbox override and
    developer instructions. Each leaves a mark if codex takes it up: a file, or `REPO_MARK` in a
    request."""
    folder = worktree / ".codex"
    folder.mkdir()
    hook = f"touch {marks / 'repo-hook'}"
    (folder / "config.toml").write_text(
        f'sandbox_mode = "danger-full-access"\ndeveloper_instructions = "{REPO_MARK}"\n\n'
        f'[mcp_servers.x]\ncommand = "sh"\nargs = ["-c", "touch {marks / "repo-mcp"}"]\n\n'
        f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "{hook}"\n',
        "utf-8",
    )
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{hook}-json"}]}]}}
    (folder / "hooks.json").write_text(json.dumps(hooks), "utf-8")
    (worktree / "README.md").write_text("An L1 checkout.\n", "utf-8")


def _spec() -> RunSpec:
    return RunSpec(
        agent="plan-agent",
        ticket="GH-243",
        workspace=Workspace(repo="acme/widgets", base="main", branch="spec/243-{slug}"),
        prompts=AgentPrompts(system=SYSTEM_PROMPT, instructions="Reply with one word."),
        model=MODEL,
    )


@pytest.fixture(scope="module")
def codex(request: pytest.FixtureRequest) -> Iterator[Codex]:
    base = root = Path(tempfile.mkdtemp(prefix="f243c-", dir="/var/tmp"))
    if os.path.realpath(base) == str(base):
        # Only where no symlink leads there already, as on Linux: on macOS codex refuses a
        # writable root holding a symlink of the test's own, past the system's `/var`.
        root = base.with_name(base.name + "-l")
        root.symlink_to(base)
    assert os.path.realpath(root) != str(root), "the scratch root must be reached by a symlink"
    with mock_api() as mock:
        session = Codex(root=root, mock=mock)
        try:
            session.setup()
            failed = request.session.testsfailed
            yield session
            # A thread cut short by a failure must not replace the tracked fixtures.
            if CAPTURE and session.rollout is not None and request.session.testsfailed == failed:
                _capture(session)
        finally:
            session.close()
            # `base` is a substring of the link's path and of the real one, so it matches a
            # process named by either.
            subprocess.run(["pkill", "-KILL", "-f", str(base)], capture_output=True)
            if root.is_symlink():
                root.unlink()
            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- the spawn


def test_the_seeded_tui_reaches_its_prompt_with_no_dialog_and_the_stop_hook_fires(
    codex: Codex,
) -> None:
    image = codex.path("run/attachments/swatch.png")
    image.write_bytes(_png((20, 20, 230)))
    turn = codex.turn_file(0, _spec())
    codex.seed(SYSTEM_PROMPT)
    codex.launch("spawn", spawn_argv(codex.flags(), [str(image)], pointer(str(turn)),
                                     binary=codex.wrapper), turn=True)  # fmt: skip
    codex.wait("the rollout", lambda: bool(codex.rollouts()), timeout=SESSION_ID_WAIT)
    finding("Q4: seconds from the launch to the rollout", time.time() - codex.launches[-1][1])
    (codex.rollout,) = codex.rollouts()
    thread = thread_of(codex.rollout)
    assert thread is not None
    codex.thread = thread
    first = codex.lines()[0]
    assert first["type"] == "session_meta" and first["payload"]["id"] == thread

    codex.wait_hooks("Stop", 1)
    assert not DIALOGS.search(codex.pane()), codex.pane()
    (start,) = codex.hooks("SessionStart")
    assert start["source"] == "startup" and start["session_id"] == thread
    codex.feed()
    assert codex.signals.idle_reason is IdleReason.TURN_END
    assert codex.signals.summary == "PONG from mock."
    assert codex.signals.last_input_at is None


def test_a_repo_codex_config_neither_blocks_startup_nor_takes_effect(codex: Codex) -> None:
    time.sleep(SETTLE)
    assert not list(codex.path("marks").iterdir()), "a repo hook or MCP server ran"
    # The repo's instructions are a key no flag overrides, unlike its `sandbox_mode`.
    assert not [r for r in codex.mock.requests() if REPO_MARK in json.dumps(r["body"])]
    (context,) = [line["payload"] for line in codex.lines() if line["type"] == "turn_context"]
    finding(
        "turn_context policy, as -s and -a set it",
        {k: context.get(k) for k in ("approval_policy", "sandbox_policy")},
    )
    assert '"trusted"' not in (codex.home / "config.toml").read_text("utf-8")


def test_the_developer_instructions_and_skills_reach_the_model(codex: Codex) -> None:
    first = codex.mock.main()[0]["body"]
    where = [
        key for key in ("instructions", "input") if SYSTEM_MARKER in json.dumps(first.get(key))
    ]
    finding("where developer_instructions ride", where)
    assert where, "the system prompt did not reach the model"
    assert FILLER in json.dumps(first), "the large system prompt arrived cut"
    assert SKILL in json.dumps(first)


def test_a_hook_command_carries_its_signals_path_without_the_env(codex: Codex) -> None:
    assert ENV_SIGNALS not in codex.env()
    assert {h["hook_event_name"] for h in codex.hooks()} >= {"SessionStart", "UserPromptSubmit"}


def test_an_image_flag_reaches_the_first_turn_as_input_image(codex: Codex) -> None:
    first = codex.mock.main()[0]["body"]
    users = [i for i in first["input"] if i.get("role") == "user"]
    assert any(c.get("type") == "input_image" for i in users for c in i["content"])
    pointed = codex.items("UserMessage")[0]
    finding("the first UserMessage item", pointed)
    turn = pointer(str(codex.path("run/turns/0.md")))
    assert [c["text"] for c in pointed["content"] if c["type"] == "text"] == [turn]
    assert [c["type"] for c in pointed["content"]] == ["local_image", "text"]
    codex.checkpoint("base")


def test_a_typed_turn_is_a_humans_prompt(codex: Codex) -> None:
    codex.send("Reply with one word. HUMAN-TURN")
    codex.wait_hooks("Stop", 2)
    prompt = codex.hooks("UserPromptSubmit")[-1]
    assert prompt["prompt"] == "Reply with one word. HUMAN-TURN"
    codex.feed()
    assert codex.signals.idle_reason is IdleReason.TURN_END
    assert codex.signals.last_input_at is not None
    codex.wait("the turn's end", lambda: len(codex.events("task_complete")) == 2)
    codex.checkpoint("human")


def test_a_command_execution_is_recorded_as_an_item_completed(codex: Codex) -> None:
    mark = f"hi-{uuid.uuid4().hex[:6]}"
    codex.send(f"RUNME echo {mark}")
    codex.wait_hooks("Stop", 3)
    codex.wait("the turn's end", lambda: len(codex.events("task_complete")) == 3)
    (command,) = codex.items("CommandExecution")
    finding("CommandExecution item", command)
    assert mark in json.dumps(command)
    codex.checkpoint("command")


def test_a_failed_turn_fires_no_stop_and_the_rollout_says_why(codex: Codex) -> None:
    since = len(codex.lines())
    codex.send("FAILME now")
    codex.wait(
        "the failed turn's end", lambda: bool(codex.events("task_complete", since)), timeout=120
    )
    (complete,) = codex.events("task_complete", since)
    finding("failed task_complete", complete)
    assert complete["error"]["message"]
    time.sleep(SETTLE)
    assert len(codex.hooks("Stop")) == 3, "a failed turn fired Stop"
    assert codex.hooks("UserPromptSubmit")[-1]["prompt"] == "FAILME now"
    codex.checkpoint("fail")


def test_an_escaped_turn_writes_turn_aborted_and_no_stop(codex: Codex) -> None:
    since = len(codex.lines())
    codex.send("SLOWME now")
    codex.wait("the slow request", lambda: bool(codex.mock.carrying("SLOWME now")))
    time.sleep(1)
    codex.sync(codex.server.run("send-keys", "-t", "=run:", "Escape"))
    codex.wait("turn_aborted", lambda: bool(codex.events("turn_aborted", since)))
    finding("turn_aborted", codex.events("turn_aborted", since))
    assert not codex.events("task_complete", since)
    time.sleep(SETTLE)
    assert len(codex.hooks("Stop")) == 3, "an aborted turn fired Stop"
    codex.checkpoint("abort")


def test_the_agent_sandbox_can_rewrite_codex_home(codex: Codex) -> None:
    """R6: `writable_roots` holds the run area, CODEX_HOME included. Every launch rewrites the
    file, which is what undoes the edit."""
    config = codex.home / "config.toml"
    before = config.read_text("utf-8")
    codex.send(f"RUNME printf '# l1-agent-edit' >> {config}")
    codex.wait_hooks("Stop", 4)
    codex.wait("the command", lambda: len(codex.items("CommandExecution")) == 2)
    command = codex.items("CommandExecution")[-1]
    assert command["exit_code"] == 0, command
    assert config.read_text("utf-8") != before
    codex.checkpoint("agent_edit")


def test_a_pasted_quit_with_only_a_watcher_attached_exits_zero_and_fires_no_prompt_hook(
    codex: Codex,
) -> None:
    codex.watcher = Watcher(codex.server.socket)
    codex.wait(
        "the watcher", lambda: codex.sync(codex.server.run("list-clients")).stdout.strip() != ""
    )
    refused = codex.sync(codex.server.run("send-keys", "-t", "=run:", "x"))
    finding("send-keys with only a read-only client", [refused.returncode, refused.stderr])
    # A draft the human left, so Ctrl-C empties it rather than quitting.
    codex.paste("a draft left in the input line")
    codex.wait("the draft", lambda: "a draft left" in codex.pane())
    assert codex.quit() == (0, QUIT_COMMAND)
    codex.watcher.close()
    codex.watcher = None
    # A read of `signals.jsonl` can miss lines a later read shows, though `signal.sh` only appends.
    codex.wait("the Stop hooks", lambda: bool(codex.hooks("Stop")))
    stops = [i for i, h in enumerate(codex.hooks()) if h["hook_event_name"] == "Stop"]
    after = codex.hooks()[stops[-1] + 1 :]
    assert "UserPromptSubmit" not in {h["hook_event_name"] for h in after}, after
    assert not list(codex.path("marks").iterdir()), "a repo hook or MCP server ran"
    codex.checkpoint("quit")


# ---------------------------------------------------------------- the resumes


def test_resume_appends_to_the_same_rollout_and_a_turnless_resume_waits_at_its_prompt(
    codex: Codex,
) -> None:
    assert codex.rollout is not None
    original = codex.rollout.read_bytes()
    since = len(codex.lines())
    codex.seed(None)
    codex.launch("resume", resume_argv(codex.flags(), codex.thread, (), None,
                                       binary=codex.wrapper), turn=False)  # fmt: skip
    codex.wait("the resumed prompt", lambda: INPUT_MARK in codex.pane())
    time.sleep(SETTLE)
    # codex runs SessionStart hooks as a turn starts, so a turnless resume fires none.
    assert len(codex.hooks("SessionStart")) == 1
    codex.feed()
    assert codex.signals.idle_reason is None
    assert codex.rollouts() == [codex.rollout]
    assert codex.rollout.read_bytes()[: len(original)] == original
    assert not codex.events("task_started", since), "a turnless resume started a turn"
    assert not DIALOGS.search(codex.pane()), codex.pane()
    finding("lines a turnless resume appended", [ln["type"] for ln in codex.lines()[since:]])


def test_a_resumed_session_keeps_its_developer_instructions(codex: Codex) -> None:
    assert "developer_instructions" not in (codex.home / "config.toml").read_text("utf-8")
    codex.send("Reply with one word. AFTER-RESUME")
    codex.wait_hooks("Stop", 5)
    start = codex.hooks("SessionStart")[-1]
    assert start["source"] == "resume" and start["session_id"] == codex.thread
    (request,) = codex.mock.carrying("AFTER-RESUME")
    body = request["body"]
    finding(
        "Q6: after a resume without them, the developer instructions ride in",
        # 0.164.0 sends no top-level `instructions`.
        [k for k in ("instructions", "input") if SYSTEM_MARKER in json.dumps(body.get(k))],
    )
    assert SYSTEM_MARKER in json.dumps(body)
    assert codex.quit() == (0, "ctrl-c")
    codex.checkpoint("resume")


def test_a_resume_with_an_image_keeps_the_thread_and_the_pointer(codex: Codex) -> None:
    image = codex.path("run/attachments/second.png")
    image.write_bytes(_png((230, 20, 20)))
    turn = codex.turn_file(1, _spec())
    prompt = pointer(str(turn))
    codex.seed(None)
    codex.launch("resume-turn", resume_argv(codex.flags(), codex.thread, [str(image)], prompt,
                                            binary=codex.wrapper), turn=True)  # fmt: skip
    codex.wait_hooks("Stop", 6)
    (request,) = codex.mock.carrying("turns/1.md")
    users = [i for i in request["body"]["input"] if i.get("role") == "user"]
    assert any(c.get("type") == "input_image" for c in users[-1]["content"]), users[-1]
    pointed = codex.items("UserMessage")[-1]
    assert [c["text"] for c in pointed["content"] if c["type"] == "text"] == [prompt]
    assert codex.rollouts() == [codex.rollout]
    assert codex.quit() == (0, "ctrl-c")
    codex.checkpoint("resume_image")


def test_a_rollout_older_than_a_week_is_compressed_then_resumed_byte_identical(
    codex: Codex,
) -> None:
    rollout = codex.rollout
    assert rollout is not None
    original = rollout.read_bytes()
    old = time.time() - 8 * 86400
    os.utime(rollout, (old, old))
    lock = codex.home / ".tmp" / "rollout-compression.lock"
    finding("a compression run marker before the throwaway launches", lock.exists())
    zst = rollout.with_name(rollout.name + ".zst")

    def throwaway(name: str, *extra: str) -> None:
        codex.launch(name, (codex.wrapper, *extra, *codex.flags()), turn=False)
        codex.wait("the throwaway's prompt", lambda: INPUT_MARK in codex.pane())
        time.sleep(SETTLE)

    # The worker that compresses is an under-development feature, off by default.
    throwaway("throwaway")
    assert not zst.exists()
    assert codex.quit() == (0, "ctrl-c")
    throwaway("throwaway-compress", "-c", "features.local_thread_store_compression=true")
    codex.wait("the compressed rollout", lambda: zst.exists() and not rollout.exists())
    assert codex.quit() == (0, "ctrl-c")

    sessions = len(codex.hooks("SessionStart"))
    # As a rework's adoption writes them: a thread that already holds them gets them again.
    codex.seed(SYSTEM_PROMPT)
    codex.launch("resume-compressed", resume_argv(codex.flags(), codex.thread, (), None,
                                                  binary=codex.wrapper), turn=False)  # fmt: skip
    codex.wait("the resumed prompt", lambda: INPUT_MARK in codex.pane())
    codex.send("Reply with one word. AFTER-COMPRESSION")
    codex.wait_hooks("SessionStart", sessions + 1)
    codex.wait_hooks("Stop", 7)
    assert rollout.exists()
    finding("the .zst after a resume", zst.exists())
    assert rollout.read_bytes()[: len(original)] == original
    for marker in ("AFTER-RESUME", "AFTER-COMPRESSION"):
        request = codex.mock.carrying(marker)[0]
        developer = [
            i
            for i in request["body"]["input"]
            if i.get("role") == "developer" and SYSTEM_MARKER in json.dumps(i)
        ]
        assert len(developer) == 1, f"{len(developer)} copies of the system prompt at {marker}"
    codex.checkpoint("resume_compressed")


def test_what_a_hangup_does_to_a_command_the_agent_left_running(codex: Codex) -> None:
    """R3: on macOS codex starts each command in a session of its own and does not end it on
    SIGHUP, so the adapter's hangup must sweep codex's descendants itself. On Linux each
    sandboxed command runs in bubblewrap's PID namespace, which dies with codex, so the command
    goes with it."""
    mark = f"7{uuid.uuid4().int % 10**6:06d}"
    try:
        codex.send(f"RUNME sleep {mark}")
        codex.wait_hooks("Stop", 8)
        alive = subprocess.run(["pgrep", "-f", f"sleep {mark}"], capture_output=True, text=True)
        assert alive.stdout.strip(), "the agent's command was not left running"
        pid = codex.pane_pid()
        codex.sync(codex.server.kill())
        time.sleep(GRACE)
        left = subprocess.run(["pgrep", "-f", f"sleep {mark}"], capture_output=True, text=True)
        assert subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0
        if sys.platform == "linux":
            assert not left.stdout.strip(), "a command outlived its PID namespace"
        else:
            assert left.stdout.strip(), "codex now ends its commands on a hangup"
    finally:
        # Not under the scratch root, so the fixture's own sweep would miss it.
        subprocess.run(["pkill", "-KILL", "-f", f"sleep {mark}"], capture_output=True)
    codex.checkpoint("hangup")


# ---------------------------------------------------------------- the egress


def test_the_websocket_attempts_and_each_launchs_first_token(codex: Codex) -> None:
    """Informational (§0.9): the TUI tries a WebSocket first, then falls back to HTTPS."""
    requests = codex.mock.requests()
    upgrades = [r for r in requests if r["upgrade"] == "websocket"]
    finding("WebSocket attempts", len(upgrades))
    finding("HTTPS POSTs", sum(r["method"] == "POST" for r in requests))
    firsts = []
    turn_starts = {e["turn_id"] for e in codex.events("task_started")}
    for complete in codex.events("task_complete"):
        if complete["turn_id"] in turn_starts:
            firsts.append(complete.get("time_to_first_token_ms"))
    finding("time_to_first_token_ms per turn", firsts)
    assert upgrades


# ---------------------------------------------------------------- the question box


def _asked(codex: Codex, since: int) -> dict[str, Any]:
    """Type the turn whose reply calls `request_user_input`, and wait for the call."""
    codex.send("ASKME which database")

    def calls() -> list[dict[str, Any]]:
        made = codex.responses("function_call", since)
        return [call for call in made if call["name"] == "request_user_input"]

    codex.wait("the question call", lambda: bool(calls()))
    (call,) = calls()
    return call


def _output(codex: Codex, call: dict[str, Any], since: int) -> list[dict[str, Any]]:
    outputs = codex.responses("function_call_output", since)
    return [o for o in outputs if o["call_id"] == call["call_id"]]


def test_codex_refuses_its_question_box_by_default_and_answers_it_alone_when_on(
    codex: Codex,
) -> None:
    """Off, Default mode refuses `request_user_input`, which the turn's Stop must still idle
    on. On, the picker waits for a person, then codex answers it with nothing: the worker must
    park before that, so the wait is measured."""
    codex.ask_start = codex.position()
    stops = len(codex.hooks("Stop"))
    since = len(codex.lines())
    codex.seed(None)
    codex.launch("ask-refused", resume_argv(codex.flags(), codex.thread, (), None,
                                            binary=codex.wrapper), turn=False)  # fmt: skip
    codex.wait("the resumed prompt", lambda: INPUT_MARK in codex.pane())
    call = _asked(codex, since)
    codex.wait("the refusal", lambda: bool(_output(codex, call, since)))
    finding("the refused call's output", _output(codex, call, since))
    codex.wait_hooks("Stop", stops + 1)
    assert codex.quit() == (0, "ctrl-c")
    codex.ask_checkpoints["refused"] = codex.position()

    since = len(codex.lines())
    codex.seed(None, ask_tool=True)
    codex.launch("ask-on", resume_argv(codex.flags(), codex.thread, (), None,
                                       binary=codex.wrapper), turn=False)  # fmt: skip
    codex.wait("the resumed prompt", lambda: INPUT_MARK in codex.pane())
    call = _asked(codex, since)
    asked_at = time.monotonic()
    codex.wait("the picker", lambda: "Which database?" in codex.pane())
    finding("the picker", codex.pane())
    codex.wait("codex's own answer", lambda: bool(_output(codex, call, since)), timeout=300)
    waited = time.monotonic() - asked_at
    finding("seconds codex waited before answering its question box alone", round(waited, 1))
    finding("the self-answered call's output", _output(codex, call, since))
    assert waited >= 100
    codex.wait_hooks("Stop", stops + 2)
    assert codex.quit() == (0, "ctrl-c")
    codex.ask_checkpoints["self_answered"] = codex.position()


# ---------------------------------------------------------------- the fixtures


def _capture(codex: Codex) -> None:
    """The thread's rollout and signals, with paths and the mock's port rewritten, and the
    line ranges each step wrote, for the translator's tests."""
    assert codex.rollout is not None
    swaps = [
        (str(codex.real / "codex"), "/CODEX_HOME"),
        (str(codex.root / "codex"), "/CODEX_HOME"),
        (str(codex.real / "wt"), "/WT"),
        (str(codex.root / "wt"), "/WT"),
        (str(codex.real / "run"), "/RUN"),
        (str(codex.root / "run"), "/RUN"),
        (str(codex.real), "/ROOT"),
        (str(codex.root), "/ROOT"),
        (codex.mock.url, "http://127.0.0.1:0"),
        (FILLER, "<filler>"),
    ]

    def rewrite(text: str) -> str:
        # Longest first: the root's link and real paths can each hold the other, by platform.
        for old, new in sorted(swaps, key=lambda swap: -len(swap[0])):
            text = text.replace(old, new)
        return text

    FIXTURES.mkdir(parents=True, exist_ok=True)
    rollout = rewrite(codex.rollout.read_text("utf-8")).splitlines(keepends=True)
    signals = rewrite(codex.path("run/signals.jsonl").read_text("utf-8")).splitlines(keepends=True)
    # The question-box launches come last and have fixtures of their own.
    end = codex.ask_start or (len(rollout), len(signals))
    _write_fixtures("session", rollout[: end[0]], signals[: end[1]], codex.checkpoints, (0, 0))
    if codex.ask_start is not None:
        ask = codex.ask_checkpoints
        _write_fixtures("ask", rollout[end[0] :], signals[end[1] :], ask, codex.ask_start)


def _write_fixtures(
    name: str,
    rollout: list[str],
    signals: list[str],
    checkpoints: dict[str, tuple[int, int]],
    start: tuple[int, int],
) -> None:
    """`<name>.rollout.jsonl`, `<name>.signals.jsonl` and the line ranges each step wrote, in
    `<name>.index.json` (the session's is `index.json`), counted from `start`."""
    (FIXTURES / f"{name}.rollout.jsonl").write_text("".join(rollout), "utf-8")
    (FIXTURES / f"{name}.signals.jsonl").write_text("".join(signals), "utf-8")
    index: dict[str, Any] = {}
    previous = (0, 0)
    for step, (rollout_lines, signal_lines) in checkpoints.items():
        at = (rollout_lines - start[0], signal_lines - start[1])
        index[step] = {"rollout": [previous[0], at[0]], "signals": [previous[1], at[1]]}
        previous = at
    file = "index.json" if name == "session" else f"{name}.index.json"
    (FIXTURES / file).write_text(json.dumps(index, indent=2) + "\n", "utf-8")
