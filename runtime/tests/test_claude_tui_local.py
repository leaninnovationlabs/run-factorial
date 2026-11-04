"""L1: the real claude TUI in a host tmux, launched from `tui.py`'s documents against a mock API,
with its log read by `Transcript` and its hooks by `SessionSignals` (decision 56).

Opt-in, and never in CI: it needs the pinned CLI and tmux. It takes a few seconds.

    FACTORIAL_TUI_CLAUDE=<a claude PINNED_VERSION binary> \\
      uv run pytest -c pyproject.toml runtime/tests/test_claude_tui_local.py -q -p no:cacheprovider

A session's files live under one `mkdtemp(dir="/tmp")`: the config dir, HOME, the worktree, the
signals and the tmux socket, whose path must stay under sun_path's ~104 bytes. The mock's request
log has a temp dir of its own. The CLI runs under
`env -i` with the E2B placeholder credentials and `ANTHROPIC_BASE_URL` at `mock_anthropic.py`,
so no real key and no real `~/.claude` is ever in reach.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_tui_support import CLAUDE, DIALOGS, INPUT_MARK, PINNED_CLI, Mock, mock_api

from runtime.adapters._cli.launch import SIGNAL_SH
from runtime.adapters._cli.session_signals import SessionSignals
from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION
from runtime.adapters.claude_cli.stream import StreamReader, add
from runtime.adapters.claude_cli.transcript import Transcript, claude_log_signals
from runtime.adapters.claude_cli.tui import (
    pointer,
    resume_argv,
    seed_files,
    spawn_argv,
    tui_env,
    tui_flags,
)
from runtime.adapters.e2b.claude import (
    ENV_NONESSENTIAL,
    PLACEHOLDER_API_KEY,
    PLACEHOLDER_OAUTH_TOKEN,
)
from runtime.contract import AgentPrompts, IdleReason, RunSpec, Workspace

TURN_WAIT = 60.0

pytestmark = PINNED_CLI


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


@pytest.fixture
def mock() -> Iterator[Mock]:
    with mock_api() as api:
        yield api


@dataclass
class Tui:
    """One scratch session: its files, its tmux server, and the reader state a supervisor keeps."""

    root: Path
    mock: Mock
    auth_mode: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signals: SessionSignals = field(default_factory=SessionSignals)
    transcript: Transcript | None = None
    #: Every translated line, across segments.
    lines: list[str] = field(default_factory=list)
    #: (hook event, idle reason after it) for each signal line fed.
    trace: list[tuple[str, IdleReason | None]] = field(default_factory=list)
    signals_read: int = 0
    log_read: int = 0

    @property
    def credential(self) -> str:
        if self.auth_mode == AUTH_MODE_SUBSCRIPTION:
            return PLACEHOLDER_OAUTH_TOKEN
        return PLACEHOLDER_API_KEY

    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def config_dir(self) -> str:
        return str(self.path("cfg"))

    @property
    def worktree(self) -> str:
        return str(self.path("wt"))

    @property
    def log(self) -> Path:
        return self.path(f"cfg/projects/run/{self.session_id}.jsonl")

    def setup(self) -> None:
        for name in ("cfg", "home", "wt", "bin", "turns", "attachments"):
            self.path(name).mkdir()
        # The developer's own git config (signing, hooks) has no say in a scratch repo.
        git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", self.worktree], check=True, env=git_env)
        identity = ("-c", "user.email=l1@example.com", "-c", "user.name=l1")
        subprocess.run(
            ["git", "-C", self.worktree, *identity, "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
            env=git_env,
        )
        script = self.path("bin/signal.sh")
        script.write_text(SIGNAL_SH, "utf-8")
        script.chmod(0o755)
        self.path("signals.jsonl").touch(mode=0o600)

    def seed(self) -> None:
        files = seed_files(
            config_dir=self.config_dir,
            workspace_real=self.worktree,
            signal_script=str(self.path("bin/signal.sh")),
            auth_mode=self.auth_mode,
            credential=self.credential,
        )
        for name, content in files.items():
            Path(name).write_bytes(content)

    def flags(self, spec: RunSpec) -> tuple[str, ...]:
        return tui_flags(
            spec,
            system_prompt_path=str(self.path("system-prompt.md")),
            mcp_path=str(self.path("mcp.json")),
        )

    def env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.path("home")),
            "USER": "agent",
            "LANG": "C.UTF-8",
            ENV_NONESSENTIAL: "1",
            "ANTHROPIC_BASE_URL": self.mock.url,
            **tui_env(
                config_dir=self.config_dir,
                signals_path=str(self.path("signals.jsonl")),
                auth_mode=self.auth_mode,
                credential=self.credential,
            ),
        }

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        return subprocess.run(
            ["tmux", "-u", "-S", str(self.path("s")), "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            check=check,
            env=env,
        )

    def launch(self, argv: tuple[str, ...], *, turn: bool, cost_base: Any = None) -> None:
        """A segment, as a supervisor starts one: reseed, then the CLI under `env -i` in a pane
        whose exit code lands in `exit` when the CLI ends by itself."""
        self.seed()
        self.path("exit").unlink(missing_ok=True)
        self.signals.start_segment(turn=turn)
        self.transcript = Transcript(self.session_id, turn=turn, cost_base=cost_base)
        pairs = [f"{k}={v}" for k, v in self.env().items()]
        # The runner must die with the hangup, as E2B's does: a shell that survives it leaves
        # an idle CLI lingering for half a minute before it exits.
        runner = f'env -i "$@"; echo $? > {self.path("exit")}'
        self.tmux(
            "new-session", "-d", "-s", "run", "-x", "200", "-y", "50", "-c", self.worktree,
            "--", "/bin/sh", "-c", runner, "sh", *pairs, CLAUDE, *argv[1:],
        )  # fmt: skip

    def pane(self) -> str:
        # The input box draws a no-break space after its mark when tmux writes UTF-8.
        pane = self.tmux("capture-pane", "-p", "-t", "run", check=False).stdout
        return pane.replace("\xa0", " ")

    def alive(self) -> bool:
        return self.tmux("has-session", "-t", "run", check=False).returncode == 0

    def send(self, text: str) -> None:
        """Type `text`, and press Enter once the input box shows all of it. A sent prompt stays
        on screen under the same mark, hence the count."""
        typed = f"{INPUT_MARK} {text}"
        before = self.pane().count(typed)
        self.tmux("send-keys", "-t", "run", "-l", text)
        self.wait(f"{text!r} in the input box", lambda: self.pane().count(typed) > before)
        self.tmux("send-keys", "-t", "run", "Enter")

    def hooks(self, event: str) -> list[dict[str, Any]]:
        path = self.path("signals.jsonl")
        lines = path.read_text("utf-8").splitlines() if path.exists() else []
        return [
            hook
            for hook in (json.loads(line)["hook"] for line in lines)
            if hook.get("hook_event_name") == event
        ]

    def poll(self) -> None:
        """What one supervisor poll does: complete signal lines first, then complete log lines,
        the log to the translator and to the signals' fallback alike."""
        now = datetime.now(UTC)
        for raw in _complete(self.path("signals.jsonl"), self.signals_read):
            self.signals_read += len(raw) + 1
            line = raw.decode("utf-8")
            self.signals.feed(line, observed_at=now)
            self.trace.append(
                (json.loads(line)["hook"]["hook_event_name"], self.signals.idle_reason)
            )
        assert self.transcript is not None
        for raw in _complete(self.log, self.log_read):
            self.log_read += len(raw) + 1
            line = raw.decode("utf-8")
            self.lines += self.transcript.feed(line)
            claude_log_signals(self.signals, line, observed_at=now)

    def poll_until(self, what: str, done: Callable[[], bool]) -> None:
        """Poll until `done`: a hook can fire before the log holds the lines of its turn."""

        def polled() -> bool:
            self.poll()
            return done()

        self.wait(what, polled)

    def settle(self, turn_ends: int) -> None:
        """Poll until the log closed this segment's `turn_ends`-th turn: every line of it read."""
        self.poll_until(
            f"the log's turn end {turn_ends}", lambda: self.signals.log_turn_ends >= turn_ends
        )

    def end(self) -> dict[str, Any]:
        self.poll()
        assert self.transcript is not None
        out = self.transcript.end_segment(stop_message=self.signals.summary)
        self.lines += out
        return json.loads(out[-1])

    def wait(self, what: str, done: Callable[[], bool], timeout: float = TURN_WAIT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done():
                return
            time.sleep(0.25)
        signals = self.path("signals.jsonl").read_text("utf-8")[-3000:]
        raise AssertionError(
            f"timed out waiting for {what}\npane:\n{self.pane()}\nsignals:\n{signals}"
        )

    def wait_hooks(self, event: str, count: int) -> None:
        self.wait(f"{count} {event} hook(s)", lambda: len(self.hooks(event)) >= count)

    def hangup(self) -> None:
        """`kill-server`, as `finish.sh`'s hangup does, then wait for the CLI to finish exiting:
        it writes the session's running total on the way out."""
        self.tmux("kill-server", check=False)
        self.wait("the CLI to exit", lambda: not self.running(), timeout=20)

    def running(self) -> bool:
        found = subprocess.run(["pgrep", "-f", self.session_id], capture_output=True)
        return found.returncode == 0


def _complete(path: Path, offset: int) -> list[bytes]:
    """The whole lines past byte `offset`, as bytes, so the caller's offsets count bytes. A test
    helper only; the supervisor's own reader is the adapter's."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    return data[: data.rfind(b"\n") + 1].split(b"\n")[:-1]


@pytest.fixture
def tui(mock: Mock, request: pytest.FixtureRequest) -> Iterator[Tui]:
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="f243-", dir="/tmp")))
    session = Tui(root=root, mock=mock, auth_mode=getattr(request, "param", AUTH_MODE_API_KEY))
    try:
        session.setup()
        yield session
    finally:
        with contextlib.suppress(AssertionError):
            session.hangup()
        # A CLI that outlived its hangup would write into the tree while it is removed.
        subprocess.run(["pkill", "-KILL", "-f", session.session_id], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def _spec() -> RunSpec:
    return RunSpec(
        agent="plan-agent",
        ticket="GH-243",
        workspace=Workspace(repo="acme/widgets", base="main", branch="spec/243-{slug}"),
        prompts=AgentPrompts(system="unused: the system prompt is a file", instructions="unused"),
        model="claude-haiku-4-5-20251001",
    )


def _events(lines: list[str], kind: str) -> list[dict[str, Any]]:
    return [e for e in map(json.loads, lines) if e["type"] == kind]


def _user_texts(lines: list[str]) -> list[tuple[str, str]]:
    return [
        (e["factorial_origin"], " ".join(b["text"] for b in e["message"]["content"]))
        for e in _events(lines, "user")
        if "factorial_origin" in e
    ]


def test_the_real_tui_runs_a_session_from_the_documents_tui_py_builds(tui: Tui) -> None:
    marker, system_marker = f"TURN-{uuid.uuid4().hex[:8]}", f"SYS-{uuid.uuid4().hex[:8]}"
    tui.path("system-prompt.md").write_text(f"You are a test agent. {system_marker}", "utf-8")
    turn = tui.path("turns/0.md")
    turn.write_text(f"Reply with one word. {marker}", "utf-8")
    image = tui.path("attachments/swatch.png")
    image.write_bytes(_png((20, 20, 230)))
    flags = tui.flags(_spec())

    # 1. Launch with the orchestrator's turn and an image: no dialog, and the hooks fire.
    tui.launch(spawn_argv(flags, tui.session_id, pointer(str(turn), [str(image)])), turn=True)
    tui.wait_hooks("Stop", 1)
    assert not DIALOGS.search(tui.pane()), tui.pane()
    tui.settle(1)
    assert tui.signals.idle_reason is IdleReason.TURN_END
    assert tui.signals.last_input_at is None

    # 2. The `@` turn file was inlined, the system prompt is the file's, the image is image
    #    content, and the CLI's credential is the placeholder.
    first = tui.mock.messages()[0]
    body = json.dumps(first["body"])
    assert marker in body, "the @turn file was not inlined"
    assert system_marker in json.dumps(first["body"]["system"])
    assert any(
        block.get("type") == "image"
        and base64.b64decode(block["source"]["data"]) == image.read_bytes()
        for message in first["body"]["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ), "the @image did not arrive as an image block"
    assert {r["x-api-key"] for r in tui.mock.requests()} == {PLACEHOLDER_API_KEY}
    assert not any(r["authorization"] for r in tui.mock.requests())

    # 3. A typed turn is a human's.
    tui.send("Reply with one word. HUMAN-TURN")
    tui.wait_hooks("Stop", 2)
    tui.settle(2)
    assert tui.signals.idle_reason is IdleReason.TURN_END
    assert tui.signals.last_input_at is not None
    texts = _user_texts(tui.lines)
    assert texts[0][0] == "orchestrator" and marker not in texts[0][1]
    assert texts[-1] == ("human", "Reply with one word. HUMAN-TURN")

    # 4. A question idles the session; the `PostToolUse` hook on the answer makes it active.
    tui.send("ASKME about the scope.")
    tui.wait("the question", lambda: bool(tui.hooks("PreToolUse")))
    tui.poll()
    assert tui.signals.idle_reason is IdleReason.QUESTION
    assert tui.signals.question == "Include archived rows? [Active only | Active and archived]"
    tui.wait("the question's menu", lambda: "Active and archived" in tui.pane())
    tui.tmux("send-keys", "-t", "run", "Enter")
    tui.wait_hooks("Stop", 3)
    tui.settle(3)
    assert ("PostToolUse", None) in tui.trace, tui.trace
    assert tui.signals.idle_reason is IdleReason.TURN_END
    (answered,) = [e for e in _events(tui.lines, "user") if "tool_use_result" in e]
    assert answered["tool_use_result"] == {"answers": {"Include archived rows?": "Active only"}}

    # 5. `/exit` ends the CLI cleanly, and the segment reads whole.
    tui.send("/exit")
    tui.wait("the CLI to exit", lambda: not tui.alive())
    assert tui.path("exit").read_text().strip() == "0"
    result = tui.end()
    assert tui.signals.ended
    assert tui.transcript is not None and tui.transcript.missing() == ()
    reader = StreamReader()
    for line in tui.lines:
        reader.feed(line)
    assert reader.missing() == ()
    assert reader.error() is None
    # Four API calls, each its own message and request id: the dedup merged nothing it should not.
    assert reader.turns == len(tui.mock.messages()) == 4
    replies = _events(tui.lines, "assistant")
    assert len({e["message"]["id"] for e in replies}) == len(replies) == 4
    assert all(e["request_id"].startswith("req_") for e in replies)
    assert tui.transcript.unknown_types == {} and tui.transcript.unreadable == 0
    first_cost = tui.transcript.cost_seen
    assert first_cost is not None and first_cost.usd > 0
    assert Decimal(str(result["total_cost_usd"])) == first_cost.usd
    # Four turns in, a marketplace fetch would have landed: none was made.
    assert not tui.path("cfg/plugins").exists()

    # 6. Reseed and resume with no turn: the same file, at its prompt.
    segment_start = tui.log_read
    tui.launch(resume_argv(flags, tui.session_id, None), turn=False, cost_base=first_cost)
    tui.wait_hooks("SessionStart", 2)
    tui.poll()
    assert tui.trace[-1] == ("SessionStart", IdleReason.TURN_END)
    assert tui.signals.idle_reason is IdleReason.TURN_END
    assert [p.name for p in tui.log.parent.glob("*.jsonl")] == [tui.log.name]

    tui.send("Reply with one word. AFTER-RESUME")
    tui.wait_hooks("Stop", 4)
    assert not DIALOGS.search(tui.pane()), tui.pane()
    tui.settle(1)
    assert _user_texts(tui.lines)[-1] == ("human", "Reply with one word. AFTER-RESUME")

    # 7. A hangup mid-turn still writes the running total, and the segment's cost is its own.
    tui.send("SLOWME")
    tui.wait(
        "the slow request",
        lambda: any("SLOWME" in json.dumps(r["body"]) for r in tui.mock.messages()),
    )
    tui.hangup()
    costs = [
        json.loads(line)
        for line in tui.log.read_bytes()[segment_start:].decode().splitlines()
        if '"cost-state"' in line
    ]
    assert costs, "no cost-state after the hangup"
    second = tui.end()
    total = Decimal(str(costs[-1]["totalCostUSD"]))
    assert total > first_cost.usd
    assert Decimal(str(second["total_cost_usd"])) == total - first_cost.usd
    assert (
        add(Decimal(str(result["total_cost_usd"])), Decimal(str(second["total_cost_usd"]))) == total
    )


@pytest.mark.parametrize("tui", [AUTH_MODE_SUBSCRIPTION], indirect=True)
def test_a_subscription_session_reaches_its_prompt_with_no_dialog(tui: Tui) -> None:
    """The OAuth placeholder alone, no API key and no approved key."""
    tui.path("system-prompt.md").write_text("You are a test agent.", "utf-8")
    turn = tui.path("turns/0.md")
    turn.write_text("Reply with one word.", "utf-8")
    tui.launch(spawn_argv(tui.flags(_spec()), tui.session_id, pointer(str(turn))), turn=True)
    tui.wait_hooks("Stop", 1)
    assert not DIALOGS.search(tui.pane()), tui.pane()
    tui.poll()
    assert tui.signals.idle_reason is IdleReason.TURN_END
    requests = tui.mock.requests()
    assert requests and all(r["path"].startswith("/v1/messages") for r in requests)
    assert {r["authorization"] for r in requests} == {f"Bearer {PLACEHOLDER_OAUTH_TOKEN}"}
    assert not any(r["x-api-key"] for r in requests)
