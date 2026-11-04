"""What a host session of the claude TUI is made of (decision 56): `ClaudeTui`, the profile
`_cli/host_session.py` launches, types into, reads and refuses the claude TUI by.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters._cli import tmux as tmux_
from runtime.adapters._cli.environment import git_env
from runtime.adapters._cli.host_session import HostCounter, LaunchFiles, Tally, TuiLaunch
from runtime.adapters._cli.launch import ENV_FILE
from runtime.adapters._cli.paths import HOME_DIRNAME
from runtime.adapters._cli.process import probe_output
from runtime.adapters._cli.session_reader import LogSignals, SessionTranscript
from runtime.adapters._cli.session_signals import CostState
from runtime.adapters._cli.session_state import HostRunState
from runtime.adapters.claude_cli.cli import (
    CLAUDE_JSON_FILE,
    ENV_CONFIG_DIR,
    ENV_DISABLE_AUTOUPDATER,
    EXIT_COMMAND,
    FLAG_VERSION,
    INPUT_MARK,
    MCP_CONFIG_FILE,
    PINNED_VERSION,
    VALUE_PROJECT_DIR,
)
from runtime.adapters.claude_cli.environment import _tui_credential, _tui_environment
from runtime.adapters.claude_cli.spec import build_mcp_config, build_prompt
from runtime.adapters.claude_cli.stream import NO_TURN, StreamReader
from runtime.adapters.claude_cli.transcript import (
    TRANSCRIPT_VERSION,
    Transcript,
    claude_log_signals,
)
from runtime.adapters.claude_cli.tui import (
    pointer,
    resume_argv,
    seed_files,
    spawn_argv,
    tui_flags,
    view_flags,
)
from runtime.contract import RunSpec

__all__ = ["SESSION_CREDENTIALS", "SYSTEM_PROMPT_FILE", "ClaudeTui", "session_log"]

#: Beside the run, outside the worktree.
SYSTEM_PROMPT_FILE: Final = "system-prompt.md"
#: A run's credential files in its config home, deleted at every end.
SESSION_CREDENTIALS: Final = (MCP_CONFIG_FILE, CLAUDE_JSON_FILE, ENV_FILE)
#: The version probe's config dir, under `local_root`, so the probe reads none of the operator's.
PROBE_CONFIG_DIRNAME: Final = ".version-probe"
ESC: Final = "\x1b"
CTRL_C: Final = "\x03"


def session_log(config_dir: Path, session_id: str) -> Path:
    return config_dir / "projects" / VALUE_PROJECT_DIR / f"{session_id}.jsonl"


class ClaudeTui:
    """`HostTui` for `claude`."""

    name: str = "claude"
    label: str = "Claude CLI"
    marker: str = ENV_CONFIG_DIR
    credentials: tuple[str, ...] = SESSION_CREDENTIALS
    system_prompt_file: str | None = SYSTEM_PROMPT_FILE
    input_mark: str = INPUT_MARK
    quit_command: str = EXIT_COMMAND
    #: Esc closes a dialog a human left open, where Enter would apply its highlighted choice.
    clear_keys: tuple[str, ...] = (ESC, CTRL_C)
    clear_quits: bool = False
    consent: str | None = None
    sign_in: tuple[str, ...] = ()
    turnless_hook: bool = True
    names_git_dir: bool = False
    sweep_descendants: bool = False
    transcript_version: int = TRANSCRIPT_VERSION
    turnless_warnings: frozenset[str] = frozenset({NO_TURN})

    @property
    def log_signals(self) -> LogSignals:
        return claude_log_signals

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> Transcript:
        return Transcript(session_id, turn=turn, cost_base=cost_base, skip=skip)

    def reader(self, prior_turns: int) -> StreamReader:
        return StreamReader(prior_turns=prior_turns)

    def tally(
        self, reader: HostCounter, *, transcript: SessionTranscript, stop_summary: str | None
    ) -> Tally:
        assert isinstance(reader, StreamReader)
        return Tally(
            error=reader.error(),
            summary=reader.summary,
            turns=reader.final_turns,
            missing=reader.missing(),
            counts={
                "cost_usd": reader.cost_usd,
                "input_tokens": reader.input_tokens,
                "cached_input_tokens": reader.cached_input_tokens,
                "cache_write_input_tokens": reader.cache_write_input_tokens,
                "output_tokens": reader.output_tokens,
            },
        )

    def session_log(self, config_dir: Path, session_id: str, recorded: str | None) -> Path:
        return session_log(config_dir, session_id)

    def discover(self, config_dir: Path, since: float) -> None:
        return None  # a spawn names the session

    def credential(self, secrets: Mapping[str, str], auth_mode: str) -> str:
        return _tui_credential(secrets, auth_mode)

    def flags(
        self, spec: RunSpec, *, run_dir: Path, config_dir: Path, worktree: Path
    ) -> tuple[str, ...]:
        return tui_flags(
            spec,
            system_prompt_path=str(run_dir / SYSTEM_PROMPT_FILE),
            mcp_path=str(config_dir / MCP_CONFIG_FILE),
        )

    def resume_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        return state.resume_args

    def view_flags(self, state: HostRunState, *, worktree: Path) -> tuple[str, ...]:
        """The resume's with no MCP server. The state file is the agent's to write."""
        return view_flags(state.resume_args)

    def git_dir(self, state: HostRunState) -> None:
        return None  # the worktree keeps its `.git` pointer

    def launch_words(self, state: HostRunState, home: Path) -> tuple[str, ...]:
        """Every launch names the session: `--session-id` or `--resume`."""
        assert state.session_id is not None
        return (state.session_id,)

    def turn_text(self, spec: RunSpec, attachment_paths: Mapping[str, str]) -> str:
        return build_prompt(spec, attachment_paths)

    def spawn_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        return spawn_argv(flags, session_id, pointer(turn, images), binary=binary)

    def resume_argv(
        self,
        flags: Sequence[str],
        session_id: str,
        turn: str | None,
        images: Sequence[str],
        *,
        binary: str,
    ) -> tuple[str, ...]:
        prompt = None if turn is None else pointer(turn, images)
        return resume_argv(flags, session_id, prompt, binary=binary)

    def render(self, launch: TuiLaunch) -> LaunchFiles:
        files: dict[Path, str] = {}
        if launch.mcp:
            document = build_mcp_config(launch.mcp, launch.secrets)
            files[launch.config_dir / MCP_CONFIG_FILE] = json.dumps(document, indent=2)
        seeds = seed_files(
            config_dir=str(launch.config_dir),
            workspace_real=launch.workspace_real,
            signal_script=str(launch.signal_script),
            auth_mode=launch.auth_mode,
            credential=launch.credential,
        )
        files |= {Path(seed): content.decode() for seed, content in seeds.items()}
        home = launch.config_dir / HOME_DIRNAME
        env = _tui_environment(
            launch.secrets,
            config_dir=launch.config_dir,
            home=home,
            signals_path=launch.signals,
            auth_mode=launch.auth_mode,
            credential=launch.credential,
            credential_less=launch.credential_less,
            config_sha=launch.config_sha,
            bot_login=launch.bot_login,
        )
        return LaunchFiles(files=files, env=env, dirs=(home,))

    def probe_env(self, secrets: Mapping[str, str], config_dir: Path) -> dict[str, str]:
        return {
            **git_env(secrets),
            **secrets,
            ENV_CONFIG_DIR: str(config_dir),
            ENV_DISABLE_AUTOUPDATER: "1",
        }

    async def refusal(self, binary: str, root: Path, *, within: float) -> str | None:
        """The installed CLI must be the pin exactly: a version that merely contains it is
        another CLI. Asked with no stdin and none of the worker's
        environment, so nothing of the orchestrator's reaches the binary under test."""
        shown = await probe_output(
            (binary, FLAG_VERSION),
            env={
                "PATH": tmux_.host_path(),
                "LANG": "C.UTF-8",
                "HOME": str(root),
                ENV_CONFIG_DIR: str(root / PROBE_CONFIG_DIRNAME),
                ENV_DISABLE_AUTOUPDATER: "1",
            },
            within=within,
        )
        words = shown.split()
        if words and words[0] == PINNED_VERSION:
            return None
        return (
            f"the installed claude ({binary}) reports {shown.strip()[:80]!r}, and a session's "
            f"log is translated only as observed on {PINNED_VERSION}: install claude "
            f"{PINNED_VERSION} on the worker's host, re-pin cli.py's PINNED CLI SURFACE against "
            "the new version with its local TUI tests"
        )
