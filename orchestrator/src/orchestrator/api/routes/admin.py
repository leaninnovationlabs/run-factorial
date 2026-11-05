"""Admin surface: projects and their readiness, ticket source, secrets, labels, process versions
and pending issues, plus the config-PR builder. The derivations behind readiness live in
`api/readiness.py`.

Nothing here touches the state machine — admitting a ticket, promoting a step, spawning an agent
and merging a PR are the worker's (CLAUDE.md, division of labor). `PUT /projects/{id}/active` only
flips a column the worker reads when choosing what to admit from; tickets in flight keep running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import Permission
from orchestrator.adapters.registry import (
    display_of,
    forge_of,
    host_registries,
    resolve_bindings,
)
from orchestrator.api.access import SIGNED_IN, GrantDep, needs, require_dashboard_origin
from orchestrator.api.auth import PersonDep, PrincipalDep
from orchestrator.api.deps import (
    AccessReaderDep,
    ConfigBranchReaderDep,
    ConfigBranchSeederDep,
    ConfigCommitWriterDep,
    ConfigSourceDep,
    ConfigSourceOptionalDep,
    ConnectionClientDep,
    GitHubForDep,
    LabelReaderDep,
    LabelWriterDep,
    OrganizationDep,
    ProtectionReaderDep,
    SessionDep,
    SettingsDep,
    WorkflowsReaderDep,
)
from orchestrator.api.guards import NoEchoRoute, refuse_bad_name
from orchestrator.api.readiness import (
    access_item,
    all_label_palettes,
    behind_access,
    blockers,
    ci_workflows_item,
    config_branch_items,
    default_or_note,
    gate_approvers_item,
    github_standing,
    gitlab_access_item,
    host_of,
    label_item,
    named_runtimes,
    parse_snapshot,
    parse_snapshots,
    process_summaries,
    protection_item,
    secret_gap,
    shared_protection,
)
from orchestrator.api.schemas import (
    AgentDetailOut,
    ComparedCommitOut,
    ComparedFileOut,
    ComponentOut,
    ConfigAdoptionIn,
    ConfigAdoptionOut,
    ConfigBranchIn,
    ConfigBranchOut,
    ConfigCommitIn,
    ConfigCommitOut,
    ConfigFileContentOut,
    ConfigFileOut,
    ConfigFilesOut,
    ConfigPreviewIn,
    ConfigPreviewOut,
    DefaultBranchConfigOut,
    DefaultProcessIn,
    DefaultProcessMovedOut,
    FileProposalOut,
    ForgeDisplayOut,
    GitHubConnectionIn,
    GitHubConnectionOut,
    LabelsCreated,
    LabelsListOut,
    LabelSpecOut,
    LanePreviewOut,
    PendingIssueOut,
    PermitsChange,
    ProcessSummary,
    ProcessSyncOut,
    ProcessSyncResultOut,
    ProcessTemplateOut,
    ProcessTemplatesOut,
    ProcessValidateIn,
    ProcessValidateOut,
    ProcessVersionDetailOut,
    ProcessVersionOut,
    ProcessVersionRawOut,
    ProcessVersionSyncIn,
    ProjectActiveIn,
    ProjectDetail,
    ProjectIn,
    ProjectListItem,
    ProjectOut,
    ProjectPatch,
    ProjectReadiness,
    ProjectRuntimeRef,
    ReadinessItem,
    SecretDeliveryIn,
    SecretDeliveryOut,
    SecretList,
    SecretName,
    SecretValueIn,
    TemplateLaneOut,
    TicketSourceIn,
    TicketSourceOut,
    UnknownHeadOut,
    VariantOfferOut,
    VocabularyOut,
    checked_repo,
)
from orchestrator.api.services import adoption
from orchestrator.api.services import config as config_service
from orchestrator.api.services import github_connection as connection_service
from orchestrator.api.services import projects as project_service
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.config import ConfigDraft, NewFile, SyncedConfig
from orchestrator.api.services.errors import Conflict, as_sentence
from orchestrator.api.services.refs import ticket_source
from orchestrator.api.services.vocabulary import config_vocabulary
from orchestrator.config import ConfigError, ProcessConfig, parse_named_process, parse_process
from orchestrator.config.lint import lint_process
from orchestrator.config.loader import dump_config_yaml
from orchestrator.config.process_templates import PROCESS_TEMPLATES
from orchestrator.config.scaffold.assemble import FileEdit
from orchestrator.config.scaffold.propose import ComponentProposal, FileProposal
from orchestrator.db.models import (
    ProcessVersion,
    Project,
    TicketSource,
)
from orchestrator.events import EventType, append
from orchestrator.factorial import CONFIG_BRANCH, DEFAULT_KEY
from orchestrator.factorial.inventory import config_inventory
from orchestrator.factorial.probe import AgentResolution, catalog_agents, resolve_agents
from orchestrator.factorial.registry import adopters, latest_process_versions
from orchestrator.factorial.runtime_policy import (
    EGRESS_KEY,
    RUNTIME_KEY,
    SettingsInvalid,
    egress_allowlist,
    egress_covers,
    egress_hosts,
    validate_settings,
)
from orchestrator.github import (
    ConfigBranchExists,
    ContentsNotFound,
    RepoNotVisible,
)
from orchestrator.github.connection import CONNECTION_SECRETS, github_bag
from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.hosts import HostRefused, check_base_url
from orchestrator.secrets import store
from orchestrator.settings import APP_PREFIX, Settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])
#: Routes whose body carries a credential: a 422 here never echoes the input.
secret_router = APIRouter(tags=["admin"], route_class=NoEchoRoute)
#: Lists and creates within one organization, mounted at `/orgs/{org}` (`api.main`).
org_router = APIRouter(tags=["admin"])


async def _require_project(
    session: SessionDep, project_id: int, *, for_update: bool = False, key_share: bool = False
) -> Project:
    """`key_share` locks `FOR NO KEY UPDATE`: still exclusive among the project's editors, but
    an INSERT whose foreign key names the project, such as a webhook's event, does not wait."""
    query = select(Project).where(Project.id == project_id)
    if for_update or key_share:
        query = query.with_for_update(key_share=key_share)
    project = (await session.execute(query)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


async def _require_version(session: SessionDep, project_id: int, version_id: int) -> ProcessVersion:
    """One snapshot row by its own id, scoped to the project.

    Keyed on the row id rather than on `version`: two lanes may both be at version 1, so the
    number alone stopped identifying a row (design.md §4). `(process_key, version)` would work
    too, and would push a compound key into every client cache entry.
    """
    row = (
        await session.execute(
            select(ProcessVersion).where(
                ProcessVersion.project_id == project_id, ProcessVersion.id == version_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} has no process version {version_id}.",
        )
    return row


async def _latest_process_versions(session: SessionDep, project_id: int) -> list[ProcessVersion]:
    """`factorial.registry.latest_process_versions`, as the list every route here reads."""
    return list((await latest_process_versions(session, project_id)).values())


async def _require_configs(
    session: SessionDep, project_id: int
) -> tuple[Project, dict[str, ProcessConfig]]:
    project = await _require_project(session, project_id)
    configs, notes = parse_snapshots(await _latest_process_versions(session, project_id))
    if not configs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=" ".join(notes) or "No process version yet. Sync .factorial/processes/ first.",
        )
    return project, configs


# ------------------------------------------------------------------------------ projects


async def _gitlab_host_resolves(settings: dict[str, Any]) -> None:
    """The GitLab host check with its DNS half, which the body's synchronous validator cannot
    run. The validator already refused a malformed `settings.gitlab`."""
    bag = settings.get("gitlab")
    base = bag.get("base_url") if isinstance(bag, dict) else None
    if not isinstance(base, str) or not base.strip():
        return
    try:
        await check_base_url(base)
    except HostRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@org_router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
@needs(Permission.PROJECTS_MANAGE)
async def create_project(
    payload: ProjectIn, session: SessionDep, organization: OrganizationDep
) -> ProjectOut:
    """Create a project. It starts inactive: the ticket source and process version have to land
    before the worker should read anything."""
    settings = {k: v for k, v in payload.settings.items() if k != "github"}
    await _gitlab_host_resolves(settings)
    project = Project(
        organization_id=organization.id,
        name=payload.name,
        description=payload.description,
        github_repo=payload.github_repo,
        settings=settings,
    )
    try:
        # A savepoint, not a plain flush: an IntegrityError deactivates its transaction, and
        # unwinding the whole unit of work to report a 409 would discard the rest of the request.
        async with session.begin_nested():
            session.add(project)
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A project named {payload.name!r} already exists in {organization.slug!r}.",
        ) from exc
    # created_at is a server default; refresh rather than risk a lazy load from async code.
    await session.refresh(project)
    return ProjectOut.model_validate(project)


@org_router.get("/projects", response_model=list[ProjectListItem])
@needs(Permission.ORG_READ)
async def list_projects(
    session: SessionDep, organization: OrganizationDep, grant: GrantDep
) -> list[ProjectListItem]:
    """The organization's projects, one runtime per row (`services.projects.list_projects`)."""
    sources = {
        source.project_id: source
        for source in (await session.execute(select(TicketSource))).scalars()
    }
    items = [
        ProjectListItem(
            **ProjectOut.model_validate(row.project).model_dump(exclude={"forge"}),
            forge=ForgeDisplayOut(
                **display_of(
                    row.project,
                    sources[row.project.id].kind if row.project.id in sources else None,
                ).payload()
            ),
            runtime=(
                None
                if row.runtime is None
                else ProjectRuntimeRef(slug=row.runtime.slug, name=row.runtime.runtime_name)
            ),
            ticket_source=(
                TicketSourceOut.model_validate(sources[row.project.id])
                if row.project.id in sources
                else None
            ),
        )
        for row in await project_service.list_projects(session, organization.id)
    ]
    if Permission.PROJECTS_READ in grant.permissions:
        return items
    return [
        item.model_copy(
            update={
                "settings": _forge_settings(item.settings),
                "ticket_source": _source_without_ref(item.ticket_source),
            }
        )
        for item in items
    ]


@router.get("/projects/{project_id}", response_model=ProjectDetail)
@needs(Permission.ORG_READ)
async def get_project(
    project_id: int,
    session: SessionDep,
    config_source: ConfigSourceOptionalDep,
    grant: GrantDep,
    settings: SettingsDep,
) -> ProjectDetail:
    """One project with everything the page can draw without asking GitHub for live state.

    This is the request the page holds its first paint for, so the repo-side probes answer
    on `GET /projects/{id}/readiness` instead. Nothing is lost by moving them: none of them can
    reach `blocked` — a live call must never be able to refuse an activation — so none of them
    decides anything the header, the tabs or the ticket table draw.
    """
    project = await _require_project(session, project_id)
    detail = await project_service.project_detail(
        session,
        project,
        config_source=config_source,
        webhook_reachable=settings.webhook_reachable,
    )
    if Permission.PROJECTS_READ in grant.permissions:
        return detail
    return _without_configuration(detail)


def _forge_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Of a project's settings, only the GitLab host the forge links need: the rest is
    configuration, for a caller who holds `projects:read`."""
    bag = settings.get("gitlab")
    base_url = bag.get("base_url") if isinstance(bag, dict) else None
    return {"gitlab": {"base_url": base_url}} if isinstance(base_url, str) else {}


def _source_without_ref(source: TicketSourceOut | None) -> TicketSourceOut | None:
    return None if source is None else source.model_copy(update={"webhook_secret_ref": None})


def _without_configuration(detail: ProjectDetail) -> ProjectDetail:
    """What a caller without `projects:read` reads: no secret names and no readiness rows.
    `blockers` stays, so the dashboard can say why nothing is admitted."""
    return detail.model_copy(
        update={
            "settings": _forge_settings(detail.settings),
            "ticket_source": _source_without_ref(detail.ticket_source),
            "secret_names": [],
            "declared_secrets": [],
            "runtime_provided_secrets": [],
            "secret_deliveries": [],
            "warnings": [],
            "probe_keys": [],
            "readiness": [],
        }
    )


@router.get("/projects/{project_id}/readiness", response_model=ProjectReadiness)
@needs(Permission.PROJECTS_READ)
async def get_project_readiness(
    project_id: int,
    session: SessionDep,
    read_labels: LabelReaderDep,
    read_protection: ProtectionReaderDep,
    read_access: AccessReaderDep,
    read_workflows: WorkflowsReaderDep,
    read_config_branch: ConfigBranchReaderDep,
    config_source: ConfigSourceOptionalDep,
) -> ProjectReadiness:
    """The readiness rows whose verdict is a live GitHub read, off the page's critical path.

    Gathered rather than awaited in turn: serial round trips were most of what the project
    page used to wait for, and no probe reads another's answer. Nothing needs shielding — each
    already degrades to `unknown` on its own exception, which is the same contract that keeps a
    GitHub outage out of `blockers`.

    `app_access` leads for the reason it always did: it is the precondition the others fail
    anonymously without, so it turns several shrugs into one cause. When it says the repo is out
    of reach, the rest report themselves unread rather than each guessing (`behind_access`).
    """
    project = await _require_project(session, project_id)
    versions = await _latest_process_versions(session, project_id)
    configs, _ = parse_snapshots(versions)
    read_protection = shared_protection(read_protection)
    source = await ticket_source(session, project_id)
    host = host_of(project, source)
    gitlab_client = None
    if not host.github_app:
        from orchestrator.gitlab.project_client import client_for_project

        gitlab_client = await client_for_project(session, project)
    # The probes below can wait on the forge for its full HTTP timeout. End the read-only
    # transaction first so a few slow readiness requests cannot consume the pool and hold the
    # project's database-only response behind them. `config_branch_items` reacquires a connection
    # only after its forge read, when it compares the observed head with recorded commits.
    await session.commit()
    # `protection_item`, `ci_workflows_item` and `gate_approvers_item` each return None when their
    # row is irrelevant, and the row is dropped rather than muted — see `protection_item`.
    access_row, branch_rows, label_row, protection_row, ci_row, gate_row = await asyncio.gather(
        gitlab_access_item(gitlab_client, project)
        if not host.github_app
        else access_item(read_access),
        config_branch_items(read_config_branch, session, config_source, project, host=host),
        label_item(read_labels, project, configs, host=host),
        protection_item(read_protection, project, configs, host=host),
        ci_workflows_item(read_workflows, project, configs, host=host),
        gate_approvers_item(read_protection, project, configs, host=host),
    )
    rows: list[ReadinessItem] = [
        access_row,
        *behind_access(
            access_row,
            [
                *branch_rows,
                label_row,
                *(row for row in (protection_row, ci_row, gate_row) if row is not None),
            ],
            project,
        ),
    ]
    return ProjectReadiness(readiness=rows, warnings=[r.detail for r in rows if r.detail])


@router.post("/projects/{project_id}/labels", response_model=LabelsCreated)
@needs(Permission.PROJECTS_MANAGE)
async def create_project_labels(
    project_id: int,
    session: SessionDep,
    read_labels: LabelReaderDep,
    write_label: LabelWriterDep,
) -> LabelsCreated:
    """Create the `factorial:*` labels the pinned process expects and the repo does not define.

    Additive and idempotent: the expected set is diffed against what the repo already has, so a
    label someone recoloured on purpose is never touched and running this twice is a no-op. The
    names come from `all_label_palettes` — the same set the readiness row diffs — so the button
    cannot create a label the checklist does not ask for.

    This is the only repo-side write the configuration page offers. Branch protection has no
    equivalent by design: it needs `administration: write`, which would let the App remove the
    control that stops a bad merge reaching the default branch (`protection_item`).
    """
    project, configs = await _require_configs(session, project_id)
    if read_labels is None or write_label is None:
        host = host_of(project, await ticket_source(session, project_id))
        detail = f"{host.unconfigured} {host.labels_hand}"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    palette = all_label_palettes(configs)
    # Read first, then write only the gap. Failing the read must not degrade into creating
    # everything — that would recolour labels the repo owner set deliberately.
    existing = await read_labels(project.github_repo)
    created: list[str] = []
    for name in sorted(set(palette) - existing):
        colour, description = palette[name]
        await write_label(project.github_repo, name, colour, description)
        created.append(name)
    return LabelsCreated(created=created)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
@needs(Permission.PROJECTS_MANAGE)
async def update_project(project_id: int, payload: ProjectPatch, session: SessionDep) -> ProjectOut:
    """Partial update. Editing `github_repo` moves nothing retroactively: admitted tickets keep
    the `ticket_source` they came in through, and process versions keep their read-at sha.

    `github_repo` takes the shape of the forge the ticket source is on, the one every repo-side
    read goes to: a nested path only on GitLab."""
    project = await _require_project(session, project_id, for_update=True)
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    if payload.github_repo is not None:
        host = host_of(project, await ticket_source(session, project_id))
        try:
            fields["github_repo"] = checked_repo(payload.github_repo, gitlab=not host.github_app)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, as_sentence(str(exc))
            ) from exc
    if "settings" in fields:
        # The connection has its own route; a whole-object write cannot add, change or drop it.
        kept = {k: v for k, v in fields["settings"].items() if k != "github"}
        if github_bag(project):
            kept["github"] = project.settings["github"]
        await _gitlab_host_resolves(kept)
        fields["settings"] = kept
    for key, value in fields.items():
        setattr(project, key, value)
    try:
        async with session.begin_nested():  # savepoint — see create_project
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A project named {fields.get('name')!r} already exists.",
        ) from exc
    await session.refresh(project)
    return ProjectOut.model_validate(project)


@router.put("/projects/{project_id}/active", response_model=ProjectOut)
@needs(Permission.PROJECTS_MANAGE)
async def set_project_active(
    project_id: int,
    payload: ProjectActiveIn,
    session: SessionDep,
    config_source: ConfigSourceOptionalDep,
    settings: SettingsDep,
) -> ProjectOut:
    """Turn admission on or off for a project.

    Activating a project that is not fully configured is a 409 listing what is missing, not a
    silent no-op: `active = true` with no ticket source looks switched on and admits nothing. The
    check is `blockers` on the same inputs the project page reads; the probe's degraded outcomes
    ("not checked" notes) are warnings there and simply not blockers here.

    Deactivating is always allowed and only stops admission. Tickets already in flight keep
    reconciling — dropping one mid-process would strand its branch and its PR.
    """
    project = await _require_project(session, project_id)
    if payload.active and not project.active:
        source = await ticket_source(session, project_id)
        versions = await _latest_process_versions(session, project_id)
        configs, _ = parse_snapshots(versions)
        runtimes = await named_runtimes(session, project.organization_id, configs)
        gap = await secret_gap(session, config_source, project, configs, runtimes)
        # No runtime row: an unusable Runtime blocks its own lane and no other, so it must not
        # refuse the whole project's activation (`runtime_items`).
        blocking = blockers(
            source,
            versions,
            gap.missing,
            default_or_note(project)[0],
            await github_standing(
                session, project, source, webhook_reachable=settings.webhook_reachable
            ),
        )
        if blocking:
            raise Conflict(
                "This project is not ready to activate. " + " ".join(blocking), code="not_ready"
            )
    project.active = payload.active
    await session.flush()
    await session.refresh(project)
    return ProjectOut.model_validate(project)


# ------------------------------------------------------------------------- ticket source


@router.get("/projects/{project_id}/ticket-source", response_model=TicketSourceOut)
@needs(Permission.PROJECTS_READ)
async def get_ticket_source(project_id: int, session: SessionDep) -> TicketSourceOut:
    await _require_project(session, project_id)
    source = await ticket_source(session, project_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The project has no ticket source."
        )
    return TicketSourceOut.model_validate(source)


@router.put("/projects/{project_id}/ticket-source", response_model=TicketSourceOut)
@needs(Permission.PROJECTS_MANAGE)
async def put_ticket_source(
    project_id: int, payload: TicketSourceIn, session: SessionDep
) -> TicketSourceOut:
    """Set (or replace) what the project reacts to. One source per project — the unique
    constraint on `ticket_source.project_id` — so this is an upsert, not a create.

    `last_polled_at` is the worker's column and is left alone; resetting it here would make the
    API look like it had observed something.
    """
    # Locked like any project edit, so a GitHub connection save sees a moved intake repo. Not
    # FOR UPDATE: a verified webhook holds this source's row, then inserts an event naming the
    # project, and the two would deadlock.
    await _require_project(session, project_id, key_share=True)
    source = await ticket_source(session, project_id)
    if source is None:
        source = TicketSource(project_id=project_id, kind=payload.kind)
        session.add(source)
    else:
        source.kind = payload.kind
    source.external_ref = payload.external_ref
    source.webhook_secret_ref = payload.webhook_secret_ref
    await session.flush()
    await session.refresh(source)
    return TicketSourceOut.model_validate(source)


# ----------------------------------------------------------------------- github connection


def _dashboard_base(settings: Settings) -> str:
    return settings.dashboard_url or APP_PREFIX


@secret_router.get("/projects/{project_id}/github-connection", response_model=GitHubConnectionOut)
@needs(Permission.PROJECTS_READ)
async def get_github_connection(
    project_id: int, session: SessionDep, settings: SettingsDep
) -> GitHubConnectionOut:
    project = await _require_project(session, project_id)
    return await connection_service.connection_out(
        session, project, base_url=_dashboard_base(settings)
    )


@secret_router.put("/projects/{project_id}/github-connection", response_model=GitHubConnectionOut)
@needs(Permission.PROJECTS_MANAGE)
async def put_github_connection(
    project_id: int,
    payload: GitHubConnectionIn,
    session: SessionDep,
    settings: SettingsDep,
    client_for: ConnectionClientDep,
    principal: PrincipalDep,
) -> GitHubConnectionOut:
    """Save the project's GitHub credentials. The credential is tried against GitHub first, so
    a saved connection always has its bot login and its installations. Secrets are write-only: a
    blank one keeps what is stored, and no read returns one."""
    project = await _require_project(session, project_id)
    return await connection_service.save_github_connection(
        session,
        project,
        payload,
        client_for=client_for,
        by=principal,
        base_url=_dashboard_base(settings),
    )


# ------------------------------------------------------------------------------- secrets


@secret_router.put(
    "/projects/{project_id}/secrets/{name}",
    response_model=SecretName,
    # Name only in the response; there is no read-back endpoint. Values leave the store exactly
    # once, into an agent run at spawn time (design.md §7.1).
    response_model_exclude_none=True,
)
@needs(Permission.PROJECTS_MANAGE)
async def put_secret(
    project_id: int,
    name: str,
    payload: SecretValueIn,
    session: SessionDep,
) -> SecretName | JSONResponse:
    # The body is a plaintext credential: never log, echo, or put it in an event (design.md §7.1).
    refuse_bad_name(name, "secret")
    _refuse_connection_secret(name)
    project = await _require_project(session, project_id)
    try:
        delivery = _delivery(project, payload)
    except _HostRefused as exc:
        return _host_refused(exc)
    await store.put(session, project_id, name, payload.value, delivery)
    return SecretName(name=name)


def _refuse_connection_secret(name: str) -> None:
    if name in CONNECTION_SECRETS:
        raise Conflict(f"{name} is the GitHub connection's. Set it on Ticket source.")


@router.patch("/projects/{project_id}/secrets/{name}", response_model=SecretDeliveryOut)
@needs(Permission.PROJECTS_MANAGE)
async def patch_secret_delivery(
    project_id: int, name: str, payload: SecretDeliveryIn, session: SessionDep
) -> SecretDeliveryOut | JSONResponse:
    """Change how a run is supplied a stored secret without writing its value again."""
    _refuse_connection_secret(name)
    project = await _require_project(session, project_id)
    try:
        delivery = _delivery(project, payload)
    except _HostRefused as exc:
        return _host_refused(exc)
    if not await store.set_delivery(session, project_id, name, delivery):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} has no secret named {name!r}.",
        )
    return _delivery_rows({name: delivery})[0]


def _host_refused(exc: _HostRefused) -> JSONResponse:
    # Coded, so the page puts it on the host field rather than under the whole form.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"code": "egress_host", "detail": as_sentence(str(exc))},
    )


class _HostRefused(ValueError):
    """A header delivery's host this project may not send a secret to."""


#: Hosts whose credential factorial sets itself: a project secret's header never rides them.
_FACTORIAL_HOSTS = ("github.com", "githubusercontent.com")


def _delivery(project: Project, payload: SecretDeliveryIn) -> store.Delivery:
    """How a write asks for the secret to be supplied. A header goes only to one of the project's
    egress hosts: the operator permits where a credential may travel, in one place (decision
    75)."""
    if payload.sandbox_delivery == "env":
        return store.Delivery(mode="env")
    assert payload.egress_host is not None and payload.egress_header is not None
    try:
        allowed = egress_allowlist(project.settings)
    except SettingsInvalid as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=as_sentence(str(exc))
        ) from exc
    try:
        (host,) = egress_hosts([payload.egress_host], source="egress_host")
    except SettingsInvalid as exc:
        raise _HostRefused(str(exc)) from exc
    if host.startswith("*."):
        raise _HostRefused(f"{host} is a wildcard. Name the one host the header goes to.")
    if any(host == domain or host.endswith(f".{domain}") for domain in _FACTORIAL_HOSTS):
        raise _HostRefused(
            f"{host} receives the run's own GitHub credential, so a project secret cannot be "
            "sent to it."
        )
    if not egress_covers(allowed, host):
        raise _HostRefused(
            f"{host} is not one of this project's egress hosts. Add it under Egress hosts on the "
            "Runtime page first."
        )
    return store.Delivery(mode="header", host=host, header=payload.egress_header)


@router.get("/projects/{project_id}/secrets", response_model=SecretList)
@needs(Permission.PROJECTS_READ)
async def list_secrets(project_id: int, session: SessionDep) -> SecretList:
    """Names and sandbox deliveries; `orchestrator.secrets.store` has no "list values" call."""
    await _require_project(session, project_id)
    # The connection's secrets are set and shown on Ticket source, never on the Secrets page.
    return SecretList(
        names=[
            n for n in await store.list_names(session, project_id) if n not in CONNECTION_SECRETS
        ],
        deliveries=_delivery_rows(
            {
                n: d
                for n, d in (await store.deliveries(session, project_id)).items()
                if n not in CONNECTION_SECRETS
            }
        ),
    )


def _delivery_rows(deliveries: Mapping[str, store.Delivery]) -> list[SecretDeliveryOut]:
    return [
        SecretDeliveryOut(name=name, mode=d.mode, host=d.host, header=d.header)
        for name, d in deliveries.items()
    ]


@router.delete("/projects/{project_id}/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT)
@needs(Permission.PROJECTS_MANAGE)
async def delete_secret(project_id: int, name: str, session: SessionDep) -> Response:
    """The Secrets page's Remove and Remove copy, and `scripts/migrate-vendor-keys.py` for a copy
    of a key the runtime now provides (decision 36). The row's delivery goes with it."""
    _refuse_connection_secret(name)
    await _require_project(session, project_id)
    if not await store.delete(session, project_id, name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} has no secret named {name!r}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------- process versions


@router.post(
    "/projects/{project_id}/process-versions/sync",
    response_model=ProcessSyncOut,
    status_code=status.HTTP_201_CREATED,
)
@needs(Permission.PROJECTS_MANAGE)
async def sync_process_versions(
    session: SessionDep,
    config_source: ConfigSourceDep,
    payload: ProcessVersionSyncIn,
    project_id: int = Path(ge=1),
) -> ProcessSyncOut:
    """Read every process the repo offers out of `.factorial/` and snapshot them at ONE sha.

    `ref` is resolved to a concrete sha before anything is read, and that one sha identifies every
    snapshot and pins every `agent` row the processes reference — a process and its agents move
    together or not at all (design.md §2.8). One outcome per lane comes back: `created` (this lane
    had not been read at that commit), `unchanged` (it had), or `retired` (its file is gone from
    this commit).

    `unchanged` is what makes this route usable with several lanes: a commit touching one lane
    leaves every other lane at a commit this project already holds. It needs no content
    comparison to be certain, because a sha is immutable. The row still becomes current again,
    which is how re-syncing an older commit rolls a lane back; tickets in flight hold their own
    `process_version.agent_pins` (revision 0013) and do not move.

    A commit that retires the lane `project.settings.default_process` names no longer fails the
    call: the default moves to a lane the commit offers and `default_process_moved` says so. A
    project whose default names nothing admits no unlabelled issue, and the page cannot re-point
    a setting at a lane the repo has just deleted, so refusing left nothing an operator could do.

    A parse error still fails the whole call — a folder that does not parse is not a partial
    state worth committing. This endpoint's repo I/O is the request itself, so it fails closed
    where the readiness probes degrade. A GitLab ticket source reads that repo through GitLab,
    not the GitHub App. No ticket already in flight changes.
    """
    project = await _require_project(session, project_id)
    return _sync_out(
        await config_service.sync_config(session, config_source, project, ref=payload.ref)
    )


def _sync_out(synced: SyncedConfig) -> ProcessSyncOut:
    result = synced.result
    return ProcessSyncOut(
        sha=result.sha,
        results=[
            ProcessSyncResultOut(
                process=entry.process_key,
                outcome=entry.outcome,
                version_id=entry.row.id if entry.row is not None else None,
            )
            for entry in result.results
        ],
        agent_warnings=synced.agent_warnings,
        default_process_moved=(
            None
            if result.default_moved is None
            else DefaultProcessMovedOut(
                previous=result.default_moved.previous, now=result.default_moved.now
            )
        ),
    )


@router.get("/projects/{project_id}/processes", response_model=list[ProcessSummary])
@needs(Permission.PROJECTS_READ)
async def list_processes(project_id: int, session: SessionDep) -> list[ProcessSummary]:
    """One row per AVAILABLE lane — what the Process config page lists, and what a
    `factorial:process:<key>` label may name."""
    project = await _require_project(session, project_id)
    versions = await _latest_process_versions(session, project_id)
    return process_summaries(
        versions,
        default=default_or_note(project)[0],
        adopted=await adopters(session, project_id),
    )


@router.patch("/projects/{project_id}/permits", response_model=ProjectOut)
@needs(Permission.PROJECTS_MANAGE)
async def change_permits(
    project_id: int, payload: PermitsChange, session: SessionDep
) -> ProjectOut:
    """Add and remove E2B runtime and egress-host permits against the settings as they stand.

    The row is locked for the read-modify-write, so two changes in flight both land. The result
    is checked as `PATCH /projects/{id}` checks a whole settings object.
    """
    project = await _require_project(session, project_id, for_update=True)
    settings = dict(project.settings)
    for key, change in (
        (RUNTIME_KEY, payload.runtime_allowlist),
        (EGRESS_KEY, payload.egress_allowlist),
    ):
        if change is None:
            continue
        current = settings.get(key)
        entries = [e for e in current if isinstance(e, str)] if isinstance(current, list) else []
        added = [e.strip().lower() if key == EGRESS_KEY else e for e in change.add]
        removed = {e.strip().lower() if key == EGRESS_KEY else e for e in change.remove}
        settings[key] = [e for e in dict.fromkeys([*entries, *added]) if e not in removed]
    try:
        validate_settings(settings)
    except SettingsInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    project.settings = settings
    await session.flush()
    await session.refresh(project)
    return ProjectOut.model_validate(project)


@router.put("/projects/{project_id}/default-process", response_model=ProjectOut)
@needs(Permission.PROJECTS_MANAGE)
async def set_default_process(
    project_id: int, payload: DefaultProcessIn, session: SessionDep
) -> ProjectOut:
    """Choose the lane an issue enters when it carries no `factorial:process:<key>` label.

    A settings write, not a repo edit: the default is operator state Postgres owns, because
    `.factorial/` is the folder agents write to and a repo-side default would let an agent
    redirect every future ticket by renaming a file (design.md §7.5). Promoting a lane therefore
    renames nothing and retires nothing — both lanes keep their version series.

    `null` clears it, which is allowed: a project with lanes and no default simply requires every
    issue to carry a process label.
    """
    project = await _require_project(session, project_id, for_update=True)
    settings = dict(project.settings)
    if payload.process_key is None:
        settings.pop(DEFAULT_KEY, None)
    else:
        available = {v.process_key for v in await _latest_process_versions(session, project_id)}
        if payload.process_key not in available:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{payload.process_key!r} is not a process this project offers "
                    f"({', '.join(sorted(available)) or 'none'}). Sync it from the repository "
                    "first."
                ),
            )
        settings[DEFAULT_KEY] = payload.process_key
    project.settings = settings
    await session.flush()
    await session.refresh(project)
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}/pending", response_model=list[PendingIssueOut])
@needs(Permission.ORG_READ)
async def list_pending_issues(
    project_id: int, session: SessionDep, github_for: GitHubForDep
) -> list[PendingIssueOut]:
    """Every open issue the admission pass declines, and why — recomputed, never read back.

    A refusal has to read as *current*. The append-only log answers "what happened"; an operator
    waiting on their issue is asking "is this still true", and only a fresh pass can answer that
    (`worker.admission.pending_issues`, the same code the worker admits with, so the page can
    never show a reason the worker does not act on).
    """
    project = await _require_project(session, project_id)
    source = await ticket_source(session, project_id)
    if source is None:
        return []
    from orchestrator.worker.admission import pending_bound
    from orchestrator.worker.context import ProjectGitHub

    async def github(_session: AsyncSession, found: Project) -> ProjectGitHub | None:
        api = await github_for(found)
        if api is None:
            return None
        return ProjectGitHub.over(api.client, api.contents, bot_login=api.bot_login)

    trackers, forges = host_registries(github)
    bindings = await resolve_bindings(session, project, trackers=trackers, forges=forges)
    if bindings is None:
        return []

    return [
        PendingIssueOut(
            external_key=row.external_key,
            title=row.title,
            process_key=row.process_key,
            reason=row.reason,
            detail=row.detail,
            awaiting=list(row.awaiting),
            needs=list(row.needs),
        )
        for row in await pending_bound(session, project, source, bindings)
    ]


@router.get("/projects/{project_id}/process-versions", response_model=list[ProcessVersionOut])
@needs(Permission.PROJECTS_READ)
async def list_process_versions(project_id: int, session: SessionDep) -> list[ProcessVersionOut]:
    """Every snapshot of every lane, the current one first within each lane (`activated_at`).
    Bodies are omitted — a snapshot is large and rarely wanted. Retired lanes are listed too:
    history stays readable, and `retired_at` is what says the lane is no longer selectable."""
    await _require_project(session, project_id)
    rows = (
        (
            await session.execute(
                select(ProcessVersion)
                .where(ProcessVersion.project_id == project_id)
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
    adopted = await adopters(session, project_id)
    return [
        ProcessVersionOut.model_validate(row).model_copy(
            update={"adopted_by": adopted.get(row.source_commit_sha)}
        )
        for row in rows
    ]


@router.get(
    "/projects/{project_id}/process-versions/{version_id}",
    response_model=ProcessVersionDetailOut,
)
@needs(Permission.PROJECTS_READ)
async def get_process_version(
    project_id: int, version_id: int, session: SessionDep
) -> ProcessVersionDetailOut:
    """One version with its snapshot — what a builder loads to show or start from."""
    await _require_project(session, project_id)
    row = await _require_version(session, project_id, version_id)
    adopted = await adopters(session, project_id)
    return ProcessVersionDetailOut.model_validate(row).model_copy(
        update={"adopted_by": adopted.get(row.source_commit_sha)}
    )


@router.get(
    "/projects/{project_id}/process-versions/{version_id}/raw",
    response_model=ProcessVersionRawOut,
)
@needs(Permission.PROJECTS_READ)
async def get_process_version_raw(
    project_id: int,
    version_id: int,
    session: SessionDep,
    config_source: ConfigSourceOptionalDep,
) -> ProcessVersionRawOut:
    """The version as editable YAML. Prefers the repo file at the pinned commit — the stored
    snapshot is comment-stripped, and an editor prefilled from it would silently discard what
    a human wrote in the file.

    Which file that is comes from the commit, not from today's layout: a version registered
    under `processes/` reads its own file there, and one registered before the folder existed
    reads the root `process.yaml` (`process_path_at`).
    """
    project = await _require_project(session, project_id)
    row = await _require_version(session, project_id, version_id)
    if config_source is not None:
        try:
            path = await config_source.process_path_at(
                project.github_repo, row.process_key, row.source_commit_sha
            )
            text = await config_source.read_process_text(
                project.github_repo, path, row.source_commit_sha
            )
        except Exception as exc:
            # Loud, not degraded: an editor seeded from the comment-stripped snapshot would
            # delete every hand-written comment on its next PR — the loss this route exists
            # to prevent.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Factorial couldn't read the pinned process file from the repository. "
                "Try again.",
            ) from exc
        return ProcessVersionRawOut(content=text, source="repo")
    return ProcessVersionRawOut(content=dump_config_yaml(row.content), source="snapshot")


@router.get(
    "/projects/{project_id}/process-versions/{version_id}/files",
    response_model=ConfigFilesOut,
)
@needs(Permission.PROJECTS_READ)
async def list_process_version_files(
    project_id: int, version_id: int, session: SessionDep, source: ConfigSourceDep
) -> ConfigFilesOut:
    """The whole `.factorial/` folder at this version's pinned commit.

    The configuration explorer's listing. Scoped to the pin rather than to the `factorial` head
    because the pin is what tickets admitted under this version actually run; drift against the
    head is reported per file so an operator can see what a sync would pick up.
    """
    project = await _require_project(session, project_id)
    row = await _require_version(session, project_id, version_id)
    config, _ = parse_snapshot(row)
    inventory = await config_inventory(
        source, project.github_repo, pinned_sha=row.source_commit_sha, config=config
    )
    return ConfigFilesOut(
        config_dir=inventory.config_dir,
        pinned_sha=inventory.pinned_sha,
        head_sha=inventory.head_sha,
        files=[
            ConfigFileOut(path=f.path, kind=f.kind, drift=f.drift, warning=f.warning, sha=f.sha)
            for f in inventory.files
        ],
    )


@router.get(
    "/projects/{project_id}/process-versions/{version_id}/files/{path:path}",
    response_model=ConfigFileContentOut,
)
@needs(Permission.PROJECTS_READ)
async def read_process_version_file(
    project_id: int, version_id: int, path: str, session: SessionDep, source: ConfigSourceDep
) -> ConfigFileContentOut:
    """One config file's text at the pinned commit.

    `path` must be a member of the listing this route recomputes for itself, which is the same
    containment rule `scaffold.assemble` enforces on the write side and for the same reason: a
    path the caller supplies must never be the thing that decides what gets read. Membership,
    not a prefix test — `.factorial/../.github/workflows/ci.yml` starts with the config folder
    and is not in it.
    """
    project = await _require_project(session, project_id)
    row = await _require_version(session, project_id, version_id)
    listed = {e.path for e in await source.list_files(project.github_repo, row.source_commit_sha)}
    if path not in listed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{path} is not in the config folder at {row.source_commit_sha[:7]}.",
        )
    try:
        content = await source.read_file(project.github_repo, path, row.source_commit_sha)
    except ContentsNotFound as exc:  # listed a moment ago, so this is a genuine read failure
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Factorial couldn't read {path} from the repository. Try again.",
        ) from exc
    return ConfigFileContentOut(path=path, content=content)


def _agent_detail(resolution: AgentResolution) -> AgentDetailOut:
    manifest = resolution.manifest
    if manifest is None:
        return AgentDetailOut(name=resolution.name, warning=resolution.reason)
    return AgentDetailOut(
        name=resolution.name,
        model=manifest.model,
        max_turns=manifest.limits.max_turns,
        skills_local=list(manifest.skills.local),
        skills_shared=list(manifest.skills.shared),
        mcp_local=list(manifest.mcp.local),
        mcp_shared=list(manifest.mcp.shared),
        secrets=[declaration.key for declaration in manifest.secrets],
        permissions=(
            None
            if manifest.permissions is None
            else manifest.permissions.model_dump(exclude_none=True)
        ),
    )


@router.get(
    "/projects/{project_id}/process-versions/{version_id}/agents",
    response_model=list[AgentDetailOut],
)
@needs(Permission.PROJECTS_READ)
async def list_process_version_agents(
    project_id: int, version_id: int, session: SessionDep, source: ConfigSourceDep
) -> list[AgentDetailOut]:
    """Every agent this version's process names, read at its pinned commit."""
    project = await _require_project(session, project_id)
    row = await _require_version(session, project_id, version_id)
    config, _ = parse_snapshot(row)
    if config is None:
        return []
    resolved = await resolve_agents(source, project.github_repo, row.source_commit_sha, config)
    return [_agent_detail(resolution) for resolution in resolved]


@router.get("/projects/{project_id}/labels", response_model=LabelsListOut)
@needs(Permission.PROJECTS_READ)
async def list_project_labels(
    project_id: int, session: SessionDep, read_labels: LabelReaderDep
) -> LabelsListOut:
    """Every label the pinned process reads or writes, with its role — and, when the App can
    look, whether the repo defines it. The same palette the create endpoint writes from."""
    project, configs = await _require_configs(session, project_id)
    existing: frozenset[str] | None = None
    if read_labels is not None:
        try:
            existing = await read_labels(project.github_repo)
        except Exception:  # same degrade contract as the readiness row: unknown, never a 500
            log.warning("label read failed for %s; listing as unverified", project.github_repo)
    palette = all_label_palettes(configs)
    return LabelsListOut(
        labels=[
            LabelSpecOut(
                name=name,
                color=color,
                description=description,
                exists=None if existing is None else name in existing,
            )
            for name, (color, description) in sorted(palette.items())
        ]
    )


@router.post(
    "/projects/{project_id}/process-versions/validate",
    response_model=ProcessValidateOut,
)
@needs(Permission.PROJECTS_READ)
async def validate_process(
    project_id: int, payload: ProcessValidateIn, session: SessionDep
) -> ProcessValidateOut:
    """Validate raw `process.yaml` text without storing anything.

    Always 200 with a verdict: a config problem is this endpoint's RESULT, not a request error.
    Errors are the parser's (the config is unusable); warnings are the lint layer's (it parses
    green and would misbehave — see `orchestrator.config.lint`).
    """
    await _require_project(session, project_id)
    # Any member may send text, and parsing it is pure-Python CPU: off the loop every tenant
    # shares.
    return await asyncio.to_thread(_validate_text, payload.content, project_id)


def _validate_text(content: str, project_id: int) -> ProcessValidateOut:
    try:
        config = parse_process(content, origin=f"project {project_id} (unsaved)")
    except ConfigError as exc:
        return ProcessValidateOut(valid=False, errors=[exc.detail])
    return ProcessValidateOut(
        valid=True,
        warnings=lint_process(config),
        content=config.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _file_out(file: FileProposal) -> FileProposalOut:
    return FileProposalOut(
        path=file.path,
        body=file.body,
        component=file.component,
        kind=file.kind,
        variant=file.variant,
        version=file.version,
        facts=dict(file.facts),
        present_in_repo=file.present_in_repo,
    )


def _component_out(component: ComponentProposal) -> ComponentOut:
    return ComponentOut(
        name=component.name,
        kind=component.kind,
        variants=[
            VariantOfferOut(
                variant=v.variant,
                summary=v.summary,
                offered=v.offered,
                default_on=v.default_on,
                reason=v.reason,
                paths=list(v.paths),
            )
            for v in component.variants
        ],
        present_in_repo=component.present_in_repo,
        referenced=component.referenced,
    )


@router.post(
    "/projects/{project_id}/config-branch",
    response_model=ConfigBranchOut,
    status_code=status.HTTP_201_CREATED,
)
@needs(Permission.PROJECTS_MANAGE)
async def create_config_branch(
    project_id: int,
    session: SessionDep,
    seed: ConfigBranchSeederDep,
    payload: ConfigBranchIn | None = None,
) -> ConfigBranchOut:
    """Create the `factorial` branch config is read from, as an orphan commit holding the default
    branch's config folder unchanged, or only a README when it has none or the operator starts
    from a template (issue #248).

    The event is what lets the sync guard accept this commit, which no pull request produced. It
    is committed before the ref exists: an event naming a sha no branch points at is harmless,
    and a branch no event names could never be synced.
    """
    project = await _require_project(session, project_id)
    if seed is None:
        row = (
            await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
        ).scalar_one_or_none()
        detail = (
            f"{display_of(project, row.kind if row is not None else None).unconfigured} "
            "The config branch cannot be seeded."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    async def record(sha: str) -> None:
        await append(
            session,
            type=EventType.CONFIG_BRANCH_CREATED,
            project_id=project.id,
            branch=CONFIG_BRANCH,
            sha=sha,
        )
        await session.commit()

    try:
        sha = await seed(
            project.github_repo, record, payload is not None and payload.from_ == "empty"
        )
    except ConfigBranchExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=as_sentence(str(exc))
        ) from exc
    except GitLabError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"GitLab refused to create the {CONFIG_BRANCH} branch: " + as_sentence(str(exc)),
        ) from exc
    return ConfigBranchOut(branch=CONFIG_BRANCH, sha=sha)


@router.get(
    "/projects/{project_id}/default-branch-config",
    response_model=DefaultBranchConfigOut | None,
)
@needs(Permission.PROJECTS_READ)
async def default_branch_config(
    project_id: int, session: SessionDep, source: ConfigSourceDep
) -> DefaultBranchConfigOut | None:
    """What a seed from the default branch would copy, for the gallery's preview. Null when that
    branch has no config folder. Read-only: nothing is synced, so the sync guard does not apply;
    the seed that follows reads the head again itself."""
    project = await _require_project(session, project_id)
    repo = project.github_repo
    try:
        branch, sha = await source.default_head(repo)
        folder = await source.present_dir(repo, sha)
    except RepoNotVisible as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=as_sentence(str(exc))
        ) from exc
    except ContentsNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{as_sentence(str(exc))} Try again."
        ) from exc
    if folder is None:
        return None
    try:
        configs = await source.load_processes(repo, sha)
    except (ConfigError, ContentsNotFound) as exc:
        detail = exc.detail if isinstance(exc, ConfigError) else str(exc)
        return DefaultBranchConfigOut(
            branch=branch, sha=sha, config_dir=folder, lanes=[], error=detail
        )
    lanes = [
        LanePreviewOut(
            key=key,
            content=config.model_dump(mode="json", by_alias=True),
            agents=[
                _agent_detail(agent) for agent in await resolve_agents(source, repo, sha, config)
            ],
        )
        for key, config in sorted(configs.items())
    ]
    return DefaultBranchConfigOut(branch=branch, sha=sha, config_dir=folder, lanes=lanes)


@router.post("/projects/{project_id}/config-preview", response_model=ConfigPreviewOut)
@needs(Permission.PROJECTS_READ)
async def preview_config_bundle(
    project_id: int, payload: ConfigPreviewIn, session: SessionDep, source: ConfigSourceDep
) -> ConfigPreviewOut:
    """What a commit of this draft would create under `.factorial/`. Writes nothing.

    Reads the repo, because "already present" is the outcome that must never be guessed at: an
    agent the operator has already written prompts for is one this must not offer to overwrite.
    The operator's per-file selection is collected from this response and sent back to
    `config-commits`.
    """
    project = await _require_project(session, project_id)
    forge = forge_of(await ticket_source(session, project_id))
    proposal = await config_service.preview_config(source, project, payload.content, forge=forge)
    return ConfigPreviewOut(
        files=[_file_out(f) for f in proposal.files.values()],
        components=[_component_out(c) for c in proposal.components],
    )


@router.get("/projects/{project_id}/config-branch/unknown-head", response_model=UnknownHeadOut)
@needs(Permission.PROJECTS_READ)
async def preview_unknown_head(
    project_id: int, session: SessionDep, source: ConfigSourceDep
) -> UnknownHeadOut:
    """The commits and files between the newest commit the sync guard accepts and the
    `factorial` head, which it does not. Writes nothing; 409 when the head is already known."""
    project = await _require_project(session, project_id)
    head = await adoption.preview_unknown_head(session, source, project)
    files = head.comparison.files
    return UnknownHeadOut(
        head_sha=head.head_sha,
        known_base=head.known_base,
        commits=[ComparedCommitOut(**asdict(c)) for c in head.comparison.commits],
        files=[ComparedFileOut(**asdict(f)) for f in files],
        total_commits=head.comparison.total_commits,
        files_truncated=len(files) >= 300,
        compare_url=head.compare_url,
    )


@router.post(
    "/projects/{project_id}/config-branch/adoptions",
    response_model=ConfigAdoptionOut,
    status_code=status.HTTP_201_CREATED,
)
@needs(Permission.PROJECTS_MANAGE)
async def adopt_config_head(
    project_id: int,
    payload: ConfigAdoptionIn,
    request: Request,
    session: SessionDep,
    source: ConfigSourceDep,
    settings: SettingsDep,
    person: PersonDep,
) -> ConfigAdoptionOut:
    """Vouch for the `factorial` head the person reviewed, then sync it (design.md §7.7).

    A signed-in person from the dashboard only: the cookie rides along from any page, so the
    `Origin` must name the dashboard, and the shared bearer names no one.
    """
    require_dashboard_origin(request, settings, "Adopt a commit from the dashboard.")
    if person is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Sign in to the dashboard to adopt a commit."
        )
    project = await _require_project(session, project_id)
    adopted = await adoption.adopt_head(
        session, source, project, sha=payload.sha, actor=Actor.dashboard(person)
    )
    return ConfigAdoptionOut(
        sha=adopted.sha,
        adopted=adopted.adopted,
        sync=None if adopted.sync is None else _sync_out(adopted.sync),
        sync_error=adopted.sync_error,
    )


@router.post(
    "/projects/{project_id}/config-commits",
    response_model=ConfigCommitOut,
    status_code=status.HTTP_201_CREATED,
)
@needs(Permission.PROJECTS_MANAGE)
async def commit_config(
    project_id: int,
    payload: ConfigCommitIn,
    session: SessionDep,
    source: ConfigSourceDep,
    write_commit: ConfigCommitWriterDep,
    principal: PrincipalDep,
) -> ConfigCommitOut:
    """Commit the config and its agent gaps to `factorial` as the App, then sync that commit.

    The operator's click is the adoption (design.md §7.7): every picked file must be a member of
    the proposal the server recomputes here (tech-stack decision 33), and every edited one a
    member of the branch's own listing at the head read here. A file already in the repo is
    rewritten only as the operator's edit of the branch's own copy, never from the catalog, so
    re-running the builder cannot overwrite a prompt a human edited. The event naming the commit
    is what lets the sync guard take it.
    """
    project = await _require_project(session, project_id)
    if write_commit is None:
        row = (
            await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
        ).scalar_one_or_none()
        detail = (
            f"{display_of(project, row.kind if row is not None else None).unconfigured} "
            "Commit the config by hand, or connect the project."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    draft = ConfigDraft(
        content=payload.content,
        files=[NewFile(item.path, item.body) for item in payload.files],
        edits=[FileEdit(e.path, e.body, e.base_sha) for e in payload.edits],
        created={item.path: item.body for item in payload.created},
    )
    result = await config_service.commit_config(
        session,
        source,
        write_commit,
        project,
        draft,
        actor=Actor.dashboard(principal),
        message=None,
    )
    return ConfigCommitOut(
        sha=result.sha,
        url=result.url,
        branch=CONFIG_BRANCH,
        committed=result.committed,
        components=[_component_out(c) for c in result.components],
        written=result.written,
        sync=None if result.sync is None else _sync_out(result.sync),
        sync_error=result.sync_error,
    )


@router.get("/process-templates", response_model=ProcessTemplatesOut)
@needs(SIGNED_IN)
async def process_templates() -> ProcessTemplatesOut:
    """The curated starting points, each a folder of whole process files — one per lane — with
    each lane's parse and its catalog agents for the gallery's preview."""
    out: list[ProcessTemplateOut] = []
    for template in PROCESS_TEMPLATES:
        lanes: list[TemplateLaneOut] = []
        for lane in template.lanes:
            config = parse_named_process(
                lane.text, key=lane.key, origin=f"template {template.key}/{lane.key}"
            )
            lanes.append(
                TemplateLaneOut(
                    key=lane.key,
                    yaml=lane.text,
                    content=config.model_dump(mode="json", by_alias=True),
                    agents=[
                        _agent_detail(agent)
                        for agent in catalog_agents(config, forge=template.forge)
                    ],
                )
            )
        out.append(
            ProcessTemplateOut(
                key=template.key,
                title=template.title,
                description=template.description,
                lanes=lanes,
            )
        )
    return ProcessTemplatesOut(templates=out)


@router.get("/vocabulary", response_model=VocabularyOut)
@needs(SIGNED_IN)
async def vocabulary() -> VocabularyOut:
    """The closed registries, for builder pickers."""
    return config_vocabulary()
