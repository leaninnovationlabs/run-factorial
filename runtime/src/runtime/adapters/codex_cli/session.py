"""What a host session of the codex TUI is made of (decision 56): `CodexTui`, the profile
`_cli/host_session.py` launches, types into, reads and refuses the codex TUI by.

A session runs only the native binary at the pin. npm's `@openai/codex` is a node script, and a
SIGKILL of it leaves the native process it started running, holding the run's credential.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters._cli.environment import git_env
from runtime.adapters._cli.host_session import (
    HostCounter,
    LaunchFiles,
    Tally,
    TuiLaunch,
    turn_file,
)
from runtime.adapters._cli.launch import ENV_FILE
from runtime.adapters._cli.mirror import MAX_LINE
from runtime.adapters._cli.paths import CLONES_DIRNAME, HOME_DIRNAME
from runtime.adapters._cli.process import probe_output
from runtime.adapters._cli.session_reader import LogSignals, SessionTranscript
from runtime.adapters._cli.session_signals import CostState, json_object
from runtime.adapters._cli.session_state import HostRunState
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_BEDROCK,
    AUTH_MODE_SUBSCRIPTION,
    DEFAULT_SUBSCRIPTION_AUTH_FILE,
    bedrock_provider_config,
    require_api_key,
    require_bedrock_bearer,
    subscription_login,
)
from runtime.adapters.codex_cli.cli import (
    AUTH_FILE,
    CLEAR_KEYS,
    CONFIG_FILE,
    CONSENT_RESTRICTED,
    ENV_CODEX_HOME,
    FLAG_VERSION,
    INPUT_MARK,
    META_ID,
    PINNED_VERSION,
    QUIT_COMMAND,
    RECORD_SESSION_META,
    ROLLOUT_DIRNAME,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TYPE,
    SIGN_IN_SCREENS,
    VALUE_SANDBOX_WORKSPACE,
    VERSION_NAME,
)
from runtime.adapters.codex_cli.stream import NO_TURN, NO_USAGE, StreamReader
from runtime.adapters.codex_cli.transcript import (
    TRANSCRIPT_VERSION,
    RolloutTranscript,
    codex_log_signals,
)
from runtime.adapters.codex_cli.tui import (
    auth_document,
    pointer,
    resume_argv,
    signal_command,
    spawn_argv,
    thread_of,
    tui_config_toml,
    tui_environment,
    tui_flags,
    turn_text,
)
from runtime.contract import AdapterUnavailable, AgentRuntimeError, RunSpec, SpawnError, SpawnReason

__all__ = ["SESSION_CREDENTIALS", "CodexTui"]

#: A run's credential files in its config home, deleted at every end. `config.toml` is one: it
#: may hold rendered stdio MCP env, and every launch writes it again.
SESSION_CREDENTIALS: Final = (AUTH_FILE, CONFIG_FILE, ENV_FILE)
_SCRIPT_MAGIC: Final = b"#!"
#: A worktree's administrative directory is `<clone>/.git/worktrees/<name>`.
GIT_DIRNAME: Final = ".git"
GIT_WORKTREES_DIRNAME: Final = "worktrees"


def _is_script(binary: str) -> bool:
    try:
        with open(binary, "rb") as handle:
            return handle.read(len(_SCRIPT_MAGIC)) == _SCRIPT_MAGIC
    except OSError as exc:
        raise AdapterUnavailable(f"cannot read {binary}: {exc}") from exc


def _resolves_under(path: Path, folder: Path) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)


def _writable_root(launch: TuiLaunch) -> Path:
    """The one root the agent's commands may write beside its worktree: the shared clone's git
    directory, which a commit writes to. The run's state, its config home and the scripts its
    hooks run all sit elsewhere under `local_root`, out of the agent's reach, so a resume may
    trust them."""
    git_dir = launch.git_dir
    clones = launch.root / CLONES_DIRNAME
    if git_dir is not None and git_dir.parent.name == GIT_WORKTREES_DIRNAME:
        common = git_dir.parent.parent
        if common.name == GIT_DIRNAME and _resolves_under(common, clones):
            return common
    raise AgentRuntimeError(
        f"a codex session needs its worktree's administrative directory in a clone under "
        f"{clones}, not {git_dir}"
    )


class CodexTui:
    """`HostTui` for `codex`."""

    name: str = "codex"
    label: str = "Codex CLI"
    marker: str = ENV_CODEX_HOME
    credentials: tuple[str, ...] = SESSION_CREDENTIALS
    #: The system prompt is config (`developer_instructions`).
    system_prompt_file: str | None = None
    input_mark: str = INPUT_MARK
    quit_command: str = QUIT_COMMAND
    clear_keys: tuple[str, ...] = CLEAR_KEYS
    clear_quits: bool = True
    consent: str | None = CONSENT_RESTRICTED
    sign_in: tuple[str, ...] = SIGN_IN_SCREENS
    #: codex runs its SessionStart hooks only as a turn starts.
    turnless_hook: bool = False
    #: The worktree has no `.git` pointer (`drop_git_pointer`).
    names_git_dir: bool = True
    #: Each command runs in a session of its own (unified exec), outside codex's group.
    sweep_descendants: bool = True
    transcript_version: int = TRANSCRIPT_VERSION
    turnless_warnings: frozenset[str] = frozenset({NO_TURN, NO_USAGE})

    def __init__(
        self,
        subscription_auth_file: Path = DEFAULT_SUBSCRIPTION_AUTH_FILE,
        *,
        ask_tool: bool = False,
    ) -> None:
        self._subscription_auth_file = subscription_auth_file
        self._ask_tool = ask_tool

    @property
    def log_signals(self) -> LogSignals:
        return functools.partial(codex_log_signals, ask_tool=self._ask_tool)

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> RolloutTranscript:
        return RolloutTranscript(session_id, turn=turn, skip=skip)

    def reader(self, prior_turns: int) -> StreamReader:
        return StreamReader(prior_turns=prior_turns)

    def tally(
        self, reader: HostCounter, *, transcript: SessionTranscript, stop_summary: str | None
    ) -> Tally:
        assert isinstance(reader, StreamReader)
        assert isinstance(transcript, RolloutTranscript)
        # A segment whose turns all failed or were aborted carries no usage, and owes none.
        excused = () if transcript.usage_due else (NO_USAGE,)
        # A failed turn is the session's signals' to judge: a later turn can end normally. A
        # segment with no turn ended normally has no final message, which is no failure here.
        return Tally(
            error=None,
            summary=reader.summary or stop_summary or "",
            turns=reader.turns,
            missing=tuple(warning for warning in reader.missing() if warning not in excused),
            counts={
                "input_tokens": reader.input_tokens,
                "cached_input_tokens": reader.cached_input_tokens,
                "cache_write_input_tokens": reader.cache_write_input_tokens,
                "output_tokens": reader.output_tokens,
            },
        )

    def session_log(self, config_dir: Path, session_id: str, recorded: str | None) -> Path | None:
        """The rollout the spawn found, which must still be one of the thread's own under the
        config home, reached through no link."""
        if recorded is None:
            return None  # codex names its rollout only once it runs
        path = Path(recorded)
        sessions = config_dir / ROLLOUT_DIRNAME
        if thread_of(path) != session_id or path.parent.parent.parent.parent != sessions:
            raise ValueError(f"{recorded!r} is not thread {session_id}'s rollout")
        if path.is_symlink() or not _resolves_under(path.parent, sessions):
            raise ValueError(f"{recorded!r} is reached through a link")
        return path

    def discover(self, config_dir: Path, since: float) -> tuple[str, Path] | None:
        """The one rollout under `sessions/` written since `since`, its thread read from its
        name and confirmed by its first line. None while there is none, or its first line is
        not whole yet; `SpawnError` for two, or a first line naming another thread."""
        found: list[tuple[str, Path]] = []
        sessions = config_dir / ROLLOUT_DIRNAME
        for path in sorted(sessions.glob("*/*/*/rollout-*.jsonl")):
            thread = thread_of(path)
            try:
                if (
                    thread is not None
                    and not path.is_symlink()
                    and _resolves_under(path.parent, sessions)
                    and path.stat().st_mtime >= since
                ):
                    found.append((thread, path))
            except FileNotFoundError:
                continue
        if not found:
            return None
        if len(found) > 1:
            raise SpawnError(
                f"codex wrote {len(found)} rollouts as it started, so its thread is not known",
                reason=SpawnReason.INFRASTRUCTURE,
            )
        thread, path = found[0]
        with path.open("rb") as handle:
            first = handle.readline(MAX_LINE)
        if not first.endswith(b"\n"):
            return None
        record = json_object(first.decode("utf-8", "replace")) or {}
        payload = record.get(ROLLOUT_PAYLOAD)
        named = payload.get(META_ID) if isinstance(payload, Mapping) else None
        if record.get(ROLLOUT_TYPE) != RECORD_SESSION_META or named != thread:
            raise SpawnError(
                f"codex's rollout {path.name} does not open on its own thread's session_meta",
                reason=SpawnReason.INFRASTRUCTURE,
            )
        return thread, path

    def credential(self, secrets: Mapping[str, str], auth_mode: str) -> str:
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            return subscription_login(secrets, self._subscription_auth_file)
        if auth_mode == AUTH_MODE_BEDROCK:
            return require_bedrock_bearer(secrets)
        return require_api_key(secrets)

    def flags(
        self, spec: RunSpec, *, run_dir: Path, config_dir: Path, worktree: Path
    ) -> tuple[str, ...]:
        return tui_flags(spec.model, worktree=str(worktree), sandbox_mode=VALUE_SANDBOX_WORKSPACE)

    def resume_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        """Built again from the run's model and its checked worktree, never read back: a flag in
        the state file would start the next launch outside the sandbox."""
        return tui_flags(state.model, worktree=str(worktree), sandbox_mode=VALUE_SANDBOX_WORKSPACE)

    def view_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        """Its MCP servers are config, which a launch with none renders without them."""
        return self.resume_flags(state, worktree=worktree)

    def git_dir(self, state: HostRunState) -> Path | None:
        return None if state.git_dir is None else Path(state.git_dir)

    def launch_words(self, state: HostRunState, home: Path) -> tuple[str, ...]:
        """A spawn names no thread, which codex makes as it starts, so a launch is named by its
        turn file, which only this run's current segment has, and a resume's also by its
        thread."""
        assert state.session_id is not None
        resumed = state.segments > 1 or state.workspace_of is not None
        turn = str(turn_file(home, state.segments - 1))
        return (*((state.session_id,) if resumed else ()), *((turn,) if state.segment_turn else ()))

    def turn_text(self, spec: RunSpec, attachment_paths: Mapping[str, str]) -> str:
        return turn_text(spec, attachment_paths)

    def spawn_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        return spawn_argv(flags, images, pointer(turn), binary=binary)

    def resume_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str | None,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        prompt = None if turn is None else pointer(turn)
        return resume_argv(flags, session_id, images, prompt, binary=binary)

    def render(self, launch: TuiLaunch) -> LaunchFiles:
        document, mcp_env = tui_config_toml(
            launch.mcp or (),
            launch.secrets,
            writable_root=_writable_root(launch),
            workspace_real=launch.workspace_real,
            codex_home_real=launch.config_real,
            signal_command=signal_command(str(launch.signals), str(launch.signal_script)),
            developer_instructions=launch.system,
            ask_tool=self._ask_tool,
            bedrock=(
                bedrock_provider_config(launch.secrets.get("AWS_REGION"))
                if launch.auth_mode == AUTH_MODE_BEDROCK
                else None
            ),
        )
        files = {launch.config_dir / CONFIG_FILE: document}
        auth = auth_document(launch.auth_mode, launch.credential)
        if auth:
            files[launch.config_dir / AUTH_FILE] = auth
        home = launch.config_dir / HOME_DIRNAME
        env = tui_environment(
            launch.secrets,
            config_dir=launch.config_dir,
            home=home,
            worktree=launch.worktree,
            git_dir=launch.git_dir,
            credential_less=launch.credential_less,
            config_sha=launch.config_sha,
            mcp_env=mcp_env,
            bot_login=launch.bot_login,
            bedrock=launch.auth_mode == AUTH_MODE_BEDROCK,
        )
        return LaunchFiles(files=files, env=env, dirs=(home,))

    def probe_env(self, secrets: Mapping[str, str], config_dir: Path) -> dict[str, str]:
        return {**git_env(secrets), **secrets, ENV_CODEX_HOME: str(config_dir)}

    async def refusal(self, binary: str, root: Path, *, within: float) -> str | None:
        """The native binary at the pin exactly, by its first line: `0.159.0` is in
        `10.159.0`. Asked with an empty environment and no stdin, so nothing of the
        orchestrator's reaches the binary under test."""
        if await asyncio.to_thread(_is_script, binary):
            return (
                f"the installed codex ({binary}) is a script, as npm's @openai/codex is, and a "
                "session runs the native binary, which a hangup ends with everything it "
                f"started: install the release's codex-package {PINNED_VERSION} on the "
                "worker's host"
            )
        shown = await probe_output((binary, FLAG_VERSION), env={}, within=within)
        first = next(iter(shown.splitlines()), "").strip()
        pinned = f"{VERSION_NAME} {PINNED_VERSION}"
        if first == pinned:
            return None
        return (
            f"the installed codex ({binary}) reports {first[:80]!r}, and a session's rollout "
            f"is translated only as observed on {pinned}: install codex {PINNED_VERSION} on "
            "the worker's host, re-pin cli.py's PINNED CLI SURFACE against the new version "
            "with its local TUI tests"
        )
