"""The subprocess seam: `Cli` runs git, gh and the agent CLI's one-shot commands."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from runtime.contract import AdapterUnavailable
from runtime.hostgit import hardened_env, host_environment

__all__ = [
    "GH_BIN",
    "GIT_BIN",
    "Cli",
    "CommandResult",
    "host_environment",
    "pid_alive",
    "probe_output",
    "wait_for_exit",
]

GIT_BIN: Final = "git"
GH_BIN: Final = "gh"


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Cli:
    """The only place that runs a subprocess.

    `create_subprocess_exec` with an argv list, never `shell=True` and never a string-interpolated
    command line (tech-stack.md decision 9), so nothing an untrusted author wrote can become a
    shell metacharacter. Each adapter binds its own agent binary; tests subclass and override
    `run`.
    """

    def __init__(self, *, agent_bin: str, git_bin: str = GIT_BIN) -> None:
        self.agent_bin = agent_bin
        self.git_bin = git_bin

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        """`inherit_host_env=False` makes `env` the program's whole environment. The default
        merges `env` over the host's, and a merge can only add: it cannot delete a host-exported
        `GH_TOKEN`."""
        if shutil.which(program) is None:
            raise AdapterUnavailable(f"`{program}` is not on PATH")
        argv = (program, *args)
        child_env = (
            dict(env or {})
            if not inherit_host_env
            else {**host_environment(), **env}
            if env
            else None
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                cwd=str(cwd) if cwd is not None else None,
            )
        except OSError as exc:
            raise AdapterUnavailable(f"cannot execute `{program}`: {exc}") from exc
        stdout, stderr = await process.communicate()
        return CommandResult(
            argv=argv,
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def git(
        self, *args: str, env: Mapping[str, str] | None = None, inherit_host_env: bool = True
    ) -> CommandResult:
        if inherit_host_env:
            # Passed only when off, so a `run` stand-in without it still serves every other git.
            return await self.run(self.git_bin, *args, env=hardened_env(env))
        return await self.run(self.git_bin, *args, env=hardened_env(env), inherit_host_env=False)


async def probe_output(argv: Sequence[str], *, env: Mapping[str, str], within: float) -> str:
    """A version probe's stdout, run with `env` whole and no stdin. A probe that cannot be run,
    does not answer `within` seconds or exits non-zero raises `AdapterUnavailable`: it says nothing
    about the binary, so it is no reason to refuse one."""
    shown = " ".join(argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            start_new_session=True,
        )
    except OSError as exc:
        raise AdapterUnavailable(f"cannot run `{shown}`: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), within)
    except TimeoutError:
        _kill_group(process.pid)
        await process.wait()
        raise AdapterUnavailable(f"`{shown}` did not answer in {within:g} s") from None
    except asyncio.CancelledError:
        _kill_group(process.pid)
        raise
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:400]
        raise AdapterUnavailable(f"`{shown}` exited {process.returncode}: {detail}")
    return stdout.decode(errors="replace")


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


async def wait_for_exit(pid: int, alive: Callable[[int], bool], *, within: float = 5.0) -> None:
    """Give a signalled process up to `within` seconds to exit, so what follows does not race
    its last writes."""
    for _ in range(round(within / 0.1)):
        if not alive(pid):
            return
        await asyncio.sleep(0.1)
