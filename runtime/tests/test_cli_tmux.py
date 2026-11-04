"""The host's tmux seam and launch script (`_cli/tmux.py`, `_cli/launch.py`), on a real tmux.

Roots come from `mkdtemp(dir="/tmp")`: pytest's tmp_path on macOS is too long for a socket.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from runtime.adapters._cli import launch, tmux
from runtime.adapters._cli.tmux import PaneState, TmuxServer
from runtime.contract import TerminalTarget
from runtime.terminal import HostTmuxRelay

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

RUN_ID = "GH-12-plan-agent-0a1b2c3d"
#: Made at run time, so no command line that holds this file's text can hold it.
MARKER = f"sk-{secrets.token_hex(8)}"
#: Everything sh would expand or end a quote on.
SECRET = f"{MARKER}'\"$(id)`id`\n\\-end"


class Refused(Exception):
    pass


@pytest.fixture
def root() -> Iterator[Path]:
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix="f243t-", dir="/tmp")))
    yield path
    for socket in (path / tmux.SOCKETS_DIRNAME).glob("*"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def server(root: Path, conf: str = tmux.SESSION_CONF_FILE) -> TmuxServer:
    binary = shutil.which("tmux")
    assert binary is not None
    tmux.prepare(root)
    home = root / "home"
    home.mkdir(exist_ok=True)
    socket = tmux.socket_path(root, RUN_ID, "s", Refused)
    return TmuxServer(binary, socket, root / conf, home, "run")


async def started(srv: TmuxServer, argv: list[str], cwd: Path) -> None:
    result = await srv.start(cwd, argv)
    assert result.ok, result.stderr


async def until(check: Callable[[], Awaitable[bool]], what: str) -> None:
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never: {what}")


async def reaped(srv: TmuxServer) -> bool:
    """tmux can show the pane dead before it has the exit status."""
    pane = await srv.pane()
    return pane is not None and pane.dead and pane.status is not None


def test_a_session_socket_fits_sun_path_and_the_gateway_charset_or_is_refused(
    root: Path,
) -> None:
    session = tmux.socket_path(root, RUN_ID, "s", Refused)
    shell = tmux.socket_path(root, RUN_ID, "i", Refused)

    assert session != shell
    for socket in (session, shell):
        assert len(socket.encode()) <= tmux.MAX_SOCKET_BYTES
        assert re.fullmatch(r"[A-Za-z0-9_./-]+", socket)
        TerminalTarget(substrate="host", socket=socket, session="run")
    with pytest.raises(Refused):
        tmux.socket_path(Path("/tmp/" + "x" * 85), RUN_ID, "s", Refused)
    with pytest.raises(Refused):
        tmux.socket_path(Path("/tmp/a:b"), RUN_ID, "s", Refused)


@needs_tmux
async def test_the_session_server_loads_the_lockdown_and_keeps_a_dead_pane(root: Path) -> None:
    srv = server(root)
    await started(srv, ["/bin/sh", "-c", "exit 3"], root)

    await until(lambda: reaped(srv), "the pane died and stayed")
    pane = await srv.pane()
    assert pane is not None and (pane.dead, pane.status) == (True, 3)
    assert (await srv.run("show-options", "-gv", "prefix")).stdout.strip() == "None"
    await srv.kill()
    assert await srv.pane() is None, "no server is no pane, never a live one"

    shell = server(root, tmux.CONF_FILE)
    await started(shell, ["/bin/sh", "-c", "sleep 30"], root)
    assert (await shell.run("show-options", "-gwv", "remain-on-exit")).stdout.strip() == "off"
    assert isinstance(await shell.pane(), PaneState)


async def launched(root: Path, env: dict[str, str]) -> tuple[TmuxServer, Path, dict[str, str]]:
    """Launch a pane process that dumps its environment and stays up, as the CLI would. A shell,
    not python, which adds to its own environment on macOS."""
    srv = server(root)
    launch.write_launch_files(root)
    env_file = root / launch.ENV_FILE
    launch.write_env_file(env_file, env)
    assert env_file.stat().st_mode & 0o777 == 0o600
    out = root / "env.out"
    dump = '/usr/bin/env -0 > "$1.tmp" && /bin/mv "$1.tmp" "$1"; exec /bin/sleep 60'
    argv = launch.launch_argv(root, env_file, ["/bin/sh", "-c", dump, "sh", str(out)])
    assert not [arg for arg in argv if MARKER in arg]
    await started(srv, argv, root)
    await until(lambda: asyncio.to_thread(out.exists), "the pane process wrote its environment")
    pairs = out.read_bytes().decode().split("\0")[:-1]
    return srv, env_file, dict(pair.split("=", 1) for pair in pairs)


@needs_tmux
async def test_launch_sh_gives_the_pane_exactly_the_env_file_and_deletes_it(root: Path) -> None:
    env = {"FACTORIAL_SECRET": SECRET, "TERM": "xterm-256color", "EMPTY": ""}
    srv, env_file, seen = await launched(root, env)

    assert {key: seen[key] for key in env} == env
    # The dumping shell's own bookkeeping; nothing of tmux's or its server's (PATH, LANG, HOME).
    assert set(seen) - set(env) <= {"PWD", "SHLVL", "_"}
    assert seen.get("PWD", str(root)) == str(root)
    assert not env_file.exists()
    ps = ["ps", "-axww", "-o", "args="]
    processes = await asyncio.to_thread(subprocess.run, ps, capture_output=True, text=True)
    assert MARKER not in processes.stdout
    assert MARKER not in (await srv.run("show-environment", "-g")).stdout
    pane = await srv.pane()
    assert pane is not None and not pane.dead


@needs_tmux
async def test_an_env_key_sh_cannot_export_is_dropped_not_fatal(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ran = root / "ran"
    env = {
        "a.b": SECRET,
        "BASH_FUNC_x%%": SECRET,
        "SHELLOPTS": "braceexpand",
        # bash evaluates an OPTIND assignment as arithmetic, a subscript's command substitution
        # included; OPTERR is one bash sets, so `set -u` does not stop it first.
        "OPTIND": f"OPTERR[$(/usr/bin/touch {ran})]",
        "KEPT": "yes",
    }
    try:
        with caplog.at_level(logging.DEBUG):
            _, _, seen = await launched(root, env)
    finally:
        assert not ran.exists(), "a value ran as a command"
    assert seen["KEPT"] == "yes"
    assert not {"a.b", "BASH_FUNC_x%%"} & set(seen)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "dropped 4 " in logged
    assert not [word for word in ("a.b", "BASH_FUNC", "SHELLOPTS", MARKER) if word in logged]


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/dash"])
def test_an_env_key_named_like_a_script_variable_reaches_the_cli_and_the_file_still_goes(
    root: Path, shell: str
) -> None:
    if not Path(shell).exists():
        pytest.skip(f"no {shell}")
    victim = root / "victim"
    victim.write_text("kept")
    launch.write_launch_files(root)
    env_file = root / launch.ENV_FILE
    launch.write_env_file(env_file, {"f": str(victim), "K": "v"})
    argv = launch.launch_argv(root, env_file, ["/usr/bin/env"])
    argv[argv.index("/bin/sh")] = shell

    done = subprocess.run(argv, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert f"f={victim}\n" in done.stdout
    assert not env_file.exists()
    assert victim.read_text() == "kept"


def test_rewriting_the_launch_files_never_breaks_a_launch_reading_them(root: Path) -> None:
    """Every launch rewrites the shared `bin/launch.sh` while other runs' panes start from it."""
    launch.write_launch_files(root)
    stop = threading.Event()

    def rewrite() -> None:
        while not stop.is_set():
            launch.write_launch_files(root)

    rewriter = threading.Thread(target=rewrite)
    rewriter.start()
    failed: list[tuple[int, str, bool]] = []
    try:
        for i in range(200):
            env_file = root / f"{i}.env"
            launch.write_env_file(env_file, {"K": "v"})
            argv = launch.launch_argv(root, env_file, ["/bin/echo", "ran"])
            done = subprocess.run(argv, capture_output=True, text=True)
            if (done.returncode, done.stdout, env_file.exists()) != (0, "ran\n", False):
                failed.append((done.returncode, done.stdout, env_file.exists()))
    finally:
        stop.set()
        rewriter.join()
    assert not failed, f"{len(failed)} of 200 launches failed: {failed[:3]}"

    victim = root / "victim"
    victim.write_text("kept")
    script = root / launch.BIN_DIRNAME / launch.LAUNCH_FILE
    script.unlink()
    script.symlink_to(victim)
    launch.write_launch_files(root)
    assert victim.read_text() == "kept" and victim.stat().st_mode & 0o777 != 0o700
    assert not script.is_symlink() and script.stat().st_mode & 0o777 == 0o700


@needs_tmux
async def test_the_server_is_in_neither_the_starting_process_group_nor_its_session(
    root: Path,
) -> None:
    """Why a signal s6 sends the worker's process group or session cannot reach it."""
    srv = server(root)
    await started(srv, ["/bin/sh", "-c", "sleep 30"], root)
    pid = int((await srv.display("#{pid}")).stdout)

    assert os.getsid(pid) != os.getsid(0)
    assert os.getpgid(pid) != os.getpgid(0)


@needs_tmux
async def test_a_live_server_without_the_session_has_no_pane(root: Path) -> None:
    """tmux then prints the format with every field empty, and exits 0."""
    srv = server(root)
    await started(srv, ["/bin/sh", "-c", "sleep 30"], root)
    assert (await srv.run("rename-session", "-t", f"={srv.session}", "other")).ok

    assert await srv.pane() is None


@needs_tmux
async def test_two_pastes_at_once_each_type_their_own_text(root: Path) -> None:
    srv = server(root)
    await started(srv, ["/bin/sh", "-c", "stty -echo; exec /bin/cat"], root)
    await until(lambda: srv.has_session(), "the pane started")

    typed = await asyncio.gather(*(srv.type_text(f"line-{n}\r") for n in range(4)))

    assert typed == [True] * 4

    async def each_once() -> bool:
        screen = await srv.capture() or ""
        return all(screen.count(f"line-{n}") == 1 for n in range(4))

    await until(each_once, "every line typed once")


@needs_tmux
async def test_typing_reaches_the_pane_with_only_a_read_only_client_attached(root: Path) -> None:
    srv = server(root)
    await started(srv, ["/bin/cat"], root)
    target = TerminalTarget(substrate="host", socket=srv.socket, session=srv.session)
    viewer = await HostTmuxRelay().attach(
        target, writable=False, cols=80, rows=24, on_output=lambda _: None
    )
    try:
        assert await srv.type_text("-hello\r")

        async def echoed() -> bool:
            # The tty's echo, then cat's line.
            return (await srv.capture() or "").count("-hello") == 2

        await until(echoed, "the typed line came back")
    finally:
        await viewer.close()
