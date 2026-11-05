"""The reconciliation loop's dependency bundle (design.md §3.1).

Everything the loop touches that is not the database arrives through `ReconcileDeps`: each
project's way to GitHub (`ProjectGitHub`: the observed-state snapshot, the projector, the bot
login), the agent runtime, token minting, and the clock. Two reasons:

* the loop is testable without GitHub or a substrate — `SnapshotState` plus a recording projector
  exercises the *real* predicate library and the *real* state machine;
* the clock is injected, so timeout and backoff behaviour is tested by moving `now` forward
  rather than by sleeping.

`ProjectGitHub` is resolved per project per tick, from that project's own connection
(`github/pool.py`), because two organizations may point at one repo with different credentials.
Inside `github_once_per_tick`, which `loop.tick` and a refresh pass enter, admission, every
ticket's reconcile and its spawn share that one resolution; the next tick resolves again, so a
connection saved between ticks is the one it uses.
None is a project with no connection: its ticks hold (`reconcile.reconcile_ticket`). Its
`state_source` is called once per tick per ticket: one `StateSource` instance is one snapshot
(github/state.py), and holding one across ticks would stop the loop observing. Inside a tick the
sources share the repo's branch and PR listings (`RepoListings`), held on the tick, not on
`ProjectGitHub`, which a run's supervisor can keep past it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.actions import ContractGitHub
from orchestrator.adapters.bindings import Bindings
from orchestrator.adapters.registry import (
    ForgeFactory,
    TrackerFactory,
    display_of,
    host_registries,
    resolve_bindings,
    tracker_kind_for,
)
from orchestrator.db.models import Project, Runtime, StepRun
from orchestrator.factorial import AgentPins, RepoConfigSource, ResolvedAgent, resolve_agent
from orchestrator.github import (
    GitHubClient,
    GitHubContractOps,
    GitHubMedia,
    GitHubProjector,
    GitHubStateSource,
    RepoContents,
    StateSource,
    TreeEntry,
)
from orchestrator.github.connection import project_bot_login
from orchestrator.github.contents import Comparison
from orchestrator.github.observer import RepoListings
from orchestrator.github.pool import pool
from orchestrator.settings import Settings, TerminalSettings, WorkspaceSettings, get_settings
from orchestrator.worker.attachments import MediaFactory
from orchestrator.worker.runtime_adapters import (
    RuntimeAdapters,
    RuntimeRefused,
    load_runtime_by_slug,
    registry_build,
    spawned_kind,
    workspace_key,
)
from runtime.contract import RuntimeAdapter

__all__ = [
    "AgentSource",
    "ProjectGitHub",
    "Projector",
    "ReconcileDeps",
    "SessionPolicy",
    "github_once_per_tick",
    "production_deps",
    "utc_now",
]


class Projector(Protocol):
    """The mutations the worker is allowed to make on GitHub — `GitHubProjector`'s surface.

    Declared here rather than imported so tests can substitute a recorder, and so the loop cannot
    reach for a GitHub call the projector does not offer.
    """

    async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None: ...

    async def close_issue(self, issue_number: int) -> None: ...

    async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None: ...

    async def comment(self, issue_number: int, body: str) -> None: ...

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Literal["merge", "squash", "rebase"] = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str: ...


def utc_now() -> datetime:
    """The default clock. Timezone-aware because every timestamp column is `timestamptz`."""
    return datetime.now(UTC)


#: `(session, project, agent name) -> ResolvedAgent`: the registry row's pin, plus the manifest,
#: both prompts and every `SKILL.md` read at that one sha (`orchestrator.factorial`). It takes the
#: caller's session because establishing a first-use pin is a write that must land in the tick's
#: own unit of work — `factorial.registry.resolve_agent` flushes and never commits.
AgentSource = Callable[[AsyncSession, Project, str, AgentPins | None], Awaitable[ResolvedAgent]]


_TERMINAL_DEFAULTS = TerminalSettings()
_WORKSPACE_DEFAULTS = WorkspaceSettings()


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """`settings.terminal`, `settings.workspace` and the dashboard's URL, read once at wiring
    time."""

    #: A browser's attach, typing and reopen only: every run is a session either way.
    terminal_enabled: bool = _TERMINAL_DEFAULTS.enabled
    idle_park_after: timedelta = _TERMINAL_DEFAULTS.idle_park_after
    finish_grace: timedelta = _TERMINAL_DEFAULTS.finish_grace
    session_ceiling: timedelta = _TERMINAL_DEFAULTS.session_ceiling
    parked_recheck: timedelta = _TERMINAL_DEFAULTS.parked_recheck
    retention: timedelta = _WORKSPACE_DEFAULTS.retention
    #: `Settings.dashboard_url`: what a comment links a terminal from. Empty, it links none.
    dashboard_url: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionPolicy:
        return cls(
            terminal_enabled=settings.terminal.enabled,
            idle_park_after=settings.terminal.idle_park_after,
            finish_grace=settings.terminal.finish_grace,
            session_ceiling=settings.terminal.session_ceiling,
            parked_recheck=settings.terminal.parked_recheck,
            retention=settings.workspace.retention,
            dashboard_url=settings.dashboard_url,
        )


@dataclass(frozen=True, slots=True)
class ProjectGitHub:
    """One project's GitHub: its connection's client and everything the worker builds on it."""

    client: GitHubClient
    #: `(repo, issue_number) -> StateSource` — a fresh snapshot per tick.
    state_source: Callable[[str, int], StateSource]
    #: `repo -> Projector`, built lazily, only when a tick has something to write.
    projector: Callable[[str], Projector]
    contents: RepoContents
    #: `repo -> MediaSource`: the images an input's text embeds (`worker/attachments.py`), built
    #: per spawn or resume whose text names one.
    media: MediaFactory
    #: `(repo, commit) -> every blob at that commit, with sizes` (`RepoContents.fresh_tree`), for
    #: what a run changed (`worker/outputs.py`).
    trees: Callable[[str, str], Awaitable[Sequence[TreeEntry]]]
    #: `(repo, base, head) -> Comparison` (`RepoContents.compare`), for the files a run changed
    #: (`worker/outputs.py`, tech-stack decision 71).
    compare: Callable[[str, str, str], Awaitable[Comparison]]
    #: `repo -> ContractGitHub` — the GitHub ref surgery gate 1
    #: (`actions.registry.fix_the_contract`), a redo and a restart (`worker.restarts`) perform.
    #: `None` leaves `kind: action` unable to run (`worker.action_steps._start_action` raises),
    #: and a redo or restart that removes anything raises.
    contract_github: Callable[[str], ContractGitHub] | None
    #: The project's own bot login, threaded into every `EvalContext` the worker builds for it
    #: (`reconcile.py`, `admission.py`) so the comment predicates' author fence never has to
    #: import settings itself, and handed to each of its runs (`inputs.run_secrets`).
    bot_login: str

    @classmethod
    def over(cls, client: GitHubClient, contents: RepoContents, *, bot_login: str) -> ProjectGitHub:
        return cls(
            client=client,
            state_source=partial(_state_source, client),
            projector=partial(GitHubProjector, client),
            contents=contents,
            media=partial(GitHubMedia, client),
            trees=contents.fresh_tree,
            compare=contents.compare,
            contract_github=partial(GitHubContractOps, client),
            bot_login=bot_login,
        )


#: `(session, project) -> ProjectGitHub | None`. It takes the tick's session because the
#: connection's secrets are read in the tick's own unit of work.
GitHubSource = Callable[[AsyncSession, Project], Awaitable[ProjectGitHub | None]]


@dataclass(slots=True)
class _Resolved:
    #: Held through the first resolution, so tickets that start together wait for it.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: bool = False
    github: ProjectGitHub | None = None


@dataclass(slots=True)
class _Tick:
    projects: dict[int, _Resolved] = field(default_factory=dict)
    listings: RepoListings = field(default_factory=RepoListings)
    #: Set when the tick ends. A task started inside it, such as a run's supervisor, keeps this
    #: object after the tick resets the variable, and must not keep reading the old connection.
    closed: bool = False


_this_tick: ContextVar[_Tick | None] = ContextVar("github_this_tick", default=None)


@contextmanager
def github_once_per_tick() -> Iterator[None]:
    """Within it, each project's GitHub is resolved once. Tasks started inside share it."""
    tick = _Tick()
    token = _this_tick.set(tick)
    try:
        yield
    finally:
        tick.closed = True
        _this_tick.reset(token)


def _state_source(client: GitHubClient, repo: str, issue_number: int) -> StateSource:
    """A source that shares the live tick's repo listings, or lists for itself outside one."""
    tick = _this_tick.get()
    shared = tick.listings if tick is not None and not tick.closed else None
    return GitHubStateSource(client, repo, issue_number, shared=shared)


async def _once_per_tick(
    project_id: int, resolve: Callable[[], Awaitable[ProjectGitHub | None]]
) -> ProjectGitHub | None:
    tick = _this_tick.get()
    if tick is None or tick.closed:
        return await resolve()
    entry = tick.projects.setdefault(project_id, _Resolved())
    async with entry.lock:
        if not entry.done:
            entry.github = await resolve()
            entry.done = True
    return entry.github


logger = logging.getLogger(__name__)

#: Projects held for want of a tracker or forge, so the warning is said once per hold.
_held: set[int] = set()


@dataclass(frozen=True, slots=True)
class ReconcileDeps:
    """Injected collaborators for one worker process."""

    #: Each project's GitHub (`github_for`). `None` leaves every GitHub project unconnected.
    #: A run's `GH_TOKEN` is minted on the project's own client (`inputs.run_secrets`).
    resolve_github: GitHubSource | None = None
    #: Tracker factories by kind, beside the shipped hosts (`host_registries`); an entry here wins.
    trackers: Mapping[str, TrackerFactory] = field(default_factory=dict)
    #: Forge factories by kind, beside the shipped hosts; an entry here wins.
    forges: Mapping[str, ForgeFactory] = field(default_factory=dict)
    now: Callable[[], datetime] = utc_now
    resolve_agent: AgentSource | None = None
    #: The registry (`runtime` rows a process names by `runtime: <slug>`): one adapter instance
    #: per row for the worker's life: an adapter holds per-run state (supervisors, children), so
    #: the one that spawned a run must be the one every later verb reaches.
    #: `None` leaves no agent able to spawn (`agent_steps` raises, which escalates as
    #: misconfiguration).
    runtime_adapters: RuntimeAdapters | None = None
    #: `(session, organization_id, slug) -> Runtime | None` — the row behind a snapshot's
    #: `runtime:`. Injected beside `resolve_agent` for the same reason: it is the tick's own unit
    #: of work that reads.
    load_runtime: Callable[[AsyncSession, int, str], Awaitable[Runtime | None]] | None = None
    #: `step_run.id -> the kind its spawn recorded`. A run's kind never changes, so the event
    #: log is read once per run per process, not on every poll.
    spawned_kinds: dict[int, str] = field(default_factory=dict)
    #: `step_run.id -> when its parked re-check last ran` (`agent_steps.recheck_parked`).
    #: Process memory, so a rolled-back tick does not undo the stamp, and the interval holds.
    parked_rechecks: dict[int, datetime] = field(default_factory=dict)
    #: `step_run.id -> the first tick that saw its session ended from the terminal with nobody
    #: seen typing` (`sessions._settle_exit`). Process memory, like `parked_rechecks`, so a
    #: rolled-back tick does not restart `EXIT_GRACE`.
    exits_seen: dict[int, datetime] = field(default_factory=dict)
    #: `ticket.id -> when its refused spawn was last asked again` (`agent_steps.spawn_cleared`),
    #: or None when the ticket's block is not a refused spawn. Process memory, as above.
    spawn_rechecks: dict[int, datetime | None] = field(default_factory=dict)
    #: `event.id` of a completion or retry request → the first tick it could not be checked
    #: (`completions.unreadable_too_long`). Process memory, like the two above.
    unreadable_requests: dict[int, datetime] = field(default_factory=dict)
    #: `ticket.id` → the `STEP_ENTERED` id `note_next_step_waiting` already judged. Process
    #: memory, like `parked_rechecks`: a steady waiting tick pays one read, not three.
    next_step_waiting_seen: dict[int, int] = field(default_factory=dict)
    sessions: SessionPolicy = field(default_factory=SessionPolicy)

    async def github_for(self, session: AsyncSession, project: Project) -> ProjectGitHub | None:
        """`project`'s GitHub, or None when it has no connection."""
        resolve = self.resolve_github
        if resolve is None:
            return None
        return await _once_per_tick(project.id, lambda: resolve(session, project))

    async def bindings_for(self, session: AsyncSession, project: Project) -> Bindings | None:
        """The tracker and forge this project uses. None when either side cannot be built."""
        trackers, forges = host_registries(self.github_for)
        return await resolve_bindings(
            session,
            project,
            trackers={**trackers, **self.trackers},
            forges={**forges, **self.forges},
        )

    async def bindings_or_hold(self, session: AsyncSession, project: Project) -> Bindings | None:
        """`bindings_for`, for the paths that hold a project without them: reconcile and
        admission. Held, not failed, on either host: a raise would count toward quarantine, which
        only a person's Requeue undoes, and the tickets should resume by themselves once the
        project is connected. So the hold is said once, at warning, and its end once more."""
        bindings = await self.bindings_for(session, project)
        if bindings is None:
            if project.id not in _held:
                _held.add(project.id)
                host = display_of(project, await tracker_kind_for(session, project))
                logger.warning(
                    "project %s is held: %s Its tickets wait and resume once it is connected.",
                    project.name,
                    host.unconfigured,
                )
        elif project.id in _held:
            _held.discard(project.id)
            logger.info("project %s is connected again; its tickets resume", project.name)
        return bindings

    async def adapter_for_run(self, session: AsyncSession, run: StepRun) -> RuntimeAdapter:
        """The one adapter that owns `run`, for every verb after the spawn.

        A run stamped with `runtime_id` belongs to that registry row's adapter of the kind its
        `agent_spawned` event recorded — from the cache when this process built it, else rebuilt
        from the row (a worker restart empties the cache; the row and the event survive).
        `session` is needed for that one cold-cache read, and `session.get` is an identity-map
        hit for the rest of the tick.
        """
        if run.runtime_id is None:
            raise RuntimeRefused(
                "this run used the deployment's default runtime, which no longer exists: exit "
                "the ticket and re-enter it on a runtime"
            )
        if self.runtime_adapters is None:
            raise RuntimeError(
                f"step_run {run.id} was spawned on runtime {run.runtime_id} but this worker has "
                "no runtime registry wired (see orchestrator.worker.context.production_deps)"
            )
        # The kind the run was spawned on, so an edit to the row since then cannot move it.
        # `None` at the spawn itself, which is the row's current kind by definition.
        kind = await self._spawned_kind(session, run)
        if kind is not None:
            cached = self.runtime_adapters.cached(run.runtime_id, kind)
            if cached is not None:
                return cached
        row = await session.get(Runtime, run.runtime_id)
        if row is None:
            raise RuntimeRefused("this run's runtime was deleted")
        return self.runtime_adapters.for_runtime(
            row, kind, held_key=await workspace_key(session, row), repoint=True
        )

    async def _spawned_kind(self, session: AsyncSession, run: StepRun) -> str | None:
        """The kind `run`'s spawn recorded, read once per run per process: it never changes."""
        if run.id is None:
            return None
        kind = self.spawned_kinds.get(run.id)
        if kind is None:
            kind = await spawned_kind(session, run.id)
            if kind is not None:
                self.spawned_kinds[run.id] = kind
        return kind


def production_deps(settings: Settings | None = None) -> ReconcileDeps:
    """Wire each project's GitHub client, observer and projector, and the runtime adapters."""
    resolved = settings if settings is not None else get_settings()

    async def github_for(session: AsyncSession, project: Project) -> ProjectGitHub | None:
        client = await pool().for_project(session, project)
        if client is None:
            return None
        return ProjectGitHub.over(
            client, pool().contents(client), bot_login=project_bot_login(project)
        )

    own_mcp_endpoints = resolved.mcp_endpoints

    async def resolve(
        session: AsyncSession, project: Project, agent: str, pins: AgentPins | None
    ) -> ResolvedAgent:
        from orchestrator.gitlab.project_client import config_source_for

        github = await _once_per_tick(project.id, lambda: github_for(session, project))
        chosen = await config_source_for(
            session,
            project,
            github=RepoConfigSource(github.contents) if github is not None else None,
        )
        if chosen is None:
            raise RuntimeError(
                f"project {project.name!r} has no connection to the forge that holds its "
                ".factorial/"
            )
        return await resolve_agent(
            session, chosen, project, agent, pins, own_mcp_endpoints=own_mcp_endpoints
        )

    return ReconcileDeps(
        resolve_github=github_for,
        runtime_adapters=RuntimeAdapters(registry_build(partial(adapter_options, resolved))),
        load_runtime=load_runtime_by_slug,
        now=utc_now,
        resolve_agent=resolve,
        sessions=SessionPolicy.from_settings(resolved),
    )


def adapter_options(settings: Settings, kind: str) -> dict[str, Any]:
    """Per-kind constructor arguments. Every kind is named explicitly and an unknown kind gets
    nothing, because passing one kind's options to another would be a guess.

    No bot login: it is the project's, so it travels with each run as `FACTORIAL_BOT_LOGIN`
    (`inputs.run_secrets`), the same value the project's `EvalContext` fences comments on.
    """
    match kind:
        case "claude_cli":
            # `local_root` is required by the adapter, not defaulted here: a default would
            # silently pick the directory an agent with permissions bypassed runs in (decision 16).
            local = settings.runtime.claude_cli
            return {
                "binary": local.claude_bin,
                "local_root": local.local_root,
                "auth_mode": local.auth_mode,
            }
        case "codex_cli":
            # `local_root` is un-defaulted for the same reason as claude_cli's (decision 34).
            codex = settings.runtime.codex_cli
            return {
                "binary": codex.codex_bin,
                "local_root": codex.local_root,
                "auth_mode": codex.auth_mode,
                "subscription_auth_file": codex.subscription_auth_file,
                "ask_tool": settings.terminal.codex_ask_tool,
            }
        case "claude_e2b" | "codex_e2b":
            # The row supplies the rest: `auth_mode` here, its team's key and template through
            # `RuntimeAdapters.for_runtime`.
            e2b = settings.runtime.e2b
            options: dict[str, Any] = {
                "state_root": e2b.state_root,
                "deployment": e2b.deployment,
                "egress": tuple(e2b.egress),
            }
            if kind == "codex_e2b":
                options["ask_tool"] = settings.terminal.codex_ask_tool
            return options
        case _:
            return {}
