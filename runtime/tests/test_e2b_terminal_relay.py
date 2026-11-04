"""`E2bTmuxRelay` over a fake `TerminalSandboxClient`, and the real client's one guard."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

import runtime.terminal
from runtime.adapters.e2b.client import (
    CommandOutput,
    E2bTerminalClient,
    SandboxGone,
    SandboxInfo,
    TerminalPty,
    TerminalSandbox,
)
from runtime.contract import SubstrateUnavailable, TerminalTarget
from runtime.terminal import E2bTmuxRelay, TerminalUnavailable

SANDBOX = "sbx-terminal"
SOCKET = "/run/factorial/tmux.sock"
SMCUP = b"\x1b[?1049h"
#: What a login shell prints before tmux takes the screen: the prompt and the echoed line.
PRE_ATTACH = b"Last login: today\r\nroot@sbx:~# stty -echo; exec tmux attach\r\n"


class FakePty:
    def __init__(self, on_data: Callable[[bytes], None], *, attaches: bool) -> None:
        self.on_data = on_data
        self.attaches = attaches
        self.sent: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.kills = 0
        self.kill_fails = False
        self.exit_code: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        if len(self.sent) == 1 and self.attaches:
            # smcup split across chunks, as envd may deliver it.
            self.on_data(PRE_ATTACH + SMCUP[:4])
            self.on_data(SMCUP[4:] + b"the pane")

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def kill(self) -> None:
        self.kills += 1
        if self.kill_fails:
            raise SubstrateUnavailable("E2B did not answer")
        self.exit(-1)

    async def wait(self) -> int:
        return await asyncio.shield(self.exit_code)

    def exit(self, code: int) -> None:
        if not self.exit_code.done():
            self.exit_code.set_result(code)


@dataclass
class FakeSandbox:
    attaches: bool = True
    #: Set to hold `pty_create` until the test releases it.
    creating: asyncio.Event | None = None
    created: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[dict[str, Any]] = field(default_factory=list)
    ptys: list[FakePty] = field(default_factory=list)
    raises: Exception | None = None
    #: What `capture-pane` prints: an empty history is one blank line.
    history: CommandOutput | Exception = field(default_factory=lambda: CommandOutput(0, "\n", ""))
    runs: list[dict[str, Any]] = field(default_factory=list)

    async def run(
        self, command: str, *, user: str, envs: Mapping[str, str], timeout_seconds: float
    ) -> CommandOutput:
        self.runs.append({"command": command, "user": user, "envs": envs})
        if isinstance(self.history, Exception):
            raise self.history
        return self.history

    async def pty_create(
        self,
        *,
        cols: int,
        rows: int,
        on_data: Callable[[bytes], None],
        user: str,
        timeout_seconds: float,
        envs: Mapping[str, str],
    ) -> TerminalPty:
        self.calls.append(
            {"cols": cols, "rows": rows, "user": user, "timeout": timeout_seconds, "envs": envs}
        )
        self.created.set()
        if self.raises is not None:
            raise self.raises
        if self.creating is not None:
            await self.creating.wait()
        pty = FakePty(on_data, attaches=self.attaches)
        self.ptys.append(pty)
        return pty


@dataclass
class FakeTerminalClient:
    sandbox: FakeSandbox = field(default_factory=FakeSandbox)
    state: str | None = "running"
    raises: Exception | None = None
    connects: int = 0

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        if self.state is None:
            return None
        return SandboxInfo(sandbox_id=sandbox_id, state=self.state, metadata={})

    async def connect_if_running(self, sandbox_id: str) -> TerminalSandbox | None:
        self.connects += 1
        if self.raises is not None:
            raise self.raises
        info = await self.info(sandbox_id)
        return self.sandbox if info is not None and info.state == "running" else None


def target(*, socket: str = SOCKET, session: str = "run") -> TerminalTarget:
    return TerminalTarget(substrate="e2b", socket=socket, session=session, sandbox_id=SANDBOX)


async def attach(
    relay: E2bTmuxRelay,
    *,
    writable: bool = False,
    output: bytearray | None = None,
    to: TerminalTarget | None = None,
) -> runtime.terminal.TerminalStream:
    sink = output if output is not None else bytearray()
    return await relay.attach(
        to or target(), writable=writable, cols=80, rows=24, on_output=sink.extend
    )


async def eventually(check: Callable[[], bool], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def _rest(states: dict[str, str]) -> tuple[httpx.MockTransport, list[str]]:
    """E2B's REST API: `GET /sandboxes/{id}` answers from `states`, every other call is
    recorded and refused."""
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        sandbox_id = request.url.path.removeprefix("/sandboxes/")
        if request.method != "GET" or sandbox_id not in states:
            return httpx.Response(404, json={"code": 404, "message": "not found"})
        detail = {
            "templateID": "factorial-agent",
            "sandboxID": sandbox_id,
            "clientID": "client",
            "startedAt": "2026-10-02T00:00:00Z",
            "endAt": "2026-10-02T01:00:00Z",
            "envdVersion": "0.2.4",
            "envdAccessToken": "envd-token",
            "domain": "e2b.test",
            "cpuCount": 2,
            "memoryMB": 1024,
            "diskSizeMB": 4096,
            "state": states[sandbox_id],
        }
        return httpx.Response(200, json=detail)

    return httpx.MockTransport(answer), calls


@pytest.mark.parametrize("state", ["paused", None])
async def test_a_viewer_never_wakes_a_paused_sandbox(state: str | None) -> None:
    transport, calls = _rest({SANDBOX: state} if state else {})
    client = E2bTerminalClient("e2b-key", transport=transport)

    assert await client.connect_if_running(SANDBOX) is None
    with pytest.raises(TerminalUnavailable):
        await attach(E2bTmuxRelay(client))
    assert calls == [f"GET /sandboxes/{SANDBOX}"] * 2, "only reads, never a connect"


async def test_a_running_sandbox_is_reached_without_the_connect_api() -> None:
    """A connect resumes a sandbox paused since the read, and sets its expiry: E2B then kills
    it, workspace and all, at that timeout. The handle is built from the read alone."""
    transport, calls = _rest({SANDBOX: "running"})
    client = E2bTerminalClient("e2b-key", transport=transport)

    sandbox = await client.connect_if_running(SANDBOX)

    assert sandbox is not None
    assert calls == [f"GET /sandboxes/{SANDBOX}"]
    handle = sandbox._sandbox  # type: ignore[attr-defined]
    assert handle.sandbox_id == SANDBOX and handle._envd_access_token == "envd-token"
    assert handle.connection_config.sandbox_headers["X-Access-Token"] == "envd-token"


async def test_a_watcher_attaches_read_only_as_root_with_no_timeout() -> None:
    client = FakeTerminalClient()
    stream = await attach(E2bTmuxRelay(client), writable=False)
    try:
        envs = {"FACTORIAL_TMUX_SOCKET": SOCKET, "FACTORIAL_TMUX_SESSION": "run"}
        assert client.sandbox.calls == [
            {"cols": 80, "rows": 24, "user": "root", "timeout": 0, "envs": envs}
        ]
        assert client.sandbox.ptys[0].sent[0] == (
            b'stty -echo; exec tmux -N -u -S "$FACTORIAL_TMUX_SOCKET" '
            b'attach-session -t ="$FACTORIAL_TMUX_SESSION" -r\n'
        )
    finally:
        await stream.close()


def _unchecked(*, socket: str = SOCKET, session: str = "run") -> TerminalTarget:
    """A target `TerminalTarget` itself would refuse: the relay must not rely on that."""
    return TerminalTarget.model_construct(
        substrate="e2b", socket=socket, session=session, sandbox_id=SANDBOX
    )


@pytest.mark.parametrize(
    "to",
    [
        _unchecked(session="x\x03echo INJECTED-$((6*7)) #"),
        _unchecked(session="run\n"),
        _unchecked(socket="/run/x.sock; touch /tmp/owned"),
    ],
)
async def test_a_target_name_never_reaches_the_root_shell_as_typed_bytes(
    to: TerminalTarget,
) -> None:
    client = FakeTerminalClient()
    with pytest.raises(ValueError):
        await attach(E2bTmuxRelay(client), to=to)
    assert client.connects == 0


async def test_output_before_tmux_attaches_is_dropped() -> None:
    output = bytearray()
    stream = await attach(E2bTmuxRelay(FakeTerminalClient()), output=output)
    try:
        assert bytes(output) == SMCUP + b"the pane"
    finally:
        await stream.close()


async def test_the_panes_history_leads_the_output() -> None:
    client = FakeTerminalClient()
    client.sandbox.history = CommandOutput(0, "one\ntwo\n", "")
    output = bytearray()
    stream = await attach(E2bTmuxRelay(client), output=output)
    try:
        assert bytes(output).startswith(b"one\r\ntwo\x1b[0m\r\n")
        assert bytes(output).endswith(SMCUP + b"the pane")
        assert client.sandbox.runs == [
            {
                "command": 'tmux -N -S "$FACTORIAL_TMUX_SOCKET" capture-pane -p -e -J '
                '-t ="$FACTORIAL_TMUX_SESSION": -S - -E -1',
                "user": "root",
                "envs": {"FACTORIAL_TMUX_SOCKET": SOCKET, "FACTORIAL_TMUX_SESSION": "run"},
            }
        ]
    finally:
        await stream.close()


async def test_a_history_that_cannot_be_read_never_stops_an_attach() -> None:
    client = FakeTerminalClient()
    client.sandbox.history = SubstrateUnavailable("E2B did not answer")
    output = bytearray()
    stream = await attach(E2bTmuxRelay(client), output=output)
    try:
        assert bytes(output) == SMCUP + b"the pane"
    finally:
        await stream.close()


async def _close_normally(client: FakeTerminalClient) -> None:
    stream = await attach(E2bTmuxRelay(client))
    await stream.close()
    assert await asyncio.wait_for(stream.ended, 1) == "closed"


async def _cancel_while_the_pty_starts(client: FakeTerminalClient) -> None:
    client.sandbox.creating = asyncio.Event()
    attaching = asyncio.create_task(attach(E2bTmuxRelay(client)))
    await client.sandbox.created.wait()
    attaching.cancel()
    await asyncio.sleep(0)
    client.sandbox.creating.set()
    with pytest.raises(asyncio.CancelledError):
        await attaching


async def _cancel_while_tmux_attaches(client: FakeTerminalClient) -> None:
    client.sandbox.attaches = False
    attaching = asyncio.create_task(attach(E2bTmuxRelay(client)))
    await client.sandbox.created.wait()
    await asyncio.sleep(0.05)
    attaching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attaching


async def _time_out_attaching(client: FakeTerminalClient) -> None:
    client.sandbox.attaches = False
    with pytest.raises(TerminalUnavailable, match="did not attach"):
        await attach(E2bTmuxRelay(client, attach_timeout=0.1))


@pytest.mark.parametrize(
    "path",
    [
        _close_normally,
        _cancel_while_the_pty_starts,
        _cancel_while_tmux_attaches,
        _time_out_attaching,
    ],
)
async def test_close_kills_the_pty(path: Callable[[FakeTerminalClient], Any]) -> None:
    client = FakeTerminalClient()
    await path(client)
    await eventually(lambda: [pty.kills for pty in client.sandbox.ptys] == [1], "one pty killed")
    await asyncio.sleep(0.05)
    assert [pty.kills for pty in client.sandbox.ptys] == [1]


@pytest.mark.parametrize("fails", ["connect_if_running", "pty_create"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SandboxGone(SANDBOX), f"sandbox {SANDBOX} is gone"),
        (SubstrateUnavailable("E2B did not answer"), "E2B did not answer"),
    ],
)
async def test_an_unreachable_sandbox_is_unavailable(
    fails: str, error: Exception, reason: str
) -> None:
    client = FakeTerminalClient()
    if fails == "connect_if_running":
        client.raises = error
    else:
        client.sandbox.raises = error
    with pytest.raises(TerminalUnavailable) as refused:
        await attach(E2bTmuxRelay(client))
    assert str(refused.value) == reason


async def test_ended_resolves_when_the_pty_exits() -> None:
    stream = await attach(E2bTmuxRelay(client := FakeTerminalClient()))
    client.sandbox.ptys[0].exit(0)
    assert await asyncio.wait_for(stream.ended, 1) == "exited"
    await stream.close()


async def test_ended_resolves_when_the_kill_cannot_reach_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime.terminal, "_CLOSE_GRACE", 0.1)
    stream = await attach(E2bTmuxRelay(client := FakeTerminalClient()))
    client.sandbox.ptys[0].kill_fails = True
    await asyncio.wait_for(stream.close(), 1)
    assert await asyncio.wait_for(stream.ended, 1) == "closed"


async def test_a_tmux_that_cannot_attach_says_why() -> None:
    client = FakeTerminalClient()
    client.sandbox.attaches = False
    attaching = asyncio.create_task(attach(E2bTmuxRelay(client)))
    await client.sandbox.created.wait()
    await asyncio.sleep(0.05)
    pty = client.sandbox.ptys[0]
    pty.on_data(PRE_ATTACH + b"can't find session: =run\r\n")
    pty.exit(1)
    with pytest.raises(TerminalUnavailable, match="can't find session: =run"):
        await attaching
