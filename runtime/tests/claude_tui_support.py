"""Helpers the tests on the real claude TUI share (L1, `test_claude_tui_local.py`; L1r,
`test_claude_cli_interactive_local.py`): the pinned CLI the developer names, the mock API it
talks to, and what its screen must never show. Imported by name, as `host_session_support.py` is.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.claude_cli.cli import INPUT_MARK, PINNED_VERSION

__all__ = ["CLAUDE", "DIALOGS", "INPUT_MARK", "PINNED_CLI", "Mock", "cli_version", "mock_api"]

CLAUDE = os.environ.get("FACTORIAL_TUI_CLAUDE", "")
MOCK = Path(__file__).with_name("mock_anthropic.py")
#: The CLI's first-run dialogs: any of them on screen means a seed is wrong.
DIALOGS = re.compile(
    r"trust this folder|Do you want to use this API key|Bypass Permissions mode|"
    r"Choose the text style|Select login method|Please run /login|security notes",
    re.IGNORECASE,
)


def cli_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout.split() or [""])[0]


PINNED_CLI = pytest.mark.skipif(
    not CLAUDE or shutil.which("tmux") is None or cli_version(CLAUDE) != PINNED_VERSION,
    reason=f"set FACTORIAL_TUI_CLAUDE to a claude {PINNED_VERSION} binary, with tmux on PATH",
)


@dataclass
class Mock:
    log: Path
    url: str

    def requests(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]

    def messages(self) -> list[dict[str, Any]]:
        """The main loop's `/v1/messages` calls: the ones offering tools. A title call has none."""
        return [
            r
            for r in self.requests()
            if r["path"].startswith("/v1/messages")
            and "count_tokens" not in r["path"]
            and (r["body"] or {}).get("tools")
        ]


@contextmanager
def mock_api() -> Iterator[Mock]:
    """`mock_anthropic.py` on a free port, its request log in a temp dir of its own, killed
    after."""
    root = Path(tempfile.mkdtemp(prefix="f243-mock-"))
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
