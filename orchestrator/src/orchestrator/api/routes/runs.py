"""One run: its transcript, and the requests a reviewer or operator makes of it.

The transcript is the only read that touches the substrate. Everything else answers from
Postgres, so a wedged adapter can hang this one and nothing else.

There is deliberately no raw-stream route. The stream is JSONL of a model harness's internal
events — not something an operator should have to read, and a verbatim forward of a process that
held a minted `GH_TOKEN` and an `ANTHROPIC_API_KEY`. `api.activity` derives instead, and what it
cannot derive (the model's own prose) it truncates and labels.

Stopping a run IS here, but only as a REQUEST. The API does not act on the state machine (see
`tickets.py`), so the stop route appends an `interrupt_requested` event and returns; the worker
observes it on its next pass, calls the substrate, and parks the ticket. That indirection is not
ceremony — the API has no lease on the ticket, and a route that reached for the adapter directly
would race the loop for a run the loop owns.

A step's preview (`events.previews`) is read and published here. Start and Stop, and a
reviewer's change request, are recorded here — as requests, for the loop to honour the way it
honours a stop (decision 54). Publishing a URL is the one write the API makes itself, like
requeue: it changes nothing the loop owns, so there is no pass to ask for it.

What a run's terminal offers is read here (decision 57), by the rule the gateway admits by, and
Open session is recorded here as a request for the inspect pass (`worker.inspect`), which is no
ticket's: it wakes that pass, and the page polls for the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.activity import parse_activity
from orchestrator.api.auth import CallerDep, PrincipalDep
from orchestrator.api.deps import RuntimeAdapterDep, SessionDep, SettingsDep
from orchestrator.api.schemas import (
    ActivityTurnOut,
    ChangedFileOut,
    ChangeRequestIn,
    ChangeRequestOut,
    CompletionIn,
    CompletionOut,
    InspectOut,
    OutputEntryOut,
    OutputsOut,
    PreviewIn,
    PreviewLinkOut,
    PreviewOut,
    PreviewStartIn,
    RequestOut,
    ResumeSessionIn,
    RunActivityResponse,
    TerminalOut,
    TerminalTicketIn,
    TerminalTicketOut,
    ToolCallOut,
)
from orchestrator.api.services import controls
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import STEP_KINDS, parked_at_its_step, ticket_of
from orchestrator.api.services.errors import Conflict, Unavailable
from orchestrator.api.services.outputs import pinned_config, pinned_preview
from orchestrator.api.services.tickets import refuse_while_quarantined
from orchestrator.api.services.waits import session_waits
from orchestrator.api.sessions import mint_terminal_ticket, sign_page_link
from orchestrator.api.wakes import wake_and_wait
from orchestrator.db.models import (
    Event,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
    current_runs,
)
from orchestrator.events import EventType, append, wake_inspect
from orchestrator.events.outputs import (
    SelectedFile,
    files_on_branch,
    outputs_observation,
    select_files,
)
from orchestrator.events.previews import (
    MAX_AWAKE_WORKSPACES,
    awake_inspect_shells,
    awake_workspaces,
    change_request_by_key,
    change_request_outcome,
    inspect_lifecycle,
    pending_change_request,
    preview_keep_until,
    preview_lifecycle,
    preview_links,
    publish_preview_url,
)
from orchestrator.events.terminals import (
    SWITCHED_OFF,
    terminal_admission,
)
from orchestrator.events.workspaces import (
    LIVE_STATUSES,
    WHY_NOT_INSPECTABLE,
    WHY_NOT_SERVABLE,
    KeptWorkspaces,
    kept_workspaces,
)
from orchestrator.settings import Settings
from orchestrator.worker.completions import CompletionAnswer, completion_outcome
from orchestrator.worker.interrupts import pending_resume_request
from runtime.contract import (
    PAGE_MAX_BYTES,
    PREVIEW_RENEWAL,
    AdapterUnavailable,
    RuntimeAdapter,
    UnknownRunError,
)

router = APIRouter(tags=["runs"])


async def _run_with_substrate(session: SessionDep, step_run_id: int) -> StepRun:
    """The row, refused unless it names a run the substrate could know about."""
    row = await _run(session, step_run_id)
    if not row.runtime_run_id:
        # A human gate, an action, or a check row. Not an error in the substrate's sense — there
        # is simply nothing to have logs of, and saying so beats an empty list that reads like a
        # lost run.
        raise Conflict(
            "This run never started an agent, so it has no transcript."
            if row.kind == StepKind.AGENT
            else f"This is {STEP_KINDS[row.kind]} step, so it has no transcript.",
            code="no_transcript",
        )
    return row


def _require_adapter(adapter: RuntimeAdapterDep) -> RuntimeAdapter:
    if adapter is None:
        raise Unavailable(
            "This run's transcript can't be read: its runtime was deleted or isn't available.",
            code="runtime_unavailable",
        )
    return adapter


def _gone() -> HTTPException:
    """A purged run is 410, not 500.

    `UnknownRunError` is a NORMAL state, not a fault: its own docstring says "expired, purged, or
    never accepted", and this substrate reaches it routinely — a run directory is cleaned up when
    its worktree is, and an operator clearing old runs does the same thing deliberately. The row
    stays in Postgres forever because `step_run` is the audit trail; the output behind it does
    not. Gone says exactly that, and distinguishes it from a step run that never existed (404).
    """
    return HTTPException(
        status.HTTP_410_GONE,
        "This run's transcript is no longer kept: its runtime cleaned it up after the run ended.",
    )


@router.get("/step-runs/{step_run_id}/activity", response_model=RunActivityResponse)
@needs(Permission.ORG_READ)
async def get_run_activity(
    step_run_id: int,
    session: SessionDep,
    adapter: RuntimeAdapterDep,
    limit: int = Query(default=400, ge=1, le=2000),
) -> RunActivityResponse:
    """The run's transcript, grouped by assistant turn.

    Derived, not forwarded: see `api.activity` for what is read out of the stream, and for the
    one part (the model's own prose) that cannot be field-restricted the way a tool input can.
    """
    row = await _run_with_substrate(session, step_run_id)
    resolved = _require_adapter(adapter)
    run_id = row.runtime_run_id or ""
    try:
        activity = await parse_activity(
            resolved.logs(run_id),
            limit=limit,
            run_id=run_id,
        )
    except UnknownRunError as exc:
        raise _gone() from exc
    except AdapterUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return RunActivityResponse(
        step_run_id=row.id,
        runtime_run_id=run_id,
        turns=[
            ActivityTurnOut(
                index=turn.index,
                text=turn.text,
                thinking=turn.thinking,
                tools=[
                    ToolCallOut(
                        tool=c.tool,
                        target=c.target,
                        ok=c.ok,
                        result=c.result,
                        question=c.question,
                        answer=c.answer,
                    )
                    for c in turn.tools
                ],
            )
            for turn in activity.turns
        ],
        milestones=activity.milestones,
        outcome=activity.outcome,
        cost_usd=activity.cost_usd,
        truncated=activity.truncated,
    )


@router.post(
    "/step-runs/{step_run_id}/interrupt",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequestOut,
)
@needs(Permission.TICKETS_OPERATE)
async def request_interrupt(
    step_run_id: int, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Ask the loop to stop this run and park its ticket. 202, not 200: what is accepted is a
    request (`api.services.controls.request_interrupt`)."""
    row = await _run(session, step_run_id)
    result = await controls.request_interrupt(session, row, Actor.dashboard(principal))
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/step-runs/{step_run_id}/retry-step",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequestOut,
)
@needs(Permission.TICKETS_OPERATE)
async def request_step_retry(
    step_run_id: int, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Retry: ask the loop to start this failed step's next attempt now, continuing the failed
    attempt's session where it can (decision 77; `api.services.controls.request_step_retry`)."""
    row = await _run(session, step_run_id)
    result = await controls.request_step_retry(session, row, Actor.dashboard(principal))
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/step-runs/{step_run_id}/resume-session",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequestOut,
)
@needs(Permission.TICKETS_OPERATE)
async def request_resume_session(
    step_run_id: int, body: ResumeSessionIn, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Ask the loop to continue a parked session, with `answer` as its turn (decision 26;
    `api.services.controls.request_resume`)."""
    row = await _run(session, step_run_id)
    result = await controls.request_resume(
        session, row, Actor.dashboard(principal), answer=body.answer
    )
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/step-runs/{step_run_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CompletionOut,
)
@needs(Permission.TICKETS_OPERATE)
async def request_completion(
    step_run_id: int, body: CompletionIn, session: SessionDep, principal: PrincipalDep
) -> CompletionOut:
    """Force complete: ask the loop to check this run's postconditions now and accept it when
    they hold (decision 78; `api.services.controls.request_completion`). The dashboard polls
    `GET /step-runs/{id}/completion/{request_id}` for the answer."""
    row = await _run(session, step_run_id)
    result, request_id = await controls.request_completion(
        session,
        row,
        Actor.dashboard(principal),
        allow_unmet=body.allow_unmet,
        seen_failures=body.seen_failures,
    )
    answer = await completion_outcome(session, row.ticket_id, request_id)
    return _completion_out(result.status, result.landed, request_id, answer)


@router.get("/step-runs/{step_run_id}/completion/{request_id}", response_model=CompletionOut)
@needs(Permission.ORG_READ)
async def get_completion(step_run_id: int, request_id: int, session: SessionDep) -> CompletionOut:
    """What became of a Force complete this run recorded."""
    request = await session.get(Event, request_id)
    if (
        request is None
        or request.type != EventType.COMPLETION_REQUESTED
        or request.step_run_id != step_run_id
        or request.ticket_id is None
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This run has no such Force complete request.",
        )
    answer = await completion_outcome(session, request.ticket_id, request_id)
    return _completion_out("requested", answer.outcome != "pending", request_id, answer)


def _completion_out(
    status_: str, landed: bool, request_id: int, answer: CompletionAnswer
) -> CompletionOut:
    return CompletionOut(
        status=status_,
        landed=landed,
        request_id=request_id,
        outcome=answer.outcome,
        reason=answer.reason,
        failures=list(answer.failures),
        overridable=answer.overridable,
    )


async def _run(session: SessionDep, step_run_id: int, *, for_update: bool = False) -> StepRun:
    query = select(StepRun).where(StepRun.id == step_run_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return row


async def _preview_out(
    session: SessionDep, ticket: Ticket, run: StepRun, settings: Settings
) -> PreviewOut:
    preview = await pinned_preview(session, ticket, run)
    served = preview is not None and preview.served_by_runtime
    status_now, latest = await preview_lifecycle(session, run)
    links = await preview_links(session, run)
    why = (
        await kept_workspaces(
            session, datetime.now(UTC), settings.workspace.retention, ticket_id=ticket.id
        )
    ).unservable(run.id)
    kept = why is None
    reason = latest.details.get("reason") if latest is not None else None
    if served and why is not None and status_now in ("starting", "ready", "stopping"):
        # The loop parks a preview it cannot serve; until it records that, frame nothing.
        status_now, links, reason = "stopped", [], WHY_NOT_SERVABLE[why]
    started = (
        latest.details if latest is not None and latest.type == EventType.PREVIEW_STARTED else {}
    )
    head_sha = started.get("head_sha")
    live = run.status in LIVE_STATUSES
    beside = live or run.id in await awake_inspect_shells(session)
    # Exact equality: a park that failed records "renewal\n<why>" and killed the sandbox.
    renewing = latest is not None and (
        (latest.type == EventType.PREVIEW_STOPPED and reason == PREVIEW_RENEWAL)
        or (latest.type == EventType.PREVIEW_REQUESTED and latest.details.get("renewal") is True)
    )
    stops_at = (
        await preview_keep_until(session, run, preview.stop_after, now=datetime.now(UTC))
        if served and preview is not None and not live and status_now in ("starting", "ready")
        else None
    )
    return PreviewOut(
        step_run_id=run.id,
        urls=[PreviewLinkOut(service=service, url=url) for service, url in links],
        status=status_now,
        reason=reason if isinstance(reason, str) and reason else None,
        served_by_runtime=served,
        # A quarantined ticket refuses a Start, so the dashboard must not start one by itself.
        startable=served
        and kept
        and status_now in ("none", "failed", "stopped")
        and ticket.quarantined_at is None,
        unavailable=WHY_NOT_SERVABLE[why] if served and why is not None else None,
        head_sha=head_sha if isinstance(head_sha, str) else None,
        uncommitted=bool(started.get("uncommitted")),
        stops_at=stops_at,
        beside_session=served and beside,
        renewing=served and renewing,
        restartable=served and status_now == "ready",
    )


@router.get("/step-runs/{step_run_id}/preview", response_model=PreviewOut)
@needs(Permission.ORG_READ)
async def get_run_preview(
    step_run_id: int, session: SessionDep, settings: SettingsDep
) -> PreviewOut:
    """The run's preview. Polled by the open viewer: it reads this metadata, never the site
    behind it."""
    row = await _run(session, step_run_id)
    return await _preview_out(session, await ticket_of(session, row), row, settings)


@router.get("/step-runs/{step_run_id}/outputs", response_model=OutputsOut)
@needs(Permission.ORG_READ)
async def get_run_outputs(
    step_run_id: int, session: SessionDep, settings: SettingsDep
) -> OutputsOut:
    """The files the run's step declares (decision 71), in their declared order. Each published
    entry the API will serve carries a link signed for that run and blob (decision 58)."""
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    preview = await pinned_preview(session, ticket, row)
    entries: list[OutputEntryOut] = []
    changed: list[ChangedFileOut] = []
    cut_off = False
    if preview is not None and preview.files:
        observed = await outputs_observation(session, row)
        cut_off = bool(observed and observed.get("cut_off"))
        project = (
            await session.execute(select(Project).where(Project.id == ticket.project_id))
        ).scalar_one()
        source = await session.get(TicketSource, ticket.ticket_source_id)
        gitlab = source is not None and source.kind == TicketSourceKind.GITLAB
        repo = project.github_repo
        entries.extend(
            _file_out(selected, row, repo, settings, gitlab=gitlab, project=project)
            for selected in select_files(preview.files, observed, ticket=ticket.external_key)
        )
        commit = observed.get("commit") if observed else None
        changed.extend(
            ChangedFileOut(
                path=path, github_url=_blob_url(repo, commit, path, gitlab=gitlab, project=project)
            )
            for path in files_on_branch(observed)
            if commit
        )
    return OutputsOut(step_run_id=row.id, entries=entries, cut_off=cut_off, changed_files=changed)


def _blob_url(
    repo: str,
    commit: str,
    path: str,
    *,
    gitlab: bool = False,
    project: Project | None = None,
) -> str:
    if gitlab:
        from orchestrator.gitlab.urls import gitlab_blob_url

        return gitlab_blob_url(
            project.settings if project is not None else {},
            repo,
            commit,
            path,
        )
    return f"https://github.com/{repo}/blob/{commit}/{quote(path)}"


def _file_out(
    selected: SelectedFile,
    run: StepRun,
    repo: str,
    settings: Settings,
    *,
    gitlab: bool = False,
    project: Project | None = None,
) -> OutputEntryOut:
    """A declared file as the dashboard shows it: the frame's link only for a file the API will
    serve."""
    too_large = selected.size is not None and selected.size > PAGE_MAX_BYTES
    url = github_url = group_url = None
    if selected.status == "published" and selected.path and selected.commit:
        github_url = _blob_url(repo, selected.commit, selected.path, gitlab=gitlab, project=project)
        if selected.folder is not None:
            if gitlab:
                from orchestrator.gitlab.urls import gitlab_origin

                settings_bag = project.settings if project is not None else {}
                group_url = f"{gitlab_origin(settings_bag)}/{repo}/-/tree/{selected.commit}"
                group_url += f"/{quote(selected.folder)}" if selected.folder else ""
            else:
                group_url = f"https://github.com/{repo}/tree/{selected.commit}"
                group_url += f"/{quote(selected.folder)}" if selected.folder else ""
        token = settings.api_token.get_secret_value()
        if selected.blob and token and not too_large:
            expires, signature = sign_page_link(
                run.id, selected.blob, api_token=token, now=datetime.now(UTC)
            )
            url = f"/pages/{run.id}?blob={selected.blob}&expires={expires}&signature={signature}"
    return OutputEntryOut(
        kind="file",
        key=selected.key,
        label=selected.label,
        status=selected.status,
        path=selected.path,
        commit=selected.commit,
        size=selected.size,
        too_large=too_large,
        matches=list(selected.matches),
        reason=selected.reason,
        url=url,
        github_url=github_url,
        changed=selected.changed,
        group=selected.group,
        group_total=selected.group_total,
        group_url=group_url,
    )


@router.put("/step-runs/{step_run_id}/preview", response_model=PreviewOut)
@needs(Permission.TICKETS_OPERATE)
async def put_run_preview(
    step_run_id: int, body: PreviewIn, session: SessionDep, settings: SettingsDep
) -> PreviewOut:
    """Publish, replace or withdraw (null) the run's preview URL — the producer's handoff.

    Refused for a step whose pinned process declares no enabled `preview:`: a URL nobody will
    look for is a record of nothing. Publishing the URL already published writes nothing.
    """
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    preview = await pinned_preview(session, ticket, row)
    if preview is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Step {row.step_name!r} has no enabled preview in this ticket's pinned process.",
        )
    if preview.served_by_runtime:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Step {row.step_name!r}'s preview is served by its runtime (preview.services), so "
            "an outside producer cannot publish for it.",
        )
    try:
        await publish_preview_url(
            session, row, body.preview_url, settings=settings, published_via="api"
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await session.commit()
    return await _preview_out(session, ticket, row, settings)


#: What a session view's park records beside its `ViewClosed` code, for the dashboard to word it.
_VIEW_CLOSED_PARAMS: Final = frozenset({"closed_by", "limit_seconds", "after_seconds", "limit"})

_AWAKE_CAP: Final = (
    f"{MAX_AWAKE_WORKSPACES} previews and open sessions are already running. Stop one first."
)


@router.post(
    "/step-runs/{step_run_id}/preview/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PreviewOut,
)
@needs(Permission.TICKETS_OPERATE)
async def start_run_preview(
    step_run_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    body: PreviewStartIn | None = None,
) -> PreviewOut:
    """Ask the loop to wake this run's kept sandbox and serve its preview (decision 54). A
    request, like interrupt: the API never calls a substrate. A Start while one is pending or
    serving records nothing; with `restart` on a serving preview it records a restart. A
    restart while one is starting records nothing either: a setup in progress is never replaced."""
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    refuse_while_quarantined(ticket, "start a preview on")
    current = await _preview_out(session, ticket, row, settings)
    restart = body is not None and body.restart and current.status == "ready"
    if current.status in ("starting", "ready") and not restart:
        return current
    if not restart and not current.startable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            current.unavailable or f"{row.step_name} has no preview to start.",
        )
    # The worker checks again under the lease; this answers a reviewer while they look. A live
    # run's sandbox is awake anyway, so its preview is not refused for the cap.
    if (
        not restart
        and row.status not in LIVE_STATUSES
        and len(await awake_workspaces(session) - {row.id}) >= MAX_AWAKE_WORKSPACES
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, _AWAKE_CAP)
    await session.execute(select(StepRun.id).where(StepRun.id == row.id).with_for_update())
    state = (await preview_lifecycle(session, row))[0]
    if state not in ("starting", "ready") or (restart and state == "ready"):
        await append(
            session,
            type=EventType.PREVIEW_REQUESTED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=row.id,
            step=row.step_name,
            attempt=row.attempt,
            requested_by=principal,
            **({"restart": True} if restart else {}),
        )
        await session.commit()
    await wake_and_wait(session, ticket)
    return await _preview_out(session, ticket, row, settings)


@router.post(
    "/step-runs/{step_run_id}/preview/keep",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PreviewOut,
)
@needs(Permission.TICKETS_OPERATE)
async def keep_run_preview(
    step_run_id: int, session: SessionDep, principal: PrincipalDep, settings: SettingsDep
) -> PreviewOut:
    """Keep a serving preview past its idle close: the clock counts again from now. Recorded,
    like Start; the loop hands the new keep-until to the substrate on its next pass."""
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    refuse_while_quarantined(ticket, "keep a preview on")
    await session.execute(select(StepRun.id).where(StepRun.id == row.id).with_for_update())
    if (await preview_lifecycle(session, row))[0] not in ("starting", "ready"):
        raise HTTPException(status.HTTP_409_CONFLICT, "No preview is running. Start it first.")
    await append(
        session,
        type=EventType.PREVIEW_KEPT,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=row.id,
        step=row.step_name,
        attempt=row.attempt,
        requested_by=principal,
    )
    await session.commit()
    await wake_and_wait(session, ticket)
    return await _preview_out(session, ticket, row, settings)


@router.post(
    "/step-runs/{step_run_id}/preview/stop",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PreviewOut,
)
@needs(Permission.TICKETS_OPERATE)
async def stop_run_preview(
    step_run_id: int, session: SessionDep, principal: PrincipalDep, settings: SettingsDep
) -> PreviewOut:
    """Ask the loop to stop this run's preview and park its sandbox. Nothing awake, nothing
    recorded."""
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    await session.execute(select(StepRun.id).where(StepRun.id == row.id).with_for_update())
    if (await preview_lifecycle(session, row))[0] not in ("starting", "ready"):
        return await _preview_out(session, ticket, row, settings)
    await append(
        session,
        type=EventType.PREVIEW_STOP_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=row.id,
        step=row.step_name,
        attempt=row.attempt,
        requested_by=principal,
    )
    await session.commit()
    await wake_and_wait(session, ticket)
    return await _preview_out(session, ticket, row, settings)


@router.get("/step-runs/{step_run_id}/terminal", response_model=TerminalOut)
@needs(Permission.ORG_READ)
async def get_run_terminal(
    step_run_id: int, session: SessionDep, settings: SettingsDep
) -> TerminalOut:
    """What the run's terminal offers now. The open drawer reads it when the run's status changes,
    when the gateway closes, on every reconnect attempt, and when a later attempt of the step
    appears or changes status; every 5 seconds while the session is IDLE; every 2 seconds while a
    shell starts or a Resume waits; and every 5 seconds, for at most 10 minutes, while the answer
    is neither `settled` nor attachable."""
    row = await _run(session, step_run_id)
    return await _terminal_out(session, settings, await ticket_of(session, row), row)


@router.post(
    "/step-runs/{step_run_id}/terminal-ticket",
    status_code=status.HTTP_201_CREATED,
    response_model=TerminalTicketOut,
)
@needs(Permission.TICKETS_TERMINAL)
async def mint_run_terminal_ticket(
    step_run_id: int,
    session: SessionDep,
    settings: SettingsDep,
    caller: CallerDep,
    body: TerminalTicketIn | None = None,
) -> TerminalTicketOut:
    """A ticket for one handshake with this run's terminal, for a reader whose browser cannot
    send the gateway a session cookie: the API token's holder. It admits nothing here; the
    gateway decides at the handshake, and its attach is the record."""
    if not settings.terminal.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, SWITCHED_OFF)
    await _run(session, step_run_id)
    ticket, expires_at = mint_terminal_ticket(
        step_run_id,
        caller.principal,
        until=caller.until,
        api_token=settings.api_token.get_secret_value(),
        now=datetime.now(UTC),
        epoch=caller.epoch,
    )
    return TerminalTicketOut(ticket=ticket, expires_at=expires_at)


@router.post(
    "/step-runs/{step_run_id}/inspect",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TerminalOut,
)
@needs(Permission.TICKETS_TERMINAL)
async def request_inspect(
    step_run_id: int, session: SessionDep, principal: PrincipalDep, settings: SettingsDep
) -> TerminalOut:
    """Ask to open this run's own session, read-only, in its kept workspace (decision 57):
    recorded as `inspect_requested`, whose id is the start's token, and honoured by the inspect
    pass this wakes. A request while one is pending or a session is open records nothing.

    Refused while the switch is off, the ticket is quarantined, the workspace cannot open a session
    (released, live, or continued by a later attempt) or the deployment's awake cap is reached.
    The pass checks all of it again.
    """
    # Locked before it is read, as the pass locks it: a request and its answer never interleave.
    row = await _run(session, step_run_id, for_update=True)
    ticket = await ticket_of(session, row)
    if (await inspect_lifecycle(session, row))[0] in ("starting", "open"):
        await session.commit()
        return await _terminal_out(session, settings, ticket, row)
    kept = await kept_workspaces(
        session, datetime.now(UTC), settings.workspace.retention, ticket_id=ticket.id
    )
    blocked = _why_no_session(settings, ticket, row, kept)
    if blocked is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, blocked)
    if len(await awake_workspaces(session) - {row.id}) >= MAX_AWAKE_WORKSPACES:
        raise HTTPException(status.HTTP_409_CONFLICT, _AWAKE_CAP)
    await append(
        session,
        type=EventType.INSPECT_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=row.id,
        step=row.step_name,
        attempt=row.attempt,
        requested_by=principal,
    )
    await wake_inspect(session, row.id)
    await session.commit()
    return await _terminal_out(session, settings, ticket, row)


@router.post(
    "/step-runs/{step_run_id}/inspect/close",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TerminalOut,
)
@needs(Permission.TICKETS_TERMINAL)
async def close_inspect(
    step_run_id: int, session: SessionDep, principal: PrincipalDep, settings: SettingsDep
) -> TerminalOut:
    """Ask the inspect pass to close this run's open session at once, freeing its awake slot.
    Nothing starting or open, nothing recorded."""
    if not settings.terminal.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, SWITCHED_OFF)
    row = await _run(session, step_run_id, for_update=True)
    ticket = await ticket_of(session, row)
    if (await inspect_lifecycle(session, row))[0] in ("starting", "open"):
        await append(
            session,
            type=EventType.INSPECT_CLOSE_REQUESTED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=row.id,
            step=row.step_name,
            attempt=row.attempt,
            requested_by=principal,
        )
        await wake_inspect(session, row.id)
    await session.commit()
    return await _terminal_out(session, settings, ticket, row)


def _why_no_session(
    settings: Settings, ticket: Ticket, run: StepRun, kept: KeptWorkspaces
) -> str | None:
    """Why Open session is not offered for this run, or None when it is."""
    if not settings.terminal.enabled:
        return SWITCHED_OFF
    if ticket.quarantined_at is not None:
        return "This ticket is stopped after repeated errors. Requeue it before you open a session."
    if run.kind != StepKind.AGENT:
        return f"This is {STEP_KINDS[run.kind]} step. Only an agent's run has a session to open."
    why = kept.uninspectable(run.id)
    if why is not None:
        return WHY_NOT_INSPECTABLE[why]
    return None


async def _terminal_out(
    session: SessionDep, settings: Settings, ticket: Ticket, run: StepRun
) -> TerminalOut:
    now = datetime.now(UTC)
    retention = settings.workspace.retention
    admission = await terminal_admission(
        session, run.id, now=now, retention=retention, enabled=settings.terminal.enabled
    )
    kept = await kept_workspaces(session, now, retention, ticket_id=ticket.id)
    shell, latest = await inspect_lifecycle(session, run)
    awake = shell in ("starting", "open")
    blocked = None if awake else _why_no_session(settings, ticket, run, kept)
    parked_view = latest if latest is not None and latest.type == EventType.INSPECT_PARKED else None
    reason = parked_view.details.get("reason") if parked_view is not None else None
    reason_details = {
        key: value
        for key, value in (parked_view.details if parked_view is not None else {}).items()
        if key in _VIEW_CLOSED_PARAMS and isinstance(value, str | int)
    }
    idle, parked = await session_waits(session, run, settings.terminal.idle_park_after)
    pending = await pending_resume_request(session, ticket.id)
    resume_pending = pending is not None and pending.step_run_id == run.id
    attachable = admission.refusal is None
    continued_by = kept.continued_by(run.id)
    continued_by_attempt = kept.runs[continued_by].attempt if continued_by is not None else None
    return TerminalOut(
        step_run_id=run.id,
        enabled=settings.terminal.enabled,
        gateway_origin=settings.terminal_origin,
        kind=admission.kind,
        attachable=attachable,
        why_not=admission.refusal.sentence if admission.refusal is not None else None,
        why_not_code=admission.refusal.code if admission.refusal is not None else None,
        read_only=admission.read_only,
        status=run.status,
        idle=idle,
        parked=parked,
        resumable=ticket.quarantined_at is None and await parked_at_its_step(session, ticket, run),
        resume_pending=resume_pending,
        inspect=InspectOut(
            status=shell,
            reason=reason if isinstance(reason, str) and reason else None,
            reason_details=reason_details,
            startable=not awake and blocked is None,
            unavailable=blocked,
        ),
        kept_until=kept.kept_until(run.id, retention),
        settled=not attachable
        and not awake
        and not resume_pending
        and (
            not settings.terminal.enabled
            or run.kind != StepKind.AGENT
            or run.status not in LIVE_STATUSES
            or ticket.quarantined_at is not None
        ),
        continued_by=continued_by,
        continued_by_attempt=continued_by_attempt,
    )


@router.post(
    "/step-runs/{step_run_id}/changes",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ChangeRequestOut,
)
@needs(Permission.TICKETS_OPERATE)
async def request_changes(
    step_run_id: int, body: ChangeRequestIn, session: SessionDep, principal: PrincipalDep
) -> ChangeRequestOut:
    """Ask for changes to the run a waiting gate is reviewing; the loop rejects that gate and
    starts the next attempt with `feedback` as the rework's `change_request` input.

    Recorded against the waiting GATE run, where the loop looks for it (`worker.gates`), and
    only while that gate is reviewing this very attempt. The API grants no approval or merge
    authority by this: the only thing a request can cause is a rejection the gate would
    otherwise have needed a named approver's review for (design.md §7). One unanswered request
    per gate run, and a repeated `idempotency_key` returns the first request's outcome, so no
    number of submissions can start more than one attempt.
    """
    row = await _run(session, step_run_id)
    ticket = await ticket_of(session, row)
    refuse_while_quarantined(ticket, "request changes on")

    request = await change_request_by_key(session, ticket.id, body.idempotency_key)
    if request is None:
        gate_run = await _reviewing_gate_run(session, ticket, row)
        # Serialises concurrent submissions for this review: the checks below must not both
        # pass before either writes.
        await session.execute(select(StepRun.id).where(StepRun.id == gate_run.id).with_for_update())
        request = await change_request_by_key(session, ticket.id, body.idempotency_key)
        if request is None:
            if await pending_change_request(session, ticket.id, gate_run.id) is not None:
                raise Conflict(
                    "A change request for this review is already being sent.",
                    code="change_request_pending",
                )
            request = await append(
                session,
                type=EventType.STEP_CHANGES_REQUESTED,
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                step_run_id=gate_run.id,
                step=gate_run.step_name,
                attempt=gate_run.attempt,
                reviewed_step=row.step_name,
                reviewed_step_run_id=row.id,
                reviewed_attempt=row.attempt,
                feedback=body.feedback,
                requested_by=principal,
                idempotency_key=body.idempotency_key,
            )
            await session.commit()
    if request.details.get("reviewed_step_run_id") != row.id:
        raise Conflict(
            "That request was already sent for another run. Reload and try again.",
            code="key_reused",
        )

    landed = await wake_and_wait(session, ticket)
    outcome, reason = await change_request_outcome(session, ticket.id, request.id)
    return ChangeRequestOut(
        status="requested", landed=landed, request_id=request.id, outcome=outcome, reason=reason
    )


async def _reviewing_gate_run(session: SessionDep, ticket: Ticket, run: StepRun) -> StepRun:
    """The gate run reviewing `run` right now, or a 409 saying why there is none: the ticket
    waits at a gate whose `onReject` reruns `run`'s step, and `run` is that step's latest
    attempt, succeeded before the gate opened.

    The loop checks the same things again when it honours the request (`worker.gates`); these
    are here so a request that can never be honoured is refused while the reviewer is still
    looking, rather than recorded and refused a pass later.
    """
    gate = ticket.current_step
    config = await pinned_config(session, ticket)
    target = config.rerun_agent_step(gate) if ticket.status == TicketStatus.WAITING_GATE else None
    if gate is None or target is None or target.name != run.step_name:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This ticket isn't waiting for a review that sends work back to {run.step_name}.",
        )
    runs = (
        await session.scalars(
            select(StepRun)
            .where(
                StepRun.ticket_id == ticket.id,
                current_runs(ticket.generation),
                StepRun.step_name.in_([run.step_name, gate]),
            )
            .order_by(StepRun.id)
        )
    ).all()
    latest = next((r for r in reversed(runs) if r.step_name == run.step_name), None)
    if latest is None or latest.id != run.id or run.status != StepRunStatus.SUCCEEDED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Attempt {run.attempt} of {run.step_name} isn't the latest finished attempt.",
        )
    gate_run = next((r for r in reversed(runs) if r.step_name == gate), None)
    if gate_run is None or gate_run.status != StepRunStatus.WAITING or gate_run.id < run.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This ticket isn't waiting for review at {gate} on this attempt.",
        )
    return gate_run
