"""The `codex exec` surface the assistant's chat turns run on (decision 63), and its child.

Agent runs are TUI sessions (decision 56) and never reach this module; a chat turn is one
`codex exec --json` child whose event stream `events.py` maps. Every launch is an argv list,
never a shell (decision 9), with the environment `env.py` constructs passed whole.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters.codex_cli.auth import ENV_API_KEY
from runtime.adapters.codex_cli.cli import ENV_CODEX_HOME
from runtime.contract import AdapterUnavailable, SpawnError, SpawnReason
from runtime.hostgit import host_environment

__all__ = [
    "FLAG_IMAGE",
    "FLAG_JSON",
    "FLAG_SKIP_GIT_REPO_CHECK",
    "STDIN_PROMPT",
    "SUBCMD_EXEC",
    "ExecChild",
    "api_key_login",
    "launch",
]

#: Non-interactive mode is a subcommand; `exec resume <id>` continues a thread.
SUBCMD_EXEC: Final = "exec"
#: `codex login --with-api-key` reads the key from STDIN and writes auth into `CODEX_HOME`.
SUBCMD_LOGIN: Final = "login"
FLAG_WITH_API_KEY: Final = "--with-api-key"
#: JSONL events on stdout, which `runtime.adapters.codex_cli.events` parses.
FLAG_JSON: Final = "--json"
#: `exec` exits 1 outside a git checkout without it, on `exec` and `exec resume` both.
FLAG_SKIP_GIT_REPO_CHECK: Final = "--skip-git-repo-check"
#: An image for the first prompt. Written `--image=<path>`, one word per file: the flag takes
#: every following word as another file, and the next word is the prompt's `-`.
FLAG_IMAGE: Final = "--image"
#: The prompt argument that means "read stdin": argv is neither private (`ps`) nor large enough.
STDIN_PROMPT: Final = "-"


async def api_key_login(codex_bin: str, config_dir: Path, api_key: str) -> None:
    """`codex login --with-api-key` into the turn's home, the key on STDIN, never argv."""
    process = await _start(
        codex_bin,
        (SUBCMD_LOGIN, FLAG_WITH_API_KEY),
        cwd=None,
        env={**host_environment(), ENV_CODEX_HOME: str(config_dir)},
    )
    _, stderr = await process.communicate(api_key.encode())
    if process.returncode:
        raise SpawnError(
            f"`codex login {FLAG_WITH_API_KEY}` exited {process.returncode}: "
            f"{stderr.decode(errors='replace').strip()[:400]}. The stored {ENV_API_KEY} was "
            "refused",
            reason=SpawnReason.CREDENTIAL,
        )


async def launch(
    codex_bin: str, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin_text: str
) -> ExecChild:
    """Start the turn and return at once. `env` is the child's whole environment."""
    process = await _start(codex_bin, argv, cwd=cwd, env=env)
    assert process.stdin is not None
    # A child that exits before reading its prompt closes the pipe under the write; its exit
    # code and stderr say why.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(stdin_text.encode())
        await process.stdin.drain()
    process.stdin.close()
    return ExecChild(process)


async def _start(
    codex_bin: str, argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str]
) -> asyncio.subprocess.Process:
    if shutil.which(codex_bin) is None:
        raise AdapterUnavailable(f"`{codex_bin}` is not on PATH")
    try:
        return await asyncio.create_subprocess_exec(
            codex_bin,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=None if cwd is None else str(cwd),
            env=dict(env),
            # Its own process group, so a cancel reaches the helpers the CLI starts.
            start_new_session=True,
        )
    except OSError as exc:
        raise AdapterUnavailable(f"cannot execute `{codex_bin}`: {exc}") from exc


class ExecChild:
    """One launched `codex exec`."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def stdout_lines(self) -> AsyncIterator[str]:
        assert self._process.stdout is not None
        # `readline` raises on a line over the 64 KiB limit, which one large event exceeds.
        buffer = b""
        while chunk := await self._process.stdout.read(65536):
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                yield line.decode(errors="replace")
        if buffer:
            yield buffer.decode(errors="replace")

    async def stderr_text(self) -> str:
        assert self._process.stderr is not None
        return (await self._process.stderr.read()).decode(errors="replace")

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
