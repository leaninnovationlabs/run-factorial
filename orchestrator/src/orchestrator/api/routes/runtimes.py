"""The runtime registry: operator-defined substrates a repo's `process.yaml` references by slug.

The same trust direction as `project.settings.mcp_allowlist` (design.md §7.5): the repo, which
agents write to, SELECTS a Runtime by slug; these rows, which only an operator can write,
DEFINE it — its type, and the credentials that type needs. `factorial.runtimes` is the
vocabulary; `factorial.registry.check_runtime_binding` is where a sync meets these rows.

Credentials are write-only, like project secrets. A codex subscription runtime can also be
connected by a device-auth login (`api/device_login.py`). A claude subscription has no such flow
— its token comes from `claude setup-token` on the operator's machine and is pasted — so
`start_login` answers 409 for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import SecretStr
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import Permission
from orchestrator.adapters.registry import display_of
from orchestrator.api import device_login
from orchestrator.api.access import SIGNED_IN, needs
from orchestrator.api.bindings import process_bindings
from orchestrator.api.deps import (
    LoginRunnerDep,
    LoginSessionsDep,
    OrganizationDep,
    ProbeHttpDep,
    SessionDep,
    SettingsDep,
)
from orchestrator.api.guards import NoEchoRoute, refuse_bad_name
from orchestrator.api.runtimes_probe import TIMEOUT_SECONDS, probe, probe_e2b
from orchestrator.api.schemas import (
    AuthFailureOut,
    CredentialCheckOut,
    CredentialTestOut,
    ForgeDisplayOut,
    RuntimeCheckIn,
    RuntimeCheckOut,
    RuntimeCredentialIn,
    RuntimeCredentialList,
    RuntimeCredentialName,
    RuntimeCredentialOut,
    RuntimeIn,
    RuntimeLoginOut,
    RuntimeLoginStatusOut,
    RuntimeOptionsOut,
    RuntimeOut,
    RuntimePatch,
    RuntimeProjectOut,
    RuntimeTemplateBuildOut,
)
from orchestrator.api.services.errors import as_sentence
from orchestrator.db.models import (
    Event,
    Project,
    Runtime,
    RuntimeCredential,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
)
from orchestrator.events import EventType
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    ISOLATION_PLACES,
    NOT_IMPLEMENTED_TYPES,
    TEMPLATE_BUILD_RE,
    CredentialValueInvalid,
    Isolation,
    RuntimeDetailsInvalid,
    RuntimeMetadataInvalid,
    create_credential_names,
    credential_name_for,
    credential_value,
    family,
    is_subscription,
    isolation_of,
    offer_of,
    parse_runtime_details,
    parse_runtime_metadata,
    requires_stored_credential,
    template_of,
)
from orchestrator.secrets import store
from orchestrator.settings import Settings
from runtime.adapters.e2b.client import E2bSandboxClient, TemplateBuild, template_builds
from runtime.adapters.e2b.template import names_harness

router = APIRouter(tags=["runtimes"], route_class=NoEchoRoute)
#: Lists and creates within one organization, mounted at `/orgs/{org}` (`api.main`).
org_router = APIRouter(tags=["runtimes"], route_class=NoEchoRoute)


#: A run that still needs its runtime: spawned and not finished, parked at a gate, or kept for
#: a resume. Deleting the runtime under one would strand it mid-flight.
_LIVE_STATUSES: Final = (
    StepRunStatus.RUNNING,
    StepRunStatus.IDLE,
    StepRunStatus.WAITING,
    StepRunStatus.INTERRUPTED,
)

#: How a refused credential reads in a run's failure or idle turn: claude's `API Error: 401 …`
#: and its error type, codex's `unexpected status 401 Unauthorized`, the vendors' own wording,
#: and the adapters' message for a refused stored login. Display only, never a decision.
_AUTH_REFUSALS: Final = (
    "api error: 401",
    "authentication_error",
    "invalid api key",
    "x-api-key",
    "invalid bearer",
    "unauthori",
    "stored login",
)
_REFUSAL_FIELDS: Final = ("failure_reason", "turn_error")
_REASON_MAX: Final = 200
#: The newest agent ends deployment-wide that the page reads, walked backward on the event
#: primary key so a runtime list never scans the whole log: a refusal older than these shows none.
_AUTH_FAILURE_WINDOW: Final = 500


logger = logging.getLogger(__name__)


async def _live_runs(session: AsyncSession, runtime_id: int) -> str:
    """The runtime's live step runs as `"<n> <status>, …"`, or empty when there are none.

    A restart abandons an interrupted run without rewriting its status, so only the ticket's
    current generation counts: an earlier one is never resumed."""
    live = (
        await session.execute(
            select(StepRun.status, func.count(StepRun.id))
            .join(Ticket, Ticket.id == StepRun.ticket_id)
            .where(
                StepRun.runtime_id == runtime_id,
                StepRun.status.in_(_LIVE_STATUSES),
                StepRun.generation == Ticket.generation,
            )
            .group_by(StepRun.status)
        )
    ).all()
    return ", ".join(f"{count} {run_status}" for run_status, count in sorted(live))


def _is_e2b(row: Runtime) -> bool:
    try:
        return isolation_of(row.details) == "e2b"
    except RuntimeDetailsInvalid:
        return False


async def _held_key(session: AsyncSession, row: Runtime) -> str | None:
    held = await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)
    return held.get_secret_value() if held is not None else None


async def _refuse_moving_a_live_team(
    session: AsyncSession, row: Runtime, before: str | None, after: str | None
) -> None:
    """The key names the team, and a paused sandbox resumes only with its own team's key, so
    a key stored, replaced or removed waits for the runtime's live runs. A kept workspace does
    not hold it: the reaper keeps a replaced key's team for `workspace.retention` (decision
    25). Storing the same key again moves nothing and goes through."""
    if before != after and (live := await _live_runs(session, row.id)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} has live step run(s) ({live}) in the sandbox team it "
            "runs in, and this change would move it to another team, where they cannot be "
            "resumed. Make the change after they finish.",
        )


def _forget_a_template_left_in_another_team(
    row: Runtime, before: str | None, after: str | None
) -> None:
    """A template is private to the team that built it, so a key that names another team, or
    none, takes the template with it: the runtime then reads "no E2B template" and its picker
    lists the new team's builds, rather than booting a build its team cannot see."""
    if before != after and template_of(row.details) is not None:
        row.details = {key: value for key, value in row.details.items() if key != "template"}


async def _require_runtime(
    session: AsyncSession, runtime_id: int, *, for_update: bool = False
) -> Runtime:
    query = select(Runtime).where(Runtime.id == runtime_id)
    if for_update:
        query = query.with_for_update()
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime not found.")
    return row


async def _credentials_by_runtime(
    session: AsyncSession, runtime_ids: Sequence[int]
) -> dict[int, list[RuntimeCredentialOut]]:
    """One query for any number of runtimes, grouped; a runtime with none is absent. Name and
    clocks only — the ciphertext column is never selected here."""
    if not runtime_ids:
        return {}
    grouped: dict[int, list[RuntimeCredentialOut]] = {}
    for runtime_id, name, created_at, updated_at in (
        await session.execute(
            select(
                RuntimeCredential.runtime_id,
                RuntimeCredential.name,
                RuntimeCredential.created_at,
                RuntimeCredential.updated_at,
            )
            .where(RuntimeCredential.runtime_id.in_(runtime_ids))
            .order_by(RuntimeCredential.name)
        )
    ).all():
        grouped.setdefault(int(runtime_id), []).append(
            RuntimeCredentialOut(name=name, created_at=created_at, updated_at=updated_at)
        )
    return grouped


async def _connected_by_runtime(
    session: AsyncSession, runtime_ids: Sequence[int]
) -> dict[int, int]:
    """Bound-project counts for any number of runtimes, from the one binding query; a runtime
    with none is absent. Counted per project, not per binding: a project naming the same runtime
    from two lanes is one connected project."""
    if not runtime_ids:
        return {}
    projects: dict[int, set[int]] = {}
    for binding in await process_bindings(session, runtime_ids=runtime_ids):
        if binding.runtime_id is not None:
            projects.setdefault(binding.runtime_id, set()).add(binding.project_id)
    return {runtime_id: len(ids) for runtime_id, ids in projects.items()}


def _refusal(details: dict[str, object]) -> str:
    """The line that names the refusal, capped: a turn error can be a whole CLI dump."""
    lines = [
        line.strip()
        for field in _REFUSAL_FIELDS
        if isinstance(text := details.get(field), str)
        for line in text.splitlines()
        if line.strip()
    ]
    named = (line for line in lines if any(word in line.lower() for word in _AUTH_REFUSALS))
    return next(named, lines[0] if lines else "")[:_REASON_MAX]


async def _last_auth_failures(
    session: AsyncSession, runtime_ids: Sequence[int]
) -> dict[int, AuthFailureOut]:
    """The newest run per runtime that failed or idled on a refused credential, among the
    newest `_AUTH_FAILURE_WINDOW` agent ends; a runtime with none is absent."""
    if not runtime_ids:
        return {}
    recent = (
        select(Event.id, Event.step_run_id, Event.created_at, Event.details)
        .where(Event.type.in_([EventType.AGENT_FAILED, EventType.AGENT_IDLE]))
        .order_by(Event.id.desc())
        .limit(_AUTH_FAILURE_WINDOW)
        .subquery()
    )
    refused = or_(
        *(
            recent.c.details[field].as_string().icontains(word, autoescape=True)
            for field in _REFUSAL_FIELDS
            for word in _AUTH_REFUSALS
        )
    )
    rows = (
        await session.execute(
            select(
                StepRun.runtime_id,
                recent.c.created_at,
                recent.c.details,
                Ticket.id,
                Ticket.external_key,
                Project.github_repo,
            )
            .select_from(recent)
            .join(StepRun, StepRun.id == recent.c.step_run_id)
            .join(Ticket, Ticket.id == StepRun.ticket_id)
            .join(Project, Project.id == Ticket.project_id)
            .where(StepRun.runtime_id.in_(runtime_ids), refused)
            .distinct(StepRun.runtime_id)
            .order_by(StepRun.runtime_id, recent.c.id.desc())
        )
    ).all()
    return {
        int(runtime_id): AuthFailureOut(
            at=at,
            reason=_refusal(details),
            ticket=f"{repo}#{external_key}",
            ticket_id=ticket_id,
        )
        for runtime_id, at, details, ticket_id, external_key, repo in rows
    }


def _runtime_out(
    row: Runtime,
    credentials: list[RuntimeCredentialOut],
    connected_projects: int,
    last_auth_failure: AuthFailureOut | None,
) -> RuntimeOut:
    return RuntimeOut(
        id=row.id,
        organization=row.organization_slug,
        slug=row.slug,
        name=row.name,
        type=row.type,
        details=row.details,
        metadata=row.metadata_,
        credential_names=[credential.name for credential in credentials],
        credentials=credentials,
        connected_projects=connected_projects,
        last_auth_failure=last_auth_failure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _out(session: SessionDep, row: Runtime) -> RuntimeOut:
    return _runtime_out(
        row,
        (await _credentials_by_runtime(session, [row.id])).get(row.id, []),
        (await _connected_by_runtime(session, [row.id])).get(row.id, 0),
        (await _last_auth_failures(session, [row.id])).get(row.id),
    )


def _validated_details(runtime_type: str, details: dict[str, object]) -> dict[str, object]:
    try:
        # Defaults are not stored: a row on the host reads `{}` as it did before isolation.
        return parse_runtime_details(runtime_type, details).model_dump(
            mode="json", exclude_defaults=True
        )
    except RuntimeDetailsInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The runtime details are invalid: {as_sentence(str(exc))}",
        ) from exc


def _validated_metadata(metadata: dict[str, object]) -> dict[str, str]:
    try:
        return parse_runtime_metadata(metadata)
    except RuntimeMetadataInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The runtime metadata is invalid: {as_sentence(str(exc))}",
        ) from exc


# ------------------------------------------------------------------------------- runtimes


@org_router.get("/runtimes", response_model=list[RuntimeOut])
@needs(Permission.RUNTIMES_MANAGE)
async def list_runtimes(session: SessionDep, organization: OrganizationDep) -> list[RuntimeOut]:
    rows = (
        (
            await session.execute(
                select(Runtime)
                .where(Runtime.organization_id == organization.id)
                .order_by(Runtime.id)
            )
        )
        .scalars()
        .all()
    )
    ids = [row.id for row in rows]
    credentials = await _credentials_by_runtime(session, ids)
    connected = await _connected_by_runtime(session, ids)
    refused = await _last_auth_failures(session, ids)
    return [
        _runtime_out(
            row,
            credentials.get(row.id, []),
            connected.get(row.id, 0),
            refused.get(row.id),
        )
        for row in rows
    ]


def _refuse_unoffered(settings: Settings, rt: str, isolation: Isolation) -> None:
    if offer_of(rt, isolation) not in settings.offers:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"This Factorial install does not offer {rt} {ISOLATION_PLACES[isolation]}.",
        )


def _offered_details_and_keys(
    settings: Settings, rt: str, details: dict[str, object], credentials: dict[str, SecretStr]
) -> tuple[dict[str, object], Isolation, dict[str, SecretStr]]:
    """The checks a create and a key check share, in order: details, offer, names, values."""
    valid = _validated_details(rt, details)
    isolation = isolation_of(valid)
    _refuse_unoffered(settings, rt, isolation)
    allowed = create_credential_names(rt, isolation)
    for name in credentials:
        if name not in allowed:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"A {rt} runtime {ISOLATION_PLACES[isolation]} does not read {name}.",
            )
    values: dict[str, SecretStr] = {}
    for name, secret in credentials.items():
        try:
            values[name] = SecretStr(credential_value(name, secret.get_secret_value()))
        except CredentialValueInvalid as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{name}: {as_sentence(str(exc))}"
            ) from exc
    return valid, isolation, values


@org_router.post("/runtimes/check", response_model=RuntimeCheckOut)
@needs(Permission.RUNTIMES_MANAGE)
async def check_runtime(
    payload: RuntimeCheckIn, response: Response, settings: SettingsDep, http: ProbeHttpDep
) -> RuntimeCheckOut:
    """Test the keys in the body, and list the sandbox team's templates, before a runtime exists.
    Nothing is stored, recorded or logged, and the answer is fixed sentences, as
    `check_credential`'s is."""
    response.headers["Cache-Control"] = "no-store"
    valid, isolation, values = _offered_details_and_keys(
        settings, payload.type, payload.details, payload.credentials
    )
    model = probe(payload.type, values, client=http, held="given", details=valid)
    e2b: CredentialCheckOut | None = None
    templates: list[RuntimeTemplateBuildOut] = []
    if isolation == "e2b":
        result, (e2b, templates) = await asyncio.gather(
            model, _check_e2b(values.get(E2B_CREDENTIAL), payload.type)
        )
    else:
        result = await model
    return RuntimeCheckOut(
        model=CredentialCheckOut(ok=result.ok, detail=result.detail),
        local=result.local,
        e2b=e2b,
        templates=templates,
    )


async def _probe_e2b_key(key: str, held: Literal["stored", "given"]) -> CredentialCheckOut:
    client = E2bSandboxClient(key, request_timeout=TIMEOUT_SECONDS)
    checked = await probe_e2b(client, held=held)
    return CredentialCheckOut(ok=checked.ok, detail=checked.detail)


async def _check_e2b(
    key: SecretStr | None, rt: str
) -> tuple[CredentialCheckOut, list[RuntimeTemplateBuildOut]]:
    """A given E2B key's check, and its team's templates when it passes."""
    if key is None:
        return CredentialCheckOut(ok=False, detail=f"No {E2B_CREDENTIAL} was given."), []
    e2b = await _probe_e2b_key(key.get_secret_value(), "given")
    if not e2b.ok:
        return e2b, []
    try:
        return e2b, _template_builds_out(await template_builds(key.get_secret_value()), rt)
    except Exception as exc:
        logger.warning("listing E2B templates for a key check failed: %s", type(exc).__name__)
        return e2b, []


@router.get("/runtime-options", response_model=RuntimeOptionsOut)
@needs(SIGNED_IN)
async def runtime_options(settings: SettingsDep) -> RuntimeOptionsOut:
    return RuntimeOptionsOut(offered=sorted(settings.offers))


@org_router.post("/runtimes", response_model=RuntimeOut, status_code=status.HTTP_201_CREATED)
@needs(Permission.RUNTIMES_MANAGE)
async def create_runtime(
    payload: RuntimeIn, session: SessionDep, settings: SettingsDep, organization: OrganizationDep
) -> RuntimeOut:
    """Create a Runtime. `slug` and `type` are fixed from here on (`RuntimePatch`)."""
    details, _, values = _offered_details_and_keys(
        settings, payload.type, payload.details, payload.credentials
    )
    if template_of(details) is not None and E2B_CREDENTIAL not in values:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"A new runtime holds no {E2B_CREDENTIAL}. Save it, then choose a template "
            "from its team.",
        )
    row = Runtime(
        organization_id=organization.id,
        slug=payload.slug,
        name=payload.name,
        type=str(payload.type),
        details=details,
        metadata_=_validated_metadata(payload.metadata),
    )
    try:
        async with session.begin_nested():  # savepoint — see admin.create_project
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A runtime with slug {payload.slug!r} already exists in {organization.slug!r}.",
        ) from exc
    for name, value in values.items():
        await store.put_for_runtime(session, row.id, name, value.get_secret_value())
    await session.refresh(row)
    return await _out(session, row)


@router.get("/runtimes/{runtime_id}", response_model=RuntimeOut)
@needs(Permission.RUNTIMES_MANAGE)
async def get_runtime(runtime_id: int, session: SessionDep) -> RuntimeOut:
    return await _out(session, await _require_runtime(session, runtime_id))


@router.patch("/runtimes/{runtime_id}", response_model=RuntimeOut)
@needs(Permission.RUNTIMES_MANAGE)
async def update_runtime(
    runtime_id: int, payload: RuntimePatch, session: SessionDep, settings: SettingsDep
) -> RuntimeOut:
    row = await _require_runtime(session, runtime_id, for_update=True)
    if payload.name is not None:
        row.name = payload.name
    if payload.details is not None:
        details = _validated_details(row.type, payload.details)
        # A live run keeps the kind it spawned with, but its E2B sandbox needs the runtime's key,
        # which only an E2B runtime holds.
        moved = details.get("isolation") != row.details.get("isolation")
        if moved:
            _refuse_unoffered(settings, row.type, isolation_of(details))
        if moved and (live := await _live_runs(session, row.id)):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Runtime {row.slug!r} has live step run(s) ({live}). Change its isolation "
                "after they finish.",
            )
        chosen = template_of(details)
        # A template is private to its team, so it is chosen from the team the key names.
        if (
            chosen is not None
            and chosen != template_of(row.details)
            and E2B_CREDENTIAL not in await store.list_names_for_runtime(session, row.id)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Runtime {row.slug!r} holds no {E2B_CREDENTIAL}. Save its team's key "
                "before you choose a template from that team.",
            )
        row.details = details
    if payload.metadata is not None:
        row.metadata_ = _validated_metadata(payload.metadata)
    await session.flush()
    await session.refresh(row)
    return await _out(session, row)


@router.delete("/runtimes/{runtime_id}", status_code=status.HTTP_204_NO_CONTENT)
@needs(Permission.RUNTIMES_MANAGE)
async def delete_runtime(runtime_id: int, session: SessionDep) -> Response:
    """Delete a Runtime and its credentials. Refused while a live run references it; finished
    history keeps its rows with `runtime_id` nulled (ON DELETE SET NULL)."""
    # FOR UPDATE: a worker tick reads the row FOR SHARE while it spawns on it and only commits
    # its RUNNING step_run afterwards, so an unlocked count here could miss a run that is
    # seconds from referencing a row this deletes. Waiting out that tick is the point.
    row = await _require_runtime(session, runtime_id, for_update=True)
    if live := await _live_runs(session, runtime_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} still has live step run(s) ({live}). Wait for them "
            "to finish, or interrupt them first.",
        )
    await session.execute(sql_delete(Runtime).where(Runtime.id == runtime_id))
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runtimes/{runtime_id}/projects", response_model=list[RuntimeProjectOut])
@needs(Permission.RUNTIMES_MANAGE)
async def list_runtime_projects(runtime_id: int, session: SessionDep) -> list[RuntimeProjectOut]:
    """The projects bound to this runtime — the rows `connected_projects` counts. One row per
    project however many of its lanes name the runtime."""
    await _require_runtime(session, runtime_id)
    bindings = await process_bindings(session, runtime_ids=[runtime_id])
    ids = {binding.project_id for binding in bindings}
    projects = {
        row.id: row
        for row in (await session.execute(select(Project).where(Project.id.in_(ids)))).scalars()
    }
    sources = await session.execute(
        select(TicketSource.project_id, TicketSource.kind).where(TicketSource.project_id.in_(ids))
    )
    kinds: dict[int, str] = {project_id: kind for project_id, kind in sources}
    seen: set[int] = set()
    rows: list[RuntimeProjectOut] = []
    for binding in bindings:
        if binding.project_id in seen:
            continue
        seen.add(binding.project_id)
        rows.append(
            RuntimeProjectOut(
                id=binding.project_id,
                name=binding.project_name,
                github_repo=binding.github_repo,
                forge=ForgeDisplayOut(
                    **display_of(
                        projects[binding.project_id], kinds.get(binding.project_id)
                    ).payload()
                ),
            )
        )
    return rows


@router.get("/runtimes/{runtime_id}/templates", response_model=list[RuntimeTemplateBuildOut])
@needs(Permission.RUNTIMES_MANAGE)
async def list_template_builds(
    runtime_id: int, session: SessionDep
) -> list[RuntimeTemplateBuildOut]:
    """The ready template builds in the E2B team this runtime spawns into, newest first, so
    the page offers pins rather than asking for one, each with its session verdict. One
    of the API's two E2B reads (decision 25): it reads with the runtime's own key only, so a
    template is chosen once the team is, and answers with pins and verdicts only."""
    row = await _require_runtime(session, runtime_id)
    if not _is_e2b(row):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} does not run in a cloud sandbox.",
        )
    key = await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)
    if key is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} holds no {E2B_CREDENTIAL}. Save it to list its "
            "team's templates.",
        )
    try:
        builds = await template_builds(key.get_secret_value())
    except Exception as exc:
        # Never the exception's text: an HTTP error from E2B can carry the request's headers.
        logger.warning("listing E2B templates for runtime %s failed: %s", row.slug, type(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="E2B did not list this team's templates. Check the key, or try again.",
        ) from None
    return _template_builds_out(builds, row.type)


def _template_builds_out(builds: Iterable[TemplateBuild], rt: str) -> list[RuntimeTemplateBuildOut]:
    return [
        RuntimeTemplateBuildOut(
            pin=build.pin,
            template=build.template,
            build_id=build.build_id,
            created_at=build.created_at,
            cpu_count=build.cpu_count,
            memory_mb=build.memory_mb,
            matches_harness=names_harness(build.template, family(rt)),
            verified=build.verified,
        )
        for build in builds
        if TEMPLATE_BUILD_RE.fullmatch(build.pin)
    ]


# ---------------------------------------------------------------------------- credentials


@router.get("/runtimes/{runtime_id}/credentials", response_model=RuntimeCredentialList)
@needs(Permission.RUNTIMES_MANAGE)
async def list_credentials(runtime_id: int, session: SessionDep) -> RuntimeCredentialList:
    """Names only — the store has no "list values" call."""
    await _require_runtime(session, runtime_id)
    return RuntimeCredentialList(
        names=list(await store.list_names_for_runtime(session, runtime_id))
    )


@router.put(
    "/runtimes/{runtime_id}/credentials/{name}",
    response_model=RuntimeCredentialName,
    response_model_exclude_none=True,
)
@needs(Permission.RUNTIMES_MANAGE)
async def put_credential(
    runtime_id: int,
    name: str,
    payload: RuntimeCredentialIn,
    session: SessionDep,
) -> RuntimeCredentialName:
    # The body is a plaintext credential: never log, echo, or put it in an event (design.md §7.1).
    refuse_bad_name(name, "credential")
    try:
        value = credential_value(name, payload.value)
    except CredentialValueInvalid as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{name}: {as_sentence(str(exc))}"
        ) from exc
    # FOR UPDATE for the same reason as `delete_runtime`: a spawn seconds from committing counts.
    row = await _require_runtime(session, runtime_id, for_update=True)
    if not requires_stored_credential(row.type) and name == credential_name_for(row.type):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Runtime {row.slug!r} is {row.type}, which mints {name} from the "
                "host's AWS role, so it is never stored."
            ),
        )
    if name == E2B_CREDENTIAL:
        if not _is_e2b(row):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Runtime {row.slug!r} does not run in a cloud sandbox. Only a cloud "
                f"sandbox runtime holds {E2B_CREDENTIAL}.",
            )
        before = await _held_key(session, row)
        await _refuse_moving_a_live_team(session, row, before, value)
        _forget_a_template_left_in_another_team(row, before, value)
    await store.put_for_runtime(session, runtime_id, name, value)
    return RuntimeCredentialName(name=name)


@router.delete("/runtimes/{runtime_id}/credentials/{name}", status_code=status.HTTP_204_NO_CONTENT)
@needs(Permission.RUNTIMES_MANAGE)
async def delete_credential(runtime_id: int, name: str, session: SessionDep) -> Response:
    row = await _require_runtime(session, runtime_id, for_update=True)
    if name not in await store.list_names_for_runtime(session, runtime_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Runtime {runtime_id} has no credential named {name!r}.",
        )
    if name == E2B_CREDENTIAL and _is_e2b(row):
        before = await _held_key(session, row)
        await _refuse_moving_a_live_team(session, row, before, None)
        _forget_a_template_left_in_another_team(row, before, None)
    await store.delete_for_runtime(session, runtime_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runtimes/{runtime_id}/test-credential", response_model=CredentialTestOut)
@needs(Permission.RUNTIMES_MANAGE)
async def check_credential(
    runtime_id: int, session: SessionDep, http: ProbeHttpDep
) -> CredentialTestOut:
    """Prove the stored model credential now, and an E2B runtime's team key beside it, with
    the vendor's cheapest authenticated call (`api.runtimes_probe`), never a sandbox. Nothing
    is stored or recorded, and the answer is a fixed sentence: a value is read only to be sent.
    Not rate-limited, like every route here: each press is one GET per credential."""
    row = await _require_runtime(session, runtime_id)
    name = credential_name_for(row.type)
    held = await store.get_for_runtime(session, row.id, name)
    result = await probe(
        row.type,
        {} if held is None else {name: held},
        client=http,
        held="stored",
        details=row.details,
    )
    e2b: CredentialCheckOut | None = None
    if _is_e2b(row):
        key = await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)
        if key is None:
            e2b = CredentialCheckOut(
                ok=False, detail=f"The runtime holds no {E2B_CREDENTIAL}. Save it first."
            )
        else:
            e2b = await _probe_e2b_key(key.get_secret_value(), "stored")
    return CredentialTestOut(ok=result.ok, detail=result.detail, local=result.local, e2b=e2b)


# ---------------------------------------------------------------------------------- login


@router.post(
    "/runtimes/{runtime_id}/login",
    response_model=RuntimeLoginOut,
    status_code=status.HTTP_202_ACCEPTED,
)
@needs(Permission.RUNTIMES_MANAGE)
async def start_login(
    runtime_id: int, session: SessionDep, start: LoginRunnerDep, sessions: LoginSessionsDep
) -> RuntimeLoginOut:
    """Begin a device-auth login for a subscription runtime.

    202: nothing is stored when this returns. The operator opens the URL and types the code.
    A watcher task owns what happens next — it stores `auth.json` under the type's credential
    name the moment the CLI exits, or kills the CLI at expiry, and removes the scratch home
    either way. The client's poll of `GET /runtimes/{id}/login/{session_id}` only reads the
    outcome; a tab closed after authorising must not be what decides whether a real login is
    left on disk. The one-time code goes to
    the client and nowhere else — no event, no log line.
    """
    row = await _require_runtime(session, runtime_id)
    if row.type in NOT_IMPLEMENTED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Sign-in for runtime type {row.type!r} is not implemented yet.",
        )
    if not is_subscription(row.type):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} is {row.type}, which does not use device sign-in.",
        )
    if family(row.type) == "claude":
        # Not 501: the type is built. Claude Code simply has no device-code flow to run here —
        # the token is minted in the operator's own browser, so the paste route is the login.
        name = credential_name_for(row.type)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Runtime {row.slug!r} is {row.type}, and Claude Code has no device sign-in. "
            "Run `claude setup-token` on your own machine and paste the token it prints as "
            f"`{name}` on this runtime's page.",
        )

    return await device_login.begin(row.id, credential_name_for(row.type), start, sessions)


@router.get("/runtimes/{runtime_id}/login/{session_id}", response_model=RuntimeLoginStatusOut)
@needs(Permission.RUNTIMES_MANAGE)
async def login_status(runtime_id: int, session_id: str) -> RuntimeLoginStatusOut:
    """Read a login's state. Settling is the watcher's alone (`device_login._watch`): a poll
    that could store would make the outcome depend on a client staying on the page."""
    login = device_login.find(runtime_id, session_id)
    if login is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sign-in session not found."
        )
    return RuntimeLoginStatusOut(session_id=session_id, status=login.status, detail=login.detail)
