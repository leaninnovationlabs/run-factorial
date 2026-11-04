"""Local Claude adapter — the `claude` CLI, run in a git worktree on the host.

A peer of the other adapters behind the same `runtime.contract`; the fleet runs on it
(tech-stack.md decision 16). Everything the CLI knows about is confined to
cli.py (the PINNED CLI SURFACE block and `_Cli`), so that module is the only thing that changes
when the CLI does.

This adapter has no containment: the agent runs with permissions bypassed on the host filesystem,
holding a real repo-scoped `GH_TOKEN`. Two guards make the blast radius inspectable rather than
safe — a worktree per run under `local_root`, and a refusal to spawn on any resolved path that
escapes it. The shared clone under `local_root/repos/` and the host user's own HOME stay
readable.

GitHub is not local: the agent pushes a branch and opens a PR against a real (scratch) repo, so
the orchestrator verifies the same predicates against the same observed state as in production.
A local `StateSource` would make the loop test a fiction (design.md decision 1).

Every run is the CLI's TUI, in its own tmux server under `local_root`, where a human can watch
it and type (decision 56). `_cli/host_session.py` holds the launch, the supervisor that reads its
hook signals and session log, and the verbs that read a run's state on disk; `session.py` holds
what of it is claude's. This module keeps `spawn`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final

from runtime.adapters._cli.checkout import (
    prepare_worktree,
)
from runtime.adapters._cli.environment import git_env, refuse_attachments, refuse_blind
from runtime.adapters._cli.host_session import HostSession, new_run_id
from runtime.adapters._cli.inspect import HostInspect
from runtime.adapters._cli.paths import (
    CONFIG_DIRNAME,
    RUNS_DIRNAME,
    contained,
    utc_now,
    write_attachments,
)
from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY
from runtime.adapters.claude_cli.cli import (
    CLAUDE_BIN,
    ENV_CONFIG_DIR,
    ENV_DISABLE_AUTOUPDATER,
    PINNED_VERSION,
    _Cli,
)
from runtime.adapters.claude_cli.session import SYSTEM_PROMPT_FILE, ClaudeTui
from runtime.adapters.claude_cli.spec import (
    _reject_model_alias,
    _reject_unverifiable_expectations,
    build_mcp_config,
    build_prompt,
    mcp_tool_patterns,
)
from runtime.contract import (
    AdapterUnavailable,
    RunHandle,
    RunSpec,
    SpawnError,
    SpawnReason,
)

__all__ = [
    "ClaudeCliAdapter",
    "build_mcp_config",
    "build_prompt",
    "mcp_tool_patterns",
]

log = logging.getLogger(__name__)

#: How often a session's supervisor reads its files.
SESSION_POLL: Final = 1.0

# --------------------------------------------------------------------------- the adapter


class ClaudeCliAdapter(HostSession, HostInspect):
    """`runtime.contract.RuntimeAdapter` over the local `claude` CLI."""

    _tui = ClaudeTui()

    #: No inbound channel: terminal state is discovered by the loop polling `status()`, which is
    #: acceptable only because polling is already authoritative (design.md decision 1).
    supports_callbacks: ClassVar[bool] = False

    #: The CLI takes `--mcp-config`, so a declared server can actually be wired up. Server-level
    #: containment is real here (`--strict-mcp-config` makes this file the whole list); tool-level
    #: narrowing is best-effort — see `FLAG_ALLOWED_TOOLS`.
    supports_mcp: ClassVar[bool] = True

    #: The lane is dormant; `test_cli_blind_clone.py` tests its clone directly.
    supports_blind_workspace: ClassVar[bool] = False

    #: Written outside the worktree and attached to the first turn by `@<path>`.
    supports_attachments: ClassVar[bool] = True

    #: No adapter-level cap: nothing here serializes runs, and a worktree per run is cheap enough
    #: that concurrency is bounded elsewhere.
    max_concurrent_runs: ClassVar[int | None] = None

    #: The CLI version this module's PINNED CLI SURFACE was observed against.
    pinned_version: ClassVar[str] = PINNED_VERSION

    def __init__(
        self,
        *,
        binary: str = CLAUDE_BIN,
        local_root: Path | str | None = None,
        cli: _Cli | None = None,
        now: Callable[[], datetime] = utc_now,
        auth_mode: str = AUTH_MODE_API_KEY,
        poll: float = SESSION_POLL,
    ) -> None:
        self._root = Path(local_root).expanduser().resolve() if local_root is not None else None
        self._auth_mode = auth_mode
        self._cli = cli if cli is not None else _Cli(claude_bin=binary)
        self._now = now
        self._poll = poll
        #: Live supervisors, by run id. Only ever an optimisation over the on-disk state: a run
        #: whose supervisor this process lost is taken back by `status()` (`_readopt`).
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        self._sessions = {}

    # -- contract ------------------------------------------------------------------------

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        root = self._require_root()
        refuse_blind(type(self).__name__, run_spec)
        _reject_model_alias(run_spec.model)
        _reject_unverifiable_expectations(run_spec)
        if not self.supports_attachments:
            refuse_attachments(type(self).__name__, run_spec.attachments)
        if run_spec.public:
            raise SpawnError(
                f"public requested, but {type(self).__name__} cannot serve a preview of its "
                "workspace: a runtime-served preview needs a cloud sandbox runtime",
                reason=SpawnReason.CONFIG,
            )
        secrets = run_spec.resolved_secrets()  # the one sanctioned unwrap (contract.py)
        # Read before anything is created, from the store only (auth.py).
        self._tui.credential(secrets, self._auth_mode)
        # Rendered before anything is created: a missing `{secrets.KEY}` is a config problem, and
        # it should cost a clean refusal rather than a half-built run directory.
        if run_spec.mcp:
            build_mcp_config(run_spec.mcp, secrets)
        if run_spec.resume_from is not None:
            return await self._adopt(run_spec, run_spec.resume_from, secrets=secrets)

        run_id = new_run_id(run_spec)
        tools = self._session_tools(root, run_id)
        run_dir = contained(root, root / RUNS_DIRNAME / run_id)
        config_dir = run_dir / CONFIG_DIRNAME
        (config_dir / "skills").mkdir(parents=True, exist_ok=True)

        # The HOST's environment: it authenticates the clone, the artifact probes and cleanup.
        # What the agent process launches with is the session's own (`_tui_environment`).
        env = {
            **git_env(secrets),
            **secrets,
            ENV_CONFIG_DIR: str(config_dir),
            ENV_DISABLE_AUTOUPDATER: "1",
        }
        # The pointer stays: claude has no sandbox that would protect its target.
        worktree, _ = await prepare_worktree(
            self._cli, root, run_dir, run_spec, env, drop_git_pointer=False
        )
        self._write_agent_files(run_dir, config_dir, run_spec)
        attachment_paths = write_attachments(run_dir, run_spec.attachments)
        return await self._spawn_session(
            run_spec,
            tools,
            run_id=run_id,
            run_dir=run_dir,
            worktree=worktree,
            secrets=secrets,
            probe_env=env,
            attachments=attachment_paths,
        )

    # -- internals -----------------------------------------------------------------------

    def _require_root(self) -> Path:
        if self._root is None:
            raise AdapterUnavailable(
                "claude_cli requires FACTORIAL_RUNTIME__CLAUDE_CLI__LOCAL_ROOT. It has no default "
                "on purpose: this runtime runs agents on the host with permissions bypassed, so "
                "a person must choose the directory they run in"
            )
        return self._root

    def _write_agent_files(self, run_dir: Path, config_dir: Path, spec: RunSpec) -> None:
        """Prompts and skills, as files, outside the worktree — a `SKILL.md` written into the
        checkout is something the agent can commit into the target repo by accident."""
        (run_dir / SYSTEM_PROMPT_FILE).write_text(spec.prompts.system, "utf-8")
        for skill in spec.skills:
            folder = config_dir / "skills" / skill.name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(skill.content, "utf-8")
