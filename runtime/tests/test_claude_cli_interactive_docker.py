"""L2h: an interactive `claude_cli` run in a container of the local image, under s6 as the image's
worker runs, on the image's own claude and tmux against `mock_anthropic.py` (decision 56). A
worker-only restart (`s6-svc -r`) leaves the session's tmux server running and the new worker
takes the session back; a container restart ends the server, and the run fails `host_lost`
with its workspace kept.

Opt-in, and never in CI: it needs Docker and an image with s6-overlay, tmux, git and the pinned
CLI. It takes under a minute.

    make image && FACTORIAL_TUI_DOCKER_IMAGE=factorial:local \\
      uv run pytest -c pyproject.toml runtime/tests/test_claude_cli_interactive_docker.py \\
      -q -p no:cacheprovider

The worker is `host_session_driver.py`, a longrun in a scratch `s6-svscan` run as `factorial`.
The image gives the CLI, tmux, s6 and the Python environment; the code under test is this
checkout's `runtime/`, copied over the image's at creation, so an image built before the last
change still tests that change. The container has no network: a bare repo in it stands for
GitHub, reached by the driver's `--origin` for the worker's git, which reads no system config,
and through a system `insteadOf` for the agent's. The run's key is a made-up one only the mock
sees.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters._cli import tmux
from runtime.adapters.claude_cli.cli import PINNED_VERSION

IMAGE = os.environ.get("FACTORIAL_TUI_DOCKER_IMAGE", "")

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason=(
        "set FACTORIAL_TUI_DOCKER_IMAGE to an image with s6-overlay, tmux 3.2 or later and claude "
        f"{PINNED_VERSION}, such as `make image`'s factorial:local"
    ),
)

#: Every container this module makes carries it, so a teardown finds them all.
LABEL = "factorial-l2h"
RUNTIME = Path(__file__).parents[1]
ROOT = Path("/data/runs/claude_cli")
RECORD = "/data/driver.run"
DRIVER_LOG = "/data/driver.log"
DRIVER_ERR = "/data/driver.err"
SCAN = "/tmp/scan"
KEY = "sk-ant-api03-l2h-made-up-key-0123456789abcdef"
WAIT = 90.0
RUN_STATUSES = {"queued", "running", "idle", "succeeded", "failed", "cancelled", "interrupted"}
#: The image's s6-overlay keeps its programs here, off the PATH a `docker exec` has.
S6 = "/command"

PREPARE = f"""set -eu
git init -q --bare -b main /srv/repo.git
seed=$(mktemp -d)
git -C "$seed" init -q -b main
echo widgets > "$seed/README.md"
git -C "$seed" add README.md
git -C "$seed" -c user.name=l2h -c user.email=l2h@example.com commit -q -m start
git -C "$seed" push -q /srv/repo.git main
git config --system url.file:///srv/repo.git.insteadOf https://github.com/acme/widgets.git
git config --system safe.directory '*'
install -d -o factorial -g factorial {ROOT} {SCAN} {SCAN}/driver
cat > {SCAN}/driver/run <<'EOF'
#!/bin/sh
exec 2>>{DRIVER_ERR}
cd /app
export HOME=/home/factorial LANG=C.UTF-8 PATH=/app/.venv/bin:/opt/clis/bin:/usr/bin:/bin
export ANTHROPIC_BASE_URL=http://127.0.0.1:18080 DRIVER_API_KEY={KEY}
exec python /app/runtime/tests/host_session_driver.py serve {ROOT} --record {RECORD} \\
  --origin /srv/repo.git >>{DRIVER_LOG}
EOF
chmod 0755 {SCAN}/driver/run
chown -R factorial:factorial {SCAN} /data /srv/repo.git
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
        for program in ("host_session_driver.py", "host_session_support.py", "mock_anthropic.py"):
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


@pytest.fixture
def box() -> Iterator[str]:
    """A container of the image idling under tini, its runtime this checkout's."""
    name = f"{LABEL}-{uuid.uuid4().hex[:8]}"
    try:
        docker(
            "run", "-d", "--init", "--network", "none", "--name", name, f"--label={LABEL}=1",
            "--entrypoint", "sleep", IMAGE, "infinity",
        )  # fmt: skip
        shown = sh(name, "claude --version").split()
        assert shown[:1] == [PINNED_VERSION], f"the image's claude is {shown}, not the pin"
        copy_runtime(name)
        sh(name, PREPARE)
        yield name
    finally:
        docker("rm", "-fv", name, check=False)


def start_mock(name: str) -> None:
    sh(
        name,
        "setsid python3 /app/runtime/tests/mock_anthropic.py /tmp/mock.jsonl 18080 "
        ">/tmp/mock.port 2>>/tmp/mock.err </dev/null &",
    )
    until(name, "the mock", lambda: sh(name, "cat /tmp/mock.port", check=False).strip() == "18080")


def start_scan(name: str) -> None:
    docker("exec", "-d", "-u", "factorial", name, f"{S6}/s6-svscan", SCAN)


def driver_lines(name: str) -> list[str]:
    return sh(name, f"cat {DRIVER_LOG} 2>/dev/null", check=False).split()


def driver_pid(name: str) -> int:
    shown = sh(name, f"{S6}/s6-svstat -o pid {SCAN}/driver", user="factorial").strip()
    return int(shown)


def state(name: str, run_id: str) -> dict[str, Any]:
    return json.loads(sh(name, f"cat {ROOT}/runs/{run_id}/state.json"))


def server_pid(name: str, socket: str) -> int:
    shown = sh(name, f"tmux -N -S {socket} display-message -p '#{{pid}}'", user="factorial")
    return int(shown.strip())


def started(name: str) -> list[str]:
    """The run id each driver printed as it started."""
    return [line for line in driver_lines(name) if line not in RUN_STATUSES]


def reports(name: str, starts: int, status: str) -> Callable[[], bool]:
    """The `starts`-th driver started, and its last report is `status`."""

    def check() -> bool:
        return len(started(name)) == starts and driver_lines(name)[-1] == status

    return check


def test_a_worker_restart_under_s6_keeps_the_session_and_a_container_restart_loses_it(
    box: str,
) -> None:
    start_mock(box)
    start_scan(box)

    # 1-2. The driver spawns once, and the run reaches its prompt on the mock.
    until(box, "the run idle", reports(box, 1, "idle"))
    [run_id] = started(box)
    assert sh(box, f"cat {RECORD}").strip() == run_id
    requests = [json.loads(line) for line in sh(box, "cat /tmp/mock.jsonl").splitlines()]
    assert {r["x-api-key"] for r in requests if r["path"].startswith("/v1/messages")} == {KEY}
    socket = tmux.socket_path(ROOT, run_id, "s", AssertionError)
    server = server_pid(box, socket)
    first_driver = driver_pid(box)

    # 3. A worker-only restart: the tmux server runs on, and the new driver takes it back.
    sh(box, f"{S6}/s6-svc -r {SCAN}/driver", user="factorial")
    until(box, "a second driver", lambda: driver_pid(box) not in (first_driver, -1))
    until(box, "the second driver's report", reports(box, 2, "idle"))
    assert started(box) == [run_id, run_id], "the new driver took the recorded run"
    assert server_pid(box, socket) == server
    # The new driver's supervisor reads the session: a human's turn reaches its state.
    sh(
        box,
        f"tmux -N -S {socket} send-keys -t =run: -l 'Reply with one word. AFTER'",
        user="factorial",
    )
    time.sleep(0.5)
    sh(box, f"tmux -N -S {socket} send-keys -t =run: Enter", user="factorial")
    until(box, "the turn after the restart", lambda: state(box, run_id)["signals"]["prompts"] == 2)
    until(box, "the run idle again", lambda: state(box, run_id)["status"] == "idle")
    assert driver_lines(box)[-1] == "idle"

    # 4. A container restart ends the server: the run fails `host_lost`, its workspace kept.
    docker("restart", "-t", "1", box)
    start_scan(box)
    until(box, "the run failed", reports(box, 3, "failed"))
    assert started(box) == [run_id] * 3
    ended = state(box, run_id)
    assert ended["status"] == "failed"
    assert ended["failure_reason"].startswith("host_lost"), ended["failure_reason"]
    sh(box, f"test -e {ROOT}/runs/{run_id}/worktree/.git")
