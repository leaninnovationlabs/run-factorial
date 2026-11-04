"""`codex login --device-auth`: starting the CLI in a private config home, and reading its
prompt. The flow around it (session ids, expiry, storing `auth.json`) belongs to the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Final, Protocol

from runtime.adapters.codex_cli.cli import ENV_CODEX_HOME
from runtime.contract import AdapterUnavailable

__all__ = ["LoginChild", "parse_device_prompt", "start_device_login"]

#: Stripped before either search. The CLI colours its prompt (`\x1b[94m…\x1b[0m`, observed on
#: 0.153.2): the reset is not whitespace, so it would ride along inside the URL, and the escape
#: ends in a letter, so `\b` never fires before the code.
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL: Final = re.compile(r"https://[^\s<>\"']+")
_USER_CODE: Final = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b")


class LoginChild(Protocol):
    """A `codex login --device-auth` process, as the login flow sees one. Tests supply a fake.

    `output` is everything the child has printed so far, both streams merged: the prompt with
    the URL and the one-time code is parsed out of it, and its tail is the failure detail.
    """

    @property
    def output(self) -> str: ...

    @property
    def returncode(self) -> int | None: ...

    def kill(self) -> None: ...


class _DeviceLogin:
    """`LoginChild` over an `asyncio` subprocess.

    Output is drained by a task from the start: a device-auth login prints its prompt, then
    blocks until the browser side completes, and a pipe nobody reads would stall it.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._chunks: list[str] = []
        self._pump = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self._process.stdout is not None
        while chunk := await self._process.stdout.read(4096):
            self._chunks.append(chunk.decode(errors="replace"))
        await self._process.wait()

    @property
    def output(self) -> str:
        return "".join(self._chunks)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()


async def start_device_login(codex_bin: str, home: Path) -> LoginChild:
    """Spawn `codex login --device-auth` with a private config home. argv list, never a shell
    (tech-stack decision 9); stdin is closed so a CLI that decides to prompt fails instead of
    waiting for a keyboard nobody has."""
    try:
        process = await asyncio.create_subprocess_exec(
            codex_bin,
            "login",
            "--device-auth",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, ENV_CODEX_HOME: str(home)},
        )
    except OSError as exc:
        raise AdapterUnavailable(f"cannot execute `{codex_bin}`: {exc}") from exc
    return _DeviceLogin(process)


def parse_device_prompt(output: str) -> tuple[str, str] | None:
    """`(verification_url, user_code)` once the CLI has printed both, else None."""
    plain = _ANSI.sub("", output)
    url = _URL.search(plain)
    code = _USER_CODE.search(plain)
    return (url.group(0), code.group(0)) if url and code else None
