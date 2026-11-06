"""The agent registry and process-version history — `.factorial/` content joined to Postgres rows.

Agent definitions are content in git, registry row in Postgres (data-model.md): the row says
which repo path and which commit, `factorial.source` reads the content there. This module chooses,
establishes and records pins; it parses no YAML itself.

Two rules the rest of the design leans on:

- Nothing runs against floating config. A NULL `agent.pinned_commit_sha` is resolved to the
  config branch's HEAD once, on first use, and written back if the sync guard accepts it.
- A config edit creates a new `process_version` row and never rewrites one (design.md §2.8): a
  ticket pinned to version 3 must still read version 3 tomorrow.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, TypedDict

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AdlcConfig, ProcessConfig
from orchestrator.db.models import Agent, Event, ProcessVersion, Project, Runtime
from orchestrator.events.types import EventType
from orchestrator.factorial.mcp_policy import (
    ALLOWLIST_KEY,
    McpAllowlistInvalid,
    check_allowed,
    parse_allowlist,
)
from orchestrator.factorial.permission_policy import (
    CEILING_KEY,
    PermissionCeilingInvalid,
    check_permissions,
    parse_ceiling,
)
from orchestrator.factorial.probe import agent_names
from orchestrator.factorial.process_policy import DEFAULT_KEY, parse_default_process
from orchestrator.factorial.source import CONFIG_BRANCH, RepoConfigSource
from orchestrator.factorial.types import ResolvedAgent

__all__ = [
    "WALK_LIMIT",
    "AgentInactive",
    "AgentNotRegistered",
    "AgentPin",
    "AgentPins",
    "AgentResolutionError",
    "ConfigCommitRefused",
    "DefaultProcessMoved",
    "McpNotAllowed",
    "PermissionNotAllowed",
    "ProcessRegistration",
    "ProcessSyncResult",
    "ProcessVersionExists",
    "RuntimeBindingInvalid",
    "WalkLimitReached",
    "adopters",
    "check_folder_runtime_bindings",
    "check_runtime_binding",
    "check_runtime_bindings",
    "is_known_commit",
    "known_base",
    "latest_process_versions",
    "register_process_versions",
    "resolve_agent",
    "upsert_agent_rows",
]

logger = logging.getLogger(__name__)


def offered_version() -> ColumnElement[bool]:
    """A snapshot a new ticket may pin: not retired, and read from the project's own repository.

    The caller joins `Project` on `ProcessVersion.project_id`. The repository match is what lets a
    repository change write nothing: the old repository's rows stop being offered at once, and so
    does any row a sync of the old repository writes after the change (issue #441).
    """
    return and_(
        ProcessVersion.retired_at.is_(None), ProcessVersion.source_repo == Project.github_repo
    )


async def latest_process_versions(
    session: AsyncSession, project_id: int
) -> dict[str, ProcessVersion]:
    """The snapshot a ticket entering each AVAILABLE lane right now would pin, in lane-key order.

    "Current" is the snapshot a sync most recently landed on (`activated_at`), so re-syncing an
    older commit makes it current again (decision 38). Ordered on `id` beside it, because one
    sync writes every lane in the same transaction and their timestamps can tie. Available is
    `offered_version`: a lane whose file the last sync stopped finding is not offerable even
    though its rows remain, and neither is one read from a repository the project has left
    (design.md §4). The page and the worker both read this, so they agree by construction.
    """
    rows = (
        (
            await session.execute(
                select(ProcessVersion)
                .join(Project, Project.id == ProcessVersion.project_id)
                .where(ProcessVersion.project_id == project_id, offered_version())
                .distinct(ProcessVersion.process_key)
                # DISTINCT ON keeps the first row per lane in this order: most recently synced wins.
                .order_by(
                    ProcessVersion.process_key,
                    ProcessVersion.activated_at.desc(),
                    ProcessVersion.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.process_key: row for row in rows}


async def lane_head(
    session: AsyncSession, project_id: int, process_key: str
) -> ProcessVersion | None:
    """The snapshot a ticket entering `process_key` right now would pin, or None when the lane is
    not offered. `latest_process_versions`'s answer for one lane, so a restart and admission
    agree."""
    return (await latest_process_versions(session, project_id)).get(process_key)


class AgentPin(TypedDict):
    """Where one agent's definition was read from when a process version was registered.

    The path travels with the sha because it is not derivable from the name at an arbitrary
    commit — `resolve_dir` reads the config folder that commit had, not the one HEAD has.
    """

    sha: str
    path: str


#: `{agent name: AgentPin}`, as `process_version.agent_pins` stores it.
AgentPins = Mapping[str, AgentPin]


class AgentResolutionError(LookupError):
    """The registry cannot produce a runnable agent definition. Always an operator problem."""


class AgentNotRegistered(AgentResolutionError):
    """No `agent` row for `(project, name)` — the process references an unregistered agent."""


class AgentInactive(AgentResolutionError):
    """The `agent` row exists but is `active = false`; someone turned it off deliberately."""


class McpNotAllowed(AgentResolutionError):
    """The agent declares MCP servers this project's operator allowlist does not permit.

    A resolution failure, not a spawn failure: the agent cannot run until an operator edits
    `project.settings.mcp_allowlist` or the repo drops the server (design.md §7.5), and failing
    here means no substrate ever sees the declaration.
    """


class PermissionNotAllowed(AgentResolutionError):
    """The agent requests GitHub App permission scopes this project's ceiling does not permit.

    A resolution failure, matching `McpNotAllowed`: the agent cannot run until an operator
    widens `project.settings.permission_ceiling` or the manifest requests less (decision S7).
    """


class RuntimeBindingInvalid(Exception):
    """`process.yaml` names a Runtime the registry cannot honour: the slug does not exist, or a
    process names none. Distinguishable so the API can 422 it. `missing` holds the sorted slugs
    that do not exist."""

    def __init__(self, message: str, *, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


class ProcessVersionExists(Exception):
    """`(project_id, source_repo, process_key, source_commit_sha)` was snapshotted by a concurrent
    writer.

    The lost insert race alone. Re-syncing a commit this project already holds is the `unchanged`
    outcome and never an error — a sha is immutable, so the row that exists IS this config.
    Distinguishable so the API can 409 the race.
    """

    def __init__(self, project_id: int, process_key: str, sha: str) -> None:
        super().__init__(
            f"process {process_key!r} at {sha[:7]} was written for project {project_id} by "
            "another sync while this one was running; sync again"
        )
        self.project_id = project_id
        self.process_key = process_key
        self.sha = sha


class ConfigCommitRefused(ValueError):
    """The sync guard: the orchestrator did not write this commit, and no human merged it into
    the config branch through a pull request."""

    def __init__(self, sha: str) -> None:
        super().__init__(
            f"Factorial didn't sync {sha[:7]}. Commit and sync didn't write it, and no pull "
            f"request merged it into `{CONFIG_BRANCH}`. If it is a direct push to "
            f"`{CONFIG_BRANCH}`, review and adopt it on the Process config page, or reset the "
            "branch."
        )
        self.sha = sha


@dataclass(frozen=True, slots=True)
class DefaultProcessMoved:
    """The sync re-pointed `project.settings.default_process`, because this commit retired it.

    A project whose default names nothing admits no issue that does not carry its own process
    label, which is a whole factory stopped by a file deletion — and the operator who renamed the
    lane has no way to fix a setting the page will not offer a retired key for. So the sync moves
    the default to a lane the commit does offer and says so, rather than refusing until the
    setting is changed by hand.

    The replacement is the first offered key in sort order. With one lane, which is the case a
    rename produces, that is the only answer; with several it is arbitrary but deterministic, and
    naming it in the response is what lets an operator pick a different one.
    """

    previous: str
    now: str


# --- agents -------------------------------------------------------------------------------------


async def resolve_agent(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    agent_name: str,
    pins: AgentPins | None = None,
    *,
    own_mcp_endpoints: frozenset[tuple[str, int]],
    repo: str | None = None,
) -> ResolvedAgent:
    """Registry row -> a fully read agent definition at a concrete sha.

    `repo` is where a pin is read from: the pinned version's `source_repo`, because its commit
    exists only there (None = the project's `github_repo`). Without a pin the row's commit is read
    from the project's repository, as it always was: the row records no repository.

    `pins` is the ticket's own `process_version.agent_pins`, and wins: the snapshot recorded where
    this agent was read from when it was registered, and a ticket's agents freeze with its steps.
    Falling back to the row is what a snapshot from before revision 0013 gets, and what a version
    POSTed as JSON with no commit to pin to gets — both resolve as they did before the column
    existed, which is why no backfill was needed.

    The row is still consulted either way, for `active` and `runtime_kind`: those are operator
    controls, not definition, and a kill switch that a pin could outlast would be no kill switch.

    Establishes the pin if the row has none and no pin covers it. Flushes but does not commit: the
    caller owns the transaction, and the write-back must land with whatever else that unit of work
    is doing.

    `own_mcp_endpoints` are the (host, port) pairs of factorial's own MCP server. Any agent server
    on one is refused, allowlisted or not (design.md §7.5 item 9).
    """
    row = (
        await session.execute(
            select(Agent).where(Agent.project_id == project.id, Agent.name == agent_name)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AgentNotRegistered(
            f"project {project.name!r} has no agent named {agent_name!r}; register it by posting "
            f"the process version that references it (its manifest belongs at "
            f"{source.agent_manifest_path(agent_name)} in {project.github_repo})"
        )
    if not row.active:
        raise AgentInactive(
            f"agent {agent_name!r} in project {project.name!r} is inactive; re-activate the "
            "registry row before a step can run it"
        )

    pin = (pins or {}).get(agent_name)
    if pin is not None:
        sha, path = pin["sha"], pin["path"]
        read_from = repo or project.github_repo
    else:
        sha, path = row.pinned_commit_sha, row.repo_path
        read_from = project.github_repo
        if sha is None:
            sha = await source.resolve_ref(project.github_repo, None)
            try:
                await _refuse_unknown_commit(session, source, project, sha)
            except ConfigCommitRefused as exc:  # an operator's problem, so the worker escalates
                raise AgentResolutionError(str(exc)) from exc
            row.pinned_commit_sha = sha
            await session.flush()
            # The audit trail for "why did this agent's behaviour change?".
            logger.info(
                "pinned agent %s (project %s) to %s@%s — first use, was unpinned",
                agent_name,
                project.name,
                project.github_repo,
                sha,
            )

    resolved = await source.load_agent(read_from, path, sha)
    _enforce_mcp_allowlist(project, resolved, own_mcp_endpoints)
    _enforce_permission_ceiling(project, resolved)
    return resolved


def _enforce_mcp_allowlist(
    project: Project, resolved: ResolvedAgent, own_endpoints: frozenset[tuple[str, int]]
) -> None:
    """The trust boundary: the repo defines MCP servers, the operator says which may run.

    Checked on every resolution, not once at pin time: the allowlist is operator state that
    changes independently of the pin, and a server revoked at 10:00 must not keep running
    because the agent was pinned at 09:00 (design.md §7.5).
    """
    if not resolved.mcp_servers:
        return  # the common case; no settings parsing, no allowlist needed
    try:
        allowlist = parse_allowlist(project.settings)
    except McpAllowlistInvalid as exc:
        raise McpNotAllowed(
            f"project {project.name!r} has an unusable MCP allowlist, so agent "
            f"{resolved.name!r} cannot run: {exc}"
        ) from exc

    problems = check_allowed(resolved.mcp_servers, allowlist, own_endpoints=own_endpoints)
    if problems:
        raise McpNotAllowed(
            f"agent {resolved.name!r} in project {project.name!r} declares MCP server(s) this "
            "project does not permit:\n  - "
            + "\n  - ".join(problems)
            + f"\nEdit project.settings.{ALLOWLIST_KEY} to permit them, or drop them from "
            f"{resolved.repo}@{resolved.commit_sha[:7]}."
        )


def _enforce_permission_ceiling(project: Project, resolved: ResolvedAgent) -> None:
    """The permission counterpart to `_enforce_mcp_allowlist` (decision S7).

    Checked on every resolution for the same reason: the ceiling is operator state that changes
    independently of the pin. Unlike the MCP allowlist there is no "declares nothing -> skip"
    shortcut — a manifest with no `permissions:` key still requests the default profile
    (`permission_policy` module docstring) and still has to clear the project's ceiling.
    """
    try:
        ceiling = parse_ceiling(project.settings)
    except PermissionCeilingInvalid as exc:
        raise PermissionNotAllowed(
            f"project {project.name!r} has an unusable {CEILING_KEY}, so agent "
            f"{resolved.name!r} cannot run: {exc}"
        ) from exc

    problems = check_permissions(resolved.manifest.permissions, ceiling)
    if problems:
        raise PermissionNotAllowed(
            f"agent {resolved.name!r} in project {project.name!r} requests permission(s) this "
            "project's ceiling does not permit:\n  - "
            + "\n  - ".join(problems)
            + f"\nEdit project.settings.{CEILING_KEY} to permit them, or narrow "
            f"{resolved.repo}@{resolved.commit_sha[:7]}'s `permissions:` block."
        )


async def upsert_agent_rows(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    process: ProcessConfig,
    sha: str,
    *,
    repo: str | None = None,
) -> dict[str, AgentPin]:
    """Create or re-pin an `agent` row for every agent the process references, read from `repo`
    (None = the project's `github_repo`).

    The only path by which an `agent` row comes into existence, so registering a process version
    is what makes its agents runnable. Rows for agents the process no longer mentions are left
    alone, not deactivated: a ticket pinned to an older `process_version` still runs those steps
    and needs the row to find their definition.

    Returns the `(sha, path)` each agent was pinned at, for the caller to record on the snapshot.
    The row's pin is project-wide and the next registration overwrites it; the returned map is
    what makes a ticket's agents survive that.
    """
    names = agent_names(process)
    if not names:
        return {}

    existing = {
        row.name: row
        for row in (
            await session.execute(
                select(Agent).where(Agent.project_id == project.id, Agent.name.in_(names))
            )
        )
        .scalars()
        .all()
    }

    # The folder this sha actually has, not this source's default: `repo_path` is read back at
    # `pinned_commit_sha`, so a path naming a folder the commit lacks resolves nowhere.
    root = await source.resolve_dir(repo or project.github_repo, sha)

    pins: dict[str, AgentPin] = {}
    for name in names:
        path = source.agent_manifest_path(name, root=root)
        row = existing.get(name)
        if row is None:
            row = Agent(
                project_id=project.id,
                name=name,
                repo_path=path,
                pinned_commit_sha=sha,
                # runtime_kind stays NULL: inherit project.settings.agent_runtime unless an
                # operator sets a per-agent override.
            )
            session.add(row)
        else:
            row.repo_path = path
            row.pinned_commit_sha = sha
            # `active` is untouched: re-registering the process must not undo a deactivation.
        pins[name] = AgentPin(sha=sha, path=path)

    await session.flush()
    return pins


# --- process versions ---------------------------------------------------------------------------


async def check_runtime_binding(
    session: AsyncSession, organization_id: int, config: ProcessConfig
) -> None:
    """`check_runtime_bindings` for the one process an edit commit submits."""
    await check_runtime_bindings(
        session, organization_id, {config.name: config}, submitted=config.name
    )


async def check_runtime_bindings(
    session: AsyncSession,
    organization_id: int,
    configs: Mapping[str, ProcessConfig],
    *,
    submitted: str | None = None,
) -> None:
    """The trust boundary for `runtime: <slug>`, checked before a snapshot is written.

    The repo file selects a Runtime; the registry row an operator created defines it (design.md
    §7.5, the same direction as `_enforce_mcp_allowlist`). Every process names one, since its
    type is what says which CLI runs the agents, and the slug must name a row in the project's
    organization. Every lane is judged and one refusal names all that fail, so the operator
    fixes them in one pass. The lane `submitted` reads as "This process"; any other is named.
    """
    missing: dict[str, list[str]] = {}
    unnamed: list[str] = []
    for key, config in sorted(configs.items()):
        if config.runtime is None:
            unnamed.append(key)
        elif not await _runtime_exists(session, organization_id, config.runtime):
            missing.setdefault(config.runtime, []).append(key)

    def process(key: str) -> str:
        return "This process" if key == submitted else f"The process {key!r}"

    sentences: list[str] = []
    if len(missing) == 1 and len(next(iter(missing.values()))) == 1:
        ((slug, (key,)),) = missing.items()
        sentences.append(
            f"{process(key)} names runtime {slug!r}, which does not exist. Create it on the "
            "Runtimes page with that slug, then sync again."
        )
    elif missing:
        named = ", ".join(f"{slug!r} ({', '.join(missing[slug])})" for slug in sorted(missing))
        sentences.append(
            f"The processes name runtimes this organization doesn't have: {named}. Create "
            "each on the Runtimes page with that slug, then sync again."
        )
    sentences.extend(
        f"{process(key)} names no runtime. Add `runtime: <slug>` with a runtime from the "
        "Runtimes page. Its type says which CLI runs the agents."
        for key in unnamed
    )
    if sentences:
        raise RuntimeBindingInvalid(" ".join(sentences), missing=tuple(sorted(missing)))


async def check_folder_runtime_bindings(
    session: AsyncSession, organization_id: int, loaded: AdlcConfig, *, submitted: str
) -> None:
    """`check_runtime_bindings` for EVERY lane of a folder already loaded — the tree an edit
    commit will produce, judged the way `register_process_versions` judges it at the new sha.
    An edit can change another lane's file, so the submitted lane is not enough."""
    await check_runtime_bindings(session, organization_id, loaded.processes, submitted=submitted)


async def _runtime_exists(session: AsyncSession, organization_id: int, slug: str) -> bool:
    """A process never binds another tenant's runtime, whatever its slug."""
    row = await session.execute(
        select(Runtime.id).where(Runtime.organization_id == organization_id, Runtime.slug == slug)
    )
    return row.first() is not None


@dataclass(frozen=True, slots=True)
class ProcessRegistration:
    """What one sync did to one lane.

    `created` — a new snapshot row, for a commit this lane had not been read at. `unchanged` —
    this project already holds that lane at that commit, so nothing about it moved. `retired` —
    the lane's file is gone from this commit.

    There is no "you forgot to bump the version" outcome any more, because there is no number to
    bump: a commit identifies a snapshot, and a sha is immutable, so `unchanged` needs no content
    comparison to be certain. Editing the file produces a new commit, which is a new row.
    """

    process_key: str
    outcome: Literal["created", "unchanged", "retired"]
    row: ProcessVersion | None = None


@dataclass(frozen=True, slots=True)
class ProcessSyncResult:
    """One sha, one row per lane the project has or the commit offers."""

    sha: str
    results: tuple[ProcessRegistration, ...]
    #: Set when this sync had to re-point the project's default lane; None when it did not.
    default_moved: DefaultProcessMoved | None = None


async def register_process_versions(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    *,
    ref: str | None = None,
    sha: str | None = None,
) -> ProcessSyncResult:
    """Snapshot every process the repo offers at `ref` (None = the config branch), at ONE sha.

    `sha` skips resolving `ref`, which is still what the snapshots record: a caller that just
    wrote a commit syncs exactly that one, never whatever the branch has moved to since.

    One sha across all lanes preserves the guarantee a single registration had: a process and
    every agent it references move together or not at all. It is also what makes the route usable
    with several lanes: a commit touching one lane leaves every other lane at a commit this
    project already holds, which is `unchanged` and not a refusal.

    Retirement is decided here: a key the commit offers is registered or confirmed and
    un-retired, and a key the project has that the commit does NOT offer is retired. The commit
    history is not a menu — without this, deleting a lane's file would leave it offerable for
    ever. Every row read from another repository than this sync's is retired too, whatever its
    key: the project left that repository, and its lanes are not files that came back. Those rows
    are already unoffered before this runs (`offered_version`), so a sync of the old repository
    that lands after a move cannot offer them again either. Tickets in flight are untouched
    either way: they hold `process_version_id` and read the row by id.

    Nothing is written until every lane has parsed and every `runtime:` binding has been checked.
    A folder that does not parse is not a partial state worth committing, and neither is one
    whose second lane names a Runtime that does not exist.

    A default lane this commit retires is re-pointed rather than refused (`DefaultProcessMoved`),
    and the caller reports the move: refusing left the operator holding a project that admits
    nothing, with no way to clear the setting from the page.

    `ConfigCommitRefused` comes first of all, for every ref and on a re-sync too
    (`_refuse_unknown_commit`).
    """
    # Read once: every row this sync writes is stamped with the repository it read.
    repo = project.github_repo
    if sha is None:
        sha = await source.resolve_ref(repo, ref)
    await _refuse_unknown_commit(session, source, project, sha)
    configs = await source.load_processes(repo, sha)
    await check_runtime_bindings(session, project.organization_id, configs)

    existing = list(
        (
            await session.execute(
                select(ProcessVersion).where(ProcessVersion.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    going = sorted({row.process_key for row in existing} - set(configs))
    default = parse_default_process(project.settings)
    moved: DefaultProcessMoved | None = None
    if default is not None and default in going:
        # `load_processes` never returns nothing — an empty `processes/` falls back to the legacy
        # root file and raises when that is missing too — so there is always a lane to move to.
        moved = DefaultProcessMoved(previous=default, now=sorted(configs)[0])
        await session.refresh(project, attribute_names=["settings"], with_for_update=True)
        project.settings = {**project.settings, DEFAULT_KEY: moved.now}
        logger.warning(
            "project %s: default process %r is not in %s@%s; default is %r now",
            project.name,
            moved.previous,
            repo,
            sha,
            moved.now,
        )

    by_key_sha = {
        (row.process_key, row.source_commit_sha): row for row in existing if row.source_repo == repo
    }
    results: list[ProcessRegistration] = []
    for key in sorted(configs):
        row = by_key_sha.get((key, sha))
        if row is not None:
            # A sha is immutable, so the row that exists IS this config — nothing to compare. It
            # still becomes current, agents included: re-syncing an older commit is a rollback.
            # clock_timestamp, not now(): now() is the transaction's start, which a newer row
            # inserted earlier in the same transaction shares.
            await session.execute(
                update(ProcessVersion)
                .where(ProcessVersion.id == row.id)
                .values(activated_at=func.clock_timestamp())
                .execution_options(synchronize_session=False)
            )
            await upsert_agent_rows(session, source, project, configs[key], sha, repo=repo)
            await session.refresh(row)
            results.append(ProcessRegistration(process_key=key, outcome="unchanged", row=row))
            continue
        results.append(
            ProcessRegistration(
                process_key=key,
                outcome="created",
                row=await _insert_version(
                    session, source, project, key, configs[key], sha, ref, repo=repo
                ),
            )
        )

    # After the inserts, so a failed one cannot leave a lane both unwritten and un-retired.
    now = datetime.now(UTC)
    newly_retired: list[str] = []
    for row in existing:
        if row.source_repo == repo and row.process_key in configs:
            row.retired_at = None  # a lane whose file came back keeps every commit it had
        elif row.retired_at is None:
            row.retired_at = now
            if row.process_key not in configs:
                newly_retired.append(row.process_key)
    results.extend(
        ProcessRegistration(process_key=key, outcome="retired")
        for key in sorted(set(newly_retired))
    )
    await session.flush()

    logger.info(
        "synced %s process(es) for project %s from %s@%s: %s",
        len(results),
        project.name,
        repo,
        sha,
        ", ".join(f"{r.process_key}={r.outcome}" for r in results),
    )
    return ProcessSyncResult(
        sha=sha,
        results=tuple(sorted(results, key=lambda r: r.process_key)),
        default_moved=moved,
    )


async def _refuse_unknown_commit(
    session: AsyncSession, source: RepoConfigSource, project: Project, sha: str
) -> None:
    if not await is_known_commit(session, source, project, sha):
        raise ConfigCommitRefused(sha)


_WRITTEN_EVENTS: tuple[str, ...] = (
    str(EventType.CONFIG_BRANCH_CREATED),
    str(EventType.CONFIG_COMMITTED),
    str(EventType.CONFIG_HEAD_ADOPTED),
)


#: Commits `_known_walk` reads down a first-parent chain before it gives up and calls the head
#: unknown.
WALK_LIMIT: Final = 50


async def is_known_commit(
    session: AsyncSession, source: RepoConfigSource, project: Project, sha: str
) -> bool:
    """A commit the orchestrator wrote (its seed, or a builder commit an operator pressed for),
    a head an operator adopted, or the result of a pull request into the config branch that a
    human merged onto a known commit. The base must be known too, or a human merge would carry an
    agent's direct push beneath it. A rebase merge of several commits leaves unmerged commits on
    that chain, so it is refused; merge or squash. The commit author is never consulted: an agent
    pushing with git sets it freely."""
    try:
        return await _known_walk(session, source, project, sha, first_only=True) == sha
    except WalkLimitReached:
        return False


class WalkLimitReached(LookupError):
    """`known_base` read `WALK_LIMIT` commits without reaching the chain's root, so whether a
    known commit lies below is not known."""


async def known_base(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    head: str,
    *,
    limit_raises: bool = False,
) -> str | None:
    """The newest known commit on `head`'s first-parent chain, the reset target for a head no
    one vouches for. The ruleset forbids force-push, so the last good head is an ancestor.

    None both when the root was reached with nothing known and when the walk gave up first;
    `limit_raises` raises `WalkLimitReached` for the second, for a caller to whom only the first
    means "no base"."""
    try:
        return await _known_walk(session, source, project, head, first_only=False)
    except WalkLimitReached:
        if limit_raises:
            raise
        return None


async def adopters(session: AsyncSession, project_id: int) -> dict[str, str]:
    """Who adopted each head an operator vouched for, by sha."""
    rows = await session.execute(
        select(Event.details["sha"].astext, Event.details["requested_by"].astext).where(
            Event.project_id == project_id,
            Event.type == str(EventType.CONFIG_HEAD_ADOPTED),
        )
    )
    return {sha: by for sha, by in rows.all()}


async def _known_walk(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    sha: str,
    *,
    first_only: bool,
) -> str | None:
    written = set(
        (
            await session.execute(
                select(Event.details["sha"].astext).where(
                    Event.project_id == project.id, Event.type.in_(_WRITTEN_EVENTS)
                )
            )
        )
        .scalars()
        .all()
    )
    # The top of the run of human merges directly above the commit being read.
    top: str | None = None
    for _ in range(WALK_LIMIT):
        if sha in written:
            return top or sha
        if (await source.merge_provenance(project.github_repo, sha)).merged_by_human:
            top = top or sha
        elif first_only:
            return None
        else:
            top = None
        parents = await source.commit_parents(project.github_repo, sha)
        if not parents:
            # A human merge with nothing beneath it carries nothing unvetted.
            return top
        sha = parents[0]
    raise WalkLimitReached(sha)


async def _insert_version(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    key: str,
    config: ProcessConfig,
    sha: str,
    ref: str | None,
    *,
    repo: str,
) -> ProcessVersion:
    """One new snapshot row plus the agent pins that freeze what its steps will execute.

    The insert runs inside a savepoint because an IntegrityError deactivates the transaction it
    happened in, and the caller still has the other lanes to write.
    """
    row = ProcessVersion(
        project_id=project.id,
        process_key=key,
        # by_alias keeps the YAML spelling, so the snapshot round-trips back into the same models
        # the loader produces (orchestrator.config docstring).
        content=config.model_dump(mode="json", by_alias=True),
        source_repo=repo,
        source_commit_sha=sha,
        # The request, not the resolution: a sha cannot be read back as the branch that carried
        # it, and the next sync wants to offer the same ref again (revision 0006).
        source_ref=CONFIG_BRANCH if ref is None else ref,
    )
    try:
        async with session.begin_nested():  # savepoint — see the docstring
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise ProcessVersionExists(project.id, key, sha) from exc

    # After the insert, because the pins are the return value of establishing the rows. An empty
    # map is still a map: it says "registered from a commit, references no agents", which a NULL
    # (a legacy row, or a JSON upload with nothing to pin to) does not.
    row.agent_pins = dict(await upsert_agent_rows(session, source, project, config, sha, repo=repo))
    await session.flush()
    await session.refresh(row)  # created_at is a server default
    return row
