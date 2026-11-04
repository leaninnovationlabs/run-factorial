"""`end_marked` ends the processes whose environment holds a run's marker, and no other.

The processes are real, each in a session of its own as the claude Bash tool starts a command,
and run this interpreter rather than an Apple binary, whose environment macOS hides.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pytest

from runtime.adapters._cli import escaped
from runtime.adapters._cli.escaped import end_marked, marked_any

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") and sys.platform != "darwin",
    reason="reads process environments on Linux and macOS only",
)

KEY = "F243_ESCAPED_TEST_MARK"  # gitleaks:allow
#: Ignores the polite signal, so only the escalation ends it, and says when its handler is in.
STUBBORN = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)"
)


@pytest.fixture
def started() -> Iterator[list[subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        process.kill()
        process.wait()


def start(processes: list[subprocess.Popen[str]], env: Mapping[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", STUBBORN],
        env={"PATH": "/usr/bin:/bin", **env},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    processes.append(process)
    assert process.stdout is not None and process.stdout.readline() == "ready\n"
    return process


async def test_it_ends_the_marked_process_and_spares_look_alikes(
    tmp_path: Path, started: list[subprocess.Popen[str]]
) -> None:
    value = str(tmp_path / "config")
    target = start(started, {KEY: value})
    decoys = [
        start(started, {KEY: f"{value}2"}),
        start(started, {f"{KEY}_OTHER": value}),
        start(started, {"NOTE": f"x {KEY}={value}"}),
        start(started, {}),
    ]

    assert await end_marked(KEY, value, grace=0.5) == 1

    assert target.wait(timeout=5) == -signal.SIGKILL, "it ignored SIGTERM, so it was killed"
    assert [decoy.poll() for decoy in decoys] == [None] * len(decoys)


async def test_nothing_marked_is_nothing_ended(tmp_path: Path) -> None:
    assert await end_marked(KEY, str(tmp_path / "nobody")) == 0


def test_one_read_of_the_table_answers_for_every_home(
    tmp_path: Path, started: list[subprocess.Popen[str]]
) -> None:
    first, second = str(tmp_path / "a" / "config"), str(tmp_path / "b" / "config")
    a, b = start(started, {KEY: first}), start(started, {KEY: second})

    assert marked_any(KEY, [first, second, f"{first}x"]) == {
        first: {a.pid},
        second: {b.pid},
        f"{first}x": set(),
    }


async def test_a_table_that_cannot_be_read_again_still_gets_the_kill(
    tmp_path: Path, started: list[subprocess.Popen[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = str(tmp_path / "config")
    target = start(started, {KEY: value})
    reads = [{value: {target.pid}}]

    def flaky(key: str, values: Iterable[str]) -> dict[str, set[int]]:
        if not reads:
            raise OSError("the table is gone")
        return reads.pop()

    monkeypatch.setattr(escaped, "marked_any", flaky)

    assert await escaped.end_marked(KEY, value, grace=0.5) == 1
    assert target.wait(timeout=5) == -signal.SIGKILL
