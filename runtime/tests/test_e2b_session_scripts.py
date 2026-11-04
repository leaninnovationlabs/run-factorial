"""The root scripts of an interactive E2B run (decision 56), and the fake that stands for them.

What can run on this machine runs: `signal.sh` under `sh`; `session-poll.sh` and `rework.sh`
under bash with their paths moved into a temp dir and a shim for the privilege drop, which only
root can do; the lockdown conf under the local tmux. The drop itself and the tmux session need
root on Linux, and are the Docker end-to-end test's.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fake_e2b import FakeSandboxClient

from runtime.adapters.e2b.adapter import _view_report
from runtime.adapters.e2b.client import NetworkPolicy, SandboxOptions
from runtime.adapters.e2b.layout import (
    _REGROUP,
    AGENT_HOME,
    AGENT_USER,
    ARGV,
    CAPTURE,
    CODEX_FINISH_SH,
    CONSENT_NOT_ASKED,
    CONSENT_NOT_TAKEN,
    CONSENT_SH,
    ENVD_PORT,
    FINISH,
    FINISH_SH,
    FIREWALL,
    FIREWALL_SH,
    GROUP_DONE,
    INSPECT,
    INSPECT_SESSION,
    INSPECT_SH,
    INSPECT_STATUS_SH,
    INSPECT_STOP_SH,
    PREVIEW_ENDED,
    PREVIEW_ENV,
    PREVIEW_LOG,
    PREVIEW_SERVICES,
    PREVIEW_SETUP,
    PREVIEW_SETUP_ENV,
    PREVIEW_SH,
    PREVIEW_START_LOG,
    PREVIEW_STATUS_SH,
    PREVIEW_STOP_SH,
    PREVIEW_USER,
    REWORK,
    REWORK_BRANCH_GONE,
    REWORK_NO_BRANCH,
    REWORK_NOT_THE_BRANCH,
    ROLLOUT_DIR,
    ROLLOUT_FIND_SH,
    ROOT_DIR,
    RUN_DIR,
    SCRIPT_ENV,
    SCRIPTS,
    SESSION,
    SESSION_POLL,
    SESSION_POLL_SH,
    SESSION_SH,
    SETUP_FAILED,
    SETUP_SH,
    SIGNAL,
    SIGNALS,
    STOP,
    TMUX_CONF,
    TMUX_DIR,
    TMUX_SOCKET,
    TTY_RUNNER_SH,
    VIEW_ARGV,
    VIEW_ENV,
    VIEW_RUNNER,
    VIEW_RUNNER_SH,
    WORKSPACE,
    WORKSPACE_GROUP,
    SessionPoll,
    read_session_poll,
    service_log,
    session_log,
)
from runtime.terminal import TMUX_LOCKDOWN

SID = "0b6f3c1e-0000-4000-8000-0000000000e2"

#: The privilege drops, as shims: each runs its command as the caller, who is not root here.
SHIMS = {
    "setpriv": '#!/bin/sh\nwhile [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac\n'
    'done\nexec "$@"\n',
    "runuser": '#!/bin/sh\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\nexec "$@"\n',
    # Not on every machine the suite runs on; the bound itself is not under test.
    "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
}


def new_root() -> Path:
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix="f243s-", dir="/tmp")))
    shims = path / "shims"
    shims.mkdir()
    for name, text in SHIMS.items():
        (shims / name).write_text(text)
        (shims / name).chmod(0o755)
    return path


@pytest.fixture
def root() -> Iterator[Path]:
    path = new_root()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def local(root: Path, path: str) -> str:
    """A sandbox path, moved under `root`."""
    return path.replace(ROOT_DIR, f"{root}/opt").replace(AGENT_HOME, f"{root}/home")


def run_script(
    root: Path, text: str, env: Mapping[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    script = root / "script.sh"
    script.write_text(local(root, text))
    shell = text.partition("\n")[0].rpartition("/")[2]
    git_isolated = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        [shell, str(script), *args],
        env={"PATH": f"{root}/shims:{os.environ['PATH']}", **git_isolated, **env},
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ("-c", "user.email=t@example.com", "-c", "user.name=t")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


# --------------------------------------------------------------------------- where things live


def test_the_tmux_socket_dir_is_roots_and_closed_to_the_agent() -> None:
    """Without search permission on the dir the agent cannot `connect()` to the root server,
    whose `run-shell` is root's shell."""
    assert f"install -d -o root -g root -m 700 {TMUX_DIR}\n" in SESSION_SH
    assert os.path.dirname(TMUX_SOCKET) == TMUX_DIR
    assert len(TMUX_SOCKET.encode()) < 100, "a unix socket path must fit sun_path"


def test_the_signals_file_is_the_agents_to_append_in_a_dir_only_root_writes() -> None:
    """The hooks run as the agent, so the file must be its own or every signal is dropped; the
    dir must be root's, or the agent could swap the file for a link root then reads."""
    assert f"install -o {AGENT_USER} -g {AGENT_USER} -m 600 /dev/null {SIGNALS}" in SESSION_SH
    assert os.path.dirname(SIGNALS) == RUN_DIR
    assert f"install -d -o root -g root -m 755 {RUN_DIR}\n" in SETUP_SH


def _tmux_calls(text: str) -> list[str]:
    """Each tmux invocation in a script: the rest of its line, from the word `tmux` on."""
    calls = []
    for line in text.splitlines():
        if not line.lstrip().startswith("#"):
            calls += [line[m.start() :] for m in re.finditer(r"(?<![\w-])tmux(?= )", line)]
    return calls


def test_every_tmux_call_loads_the_lockdown_conf_and_only_two_scripts_start_a_server() -> None:
    """A server started without the conf leaves a viewer typing into the pane the prefix and
    `run-shell`, as root. Every other call is `-N`: it can never start one."""
    calls = {path: _tmux_calls(text) for path, text in SCRIPTS.items()}
    assert all(calls[path] for path in (SESSION, FINISH, CAPTURE, INSPECT))
    for path, found in calls.items():
        for call in found:
            assert call.startswith(f"tmux -u -S {TMUX_SOCKET} -f {TMUX_CONF} "), (path, call)
            assert ("new-session" in call) is (" -N " not in call), (path, call)
            assert "new-session" not in call or path in (SESSION, INSPECT), (path, call)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_the_lockdown_conf_leaves_the_server_no_prefix_and_no_key(root: Path) -> None:
    conf = root / "tmux.conf"
    conf.write_text(TMUX_LOCKDOWN)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    def tmux(*args: str) -> str:
        argv = ["tmux", "-u", "-S", str(root / "s"), "-f", str(conf), *args]
        return subprocess.run(argv, env=env, capture_output=True, text=True, check=True).stdout

    tmux("new-session", "-d", "-s", "run", "--", "sleep", "30")
    try:
        shown = {
            option: tmux("show-options", "-gv", option).strip()
            for option in ("prefix", "prefix2", "status", "mouse", "allow-passthrough")
        }
        assert shown == {
            "prefix": "None",
            "prefix2": "None",
            "status": "off",
            "mouse": "off",
            "allow-passthrough": "off",
        }
        assert tmux("show-options", "-sv", "set-clipboard").strip() == "off"
        assert tmux("list-keys") == ""
    finally:
        subprocess.run(["tmux", "-S", str(root / "s"), "kill-server"], env=env, check=False)


# --------------------------------------------------------------------------- the inputs


def test_every_script_takes_its_values_from_the_environment_it_declares_or_a_file() -> None:
    """The scripts are constants: a value reaches one through a declared variable or a file it
    reads, and is never evaluated as code by root. Only the agent runs a script it was handed:
    a preview's commands, which are the agent's own config."""
    assigned = re.compile(r"(?:^|[\s;(])([A-Z_][A-Z0-9_]*)=|mapfile .*-t ([A-Z_][A-Z0-9_]*)")
    read = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)")
    assert SCRIPT_ENV.keys() == SCRIPTS.keys()
    for path, declared in SCRIPT_ENV.items():
        lines = [line for line in SCRIPTS[path].splitlines() if not line.lstrip().startswith("#")]
        text = "\n".join(lines)
        own = {name for match in assigned.finditer(text) for name in match.groups() if name}
        assert set(read.findall(text)) - own - {"PIPESTATUS"} == declared, path
        assert "eval" not in text.split(), path
        for line in lines:
            for flag in re.finditer(r"\b(?:ba)?sh -c (\S)", line):
                unprivileged = "as_agent " in line or "as_preview " in line
                assert flag.group(1) == "'" or unprivileged, f"{path}: {line}"


def test_signal_script_prints_nothing_and_always_exits_zero(root: Path) -> None:
    """A hook's stdout can reach the model, and its exit code 2 blocks the tool call."""
    signals = root / "signals.jsonl"
    hook = {"hook_event_name": "Stop", "text": "a $(touch pwned) `x`"}
    done = subprocess.run(
        ["sh", "-c", SCRIPTS[SIGNAL], "signal.sh"],
        input=json.dumps(hook, indent=2),
        env={"PATH": os.environ["PATH"], "FACTORIAL_SIGNALS": str(signals)},
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    (line,) = signals.read_text().splitlines()
    assert json.loads(line)["hook"] == hook
    assert not (root / "pwned").exists()

    unwritable = subprocess.run(
        ["sh", "-c", SCRIPTS[SIGNAL], "signal.sh"],
        input="{}",
        env={"PATH": os.environ["PATH"], "FACTORIAL_SIGNALS": str(root / "no" / "such")},
        capture_output=True,
        text=True,
    )
    assert (unwritable.returncode, unwritable.stdout) == (0, "")


# --------------------------------------------------------------------------- session-poll.sh


def _poll_files(root: Path, signals: bytes, log: bytes | None) -> None:
    run_dir = Path(local(root, RUN_DIR))
    run_dir.mkdir(parents=True)
    (run_dir / "signals.jsonl").write_bytes(signals)
    if log is not None:
        path = Path(local(root, session_log(SID)))
        path.parent.mkdir(parents=True)
        path.write_bytes(log)


def _poll_env(root: Path | None, sig_from: int, log_from: int, limit: int) -> dict[str, str]:
    log = session_log(SID)
    return {
        "SIG_FROM": str(sig_from),
        "LOG_FROM": str(log_from),
        "LIMIT": str(limit),
        "LOG": log if root is None else local(root, log),
    }


@pytest.mark.parametrize("has_log", [True, False])
async def test_the_fake_answers_a_session_poll_as_the_script_does(
    root: Path, has_log: bool
) -> None:
    """The adapter's reader is tested against the fake: the two must print the same thing."""
    signals = b'{"at":"2026-10-01T00:00:00Z","hook":{"hook_event_name":"Stop"}}\n' * 3
    log = '{"type":"assistant","message":{"content":"café"}}\n'.encode() * 5 if has_log else None
    _poll_files(root, signals, log)
    done = run_script(root, SESSION_POLL_SH, _poll_env(root, 10, 7, 100))
    assert done.returncode == 0, done.stderr
    polled = read_session_poll(done.stdout)
    assert polled == SessionPoll(
        log_size=None if log is None else len(log),
        signals_size=len(signals),
        signals=signals[10:110],
        log=b"" if log is None else log[7:107],
    )

    client = FakeSandboxClient()
    sandbox = await client.create(SandboxOptions("t", 60, {}, NetworkPolicy(allow=())))
    sandbox.signals_file = signals
    if log is not None:
        sandbox.logs[session_log(SID)] = log
    faked = await sandbox.run(SESSION_POLL, envs=_poll_env(None, 10, 7, 100))
    assert faked.stdout == done.stdout


@pytest.mark.parametrize("offset", ["a[$(touch {root}/pwned)]", "010"], ids=["code", "octal"])
def test_a_session_poll_evaluates_no_offset_as_code(root: Path, offset: str) -> None:
    """Bash arithmetic would run `a[$(cmd)]`, and read `010` as 8: the numbers are checked
    before any is used."""
    _poll_files(root, b"", None)
    env = {**_poll_env(root, 0, 0, 10), "SIG_FROM": offset.format(root=root)}
    done = run_script(root, SESSION_POLL_SH, env)
    assert done.returncode == 2
    assert not (root / "pwned").exists()


def test_the_session_log_is_read_only_as_the_agent() -> None:
    """The log and its dir are the agent's: read as root, a link there reaches any file."""
    uses = [
        line
        for line in SESSION_POLL_SH.splitlines()
        if re.search(r"\$\{?LOG\b", line) and not line.lstrip().startswith(("#", ': "${LOG:?'))
    ]
    assert uses and all("as_agent " in line for line in uses), uses


# --------------------------------------------------------------------------- rework.sh

HOSTILE = "spec/12-$(touch${IFS}pwned)`touch${IFS}pwned2`;id"


@pytest.fixture
def checkout(root: Path) -> tuple[Path, Path]:
    """A kept checkout on the PR's branch, behind origin, with uncommitted work in it."""
    origin, workspace = root / "origin.git", Path(local(root, WORKSPACE))
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    workspace.parent.mkdir(parents=True)
    _git(root, "clone", "-q", str(origin), str(workspace))
    _git(workspace, "commit", "-q", "--allow-empty", "-m", "base")
    _git(workspace, "checkout", "-q", "-b", HOSTILE)
    (workspace / "tracked.txt").write_text("v1\n")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-q", "-m", "attempt 1")
    _git(workspace, "push", "-q", "origin", "HEAD")
    other = root / "other"
    _git(root, "clone", "-q", "-b", HOSTILE, str(origin), str(other))
    (other / "tracked.txt").write_text("from review\n")
    _git(other, "commit", "-q", "-am", "the PR head")
    _git(other, "push", "-q", "origin", "HEAD")
    (workspace / "tracked.txt").write_text("uncommitted\n")
    (workspace / "notes.txt").write_text("untracked\n")
    return workspace, other


def test_a_rework_stashes_then_resets_to_the_pr_head_and_never_runs_the_branch_name(
    root: Path, checkout: tuple[Path, Path]
) -> None:
    workspace, other = checkout
    done = run_script(root, SCRIPTS[REWORK], {"BRANCH_GLOB": "spec/12-*", "RUN": "run-2"})
    assert done.returncode == 0, done.stderr

    head = _git(other, "rev-parse", "HEAD")
    stash = _git(workspace, "rev-parse", "refs/stash")
    assert done.stdout == f"{HOSTILE} {head}\nstash {stash}\n"
    assert _git(workspace, "rev-parse", "HEAD") == head
    assert _git(workspace, "status", "--porcelain") == ""
    assert "factorial: before the rework by run run-2" in _git(workspace, "stash", "list")
    assert _git(workspace, "show", "stash@{0}:tracked.txt") == "uncommitted"
    assert _git(workspace, "show", "stash@{0}^3:notes.txt") == "untracked"
    assert not list(root.rglob("pwned*"))


def test_a_clean_rework_makes_no_stash(root: Path, checkout: tuple[Path, Path]) -> None:
    workspace, _ = checkout
    _git(workspace, "checkout", "-q", "--", "tracked.txt")
    (workspace / "notes.txt").unlink()
    done = run_script(root, SCRIPTS[REWORK], {"BRANCH_GLOB": "spec/12-*", "RUN": "run-2"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("\n") == 1 and "stash" not in done.stdout


def test_a_rework_says_where_commits_the_pr_head_lacks_are(
    root: Path, checkout: tuple[Path, Path]
) -> None:
    """A stash holds only uncommitted work: a commit the attempt never pushed is left behind by
    the reset, so its sha is reported for the turn to name."""
    workspace, other = checkout
    _git(workspace, "stash", "-q", "--include-untracked")
    _git(workspace, "commit", "-q", "--allow-empty", "-m", "never pushed")
    old = _git(workspace, "rev-parse", "HEAD")
    done = run_script(root, SCRIPTS[REWORK], {"BRANCH_GLOB": "spec/12-*", "RUN": "run-2"})
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{HOSTILE} {_git(other, 'rev-parse', 'HEAD')}\nlost {old}\n"


def test_a_rework_runs_none_of_the_agents_hooks(root: Path, checkout: tuple[Path, Path]) -> None:
    """The agent writes its checkout's hooks; run with the rework's credentials, one could also
    print into the report the adapter reads."""
    workspace, _ = checkout
    for name in ("reference-transaction", "post-checkout", "post-rewrite"):
        hook = workspace / ".git" / "hooks" / name
        hook.write_text(f'#!/bin/sh\necho "stash {"0" * 40}"\ntouch "{root}/hooked"\n')
        hook.chmod(0o755)
    done = run_script(root, SCRIPTS[REWORK], {"BRANCH_GLOB": "spec/12-*", "RUN": "run-2"})
    assert done.returncode == 0, done.stderr
    assert not (root / "hooked").exists()
    assert done.stdout.count("\n") == 2, done.stdout


@pytest.mark.parametrize(
    ("setup", "glob", "code"),
    [
        (("checkout", "-q", "--detach"), "spec/12-*", REWORK_NO_BRANCH),
        ((), "spec/99-*", REWORK_NOT_THE_BRANCH),
        (("checkout", "-q", "-b", "spec/12-never-pushed"), "spec/12-*", REWORK_BRANCH_GONE),
    ],
    ids=["detached", "another-branch", "gone-from-origin"],
)
def test_a_rework_refuses_a_checkout_it_cannot_put_on_its_pr(
    root: Path, checkout: tuple[Path, Path], setup: tuple[str, ...], glob: str, code: int
) -> None:
    """The adapter reads these three codes as a session that cannot be resumed; any other
    failure is the substrate's."""
    workspace, _ = checkout
    _git(workspace, "stash", "-q", "--include-untracked")
    if setup:
        _git(workspace, *setup)
    before = _git(workspace, "rev-parse", "HEAD")
    done = run_script(root, SCRIPTS[REWORK], {"BRANCH_GLOB": glob})
    assert done.returncode == code, done.stderr
    assert done.stdout == ""
    assert _git(workspace, "rev-parse", "HEAD") == before


def test_a_retry_moves_nothing_and_reports_the_local_tip(
    root: Path, checkout: tuple[Path, Path]
) -> None:
    workspace, _ = checkout
    head = _git(workspace, "rev-parse", "HEAD")
    done = run_script(
        root, SCRIPTS[REWORK], {"BRANCH_GLOB": "spec/12-*", "RUN": "run-2", "MODE": "retry"}
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{HOSTILE} {head}\n"
    assert _git(workspace, "rev-parse", "HEAD") == head
    assert "notes.txt" in _git(workspace, "status", "--porcelain")
    assert not (workspace / ".git" / "FETCH_HEAD").exists() and not list(root.rglob("pwned*"))


def test_every_git_command_in_a_rework_runs_as_the_agent() -> None:
    """The checkout's config and hooks are the agent's: git run as root there runs the agent's
    code as root."""
    text = SCRIPTS[REWORK]
    calls = [
        line
        for line in text.splitlines()
        if re.search(r"(?<![\w-])git ", line) and not line.lstrip().startswith("#")
    ]
    assert calls
    assert all(re.search(r"as_agent git ", line) for line in calls), calls


# --------------------------------------------------------------------------- the session view


def _stop_stub(root: Path) -> Path:
    """`STOP` at its local path, leaving a mark: `pkill` as root is the sandbox's to prove."""
    stop = Path(local(root, STOP))
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(f"#!/bin/sh\ntouch {root}/stopped\n")
    stop.chmod(0o755)
    return root / "stopped"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_the_view_status_read_sees_only_the_view_and_its_stop_ends_only_the_view(
    root: Path,
) -> None:
    """The adapter parses the status line; another session of the server is not the view's."""
    conf, socket = Path(local(root, TMUX_CONF)), Path(local(root, TMUX_SOCKET))
    socket.parent.mkdir(parents=True)
    conf.write_text(TMUX_LOCKDOWN)
    _stop_stub(root)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        argv = ["tmux", "-S", str(socket), "-f", str(conf), *args]
        return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)

    assert tmux("new-session", "-d", "-s", "run", "--", "sleep", "60").returncode == 0
    try:
        assert run_script(root, INSPECT_STATUS_SH, {}).stdout == "closed\n"
        assert tmux("new-session", "-d", "-s", INSPECT_SESSION, "--", "sleep", "60").returncode == 0
        shown = _view_report(run_script(root, INSPECT_STATUS_SH, {}).stdout)
        assert shown is not None and shown[0] == 0
        assert abs(shown[1].timestamp() - time.time()) < 60

        assert run_script(root, INSPECT_STOP_SH, {"SIZE": "0"}).returncode == 0
        assert run_script(root, INSPECT_STATUS_SH, {}).stdout == "closed\n"
        assert tmux("has-session", "-t", "=run").returncode == 0, "the other session stays"
        stopped = run_script(root, INSPECT_STOP_SH, {"SIZE": "0"})
        assert stopped.returncode == 0, "and a stop is idempotent"
    finally:
        tmux("kill-server")


def test_a_views_end_cuts_the_log_back_only_after_stop_and_deletes_its_files(root: Path) -> None:
    """A view takes no turn, so what its CLI appended to the session log goes, the line it
    writes as `STOP` ends it among them; a log that is shorter, or gone, is left as it is."""
    stopped = _stop_stub(root)
    log = root / "home" / "session.jsonl"
    log.parent.mkdir(parents=True)
    Path(local(root, STOP)).write_text(f"#!/bin/sh\ntouch {stopped}\nprintf 'bye\\n' >> {log}\n")
    credential = root / "home" / "auth.json"
    credential.write_text("{}")
    log.write_bytes(b"turn one\nthe view's\n")
    size = {"LOG": str(log), "SIZE": str(len(b"turn one\n"))}

    done = run_script(root, INSPECT_STOP_SH, size, str(credential))

    assert done.returncode == 0, done.stderr
    assert stopped.exists() and log.read_bytes() == b"turn one\n"
    assert not credential.exists()

    _stop_stub(root)
    log.write_bytes(b"short")
    assert run_script(root, INSPECT_STOP_SH, size).returncode == 0
    assert log.read_bytes() == b"short", "a shorter log stays"
    log.unlink()
    assert run_script(root, INSPECT_STOP_SH, size).returncode == 0
    assert not log.exists(), "a log that is gone is not made"
    refused = run_script(root, INSPECT_STOP_SH, {"LOG": str(log), "SIZE": "1; touch pwned"})
    assert refused.returncode == 2 and not list(root.rglob("pwned*"))


def _script(root: Path, text: str) -> Path:
    script = root / "script.sh"
    script.write_text(local(root, text))
    return script


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_the_view_runs_its_own_launch_in_the_workspace_and_nothing_of_roots(
    root: Path,
) -> None:
    """The pane runs the view's argv with the view's env as its whole environment, never the
    session's launch files, and neither root's environment nor tmux's TMUX reaches it."""
    shims = root / "shims"
    (shims / "install").write_text('#!/bin/sh\nfor a; do last=$a; done\nmkdir -p "$last"\n')
    (shims / "install").chmod(0o755)
    workspace = Path(local(root, WORKSPACE))
    workspace.mkdir(parents=True)
    Path(local(root, TMUX_CONF)).parent.mkdir(parents=True, exist_ok=True)
    Path(local(root, TMUX_CONF)).write_text(TMUX_LOCKDOWN)
    Path(local(root, VIEW_RUNNER)).parent.mkdir(parents=True, exist_ok=True)
    Path(local(root, VIEW_RUNNER)).write_text(local(root, VIEW_RUNNER_SH))
    out = root / "seen.txt"
    argv = ["/bin/sh", "-c", f"env > {out}.tmp; pwd >> {out}.tmp; mv {out}.tmp {out}; sleep 30"]
    entries = [f"PATH={os.environ['PATH']}", "VIEWED=1"]
    view_argv, view_env = Path(local(root, VIEW_ARGV)), Path(local(root, VIEW_ENV))
    view_argv.parent.mkdir(parents=True, exist_ok=True)
    view_argv.write_bytes(b"".join(word.encode() + b"\0" for word in argv))
    view_env.write_bytes(b"".join(entry.encode() + b"\0" for entry in entries))
    Path(local(root, ARGV)).write_bytes(b"/bin/false\0")
    socket = local(root, TMUX_SOCKET)
    env = {"PATH": f"{shims}:{os.environ['PATH']}", "ROOT_SECRET": "root-env-must-not-reach"}
    start = subprocess.Popen(["bash", str(_script(root, INSPECT_SH))], env=env, cwd=root)

    def tmux(*args: str) -> int:
        argv = ["tmux", "-N", "-S", socket, *args]
        return subprocess.run(argv, capture_output=True, check=False).returncode

    try:
        for _ in range(100):
            if out.exists():
                break
            time.sleep(0.1)
        lines = out.read_text().splitlines()
        assert lines[-1] == str(workspace)
        seen = set(lines[:-1]) - {"SHLVL=1", f"PWD={workspace}", "_=/usr/bin/env"}
        assert seen == set(entries)
        assert tmux("has-session", "-t", f"={INSPECT_SESSION}") == 0
        tmux("kill-session", "-t", f"={INSPECT_SESSION}")
        assert start.wait(timeout=10) == 0, "the start script ends with the view"
    finally:
        start.kill()
        tmux("kill-server")


# --------------------------------------------------------------------------- the TUI's keys

#: A TUI under a tmux server, enough for the finish and consent scripts: the screen they read, the
#: pastes they make, and codex's answer to each. Its state is a JSON file at $STUB.
STUB_TMUX = f"""#!{sys.executable}
import json, os, sys
path = os.environ["STUB"]
with open(path) as f:
    tui = json.load(f)
args = sys.argv[1:]
while args[0].startswith("-"):
    if args.pop(0) in ("-S", "-f"):
        args.pop(0)
command, rest = args[0], args[1:]
if tui["down"] or not tui["alive"]:
    if tui["down"]:
        tui["down"] -= 1  # its server is not up yet
    tui["refused"].append(command)  # the server left with its one session
    with open(path, "w") as f:
        json.dump(tui, f)
    sys.exit(1)
if "-t" in rest:
    tui["targets"].append(rest[rest.index("-t") + 1])
if command == "has-session" and tui.get("quitting"):
    tui["alive"] = False  # codex's Ctrl-C quit takes a moment
elif command == "capture-pane":
    tui["captures"] += 1
    if tui["captures"] == tui["asks_at"]:
        tui["asking"] = True
    if tui["asking"]:
        print("  Folder access\\n\\n\\u203a 1. Open restricted\\n  2. Quit")
    elif tui["reviewing"]:
        print("  Hooks need review\\n\\n\\u203a 1. Review hooks\\n  2. Trust all and continue")
    else:
        turns = ""
        if tui["then"] == "history":
            turns = "\\u203a 1. fix the failing tests\\n\\n\\u2022 Done, pushed.\\n\\n"
        prompt = tui["draft"] or "Ask Codex to do anything"
        print("  >_ OpenAI Codex\\n\\n" + turns + "\\u203a " + prompt)
elif command == "set-buffer":
    tui["buffer"] = rest[-1]
elif command == "paste-buffer":
    data = tui.pop("buffer")
    tui["pastes"].append([data, "-p" in rest, tui["captures"]])
    if tui["asking"]:
        taken = data == "\\r" and tui["then"] != "stuck"
        tui["asking"], tui["reviewing"] = not taken, taken and tui["then"] == "hooks"
    elif data == "\\x03":
        tui["quitting"] = tui["draft"] == ""
        tui["draft"] = ""
    elif data == "\\r":
        tui["alive"] = tui["draft"] != "/quit"
        tui["draft"] = ""
    else:
        tui["draft"] += data
elif command == "kill-server":
    tui["alive"] = False
with open(path, "w") as f:
    json.dump(tui, f)
"""


def stub_tui(
    root: Path, *, draft: str = "", asks_at: int = 0, then: str = "prompt", down: int = 0
) -> Path:
    """The stub on PATH, with a codex TUI that has `draft` typed, and that puts its consent
    screen up at its `asks_at`th capture. After the consent's Enter it shows `then`: its prompt,
    the consent still (`stuck`), or a hook review (`hooks`). Its server refuses the first `down`
    calls, as one not yet up does."""
    tmux = root / "shims" / "tmux"
    tmux.write_text(STUB_TMUX)
    tmux.chmod(0o755)
    pgrep = root / "shims" / "pgrep"
    pgrep.write_text(f'#!/bin/sh\necho "$*" >> {root}/pgrep\nexit 1\n')
    pgrep.chmod(0o755)
    stop = Path(local(root, STOP))
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(f"#!/bin/sh\ntouch {root}/stopped\n")
    stop.chmod(0o755)
    state = root / "tui.json"
    tui = {"alive": True, "draft": draft, "asks_at": asks_at, "asking": False, "captures": 0}
    state.write_text(
        json.dumps(
            {
                **tui,
                "then": then,
                "down": down,
                "reviewing": False,
                "pastes": [],
                "refused": [],
                "targets": [],
            }
        )
    )
    return state


def test_the_claude_finish_script_is_unchanged_byte_for_byte() -> None:
    """Proven on the real 2.1.284 in a Linux sandbox (decision 56): the codex rendering must not
    move a byte of it. On a mismatch, diff it against `FINISH_SH` at that commit."""
    assert hashlib.sha256(FINISH_SH.encode()).hexdigest() == (
        "95f33d70877569963b74d9609d695f0b02741d675c4e72768deae5a08cc19155"
    )


@pytest.mark.parametrize(
    ("draft", "pastes"),
    [
        ("", [["\x03", False]]),
        ("half a reply", [["\x03", False], ["/quit", True], ["\r", False]]),
    ],
    ids=["an idle prompt quits on ctrl-c", "a draft is emptied, then /quit"],
)
def test_the_codex_finish_script_sends_no_escape_pastes_quit_and_waits_for_codex_native(
    root: Path, draft: str, pastes: list[list[object]]
) -> None:
    """Codex reads an Esc at an idle prompt as editing the last message, and Ctrl-C at an empty
    one as quit. A draft survives Ctrl-C's quit, so `/quit` follows only for a TUI still there,
    its Enter a paste of its own: inside the paste, codex would take it for a newline."""
    assert subprocess.run(["bash", "-n"], input=CODEX_FINISH_SH, text=True).returncode == 0
    state = stub_tui(root, draft=draft)
    done = run_script(root, CODEX_FINISH_SH, {"MODE": "exit", "STUB": str(state)})

    assert done.returncode == 0, done.stderr
    tui = json.loads(state.read_text())
    assert [paste[:2] for paste in tui["pastes"]] == pastes
    assert "set-buffer" not in tui["refused"], "typed into a TUI that had already quit"
    assert (root / "pgrep").read_text() == f"-u {AGENT_USER} -x codex-native\n"
    assert (root / "stopped").exists()


def test_a_codex_launch_takes_the_folder_consent_before_anything_else(root: Path) -> None:
    """Every codex launch and resume in a checkout seeded untrusted asks for the folder first,
    "Open restricted" already chosen (0.159.0). One Enter takes it, and only once it is on
    screen: an Enter sooner would reach whatever the screen was."""
    state = stub_tui(root, asks_at=3)
    done = run_script(root, CONSENT_SH, {"STUB": str(state)})

    assert done.returncode == 0, done.stdout + done.stderr
    tui = json.loads(state.read_text())
    ((key, bracketed, captures),) = tui["pastes"]
    assert (key, bracketed) == ("\r", False)
    assert captures >= 3, "Enter before the consent screen was up"
    assert set(tui["targets"]) == {"=run:"}, "the run's own session, unless told otherwise"


def test_a_views_consent_is_taken_in_the_views_own_tmux_session(root: Path) -> None:
    """A session view's CLI asks for the folder as a resume's does, in the view's session."""
    state = stub_tui(root, asks_at=2)
    done = run_script(root, CONSENT_SH, {"STUB": str(state), "TMUX_TARGET": INSPECT_SESSION})

    assert done.returncode == 0, done.stdout + done.stderr
    tui = json.loads(state.read_text())
    assert [paste[0] for paste in tui["pastes"]] == ["\r"]
    assert set(tui["targets"]) == {f"={INSPECT_SESSION}:"}
    target = {"STUB": str(state), "TMUX_TARGET": f"{INSPECT_SESSION}; touch {root}/pwned"}
    refused = run_script(root, CONSENT_SH, target)
    assert refused.returncode == 2 and not list(root.rglob("pwned*"))


def test_a_codex_launch_waits_for_its_tmux_server_before_it_judges_the_tui_gone(
    root: Path,
) -> None:
    """E2B's start returns before session.sh's server is up, so the first looks can find no
    server. Judging the TUI gone there failed a live canary's resume on E2B."""
    state = stub_tui(root, asks_at=1, down=3)
    done = run_script(root, CONSENT_SH, {"STUB": str(state)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert [paste[0] for paste in json.loads(state.read_text())["pastes"]] == ["\r"]


def test_a_codex_launch_that_does_not_reach_its_prompt_says_what_is_on_screen() -> None:
    """A hook the agent added to its own config home has no trusted hash in the config the
    launch renders, so codex asks to review it, and that dialog draws its choices behind the
    input mark too, as the history does a human's turn, numbered or not. So the last line behind
    the mark decides: the prompt is the bottom one unless a dialog holds the bottom pane. The
    cases run together, since each waits out the script's timeout."""
    cases = {
        "no consent screen": (10**6, "prompt", CONSENT_NOT_ASKED, "Ask Codex"),
        "a consent that does not close": (1, "stuck", CONSENT_NOT_TAKEN, "Open restricted"),
        "a hook review after it": (1, "hooks", CONSENT_NOT_TAKEN, "Hooks need review"),
        "a human's numbered turn above the prompt": (1, "history", 0, ""),
    }
    roots = {name: new_root() for name in cases}

    def launch(name: str) -> subprocess.CompletedProcess[str]:
        asks_at, then, _, _ = cases[name]
        state = stub_tui(roots[name], asks_at=asks_at, then=then)
        return run_script(roots[name], CONSENT_SH, {"STUB": str(state)})

    try:
        with ThreadPoolExecutor(len(cases)) as pool:
            done = dict(zip(cases, pool.map(launch, cases), strict=True))
    finally:
        for path in roots.values():
            shutil.rmtree(path, ignore_errors=True)
    for name, (_, _, code, screen) in cases.items():
        assert (done[name].returncode, screen in done[name].stdout) == (code, True), name


def test_the_rollout_finder_prints_one_new_rollout_or_nothing(root: Path) -> None:
    """Codex names its rollout only once it runs, so a spawn finds it: the plain file begun
    since this launch's argv was written. An earlier launch's, or a compressed one, is not it."""
    day = Path(local(root, ROLLOUT_DIR)) / "2026/09/23"
    day.mkdir(parents=True)
    argv = Path(local(root, ARGV))
    argv.parent.mkdir(parents=True)
    argv.touch()
    now = time.time()
    os.utime(argv, (now - 60, now - 60))
    name = "rollout-2026-09-23T12-00-0{}-0199a213-81c0-7800-8aa1-bbab2a03{:04d}.jsonl"
    older = day / name.format(0, 1)
    older.touch()
    os.utime(older, (now - 3600, now - 3600))
    (day / (name.format(1, 2) + ".zst")).touch()

    assert run_script(root, ROLLOUT_FIND_SH, {}).stdout == ""
    new = day / name.format(2, 3)
    new.touch()
    assert run_script(root, ROLLOUT_FIND_SH, {}).stdout == f"{new}\n"



def test_preview_sh_regroups_the_checkout_once_and_runs_services_as_the_preview_user() -> None:
    assert f"setpriv --reuid={PREVIEW_USER} --regid={PREVIEW_USER}" in PREVIEW_SH
    assert f"[ ! -f {GROUP_DONE} ]" in PREVIEW_SH
    assert "umask 002" in PREVIEW_SH
    # setup is the agent's, from the env file that carries no preview value
    assert 'as_agent bash -c "$step"' in PREVIEW_SH
    assert f"mapfile -d '' -t SETUP_ENVV < {PREVIEW_SETUP_ENV}" in PREVIEW_SH
    assert 'as_preview PORT="${S[i+1]}"' in PREVIEW_SH


def test_the_regroup_runs_as_the_agent_and_leaves_git_alone() -> None:
    """Root never walks the checkout, a path the agent can relink, and the preview gets no write
    access to `.git`, whose config the agent's git would otherwise run."""
    drop = f"setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env"
    assert f"{drop} \\\n    bash -c '" in PREVIEW_SH
    assert f"regroup {WORKSPACE} && touch {GROUP_DONE}" in PREVIEW_SH
    assert "chgrp -R" not in PREVIEW_SH
    assert "chmod -R" not in PREVIEW_SH
    assert PREVIEW_SH.index("usermod -aG") < PREVIEW_SH.index(drop)
    assert f"getent group {WORKSPACE_GROUP}" in SETUP_SH
    assert f"usermod -aG {WORKSPACE_GROUP} {AGENT_USER}" in SETUP_SH


def test_the_regroup_makes_the_checkout_group_writable_except_every_git(tmp_path: Path) -> None:
    group = grp.getgrgid(os.getgid()).gr_name
    me = pwd.getpwuid(os.getuid()).pw_name
    work = tmp_path / "work"
    (work / "src" / "sub").mkdir(parents=True)
    (work / ".git" / "objects").mkdir(parents=True)
    (work / "vendor" / "lib" / ".git").mkdir(parents=True)
    (work / "sm").mkdir()
    (work / "src" / "app.js").write_text("x")
    (work / ".git" / "config").write_text("x")
    (work / "vendor" / "lib" / ".git" / "config").write_text("x")
    (work / "sm" / ".git").write_text("gitdir: ../.git/modules/sm\n")
    for path in work.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    work.chmod(0o755)
    script = _REGROUP.replace(f"chgrp {WORKSPACE_GROUP}", f"chgrp {group}")
    script = script.replace(f"-user {AGENT_USER}", f"-user {me}")
    done = subprocess.run(["bash", "-c", script, "regroup", str(work)], capture_output=True)
    assert done.returncode == 0, done.stderr
    assert (work / "src" / "app.js").stat().st_mode & 0o020
    assert (work / "src" / "sub").stat().st_mode & 0o2020 == 0o2020
    assert work.stat().st_mode & 0o3020 == 0o3020, "the root is group-writable, setgid and sticky"
    for path in (
        work / ".git",
        work / ".git" / "config",
        work / ".git" / "objects",
        work / "vendor" / "lib" / ".git",
        work / "vendor" / "lib" / ".git" / "config",
        work / "sm" / ".git",
    ):
        assert not path.stat().st_mode & 0o2020, path
    assert (work / "vendor" / "lib").stat().st_mode & 0o020, "only the .git is left out"


def test_the_regroup_touches_only_the_agents_own_entries() -> None:
    assert _REGROUP.count(f"-user {AGENT_USER}") == 2
    assert _REGROUP.count("-name .git -prune") == 2
    assert _REGROUP.endswith('chmod +t "$1"')


def _shim(root: Path, name: str, text: str) -> None:
    (root / "shims" / name).write_text(text)
    (root / "shims" / name).chmod(0o755)


def _preview_sh(root: Path, steps: list[str], services: list[str]) -> str:
    """`PREVIEW_SH` with its files inlined as arrays (the script's `mapfile` is not in the bash of
    a Mac), every account and group taken as made, and the dirs the adapter writes made."""
    arrays = {
        f"mapfile -d '' -t SETUP_ENVV < {PREVIEW_SETUP_ENV}": ["PATH=/usr/bin:/bin"],
        f"mapfile -d '' -t ENVV < {PREVIEW_ENV}": ["PATH=/usr/bin:/bin"],
        f"mapfile -d '' -t STEPS < {PREVIEW_SETUP}": steps,
        f"mapfile -d '' -t S < {PREVIEW_SERVICES}": services,
    }
    text = PREVIEW_SH
    for line, values in arrays.items():
        assert line in text
        name = line.split()[4]
        text = text.replace(line, f"{name}=({' '.join(map(shlex.quote, values))})")
    for name in ("id", "getent", "usermod"):
        _shim(root, name, "#!/bin/sh\nexit 0\n")
    Path(local(root, PREVIEW_START_LOG)).parent.mkdir(parents=True)
    Path(local(root, WORKSPACE)).mkdir(parents=True)
    return text


def _stages(root: Path) -> list[str]:
    """The start log's timestamped lines, without their timestamps."""
    stamped = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (.+)")
    lines = Path(local(root, PREVIEW_START_LOG)).read_text().splitlines()
    return [match[1] for line in lines if (match := stamped.fullmatch(line))]


def test_a_preview_start_logs_each_stage_until_its_services_run(root: Path) -> None:
    _shim(root, "find", "#!/bin/sh\nexit 0\n")
    text = _preview_sh(root, ["true"], ["web", "3000", "/", "echo up"])

    done = run_script(root, text, {})

    assert done.returncode == 0, Path(local(root, PREVIEW_START_LOG)).read_text()
    assert _stages(root) == [
        "starting",
        "regrouping the checkout",
        "setup step 1/1",
        "setup done",
        "starting service web on port 3000",
        "serving",
        "exit 0 during serving",
    ]
    assert Path(local(root, service_log("web"))).read_text() == "up\n"
    assert (done.stdout, done.stderr) == ("", ""), "the script's own output goes to its log"


def test_a_preview_start_that_fails_before_its_setup_logs_why_and_at_which_stage(
    root: Path,
) -> None:
    """What runs before setup marks nothing of it, so the adapter reads only that the script is
    gone: the error and the stage are what tell a regroup a vanished file failed from a kill."""
    vanished = "find: './.next/cache/x': No such file or directory"
    _shim(root, "find", f'#!/bin/sh\necho "{vanished}" >&2\nexit 1\n')
    text = _preview_sh(root, ["npm ci"], ["web", "3000", "/", "npm run dev"])

    done = run_script(root, text, {})

    assert done.returncode == 1
    assert _stages(root) == [
        "starting",
        "regrouping the checkout",
        "exit 1 during regrouping the checkout",
    ]
    assert vanished in Path(local(root, PREVIEW_START_LOG)).read_text()
    assert not Path(local(root, PREVIEW_LOG)).exists(), "no setup step ran"


def test_the_regroup_starts_in_the_checkout_not_where_the_start_began(root: Path) -> None:
    """E2B starts the script in root's home, which the agent cannot enter, and find runs each
    `-exec` batch from where it started: from there every batch fails with `Failed to change
    directory: /root`. The script begins here in `root`, which stands for that home."""
    _shim(root, "find", f'#!/bin/sh\npwd >> "{root}/find.cwd"\n')
    text = _preview_sh(root, ["true"], ["web", "3000", "/", "true"])

    done = run_script(root, text, {})

    assert done.returncode == 0, Path(local(root, PREVIEW_START_LOG)).read_text()
    workspace = local(root, WORKSPACE)
    assert (root / "find.cwd").read_text() == f"{workspace}\n{workspace}\n"


def test_a_failing_setup_step_is_logged_under_its_number_with_its_exit_code(root: Path) -> None:
    text = _preview_sh(root, ["echo one", "echo two; exit 3", "echo three"], ["web", "1", "/", "x"])
    Path(local(root, GROUP_DONE)).touch()

    done = run_script(root, text, {})

    assert done.returncode == 1
    assert Path(local(root, PREVIEW_LOG)).read_text() == (
        "== step 1/3: echo one\none\n== step 2/3: echo two; exit 3\ntwo\n== step 2/3 exited 3\n"
    )
    assert Path(local(root, SETUP_FAILED)).exists()
    assert _stages(root)[-2:] == ["setup step 2/3", "exit 1 during setup step 2/3"]


def test_a_preview_start_a_signal_ends_says_so_rather_than_exit_0(root: Path) -> None:
    """Bash's exit trap reads `$?` as 0 when a signal ends it during a command, the way the
    adapter's own stop does: a start never logs a clean exit it did not take."""
    text = _preview_sh(root, ["kill -TERM $PPID; sleep 2"], ["web", "1", "/", "x"])
    Path(local(root, GROUP_DONE)).touch()

    run_script(root, text, {})

    assert _stages(root)[-2:] == ["setup step 1/1", "ended by a signal during setup step 1/1"]


def _preview_ended(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", local(root, PREVIEW_ENDED)],
        env={"PATH": f"{root}/shims:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_gone_start_is_read_from_both_logs_and_what_the_kernel_killed(root: Path) -> None:
    Path(local(root, PREVIEW_START_LOG)).parent.mkdir(parents=True)
    Path(local(root, PREVIEW_START_LOG)).write_text("T starting\nT setup step 1/1\n")
    Path(local(root, PREVIEW_LOG)).write_text("== step 1/1: npm ci\nnpm http fetch\n")
    kernel = "[ 1.0] eth0: link up\n[ 9.5] Out of memory: Killed process 4242 (node)\n"
    _shim(root, "dmesg", f"#!/bin/sh\nprintf '{kernel}'\n")

    done = _preview_ended(root)

    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "T starting\nT setup step 1/1\n"
        "-- setup log --\n== step 1/1: npm ci\nnpm http fetch\n"
        "-- killed by the kernel --\n[ 9.5] Out of memory: Killed process 4242 (node)\n"
    )


def test_a_gone_start_leaves_out_a_log_or_a_kill_it_has_none_of(root: Path) -> None:
    Path(local(root, PREVIEW_START_LOG)).parent.mkdir(parents=True)
    Path(local(root, PREVIEW_START_LOG)).write_text("T starting\n")
    _shim(root, "dmesg", "#!/bin/sh\necho 'dmesg: read kernel buffer failed' >&2\nexit 1\n")

    done = _preview_ended(root)

    assert (done.returncode, done.stdout) == (0, "T starting\n")


def test_the_firewall_contains_the_preview_user_and_admits_only_declared_ports() -> None:
    assert "owner=" + f'"meta skuid {PREVIEW_USER}"' in FIREWALL_SH
    assert f"tcp dport != {{ $PORTS, {ENVD_PORT} }}" in FIREWALL_SH
    assert "delete table inet factorial" in FIREWALL_SH, "the load replaces the table whole"
    assert SCRIPT_ENV[FIREWALL] == frozenset({"MODE", "PORTS"})


def test_the_contain_rules_block_only_what_the_preview_originates() -> None:
    """Rules on a bare `reject` per uid also catch its replies, and it cannot serve."""
    lo = '\\"lo\\"'
    assert f'$owner oifname != {lo} meta l4proto != tcp reject' in FIREWALL_SH
    assert f"$owner oifname != {lo} tcp sport != {{ $PORTS }} reject" in FIREWALL_SH
    assert f"$owner oifname != {lo} tcp flags & (syn | ack) == syn reject" in FIREWALL_SH
    assert f"$owner oifname {lo} meta l4proto != tcp reject" in FIREWALL_SH
    assert (
        f"$owner oifname {lo} tcp flags & (syn | ack) == syn tcp dport != {{ $PORTS }} reject"
        in FIREWALL_SH
    )
    assert f"oifname {lo} reject" not in FIREWALL_SH


def test_contain_cuts_the_connections_of_declared_ports_after_the_table_loads() -> None:
    cut = 'ss -K -tn state connected "( $declared )"'
    assert cut in FIREWALL_SH
    assert FIREWALL_SH.index("nft -f -") < FIREWALL_SH.index(cut)
    assert 'declared="${declared:+$declared or }sport = :$port"' in FIREWALL_SH


def firewall_run(
    tmp_path: Path, mode: str, ports: str, shims: bool
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "firewall.sh"
    script.write_text(FIREWALL_SH)
    path = "/usr/bin:/bin"
    if shims:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        for name in ("id", "useradd", "nft"):
            (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / name).chmod(0o755)
        (bin_dir / "ss").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/ss.args\n')
        (bin_dir / "ss").chmod(0o755)
        path = f"{bin_dir}:{path}"
    return subprocess.run(
        ["bash", str(script)],
        env={"MODE": mode, "PORTS": ports, "PATH": path},
        capture_output=True,
        text=True,
    )


def test_the_firewall_refuses_a_port_list_it_could_be_tricked_by(tmp_path: Path) -> None:
    for ports in ("", "80;id", "80, 81", "{ 1 }", "0", "08080", "65536", "99999", "80,,81"):
        done = firewall_run(tmp_path, "contain", ports, shims=False)
        assert done.returncode == 2, (ports, done.stderr)


def test_the_firewall_refuses_an_unknown_mode(tmp_path: Path) -> None:
    done = firewall_run(tmp_path, "closed", "8080", shims=False)
    assert done.returncode == 2, done.stderr


def test_the_firewall_accepts_declared_ports_and_cuts_exactly_them(tmp_path: Path) -> None:
    done = firewall_run(tmp_path, "contain", "8080,65535", shims=True)
    assert done.returncode == 0, done.stderr
    args = (tmp_path / "ss.args").read_text().strip()
    assert args == "-K -tn state connected ( sport = :8080 or sport = :65535 )"
    (tmp_path / "ss.args").unlink()
    assert firewall_run(tmp_path, "open", "8080", shims=True).returncode == 0
    assert not (tmp_path / "ss.args").exists(), "open cuts nothing"


def test_the_preview_stop_ends_the_preview_user_alone() -> None:
    assert f"pkill -TERM -u {PREVIEW_USER}" in PREVIEW_STOP_SH
    assert f"-u {AGENT_USER}" not in PREVIEW_STOP_SH
    assert (
        f"for _ in $(seq 1 20); do\n  pkill -KILL -u {PREVIEW_USER} || exit 0\n  sleep 0.5\ndone"
        in PREVIEW_STOP_SH
    ), "kills again until none is left"
    assert PREVIEW_STOP_SH.endswith(f"pgrep -u {PREVIEW_USER} >/dev/null && exit 1\nexit 0\n")


def test_the_preview_status_runs_git_without_the_checkouts_hooks_or_fsmonitor() -> None:
    assert "-c core.hooksPath=/dev/null -c core.fsmonitor=false" in PREVIEW_STATUS_SH
    runs = [line for line in PREVIEW_STATUS_SH.splitlines() if "runuser" in line]
    assert len(runs) == 1, "every git call goes through the one hardened function"
    assert "agit status --porcelain --ignore-submodules=all" in PREVIEW_STATUS_SH


def test_a_listener_of_another_user_is_reported_held_and_the_preview_own_ready(
    root: Path,
) -> None:
    """The decision, run here with `ss`, `node` and `id` shimmed and the services inlined (the
    script's `mapfile` is not in the bash of a Mac)."""
    shims = root / "shims"
    answers = {
        "8080": "LISTEN 0 5 127.0.0.1:8080 0.0.0.0:* uid:1002 ino:1 sk:1 <->\n",
        "8081": (
            "LISTEN 0 5 127.0.0.1:8081 0.0.0.0:* uid:1002 ino:2 sk:2 <->\n"
            "LISTEN 0 5 169.254.0.21:8081 0.0.0.0:* uid:1001 ino:3 sk:3 <->\n"
        ),
        "8082": "LISTEN 0 4096 *:8082 *:* ino:4 sk:4 v6only:0 <->\n",
        "8083": "LISTEN 0 5 0.0.0.0:8083 0.0.0.0:* uid:10020 ino:5 sk:5 <->\n",
        "8084": (
            "LISTEN 0 5 169.254.0.21:8084 0.0.0.0:* uid:1001 ino:6 sk:6 cgroup:/x uid:1002 <->\n"
        ),
    }
    for port, text in answers.items():
        (root / f"ss-{port}").write_text(text)
    shim_text = {
        "ss": '#!/bin/sh\ncat "$(dirname "$0")/../ss-${2#sport = :}"\n',
        "id": "#!/bin/sh\necho 1002\n",
        "node": "#!/bin/sh\nexit 0\n",
    }
    for name, text in shim_text.items():
        (shims / name).write_text(text)
        (shims / name).chmod(0o755)
    (root / "opt/run/preview").mkdir(parents=True)
    (root / "opt/run/preview/setup.done").write_text("")
    (root / "home/workspace").mkdir(parents=True)
    services = f"mapfile -d '' -t S < {PREVIEW_SERVICES}"
    assert services in PREVIEW_STATUS_SH
    inline = "S=(web 8080 / x api 8081 / x quiet 8082 / x other 8083 / x shaped 8084 / x)"
    done = run_script(root, PREVIEW_STATUS_SH.replace(services, inline), {})
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert "ready web" in lines
    assert "held api" in lines
    assert "held other" in lines, "uid 10020 is not the preview's 1002"
    assert "ready api" not in lines
    assert "held shaped" in lines, "a uid inside a later field is not the line's owner"
    assert "ready shaped" not in lines
    assert "ready quiet" not in lines and "held quiet" not in lines


def test_every_agent_launch_runs_with_a_group_writable_umask() -> None:
    for script in (TTY_RUNNER_SH, VIEW_RUNNER_SH):
        assert "umask 002" in script
