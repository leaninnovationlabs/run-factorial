"""L2h for `codex_cli`: its runs in a container of the local image, under s6 as the image's worker
runs, on the image's own codex and tmux against `mock_openai.py` (decisions 52 and 56). With the
seccomp profile `docker/seccomp.json`, as `make up` starts the image, a session edits,
commits and pushes from inside codex's sandbox on a human's turn, after a worker-only restart
(`s6-svc -r`) whose new worker takes the session back, and then finishes. Without the profile,
under Docker's default one, the spawn is refused for good (`SpawnReason.CONFIG`) with the
message that names the profile, and leaves nothing behind.

Opt-in, and never in CI: it needs Docker and an image with s6-overlay, tmux, git and the pinned
codex. It takes under a minute.

    make image && FACTORIAL_TUI_DOCKER_IMAGE=factorial:local \\
      uv run pytest -c pyproject.toml runtime/tests/test_codex_cli_interactive_docker.py \\
      -q -p no:cacheprovider

The worker is `codex_session_driver.py`, a longrun in a scratch `s6-svscan` run as `factorial`,
and the codex it runs is `codex_tui_support.wrapper`, which points the image's binary at the mock.
The image gives codex, tmux, s6 and the Python environment; the code under test is this
checkout's `runtime/`, copied over the image's at creation, so an image built before the last
change still tests that change. The container has no network: a bare repo in it stands for
GitHub, reached by the driver's `--origin` for the worker's git, and over a `git daemon` on
loopback for the agent's push, because a push to a `file://` origin would write outside the
sandbox. The run's key is a made-up one only the mock sees.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from codex_tui_support import DIALOGS, Mock, commit_and_push, wrapper
from host_session_support import REPO

from runtime.adapters._cli import tmux
from runtime.adapters.codex_cli.cli import INPUT_MARK, PINNED_VERSION
from runtime.contract import SpawnReason

IMAGE = os.environ.get("FACTORIAL_TUI_DOCKER_IMAGE", "")

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason=(
        "set FACTORIAL_TUI_DOCKER_IMAGE to an image with s6-overlay, tmux 3.2 or later and codex "
        f"{PINNED_VERSION}, such as `make image`'s factorial:local"
    ),
)

#: Every container this module makes carries it, so a teardown finds them all.
LABEL = "factorial-l2h-codex"
RUNTIME = Path(__file__).parents[1]
PROFILE = RUNTIME.parent / "docker" / "seccomp.json"
ROOT = Path("/data/runs/codex_cli")
CODEX = "/opt/codex/bin/codex"
WRAPPER = "/usr/local/bin/codex-via-mock"
MOCK_URL = "http://127.0.0.1:18080"
ORIGIN = "/srv/repo.git"
SERVED = "git://127.0.0.1:9418/repo.git"
BRANCH = "spec/12-l2h"
RECORD = "/data/driver.run"
FINISH = "/data/finish"
DRIVER_LOG = "/data/driver.log"
DRIVER_ERR = "/data/driver.err"
SCAN = "/tmp/scan"
KEY = "sk-proj-l2h-made-up-key-0123456789abcdef"
WAIT = 240.0
SETTLE = 0.5
ENDED = ("succeeded", "failed", "timeout", "cancelled", "interrupted")
RUN_STATUSES = {"queued", "running", "idle", *ENDED}
#: As bwrap prints it in the image, less the full stop the refusal drops.
BWRAP = (
    "bwrap: No permissions to create a new namespace, likely because the kernel does not allow "
    "non-privileged user namespaces. On e.g. debian this can be enabled with "
    "'sysctl kernel.unprivileged_userns_clone=1'"
)
#: The image's s6-overlay keeps its programs here, off the PATH a `docker exec` has.
S6 = "/command"

PREPARE = f"""set -eu
git init -q --bare -b main {ORIGIN}
seed=$(mktemp -d)
git -C "$seed" init -q -b main
echo widgets > "$seed/README.md"
git -C "$seed" add README.md
git -C "$seed" -c user.name=l2h -c user.email=l2h@example.com commit -q -m start
git -C "$seed" push -q {ORIGIN} main
install -d -o factorial -g factorial {ROOT} {SCAN} {SCAN}/driver
cat > {WRAPPER} <<'EOF'
{wrapper(MOCK_URL, codex=CODEX)}EOF
chmod 0755 {WRAPPER}
chown -R factorial:factorial {SCAN} /data {ORIGIN}
"""

DRIVER_RUN = """#!/bin/sh
exec 2>>{err}
cd /app
export HOME=/home/factorial LANG=C.UTF-8 PATH=/app/.venv/bin:/usr/bin:/bin DRIVER_API_KEY={key}
exec {command} >>{log}
"""


def docker(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=120)
    if check and done.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])} failed: {done.stderr.decode()[-800:]}")
    return done.stdout.decode()


def sh(name: str, script: str, *, user: str = "root", check: bool = True) -> str:
    return docker("exec", "-u", user, name, "sh", "-c", script, check=check)


def copy_runtime(name: str) -> None:
    """This checkout's adapter code and the test programs the container runs, over the image's."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:

        def plain(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if "__pycache__" in info.name:
                return None
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            return info

        archive.add(RUNTIME / "src" / "runtime", "app/runtime/src/runtime", filter=plain)
        for program in ("codex_session_driver.py", "host_session_support.py", "mock_openai.py"):
            archive.add(RUNTIME / "tests" / program, f"app/runtime/tests/{program}", filter=plain)
    sh(name, "rm -rf /app/runtime/src/runtime")
    docker("exec", "-i", name, "tar", "-x", "-C", "/", stdin=buffer.getvalue())


def until(name: str, what: str, check: Callable[[], bool], within: float = WAIT) -> None:
    """Fails with the tail of what the driver printed and logged."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    tails = sh(name, f"tail -n 30 {DRIVER_LOG} {DRIVER_ERR} 2>&1", check=False)
    raise AssertionError(f"never: {what}\n{tails}")


def start_mock(name: str) -> None:
    sh(
        name,
        "setsid python3 /app/runtime/tests/mock_openai.py /tmp/mock.jsonl 18080 "
        ">/tmp/mock.port 2>>/tmp/mock.err </dev/null &",
    )
    until(name, "the mock", lambda: sh(name, "cat /tmp/mock.port", check=False).strip() == "18080")


def start_daemon(name: str) -> None:
    """The origin over `git daemon` on loopback, for the agent's push."""
    docker(
        "exec", "-d", "-u", "factorial", name, "git", "daemon", "--reuseaddr",
        "--listen=127.0.0.1", "--port=9418", "--base-path=/srv", "--export-all",
        "--enable=receive-pack", "/srv",
    )  # fmt: skip
    served = f"git ls-remote -q {SERVED} >/dev/null 2>&1 && echo up"
    until(name, "the git daemon", lambda: sh(name, served, check=False).strip() == "up")


@contextmanager
def container(*options: str) -> Iterator[str]:
    """A container of the image idling under tini, its runtime this checkout's, with the mock
    and the origin served."""
    name = f"{LABEL}-{uuid.uuid4().hex[:8]}"
    try:
        docker(
            "run", "-d", "--init", "--network", "none", "--name", name, f"--label={LABEL}=1",
            *options, "--entrypoint", "sleep", IMAGE, "infinity",
        )  # fmt: skip
        shown = sh(name, f"{CODEX} --version").strip()
        assert shown == f"codex-cli {PINNED_VERSION}", (
            f"the image's codex is {shown!r}, not the pin"
        )
        copy_runtime(name)
        sh(name, PREPARE)
        start_mock(name)
        start_daemon(name)
        yield name
    finally:
        docker("rm", "-fv", name, check=False)


@pytest.fixture
def box() -> Iterator[str]:
    """Started with the profile, as `make up` starts the image."""
    with container("--security-opt", f"seccomp={PROFILE}") as name:
        yield name


@pytest.fixture
def unprofiled_box() -> Iterator[str]:
    """Started as a plain `docker run` starts the image, under Docker's default profile."""
    with container() as name:
        yield name


def start_driver(name: str, *options: str) -> None:
    """The driver as an s6 longrun, its run script given `options` after the fixed ones."""
    command = shlex.join(
        [
            "python", "/app/runtime/tests/codex_session_driver.py", "serve", str(ROOT),
            "--codex", WRAPPER, "--origin", ORIGIN, "--record", RECORD, *options,
        ]
    )  # fmt: skip
    run = DRIVER_RUN.format(err=DRIVER_ERR, key=KEY, command=command, log=DRIVER_LOG)
    script = f"{SCAN}/driver/run"
    docker(
        "exec", "-i", "-u", "factorial", name, "sh", "-c", f"cat > {script} && chmod 0755 {script}",
        stdin=run.encode(),
    )  # fmt: skip
    docker("exec", "-d", "-u", "factorial", name, f"{S6}/s6-svscan", SCAN)


def lines(name: str) -> list[str]:
    return sh(name, f"cat {DRIVER_LOG} 2>/dev/null", check=False).splitlines()


def started(name: str) -> list[str]:
    """The run id each driver printed as it started."""
    return [line for line in lines(name) if line not in RUN_STATUSES]


def reports(name: str, starts: int, *statuses: str) -> Callable[[], bool]:
    """The `starts`-th driver started, and its last report is one of `statuses`. A refused
    spawn fails at once, with its message."""

    def check() -> bool:
        shown = lines(name)
        assert not shown or not shown[0].startswith("refused "), "\n".join(shown[:2])
        ids = [line for line in shown if line not in RUN_STATUSES]
        return len(ids) == starts and shown[-1] in statuses

    return check


def driver_pid(name: str) -> int:
    shown = sh(name, f"{S6}/s6-svstat -o pid {SCAN}/driver", user="factorial").strip()
    return int(shown)


def state(name: str, run_id: str) -> dict[str, Any]:
    return json.loads(sh(name, f"cat {ROOT}/runs/{run_id}/state.json"))


def ref(name: str, git_dir: str | Path) -> str:
    """The branch's head in `git_dir`, or "" while it has none."""
    shown = f"git --git-dir {git_dir} rev-parse -q --verify refs/heads/{BRANCH}"
    return sh(name, shown, user="factorial", check=False).strip()


def assert_pushed(name: str, n: int) -> None:
    """The agent's commit is the branch's head in the worker's clone and in the origin, and
    holds its edit."""
    head = ref(name, ROOT / "repos" / REPO / ".git")
    assert head, "the agent never committed"
    assert ref(name, ORIGIN) == head, "the agent's commit never reached the origin"
    edit = sh(name, f"git --git-dir {ORIGIN} show {head}:spec.md", user="factorial")
    assert edit.strip() == str(n)


def authorizations(name: str, tmp_path: Path) -> set[str | None]:
    """The credential on each of the model calls the mock answered."""
    log = tmp_path / "requests.jsonl"
    docker("cp", f"{name}:/tmp/mock.jsonl", str(log))
    return {request["authorization"] for request in Mock(log=log, url=MOCK_URL).main()}


def unspaced(text: str) -> str:
    return "".join(text.split())


def screen(name: str, socket: str) -> str:
    shown = docker(
        "exec", "-u", "factorial", name, "tmux", "-N", "-u", "-S", socket,
        "capture-pane", "-p", "-t", "=run:",
    )  # fmt: skip
    return shown.replace("\xa0", " ")


def say(name: str, socket: str, text: str) -> None:
    """A human types `text`, and presses Enter once all of it is drawn and a moment has passed:
    codex takes an Enter inside a burst of keys for a newline."""
    keys = ("exec", "-u", "factorial", name, "tmux", "-N", "-S", socket, "send-keys", "-t", "=run:")
    docker(*keys, "-l", text)
    typed = unspaced(f"{INPUT_MARK} {text}")
    until(name, f"{text!r} on the input line", lambda: typed in unspaced(screen(name, socket)))
    time.sleep(SETTLE)
    docker(*keys, "Enter")


def test_a_session_commits_and_pushes_on_a_humans_turn_after_a_worker_restart_and_finishes(
    box: str, tmp_path: Path
) -> None:
    start_driver(box, "--finish", FINISH)

    # 1. The driver spawns the session, which reaches its prompt with no first-run dialog.
    until(box, "the session idle", reports(box, 1, "idle"))
    [run_id] = started(box)
    socket = tmux.socket_path(ROOT, run_id, "s", AssertionError)
    assert not DIALOGS.search(screen(box, socket)), screen(box, socket)

    # 2. A worker-only restart: the new driver takes the recorded session back.
    first_driver = driver_pid(box)
    sh(box, f"{S6}/s6-svc -r {SCAN}/driver", user="factorial")
    until(box, "a second driver", lambda: driver_pid(box) not in (first_driver, -1))
    until(box, "the second driver's report", reports(box, 2, "idle"))
    assert started(box) == [run_id, run_id], "the new driver took the recorded run"

    # 3. A human's turn: the agent edits, commits and pushes from its sandbox.
    say(box, socket, commit_and_push(SERVED, BRANCH, 1))

    def answered() -> bool:
        now = state(box, run_id)
        return now["signals"]["prompts"] >= 2 and now["status"] == "idle"

    until(box, "the human's turn answered", answered)
    assert_pushed(box, 1)

    # 4. The finish: the CLI leaves, and the run succeeds.
    sh(box, f"touch {FINISH}", user="factorial")
    until(box, "the run finished", reports(box, 2, *ENDED))
    ended = state(box, run_id)
    assert (ended["status"], ended["exit_code"]) == ("succeeded", 0), ended["failure_reason"]
    assert authorizations(box, tmp_path) == {f"Bearer {KEY}"}


def test_without_the_profile_a_spawn_is_refused_with_the_fix(unprofiled_box: str) -> None:
    start_driver(unprofiled_box)

    until(unprofiled_box, "the driver's refusal", lambda: len(lines(unprofiled_box)) >= 2)
    shown = lines(unprofiled_box)
    assert shown[0] == f"refused {SpawnReason.CONFIG}", shown
    assert len(shown) == 2, shown
    message = shown[1]
    assert message.startswith(
        f"codex's workspace-write sandbox cannot start on this machine: {BWRAP}. A container "
        "needs the seccomp profile docker/seccomp.json "
        "(--security-opt seccomp=docker/seccomp.json)"
    ), message
    assert sh(unprofiled_box, f"ls -A {ROOT}") == "", "the refused spawn left files in its root"
