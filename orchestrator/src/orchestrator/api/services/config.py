"""Config read, validate, commit and sync: the path behind the builder's Commit and sync, shared
by the dashboard routes and the MCP config tools (design.md §7.7, §7.17).

`_prepare` is the one gate every surface passes, so a validate answers exactly what a commit
would. On every surface but the dashboard (the MCP server, and the assistant's approved
proposals) it also refuses what a model must not rewrite: any file the commit would write or edit
under `agents/`, where the prompt-level prohibitions live, or under `skills/`, which load into
agents' context the same way, and any change to who approves a gate
(`_refuse_approver_changes`). Those stay behind a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from githubkit.exception import RequestFailed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import ConfigCommitWriter
from orchestrator.api.readiness import (
    default_or_note,
    forge_of,
    parse_snapshot,
    reset_command,
    unknown_head_detail,
)
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import (
    Conflict,
    Invalid,
    NotFound,
    UpstreamFailed,
    as_sentence,
    render,
)
from orchestrator.api.services.refs import ticket_source
from orchestrator.config import (
    KNOWN_PLACEHOLDERS,
    ConfigError,
    GateStep,
    ProcessConfig,
    TemplateError,
    check_process_key,
    expand_principals,
    parse_process,
)
from orchestrator.config.lint import LintWarning, lint_process
from orchestrator.config.loader import (
    AGENT_FILE,
    AGENTS_DIR,
    MCP_DIR,
    MCP_SUFFIX,
    PROCESS_FILE,
    PROCESS_PREFIX,
    PROCESS_SUFFIX,
    PROCESSES_DIR,
    SKILLS_DIR,
)
from orchestrator.config.scaffold import ScaffoldBundle
from orchestrator.config.scaffold.assemble import (
    CONFIG_DIR,
    EditConflict,
    FileEdit,
    SelectionError,
    assemble,
    process_bundle_path,
)
from orchestrator.config.scaffold.catalog import Forge
from orchestrator.config.scaffold.propose import ComponentProposal, Proposal, propose
from orchestrator.config.scaffold.validate import validate_bundle
from orchestrator.db.models import ProcessVersion, Project, TicketSource, TicketSourceKind
from orchestrator.events import EventType, append
from orchestrator.events.types import RequestedVia
from orchestrator.factorial import (
    CONFIG_BRANCH,
    ConfigCommitRefused,
    ProcessVersionExists,
    RepoConfigSource,
    register_process_versions,
)
from orchestrator.factorial.inventory import ConfigInventory, classify, config_inventory
from orchestrator.factorial.probe import AgentResolution, agent_gaps, resolve_agents
from orchestrator.factorial.registry import (
    ProcessSyncResult,
    RuntimeBindingInvalid,
    check_folder_runtime_bindings,
    check_runtime_binding,
    is_known_commit,
    known_base,
    latest_process_versions,
)
from orchestrator.github import ConfigBranchMoved, ContentsNotFound, TreeEntry
from orchestrator.github.client import refusal_reason
from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.urls import gitlab_commit_url

__all__ = [
    "ConfigCheck",
    "ConfigCommitResult",
    "ConfigDraft",
    "ConfigRead",
    "FileText",
    "LaneConfig",
    "LaneRef",
    "NewFile",
    "PlannedWrite",
    "Surface",
    "SyncedConfig",
    "check_config",
    "commit_config",
    "git_blob_sha",
    "preview_config",
    "read_config",
    "sync_config",
]

log = logging.getLogger(__name__)

#: Every surface but "dashboard" is guarded.
Surface = Literal["dashboard", "mcp", "chat"]

WriteOrigin = Literal["process", "file", "edit", "stub"]

_AGENTS_PREFIX = f"{CONFIG_DIR}/{AGENTS_DIR}/"
_SKILLS_PREFIX = f"{CONFIG_DIR}/{SKILLS_DIR}/"
_DASHBOARD_ONLY = (_AGENTS_PREFIX, _SKILLS_PREFIX)

#: Every placeholder kept as written, so expanding an approver list resolves its roles and
#: nothing else.
_AS_WRITTEN = {name: f"{{{name}}}" for name in KNOWN_PLACEHOLDERS}


@dataclass(frozen=True, slots=True)
class NewFile:
    """A file from the proposal. `body=None` takes the catalog's own body."""

    path: str
    body: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigDraft:
    content: str
    files: list[NewFile] = field(default_factory=list)
    edits: list[FileEdit] = field(default_factory=list)
    #: Files the operator created, by path. Dashboard-only, like the agent files.
    created: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: str
    origin: WriteOrigin


@dataclass(frozen=True, slots=True)
class ConfigCheck:
    """`valid` means a commit of this draft would be made. `errors` are the parser's; `blockers`
    are the refusals a commit would answer with."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[LintWarning] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    base_sha: str | None = None
    would_write: list[PlannedWrite] = field(default_factory=list)
    offered: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncedConfig:
    result: ProcessSyncResult
    #: One line per referenced agent that does not resolve at the synced sha.
    agent_warnings: list[str]


@dataclass(frozen=True, slots=True)
class ConfigCommitResult:
    sha: str
    url: str
    #: False when the head already held every file: nothing was committed, the head was synced.
    committed: bool
    components: tuple[ComponentProposal, ...]
    written: list[str]
    sync: SyncedConfig | None
    sync_error: str | None


@dataclass(frozen=True, slots=True)
class LaneRef:
    process: str
    version_id: int
    sha: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class LaneConfig:
    row: ProcessVersion
    yaml: str
    inventory: ConfigInventory
    agents: list[AgentResolution]


@dataclass(frozen=True, slots=True)
class FileText:
    path: str
    content: str
    blob_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ConfigRead:
    lanes: list[LaneRef]
    lane: LaneConfig | None = None
    file: FileText | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    config: ProcessConfig
    base_sha: str
    proposal: Proposal
    bundle: ScaffoldBundle
    blobs: Mapping[str, str]


def git_blob_sha(text: str) -> str:
    """The sha git gives a blob holding `text`, as a tree listing reports it."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _commit_url(
    repo: str, sha: str, *, project: Project | None = None, source: TicketSource | None = None
) -> str:
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        settings = project.settings if project is not None else {}
        return gitlab_commit_url(settings, repo, sha)
    return f"https://github.com/{repo}/commit/{sha}"


def _parse_draft(content: str, project: Project) -> ProcessConfig:
    try:
        return parse_process(content, origin=f"project {project.id} config-commit")
    except ConfigError as exc:
        raise Invalid(exc.detail) from exc


async def _branch_head(source: RepoConfigSource, project: Project) -> str:
    """The config branch's head, or a refusal saying where to create it."""
    try:
        return await source.resolve_ref(project.github_repo, None)
    except ContentsNotFound as exc:
        raise Conflict(str(exc)) from exc


async def _propose_at(
    source: RepoConfigSource, project: Project, config: ProcessConfig, sha: str, *, forge: Forge
) -> Proposal:
    """Preview and commit must offer the same proposal: commit's membership check (§7.7, decision
    33) recomputes what preview showed. So both take `forge` from `forge_of` on the project's
    ticket source."""
    repo = project.github_repo
    agents, skills, mcp = await asyncio.gather(
        source.agent_names(repo, sha),
        source.shared_skill_names(repo, sha),
        source.mcp_server_names(repo, sha),
    )
    return propose(
        config, forge=forge, present_agents=agents, present_skills=skills, present_mcp=mcp
    )


async def preview_config(
    source: RepoConfigSource, project: Project, content: str, *, forge: Forge
) -> Proposal:
    """What a commit of `content` would create, read against the branch head. Writes nothing."""
    config = _parse_draft(content, project)
    head = await _branch_head(source, project)
    return await _propose_at(source, project, config, head, forge=forge)


async def _refuse_legacy_layout(source: RepoConfigSource, project: Project, base_sha: str) -> None:
    """Refuse any edit to a repo still carrying the root `.factorial/process.yaml`.

    The writer emits `processes/process-<name>.yaml` and nothing else (decision 37), so writing
    beside the old file would create the mixed layout the loader refuses at sync — every edit,
    not only one that adds a second lane. Migration is one `git mv` the operator makes
    deliberately; there is no automatic move and no button that makes one.

    Checked against the ACTUAL base commit. The bundle validator works in a temp directory
    holding only the submitted files, so it cannot see the repo file this would collide with.
    """
    root = await source.resolve_dir(project.github_repo, base_sha)
    legacy = f"{root}/{PROCESS_FILE}"
    try:
        await source.read_file(project.github_repo, legacy, base_sha)
    except ContentsNotFound:
        return
    raise Conflict(
        f"{project.github_repo} still keeps its process at {legacy}. The editor writes "
        f"{root}/{PROCESSES_DIR}/{PROCESS_PREFIX}<name>{PROCESS_SUFFIX}, and a repository "
        f"that holds both layouts is refused at sync. Move it first:\n"
        f"  git mv {legacy} {root}/{PROCESSES_DIR}/"
        f"{PROCESS_PREFIX}<name>{PROCESS_SUFFIX}\n"
        "Here <name> is the file's own `name:` field."
    )


def _parsed_by_loader(path: str) -> bool:
    """Whether `load_config_folder` reads this listed file's content, not only its existence."""
    parts = path.split("/")[1:]
    match len(parts):
        case 1:
            return parts[0] == PROCESS_FILE
        case 2:
            folder, name = parts
            if folder == PROCESSES_DIR:
                return name.startswith(PROCESS_PREFIX) and name.endswith(PROCESS_SUFFIX)
            return folder == MCP_DIR and name.endswith(MCP_SUFFIX)
        case 3:
            return parts[0] == AGENTS_DIR and parts[2] == AGENT_FILE
        case 4:
            return parts[0] == AGENTS_DIR and parts[2] == MCP_DIR and parts[3].endswith(MCP_SUFFIX)
        case _:
            return False


async def _read_files(
    source: RepoConfigSource, repo: str, sha: str, listing: Sequence[TreeEntry]
) -> dict[str, str]:
    """The folder an edit commit is validated against, at `sha`.

    Only the files the loader parses are read; every other path stands as an empty file, since
    the loader only checks that prompts and `SKILL.md` exist. Reading them all would refuse every
    edit on a repo holding one binary or over-large file the loader never opens.
    """

    async def read(path: str) -> str:
        if not _parsed_by_loader(path):
            return ""
        try:
            return await source.read_file(repo, path, sha)
        except ValueError as exc:  # not UTF-8, or over the contents API's size limit
            raise Invalid(
                f"{path} cannot be read as text, so the config cannot be checked: "
                f"{as_sentence(str(exc))}"
            ) from exc

    try:
        texts = await asyncio.gather(*(read(entry.path) for entry in listing))
    except ContentsNotFound as exc:
        raise UpstreamFailed(f"{as_sentence(str(exc))} Try again.") from exc
    return {entry.path: text for entry, text in zip(listing, texts, strict=True)}


def _refuse_agent_paths(draft: ConfigDraft) -> None:
    for path in sorted({f.path for f in draft.files} | {e.path for e in draft.edits}):
        if path.startswith(_AGENTS_PREFIX):
            raise Invalid(
                f"{path} is dashboard-only. The files under {_AGENTS_PREFIX} hold the rules an "
                "agent works under, so a person commits them.",
                hint="Drop it from files and edits. Add or change agents from the dashboard.",
            )
        if path.startswith(_SKILLS_PREFIX):
            raise Invalid(
                f"{path} is dashboard-only. Shared skills under {_SKILLS_PREFIX} load into "
                "agents' context like their prompts, so a person commits them.",
                hint="Drop it from files and edits. Add or change shared skills from the "
                "dashboard.",
            )


def _approver_view(config: ProcessConfig) -> dict[str, tuple[tuple[str, ...], str | None]]:
    """Each gate's approvers, roles expanded so a change to a role's members shows here too."""
    view: dict[str, tuple[tuple[str, ...], str | None]] = {}
    for step in config.steps:
        if not isinstance(step, GateStep):
            continue
        try:
            approvers = expand_principals(step.gate.approvers, config.roles, _AS_WRITTEN)
        except TemplateError:
            approvers = step.gate.approvers
        view[step.name] = (tuple(sorted(approvers)), step.gate.approver_permission)
    return view


async def _base_approvers(
    source: RepoConfigSource, repo: str, path: str, base_sha: str
) -> dict[str, tuple[tuple[str, ...], str | None]]:
    try:
        text = await source.read_file(repo, path, base_sha)
    except ContentsNotFound as exc:
        raise UpstreamFailed(
            f"Factorial couldn't read {path} from the repository. Try again."
        ) from exc
    try:
        return _approver_view(parse_process(text, origin=f"{repo}:{path}@{base_sha[:7]}"))
    except ConfigError as exc:
        raise Invalid(
            f"{path} does not parse on the {CONFIG_BRANCH} branch, so a change to its gate "
            "approvers cannot be ruled out. Make this change from the dashboard."
        ) from exc


async def _refuse_approver_changes(
    source: RepoConfigSource, repo: str, prepared: _Prepared
) -> None:
    """On a lane the branch has, adding, removing or renaming a gate is an approver change, as is
    a change to a gate's `approvers` or `approverPermission`. A lane the branch lacks may add
    gates only with both left unset: no approvers and no `approverPermission`."""
    for path in sorted(prepared.bundle.files):
        if classify(path, root=CONFIG_DIR) != "process":
            continue
        try:
            config = parse_process(prepared.bundle.files[path], origin=path)
        except ConfigError as exc:
            raise Invalid(exc.detail) from exc
        if path in prepared.blobs:
            before = await _base_approvers(source, repo, path, prepared.base_sha)
            after = _approver_view(config)
            changed = [g for g in sorted(set(before) | set(after)) if before.get(g) != after.get(g)]
            if changed:
                raise Invalid(
                    f"{path}: gate {changed[0]!r} is added, removed or renamed, or changes "
                    "`approvers` or `approverPermission`. That change is dashboard-only, "
                    "because a person decides who approves.",
                    tool_hint="Keep every gate, and its approver fields, as "
                    "factorial_config_read shows them.",
                )
            continue
        named = sorted(
            step.name
            for step in config.steps
            if isinstance(step, GateStep)
            and (step.gate.approvers or step.gate.approver_permission is not None)
        )
        if named:
            raise Invalid(
                f"{path}: new gate {named[0]!r} sets `approvers` or `approverPermission`. That is "
                "dashboard-only, because a person decides who approves.",
                hint="Leave both unset here and set them from the dashboard.",
            )


async def _prepare(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    draft: ConfigDraft,
    *,
    surface: Surface,
    expected_head: str | None = None,
) -> _Prepared:
    config = _parse_draft(draft.content, project)
    try:
        check_process_key(config.name, f"project {project.id} config-commit")
    except ConfigError as exc:
        raise Invalid(exc.detail) from exc
    if surface != "dashboard":
        _refuse_agent_paths(draft)
        if draft.created:
            raise Invalid(
                f"{min(draft.created)} is a new file, and new files are dashboard-only, "
                "because a person names every path the commit invents.",
                hint="Drop it, and create files from the dashboard's config editor.",
            )

    repo = project.github_repo
    # One sha for every read and the commit's parent: they must never see different states.
    base_sha = await _branch_head(source, project)
    if expected_head is not None and base_sha != expected_head:
        raise Conflict(
            f"The {CONFIG_BRANCH} branch moved since this was proposed. "
            "Ask the assistant to read it again and propose again."
        )
    # Building on an agent's push would carry it into a commit the event then vouches for.
    if not await is_known_commit(session, source, project, base_sha):
        known = await known_base(session, source, project, base_sha)
        detail = unknown_head_detail(base_sha, known)
        raise Conflict(
            detail if known is None else f"{detail}\n  {reset_command(known)}",
            code="unknown_head",
        )
    await _refuse_legacy_layout(source, project, base_sha)
    forge = forge_of(await ticket_source(session, project.id))
    proposal = await _propose_at(source, project, config, base_sha, forge=forge)
    listing = await source.list_files(repo, base_sha)
    blobs = {entry.path: entry.sha for entry in listing}
    selected = {
        f.path: f.body if f.body is not None else _catalog_body(proposal, f.path)
        for f in draft.files
    }
    try:
        bundle = assemble(
            config,
            draft.content,
            selected,
            proposal,
            edits=draft.edits,
            created=draft.created,
            repo_blobs=blobs,
        )
        # Edits and new files change the folder beside the lane, so the whole of it is checked.
        whole = bool(draft.edits or draft.created)
        base_files = await _read_files(source, repo, base_sha, listing) if whole else None
        loaded = validate_bundle(bundle, base_files=base_files)
    except EditConflict as exc:
        raise Conflict(exc.detail) from exc
    except (SelectionError, ConfigError) as exc:
        raise Invalid(exc.detail) from exc
    if surface != "dashboard" and bundle.stub_paths:
        # The stubs `assemble` adds for a newly named agent are writes under agents/ too.
        stub = min(bundle.stub_paths)
        raise Invalid(
            f"{stub} is dashboard-only. The files under {_AGENTS_PREFIX} hold the rules "
            "an agent works under, so a person commits them.",
            hint="Add the agent from the dashboard first, then commit the process here.",
        )
    try:
        if whole:
            await check_folder_runtime_bindings(
                session, project.organization_id, loaded, submitted=config.name
            )
        else:
            await check_runtime_binding(session, project.organization_id, config)
    except RuntimeBindingInvalid as exc:
        raise Invalid(str(exc)) from exc

    prepared = _Prepared(config, base_sha, proposal, bundle, blobs)
    if surface != "dashboard":
        await _refuse_approver_changes(source, repo, prepared)
    return prepared


def _catalog_body(proposal: Proposal, path: str) -> str:
    proposed = proposal.files.get(path)
    # An unproposed path is `assemble`'s to refuse, by name.
    return proposed.body if proposed is not None else ""


def _planned(prepared: _Prepared, draft: ConfigDraft) -> list[PlannedWrite]:
    edited = {edit.path for edit in draft.edits}
    process_path = process_bundle_path(prepared.config.name)

    def origin(path: str) -> WriteOrigin:
        if path == process_path:
            return "process"
        if path in edited:
            return "edit"
        return "stub" if path in prepared.bundle.stub_paths else "file"

    return [PlannedWrite(path, origin(path)) for path in sorted(prepared.bundle.files)]


def _offered(proposal: Proposal, surface: Surface) -> list[str]:
    """The catalog files a draft may add to `files`. Off the dashboard, no variant that writes
    under `agents/` or `skills/`: the commit would refuse it."""
    return sorted(
        {
            path
            for component in proposal.components
            if not component.present_in_repo
            for offer in component.variants
            if offer.offered
            if surface == "dashboard" or not any(p.startswith(_DASHBOARD_ONLY) for p in offer.paths)
            for path in offer.paths
        }
    )


async def check_config(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    draft: ConfigDraft,
    *,
    surface: Surface,
) -> ConfigCheck:
    """Exactly the checks `commit_config` runs, as a verdict. Bad config never raises; a GitHub
    failure does. A parse error reads nothing from GitHub."""
    try:
        config = _parse_draft(draft.content, project)
    except Invalid as exc:
        return ConfigCheck(valid=False, errors=[exc.message])
    warnings = lint_process(config)
    try:
        prepared = await _prepare(session, source, project, draft, surface=surface)
    except (Conflict, Invalid) as exc:
        return ConfigCheck(valid=False, warnings=warnings, blockers=[render(exc)])
    return ConfigCheck(
        valid=True,
        warnings=warnings,
        base_sha=prepared.base_sha,
        would_write=_planned(prepared, draft),
        offered=_offered(prepared.proposal, surface),
    )


async def _synced(result: ProcessSyncResult, source: RepoConfigSource, repo: str) -> SyncedConfig:
    # The snapshots are written either way. Agents pinned by convention may still be missing or
    # unparseable, and that has always surfaced at the first spawn instead of here. Reported once
    # for the whole call: several lanes usually share agents, and one gap named three times reads
    # as three gaps.
    warnings: dict[str, None] = {}
    for entry in result.results:
        config, _ = parse_snapshot(entry.row)
        if config is None:
            continue
        for gap in await agent_gaps(source, repo, result.sha, config):
            warnings[gap.line()] = None
    return SyncedConfig(result=result, agent_warnings=sorted(warnings))


async def sync_config(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    *,
    ref: str | None = None,
    sha: str | None = None,
) -> SyncedConfig:
    """`register_process_versions`, with its refusals as service errors. Nothing is written when
    it refuses."""
    try:
        result = await register_process_versions(session, source, project, ref=ref, sha=sha)
    except ProcessVersionExists as exc:
        raise Conflict(as_sentence(str(exc))) from exc
    except ContentsNotFound as exc:
        # A missing ref or a repo with no `.factorial/` is an operator problem with a fixable
        # cause, so it must not surface as the generic 500 handler's "internal error".
        raise Invalid(
            f"Factorial couldn't read the config from {project.github_repo}: "
            f"{as_sentence(str(exc))}"
        ) from exc
    except ConfigError as exc:
        raise Invalid(f"Factorial couldn't sync the config at {exc.origin}. {exc.detail}") from exc
    except ConfigCommitRefused as exc:
        # Refused before anything was written: the sync guard.
        raise Invalid(as_sentence(str(exc)), code="unknown_head") from exc
    except RuntimeBindingInvalid as exc:
        # Refused before anything was written: a `runtime:` the registry does not define, or
        # a process that names none.
        raise Invalid(
            as_sentence(str(exc)),
            code="runtime_missing" if exc.missing else None,
            missing_runtimes=exc.missing,
        ) from exc
    except GitLabError as exc:
        raise Invalid(
            f"Factorial couldn't read the config from {project.github_repo}: "
            f"{as_sentence(str(exc))}"
        ) from exc
    return await _synced(result, source, project.github_repo)


_SURFACES: Final[Mapping[RequestedVia, Surface]] = {
    RequestedVia.DASHBOARD: "dashboard",
    RequestedVia.MCP: "mcp",
    RequestedVia.CHAT: "chat",
}
_THROUGH: Final[Mapping[RequestedVia, str]] = {
    RequestedVia.MCP: "the Factorial MCP server",
    RequestedVia.CHAT: "the Factorial assistant",
}


def _commit_message(process: str, actor: Actor, message: str | None) -> str:
    if actor.via is RequestedVia.DASHBOARD:
        return f"Update the {process} process from the Factorial dashboard"
    through = _THROUGH[actor.via]
    if subject := " ".join((message or "").split()):
        return f"{subject}\n\nRequested by {actor.by} through {through}"
    return f"Update the {process} process through {through}\n\nRequested by {actor.by}"


async def commit_config(
    session: AsyncSession,
    source: RepoConfigSource,
    writer: ConfigCommitWriter,
    project: Project,
    draft: ConfigDraft,
    *,
    actor: Actor,
    message: str | None,
    expected_head: str | None = None,
) -> ConfigCommitResult:
    """Commit the draft and its agent gaps to `factorial` as the App, then sync that commit.

    Every picked file must be a member of the proposal recomputed here (tech-stack decision 33),
    and every edited one a member of the branch's own listing at the head read here. The event
    naming the commit is what lets the sync guard take it, so it is committed before the ref
    moves (decision 53). A draft the head already holds commits nothing and re-syncs the head,
    which is what makes a retried commit safe. `expected_head` is the head an approved
    proposal's diff was drawn against: a branch that has moved since refuses the commit.
    """
    prepared = await _prepare(
        session,
        source,
        project,
        draft,
        surface=_SURFACES[actor.via],
        expected_head=expected_head,
    )
    repo = project.github_repo
    config, bundle = prepared.config, prepared.bundle
    ticket_source = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
    ).scalar_one_or_none()
    if all(prepared.blobs.get(path) == git_blob_sha(text) for path, text in bundle.files.items()):
        synced = await sync_config(session, source, project, sha=prepared.base_sha)
        return ConfigCommitResult(
            sha=prepared.base_sha,
            url=_commit_url(repo, prepared.base_sha, project=project, source=ticket_source),
            committed=False,
            components=prepared.proposal.components,
            written=[],
            sync=synced,
            sync_error=None,
        )

    async def record(sha: str) -> None:
        details: dict[str, Any] = {
            "branch": CONFIG_BRANCH,
            "sha": sha,
            "process": config.name,
            **actor.fields(),
        }
        await append(session, type=EventType.CONFIG_COMMITTED, project_id=project.id, **details)
        await session.commit()

    try:
        sha = await writer(
            repo,
            CONFIG_BRANCH,
            prepared.base_sha,
            bundle.files,
            _commit_message(config.name, actor, message),
            record,
        )
    except ConfigBranchMoved as exc:
        raise Conflict(f"The {CONFIG_BRANCH} branch moved during the commit. Try again.") from exc
    except RequestFailed as exc:
        raise Conflict(
            f"GitHub refused the config commit: {as_sentence(refusal_reason(exc))}"
        ) from exc
    except GitLabError as exc:
        raise Conflict(f"GitLab refused the config commit: {as_sentence(str(exc))}") from exc

    sync: SyncedConfig | None = None
    sync_error: str | None = None
    try:
        # A savepoint, so a failed sync leaves nothing half-written beside the committed event.
        async with session.begin_nested():
            result = await register_process_versions(session, source, project, sha=sha)
        sync = await _synced(result, source, repo)
    except Exception as exc:  # the commit landed; the operator can press Sync once it is fixed
        log.warning("config commit %s on %s did not sync: %s", sha[:7], repo, exc)
        sync_error = exc.detail if isinstance(exc, ConfigError) else str(exc) or repr(exc)
    return ConfigCommitResult(
        sha=sha,
        url=_commit_url(repo, sha, project=project, source=ticket_source),
        committed=True,
        components=prepared.proposal.components,
        written=sorted(bundle.files),
        sync=sync,
        sync_error=sync_error,
    )


async def _lane_config(
    source: RepoConfigSource, project: Project, row: ProcessVersion
) -> LaneConfig:
    repo, sha = project.github_repo, row.source_commit_sha
    try:
        path = await source.process_path_at(repo, row.process_key, sha)
        text = await source.read_process_text(repo, path, sha)
    except (ContentsNotFound, ConfigError, ValueError, RequestFailed) as exc:
        raise UpstreamFailed(
            "Factorial couldn't read the pinned process file from the repository. Try again."
        ) from exc
    config, _ = parse_snapshot(row)
    inventory = await config_inventory(source, repo, pinned_sha=sha, config=config)
    agents = await resolve_agents(source, repo, sha, config) if config is not None else []
    return LaneConfig(row=row, yaml=text, inventory=inventory, agents=agents)


async def _head_file(source: RepoConfigSource, project: Project, path: str) -> FileText:
    """At the head, not at a pin: an edit's `base_sha` must be the blob a commit checks."""
    repo = project.github_repo
    head = await _branch_head(source, project)
    listed = {entry.path: entry.sha for entry in await source.list_files(repo, head)}
    # Membership, not a prefix test: a path the caller supplies never decides what is read.
    blob = listed.get(path)
    if blob is None:
        raise NotFound(
            f"{path} is not in the config folder at {head[:7]}.", candidates=sorted(listed)[:20]
        )
    try:
        content = await source.read_file(repo, path, head)
    except ContentsNotFound as exc:
        raise UpstreamFailed(
            f"Factorial couldn't read {path} from the repository. Try again."
        ) from exc
    except ValueError as exc:
        raise Invalid(f"{path} cannot be read as text: {as_sentence(str(exc))}") from exc
    return FileText(path=path, content=content, blob_sha=blob, head_sha=head)


async def read_config(
    session: AsyncSession,
    source: RepoConfigSource,
    project: Project,
    *,
    process: str | None,
    path: str | None,
) -> ConfigRead:
    """The available lanes; with `process`, that lane at its pinned commit; with `path`, one
    config file at the `factorial` head."""
    rows = await latest_process_versions(session, project.id)
    default, _ = default_or_note(project)
    lanes = [
        LaneRef(key, row.id, row.source_commit_sha, key == default) for key, row in rows.items()
    ]
    lane: LaneConfig | None = None
    if process is not None:
        row = rows.get(process)
        if row is None:
            raise NotFound(f"{project.name} has no process {process!r}.", candidates=sorted(rows))
        lane = await _lane_config(source, project, row)
    file = await _head_file(source, project, path) if path is not None else None
    return ConfigRead(lanes=lanes, lane=lane, file=file)
