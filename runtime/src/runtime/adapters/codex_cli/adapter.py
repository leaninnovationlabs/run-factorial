"""Codex-local adapter — OpenAI's `codex` CLI, run in a git worktree on the host.

A peer of the other adapters behind the same `runtime.contract`, the first non-Claude substrate,
and one the fleet runs (tech-stack.md decision 34). The CLI's observed surface is pinned in
cli.py (the PINNED CLI SURFACE and `_Cli`).

Every run is the CLI's TUI, in its own tmux server under `local_root`, where a human can watch
it and type (decision 56), inside codex's own `workspace-write` sandbox: it may write its
worktree and the shared clone's git directory and reach the network, holding a real
repo-scoped `GH_TOKEN` (decision 34). Two guards make the blast radius inspectable — a worktree
per run under `local_root`, and a refusal to spawn on any resolved path that escapes it. GitHub
is not local: the agent pushes a branch and opens a PR against the real repo, so the
orchestrator verifies the same predicates against the same observed state as for every other
substrate.

`_cli/host_session.py` holds the launch, the supervisor and the verbs that read a run's state
on disk; `session.py` holds what of it is codex's, and `tui.py` the documents each launch
writes. Nothing a resume reads back is the agent's to write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Final

from pydantic import SecretStr

from runtime.adapters._cli.checkout import (
    prepare_worktree,
)
from runtime.adapters._cli.environment import (
    git_env,
    refuse_attachments,
    refuse_blind,
)
from runtime.adapters._cli.host_session import (
    HostSession,
    new_run_id,
)
from runtime.adapters._cli.inspect import HostInspect
from runtime.adapters._cli.paths import (
    CONFIG_DIRNAME,
    RUNS_DIRNAME,
    contained,
    utc_now,
    write_attachments,
)
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_API_KEY,
    DEFAULT_SUBSCRIPTION_AUTH_FILE,
)
from runtime.adapters.codex_cli.cli import (
    CODEX_BIN,
    ENV_CODEX_HOME,
    PINNED_VERSION,
    SKILLS_DIRNAME,
    _Cli,
)
from runtime.adapters.codex_cli.sandbox import sandbox_refusal
from runtime.adapters.codex_cli.session import CodexTui
from runtime.adapters.codex_cli.spec import (
    _reject_model_alias,
    _reject_unverifiable_expectations,
    build_config_toml,
    build_prompt,
    mcp_tables,
)
from runtime.contract import (
    AdapterUnavailable,
    InspectState,
    InspectUnavailable,
    RunHandle,
    RunSpec,
    SpawnError,
    SpawnReason,
)

log = logging.getLogger(__name__)

#: How often a session's supervisor reads its files.
SESSION_POLL: Final = 1.0

__all__ = [
    "CodexCliAdapter",
    "build_config_toml",
    "build_prompt",
]


class CodexCliAdapter(HostSession, HostInspect):
    """`runtime.contract.RuntimeAdapter` over the local `codex` CLI."""

    #: No inbound channel: terminal state is discovered by the loop polling `status()`, which is
    #: acceptable only because polling is already authoritative (design.md decision 1).
    supports_callbacks: ClassVar[bool] = False

    #: MCP servers render into the per-run `$CODEX_HOME/config.toml`, which is the run's whole
    #: server list (nothing else is consulted). Server-level containment is real; tool-level
    #: narrowing is NOT enforced — 0.146.0 exposes no observed surface for it (spec.py).
    supports_mcp: ClassVar[bool] = True

    #: The lane is dormant; `test_cli_blind_clone.py` tests its clone directly.
    supports_blind_workspace: ClassVar[bool] = False

    #: Written beside the worktree and attached to the first turn by `--image`.
    supports_attachments: ClassVar[bool] = True

    #: No adapter-level cap: a worktree per run is cheap, and concurrency is bounded elsewhere.
    max_concurrent_runs: ClassVar[int | None] = None

    #: The CLI version this package's PINNED CLI SURFACE was observed against.
    pinned_version: ClassVar[str] = PINNED_VERSION

    def __init__(
        self,
        *,
        binary: str = CODEX_BIN,
        local_root: Path | str | None = None,
        cli: _Cli | None = None,
        now: Callable[[], datetime] = utc_now,
        auth_mode: str = AUTH_MODE_API_KEY,
        subscription_auth_file: Path | str | None = None,
        poll: float = SESSION_POLL,
        ask_tool: bool = False,
    ) -> None:
        self._root = Path(local_root).expanduser().resolve() if local_root is not None else None
        self._auth_mode = auth_mode
        self._subscription_auth_file = Path(
            subscription_auth_file
            if subscription_auth_file is not None
            else DEFAULT_SUBSCRIPTION_AUTH_FILE
        ).expanduser()
        self._cli = cli if cli is not None else _Cli(codex_bin=binary)
        self._tui = CodexTui(self._subscription_auth_file, ask_tool=ask_tool)
        self._now = now
        self._poll = poll
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        self._sessions = {}
        self._sandbox_passed = False

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
        # Read before anything is created, like every other refusal here (auth.py).
        self._tui.credential(secrets, self._auth_mode)
        # Before the rework's `_adopt` below, which can stop a ghost run.
        if (why := await self._launch_refusal()) is not None:
            raise SpawnError(why, reason=SpawnReason.CONFIG)

        # Rendered before anything is created, and again at every launch: a missing
        # `{secrets.KEY}`, an SSE server or an unmappable header is a config problem, and it
        # should cost a clean refusal rather than a half-built run directory.
        mcp_tables(run_spec.mcp, secrets)
        run_id = new_run_id(run_spec)
        run_dir = contained(root, root / RUNS_DIRNAME / run_id, create=False)
        if run_spec.resume_from is not None:
            return await self._adopt(
                run_spec, run_spec.resume_from, secrets=secrets, own={"model": run_spec.model}
            )

        tools = self._session_tools(root, run_id)
        config_dir = run_dir / CONFIG_DIRNAME
        (config_dir / SKILLS_DIRNAME).mkdir(parents=True, exist_ok=True)

        # The HOST's environment: it authenticates the clone, the artifact probes and cleanup.
        # Every launch of the session writes its own login, config and environment.
        env = {**git_env(secrets), **secrets, ENV_CODEX_HOME: str(config_dir)}
        worktree, git_dir = await prepare_worktree(
            self._cli, root, run_dir, run_spec, env, drop_git_pointer=True
        )
        assert git_dir is not None
        self._write_agent_files(config_dir, run_spec)
        return await self._spawn_session(
            run_spec,
            tools,
            run_id=run_id,
            run_dir=run_dir,
            worktree=worktree,
            secrets=secrets,
            probe_env=env,
            attachments=write_attachments(run_dir, run_spec.attachments),
            git_dir=git_dir,
            own={"model": run_spec.model, "git_dir": str(git_dir)},
        )

    async def inspect_start(
        self,
        run_id: str,
        *,
        token: str,
        limit: timedelta,
        credentials: Mapping[str, SecretStr],
    ) -> InspectState:
        """The view resumes the session's codex, under the same sandbox."""
        if (why := await self._launch_refusal()) is not None:
            raise InspectUnavailable(why)
        return await super().inspect_start(
            run_id, token=token, limit=limit, credentials=credentials
        )

    # -- internals -----------------------------------------------------------------------

    def _require_root(self) -> Path:
        if self._root is None:
            raise AdapterUnavailable(
                "codex_cli requires FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT. It has no default "
                "on purpose: this runtime runs agents directly on the host, and a default would "
                "decide where the agent can write"
            )
        return self._root

    async def _launch_refusal(self) -> str | None:
        """codex's sandbox. Only a pass is remembered: a refusal can be fixed while the worker
        runs."""
        if self._sandbox_passed:
            return None
        why = await sandbox_refusal(self._cli, self._require_root())
        self._sandbox_passed = why is None
        return why

    def _write_agent_files(self, config_dir: Path, spec: RunSpec) -> None:
        """Skills as files under the per-run CODEX_HOME, never the worktree — a `SKILL.md`
        written into the checkout is something the agent can commit into the target repo by
        accident. The system prompt needs no file: it goes in the config's
        `developer_instructions` (tui.py)."""
        for skill in spec.skills:
            folder = config_dir / SKILLS_DIRNAME / skill.name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(skill.content, "utf-8")
