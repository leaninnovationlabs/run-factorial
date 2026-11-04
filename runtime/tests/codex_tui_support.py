"""Helpers the tests on the real codex TUI share (L1, `test_codex_tui_local.py`; L1r,
`test_codex_cli_interactive_local.py`; L2h, `test_codex_cli_interactive_docker.py`): the pinned
native binary the developer names, the mock API it talks to, the wrapper that points it there,
the turn that commits and pushes, and what its screen must never show. Imported by name, as
`host_session_support.py` is.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.codex_cli.cli import PINNED_VERSION

__all__ = [
    "CODEX",
    "DIALOGS",
    "PINNED_CODEX",
    "Mock",
    "codex_version",
    "commit_and_push",
    "mock_api",
    "wrapper",
]

CODEX = os.environ.get("FACTORIAL_TUI_CODEX", "")
MOCK = Path(__file__).with_name("mock_openai.py")
#: Codex's first-run screens and errors: any of them on screen means a seed is wrong.
DIALOGS = re.compile(
    r"Hooks need review|Sign in with ChatGPT|Provide your own API key|Do you trust|"
    r"Trust this folder|trust the contents|allow Codex to work|Update available|"
    r"no complete local package",
    re.IGNORECASE,
)


def codex_version(binary: str) -> str:
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, env={}
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip()


PINNED_CODEX = pytest.mark.skipif(
    not CODEX
    or shutil.which("tmux") is None
    or codex_version(CODEX) != f"codex-cli {PINNED_VERSION}",
    reason=f"set FACTORIAL_TUI_CODEX to a native codex-cli {PINNED_VERSION}, with tmux on PATH",
)


def wrapper(mock_url: str, codex: str = CODEX) -> str:
    """The pinned binary pointed at the mock. codex reads no base URL from its environment, so
    the wrapper passes it as config; `exec` keeps the pid, so the pane's pid is codex's."""
    return f'#!/bin/sh\nexec {codex} -c \'openai_base_url="{mock_url}/v1"\' "$@"\n'


def commit_and_push(served: str, branch: str, n: int) -> str:
    """What a human asks the agent to run: a commit in its checkout and a push over the network,
    both inside its sandbox. `served` is the origin over `git daemon`: a push to a `file://`
    origin would write outside the sandbox's writable roots, the worktree and the clone's
    `.git`."""
    agent = "-c user.name=agent -c user.email=agent@example.com"
    return (
        f"RUNME git checkout -q -B {branch} && echo {n} > spec.md && git add -A && "
        f"git {agent} commit -qm spec-{n} && git push -q {served} {branch} && echo PUSHED-{n}"
    )


@dataclass
class Mock:
    log: Path
    url: str

    def requests(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]

    def main(self) -> list[dict[str, Any]]:
        """The main loop's `POST /responses`: the ones offering tools. A title call has none."""
        return [
            r
            for r in self.requests()
            if r["method"] == "POST"
            and r["path"].endswith("/responses")
            and (r["body"] or {}).get("tools")
        ]

    def carrying(self, text: str) -> list[dict[str, Any]]:
        return [r for r in self.main() if text in json.dumps(r["body"])]


@contextlib.contextmanager
def mock_api() -> Iterator[Mock]:
    """`mock_openai.py` on a free port, its request log in a temp dir of its own, killed
    after."""
    root = Path(tempfile.mkdtemp(prefix="f243-oa-"))
    log = root / "requests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), str(log)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline())
        yield Mock(log=log, url=f"http://127.0.0.1:{port}")
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(root, ignore_errors=True)
