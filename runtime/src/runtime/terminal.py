"""Terminal relays: attach one viewer to a run's tmux session (decision 57).

A relay attaches a tmux CLIENT to a session the worker or the runner already started; it never
starts a server. A watcher is a read-only client (`attach -r`), so read-only is enforced by tmux,
not by whoever forwards the keystrokes. Every tmux server we start loads `TMUX_LOCKDOWN`, which
leaves a writer no key that reaches a tmux command: on E2B that server is root's. With copy mode
locked away, a viewer scrolls back in its own terminal: a relay sends the pane's history ahead of
tmux's first redraw.

`HostTmuxRelay` attaches on this host; `E2bTmuxRelay` reaches a sandbox only through a
`TerminalSandboxClient`, which cannot wake a paused one (decision 25).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final, Protocol

from runtime.contract import SubstrateUnavailable, TerminalTarget

if TYPE_CHECKING:
    from runtime.adapters.e2b.client import TerminalPty, TerminalSandbox, TerminalSandboxClient

log = logging.getLogger(__name__)

__all__ = [
    "TMUX_LOCKDOWN",
    "E2bTmuxRelay",
    "HostTmuxRelay",
    "TerminalRelay",
    "TerminalStream",
    "TerminalUnavailable",
    "attach_argv",
    "history_argv",
]

TMUX_LOCKDOWN: Final[str] = (
    "set -g prefix None\n"
    "set -g prefix2 None\n"
    "unbind-key -a -T prefix\n"
    "unbind-key -a -T root\n"
    "unbind-key -a -T copy-mode\n"
    "unbind-key -a -T copy-mode-vi\n"
    "set -g status off\n"
    "set -g mouse off\n"
    "set -g history-limit 10000\n"
    # `-q`: tmux 3.2, the floor, has no such option and always passes escapes through. An error
    # here would leave the session's pane in view mode, with no key left to close it.
    "set -gq allow-passthrough off\n"
    "set -g set-clipboard off\n"
)

#: tmux's smcup: written once the client has attached.
_ATTACHED: Final = b"\x1b[?1049h"

#: Pending input past this many bytes makes `write()` wait for the pty to drain.
_HIGH_WATER: Final = 64 * 1024

#: Seconds `close()` waits for a client to go before it stops waiting.
_CLOSE_GRACE: Final = 5.0

#: Pre-attach output kept to find tmux's smcup in: a login shell's banner can be long.
_PRE_ATTACH_KEPT: Final = 64 * 1024

#: The lockdown's history-limit: no viewer is sent more history than this.
_HISTORY_LINES: Final = 10000

#: Seconds an attach waits for the pane's history before it attaches without it.
_HISTORY_TIMEOUT: Final = 5.0

#: Colours are all a viewer's history keeps. capture-pane `-e` also writes OSC 8 hyperlinks,
#: which the live stream never carries; anything else here would be a tmux change.
_ESCAPE: Final = re.compile(
    rb"(\x1b\[[0-9;:]*m)"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    rb"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    rb"|\x1b\[[0-?]*[ -/]*[@-~]"
    rb"|\x1b[^\[\]PX^_]?"
    rb"|[\x00-\x08\x0b-\x1f\x7f]|\xc2[\x80-\x9f]"
)

#: What an E2B socket or session name may hold. They reach the root shell only as variables;
#: this is a second fence, since a target is read back from the event log.
_E2B_NAME: Final = re.compile(r"[A-Za-z0-9_./-]+")


def attach_argv(socket: str, session: str, *, writable: bool) -> list[str]:
    """`-N`: without it `attach-session` starts a server when none is running, loading the
    attaching user's own config. `-u`: the browser's terminal always decodes UTF-8, whatever
    locale the relay runs under. `=` makes the session name an exact match."""
    return [
        "tmux",
        "-N",
        "-u",
        "-S",
        socket,
        "attach-session",
        "-t",
        f"={session}",
        *([] if writable else ["-r"]),
    ]


def history_argv(socket: str, session: str) -> list[str]:
    """The pane's lines above its screen, wrapped lines joined (`-J`), with their colours (`-e`,
    SGR only: never `-C`, which would pass other control sequences)."""
    return [
        "tmux",
        "-N",
        "-S",
        socket,
        "capture-pane",
        "-p",
        "-e",
        "-J",
        "-t",
        f"={session}:",
        "-S",
        "-",
        "-E",
        "-1",
    ]


def _sgr_only(captured: bytes) -> bytes:
    return _ESCAPE.sub(lambda m: m[1] or b"", captured)


def _history(captured: bytes, rows: int) -> bytes:
    """Captured history as a viewer's first output. The newlines after it scroll it all off the
    screen, which tmux clears on attach."""
    lines = _sgr_only(captured).splitlines()[-_HISTORY_LINES:]
    if not any(lines):
        return b""
    return b"\r\n".join(lines) + b"\x1b[0m" + b"\r\n" * rows


def _host_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath), "TERM": "xterm-256color"}


async def _host_history(target: TerminalTarget, rows: int) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            *history_argv(target.socket, target.session),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_host_env(),
        )
    except OSError:
        log.warning(
            "reading %s's history failed; attaching without it", target.session, exc_info=True
        )
        return b""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _HISTORY_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        log.warning("reading %s's history timed out; attaching without it", target.session)
        return b""
    if proc.returncode != 0:
        log.warning(
            "reading %s's history failed (%s); attaching without it",
            target.session,
            err.decode(errors="replace").strip() or f"status {proc.returncode}",
        )
        return b""
    return _history(out, rows)


async def _e2b_history(sandbox: TerminalSandbox, envs: Mapping[str, str], rows: int) -> bytes:
    argv = history_argv('"$FACTORIAL_TMUX_SOCKET"', '"$FACTORIAL_TMUX_SESSION"')
    try:
        done = await sandbox.run(
            " ".join(argv), user="root", envs=envs, timeout_seconds=_HISTORY_TIMEOUT
        )
    except Exception:
        log.warning("reading a sandbox pane's history failed; attaching without it", exc_info=True)
        return b""
    if not done.ok:
        log.warning(
            "reading a sandbox pane's history failed (%s); attaching without it",
            done.stderr.strip() or f"status {done.exit_code}",
        )
        return b""
    return _history(done.stdout.encode(), rows)


class TerminalUnavailable(Exception):
    """The session cannot be attached now: no server, no such session, or no tmux."""


class TerminalStream(Protocol):
    #: Resolves once the client is gone: `"closed"` after `close()`, `"exited"` when tmux let it
    #: go (the session ended), or a sentence when it failed.
    ended: asyncio.Future[str]

    async def write(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def close(self) -> None:
        """Idempotent. Ends this viewer's client only; the session and other viewers stay."""
        ...


class TerminalRelay(Protocol):
    async def attach(
        self,
        target: TerminalTarget,
        *,
        writable: bool,
        cols: int,
        rows: int,
        on_output: Callable[[bytes], None],
        history: bool = True,
    ) -> TerminalStream:
        """With `history`, the pane's history comes ahead of the first output; a viewer already
        holding it re-attaches without."""
        ...


def _check_size(cols: int, rows: int) -> None:
    if not (0 < cols <= 0xFFFF and 0 < rows <= 0xFFFF):
        raise ValueError(
            f"a terminal's columns and rows must each be between 1 and 65535, not {cols} by {rows}"
        )


def _set_size(fd: int, cols: int, rows: int) -> None:
    _check_size(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class HostTmuxRelay:
    """`tmux attach` on a pty this process owns, for a session on this host."""

    def __init__(self, *, attach_timeout: float = 5.0) -> None:
        self._attach_timeout = attach_timeout

    async def attach(
        self,
        target: TerminalTarget,
        *,
        writable: bool,
        cols: int,
        rows: int,
        on_output: Callable[[bytes], None],
        history: bool = True,
    ) -> TerminalStream:
        # Read before the client attaches: a line that scrolls in between is lost, never doubled.
        t0 = time.monotonic()
        scrollback = await _host_history(target, rows) if history else b""
        t_history = time.monotonic()
        master, slave = pty.openpty()
        try:
            _set_size(master, cols, rows)
            os.set_blocking(master, False)
            proc = await asyncio.create_subprocess_exec(
                *attach_argv(target.socket, target.session, writable=writable),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=_host_env(),
            )
        except FileNotFoundError as e:
            os.close(master)
            raise TerminalUnavailable("tmux is not installed on this host") from e
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)

        t_pty = time.monotonic()
        stream = _HostTmuxStream(master, proc, on_output)
        stream.history = scrollback
        try:
            refused = await asyncio.wait_for(asyncio.shield(stream.attached), self._attach_timeout)
            if refused is not None:
                raise TerminalUnavailable(refused)
        except BaseException as e:
            await stream.close()
            if isinstance(e, TimeoutError):
                raise TerminalUnavailable(
                    f"tmux did not attach to {target.session} within {self._attach_timeout:g}s"
                ) from None
            raise
        done = time.monotonic()
        log.info(
            "attached to %s in %.2fs (connect -, pty %.2fs, history %.2fs %d bytes, "
            "tmux %.2fs)",
            target.session,
            done - t0,
            t_pty - t_history,
            t_history - t0,
            len(scrollback),
            done - t_pty,
        )
        return stream


class _HostTmuxStream:
    def __init__(
        self,
        master: int,
        proc: asyncio.subprocess.Process,
        on_output: Callable[[bytes], None],
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._master: int | None = master
        self._proc = proc
        self._on_output = on_output
        self._before_attach = bytearray()
        #: Sent ahead of the first output once tmux has the screen.
        self.history = b""
        self._pending = bytearray()
        self._drained = asyncio.Event()
        self._drained.set()
        self._closing = False
        self._output_failed = False
        #: `None` once tmux has the screen, else why it never will. Never an exception: asyncio
        #: logs an error for a shielded future that fails after its waiter gave up.
        self.attached: asyncio.Future[str | None] = self._loop.create_future()
        self.ended: asyncio.Future[str] = self._loop.create_future()
        self._loop.add_reader(master, self._on_readable)
        self._waiter = self._loop.create_task(self._wait_for_exit())

    def _on_readable(self) -> None:
        if self._master is None:
            return
        try:
            chunk = os.read(self._master, 65536)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every slave end is closed. `_wait_for_exit` finishes the stream.
            chunk = b""
        if not chunk:
            self._loop.remove_reader(self._master)
            return
        self._deliver(chunk)

    def _deliver(self, chunk: bytes) -> None:
        if self.attached.done():
            self._emit(chunk)
            return
        self._before_attach += chunk
        if _ATTACHED in self._before_attach:
            self.attached.set_result(None)
            self._emit(self.history + self._before_attach)
            self._before_attach.clear()

    def _emit(self, chunk: bytes) -> None:
        try:
            self._on_output(chunk)
        except Exception:
            if not self._output_failed:
                self._output_failed = True
                log.exception(
                    "a terminal viewer's output callback failed; later failures go unlogged"
                )

    def _drain_master(self) -> None:
        while self._master is not None:
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._deliver(chunk)

    async def _wait_for_exit(self) -> None:
        rc = await self._proc.wait()
        try:
            self._drain_master()
        except Exception:
            log.exception("draining a terminal client's last output failed")
        if self._closing:
            reason = "closed"
        elif rc == 0:
            reason = "exited"
        else:
            said = bytes(self._before_attach).decode(errors="replace").strip()
            reason = said or f"tmux exited with status {rc}"
        if not self.attached.done():
            self.attached.set_result(reason)
        self._release()
        if not self.ended.done():
            self.ended.set_result(reason)

    def _release(self) -> None:
        if self._master is None:
            return
        self._loop.remove_reader(self._master)
        self._loop.remove_writer(self._master)
        os.close(self._master)
        self._master = None
        self._pending.clear()
        self._drained.set()

    async def write(self, data: bytes) -> None:
        if self._master is None or not data:
            return
        self._pending += data
        self._flush()
        while len(self._pending) > _HIGH_WATER and self._master is not None:
            self._drained.clear()
            await self._drained.wait()

    def _flush(self) -> None:
        if self._master is None:
            return
        try:
            written = os.write(self._master, self._pending)
        except BlockingIOError:
            written = 0
        except OSError:
            self._pending.clear()
            self._loop.remove_writer(self._master)
            self._drained.set()
            return
        del self._pending[:written]
        if self._pending:
            self._loop.add_writer(self._master, self._flush)
        else:
            self._loop.remove_writer(self._master)
        if len(self._pending) <= _HIGH_WATER:
            self._drained.set()

    async def resize(self, cols: int, rows: int) -> None:
        if self._master is None:
            return
        _set_size(self._master, cols, rows)
        # The client has no controlling tty, so the kernel sends it no SIGWINCH of its own.
        with contextlib.suppress(ProcessLookupError):
            self._proc.send_signal(signal.SIGWINCH)

    async def close(self) -> None:
        if not self._closing:
            self._closing = True
            with contextlib.suppress(ProcessLookupError):
                self._proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(self._waiter), _CLOSE_GRACE)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._waiter


class E2bTmuxRelay:
    """`tmux attach` on a pty in the run's sandbox, as root: the sandbox's tmux server is root's."""

    def __init__(self, client: TerminalSandboxClient, *, attach_timeout: float = 15.0) -> None:
        self._client = client
        self._attach_timeout = attach_timeout

    async def attach(
        self,
        target: TerminalTarget,
        *,
        writable: bool,
        cols: int,
        rows: int,
        on_output: Callable[[bytes], None],
        history: bool = True,
    ) -> TerminalStream:
        sandbox_id = target.sandbox_id
        if sandbox_id is None:
            raise ValueError("an E2B terminal target must name its sandbox")
        for name in (target.socket, target.session):
            if not _E2B_NAME.fullmatch(name):
                raise ValueError(f"an E2B terminal target names a socket or session {name!r}")
        _check_size(cols, rows)
        t0 = time.monotonic()
        try:
            sandbox = await self._client.connect_if_running(sandbox_id)
        except _unreachable() as e:
            raise TerminalUnavailable(_why(e, sandbox_id)) from e
        if sandbox is None:
            raise TerminalUnavailable(f"sandbox {sandbox_id} is not running")

        t_connect = time.monotonic()
        stream = _E2bTmuxStream(sandbox_id, on_output)
        envs = {"FACTORIAL_TMUX_SOCKET": target.socket, "FACTORIAL_TMUX_SESSION": target.session}
        capturing = asyncio.ensure_future(
            _e2b_history(sandbox, envs, rows) if history else asyncio.sleep(0, b"")
        )
        creating = asyncio.ensure_future(
            sandbox.pty_create(
                cols=cols,
                rows=rows,
                on_data=stream.deliver,
                user="root",
                timeout_seconds=0,
                envs=envs,
            )
        )
        # A pty envd starts after this attach gave up still reaches the stream, to be killed.
        creating.add_done_callback(stream.adopt)
        try:
            # Not `shield`: it would log a creation that fails after a cancel as an error.
            await asyncio.wait([creating])
            stream.start(creating.result())
            t_pty = time.monotonic()
            # Before tmux attaches: a line that scrolls in between is lost, never doubled.
            stream.history = await capturing
            t_history = time.monotonic()
            # The pty runs a login bash whatever is asked for. The names reach it only as
            # variables, so no byte of theirs is ever typed into a root shell. `exec` leaves
            # tmux's exit status as the pty's, and output is dropped until tmux takes the screen.
            argv = attach_argv(
                '"$FACTORIAL_TMUX_SOCKET"', '"$FACTORIAL_TMUX_SESSION"', writable=writable
            )
            await stream.send(f"stty -echo; exec {' '.join(argv)}\n".encode())
            refused = await asyncio.wait_for(asyncio.shield(stream.attached), self._attach_timeout)
            if refused is not None:
                raise TerminalUnavailable(refused)
        except BaseException as e:
            capturing.cancel()
            await stream.close()
            if isinstance(e, TimeoutError):
                raise TerminalUnavailable(
                    f"tmux did not attach to {target.session} within {self._attach_timeout:g}s"
                ) from None
            if isinstance(e, _unreachable()):
                raise TerminalUnavailable(_why(e, sandbox_id)) from e
            raise
        done = time.monotonic()
        # The capture runs beside the pty's creation, so its figure is the wait left after it.
        log.info(
            "attached to %s in %.2fs (connect %.2fs, pty %.2fs, history %.2fs %d bytes, "
            "tmux %.2fs)",
            target.session,
            done - t0,
            t_connect - t0,
            t_pty - t_connect,
            t_history - t_pty,
            len(stream.history),
            done - t_history,
        )
        return stream


def _unreachable() -> tuple[type[Exception], ...]:
    # Imported late: the e2b package's import pulls in the adapter, which may import this module.
    from runtime.adapters.e2b.client import SandboxGone

    return (SandboxGone, SubstrateUnavailable)


def _why(e: Exception, sandbox_id: str) -> str:
    return str(e) if isinstance(e, SubstrateUnavailable) else f"sandbox {sandbox_id} is gone"


class _E2bTmuxStream:
    def __init__(self, sandbox_id: str, on_output: Callable[[bytes], None]) -> None:
        self._loop = asyncio.get_running_loop()
        self._sandbox_id = sandbox_id
        self._on_output = on_output
        self._unreachable = _unreachable()
        self._pty: TerminalPty | None = None
        self._before_attach = bytearray()
        #: Sent ahead of the first output once tmux has the screen.
        self.history = b""
        self._closing = False
        self._failure: str | None = None
        self._output_failed = False
        self._waiter: asyncio.Task[None] | None = None
        self._killing: asyncio.Task[None] | None = None
        #: `None` once tmux has the screen, else why it never will. Never an exception: asyncio
        #: logs an error for a shielded future that fails after its waiter gave up.
        self.attached: asyncio.Future[str | None] = self._loop.create_future()
        self.ended: asyncio.Future[str] = self._loop.create_future()

    def adopt(self, creating: asyncio.Future[TerminalPty]) -> None:
        if creating.cancelled() or creating.exception() is not None:
            return
        self.start(creating.result())
        if self._closing:
            self._kill_now()

    def start(self, pty: TerminalPty) -> None:
        if self._pty is not None:
            return
        self._pty = pty
        self._waiter = self._loop.create_task(self._wait_for_exit(pty))

    def deliver(self, chunk: bytes) -> None:
        if self.attached.done():
            self._emit(chunk)
            return
        self._before_attach += chunk
        at = self._before_attach.find(_ATTACHED)
        if at < 0:
            del self._before_attach[:-_PRE_ATTACH_KEPT]
            return
        self.attached.set_result(None)
        self._emit(self.history + self._before_attach[at:])
        self._before_attach.clear()

    def _emit(self, chunk: bytes) -> None:
        try:
            self._on_output(chunk)
        except Exception:
            if not self._output_failed:
                self._output_failed = True
                log.exception(
                    "a terminal viewer's output callback failed; later failures go unlogged"
                )

    async def _wait_for_exit(self, pty: TerminalPty) -> None:
        reason = "closed"
        try:
            status = await pty.wait()
            if not self._closing:
                reason = self._exit_reason(status)
        except Exception as e:
            if not self._closing:
                log.warning("the stream from a sandbox terminal broke", exc_info=True)
                reason = f"the connection to the sandbox's terminal broke: {e}"
        finally:
            self._finish(self._failure or reason)

    def _exit_reason(self, status: int) -> str:
        if status == 0:
            return "exited"
        said = bytes(self._before_attach).decode(errors="replace").strip().splitlines()
        return said[-1].strip() if said else f"tmux exited with status {status}"

    def _finish(self, reason: str) -> None:
        if not self.attached.done():
            self.attached.set_result(reason)
        if not self.ended.done():
            self.ended.set_result(reason)

    async def send(self, data: bytes) -> None:
        if self._pty is not None and not self._closing:
            await self._pty.send(data)

    async def write(self, data: bytes) -> None:
        if not data:
            return
        try:
            await self.send(data)
        except self._unreachable as e:
            await self._fail(
                f"typing into the sandbox's terminal failed: {_why(e, self._sandbox_id)}"
            )

    async def resize(self, cols: int, rows: int) -> None:
        _check_size(cols, rows)
        if self._pty is None or self._closing:
            return
        try:
            await self._pty.resize(cols, rows)
        except self._unreachable as e:
            await self._fail(f"resizing the sandbox's terminal failed: {_why(e, self._sandbox_id)}")

    async def _fail(self, reason: str) -> None:
        self._failure = self._failure or reason
        await self.close()

    async def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._kill_now()
        if self._killing is not None:
            await asyncio.shield(self._killing)
        if self._waiter is None:
            self._finish(self._failure or "closed")
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._waiter), _CLOSE_GRACE)
        except TimeoutError:
            self._waiter.cancel()
            await asyncio.wait([self._waiter])

    def _kill_now(self) -> None:
        # A task, so a close cancelled mid-call still kills the pty: it has no time limit.
        if self._pty is not None and self._killing is None:
            self._killing = self._loop.create_task(self._kill(self._pty))

    async def _kill(self, pty: TerminalPty) -> None:
        try:
            await pty.kill()
        except Exception:
            log.exception("killing a sandbox terminal's pty failed; it may outlive its viewer")
