"""tmux on a host runtime: one server per run and use, its socket under `<local_root>/t/`.

Every server starts from an environment built from nothing, so it keeps none of the worker's,
and loads the lockdown conf (`runtime.terminal.TMUX_LOCKDOWN`). A session's server also keeps
its pane once the process in it exits, so that process's exit status can be read. Typing goes
through a paste buffer, never `send-keys`, which tmux refuses while the only client attached
is a read-only one.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from runtime.adapters._cli.paths import write_private
from runtime.adapters._cli.process import CommandResult, probe_output
from runtime.contract import AgentRuntimeError, TerminalTarget
from runtime.terminal import TMUX_LOCKDOWN

__all__ = [
    "CONF_FILE",
    "MAX_SOCKET_BYTES",
    "SESSION_CONF_FILE",
    "SOCKETS_DIRNAME",
    "PaneState",
    "TmuxServer",
    "host_path",
    "prepare",
    "socket_path",
    "tmux_version",
]

SUBSTRATE: Final = "host"
SOCKETS_DIRNAME: Final = "t"
#: A session view's: a CLI that exits ends its session.
CONF_FILE: Final = "tmux.conf"
#: A session's: the dead pane stays, holding the CLI's exit status.
SESSION_CONF_FILE: Final = "tmux-session.conf"
SESSION_CONF: Final = f"{TMUX_LOCKDOWN}setw -g remain-on-exit on\n"
#: `sun_path` is 104 bytes on macOS and 108 on Linux, its NUL included.
MAX_SOCKET_BYTES: Final = 100
#: Each paste names its own buffer under this prefix: two pastes at once must not swap text.
BUFFER: Final = "factorial"
TIMEOUT: Final = 30.0
#: Between two looks at whether a killed server still serves its socket.
GONE_GAP: Final = 0.05

#: `-N`, which every call but a start passes and the gateway's attach too, came in 3.2.
MIN_VERSION: Final = (3, 2)
#: From 3.7 a paste's control characters arrive as text, `^[` for an Esc, unless it is given
#: `-S`, which no earlier tmux accepts.
RAW_PASTE_VERSION: Final = (3, 7)
#: Whether each tmux binary needs `-S`, asked once: one upgraded in place under the worker
#: fails its pastes of keys, and `finish` hangs up instead.
_raw_paste: dict[str, bool] = {}
_VERSION: Final = re.compile(r"(\d+)\.(\d+)")

#: `i` for a session view, `s` for the session: a run may have both at once.
SocketKind = Literal["i", "s"]


def socket_path(root: Path, run_id: str, kind: SocketKind, error: type[Exception]) -> str:
    """The run's socket of this kind. Raises `error` for one tmux or the gateway could not use:
    too long for `sun_path`, or outside the names a terminal target may carry."""
    digest = hashlib.sha256(run_id.encode()).hexdigest()[:12]
    socket = str(root / SOCKETS_DIRNAME / f"{digest}-{kind}")
    if len(socket.encode()) > MAX_SOCKET_BYTES:
        raise error(
            f"local_root {root} is too long for a tmux socket under it; it needs a path of "
            f"at most {MAX_SOCKET_BYTES - len(SOCKETS_DIRNAME) - 16} bytes"
        )
    try:
        TerminalTarget(substrate=SUBSTRATE, socket=socket, session="run")
    except ValueError as exc:
        raise error(f"local_root {root} holds a character a terminal target may not carry") from exc
    return socket


def prepare(root: Path) -> None:
    """The socket dir, closed to other users, and both confs, rewritten at every start."""
    sockets = root / SOCKETS_DIRNAME
    sockets.mkdir(mode=0o700, exist_ok=True)
    sockets.chmod(0o700)
    write_private(root / CONF_FILE, TMUX_LOCKDOWN)
    write_private(root / SESSION_CONF_FILE, SESSION_CONF)


def host_path() -> str:
    """The worker's PATH, absolute entries only: a relative one resolves in the worktree."""
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if entry.startswith("/"))


async def tmux_version(binary: str) -> tuple[int, int] | None:
    """`tmux -V`'s major and minor, or `None` when it names none ("tmux master"). Raises
    `AdapterUnavailable` when tmux does not answer."""
    shown = await probe_output(
        (binary, "-V"), env={"PATH": host_path(), "LANG": "C.UTF-8"}, within=TIMEOUT
    )
    found = _VERSION.search(shown)
    return (int(found[1]), int(found[2])) if found else None


@dataclass(frozen=True, slots=True)
class PaneState:
    dead: bool
    #: The exit status of a dead pane's process; `None` while it runs, if a signal ended it, or
    #: until tmux reaps it: tmux marks a pane dead once its terminal closes, which can come first.
    status: int | None
    pid: int
    #: The signal that ended a dead pane's process, once tmux reaped it. Never named by tmux 3.2.
    signal: str | None = None


@dataclass(frozen=True, slots=True)
class TmuxServer:
    """One run's server and its one session."""

    binary: str
    socket: str
    conf: Path
    #: The server's HOME: nothing in the operator's own is read for it.
    home: Path
    session: str

    @property
    def _target(self) -> str:
        return f"={self.session}:"

    async def run(self, *command: str, start: bool = False) -> CommandResult:
        """One tmux command on this socket. Only a start may start a server, and the server
        keeps the environment it started with: PATH, LANG and HOME."""
        argv = [self.binary, *([] if start else ["-N"]), "-u", "-S", self.socket]
        argv += ["-f", str(self.conf), *command]
        env = {"PATH": host_path(), "LANG": "C.UTF-8", "HOME": str(self.home)}
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(tuple(argv), -1, "", "tmux did not answer")
        return CommandResult(
            argv=tuple(argv),
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def start(
        self, cwd: Path, argv: list[str], *, cols: int = 200, rows: int = 50
    ) -> CommandResult:
        return await self.run(
            "new-session",
            "-d",
            "-s",
            self.session,
            "-x",
            str(cols),
            "-y",
            str(rows),
            "-c",
            str(cwd),
            "--",
            *argv,
            start=True,
        )

    async def has_session(self) -> bool:
        return (await self.run("has-session", "-t", f"={self.session}")).ok

    async def kill(self) -> CommandResult:
        """Returns once the server no longer serves the socket: tmux answers `kill-server`
        before its server exits, which until then accepts a client and drops it, so a start
        on the socket meanwhile fails."""
        killed = await self.run("kill-server")
        if killed.ok:
            for _ in range(round(TIMEOUT / GONE_GAP)):
                if not await asyncio.to_thread(self._served):
                    break
                await asyncio.sleep(GONE_GAP)
        return killed

    def _served(self) -> bool:
        with socket.socket(socket.AF_UNIX) as probe:
            # A server that stops accepting while it holds its socket fills the backlog, and a
            # connect then blocks: a probe that times out is a server still there.
            probe.settimeout(1.0)
            try:
                probe.connect(self.socket)
            except TimeoutError:
                return True
            except OSError:
                return False
        return True

    async def display(self, fmt: str) -> CommandResult:
        return await self.run("display-message", "-p", "-t", self._target, fmt)

    async def pid(self) -> int | None:
        """The pid the server on the socket prints as its own."""
        shown = await self.display("#{pid}")
        word = shown.stdout.strip()
        return int(word) if shown.ok and word.isdigit() else None

    async def pane(self) -> PaneState | None:
        """The session's pane, or `None` when its server or session is gone. Raises when tmux
        does not answer: that is no proof the session ended. A live server without the session
        still prints the format, every field empty, and exits 0."""
        shown = await self.display(
            "#{session_name}:#{pane_dead}:#{pane_dead_status}:#{pane_dead_signal}:#{pane_pid}"
        )
        if shown.returncode == -1:
            raise AgentRuntimeError(f"tmux on {self.socket} did not answer")
        if not shown.ok:
            return None
        words = shown.stdout.strip().split(":")
        if len(words) == 5 and words[0] == "":
            return None
        if (
            len(words) != 5
            or words[0] != self.session
            or words[1] not in ("0", "1")
            or not words[4].isdigit()
        ):
            raise AgentRuntimeError(f"tmux on {self.socket} printed {shown.stdout[:80]!r}")
        _, dead, status, signal, pid = words
        return PaneState(
            dead=dead == "1",
            status=int(status) if dead == "1" and status.isdigit() else None,
            pid=int(pid),
            signal=signal if dead == "1" and signal else None,
        )

    async def type_text(self, data: str, *, bracketed: bool = False, key: bool = False) -> bool:
        """Paste `data` into the pane as one paste. `bracketed` wraps it in bracketed-paste
        markers if the application in the pane asked for them, as a TUI's input line does;
        `key` sends control characters as themselves, for a constant key such as an Esc. False
        when nothing was typed: tmux refuses a command over about 16 KB, and pastes no empty
        buffer."""
        flags = ["-p"] if bracketed else []
        if key and await self._pastes_escaped():
            flags.append("-S")
        buffer = f"{BUFFER}-{secrets.token_hex(4)}"
        if not (await self.run("set-buffer", "-b", buffer, "--", data)).ok:
            return False
        pasted = await self.run("paste-buffer", "-d", *flags, "-b", buffer, "-t", self._target)
        if not pasted.ok:
            await self.run("delete-buffer", "-b", buffer)
        return pasted.ok

    async def _pastes_escaped(self) -> bool:
        if self.binary not in _raw_paste:
            version = await tmux_version(self.binary)
            _raw_paste[self.binary] = version is None or version >= RAW_PASTE_VERSION
        return _raw_paste[self.binary]

    async def capture(self) -> str | None:
        """The pane's screen, with tmux's non-breaking spaces as spaces; `None` once it is gone.
        Raises when tmux does not answer, as `pane()` does."""
        shown = await self.run("capture-pane", "-p", "-t", self._target)
        if shown.returncode == -1:
            raise AgentRuntimeError(f"tmux on {self.socket} did not answer")
        return shown.stdout.replace("\xa0", " ") if shown.ok else None
