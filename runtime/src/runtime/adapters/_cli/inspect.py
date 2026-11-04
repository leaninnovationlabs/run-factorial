"""Open session on a host runtime (decision 57): a finished run's own CLI, resumed
on its session in its kept worktree, for a person to read.

The view is launched as `HostSession`'s resume launches the session, from the same `HostTui`
profile, but with the model credential alone: no GitHub token from the credentials handed in or
the worker's environment, no MCP server, no turn, and nothing typed into it but the Enter that
takes a CLI's consent screen. The gateway attaches to it read-only. Its hooks append nowhere,
so the run's signals stay the session's own. The run stays terminal: nothing here changes its
state.

One tmux server per run, its socket under `<local_root>/t/` by `_cli/tmux.py`'s rule, beside
the session's own. The view's record is `inspect.json` beside the run's state, and never holds
a secret. Its end waits for the CLI to exit, cuts the session log back to its length at the
launch, so what the view's CLI wrote there is never read as the session's, and deletes the
credential files the launch wrote. A resume or a rework's adoption ends it before they write
theirs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, SecretStr

from runtime.adapters._cli import tmux as tmux_
from runtime.adapters._cli.environment import GITHUB_KEY, view_git_env
from runtime.adapters._cli.host_session import (
    KeptRun,
    LaunchFiles,
    TuiLaunch,
    _hangup,
    _take_consent,
    _write_launch,
)
from runtime.adapters._cli.launch import BIN_DIRNAME, SIGNAL_SCRIPT, VIEW_ENV_FILE, launch_argv
from runtime.adapters._cli.paths import (
    CONFIG_DIRNAME,
    WORKTREE_DIRNAME,
    contained,
    delete_credentials,
)
from runtime.adapters._cli.tmux import TmuxServer
from runtime.contract import (
    AgentRuntimeError,
    InspectLoginRefused,
    InspectNoSession,
    InspectPhase,
    InspectState,
    InspectUnavailable,
    SpawnError,
    SpawnReason,
    TerminalTarget,
    UnknownRunError,
)

__all__ = ["HostInspect"]

log = logging.getLogger(__name__)

SUBSTRATE: Final = "host"
SESSION: Final = "inspect"
RECORD_FILE: Final = "inspect.json"
#: A record in these has a view the worker has not ended.
_LIVE: Final = frozenset({InspectPhase.STARTING, InspectPhase.OPEN})
#: Past its limit by this much, a view the worker has not stopped is stopped by its status read.
LIMIT_SLACK: Final = timedelta(minutes=1)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    phase: InspectPhase
    started_at: datetime
    deadline: datetime
    clients: int = 0
    last_activity: datetime | None = None
    detail: str | None = None
    #: The session log's size before the view's launch; None when there was no log.
    log_size: int | None = None

    def state(self, socket: str) -> InspectState:
        if self.phase is not InspectPhase.OPEN:
            return InspectState(
                phase=self.phase,
                token=self.token,
                last_activity=self.last_activity,
                started_at=self.started_at,
                detail=self.detail,
            )
        return InspectState(
            phase=self.phase,
            token=self.token,
            target=TerminalTarget(substrate=SUBSTRATE, socket=socket, session=SESSION),
            clients=self.clients,
            last_activity=self.last_activity,
            started_at=self.started_at,
        )


class HostInspect:
    """`runtime.contract.InspectHost` for a host adapter."""

    _now: Callable[[], datetime]
    _auth_mode: str

    def _require_root(self) -> Path:
        raise NotImplementedError

    def _kept_run(self, run_id: str) -> KeptRun:
        """`HostSession`'s."""
        raise NotImplementedError

    async def inspect_start(
        self,
        run_id: str,
        *,
        token: str,
        limit: timedelta,
        credentials: Mapping[str, SecretStr],
    ) -> InspectState:
        root = self._require_root()
        run = await asyncio.to_thread(self._kept_run, run_id)
        state = run.state
        if not state.status.is_terminal:
            raise InspectUnavailable(
                f"run {run_id}'s agent is still running; its session opens once it stops"
            )
        if state.continued_by is not None:
            raise InspectUnavailable(
                f"run {run_id}'s workspace was taken over by its rework, run {state.continued_by}"
            )
        if not state.began_session:
            raise InspectNoSession()
        worktree, config = await asyncio.to_thread(_confined, root, run)
        socket = _socket(root, run_id)
        record = await asyncio.to_thread(_read_record, run.run_dir)
        if record is not None and record.token == token and record.phase is InspectPhase.OPEN:
            return await self._read(run, record, socket)
        tmux = _tmux_binary()
        # One view per run: a new start replaces whatever the last one left.
        if record is not None:
            await self._end(run, record, socket, "replaced by another view")
        else:
            await _hangup(_server(tmux, root, socket, run), sweep=run.tui.sweep_descendants)
        server = _server(tmux, root, socket, run)
        env_file = config / VIEW_ENV_FILE
        rendered, argv = await asyncio.to_thread(
            self._launch, root, run, worktree, env_file, credentials
        )
        now = self._now()
        record = _Record(
            token=token,
            phase=InspectPhase.STARTING,
            started_at=now,
            deadline=now + limit,
            last_activity=now,
            log_size=await asyncio.to_thread(_log_size, root, run),
        )
        # Before the launch: the view's end cuts the log back to this, whoever ends it.
        await asyncio.to_thread(_write_record, run.run_dir, record)
        try:
            await asyncio.to_thread(
                _write_launch, root, turn=None, rendered=rendered, env_file=env_file
            )
            started = await server.start(worktree, argv)
            if started.returncode != 0:
                log.warning("tmux refused run %s's session view: %s", run_id, started.stderr[-400:])
                raise InspectUnavailable(f"tmux could not start run {run_id}'s session")
            await _take_consent(server, run.tui)
        except SpawnError as exc:
            await self._end(run, record, socket, str(exc))
            if exc.reason is SpawnReason.CREDENTIAL:
                raise InspectLoginRefused() from exc
            raise InspectUnavailable(str(exc)) from exc
        except BaseException:
            await self._end(run, record, socket, "the session view did not start")
            raise
        record = record.model_copy(update={"phase": InspectPhase.OPEN})
        await asyncio.to_thread(_write_record, run.run_dir, record)
        return await self._read(run, record, socket)

    def _launch(
        self,
        root: Path,
        run: KeptRun,
        worktree: Path,
        env_file: Path,
        credentials: Mapping[str, SecretStr],
    ) -> tuple[LaunchFiles, list[str]]:
        """The resume's launch and its pane command, with no GitHub credential from any source
        and no MCP server."""
        state, tui = run.state, run.tui
        assert state.session_id is not None
        cli = shutil.which(run.binary)
        if cli is None:
            raise InspectUnavailable(f"`{run.binary}` is not on the worker's PATH")
        config = env_file.parent
        secrets = {
            key: value.get_secret_value()
            for key, value in credentials.items()
            if not GITHUB_KEY.search(key)
        }
        auth_mode = state.auth_mode or self._auth_mode
        try:
            credential = tui.credential(secrets, auth_mode)
        except SpawnError as exc:
            raise InspectUnavailable(str(exc)) from exc
        rendered = tui.render(
            TuiLaunch(
                root=root,
                config_dir=config,
                worktree=worktree,
                workspace_real=os.path.realpath(worktree),
                config_real=os.path.realpath(config),
                signals=Path(os.devnull),
                signal_script=root / BIN_DIRNAME / SIGNAL_SCRIPT,
                secrets=secrets,
                auth_mode=auth_mode,
                credential=credential,
                credential_less=state.credential_less,
                config_sha=state.config_sha,
                bot_login=state.bot_login,
                mcp=(),
                system=None,
                git_dir=tui.git_dir(state),
            )
        )
        # A second lock: the view's environment holds no GitHub credential from any source.
        env = view_git_env(
            {key: value for key, value in rendered.env.items() if not GITHUB_KEY.search(key)}
        )
        flags = tui.view_flags(state, worktree=worktree)
        argv = tui.resume_argv(flags, state.session_id, None, (), binary=os.path.abspath(cli))
        launch = LaunchFiles(files=rendered.files, env=env, dirs=rendered.dirs)
        return launch, launch_argv(root, env_file, argv)

    async def inspect_status(self, run_id: str) -> InspectState:
        root = self._require_root()
        run = await asyncio.to_thread(self._kept_run, run_id)
        socket = _socket(root, run_id)
        record = await asyncio.to_thread(_read_record, run.run_dir)
        if record is None:
            return InspectState(phase=InspectPhase.STOPPED)
        if record.phase not in _LIVE:
            return record.state(socket)
        if self._now() >= record.deadline + LIMIT_SLACK:
            # A STARTING record this late is a launch whose worker died: it ends here too.
            return await self._end(run, record, socket, "stopped at its limit")
        if record.phase is InspectPhase.STARTING:
            return record.state(socket)
        return await self._read(run, record, socket)

    async def inspect_stop(self, run_id: str) -> None:
        root = self._require_root()
        run = await asyncio.to_thread(self._kept_run, run_id)
        record = await asyncio.to_thread(_read_record, run.run_dir)
        if record is None or record.phase not in _LIVE:
            return
        await self._end(run, record, _socket(root, run_id), "stopped")

    async def _end_inspect(self, run_id: str) -> None:
        """End the run's session view before a resume or an adoption writes its credential files
        again, whatever its record says, and raise unless it is gone."""
        root = self._require_root()
        run = await asyncio.to_thread(self._kept_run, run_id)
        tmux = shutil.which("tmux")
        try:
            socket = _socket(root, run_id)
        except InspectUnavailable:
            return  # no view can open under this root
        if tmux is None:
            return  # nor without tmux
        server = _server(tmux, root, socket, run)
        record = await asyncio.to_thread(_read_record, run.run_dir)
        if record is not None:
            await self._end(run, record, socket, "the agent resumed")
        else:
            await _hangup(server, sweep=run.tui.sweep_descendants)
            await asyncio.to_thread(_delete_credentials, root, run)
        if await server.has_session():
            raise AgentRuntimeError(f"run {run_id}'s session view would not end")

    async def _read(self, run: KeptRun, record: _Record, socket: str) -> InspectState:
        server = _server(_tmux_binary(), self._require_root(), socket, run)
        shown = None
        if await server.has_session():
            printed = await server.display("#{session_attached} #{session_activity}")
            shown = _shown(printed.stdout)
            if printed.returncode != 0 or shown is None:
                # Its session answered a moment ago: not proof that the view is gone.
                raise AgentRuntimeError(f"reading run {run.run_dir.name}'s session view failed")
        if shown is None:
            return await self._end(run, record, socket, "the session exited")
        clients, activity = shown
        now = self._now()
        seen = [at for at in (record.last_activity, min(activity, now)) if at is not None]
        record.last_activity = max([*seen, *([now] if clients else [])])
        record.clients = clients
        await asyncio.to_thread(_write_record, run.run_dir, record)
        return record.state(socket)

    async def _end(self, run: KeptRun, record: _Record, socket: str, detail: str) -> InspectState:
        """Hang the view up and wait for its CLI to exit, since a dying CLI still writes its
        config home and log; only then cut the log back and delete the credential files."""
        root = self._require_root()
        tmux = shutil.which("tmux")
        if tmux is not None:
            await _hangup(_server(tmux, root, socket, run), sweep=run.tui.sweep_descendants)
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(os.unlink, socket)
        live = record.phase in _LIVE
        if live and record.log_size is not None:
            await asyncio.to_thread(_cut_log, root, run, record.log_size)
        await asyncio.to_thread(_delete_credentials, root, run)
        if live:
            ended = {"phase": InspectPhase.STOPPED, "detail": detail, "clients": 0}
            record = record.model_copy(update=ended)
            await asyncio.to_thread(_write_record, run.run_dir, record)
        return record.state(socket)


def _socket(root: Path, run_id: str) -> str:
    return tmux_.socket_path(root, run_id, "i", InspectUnavailable)


def _server(tmux: str, root: Path, socket: str, run: KeptRun) -> TmuxServer:
    return TmuxServer(tmux, socket, root / tmux_.CONF_FILE, run.run_dir, SESSION)


def _confined(root: Path, run: KeptRun) -> tuple[Path, Path]:
    """The run's worktree and config home, resolved under `local_root`. The state file is the
    adapter's, but the agent could write it on a host, so it is checked rather than followed."""
    expected = run.workspace_dir / WORKTREE_DIRNAME
    if Path(run.state.worktree) != expected or expected.is_symlink():
        raise InspectUnavailable(f"run {run.run_dir.name}'s worktree is not its own")
    try:
        worktree = contained(root, expected, create=False)
    except AgentRuntimeError as exc:
        raise InspectUnavailable(f"run {run.run_dir.name}'s worktree escapes local_root") from exc
    if not worktree.is_dir():
        raise UnknownRunError(f"run {run.run_dir.name}'s worktree is gone")
    config = run.workspace_dir / CONFIG_DIRNAME
    if config.is_symlink() or not config.is_dir():
        raise UnknownRunError(f"run {run.run_dir.name}'s config home is gone, or is not its own")
    return worktree, config


def _delete_credentials(root: Path, run: KeptRun) -> None:
    """The session's credential files a view's launch wrote."""
    delete_credentials(root, run.workspace_dir, (*run.tui.credentials, VIEW_ENV_FILE))


def _session_log(root: Path, run: KeptRun) -> Path | None:
    state = run.state
    if state.session_id is None:
        return None
    config = run.workspace_dir / CONFIG_DIRNAME
    try:
        named = run.tui.session_log(config, state.session_id, state.session_log)
        return None if named is None else contained(root, named, create=False)
    except (ValueError, SpawnError):
        return None


def _log_size(root: Path, run: KeptRun) -> int | None:
    path = _session_log(root, run)
    try:
        return None if path is None else path.stat().st_size
    except FileNotFoundError:
        return None


def _cut_log(root: Path, run: KeptRun, size: int) -> None:
    """The session log back to `size`, unless it is shorter or gone."""
    path = _session_log(root, run)
    if path is None:
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("run %s's session log could not be cut back: %s", run.run_dir.name, exc)
        return
    try:
        if os.fstat(descriptor).st_size > size:
            os.ftruncate(descriptor, size)
    finally:
        os.close(descriptor)


def _tmux_binary() -> str:
    tmux = shutil.which("tmux")
    if tmux is None:
        raise InspectUnavailable("tmux is not on the worker's PATH")
    return tmux


def _shown(printed: str) -> tuple[int, datetime] | None:
    words = printed.split()
    if len(words) != 2 or not all(word.isdigit() for word in words):
        return None
    return int(words[0]), datetime.fromtimestamp(int(words[1]), UTC)


def _read_record(run_dir: Path) -> _Record | None:
    try:
        return _Record.model_validate_json((run_dir / RECORD_FILE).read_text("utf-8"))
    except FileNotFoundError:
        return None


def _write_record(run_dir: Path, record: _Record) -> None:
    target = run_dir / RECORD_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(record.model_dump_json(indent=2), "utf-8")
    os.replace(tmp, target)
