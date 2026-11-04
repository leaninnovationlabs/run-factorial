"""A host run: a CLI's TUI in its own tmux server under `local_root` (decision 56).

`HostSession` is mixed into a host adapter, whose `HostTui` profile is what differs between
CLIs: the argv, the files a launch writes, the keys `finish` types, the log's translator and the
refusal. Every launch renders the profile's files and environment again, from fresh secrets, so
nothing the agent wrote into its config home lasts into the next launch.

The contract's verbs (`status`, `result`, `logs`, `resume`, `cancel`, `interrupt`, `reap`,
`sweep_credentials`) and the park read a run's state on disk the same way for either CLI, so
they are `HostSession`'s too. Each adapter keeps `spawn`.

A spawn writes the run's environment to a 0600 file that `launch.sh` reads, deletes and execs
the CLI with, so no secret is on argv or in the tmux server's own environment
(`_cli/launch.py`). The CLI is the pane's process, and the session conf keeps its pane once it
dies, so the pane holds its pid and then its exit status.

`_HostSessionSupervisor` reads the run's hook signals and the session log from local files as
E2B's supervisor reads them from its sandbox (`_cli/session_reader.py`), and watches the pane
for the CLI's end. The adapter never times out a session: the worker owns its active time, and
a host has no cap.

A launch first takes the screen a CLI asks on at every start, if it has one (`HostTui.consent`).
A spawn of a CLI that names its own session returns only once the log it began says which.

`finish` types the CLI's quit command at its plain prompt and hangs up on a question tool's
dialog, where typing would answer it. `interrupt` and `cancel` hang the session up. For a CLI
that starts commands outside its process group, every end also ends their groups, read off
the live pane.

`resume` starts the run's tmux server again on the same worktree, the CLI resumed on its
session, after its session view is ended and fresh credentials are written: a new segment of
the same session, its cost counted from the running total.

A rework's or a retry's spawn with `resume_from` takes the earlier run's kept worktree and
config home over (`_adopt`): the branch the earlier run held is checked out again, for a rework
at the pull request's head with its uncommitted work stashed, for a retry at its local tip with
everything the attempt left kept; and the same session goes on in the new run's own tmux server,
its transcript, turns and cost starting where the earlier run's log ended. A run that held no
branch at its end is never continued (`NoBranchHeld`).

The tmux server outlives a worker restart under s6, and `status()` takes the session back
(`_readopt`): the run's `supervise.lock` lets one supervisor in any process read it, the mirrors
are cut back to the state and the translator rebuilt from them, and the server must still run
the run's own launch, whose pid is read off its pane, never off the state. A server gone with
no stop in flight fails the run `host_lost`; a finish or a stop already persisted keeps its
outcome.

`spawn_refusal` says whether this host can run a session at all: the session-log translator
is proven only against the pinned CLI (`HostTui.refusal`), and the tmux seam (`_cli/tmux.py`)
needs 3.2's `-N`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import fnmatch
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Final, NamedTuple, Protocol

from pydantic import SecretStr

from runtime.adapters._cli import mirror
from runtime.adapters._cli import tmux as tmux_
from runtime.adapters._cli.checkout import (
    Reapable,
    detach_head,
    end_park,
    held_branch,
    park_workspace,
    probe_git_at,
    reap_workspaces,
    sweep_credentials,
    worktree_git_at,
)
from runtime.adapters._cli.environment import (
    attachment_lines,
    git_env,
    pinned_config_sha,
    refuse_attachments,
)
from runtime.adapters._cli.escaped import end_marked, end_marked_any
from runtime.adapters._cli.launch import (
    BIN_DIRNAME,
    ENV_FILE,
    SIGNAL_SCRIPT,
    SIGNAL_SH,
    VIEW_ENV_FILE,
    launch_argv,
    launch_prefix,
    write_env_file,
    write_launch_files,
)
from runtime.adapters._cli.mirror import MAX_LINE, READ_LIMIT, TRANSLATOR_CHANGED
from runtime.adapters._cli.paths import (
    CLONES_DIRNAME,
    CONFIG_DIRNAME,
    RUNS_DIRNAME,
    WORKTREE_DIRNAME,
    contained,
    delete_credentials,
    existing_run_dir,
    is_run_id,
    path_slug,
    workspace_dir,
    write_attachments,
    write_new,
    write_private,
)
from runtime.adapters._cli.probes import ENV_BOT_LOGIN, discover_artifacts, expectation_verdict
from runtime.adapters._cli.process import Cli, wait_for_exit
from runtime.adapters._cli.process import pid_alive as _pid_alive
from runtime.adapters._cli.rework import (
    ReworkRefused,
    plain_branch,
    retry_checkout,
    rework_checkout,
)
from runtime.adapters._cli.session_reader import (
    LogSignals,
    SessionReader,
    SessionTranscript,
    TornPoll,
    login_refused,
    turn_cap_reason,
)
from runtime.adapters._cli.session_signals import CostState
from runtime.adapters._cli.session_state import (
    LOG_FILE,
    STATE_FILE,
    HostRunState,
    add,
    log_lines,
    merge_warnings,
    read_state,
)
from runtime.adapters._cli.session_state import finalise as _finalise
from runtime.adapters._cli.session_state import write_state as _write_state
from runtime.adapters._cli.tmux import PaneState, TmuxServer
from runtime.contract import (
    AdapterUnavailable,
    AgentRuntimeError,
    Artifact,
    IdleReason,
    McpServerSpec,
    MissingAttachment,
    NoBranchHeld,
    ParkReason,
    ResumeFrom,
    RunAttachment,
    RunHandle,
    RunResult,
    RunSpec,
    RunStatus,
    RunUsage,
    SessionState,
    SessionUnavailable,
    SpawnError,
    SpawnReason,
    TerminalTarget,
    UnknownRunError,
)

__all__ = [
    "SESSION_FILES",
    "TMUX_BIN",
    "HostCounter",
    "HostSession",
    "HostTui",
    "KeptRun",
    "LaunchFiles",
    "Tally",
    "TuiLaunch",
    "end_stray",
    "end_stray_session",
    "ended_config_home",
    "new_run_id",
    "sweep_ended",
    "turn_file",
]

log = logging.getLogger(__name__)

TMUX_BIN: Final = "tmux"
PROBE_TIMEOUT: Final = 30.0
#: The tmux session every run's server holds.
SESSION: Final = "run"
#: Beside the run: the file the hooks append to, and the raw mirrors of it and the session log.
SIGNALS_FILE: Final = "signals.jsonl"
SIGNALS_MIRROR: Final = "signals.mirror.jsonl"
SESSION_MIRROR: Final = "session.jsonl"
TURNS_DIRNAME: Final = "turns"
#: Beside the run: held by the one supervisor reading its session, in any process.
LOCK_FILE: Final = "supervise.lock"
#: What the reaper removes with a session's config home, or alone from a rework run's dir, which
#: holds none: the raw copies of what the agent's hooks and CLI wrote. The translated mirror and
#: the state stay.
SESSION_FILES: Final = (SIGNALS_FILE, SIGNALS_MIRROR, SESSION_MIRROR)
#: Reads after the CLI is gone, at most: an agent process can keep appending to its files.
FINAL_READS: Final = 8
#: A hung-up CLI's time to leave before its process group is killed.
GRACE: Final = 10.0
#: The tmux server is gone and the CLI left no exit status: the host restarted under it (a
#: container restart, a deploy's included). The `host_lost` code leads, for what matches it.
HOST_LOST: Final = (
    "host_lost: The session stopped because the server restarted (for example, during a "
    "deploy). Its work is kept."
)
#: How long `finish`, `interrupt` and `cancel` wait for the supervisor to finalise the run.
STOP_WAIT: Final = 120.0
#: Between `finish`'s keys, so the TUI reads each alone: an Esc right before a Ctrl-C is Meta.
KEY_GAP: Final = 0.3
#: For a CLI whose clear keys quit an idle session to leave on them (`HostTui.clear_quits`):
#: codex 0.159.0 in a Linux container takes about 5 s, and a quit typed into a TUI already on its
#: way out ends in the hangup's exit status instead of its own.
CLEAR_WAIT: Final = 10.0
#: For tmux to reap a pane's process it already shows dead (`PaneState.status`).
REAP_WAIT: Final = 5.0
REAP_GAP: Final = 0.05
#: At most, for the pasted quit command to be drawn on the input line: a slow host draws it late,
#: and a quit judged undrawn ends in the hangup, which leaves no exit code.
DRAW_WAIT: Final = 5.0
#: For the CLI to leave once Enter was pressed on its quit command.
EXIT_WAIT: Final = 10.0
#: For a launch's consent screen (`HostTui.consent`), and then for the prompt behind it.
CONSENT_WAIT: Final = 20.0
PROMPT_WAIT: Final = 10.0
#: For a CLI that names its own session to begin its log, once its consent is taken.
SESSION_ID_WAIT: Final = 30.0
#: Between two looks at a screen or a folder a launch waits on.
LOOK_GAP: Final = 0.25
_PARKED: Final = {
    ParkReason.USER: "Interrupted by a person. Its workspace is kept for a resume.",
    ParkReason.IDLE: "Paused while it waits for a person. Its workspace is kept for a resume.",
    ParkReason.ROLLOVER: "Paused to renew its token. Its workspace is kept for a resume.",
    ParkReason.CEILING: "Paused at its session time limit. Its workspace is kept for a resume.",
    ParkReason.EXITED: "Quit from its terminal. Its workspace is kept for a resume.",
}

#: Each binary's real path, device, inode, size, mtime and ctime: a replacement that keeps the
#: path, size and mtime (`cp -p`, `touch -r`) still changes the ctime, which no user can set.
_Identity = tuple[str, int, int, int, int, int]
_Installed = tuple[_Identity, _Identity]


class _RootRefused(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TuiLaunch:
    """What one launch of a session's CLI is rendered from, at every launch."""

    root: Path
    #: The config home the CLI runs from: a rework's run shares its workspace's.
    config_dir: Path
    worktree: Path
    #: The fully resolved worktree and config home, as the CLI will see them.
    workspace_real: str
    config_real: str
    #: The file the hooks append to, and the script they run.
    signals: Path
    signal_script: Path
    secrets: Mapping[str, str]
    auth_mode: str
    #: The one credential the CLI sees (`HostTui.credential`).
    credential: str
    credential_less: bool
    config_sha: str | None
    #: The run's bot login, which the agent's commits name (`agent_git_identity`).
    bot_login: str
    mcp: tuple[McpServerSpec, ...] | None
    #: The system prompt, for a CLI that takes it as config: a spawn and an adoption pass it,
    #: a resume None, since the session already holds it.
    system: str | None
    #: The worktree's administrative directory, for a CLI whose worktree has no `.git` pointer.
    git_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class LaunchFiles:
    """A launch's files, by path, written whole in this order, and the CLI's environment."""

    files: Mapping[Path, str]
    env: Mapping[str, str]
    #: Made before the files.
    dirs: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class Tally:
    """A finished segment's figures, as its CLI's stream reported them."""

    error: str | None
    summary: str
    turns: int
    #: What the stream never carried.
    missing: tuple[str, ...]
    #: The segment's own cost and tokens, by state field, added to the earlier segments'.
    counts: Mapping[str, Decimal | int | None] = field(default_factory=dict)


class HostCounter(Protocol):
    """What a session's supervisor reads of its CLI's stream reader while the CLI runs."""

    def feed(self, line: str) -> bool: ...

    @property
    def turns(self) -> int: ...

    @property
    def live_usage(self) -> RunUsage | None: ...

    def over_cap(self, cap: int | None) -> bool: ...


class HostTui(Protocol):
    """One CLI's part in a host session."""

    @property
    def name(self) -> str:
        """The CLI's name, for messages and task names."""
        ...

    @property
    def label(self) -> str:
        """The CLI as a person names it, for a run's failure reason."""
        ...

    @property
    def marker(self) -> str:
        """The variable naming the config home, by which `end_marked` finds what the run left
        running."""
        ...

    @property
    def credentials(self) -> tuple[str, ...]:
        """The credential files in the config home, deleted at every end."""
        ...

    @property
    def system_prompt_file(self) -> str | None:
        """The run's system prompt file, for a CLI that reads it from one."""
        ...

    @property
    def input_mark(self) -> str:
        """What the TUI's input line opens with."""
        ...

    @property
    def quit_command(self) -> str: ...

    @property
    def clear_keys(self) -> tuple[str, ...]:
        """What `finish` types first, each alone: a dialog closed, a draft emptied."""
        ...

    @property
    def clear_quits(self) -> bool:
        """Whether the clear keys alone quit a session idle at an empty input line, so `finish`
        waits `CLEAR_WAIT` for the CLI to leave before it types the quit command."""
        ...

    @property
    def consent(self) -> str | None:
        """A screen every launch shows before the session, which one Enter takes."""
        ...

    @property
    def sign_in(self) -> tuple[str, ...]:
        """What a launch shows instead of its session when the CLI refused the login it holds."""
        ...

    @property
    def turnless_hook(self) -> bool:
        """Whether a launch with no turn fires a hook that idles the session."""
        ...

    @property
    def names_git_dir(self) -> bool:
        """Whether its worktree has no `.git` pointer, so a launch names the administrative
        directory (`TuiLaunch.git_dir`)."""
        ...

    @property
    def sweep_descendants(self) -> bool:
        """Whether the CLI starts commands outside its process group, which a hangup's
        `killpg` cannot reach."""
        ...

    @property
    def transcript_version(self) -> int:
        """Its translator's version: a restart replays only through the one that wrote."""
        ...

    @property
    def log_signals(self) -> LogSignals:
        """What its session log says to `SessionSignals` when a hook did not fire."""
        ...

    @property
    def turnless_warnings(self) -> frozenset[str]:
        """Its reader's words for a segment with no turn in it."""
        ...

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> SessionTranscript:
        """The log's translator into the stream `reader` reads, for one segment."""
        ...

    def reader(self, prior_turns: int) -> HostCounter: ...

    def tally(
        self, reader: HostCounter, *, transcript: SessionTranscript, stop_summary: str | None
    ) -> Tally:
        """`stop_summary` is the last `Stop` hook's closing words."""
        ...

    def session_log(self, config_dir: Path, session_id: str, recorded: str | None) -> Path | None:
        """Where the CLI logs the session; None when it names the log only once it runs, so a
        spawn finds it (`discover`) and the run's state records it, as `recorded`. Raises
        `ValueError` for a recorded log the session cannot have written."""
        ...

    def discover(self, config_dir: Path, since: float) -> tuple[str, Path] | None:
        """The session a spawn started, and its log, once the CLI has named them."""
        ...

    def credential(self, secrets: Mapping[str, str], auth_mode: str) -> str:
        """The one credential the CLI sees, from the store."""
        ...

    def flags(
        self, spec: RunSpec, *, run_dir: Path, config_dir: Path, worktree: Path
    ) -> tuple[str, ...]:
        """The options a spawn and every resume repeat, and all a resume stores."""
        ...

    def resume_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        """`flags` again, for a resume of the run in `worktree`."""
        ...

    def view_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        """`resume_flags` for a read-only view of the session, which starts no MCP server."""
        ...

    def git_dir(self, state: HostRunState) -> Path | None:
        """The worktree's administrative directory a launch names (`TuiLaunch.git_dir`)."""
        ...

    def launch_words(self, state: HostRunState, home: Path) -> tuple[str, ...]:
        """Words the pane command of the run's current launch holds, which a launch of any
        other run's or session's does not: what a server is taken back by (`_ours`). `home` is
        the directory of the run's workspace, where its turn files are."""
        ...

    def turn_text(self, spec: RunSpec, attachment_paths: Mapping[str, str]) -> str:
        """The step's turn file."""
        ...

    def spawn_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        """The CLI on its first turn, the file at `turn`. `ValueError` for a path it cannot
        carry."""
        ...

    def resume_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str | None,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        """The session again, on the file at `turn`, or at its prompt with none."""
        ...

    def render(self, launch: TuiLaunch) -> LaunchFiles:
        """The launch's files and the CLI's whole environment. Called for every launch, never
        reused: the agent can write its config home."""
        ...

    def probe_env(self, secrets: Mapping[str, str], config_dir: Path) -> dict[str, str]:
        """The host's environment for discovery's git after a resume or an adoption."""
        ...

    async def refusal(self, binary: str, root: Path, *, within: float) -> str | None:
        """None when `binary` is the CLI the session was proven against. A version probe that
        cannot run, or does not answer `within` seconds, raises `AdapterUnavailable`."""
        ...


@dataclass(frozen=True, slots=True)
class KeptRun:
    """What the adapter's own state says about a run, as its session view needs it."""

    run_dir: Path
    state: HostRunState
    tui: HostTui
    #: The CLI as the adapter names it, for `shutil.which`.
    binary: str
    #: Where the worktree and config home are: `run_dir`, unless the run continues another
    #: run's workspace.
    workspace_dir: Path


@dataclass(frozen=True, slots=True)
class SessionTools:
    """What a spawn launches a session with, found before anything is created."""

    tmux: str
    cli: str
    socket: str


class _Read(NamedTuple):
    signals: bytes
    signals_size: int
    log: bytes
    #: None while the CLI has not created the log.
    log_size: int | None


class _Kept(NamedTuple):
    """What an earlier run's session lives in, as a rework finds it."""

    home: Path
    worktree: Path
    config: Path
    log: Path
    branch: str
    git_at: tuple[str, ...]
    #: The worktree's administrative directory, as the clone registers it.
    admin: Path


def new_run_id(spec: RunSpec) -> str:
    return f"{path_slug(spec.ticket)}-{path_slug(spec.agent)}-{uuid.uuid4().hex[:8]}"


def _identity(binary: str) -> _Identity:
    real = os.path.realpath(binary)
    try:
        stat = os.stat(real)
    except OSError as exc:
        raise AdapterUnavailable(f"cannot read {binary}: {exc}") from exc
    return real, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _installed(cli: str, tmux: str) -> _Installed:
    return _identity(cli), _identity(tmux)


def _root_refusal(root: Path) -> str | None:
    """A root whose session socket tmux or the gateway could not use, or whose paths an `@` in
    the TUI's prompt would cut short."""
    if any(char.isspace() for char in str(root)):
        return (
            f"local_root {str(root)!r} contains whitespace, where an `@` path in the TUI's "
            f"prompt ends: move local_root to a path without any"
        )
    try:
        tmux_.socket_path(root, "probe", "s", _RootRefused)
    except _RootRefused as exc:
        return f"{exc}: move local_root"
    return None


def _session_log(tui: HostTui, config_dir: Path, state: HostRunState) -> Path:
    """The run's session log: where the session id says, or where its spawn found it."""
    assert state.session_id is not None
    try:
        named = tui.session_log(config_dir, state.session_id, state.session_log)
    except ValueError as exc:
        raise AgentRuntimeError(f"run {state.run_id}'s session log: {exc}") from exc
    if named is None:
        raise AgentRuntimeError(f"run {state.run_id} never recorded its session log")
    return named


def _discovers(tui: HostTui, config_dir: Path, session_id: str) -> bool:
    """Whether the CLI names its own session, so a spawn learns it from the log it begins."""
    return tui.session_log(config_dir, session_id, None) is None


def _server(tmux: str, root: Path, socket: str, run_dir: Path) -> TmuxServer:
    return TmuxServer(tmux, socket, root / tmux_.SESSION_CONF_FILE, run_dir, SESSION)


def _begin_segment(state: HostRunState, *, turn: bool, version: int) -> None:
    """A launch of the session's CLI: its translator starts where the mirrors and the log stand,
    and its cost from the running total."""
    state.segments += 1
    state.segment_turn = turn
    state.segment_start = state.offset
    state.segment_log_start = state.log_read
    state.cost_base = state.cost_seen
    state.usage_base = state.live_usage
    state.transcript_version = version
    state.signals.start_segment(turn=turn)


def _requested(state: HostRunState) -> tuple[RunStatus, str]:
    """The outcome and the reason a stop someone asked for ends the run with."""
    if state.stopping is not RunStatus.INTERRUPTED:
        return RunStatus.CANCELLED, "cancelled by the operator"
    return RunStatus.INTERRUPTED, _PARKED[state.park_reason or ParkReason.USER]


def _report_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("supervising %s crashed", task.get_name(), exc_info=exc)


def _lock(run_dir: Path) -> int | None:
    """The run's supervise lock, or None while another supervisor holds it, in this process or
    another: a flock belongs to its open file, so a second open conflicts even here. Taken on
    the loop, never in a thread, so a cancelled caller cannot leave it held."""
    try:
        held = os.open(run_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise AdapterUnavailable(f"cannot open run {run_dir.name}'s supervise lock: {exc}") from exc
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(held)
        return None
    except BaseException:
        os.close(held)
        raise
    return held


def _launch_lock(run_dir: Path) -> int:
    if (held := _lock(run_dir)) is None:
        raise AdapterUnavailable(f"another supervisor reads run {run_dir.name}'s session")
    return held


def _stray_socket(root: Path, run_id: str) -> str | None:
    try:
        socket = tmux_.socket_path(root, run_id, "s", ValueError)
    except ValueError:
        return None
    return socket if os.path.lexists(socket) else None


async def end_stray(
    tui: HostTui, tmux: str, root: Path, run_id: str, *, sweep: bool = True
) -> None:
    """Hang up a run's session server, if one outlived the run, and remove its socket, which
    tmux leaves behind: a worker that died between a launch and its record leaves the CLI
    running with its key. The pid it signals is read off the live server, as every hangup's
    is, and a dead pane's never. Then, with `sweep`, end what the run left outside its process
    group, even when the hangup failed."""
    run_dir = root / RUNS_DIRNAME / run_id
    try:
        if (socket := _stray_socket(root, run_id)) is not None:
            await _hangup(_server(tmux, root, socket, run_dir), sweep=tui.sweep_descendants)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(socket)
    finally:
        if sweep and (config := await asyncio.to_thread(ended_config_home, root, run_dir)):
            await end_marked(tui.marker, str(config))


def ended_config_home(root: Path, run_dir: Path) -> Path | None:
    """The config home an ended run was the last to use: None while the run is not terminal, or
    once a rework continues it, whose own ends own that home now."""
    try:
        state = read_state(run_dir, run_dir.name)
    except Exception:
        return None
    if not state.status.is_terminal or state.continued_by is not None:
        return None
    return _config_home(root, run_dir, state)


def end_stray_session(tui: HostTui, tmux: str, root: Path, run_id: str) -> None:
    """`end_stray`'s hangup, for the startup sweep, which is not a coroutine and may be called
    from one, so it runs on a loop of its own. The sweep ends what runs left behind in one pass
    over the process table for them all (`sweep_ended`)."""
    if _stray_socket(root, run_id) is None:
        return
    with ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, end_stray(tui, tmux, root, run_id, sweep=False)).result()


def sweep_ended(tui: HostTui, homes: Sequence[Path]) -> int:
    """End what processes hold any of these ended runs' config homes, on a loop of its own."""
    if not homes:
        return 0
    with ThreadPoolExecutor(1) as pool:
        values = [str(home) for home in homes]
        return pool.submit(asyncio.run, end_marked_any(tui.marker, values)).result()


class HostSession:
    """The contract's verbs for a host adapter, over a run's tmux session and its state on disk,
    which read the same whichever CLI ran it."""

    _root: Path | None
    _cli: Cli
    _auth_mode: str
    _now: Callable[[], datetime]
    _poll: float
    #: The CLI's part, which each adapter names.
    _tui: HostTui
    #: The CLI version the adapter's PINNED CLI SURFACE was observed against.
    pinned_version: ClassVar[str]
    _supervisors: dict[str, asyncio.Task[None]]
    #: The session supervisors among them, whose state is the one they write.
    _sessions: dict[str, _HostSessionSupervisor]
    #: `HostInspect`'s, which a stub here would shadow.
    _end_inspect: Callable[[str], Awaitable[None]]
    supports_attachments: ClassVar[bool]
    #: The binaries the last full pass ran against. A refusal is never kept: it can be fixed.
    _session_passed: _Installed | None = None

    def _require_root(self) -> Path:
        raise NotImplementedError

    async def _launch_refusal(self) -> str | None:
        """Why this host cannot launch the CLI now, checked before a resume launches it."""
        return None

    def _existing_run_dir(self, run_id: str) -> Path:
        return existing_run_dir(self._require_root(), run_id)

    def _kept_run(self, run_id: str) -> KeptRun:
        root = self._require_root()
        run_dir = existing_run_dir(root, run_id)
        state = read_state(run_dir, run_id)
        home = workspace_dir(root, run_dir, state.workspace_of)
        return KeptRun(run_dir, state, self._tui, self._cli.agent_bin, home)

    @property
    def _credentials(self) -> tuple[str, ...]:
        """The run's credential files, deleted at every end, with a session view's env file,
        which a worker that died under an open view left."""
        return (*self._tui.credentials, VIEW_ENV_FILE)

    def _parse_reapable(self, run_dir: Path, raw: str) -> Reapable:
        """A run's state file, read as the reaper and the sweep need it."""
        state = HostRunState.model_validate_json(raw)
        runs = run_dir.parent
        credentials = self._credentials
        continuer = state.continued_by or ""
        if is_run_id(continuer) and (runs / continuer / STATE_FILE).is_file():
            # Its workspace's credentials are its continuer's now, which may still be running.
            credentials = ()
        try:
            home = workspace_dir(runs.parent, run_dir, state.workspace_of)
        except UnknownRunError:
            home = None
        return Reapable(
            state.run_id,
            state.status,
            state.repo,
            state.worktree,
            state.blind,
            credentials,
            home,
        )

    async def _discover_artifacts(
        self, state: HostRunState, env: Mapping[str, str]
    ) -> tuple[Artifact, ...]:
        git_at, git_at_env = await asyncio.to_thread(
            probe_git_at,
            self._require_root(),
            state.repo,
            state.worktree,
            state.git_dir,
            blind=state.blind,
            host=env.get("GIT_HTTP_HOST", ""),
        )
        return await discover_artifacts(
            self._cli, state, env, git_at=git_at, git_at_env=git_at_env
        )

    async def _park(self, state: HostRunState, outcome: RunStatus) -> tuple[str, ...]:
        """Called before the terminal status is written, so a run that reads terminal already
        holds no credential and no branch another attempt needs (`park_workspace`). Returns its
        stream warnings, and never raises: the terminal status must be written whatever fails
        here."""
        try:
            root = self._require_root()
            run_dir = contained(root, root / RUNS_DIRNAME / state.run_id, create=False)
            return await park_workspace(
                self._cli,
                root,
                run_dir,
                credentials=self._credentials,
                interrupted=outcome is RunStatus.INTERRUPTED,
                repo=state.repo,
                worktree=None if state.blind else Path(state.worktree),
                workspace_dir=workspace_dir(root, run_dir, state.workspace_of),
            )
        except Exception:
            log.exception("parking run %s failed", state.run_id)
            return ("its workspace could not be parked; the worker's log says why",)

    # -- contract ------------------------------------------------------------------------

    async def drain(self) -> None:
        """Wait for every live supervisor to finish.

        Not part of the contract, and no worker calls it: tests use it to wait for a run's
        terminal state instead of sleeping.
        """
        pending = [task for task in self._supervisors.values() if not task.done()]
        if pending:
            await asyncio.gather(*pending)
        self._supervisors = {
            run_id: task for run_id, task in self._supervisors.items() if not task.done()
        }

    async def status(self, run_id: str) -> RunStatus:
        run_dir = self._existing_run_dir(run_id)
        state = read_state(run_dir, run_id)
        if state.status.is_terminal:
            return state.status

        supervisor = self._supervisors.get(run_id)
        if supervisor is not None and not supervisor.done():
            # Ours and still finalising — its own terminal write is the authority.
            return state.status
        return await self._readopt(run_dir, run_id)

    async def result(self, run_id: str) -> RunResult:
        run_dir = self._existing_run_dir(run_id)
        state = read_state(run_dir, run_id)
        if not state.status.is_terminal:
            raise AgentRuntimeError(f"run {run_id} has not reached a terminal state yet")
        return RunResult(
            run_id=run_id,
            outcome=state.status,
            summary=state.summary,
            artifacts=state.artifacts,
            cost_usd=state.cost_usd,
            input_tokens=state.input_tokens,
            cached_input_tokens=state.cached_input_tokens,
            cache_write_input_tokens=state.cache_write_input_tokens,
            output_tokens=state.output_tokens,
            failure_reason=state.failure_reason,
            stream_warnings=state.stream_warnings,
            session_id=state.session_id,
            park_reason=state.park_reason if state.status is RunStatus.INTERRUPTED else None,
        )

    async def logs(self, run_id: str) -> AsyncIterator[str]:
        """The captured event stream, one JSON object per line — what the run actually did."""
        run_dir = self._existing_run_dir(run_id)
        for line in await asyncio.to_thread(log_lines, run_dir / LOG_FILE):
            yield line

    async def resume(
        self,
        run_id: str,
        turn: str | None,
        secrets: Mapping[str, SecretStr],
        attachments: tuple[RunAttachment, ...] = (),
        missing_attachments: tuple[MissingAttachment, ...] = (),
    ) -> RunHandle:
        """Continue an interrupted session in the workspace the interrupt kept, in a tmux server
        of its own. The run id, the run directory, the per-run config home and the worktree are
        reused, and the credential files the park deleted are rendered again from `secrets`.
        Refuses rather than falling back to a fresh run: a human who asked to resume did not
        ask to pay for a restart that may diverge."""
        if not self.supports_attachments:
            refuse_attachments(type(self).__name__, attachments)
        run_dir = self._existing_run_dir(run_id)
        state = read_state(run_dir, run_id)
        if (why := await self._launch_refusal()) is not None:
            raise SpawnError(why, reason=SpawnReason.CONFIG)
        values = {key: value.get_secret_value() for key, value in secrets.items()}
        return await self._resume_session(
            run_dir, state, turn, values, attachments, missing_attachments
        )

    async def interrupt(self, run_id: str, reason: ParkReason = ParkReason.USER) -> None:
        """Stop the run, keep its branch checked out for the resume (decision 26). The session
        hangs up exactly as `cancel` hangs it up: only the park and the reported outcome
        differ."""
        run_dir = self._existing_run_dir(run_id)
        state = read_state(run_dir, run_id)
        if state.status.is_terminal:
            return  # idempotent
        await self._stop_session(run_id, RunStatus.INTERRUPTED, reason)

    async def cancel(self, run_id: str) -> None:
        run_dir = self._existing_run_dir(run_id)
        state = read_state(run_dir, run_id)
        if state.status.is_terminal:
            return  # idempotent
        await self._stop_session(run_id, RunStatus.CANCELLED)

    async def reap(self, keep: Collection[str], *, retain: timedelta | None) -> tuple[str, ...]:
        """Remove the kept workspace of every terminal run `keep` does not name (decision 54).
        `retain` prunes E2B's transcript mirrors; a host run's transcript is its own `state.json`
        and stream, which stay."""
        root = self._require_root()
        reaped = await reap_workspaces(
            self._cli,
            root,
            keep,
            now=self._now(),
            state_file=STATE_FILE,
            parse=self._parse_reapable,
            session_files=SESSION_FILES,
        )
        if (tmux := shutil.which(TMUX_BIN)) is not None:
            for run_id in reaped:
                try:
                    await end_stray(self._tui, tmux, root, run_id)
                except Exception:
                    log.exception("ending what run %s left on its socket failed", run_id)
        return reaped

    def sweep_credentials(self) -> int:
        """Delete the credential files of every terminal run, hang up a finished session's
        server that outlived it, a spawn's that died before its first state write included, and
        end what ended runs left running. Called once at worker startup, before any tick
        (`runtime.contract.CredentialSweeper`)."""
        root = self._require_root()
        tmux = shutil.which(TMUX_BIN)
        homes: list[Path] = []

        def end_session(run_dir: Path) -> None:
            if tmux is not None:
                end_stray_session(self._tui, tmux, root, run_dir.name)
            # A home the reaper removed was swept as it went.
            if (home := ended_config_home(root, run_dir)) is not None and home.is_dir():
                homes.append(home)

        deleted = sweep_credentials(
            root,
            state_file=STATE_FILE,
            parse=self._parse_reapable,
            before_delete=end_session,
            stateless_credentials=self._tui.credentials,
        )
        sweep_ended(self._tui, homes)
        return deleted

    # -- session -------------------------------------------------------------------------

    async def spawn_refusal(self) -> str | None:
        """None when this host can run a session now. No `local_root` is left to `spawn`, which
        refuses it; a CLI that cannot be asked its version raises `AdapterUnavailable`."""
        root = self._root
        if root is None:
            return None
        tmux = shutil.which(TMUX_BIN)
        if tmux is None:
            return (
                "tmux is not on the worker's PATH, and a host session runs in tmux 3.2 or "
                "later: install it on the worker's host"
            )
        cli = shutil.which(self._cli.agent_bin)
        if cli is None:
            raise AdapterUnavailable(f"`{self._cli.agent_bin}` is not on PATH")
        installed = await asyncio.to_thread(_installed, cli, tmux)
        if installed == self._session_passed:
            return None
        refusal = (
            await _tmux_refusal(tmux)
            or _root_refusal(root)
            or await self._tui.refusal(cli, root, within=PROBE_TIMEOUT)
        )
        if refusal is None:
            self._session_passed = installed
        return refusal

    async def finish(self, run_id: str) -> None:
        """The quit command at a turn's end, a hangup on a question. Nothing while a turn runs, even
        after a finish that failed: a human just started it. Sent again by a call while the
        session still idles, which is the worker's one re-send. Raises only when it could not
        be sent; one that does not settle within `STOP_WAIT` leaves the session IDLE, for that
        re-send. On an interrupted run, ends its park (`_end_park`)."""
        run_dir = self._existing_run_dir(run_id)
        # Read with no await before the write below: a run finalised meanwhile would have its
        # result overwritten by the state read before it.
        if self._attached(run_id) is None and (ended := self._session(run_id)).status.is_terminal:
            if ended.status is RunStatus.INTERRUPTED and not ended.park_ended:
                await self._end_park(run_dir, ended)
            return
        if (attached := await self._attach(run_id)) is None:
            return
        live = attached._state
        if live.status.is_terminal:
            return
        if live.stopping is not None:
            return  # a stop asked for first decides the outcome
        reason = live.signals.idle_reason
        if reason is None:
            return
        # Before the keys: the CLI can be gone, and the run finalised, before they return.
        # Written on the loop, as the supervisor writes it: two writers would share its tmp file.
        live.finishing = True
        _write_state(run_dir, live)
        server = attached._server
        sweep = self._tui.sweep_descendants
        try:
            typing = live.signals.at_plain_prompt
            # Read while the CLI runs: once it has quit, what it left running belongs to init.
            family = await _family(server) if typing and sweep else frozenset()
            if not (typing and await _type_exit(server, self._tui)):
                if typing:
                    log.warning("run %s: its quit did not end the CLI, so finish hangs up", run_id)
                await _hangup(server, seen=attached._pid, sweep=sweep)
            else:
                await _end_groups(family)
        except Exception as exc:
            if await _landed(run_dir, run_id, server, attached):
                return
            raise AdapterUnavailable(f"cannot finish run {run_id}: {exc}") from exc
        try:
            await asyncio.wait_for(asyncio.shield(attached.task), timeout=STOP_WAIT)
        except TimeoutError:
            log.error("run %s did not finalise within %.0fs of its finish", run_id, STOP_WAIT)

    async def _end_park(self, run_dir: Path, state: HostRunState) -> None:
        """Park an interrupted run as any other end parks it, its outcome and result unchanged:
        the run was accepted without a resume, and the branch its worktree holds would refuse a
        later fresh attempt. Nothing for a run another continues, whose worktree is that run's.
        A HEAD that would not detach is logged, as `_park` logs it. The credentials went at the
        interrupt, and an open view's are its own."""
        if state.continued_by is not None:
            return
        try:
            await end_park(
                self._cli,
                self._require_root(),
                run_dir,
                repo=state.repo,
                worktree=None if state.blind else Path(state.worktree),
            )
        except Exception as exc:
            raise AdapterUnavailable(f"cannot end run {state.run_id}'s park: {exc}") from exc
        state.park_ended = True
        await asyncio.to_thread(_write_state, run_dir, state)

    async def _stop_session(
        self, run_id: str, outcome: RunStatus, reason: ParkReason = ParkReason.USER
    ) -> None:
        """Hang the session up, for `outcome`: persisted first, since the hangup may fail or
        the worker restart before it lands. Returns once the run is finalised."""
        run_dir = self._existing_run_dir(run_id)
        # As `finish` reads it, with no await before the write.
        if (attached := await self._attach(run_id)) is None:
            return  # idempotent
        live = attached._state
        if live.status.is_terminal:
            return
        if live.stopping is not RunStatus.INTERRUPTED:
            # An interrupt already asked for keeps the session resumable, whatever follows it.
            live.stopping = outcome
            if outcome is RunStatus.INTERRUPTED:
                live.park_reason = reason
        _write_state(run_dir, live)
        try:
            await _hangup(attached._server, seen=attached._pid, sweep=self._tui.sweep_descendants)
        except Exception as exc:
            if await _landed(run_dir, run_id, attached._server, attached):
                return
            raise AdapterUnavailable(f"cannot stop run {run_id}: {exc}") from exc
        # Returned only once the run is finalised and parked, so a resume right after never
        # races the park.
        try:
            await asyncio.wait_for(asyncio.shield(attached.task), timeout=STOP_WAIT)
        except TimeoutError as exc:
            log.error("run %s did not finalise within %.0fs of its stop", run_id, STOP_WAIT)
            raise AdapterUnavailable(
                f"run {run_id} did not finalise within {STOP_WAIT:.0f}s of its stop; the stop "
                "is retried"
            ) from exc

    def _attached(self, run_id: str) -> _HostSessionSupervisor | None:
        supervisor = self._sessions.get(run_id)
        if supervisor is None or supervisor.task.done():
            return None
        return supervisor

    async def _attach(self, run_id: str) -> _HostSessionSupervisor | None:
        """`_attached`, taking the session back first after a worker restart. None when the
        run turned out to have ended. Nothing is awaited between the taking back and the
        return, so a caller's next write reaches the state before the new supervisor's first
        poll: a stop it persists then decides a server found gone."""
        if (attached := self._attached(run_id)) is not None:
            return attached
        status = await self.status(run_id)
        if (attached := self._attached(run_id)) is not None:
            return attached
        if status.is_terminal:
            return None
        raise AdapterUnavailable(f"run {run_id}'s session is not supervised by this worker")

    async def session_state(self, run_id: str) -> SessionState:
        """From the state file alone: it never asks tmux."""
        state = await asyncio.to_thread(self._session, run_id)
        signals = state.signals
        idle = state.status is RunStatus.IDLE and signals.idle_since is not None
        asking = idle and signals.idle_reason is IdleReason.QUESTION
        failed = idle and signals.idle_reason is IdleReason.TURN_ERROR
        return SessionState(
            idle_reason=signals.idle_reason if idle else None,
            idle_since=signals.idle_since if idle else None,
            question=signals.question if asking else None,
            question_expires_at=signals.question_expires_at if asking else None,
            turn_error=signals.turn_error if failed else None,
            turns=state.session_turns,
            session_deadline=None,
            log_offset=state.log_offset,
            last_input_at=signals.last_input_at,
            session_id=state.session_id,
            usage=state.live_usage,
        )

    async def terminal_target(self, run_id: str) -> TerminalTarget:
        """The run's own socket: keyed by its id, so the same across its resumes."""
        await asyncio.to_thread(self._session, run_id)
        socket = tmux_.socket_path(self._require_root(), run_id, "s", AgentRuntimeError)
        return TerminalTarget(substrate=tmux_.SUBSTRATE, socket=socket, session=SESSION)

    def _session(self, run_id: str) -> HostRunState:
        return read_state(self._existing_run_dir(run_id), run_id)

    def _session_tools(self, root: Path, run_id: str) -> SessionTools:
        tmux = shutil.which(TMUX_BIN)
        if tmux is None:
            raise AdapterUnavailable("tmux is not on the worker's PATH")
        cli = shutil.which(self._cli.agent_bin)
        if cli is None:
            raise AdapterUnavailable(f"`{self._cli.agent_bin}` is not on PATH")
        try:
            socket = tmux_.socket_path(root, run_id, "s", ValueError)
        except ValueError as exc:
            raise SpawnError(str(exc), reason=SpawnReason.CONFIG) from exc
        return SessionTools(tmux=tmux, cli=os.path.abspath(cli), socket=socket)

    async def _spawn_session(
        self,
        spec: RunSpec,
        tools: SessionTools,
        *,
        run_id: str,
        run_dir: Path,
        worktree: Path,
        secrets: Mapping[str, str],
        probe_env: Mapping[str, str],
        attachments: Mapping[str, str],
        git_dir: Path | None = None,
        own: Mapping[str, Any] | None = None,
    ) -> RunHandle:
        """Launch the TUI on the spawn's checkout, its first turn in `turns/0.md`. `own` is the
        adapter's own fields of the run's state. Returns once the session is named: a CLI that
        names its own is waited for until its log says which it began."""
        root = self._require_root()
        tui = self._tui
        config_dir = run_dir / CONFIG_DIRNAME
        turn = turn_file(run_dir, 0)
        flags = tui.flags(spec, run_dir=run_dir, config_dir=config_dir, worktree=worktree)
        # A CLI that names its own session ignores this one.
        session_id = str(uuid.uuid4())
        env_file = config_dir / ENV_FILE
        try:
            argv = launch_argv(
                root,
                env_file,
                tui.spawn_argv(
                    flags, session_id, str(turn), tuple(attachments.values()), binary=tools.cli
                ),
            )
        except ValueError as exc:
            raise SpawnError(str(exc), reason=SpawnReason.CONFIG) from exc
        credential = tui.credential(secrets, self._auth_mode)
        config_sha = pinned_config_sha(spec.workspace)
        bot_login = secrets.get(ENV_BOT_LOGIN, "")
        # Read where the CLI runs, before it exists, so the seed trusts exactly what it will see.
        workspace_real = await asyncio.to_thread(os.path.realpath, worktree)
        rendered = tui.render(
            TuiLaunch(
                root=root,
                config_dir=config_dir,
                worktree=worktree,
                workspace_real=workspace_real,
                config_real=await asyncio.to_thread(os.path.realpath, config_dir),
                signals=run_dir / SIGNALS_FILE,
                signal_script=root / BIN_DIRNAME / SIGNAL_SCRIPT,
                secrets=secrets,
                auth_mode=self._auth_mode,
                credential=credential,
                credential_less=spec.workspace.credential == "none",
                config_sha=config_sha,
                bot_login=bot_login,
                mcp=spec.mcp,
                system=spec.prompts.system,
                git_dir=git_dir,
            )
        )
        server = _server(tools.tmux, root, tools.socket, run_dir)
        # Before the state exists: a status() elsewhere would otherwise take the run back.
        lock = _launch_lock(run_dir)
        try:
            await asyncio.to_thread(
                _write_launch,
                root,
                turn=(turn, tui.turn_text(spec, attachments)),
                rendered=rendered,
                env_file=env_file,
            )
            # The log's mtime is a wall-clock time.
            since = time.time()
            started = await server.start(worktree, argv)
            if not started.ok:
                raise AdapterUnavailable(
                    f"tmux could not start run {run_id}'s session: {started.stderr.strip()[:400]}"
                )
            pane = await server.pane()
            if pane is None:
                raise AdapterUnavailable(f"run {run_id}'s tmux server ended as it started")
            await _take_consent(server, tui)
            session_log = None
            if _discovers(tui, config_dir, session_id):
                session_id, found = await _discover(server, tui, config_dir, since)
                session_log = str(found)
            state = HostRunState(
                run_id=run_id,
                status=RunStatus.RUNNING,
                agent=spec.agent,
                ticket=spec.ticket,
                repo=spec.workspace.repo,
                branch=spec.workspace.branch,
                worktree=str(worktree),
                started_at=self._now(),
                session_id=session_id,
                max_turns=spec.max_turns,
                expected=spec.expected_artifacts,
                resume_args=flags,
                mcp=spec.mcp,
                config_sha=config_sha,
                auth_mode=self._auth_mode,
                credential_less=spec.workspace.credential == "none",
                bot_login=bot_login,
                workspace_real=workspace_real,
                session_log=session_log,
                **(own or {}),
            )
            _begin_segment(state, turn=True, version=tui.transcript_version)
            await asyncio.to_thread(_write_state, run_dir, state)
        except BaseException:
            os.close(lock)
            with contextlib.suppress(Exception):
                await _hangup(server, sweep=tui.sweep_descendants)
            await end_marked(tui.marker, str(config_dir))
            await asyncio.to_thread(delete_credentials, root, run_dir, tui.credentials)
            raise
        self._supervise_session(run_dir, state, server, probe_env, lock, pid=pane.pid)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    async def _resume_session(
        self,
        run_dir: Path,
        state: HostRunState,
        turn: str | None,
        secrets: Mapping[str, str],
        attachments: tuple[RunAttachment, ...],
        missing: tuple[MissingAttachment, ...],
    ) -> RunHandle:
        """The session again, in a new tmux server on its kept worktree: at its prompt when
        `turn` is None, else on `turns/<segment>.md`. A launch that fails leaves the run
        INTERRUPTED, holding no credential."""
        run_id = state.run_id
        if state.status is not RunStatus.INTERRUPTED:
            raise UnknownRunError(
                f"This run isn't paused, so there is no session to resume.\n\nRun {run_id} is "
                f"{state.status}."
            )
        if state.continued_by is not None:
            raise UnknownRunError(
                "This run's session was already continued by a later run.\n\nRun "
                f"{state.continued_by} continued it."
            )
        if not state.began_session:
            raise UnknownRunError("This run had no terminal session to resume.")
        assert state.session_id is not None
        if turn is None and attachments:
            raise AgentRuntimeError(
                f"run {run_id} resumes without a turn, so nothing would point at its images"
            )
        root = self._require_root()
        tui = self._tui
        tools = self._session_tools(root, run_id)
        worktree, config_dir = await asyncio.to_thread(_kept_worktree, root, run_dir, state)
        # Checked before anything is launched, rather than by the supervisor after. For a CLI
        # that names its own session, the log's name also proves the session id is one.
        _session_log(tui, config_dir, state)
        flags = tui.resume_flags(state, worktree=worktree)
        auth_mode = state.auth_mode or self._auth_mode
        credential = tui.credential(secrets, auth_mode)
        bot_login = secrets.get(ENV_BOT_LOGIN) or state.bot_login
        workspace_real = await asyncio.to_thread(os.path.realpath, worktree)
        rendered = tui.render(
            TuiLaunch(
                root=root,
                config_dir=config_dir,
                worktree=worktree,
                workspace_real=workspace_real,
                config_real=await asyncio.to_thread(os.path.realpath, config_dir),
                signals=run_dir / SIGNALS_FILE,
                signal_script=root / BIN_DIRNAME / SIGNAL_SCRIPT,
                secrets=secrets,
                auth_mode=auth_mode,
                credential=credential,
                credential_less=state.credential_less,
                config_sha=state.config_sha,
                bot_login=bot_login,
                mcp=state.mcp,
                system=None,
                git_dir=tui.git_dir(state),
            )
        )
        # Before any credential is written: the view's end deletes the credential files.
        await self._end_inspect(run_id)
        server = _server(tools.tmux, root, tools.socket, run_dir)
        await _hangup(server, sweep=tui.sweep_descendants)  # one a worker lost track of
        env_file = config_dir / ENV_FILE
        lock = _launch_lock(run_dir)
        try:
            paths = await asyncio.to_thread(write_attachments, run_dir, attachments)
            launched_turn = None
            if turn is not None:
                path = turn_file(config_dir.parent, state.segments)
                text = "\n".join([turn, *attachment_lines(attachments, missing, paths)])
                launched_turn = (path, text)
            argv = launch_argv(
                root,
                env_file,
                tui.resume_argv(
                    flags,
                    state.session_id,
                    None if launched_turn is None else str(launched_turn[0]),
                    tuple(paths.values()),
                    binary=tools.cli,
                ),
            )
            await asyncio.to_thread(
                _write_launch,
                root,
                turn=launched_turn,
                rendered=rendered,
                env_file=env_file,
            )
            started = await server.start(worktree, argv)
            if not started.ok:
                raise AdapterUnavailable(
                    f"tmux could not start run {run_id}'s session again: "
                    f"{started.stderr.strip()[:400]}"
                )
            pane = await server.pane()
            if pane is None:
                raise AdapterUnavailable(f"run {run_id}'s tmux server ended as it started")
            await _take_consent(server, tui)
            state.status = RunStatus.RUNNING
            state.exit_code = None
            state.failure_reason = None
            state.stopping = state.park_reason = None
            state.finishing = state.park_ended = False
            state.workspace_real = workspace_real
            state.bot_login = bot_login
            _begin_segment(state, turn=turn is not None, version=tui.transcript_version)
            if turn is None and not tui.turnless_hook:
                # Its consent saw it reach its prompt, the only word on that until a human types.
                state.signals.note_at_prompt(self._now())
            await asyncio.to_thread(_write_state, run_dir, state)
        except BaseException:
            os.close(lock)
            with contextlib.suppress(Exception):
                await _hangup(server, sweep=tui.sweep_descendants)
            await end_marked(tui.marker, str(config_dir))
            await asyncio.to_thread(delete_credentials, root, config_dir.parent, tui.credentials)
            raise
        # Discovery's git after this segment runs on the token this resume was handed: the
        # spawn's has expired by the time a rollover resumes.
        probe_env = tui.probe_env(secrets, config_dir)
        self._supervise_session(run_dir, state, server, probe_env, lock, pid=pane.pid)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    async def _adopt(
        self,
        spec: RunSpec,
        resume_from: ResumeFrom,
        *,
        secrets: Mapping[str, str],
        own: Mapping[str, Any] | None = None,
    ) -> RunHandle:
        """A rework or a retry continues `resume_from`'s session in that run's kept workspace
        (decision 56): the branch it held checked out again, at the pull request's head for a
        rework and at its local tip for a retry (`resume_from.mode`), and the session resumed
        with the step's prompt as its turn, in a tmux server of the new run's own, under a run
        that begins where the earlier one's log ended. `SessionUnavailable`, with nothing
        launched and the earlier run as it was, when there is no such session to continue.
        `own` is the adapter's own fields of the new run's state.

        What the launch is rendered from is the step's spec and what the host finds: the
        worktree, config home and session log where the earlier run's workspace must hold them,
        and the administrative directory the clone registers for that worktree, never the one
        the earlier run's state names."""
        root = self._require_root()
        tui = self._tui
        prev_dir, prev = await asyncio.to_thread(self._adoptable, spec, resume_from)
        seen = {prev.run_id}
        while prev.continued_by is not None and await asyncio.to_thread(
            self._exists, prev.continued_by
        ):
            # A rework names the step's latest attempt, so a continuer it does not name is one
            # whose spawn the orchestrator never recorded: a lost tick left it running in the
            # workspace, credentialled and unwatched. It is stopped, and its session continued.
            ghost = prev.continued_by
            if ghost in seen:
                raise SessionUnavailable(f"run {ghost}'s continuers name each other")
            seen.add(ghost)
            ghosted = ResumeFrom(run_id=ghost, session_id=resume_from.session_id)
            # Before it is stopped: the state naming it is the agent's to write.
            home = prev.workspace_of or prev.run_id
            await asyncio.to_thread(self._adoptable, spec, ghosted, ended=False, home=home)
            await self.cancel(ghost)
            prev_dir, prev = await asyncio.to_thread(self._adoptable, spec, ghosted)
        kept = await asyncio.to_thread(_kept_session, tui, root, prev_dir, prev, spec)
        git_dir = kept.admin if tui.names_git_dir else None
        fields = {**(own or {}), **({} if git_dir is None else {"git_dir": str(git_dir)})}
        # One that never wrote its state died in its own adoption: its CLI may still run.
        stale = {prev.run_id, prev.continued_by} - {None}
        run_id = new_run_id(spec)
        tools = self._session_tools(root, run_id)
        run_dir = contained(root, root / RUNS_DIRNAME / run_id, create=False)
        session_id = resume_from.session_id
        config_dir = kept.config
        turn = turn_file(kept.home, prev.segments)
        flags = tui.flags(spec, run_dir=run_dir, config_dir=config_dir, worktree=kept.worktree)
        credential = tui.credential(secrets, self._auth_mode)
        credential_less = spec.workspace.credential == "none"
        config_sha = pinned_config_sha(spec.workspace)
        bot_login = secrets.get(ENV_BOT_LOGIN) or prev.bot_login
        env_file = config_dir / ENV_FILE
        server = _server(tools.tmux, root, tools.socket, run_dir)
        await asyncio.to_thread(run_dir.mkdir, mode=0o700)
        # Before the state exists: a status() elsewhere would otherwise take the run back.
        lock = _launch_lock(run_dir)
        try:
            # From here a resume or a view of the earlier run is refused.
            prev.continued_by = run_id
            await asyncio.to_thread(_write_state, prev_dir, prev)
            # Before the checkout moves and any credential is written, which the view's end
            # would delete.
            await self._end_inspect(prev.run_id)
            for earlier in stale:
                await end_stray(tui, tools.tmux, root, earlier)
            # What a continuer that died in its adoption left running in the workspace.
            await end_marked(tui.marker, str(config_dir))
            clone = contained(root, root / CLONES_DIRNAME / spec.workspace.repo, create=False)
            glob = spec.workspace.branch.replace("{slug}", "*")
            checkout = (
                retry_checkout
                if resume_from.mode == "retry"
                else partial(rework_checkout, run_id=run_id)
            )
            header = await checkout(
                self._cli,
                kept.git_at,
                clone=clone,
                repo=spec.workspace.repo,
                branch=kept.branch,
                glob=glob,
                env=git_env(secrets),
                home=run_dir,
            )
            # Measured after the rework: the agent's git can still append to the session log,
            # which is the agent's own file, and those lines are not this run's.
            log_size = await asyncio.to_thread(_size, kept.log)
            if log_size is None:
                raise ReworkRefused(f"run {prev.run_id}'s session log is gone")
            paths = await asyncio.to_thread(write_attachments, run_dir, spec.attachments)
            try:
                argv = launch_argv(
                    root,
                    env_file,
                    tui.resume_argv(
                        flags, session_id, str(turn), tuple(paths.values()), binary=tools.cli
                    ),
                )
            except ValueError as exc:
                raise SpawnError(str(exc), reason=SpawnReason.CONFIG) from exc
            workspace_real = await asyncio.to_thread(os.path.realpath, kept.worktree)
            rendered = tui.render(
                TuiLaunch(
                    root=root,
                    config_dir=config_dir,
                    worktree=kept.worktree,
                    workspace_real=workspace_real,
                    config_real=await asyncio.to_thread(os.path.realpath, config_dir),
                    signals=run_dir / SIGNALS_FILE,
                    signal_script=root / BIN_DIRNAME / SIGNAL_SCRIPT,
                    secrets=secrets,
                    auth_mode=self._auth_mode,
                    credential=credential,
                    credential_less=credential_less,
                    config_sha=config_sha,
                    bot_login=bot_login,
                    mcp=spec.mcp,
                    system=spec.prompts.system,
                    git_dir=git_dir,
                )
            )
            await asyncio.to_thread(
                _write_adopted_files, run_dir, config_dir, spec, tui.system_prompt_file
            )
            await asyncio.to_thread(
                _write_launch,
                root,
                turn=(turn, f"{header}\n\n{tui.turn_text(spec, paths)}"),
                rendered=rendered,
                env_file=env_file,
            )
            started = await server.start(kept.worktree, argv)
            if not started.ok:
                raise AdapterUnavailable(
                    f"tmux could not start run {run_id}'s session: {started.stderr.strip()[:400]}"
                )
            pane = await server.pane()
            if pane is None:
                raise AdapterUnavailable(f"run {run_id}'s tmux server ended as it started")
            await _take_consent(server, tui)
            state = HostRunState(
                run_id=run_id,
                status=RunStatus.RUNNING,
                agent=spec.agent,
                ticket=spec.ticket,
                repo=spec.workspace.repo,
                branch=spec.workspace.branch,
                worktree=str(kept.worktree),
                started_at=self._now(),
                session_id=session_id,
                max_turns=spec.max_turns,
                expected=spec.expected_artifacts,
                resume_args=flags,
                mcp=spec.mcp,
                config_sha=config_sha,
                auth_mode=self._auth_mode,
                credential_less=credential_less,
                bot_login=bot_login,
                workspace_real=workspace_real,
                workspace_of=prev.workspace_of or prev.run_id,
                held_branch=kept.branch,
                segments=prev.segments,
                log_offset=log_size,
                log_read=log_size,
                cost_seen=prev.cost_seen,
                session_log=prev.session_log,
                **fields,
            )
            _begin_segment(state, turn=True, version=tui.transcript_version)
            await asyncio.to_thread(_write_state, run_dir, state)
        except BaseException as exc:
            os.close(lock)
            refused = await self._unadopt(server, prev_dir, prev, run_dir, kept, exc)
            if refused is None:
                raise
            raise refused from exc
        probe_env = tui.probe_env(secrets, config_dir)
        self._supervise_session(run_dir, state, server, probe_env, lock, pid=pane.pid)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    def _adoptable(
        self,
        spec: RunSpec,
        resume_from: ResumeFrom,
        *,
        ended: bool = True,
        home: str | None = None,
    ) -> tuple[Path, HostRunState]:
        """The earlier run, read from disk, or `SessionUnavailable` saying why it cannot be
        continued. Its workspace is not looked at, only, given `home`, whose workspace it names.
        `ended=False` asks the same of a continuer still running, before it is stopped."""
        run_id = resume_from.run_id
        try:
            prev_dir = self._existing_run_dir(run_id)
            prev = read_state(prev_dir, run_id)
        except (AgentRuntimeError, ValueError) as exc:
            raise SessionUnavailable(f"there is no run {run_id} to continue: {exc}") from exc
        workspace = spec.workspace
        why = None
        if (prev.repo, prev.ticket, prev.agent) != (workspace.repo, spec.ticket, spec.agent):
            why = f"is {prev.agent} on {prev.repo} for {prev.ticket}, not this run's"
        elif ended and not prev.status.is_terminal:
            why = f"is {prev.status}, not ended"
        elif not prev.began_session or prev.session_id != resume_from.session_id:
            why = f"has no session {resume_from.session_id}"
        elif home is not None and (prev.workspace_of or prev.run_id) != home:
            why = f"works in run {prev.workspace_of or prev.run_id}'s workspace, not run {home}'s"
        if why is not None:
            raise SessionUnavailable(f"run {run_id} {why}, so its session cannot be continued")
        return prev_dir, prev

    def _exists(self, run_id: str) -> bool:
        if not is_run_id(run_id):
            return False
        try:
            return (self._existing_run_dir(run_id) / STATE_FILE).is_file()
        except AgentRuntimeError:
            return False

    async def _unadopt(
        self,
        server: TmuxServer,
        prev_dir: Path,
        prev: HostRunState,
        run_dir: Path,
        kept: _Kept,
        exc: BaseException,
    ) -> AgentRuntimeError | None:
        """Undo an adoption that failed before its run existed: no CLI of it running, none of its
        credentials left, the branch released for a fresh attempt to check out, and the earlier
        run continuable as it was. Returns what the spawn raises instead of `exc`, or None to
        raise `exc` itself."""
        root = self._require_root()
        with contextlib.suppress(Exception):
            await _hangup(server, sweep=self._tui.sweep_descendants)
        await end_marked(self._tui.marker, str(kept.config))
        await asyncio.to_thread(delete_credentials, root, kept.home, self._tui.credentials)
        if prev.status is not RunStatus.INTERRUPTED or prev.park_ended:
            # As its park left it: the rework may have checked the branch out again.
            why = await detach_head(self._cli, root, prev.repo, kept.worktree)
            if why is not None:
                log.warning("run %s: HEAD was not detached again: %s", prev.run_id, why)
        prev.continued_by = None
        await asyncio.to_thread(_write_state, prev_dir, prev)
        await asyncio.to_thread(shutil.rmtree, run_dir, ignore_errors=True)
        if isinstance(exc, ReworkRefused):
            return SessionUnavailable(f"run {prev.run_id}'s session cannot be continued: {exc}")
        if isinstance(exc, (SpawnError, AdapterUnavailable)) or not isinstance(exc, Exception):
            return None
        return AdapterUnavailable(f"continuing run {prev.run_id}'s session failed: {exc}")

    def _supervise_session(
        self,
        run_dir: Path,
        state: HostRunState,
        server: TmuxServer,
        probe_env: Mapping[str, str],
        lock: int,
        *,
        pid: int,
    ) -> None:
        """A launch's supervisor, holding the lock the launch took."""
        assert state.session_id is not None
        try:
            transcript = self._tui.transcript(
                state.session_id, turn=state.segment_turn, cost_base=state.cost_base, skip=0
            )
            supervisor = _HostSessionSupervisor(
                self, run_dir, state, server, probe_env, transcript, pid=pid
            )
        except BaseException:
            os.close(lock)
            raise
        self._start(supervisor, lock)

    async def _readopt(self, run_dir: Path, run_id: str) -> RunStatus:
        """Take back a session no supervisor in this process reads: after a worker restart, or
        after a poll that tore. Returns the persisted status, and the supervisor's first poll
        finds whether the session lives. A supervisor elsewhere holding the lock keeps the run,
        and this one reads its status. All on the loop, so nothing awaited here can be
        cancelled with the lock held, and a caller's next write reaches the state before the
        new supervisor's first poll."""
        tmux = shutil.which(TMUX_BIN)
        if tmux is None:
            raise AdapterUnavailable("tmux is not on the worker's PATH")
        root = self._require_root()
        socket = tmux_.socket_path(root, run_id, "s", AgentRuntimeError)
        lock = _lock(run_dir)
        if lock is None:
            return read_state(run_dir, run_id).status
        try:
            # Read under the lock: the last write of the supervisor that held it.
            state = read_state(run_dir, run_id)
            supervisor = None
            if not state.status.is_terminal and state.began_session:
                supervisor = self._rebuilt(run_dir, state, _server(tmux, root, socket, run_dir))
        except BaseException:
            os.close(lock)
            raise
        if supervisor is not None:
            self._start(supervisor, lock)
            return state.status
        os.close(lock)
        if state.status.is_terminal:
            return state.status
        # A run from before every run was a session, live past the deploy's guard: nothing reads
        # it, so it ends here rather than staying RUNNING for ever.
        parked = await self._park(state, RunStatus.FAILED)
        return _finalise(
            run_dir,
            state,
            RunStatus.FAILED,
            failure_reason="the run never began a session, so it cannot be taken back",
            stream_warnings=merge_warnings(state.stream_warnings, parked),
        ).status

    def _rebuilt(
        self, run_dir: Path, state: HostRunState, server: TmuxServer
    ) -> _HostSessionSupervisor:
        """A supervisor for a session taken back, as E2B's `E2bAdapter._supervisor` rebuilds
        one. Its pid is read off the pane, never the state: after a container restart a
        recorded pid can be anyone's. It holds no token, so it skips discovery."""
        assert state.session_id is not None
        tui = self._tui
        # The state is written after the mirrors, so a mirror can hold a read the state lost.
        mirror.truncate(run_dir / LOG_FILE, state.offset)
        mirror.truncate(run_dir / SESSION_MIRROR, state.log_read - state.log_offset)
        mirror.truncate(run_dir / SIGNALS_MIRROR, state.signals_offset)
        # The segment's lines so far rebuild the turn count, never count again.
        reader = tui.reader(state.turns)
        translated = mirror.read_slice(run_dir / LOG_FILE, state.segment_start, state.offset)
        replayed = 0
        for line in mirror.lines(translated):
            reader.feed(line)
            replayed += 1
        if state.transcript_version == tui.transcript_version:
            # The translator holds a message whose lines are still arriving: rebuilt from the
            # segment's raw lines, keeping only what the mirror does not hold yet.
            transcript = tui.transcript(
                state.session_id,
                turn=state.segment_turn,
                cost_base=state.cost_base,
                skip=replayed,
            )
            raw = mirror.read_slice(
                run_dir / SESSION_MIRROR,
                state.segment_log_start - state.log_offset,
                state.log_read - state.log_offset,
            )
            pending = [out for line in mirror.lines(raw) for out in transcript.feed(line)]
        else:
            transcript = tui.transcript(
                state.session_id,
                turn=state.segment_turn and replayed == 0,
                cost_base=state.cost_base,
                skip=min(replayed, 1),
            )
            pending = []
            state.transcript_version = tui.transcript_version
            state.stream_warnings = merge_warnings(state.stream_warnings, (TRANSLATOR_CHANGED,))
            _write_state(run_dir, state)
        supervisor = _HostSessionSupervisor(
            self, run_dir, state, server, None, transcript, pid=None, reader=reader
        )
        supervisor._emit(pending)
        return supervisor

    def _start(self, supervisor: _HostSessionSupervisor, lock: int) -> None:
        run_id = supervisor._state.run_id
        try:
            task = asyncio.create_task(supervisor.run(), name=f"{self._tui.name}-cli-{run_id}")
        except BaseException:
            os.close(lock)
            raise
        task.add_done_callback(_report_crash)
        # However the supervisor ends, a cancel before its first step included.
        task.add_done_callback(lambda _: os.close(lock))
        supervisor.task = task
        self._supervisors[run_id] = task
        self._sessions[run_id] = supervisor


def turn_file(home: Path, segment: int) -> Path:
    """A turn's file, beside the worktree the session runs in: a CLI is told to read it, and
    refuses a path outside its workspace, which a rework's own run directory is."""
    return home / TURNS_DIRNAME / f"{segment}.md"


def _config_home(root: Path, run_dir: Path, state: HostRunState) -> Path:
    """The config home the run's session launches with and logs under: its workspace's
    (`workspace_dir`), which a rework's run shares with the run that made it."""
    return workspace_dir(root, run_dir, state.workspace_of) / CONFIG_DIRNAME


def _kept_worktree(root: Path, run_dir: Path, state: HostRunState) -> tuple[Path, Path]:
    """The worktree and config home the run's session lives in, both still there: its own, or
    those of the run whose workspace a rework continues. The state file is the adapter's, but
    the agent can write it on a host, so it is checked rather than followed."""
    config = _config_home(root, run_dir, state)
    home = config.parent
    worktree = home / WORKTREE_DIRNAME
    if (
        home.is_symlink()
        or Path(state.worktree) != worktree
        or worktree.is_symlink()
        or not worktree.is_dir()
    ):
        raise UnknownRunError(f"run {state.run_id}'s worktree is gone, or is not its workspace's")
    if config.is_symlink() or not config.is_dir():
        raise UnknownRunError(f"run {state.run_id}'s config home is gone, or is not its own")
    return worktree, config


def _kept_session(
    tui: HostTui, root: Path, run_dir: Path, state: HostRunState, spec: RunSpec
) -> _Kept:
    """What the earlier run's session lives in, or `SessionUnavailable` saying what is gone."""
    try:
        worktree, config = _kept_worktree(root, run_dir, state)
    except UnknownRunError as exc:
        raise SessionUnavailable(f"{exc}, so its session cannot be continued") from exc
    try:
        log_path = _session_log(tui, config, state)
    except AgentRuntimeError as exc:
        raise SessionUnavailable(f"{exc}, so its session cannot be continued") from exc
    glob = spec.workspace.branch.replace("{slug}", "*")
    branch, git_at = state.held_branch, worktree_git_at(root, state.repo, worktree)
    if not log_path.is_file():
        why = "session log is gone"
    elif branch is None:
        raise NoBranchHeld(
            f"run {state.run_id}'s checkout held no branch at its end, so its session cannot be"
            " continued"
        )
    elif not plain_branch(branch) or not fnmatch.fnmatchcase(branch, glob):
        why = f"checkout held {branch!r}, not a branch matching {glob!r}"
    elif git_at is None:
        why = "worktree is not one the clone registers"
    else:
        admin = Path(git_at[git_at.index("--git-dir") + 1])
        return _Kept(worktree.parent, worktree, config, log_path, branch, git_at, admin)
    raise SessionUnavailable(f"run {state.run_id}'s {why}, so its session cannot be continued")


def _write_adopted_files(
    run_dir: Path, config_dir: Path, spec: RunSpec, system_prompt_file: str | None
) -> None:
    """The new run's system prompt, and the step's skills in place of the earlier run's."""
    if system_prompt_file is not None:
        write_private(run_dir / system_prompt_file, spec.prompts.system)
    skills = config_dir / "skills"
    if skills.is_symlink() or skills.is_file():
        skills.unlink()
    elif skills.exists():
        shutil.rmtree(skills)
    for skill in spec.skills:
        folder = skills / skill.name
        folder.mkdir(parents=True, exist_ok=True)
        write_new(folder / "SKILL.md", skill.content.encode(), 0o644)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _write_launch(
    root: Path,
    *,
    turn: tuple[Path, str] | None,
    rendered: LaunchFiles,
    env_file: Path,
) -> None:
    """Every file a launch reads, the env file last: it is the one the pane deletes. Each is
    replaced whole, never written through a link the agent left in its place."""
    if turn is not None:
        path, text = turn
        path.parent.mkdir(mode=0o700, exist_ok=True)
        write_private(path, text)
    for folder in rendered.dirs:
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            folder.unlink()
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
    for path, text in rendered.files.items():
        write_private(path, text)
    tmux_.prepare(root)
    write_launch_files(root, {SIGNAL_SCRIPT: SIGNAL_SH})
    write_env_file(env_file, rendered.env)


def _read(signals: Path, signals_from: int, log_path: Path, log_from: int, limit: int) -> _Read:
    signals_chunk, signals_size = _read_from(signals, signals_from, limit)
    log_chunk, log_size = _read_from(log_path, log_from, limit)
    return _Read(signals_chunk, signals_size or 0, log_chunk, log_size)


def _read_from(path: Path, start: int, limit: int) -> tuple[bytes, int | None]:
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size <= start:
                return b"", size
            handle.seek(start)
            return handle.read(limit), size
    except FileNotFoundError:
        return b"", None


class _HostSessionSupervisor(SessionReader[HostRunState, HostCounter]):
    """Mirrors and translates one segment of a session, sets RUNNING or IDLE from its signals,
    enforces the turn cap, and finalises the run once the CLI is gone."""

    _signals_mirror = SIGNALS_MIRROR
    _session_mirror = SESSION_MIRROR
    _translated_mirror = LOG_FILE

    def __init__(
        self,
        adapter: HostSession,
        run_dir: Path,
        state: HostRunState,
        server: TmuxServer,
        probe_env: Mapping[str, str] | None,
        transcript: SessionTranscript,
        *,
        pid: int | None,
        reader: HostCounter | None = None,
    ) -> None:
        assert state.session_id is not None
        tui = adapter._tui
        self._adapter = adapter
        self._tui = tui
        self._cli_label = tui.label
        self._turnless = tui.turnless_warnings
        self._parked_reasons = _PARKED
        #: The CLI's pid as this process read it off the live pane, which a lost server's
        #: final reads wait for: hung up, it is still writing its cost.
        self._pid = pid
        #: Whether the server on the run's socket is known to run its launch: one this process
        #: started is, one taken back is asked once.
        self._proven = pid is not None
        self.task: asyncio.Task[None]
        self._run_dir = run_dir
        self._state = state
        self._server = server
        #: The run's host environment, for discovery's git: never written, since it holds the
        #: token. None for a run this process did not launch, which skips discovery.
        self._probe_env = probe_env
        self._transcript = transcript
        self._log_signals = tui.log_signals
        self._worktree = Path(state.worktree)
        self._reader = reader or tui.reader(state.turns)
        self._limit = self._read_limit = READ_LIMIT
        self._max_line = MAX_LINE
        self._override = None
        self._signals = run_dir / SIGNALS_FILE
        #: The config home the session launches from and logs under: a rework's run shares its
        #: workspace's.
        self._config = _config_home(adapter._require_root(), run_dir, state)
        self._log = _session_log(tui, self._config, state)
        if _discovers(tui, self._config, state.session_id):
            # A CLI that names its own session keeps it for the session's life; a hook naming
            # another is another thread's, such as a sub-agent's.
            self._hook_session = state.session_id

    async def run(self) -> None:
        pane: PaneState | None = None
        while True:
            try:
                more = await self._poll()
                if self._override is None:
                    await self._enforce()
                if more:
                    continue
                pane = await self._server.pane()
                if pane is not None and not self._proven:
                    pane = await self._ours(pane)
                if pane is not None and not pane.dead:
                    self._pid = pane.pid
                elif pane is None and self._pid is not None:
                    log.warning(
                        "run %s: its tmux server was gone, with no exit status", self._run_dir.name
                    )
                    await wait_for_exit(self._pid, _pid_alive, within=GRACE)
                if pane is not None and pane.dead:
                    pane = await _reaped(self._server, pane)
                if pane is None or pane.dead:
                    # The CLI is gone, so a line it left unterminated is as whole as it gets.
                    for _ in range(FINAL_READS):
                        if not await self._poll(last=True):
                            break
                    break
            except TornPoll:
                raise
            except Exception as exc:
                log.warning("polling session %s failed: %s", self._state.run_id, exc)
            await asyncio.sleep(self._adapter._poll)
        with contextlib.suppress(Exception):
            await _hangup(self._server, seen=self._pid, sweep=self._tui.sweep_descendants)
        # Before discovery, whose git carries the same marker.
        await end_marked(self._tui.marker, str(self._config))
        await self._finish(None if pane is None else pane.status, lost=pane is None)

    async def _ours(self, pane: PaneState) -> PaneState | None:
        """`pane` when its command is this run's launch; None, a lost session, when the socket
        is served by anything else: any process of the worker's user can serve on it.

        A consistency check, not a boundary: a process of the same user can forge it, killing
        the server and starting a look-alike launch with the words `HostTui.launch_words` names,
        from `state.json` or `ps`. Adopting that server gives it nothing past the same-user
        reach decision 16 and design §7.14 already accept."""
        shown = await self._server.display("#{pane_start_command}")
        if shown.returncode == -1:
            raise AgentRuntimeError(f"tmux on {self._server.socket} did not answer")
        prefix = launch_prefix(self._adapter._require_root(), self._config / ENV_FILE)
        words = shown.stdout.split()
        named = self._tui.launch_words(self._state, self._config.parent)
        self._proven = True
        if words[: len(prefix)] == prefix and named and all(word in words for word in named):
            return pane
        run_id = self._state.run_id
        log.warning("run %s: its socket serves something other than its session", run_id)
        return None

    async def _read(self) -> _Read:
        state = self._state
        return await asyncio.to_thread(
            _read, self._signals, state.signals_offset, self._log, state.log_read, self._limit
        )

    def _observed(self) -> datetime:
        return self._adapter._now()

    def _save(self) -> None:
        _write_state(self._run_dir, self._state)

    async def _enforce(self) -> None:
        cap = self._state.max_turns
        if self._reader.over_cap(cap):
            await self._stop(RunStatus.FAILED, turn_cap_reason(cap))

    async def _stop(self, outcome: RunStatus, reason: str) -> None:
        # The verdict is kept only once the hangup landed: one that failed is sent again next
        # poll, since the cap is judged again while no verdict is kept.
        await _hangup(self._server, seen=self._pid, sweep=self._tui.sweep_descendants)
        self._override = (outcome, reason)

    async def _finish(self, exit_code: int | None, *, lost: bool) -> None:
        adapter, state = self._adapter, self._state
        self._end_segment()
        tally = self._tui.tally(
            self._reader, transcript=self._transcript, stop_summary=state.signals.summary
        )
        error = tally.error
        status, reason = self._verdict(error, exit_code, lost=lost)
        artifacts = ()
        if self._probe_env is not None:
            artifacts = await adapter._discover_artifacts(state, self._probe_env)
            status, reason = expectation_verdict(
                state, status, reason, artifacts, env=self._probe_env
            )
        # Read before the park detaches HEAD: the branch a rework checks out again.
        held = await held_branch(adapter._cli, adapter._require_root(), state.repo, self._worktree)
        state.held_branch = held or state.held_branch
        # Parked before the status is written, so a run that reads terminal holds no credential.
        parked = await adapter._park(state, status)
        warned = self._session_warnings(status, exit_code, stream=tally.missing, error=error)
        # A resumed segment reports only itself, so the earlier segments' figures carry.
        counts = {name: add(getattr(state, name), value) for name, value in tally.counts.items()}
        _finalise(
            self._run_dir,
            state,
            status,
            failure_reason=reason,
            exit_code=exit_code,
            turns=tally.turns,
            summary=tally.summary,
            **counts,
            artifacts=artifacts,
            stream_warnings=merge_warnings(state.stream_warnings, (*warned, *parked)),
        )
        adapter._sessions.pop(state.run_id, None)

    def _verdict(
        self, error: str | None, exit_code: int | None, *, lost: bool
    ) -> tuple[RunStatus, str | None]:
        state = self._state
        if state.stopping is not None:
            return _requested(state)
        if self._override is not None:
            return self._override
        if lost and not state.finishing:
            return RunStatus.FAILED, HOST_LOST
        return self._session_verdict(error, exit_code)


async def _type_exit(server: TmuxServer, tui: HostTui) -> bool:
    """The quit command, typed as a person would, so the CLI leaves on its own and writes its
    cost. The clear keys go first, to close a dialog a human left open, where Enter would apply
    its highlighted choice, and to empty a draft the command would join. Enter is pressed only
    on an input line that reads the command alone. True once the CLI is gone; False leaves the
    session to the hangup."""
    for key in tui.clear_keys:
        if not await server.type_text(key, key=True):
            return False
        await asyncio.sleep(KEY_GAP)
    if tui.clear_quits and await _gone(server, within=CLEAR_WAIT):
        return True
    if not await server.type_text(tui.quit_command, bracketed=True):
        return False
    typed = re.compile(rf"{re.escape(tui.input_mark)} {re.escape(tui.quit_command)} *")

    def drawn(shown: str) -> bool:
        return any(typed.fullmatch(line) for line in shown.splitlines())

    if not drawn(screen := await _look(server, drawn, within=DRAW_WAIT)):
        log.warning("the quit command was not drawn; the screen ends %r", screen[-300:])
        return False
    if not await server.type_text("\r", key=True):
        return False
    if not await _gone(server, within=EXIT_WAIT):
        log.warning("the CLI was still there %ss after its quit command", EXIT_WAIT)
        return False
    return True


async def _take_consent(server: TmuxServer, tui: HostTui) -> None:
    """Take the screen a CLI asks on first at every launch (`HostTui.consent`) with one Enter,
    pasted alone; `SpawnError` naming what the screen shows when no such screen came, or the
    prompt did not follow it. Judged by the last input-mark line, which is the input line: a
    human's turns in the history above it carry the mark too, and a dialog draws its selected
    choice there."""
    consent = tui.consent
    if consent is None:
        return
    screen = await _look(server, lambda shown: consent in shown, within=CONSENT_WAIT)
    if consent not in screen:
        raise _unlaunched(tui, "put up no consent screen at its launch", screen)
    await asyncio.sleep(KEY_GAP)
    if not await server.type_text("\r", key=True):
        raise SpawnError(
            f"`{tui.name}`'s consent screen could not be answered",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    screen = await _look(server, lambda shown: _at_prompt(shown, tui), within=PROMPT_WAIT)
    if not _at_prompt(screen, tui):
        raise _unlaunched(tui, "did not reach its prompt once its consent screen was taken", screen)


def _at_prompt(screen: str, tui: HostTui) -> bool:
    # tmux drops a line's trailing blanks, so an empty input line reads as the mark alone.
    mark = tui.input_mark
    marked = [line for line in screen.splitlines() if line.split(" ", 1)[0] == mark]
    return bool(marked) and re.match(rf"{re.escape(mark)} \d+\. ", marked[-1]) is None


async def _look(server: TmuxServer, seen: Callable[[str], bool], *, within: float) -> str:
    """The pane's screen once `seen` holds of it, or as it last stood after `within` seconds,
    or once the pane is gone."""
    screen = ""
    for _ in range(max(1, round(within / LOOK_GAP))):
        shown = await server.capture()
        if shown is None:
            return screen
        screen = shown
        if seen(screen):
            return screen
        pane = await server.pane()
        if pane is None or pane.dead:
            return screen
        await asyncio.sleep(LOOK_GAP)
    return screen


async def _discover(
    server: TmuxServer, tui: HostTui, config_dir: Path, since: float
) -> tuple[str, Path]:
    """The session a CLI that names its own began, and its log, once the log names it;
    `SpawnError` naming what the screen shows when none comes within `SESSION_ID_WAIT`."""
    why = f"began no session log within {SESSION_ID_WAIT:.0f}s of its launch"
    for _ in range(round(SESSION_ID_WAIT / LOOK_GAP)):
        if (found := await asyncio.to_thread(tui.discover, config_dir, since)) is not None:
            return found
        pane = await server.pane()
        if pane is None or pane.dead:
            why = "left before it began a session log"
            break
        await asyncio.sleep(LOOK_GAP)
    raise _unlaunched(tui, why, await server.capture() or "")


def _unlaunched(tui: HostTui, why: str, screen: str) -> SpawnError:
    """A launch that did not reach its session, named by its screen's first line of text, which
    is a dialog its seeds did not answer."""
    shown = "nothing"
    for line in screen.splitlines():
        text = line.strip(" \t\u2500\u2502\u256d\u256e\u256f\u2570")
        if any(char.isalnum() for char in text):
            shown = repr(text[:200])
            break
    if any(screen_text in screen for screen_text in tui.sign_in):
        # No retry signs it in, and an INFRASTRUCTURE spawn error is retried every tick unseen.
        return SpawnError(
            f"{login_refused(tui.label)}\n\n`{tui.name}` {why}; its screen shows {shown}.",
            reason=SpawnReason.CREDENTIAL,
        )
    return SpawnError(
        f"`{tui.name}` {why}; its screen shows {shown}", reason=SpawnReason.INFRASTRUCTURE
    )


async def _reaped(server: TmuxServer, pane: PaneState) -> PaneState:
    """A dead `pane` once tmux has its process's exit status or signal, or as it stands after
    `REAP_WAIT`, which a signal's end under tmux 3.2 always waits out."""
    if pane.status is None and pane.signal is None:
        await _nudge(server, pane)
    for _ in range(round(REAP_WAIT / REAP_GAP)):
        if pane.status is not None or pane.signal is not None:
            break
        await asyncio.sleep(REAP_GAP)
        if (now := await server.pane()) is None:
            break
        pane = now
    if pane.status is None and pane.signal is None:
        log.warning("tmux gave a dead pane no exit status within %ss", REAP_WAIT)
    return pane


async def _nudge(server: TmuxServer, pane: PaneState) -> None:
    """Send SIGCHLD to the tmux server when it is the parent of the dead pane's process. A tmux
    built with utempter, as Ubuntu's and Debian's are, sets SIGCHLD to its default while
    utempter's helper runs on a dead pane's terminal, and the kernel discards a SIGCHLD sent
    then: a CLI that exits in that window stays a zombie the server never waits for, so the pane
    has no exit status. Any later SIGCHLD has the server wait for every child. The helper has
    returned before the server shows the pane dead, so this one is never discarded."""
    parent = await server.pid()
    if parent is None or parent <= 1 or parent == os.getpid():
        return
    rows = await asyncio.to_thread(_process_table)
    if (pane.pid, parent) in {(pid, ppid) for pid, ppid, _ in rows}:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(parent, signal.SIGCHLD)


async def _gone(server: TmuxServer, *, within: float) -> bool:
    for _ in range(round(within / 0.25)):
        pane = await server.pane()
        if pane is None or pane.dead:
            return True
        await asyncio.sleep(0.25)
    return False


async def _landed(
    run_dir: Path, run_id: str, server: TmuxServer, supervisor: _HostSessionSupervisor
) -> bool:
    """Whether a stop that failed ended the session anyway: the supervisor's own end can kill
    the server under it. A CLI still running was not ended, which fails at once. Otherwise the
    run must be finalised, which a supervisor that died never does."""
    with contextlib.suppress(Exception):
        pane = await server.pane()
        if pane is not None and not pane.dead:
            return False
    await asyncio.wait({supervisor.task}, timeout=STOP_WAIT)
    state = await asyncio.to_thread(read_state, run_dir, run_id)
    return state.status.is_terminal


async def _hangup(server: TmuxServer, *, seen: int | None = None, sweep: bool = False) -> None:
    """End the session as a closed terminal does: the CLI gets SIGHUP and writes its cost on the
    way out. Then its process group, for what the agent left running in it, such as a job that
    ignores the hangup and keeps the CLI's environment, and with `sweep` the groups of what it
    started outside its own (`HostTui.sweep_descendants`). Raises when tmux would not end a
    session whose CLI still runs.

    The group is signalled only by a pid read off a live pane, now or, as `seen`, earlier by
    this process: a dead pane's pid is free for any process to take, and one recorded before a
    restart may belong to anything by now. The CLI's descendants are read off a live pane only:
    once it is gone they belong to init."""
    pane = await server.pane()
    family: frozenset[_Member] = frozenset()
    if sweep and pane is not None and not pane.dead:
        family = await asyncio.to_thread(_descendants, pane.pid)
    killed = await server.kill()
    if pane is None:
        return
    if not killed.ok:
        now = await server.pane()
        if now is not None and not now.dead:
            raise AgentRuntimeError(
                f"tmux on {server.socket} would not end the session: {killed.stderr.strip()[:200]}"
            )
    await _end_cli_group(pane, seen)
    await _end_groups(family)


async def _family(server: TmuxServer) -> frozenset[_Member]:
    pane = await server.pane()
    if pane is None or pane.dead:
        return frozenset()
    return await asyncio.to_thread(_descendants, pane.pid)


async def _end_cli_group(pane: PaneState, seen: int | None) -> None:
    if pane.pid <= 1:
        return  # `killpg` of 0 is the worker's own group
    if not pane.dead:
        await wait_for_exit(pane.pid, _pid_alive, within=GRACE)
    elif pane.pid != seen:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pane.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        await wait_for_exit(pane.pid, _group_alive, within=1.0)


#: A process, by its pid and its process group.
_Member = tuple[int, int]


def _process_table() -> list[tuple[int, int, int]]:
    """Every process's pid, parent pid and process group; none when `ps` cannot say, since a
    hangup must go on without them."""
    try:
        shown = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            env={"PATH": "/bin:/usr/bin", "LANG": "C"},
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("reading the process table failed: %s", exc)
        return []
    rows: list[tuple[int, int, int]] = []
    for line in shown.stdout.splitlines():
        words = line.split()
        if len(words) == 3 and all(word.isdigit() for word in words):
            rows.append((int(words[0]), int(words[1]), int(words[2])))
    return rows


def _descendants(pid: int) -> frozenset[_Member]:
    """The processes below `pid` that sit outside its process group, which a `killpg` of it
    misses, and outside the worker's own."""
    rows = _process_table()
    children: dict[int, list[_Member]] = {}
    for child, parent, group in rows:
        children.setdefault(parent, []).append((child, group))
    spared = {pid, os.getpgrp(), 0, 1}
    found: set[_Member] = set()
    below = [pid]
    while below:
        for member in children.get(below.pop(), []):
            if member not in found:
                found.add(member)
                below.append(member[0])
    return frozenset(member for member in found if member[1] not in spared)


def _still(family: frozenset[_Member]) -> set[int]:
    """The groups of `family` with a member still running in them: a pid alone may be
    anyone's by now."""
    now = {(pid, group) for pid, _, group in _process_table()}
    return {group for _, group in family & now}


async def _end_groups(family: frozenset[_Member]) -> None:
    """SIGTERM each group of `family` still running, then SIGKILL what is left a second
    later."""
    if not family:
        return
    groups = await asyncio.to_thread(_still, family)
    if groups:
        log.warning("ending %d process group(s) a session left outside its own", len(groups))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, sig)
        for _ in range(10):
            if not groups:
                return
            await asyncio.sleep(0.1)
            groups = await asyncio.to_thread(_still, family)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _tmux_refusal(tmux: str) -> str | None:
    version = await tmux_.tmux_version(tmux)
    if version is not None and version >= tmux_.MIN_VERSION:
        return None
    named = "names no version" if version is None else "is {}.{}".format(*version)
    return (
        f"tmux at {tmux} {named}, and a host session needs tmux 3.2 or later: upgrade it on "
        "the worker's host"
    )
