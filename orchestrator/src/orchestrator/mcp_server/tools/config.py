"""Config tools: read what a lane runs, check a draft, and commit it to the `factorial` branch.

`factorial_config_commit` presses the dashboard's Commit and sync (design.md §7.17). On this
surface the service refuses any file it would write under `agents/` or `skills/` and any change
to a gate's approvers.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, Field

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.config import (
    ConfigDraft,
    NewFile,
    SyncedConfig,
    check_config,
    commit_config,
    read_config,
)
from orchestrator.api.services.errors import Unavailable
from orchestrator.api.services.refs import resolve_project, ticket_source
from orchestrator.config.loader import MAX_CONFIG_CHARS
from orchestrator.config.scaffold.assemble import FileEdit
from orchestrator.db.models import Project, TicketSourceKind
from orchestrator.factorial import RepoConfigSource
from orchestrator.mcp_server.auth import current_actor
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import Untrusted, respond, untrusted
from orchestrator.mcp_server.tokens import READ_SCOPE, WRITE_SCOPE
from orchestrator.mcp_server.tools.params import ProjectArg

__all__ = [
    "TOOLS",
    "Content",
    "Edits",
    "Files",
    "WouldWriteOut",
    "config_draft",
    "forge_commit_writer",
    "forge_config_source",
    "run_config_commit",
]

#: A config file or a lane's YAML is returned whole up to this; past it, cut and marked.
FILE_LIMIT = 40_000

Content = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_CONFIG_CHARS,
        description="The whole process YAML, committed byte for byte.",
    ),
]


class NewFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    body: str | None = Field(
        default=None, max_length=MAX_CONFIG_CHARS, description="Omit to take the catalog's body."
    )


class FileEditIn(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    body: str = Field(max_length=MAX_CONFIG_CHARS)
    base_sha: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="The blob_sha factorial_config_read returned for this path.",
    )


Files = Annotated[
    list[NewFileIn] | None,
    Field(
        description="Catalog files to add, from `offered`. Never a path under agents/ or skills/."
    ),
]
Edits = Annotated[
    list[FileEditIn] | None,
    Field(
        description=(
            "Rewrites of files the factorial branch has. Never a path under agents/ or skills/."
        )
    ),
]


class ConfigLaneOut(BaseModel):
    process: str
    version_id: int
    sha: str
    is_default: bool


class ListedFileOut(BaseModel):
    path: str
    kind: str
    drift: str
    warning: Untrusted | None = None


class AgentOut(BaseModel):
    name: str
    model: Untrusted | None = None
    warning: Untrusted | None = None


class FileTextOut(BaseModel):
    path: str
    content: Untrusted
    blob_sha: str | None = Field(
        description="The base_sha an edit of this file needs. None when `truncated`: an edit "
        "must be built from the whole file, so it cannot be edited from this read."
    )
    truncated: bool


class ConfigReadOut(BaseModel):
    lanes: list[ConfigLaneOut]
    process: str | None = None
    version_id: int | None = None
    pinned_sha: str | None = None
    head_sha: str | None = None
    yaml: Untrusted | None = None
    yaml_truncated: bool = Field(
        default=False,
        description="The lane's YAML is over the size limit, so `yaml` is withheld: a commit "
        "built from part of it would drop the rest. Such a file is edited from the dashboard.",
    )
    files: list[ListedFileOut] = Field(default_factory=list)
    agents: list[AgentOut] = Field(default_factory=list)
    file: FileTextOut | None = None


class LintOut(BaseModel):
    code: str
    loc: str
    msg: Untrusted


class WouldWriteOut(BaseModel):
    path: str
    origin: Literal["process", "file", "edit", "stub"]


class ConfigCheckOut(BaseModel):
    valid: bool
    errors: list[Untrusted]
    warnings: list[LintOut]
    blockers: list[Untrusted]
    base_sha: str | None = None
    would_write: list[WouldWriteOut]
    offered: list[str]


class LaneSyncOut(BaseModel):
    process: str
    outcome: Literal["created", "unchanged", "retired"]


class DefaultMovedOut(BaseModel):
    previous: str
    now: str


class SyncOut(BaseModel):
    sha: str
    lanes: list[LaneSyncOut]
    agent_warnings: list[Untrusted]
    default_moved: DefaultMovedOut | None = None


class ConfigCommitOut(BaseModel):
    sha: str
    url: str
    written: list[str]
    committed: bool
    sync: SyncOut | None = None
    sync_error: Untrusted | None = None


def _cut(text: str) -> tuple[str, bool]:
    return (text, False) if len(text) <= FILE_LIMIT else (text[:FILE_LIMIT], True)


def _lane_yaml(text: str) -> tuple[str | None, bool]:
    # A process change commits the whole YAML, so a cut one would commit the cut file.
    return (text, False) if len(text) <= FILE_LIMIT else (None, True)


def _file_out(path: str, text: str, blob_sha: str) -> FileTextOut:
    content, truncated = _cut(text)
    # An edit built on a cut read would commit the cut file under a base_sha that vouches for it.
    return FileTextOut(
        path=path, content=content, blob_sha=None if truncated else blob_sha, truncated=truncated
    )


def config_draft(
    content: str, files: list[NewFileIn] | None, edits: list[FileEditIn] | None
) -> ConfigDraft:
    return ConfigDraft(
        content=content,
        files=[NewFile(f.path, f.body) for f in files or ()],
        edits=[FileEdit(e.path, e.body, e.base_sha) for e in edits or ()],
    )


def _sync_out(synced: SyncedConfig) -> SyncOut:
    result = synced.result
    moved = result.default_moved
    return SyncOut(
        sha=result.sha,
        lanes=[LaneSyncOut(process=e.process_key, outcome=e.outcome) for e in result.results],
        agent_warnings=[untrusted(line, 300) for line in synced.agent_warnings],
        default_moved=(
            None if moved is None else DefaultMovedOut(previous=moved.previous, now=moved.now)
        ),
    )


@tool(READ_SCOPE, title="Read config", output=ConfigReadOut)
async def factorial_config_read(
    project: ProjectArg,
    process: Annotated[
        str | None, Field(description="A lane key from `lanes`: adds that lane's detail.")
    ] = None,
    path: Annotated[
        str | None, Field(description="A file under .factorial/: its text at the branch head.")
    ] = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """A project's lanes. With `process`: that lane's YAML at its pinned commit, its config
    files with drift against the factorial branch head, and its agents. With `path`: one file's
    text and blob_sha at the head, the base_sha an edit of it needs. A file over the size limit
    comes back truncated with no blob_sha, and a lane YAML over it is withheld: neither can be
    edited here."""
    row = await resolve_project(tc.session, tc.organization_id, project)
    source = await forge_config_source(tc, row)
    read = await read_config(tc.session, source, row, process=process, path=path)
    out = ConfigReadOut(
        lanes=[
            ConfigLaneOut(
                process=lane.process,
                version_id=lane.version_id,
                sha=lane.sha,
                is_default=lane.is_default,
            )
            for lane in read.lanes
        ]
    )
    summary = f"{len(out.lanes)} lanes"
    if read.lane is not None:
        inventory = read.lane.inventory
        out.yaml, out.yaml_truncated = _lane_yaml(read.lane.yaml)
        out.process = read.lane.row.process_key
        out.version_id = read.lane.row.id
        out.pinned_sha = inventory.pinned_sha
        out.head_sha = inventory.head_sha
        out.files = [
            ListedFileOut(
                path=f.path,
                kind=f.kind,
                drift=f.drift,
                warning=None if f.warning is None else untrusted(f.warning, 300),
            )
            for f in inventory.files
        ]
        out.agents = [
            AgentOut(
                name=a.name,
                model=None if a.manifest is None else untrusted(a.manifest.model, 100),
                warning=None if a.reason is None else untrusted(a.reason, 300),
            )
            for a in read.lane.agents
        ]
        summary += f"; lane {out.process}: {len(out.files)} files, {len(out.agents)} agents"
        if out.yaml_truncated:
            summary += "; YAML over the size limit, withheld"
    if read.file is not None:
        out.file = _file_out(read.file.path, read.file.content, read.file.blob_sha)
        out.head_sha = read.file.head_sha
        if out.file.truncated:
            summary += f"; file truncated, not editable here, at head {read.file.head_sha[:7]}"
        else:
            summary += f"; file blob {read.file.blob_sha[:7]} at head {read.file.head_sha[:7]}"
    return respond(out, summary)


@tool(READ_SCOPE, title="Validate config", output=ConfigCheckOut)
async def factorial_config_validate(
    project: ProjectArg,
    content: Content,
    files: Files = None,
    edits: Edits = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Run exactly the checks factorial_config_commit runs, and write nothing. `valid` false
    with `errors` is a YAML that does not parse; with `blockers`, a refusal the commit would
    answer. `would_write` is what a commit writes; `offered` the catalog files `files` may add."""
    row = await resolve_project(tc.session, tc.organization_id, project)
    source = await forge_config_source(tc, row)
    check = await check_config(
        tc.session, source, row, config_draft(content, files, edits), surface="mcp"
    )
    out = ConfigCheckOut(
        valid=check.valid,
        errors=[untrusted(e, 600) for e in check.errors],
        warnings=[
            LintOut(code=w.code, loc=w.loc, msg=untrusted(w.msg, 300)) for w in check.warnings
        ],
        blockers=[untrusted(b, 600) for b in check.blockers],
        base_sha=check.base_sha,
        would_write=[WouldWriteOut(path=w.path, origin=w.origin) for w in check.would_write],
        offered=check.offered,
    )
    verdict = "valid" if check.valid else "invalid"
    return respond(
        out,
        f"{verdict}: {len(out.errors)} errors, {len(out.blockers)} blockers, "
        f"{len(out.warnings)} warnings, {len(out.would_write)} files to write",
    )


@tool(
    WRITE_SCOPE,
    title="Commit config",
    output=ConfigCommitOut,
    destructive=True,
    idempotent=False,
)
async def factorial_config_commit(
    project: ProjectArg,
    content: Content,
    files: Files = None,
    edits: Edits = None,
    message: Annotated[
        str | None, Field(max_length=200, description="The commit's subject line.")
    ] = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Validate, commit to the factorial branch as the App, then sync. A draft the head already
    holds commits nothing and re-syncs, so a retry is safe. Any file under agents/ (a new
    agent's stub included) or skills/, and any change to a gate's approvers, is dashboard-only.
    Tickets in flight keep their pinned config."""
    return await run_config_commit(
        tc,
        current_actor(write=True),
        project=project,
        content=content,
        files=files,
        edits=edits,
        message=message,
    )


async def run_config_commit(
    tc: ToolContext,
    actor: Actor,
    *,
    project: str,
    content: str,
    files: list[NewFileIn] | None,
    edits: list[FileEditIn] | None,
    message: str | None,
    expected_head: str | None = None,
) -> ToolResult:
    row = await resolve_project(tc.session, tc.organization_id, project)
    result = await commit_config(
        tc.session,
        await forge_config_source(tc, row),
        await forge_commit_writer(tc, row),
        row,
        config_draft(content, files, edits),
        actor=actor,
        message=message,
        expected_head=expected_head,
    )
    out = ConfigCommitOut(
        sha=result.sha,
        url=result.url,
        written=result.written,
        committed=result.committed,
        sync=None if result.sync is None else _sync_out(result.sync),
        sync_error=None if result.sync_error is None else untrusted(result.sync_error, 600),
    )
    what = f"committed {result.sha[:7]}" if result.committed else f"unchanged at {result.sha[:7]}"
    if out.sync is not None:
        created = sum(lane.outcome == "created" for lane in out.sync.lanes)
        tail = f"synced, {created} lanes created"
    else:
        tail = "sync failed; see sync_error"
    return respond(out, f"{what}, {len(out.written)} files written; {tail}")


TOOLS: list[ToolSpec] = [factorial_config_read, factorial_config_validate, factorial_config_commit]


async def forge_config_source(tc: ToolContext, project: Project) -> RepoConfigSource:
    """The forge this project reads `.factorial/` from."""
    from orchestrator.gitlab.project_client import config_source_for

    github = await tc.deps.config_source(project)
    chosen = await config_source_for(tc.session, project, github=github, gitlab=tc.deps.gitlab)
    if chosen is None:
        source = await ticket_source(tc.session, project.id)
        if source is not None and source.kind == TicketSourceKind.GITLAB:
            raise Unavailable(tc.deps.gitlab_missing)
        raise Unavailable(tc.deps.github_missing)
    return chosen


async def forge_commit_writer(tc: ToolContext, project: Project):
    """The forge this project commits `.factorial/` to."""
    from functools import partial

    from orchestrator.gitlab.files_commit import commit_config_files

    source = await ticket_source(tc.session, project.id)
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        return partial(commit_config_files, await tc.gitlab(project))
    return await tc.commit_writer(project)
