"""Whether codex's `workspace-write` sandbox can start on this machine at all.

On Linux that sandbox is bubblewrap, which needs a user namespace, and a container under
Docker's default seccomp profile is refused one. Every agent there would die on its first
command with an opaque bwrap error, so the CLI's own `sandbox` subcommand is asked first, and its
refusal is turned into one that names the fix.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Final

from runtime.adapters._cli.process import Cli
from runtime.adapters.codex_cli.cli import (
    CONFIG_SANDBOX_MODE,
    ENV_CODEX_HOME,
    FLAG_CONFIG,
    SUBCMD_SANDBOX,
    VALUE_SANDBOX_WORKSPACE,
)

SCRATCH_PREFIX: Final = ".sandbox-check-"
DETAIL_LIMIT: Final = 400


async def sandbox_refusal(cli: Cli, root: Path) -> str | None:
    """None when the sandbox starts, otherwise why it cannot and what fixes it.

    The probe runs `true`, not an agent: an empty CODEX_HOME of its own, and no secret."""
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    # Under `local_root`, not $TMPDIR: codex warns about its helper binaries when CODEX_HOME is
    # under /tmp.
    scratch = await asyncio.to_thread(tempfile.mkdtemp, prefix=SCRATCH_PREFIX, dir=root)
    try:
        result = await cli.run(
            cli.agent_bin,
            SUBCMD_SANDBOX,
            FLAG_CONFIG,
            f'{CONFIG_SANDBOX_MODE}="{VALUE_SANDBOX_WORKSPACE}"',
            "--",
            "true",
            env={ENV_CODEX_HOME: scratch},
            cwd=Path(scratch),
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
    if result.ok:
        return None
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    detail = lines[0][:DETAIL_LIMIT] if lines else f"`codex sandbox` exited {result.returncode}"
    detail = detail.rstrip(".")
    # Tech-stack decision 52 says why a container needs the profile, and that Fargate cannot
    # apply one.
    return (
        f"Codex's workspace-write sandbox can't start on this machine: {detail}. Run the "
        "container with a seccomp profile: /etc/factorial/seccomp.json in the image, "
        "docker/seccomp.json in a checkout (--security-opt seccomp=<that file>). "
        "See https://www.runfactorial.com/docs/customize-setup/#first-runtime. "
        "Fargate can't apply one."
    )
