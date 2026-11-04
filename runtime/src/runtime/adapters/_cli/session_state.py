"""A host run's state file, and its session's fields (decision 56).

`HostRunState` is either host adapter's run state: `SessionFields`, the session's block, and the
run's own. A resumed segment reports only itself, so `add` carries the earlier segments' figures.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.adapters._cli.session_signals import CostState, SessionSignals
from runtime.contract import (
    Artifact,
    ExpectedArtifact,
    McpServerSpec,
    ParkReason,
    RunStatus,
    RunUsage,
    UnknownRunError,
)

__all__ = [
    "LOG_FILE",
    "STATE_FILE",
    "HostRunState",
    "SessionFields",
    "add",
    "drop_retired",
    "finalise",
    "log_lines",
    "merge_warnings",
    "read_state",
    "write_state",
]

#: Under `<local_root>/runs/<run_id>/`: the run's state, and the stream `logs()` serves.
STATE_FILE: Final = "state.json"
LOG_FILE: Final = "stdout.jsonl"
#: A host state file's keys from before every run was a session, and the deadline and pid it
#: recorded and never read.
_RETIRED_KEYS: Final = frozenset({"interactive", "deadline", "pid"})


def drop_retired(data: Any, keys: frozenset[str]) -> Any:
    """`data` without `keys`, which a state file from before every run was a session holds: the
    state forbids unknown keys, and one it cannot read is never swept or reaped."""
    if not isinstance(data, dict) or not keys & data.keys():
        return data
    return {key: value for key, value in data.items() if key not in keys}


def log_lines(path: Path) -> list[str]:
    try:
        return path.read_text("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


class SessionFields(BaseModel):
    """A run's session (decision 56; `host_session.py`). The worker owns its active time."""

    auth_mode: str | None = None
    #: The spec's `credential="none"`: a resume hands the CLI no GitHub token either.
    credential_less: bool = False
    #: The forge login the run was last handed (`probes.ENV_BOT_LOGIN`): a launch whose secrets
    #: leave it out, a session view's among them, names the agent with it.
    bot_login: str = ""
    #: Launches of the session's CLI, a continued run's included, and whether the current one
    #: carried the orchestrator's turn.
    segments: int = 0
    segment_turn: bool = False
    #: Bytes of the translated mirror, and where the current segment's lines begin.
    offset: int = 0
    segment_start: int = 0
    #: How far this run's signals file is read.
    signals_offset: int = 0
    #: Where this run's slice of the session log starts (a rework continues another run's log),
    #: how far it is read, and where the current segment's lines begin.
    log_offset: int = 0
    log_read: int = 0
    segment_log_start: int = 0
    #: The session's running cost at the segment's start, and the latest seen.
    cost_base: CostState | None = None
    cost_seen: CostState | None = None
    #: The run's tokens at the segment's start, and so far, counted off its log as it runs.
    usage_base: RunUsage | None = None
    live_usage: RunUsage | None = None
    #: Where the session stands, from its hooks; persisted whole, so a restart replays none.
    signals: SessionSignals = Field(default_factory=SessionSignals)
    #: Turns across every segment, as `session_state` reports them.
    session_turns: int = 0
    #: A `finish` was sent: the session's end is a success.
    finishing: bool = False
    #: A stop someone asked for, written before the hangup, and why an interrupt parks it.
    stopping: RunStatus | None = None
    park_reason: ParkReason | None = None
    #: The run whose workspace this one continues in, the run that took this one's session
    #: over, and the branch the checkout held at the end, read before the park detaches it.
    workspace_of: str | None = None
    continued_by: str | None = None
    held_branch: str | None = None
    #: The translator that wrote the mirror: a restart replays only through the same one.
    transcript_version: int | None = None
    #: The checkout's resolved path, which the CLI's trust seed names.
    workspace_real: str | None = None
    #: The session log, for a CLI that names it only once it runs; None where the session id
    #: says where it is.
    session_log: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, data: Any) -> Any:
        return drop_retired(data, _RETIRED_KEYS)


class HostRunState(SessionFields):
    """The run's whole life, on disk.

    On disk rather than in memory because `status()` must survive a worker restart: the
    orchestrator's `step_run.runtime_run_id` outlives this process, and an in-memory registry
    would turn every restart into a fleet of runs the substrate never heard of.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    agent: str
    ticket: str
    repo: str
    branch: str
    worktree: str
    started_at: datetime
    #: The session `--resume` addresses: claude's `--session-id`, codex's thread id.
    session_id: str | None = None
    #: The model codex passes again at every resume; claude's flags carry it in `resume_args`.
    #: Defaulted for state files written before it.
    model: str = ""
    max_turns: int | None = None
    turns: int = 0
    exit_code: int | None = None
    summary: str = ""
    failure_reason: str | None = None
    #: Always None for codex, which reports tokens and no dollar figure (its cli.py's
    #: NO_COST_REPORTED pin); the orchestrator estimates from the cache split.
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    #: The cache read and the cache write, each a part of `input_tokens` here: claude's own
    #: `total_cost_usd` already prices them, and a run that ends without one is priced from them.
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    artifacts: tuple[Artifact, ...] = ()
    stream_warnings: tuple[str, ...] = ()
    #: The TUI flags every resume repeats.
    resume_args: tuple[str, ...] = ()
    #: The declared MCP servers, templates and never values: the park deletes the rendered
    #: config, and a resume renders it again from its own secrets. None for a state written
    #: before the field existed, which cannot be rendered again.
    mcp: tuple[McpServerSpec, ...] | None = None
    #: What the spec declared this run must deliver. Empty leaves discovery and the terminal
    #: verdict at their default behaviour.
    expected: tuple[ExpectedArtifact, ...] = ()
    #: Which checkout shape this run got. A blind worktree is a standalone clone, not a worktree
    #: of the shared clone, so the reaper must remove it differently.
    blind: bool = False
    #: The pinned config commit a resume exports again (`FACTORIAL_CONFIG_SHA`). None for a
    #: blind run, a spec without a pin, or a state file written before the field existed.
    config_sha: str | None = None
    #: The worktree's administrative directory in the shared clone, for a CLI whose worktree
    #: carries no `.git` pointer (codex): what a resume and the host's probes address. None for
    #: claude and for a blind clone, which keeps its own `.git`.
    git_dir: str | None = None
    #: An interrupted run whose park `finish` ended: it holds what any other end holds.
    park_ended: bool = False

    @property
    def began_session(self) -> bool:
        return self.session_id is not None and self.workspace_real is not None


def read_state(run_dir: Path, run_id: str) -> HostRunState:
    try:
        raw = (run_dir / STATE_FILE).read_text("utf-8")
    except FileNotFoundError as exc:
        raise UnknownRunError(f"no local run {run_id} under {run_dir}") from exc
    return HostRunState.model_validate_json(raw)


def write_state(run_dir: Path, state: HostRunState) -> None:
    # Written whole to a temp file and renamed: `status()` in another process must never read a
    # half-written state, and os.replace is atomic within a directory.
    target = run_dir / STATE_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), "utf-8")
    os.replace(tmp, target)


def finalise(
    run_dir: Path,
    state: HostRunState,
    outcome: RunStatus,
    *,
    failure_reason: str | None = None,
    **fields: Any,
) -> HostRunState:
    """Write the terminal state once. Every terminal path goes through here."""
    updated = state.model_copy(
        update={"status": outcome, "failure_reason": failure_reason, **fields}
    )
    write_state(run_dir, updated)
    return updated


def add(prior: Any, segment: Any) -> Any:
    """A segment's figure on top of the earlier segments'; None only when both are unknown."""
    if prior is None:
        return segment
    if segment is None:
        return prior
    return prior + segment


def merge_warnings(prior: tuple[str, ...], segment: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*prior, *segment)))
