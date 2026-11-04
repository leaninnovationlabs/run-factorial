"""The processes a host run leaves outside its process group, ended by its environment.

claude's Bash tool starts every command in a session of its own, so a job the agent backgrounds
(`nohup … &`) is in no group that a hangup or `killpg` reaches. It outlives the run, holding the
run's environment: the model key and the GitHub token. Every process of a run inherits one value
no other live run's holds, its `CLAUDE_CONFIG_DIR`, so at every end of a run `end_marked` ends
each process of the worker's user whose environment holds exactly that `KEY=VALUE` entry. A
rework continues in the config home of the run it continues, so a home is swept for an ended run
only while no rework continues it (`session.ended_config_home`).

Linux reads `/proc/<pid>/environ`. macOS lists processes with `ps` and reads each environment
with `sysctl(KERN_PROCARGS2)`, which hides the environment of Apple's own binaries (`/bin/sleep`,
`/bin/sh`) even from their user, so such a job escapes there. Elsewhere nothing is ended.

Best effort, never containment: a process that scrubs or rewrites its own environment escapes,
as any process of the same user could (decision 16, design §7.14). The worker and its ancestors
are never signalled, nor pid 0 or 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import signal
import struct
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Final

__all__ = ["end_marked", "end_marked_any", "marked_any"]

log = logging.getLogger(__name__)

#: SIGTERM, then this long for the processes to go before SIGKILL.
GRACE: Final = 1.0
_PROC: Final = Path("/proc")
_CTL_KERN: Final = 1
_KERN_PROCARGS2: Final = 49
_ARGS_MAX: Final = 1 << 20

#: pid -> (parent pid, its environment entries); None where the environment cannot be read.
_Table = dict[int, tuple[int, frozenset[bytes] | None]]

_libc: ctypes.CDLL | None = None
if sys.platform == "darwin":
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]


def _linux_table() -> _Table:
    uid = os.getuid()
    table: _Table = {}
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            stat = (entry / "stat").read_bytes()
            parent = int(stat[stat.rindex(b")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        try:
            env: frozenset[bytes] | None = frozenset((entry / "environ").read_bytes().split(b"\0"))
        except OSError:
            env = None
        table[int(entry.name)] = (parent, env)
    return table


def _macos_environment(pid: int) -> frozenset[bytes] | None:
    """`KERN_PROCARGS2` holds argc, the exec path, argv and then the environment, each entry
    NUL-terminated, so an entry is matched whole."""
    if _libc is None:
        return None
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(_ARGS_MAX)
    buffer = ctypes.create_string_buffer(_ARGS_MAX)
    if _libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    raw = buffer.raw[: size.value]
    if len(raw) < 4:
        return None
    (argc,) = struct.unpack("=i", raw[:4])
    rest = raw[4:]
    end = rest.find(b"\0")
    if end < 0:
        return None
    rest = rest[end:].lstrip(b"\0")
    entries = rest.split(b"\0")
    env = entries[argc:]
    if b"" in env:
        env = env[: env.index(b"")]
    return frozenset(env)


def _macos_table() -> _Table:
    shown = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid=,uid="],
        capture_output=True,
        text=True,
        env={"PATH": "/bin:/usr/bin", "LANG": "C"},
        timeout=30,
    )
    uid = os.getuid()
    table: _Table = {}
    for line in shown.stdout.splitlines():
        words = line.split()
        if len(words) != 3 or not all(word.isdigit() for word in words):
            continue
        pid, parent, owner = map(int, words)
        if owner == uid:
            table[pid] = (parent, _macos_environment(pid))
    return table


def _table() -> _Table:
    if sys.platform.startswith("linux"):
        return _linux_table()
    if sys.platform == "darwin":
        return _macos_table()
    return {}


def _spared(table: _Table) -> set[int]:
    """Pid 0, pid 1, the worker and every process it descends from."""
    spared = {0, 1}
    pid = os.getpid()
    while pid not in spared:
        spared.add(pid)
        pid = table.get(pid, (1, None))[0]
    return spared


def marked_any(key: str, values: Iterable[str]) -> dict[str, set[int]]:
    """For each of `values`, the pids of the worker's user whose environment holds `key=value`
    exactly, but for the worker and its ancestors: one read of the process table for them all."""
    table = _table()
    spared = _spared(table)
    found: dict[str, set[int]] = {}
    for value in values:
        entry = f"{key}={value}".encode()
        found[value] = {p for p, (_, env) in table.items() if env and entry in env} - spared
    return found


def _signal(pids: set[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def _union(found: dict[str, set[int]]) -> set[int]:
    return set().union(*found.values())


async def end_marked_any(key: str, values: Iterable[str], *, grace: float = GRACE) -> int:
    """SIGTERM every process `marked_any` finds, SIGKILL those still marked after `grace`
    seconds, and return how many there were. Never raises: an end of a run must go on whatever
    fails."""
    values = list(values)
    if not values:
        return 0
    try:
        found = _union(await asyncio.to_thread(marked_any, key, values))
    except Exception:
        log.exception("reading the process table for what runs left behind failed")
        return 0
    if not found:
        return 0
    _signal(found, signal.SIGTERM)
    left = found
    for _ in range(max(1, round(grace / 0.1))):
        await asyncio.sleep(0.1)
        try:
            # Asked again rather than by pid alone: a pid that exited may be anyone's by now.
            left = found & _union(await asyncio.to_thread(marked_any, key, values))
        except Exception:
            log.exception("reading the process table again failed; killing what was found")
            left = found
            break
        if not left:
            break
    _signal(left, signal.SIGKILL)
    log.warning("ended %d process(es) a run left outside its process group", len(found))
    return len(found)


async def end_marked(key: str, value: str, *, grace: float = GRACE) -> int:
    """`end_marked_any` for one run."""
    return await end_marked_any(key, [value], grace=grace)
