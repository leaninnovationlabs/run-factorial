"""`HostTmuxRelay` against a real tmux server. tmux is required, never skipped: CI installs it."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import runtime.terminal
from runtime.contract import TerminalTarget
from runtime.terminal import TMUX_LOCKDOWN, HostTmuxRelay, TerminalStream, TerminalUnavailable

SESSION = "run"


@pytest.fixture(autouse=True)
def _tmux_installed() -> None:
    if shutil.which("tmux") is None:
        pytest.fail("tmux is not installed; these tests drive a real tmux server")


@dataclass
class Server:
    dir: Path
    socket: str

    def tmux(self, *args: str) -> str:
        done = subprocess.run(
            ["tmux", "-S", self.socket, *args],
            capture_output=True,
            text=True,
            env=_env_outside_tmux(),
        )
        if done.returncode != 0:
            pytest.fail(f"tmux {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout

    def pane(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", f"={SESSION}:")

    def window_size(self) -> str:
        return self.tmux(
            "display-message", "-p", "-t", f"={SESSION}:", "#{window_width}x#{window_height}"
        ).strip()

    def print_in_pane(self, data: bytes) -> None:
        # Not `send-keys`: with only a read-only client attached tmux refuses it.
        tty = self.tmux("display-message", "-p", "-t", f"={SESSION}:", "#{pane_tty}")
        Path(tty.strip()).write_bytes(data)

    def clients(self) -> int:
        return len(self.tmux("list-clients").splitlines())

    def target(self) -> TerminalTarget:
        return TerminalTarget(substrate="host", socket=self.socket, session=SESSION)


def _env_outside_tmux() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def _short_dir() -> Path:
    # pytest's tmp_path on macOS is longer than a unix socket path may be.
    return Path(tempfile.mkdtemp(dir="/tmp"))


@pytest.fixture
def server() -> Iterator[Server]:
    d = _short_dir()
    conf = d / "tmux.conf"
    conf.write_text(TMUX_LOCKDOWN)
    srv = Server(dir=d, socket=str(d / "tmux.sock"))
    new_session = ["new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "cat"]
    subprocess.run(
        ["tmux", "-S", srv.socket, "-f", str(conf), *new_session],
        check=True,
        env=_env_outside_tmux(),
    )
    try:
        yield srv
    finally:
        subprocess.run(["tmux", "-S", srv.socket, "kill-server"], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


async def eventually(check: Callable[[], bool], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


@asynccontextmanager
async def attached(
    srv: Server, *, writable: bool, cols: int = 80, rows: int = 24
) -> AsyncIterator[tuple[TerminalStream, bytearray]]:
    output = bytearray()
    stream = await HostTmuxRelay().attach(
        srv.target(), writable=writable, cols=cols, rows=rows, on_output=output.extend
    )
    try:
        yield stream, output
    finally:
        await stream.close()


async def test_a_watcher_cannot_type(server: Server) -> None:
    async with attached(server, writable=False) as (watcher, output):
        await watcher.write(b"typed-by-watcher\r")
        server.print_in_pane(b"from-the-pane\r\n")
        await eventually(lambda: b"from-the-pane" in output, "pane output at the watcher")
        await asyncio.sleep(0.3)
        assert "typed-by-watcher" not in server.pane()


async def test_a_writer_types_into_the_pane(server: Server) -> None:
    async with attached(server, writable=True) as (writer, _):
        await writer.write(b"typed-by-writer\r")
        await eventually(lambda: "typed-by-writer" in server.pane(), "the typed line in the pane")


async def test_the_writer_sets_the_window_size_and_a_watcher_does_not(server: Server) -> None:
    async with attached(server, writable=True, cols=100, rows=30) as (writer, _):
        await eventually(lambda: server.window_size() == "100x30", "the writer's attach size")
        await writer.resize(90, 25)
        await eventually(lambda: server.window_size() == "90x25", "the writer's resize")

        async with attached(server, writable=False, cols=60, rows=20) as (watcher, _):
            await eventually(lambda: server.clients() == 2, "the watcher attached")
            await watcher.resize(70, 22)
            await asyncio.sleep(0.5)
            assert server.window_size() == "90x25"


async def test_a_viewer_gets_the_panes_history_before_the_screen(server: Server) -> None:
    linked = b"\x1b[31m\x1b]8;;https://example.com/\x1b\\line-000\x1b]8;;\x1b\\\x1b[0m\r\n"
    server.print_in_pane(linked + b"".join(b"line-%03d\r\n" % n for n in range(1, 201)))
    await eventually(
        lambda: (
            int(server.tmux("display-message", "-p", "-t", f"={SESSION}:", "#{history_size}"))
            >= 150
        ),
        "the lines in the pane's history",
    )
    chunks: list[bytes] = []
    stream = await HostTmuxRelay().attach(
        server.target(), writable=False, cols=80, rows=24, on_output=chunks.append
    )
    try:
        history = chunks[0][: chunks[0].index(b"line-150")]
        assert history.startswith(b"\x1b[31mline-000\r\n")
        assert b"\x1b]" not in history, "a hyperlink reached the viewer's history"
        assert b"line-001\r\nline-002\r\n" in history
    finally:
        await stream.close()


async def test_closing_a_viewer_leaves_the_session_and_every_other_viewer(server: Server) -> None:
    async with attached(server, writable=True) as (writer, _):
        watcher = await HostTmuxRelay().attach(
            server.target(), writable=False, cols=80, rows=24, on_output=lambda _: None
        )
        await eventually(lambda: server.clients() == 2, "both viewers attached")
        await watcher.close()
        await watcher.close()

        await eventually(lambda: server.clients() == 1, "only the watcher gone")
        await writer.write(b"still-here\r")
        await eventually(lambda: "still-here" in server.pane(), "the writer still typing")


async def test_a_cancelled_attach_leaves_no_client_behind(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def spawn_then_signal(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await spawn(*args, **kwargs)
        if "attach-session" in args:
            spawned.set()
        return proc

    monkeypatch.setattr(runtime.terminal.asyncio, "create_subprocess_exec", spawn_then_signal)
    attach = asyncio.create_task(
        HostTmuxRelay().attach(
            server.target(), writable=True, cols=80, rows=24, on_output=lambda _: None
        )
    )
    await spawned.wait()
    attach.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attach

    await eventually(lambda: server.clients() == 0, "no client left attached")


async def test_an_output_callback_that_raises_never_stops_the_stream(server: Server) -> None:
    def broken(_: bytes) -> None:
        raise RuntimeError("the viewer went away")

    escaped: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _, context: escaped.append(context))
    try:
        writer = await HostTmuxRelay().attach(
            server.target(), writable=True, cols=80, rows=24, on_output=broken
        )
        await writer.write(b"typed\r")
        await eventually(lambda: "typed" in server.pane(), "the writer still typing")
        await writer.write(b"\x04")
        assert await asyncio.wait_for(writer.ended, 5) == "exited"
        await writer.close()
    finally:
        loop.set_exception_handler(None)
    assert escaped == []


async def test_lockdown_leaves_a_writer_no_way_into_tmux_commands(server: Server) -> None:
    marker = server.dir / "MARKER"

    def windows() -> int:
        return len(server.tmux("list-windows", "-t", f"={SESSION}").splitlines())

    async with attached(server, writable=True) as (writer, _):
        command = b'run-shell "touch ' + str(marker).encode() + b'"'
        for key in (b"\x02", b":", command, b"\r", b"\x02", b"c", b"input-settled\r"):
            await writer.write(key)
            # Keys closer together than tmux's assume-paste-time are a paste, which binds
            # nothing: without the gap this passes with no lockdown at all.
            await asyncio.sleep(0.05)
        await eventually(
            lambda: "input-settled" in server.pane() or marker.exists() or windows() > 1,
            "the writer's input settled",
        )
        await asyncio.sleep(0.3)

    assert not marker.exists()
    assert windows() == 1


@pytest.fixture
def no_server() -> Iterator[Path]:
    d = _short_dir()
    try:
        yield d
    finally:
        subprocess.run(["tmux", "-S", str(d / "tmux.sock"), "kill-server"], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


async def test_attach_never_starts_a_server(no_server: Path) -> None:
    target = TerminalTarget(substrate="host", socket=str(no_server / "tmux.sock"), session=SESSION)
    with pytest.raises(TerminalUnavailable):
        await HostTmuxRelay().attach(
            target, writable=True, cols=80, rows=24, on_output=lambda _: None
        )
    assert os.listdir(no_server) == []


async def test_a_session_that_ends_resolves_ended(server: Server) -> None:
    async with attached(server, writable=True) as (writer, _):
        await writer.write(b"\x04")
        assert await asyncio.wait_for(writer.ended, 5) == "exited"
