"""Ticket read models: the board, one ticket's detail, one ticket's timeline, its issue, and
what restarting it would do.

Read models, plus seven writes. The API never advances the state machine, spawns an agent,
writes a label or merges anything — that is the worker's job (CLAUDE.md, division of labor).
So `POST …/exit`, `POST …/reenter`, `POST …/unblock`, `POST …/restart` and `POST …/redo`
append an event, commit it, wake the worker for that ticket and wait for the pass
(`api.wakes`), and return 202; the worker observes the event on that pass and does the
transition (decision 29), the same shape `runs.py`'s interrupt route takes.
That indirection is not ceremony — the API has no lease on the ticket. `POST …/refresh` is the
same wake with nothing recorded. `POST …/requeue` is the one exception, and its own docstring
says why: it clears an operational flag that gates leasing rather than advancing the machine, and
the loop could never observe a request event for a ticket it does not lease.

Every route here answers from Postgres except `GET /tickets/{id}/issue` and
`GET /tickets/{id}/restart/preview`, which read GitHub and fail when GitHub does. Slow ones:
those reads, and the seven writes above, which hold their answer up to `api.wakes.WAIT_SECONDS`
for the worker's pass.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from githubkit.exception import RequestFailed
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.auth import PrincipalDep
from orchestrator.api.deps import (
    GITHUB_NOT_CONNECTED,
    GitHubForDep,
    IssueReaderDep,
    RepoReaderDep,
    SessionDep,
)
from orchestrator.api.gate_pull import observed_gate_pull_request
from orchestrator.api.schemas import (
    BoardCellOut,
    BoardResponse,
    ExitIn,
    GatePullRequestOut,
    InFlightRunOut,
    ProjectSpendOut,
    RedoIn,
    RefreshOut,
    RequestOut,
    RestartIn,
    RestartPreviewOut,
    SandboxWaitOut,
    StepRunOut,
    TicketDetail,
    TicketIssueOut,
    TicketListItem,
    TicketListResponse,
    TicketProcessOut,
    TimelineEntryOut,
    TimelineResponse,
)
from orchestrator.api.services import controls, redos, restarts
from orchestrator.api.services import tickets as ticket_requests
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict
from orchestrator.api.wakes import await_pass, wake_for_request
from orchestrator.config import GateStep, ProcessConfig
from orchestrator.config.conditions import AllOf
from orchestrator.db.models import (
    TICKET_STATUSES,
    Event,
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events import (
    EventType,
    in_flight_runs,
    pipeline_board,
    project_spend,
    project_tickets,
    sandbox_waits,
    ticket_park_reason,
    ticket_timeline,
)
from orchestrator.factorial.registry import lane_head
from orchestrator.predicates.words import describe
from orchestrator.worker.gate_conflicts import pending_conflict_fix
from orchestrator.worker.redos import redo_target

router = APIRouter(tags=["tickets"])
logger = logging.getLogger(__name__)


@router.get("/projects/{project_id}/board", response_model=BoardResponse)
@needs(Permission.ORG_READ)
async def get_board(project_id: int, session: SessionDep) -> BoardResponse:
    """Tickets by step and status, what is currently in flight, and what the project has spent."""
    cells = await pipeline_board(session, project_id)
    running = await in_flight_runs(session, project_id)
    spend = await project_spend(session, project_id)
    return BoardResponse(
        project_id=project_id,
        cells=[BoardCellOut.model_validate(cell) for cell in cells],
        in_flight=[InFlightRunOut.model_validate(run) for run in running],
        spend=ProjectSpendOut.model_validate(spend),
    )


@router.get("/projects/{project_id}/tickets", response_model=TicketListResponse)
@needs(Permission.ORG_READ)
async def list_tickets(
    project_id: int,
    session: SessionDep,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description=f"one of {', '.join(TICKET_STATUSES)}"),
    ] = None,
    open_only: Annotated[bool, Query(description="exclude tickets that have left the pipeline")] = (
        False
    ),
) -> TicketListResponse:
    """Every ticket in the project, newest first — the board's counts, itemised.

    An unknown `status` is a 422 rather than an empty list: returning nothing for a typo reads
    exactly like "this project has no blocked tickets".
    """
    if status_filter is not None and status_filter not in TICKET_STATUSES:
        known = ", ".join(TICKET_STATUSES)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown status {status_filter!r}. Use one of {known}.",
        )
    rows = await project_tickets(session, project_id, status=status_filter, open_only=open_only)
    return TicketListResponse(
        project_id=project_id,
        status=status_filter,
        open_only=open_only,
        tickets=[TicketListItem.model_validate(row) for row in rows],
    )


async def _ticket_detail(session: SessionDep, ticket: Ticket) -> TicketDetail:
    """One ticket with its attempts. Relationships are `lazy="raise"`, so the runs and the pinned
    snapshot's lane are separate explicit queries rather than accidental lazy loads from an async
    context."""
    process_key, process_sha, content = (
        await session.execute(
            select(
                ProcessVersion.process_key,
                ProcessVersion.source_commit_sha,
                ProcessVersion.content,
            ).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).one()
    head = await lane_head(session, ticket.project_id, process_key)
    organization, repo = (
        await session.execute(
            select(Project.organization_slug, Project.github_repo).where(
                Project.id == ticket.project_id
            )
        )
    ).one()
    runs = (
        (
            await session.execute(
                select(StepRun)
                .where(StepRun.ticket_id == ticket.id)
                .order_by(StepRun.created_at, StepRun.id)
            )
        )
        .scalars()
        .all()
    )

    try:
        config: ProcessConfig | None = ProcessConfig.model_validate(content)
    except ValidationError:
        logger.debug("ticket %s: its pinned process does not parse", ticket.id)
        config = None

    wait = (await sandbox_waits(session, ticket_id=ticket.id)).get(ticket.id)
    # Field by field, not `model_validate(ticket)`: `Ticket.step_runs` is `lazy="raise"`, so
    # letting Pydantic read the attribute would raise instead of using the query above.
    return TicketDetail(
        id=ticket.id,
        project_id=ticket.project_id,
        organization=organization,
        external_key=ticket.external_key,
        title=ticket.title,
        process_version_id=ticket.process_version_id,
        process_sha=process_sha,
        process_key=process_key,
        lane_head=None if head is None else restarts.pin_of(head),
        generation=ticket.generation,
        owner_at_admission=ticket.owner_at_admission,
        current_step=ticket.current_step,
        status=ticket.status,
        admitted_at=ticket.admitted_at,
        closed_at=ticket.closed_at,
        quarantined_at=ticket.quarantined_at,
        consecutive_failures=ticket.consecutive_failures,
        park_reason=await ticket_park_reason(session, ticket.id),
        redo_target=_redo_target(ticket, config),
        waiting_on=_waiting_on(ticket, config, repo),
        sandbox_wait=(
            None if wait is None else SandboxWaitOut.model_validate(wait)
        ),
        step_runs=await _step_runs_out(session, ticket, runs, config),
    )


async def _step_runs_out(
    session: AsyncSession, ticket: Ticket, runs: Sequence[StepRun], config: ProcessConfig | None
) -> list[StepRunOut]:
    """The runs, with the conflict facts of the gate runs among them: two queries for the ticket,
    never one per run."""
    gate_ids = [run.id for run in runs if run.kind == StepKind.HUMAN_GATE]
    sent_back: set[int] = set()
    readings: dict[int, tuple[str, str | None]] = {}
    if gate_ids:
        events = await session.execute(
            select(Event.step_run_id, Event.type, Event.details)
            .where(
                Event.ticket_id == ticket.id,
                Event.step_run_id.in_(gate_ids),
                Event.type.in_((EventType.GATE_CONFLICT_SENT_BACK, EventType.GATE_MERGEABILITY)),
            )
            .order_by(Event.id)
        )
        for run_id, kind, details in events:
            if kind == EventType.GATE_CONFLICT_SENT_BACK:
                sent_back.add(run_id)
            else:
                readings[run_id] = (details.get("state"), details.get("base_ref"))
    out: list[StepRunOut] = []
    for run in runs:
        item = StepRunOut.model_validate(run)
        item.conflict_send_back = run.id in sent_back and run.status == StepRunStatus.REJECTED
        if run.kind == StepKind.HUMAN_GATE and run.status == StepRunStatus.WAITING:
            step = None if config is None else config.find_step(run.step_name)
            if isinstance(step, GateStep) and step.gate.on_approve.merge:
                assert config is not None
                state, base_ref = readings.get(run.id, (None, None))
                if state == "conflicting" or state == "behind":
                    item.mergeability = state
                item.base_ref = base_ref
                item.can_fix_conflict = config.conflict_fix_step(run.step_name) is not None
                item.conflict_fix_pending = (
                    await pending_conflict_fix(session, ticket.id, run.id) is not None
                )
        out.append(item)
    return out


def _redo_target(ticket: Ticket, config: ProcessConfig | None) -> str | None:
    """`worker.redos.redo_target` for the detail. A snapshot that no longer parses offers no
    Redo, since no pass could answer one, and must not take the ticket's page down with it."""
    if (
        config is None
        or ticket.quarantined_at is not None
        or ticket.status
        in (
            TicketStatus.DONE,
            TicketStatus.EXITED,
        )
    ):
        return None
    return redo_target(config, ticket.current_step, ticket.status)


def _waiting_on(ticket: Ticket, config: ProcessConfig | None, repo: str) -> list[str]:
    """The current step's preconditions in words, one per `all` child, with the values a ticket
    knows without reading GitHub expanded. A `{slug}` the agent has not chosen yet reads as `…`."""
    step = None if config is None else config.find_step(ticket.current_step)
    condition = getattr(step, "preconditions", None)
    if condition is None:
        return []
    values = {
        "ticket": ticket.external_key,
        "ticket_owner": ticket.owner_at_admission,
        "repo": repo,
    }
    children = condition.all_of if isinstance(condition, AllOf) else (condition,)
    return [describe(child, values) for child in children]


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
@needs(Permission.ORG_READ)
async def get_ticket(ticket_id: int, session: SessionDep) -> TicketDetail:
    return await _ticket_detail(session, await _require_ticket(session, ticket_id))


@router.post("/tickets/{ticket_id}/refresh", response_model=RefreshOut)
@needs(Permission.TICKETS_OPERATE)
async def refresh_ticket(ticket_id: int, session: SessionDep, response: Response) -> RefreshOut:
    """Ask the worker for one pass over this ticket now, and wait for it (decision 4).

    The worker does the pass; this route has no lease and never reconciles. It writes no event
    either — nothing about the ticket changes here, and the answer is on the page. 200 once a
    pass that claimed the ticket after this request has released it; 202 when
    `api.wakes.WAIT_SECONDS` runs out first (a fleet tick holds the ticket, or no worker is
    listening), and the page's own polling picks the pass up.
    """
    ticket = await _require_ticket(session, ticket_id)
    if ticket.status == TicketStatus.DONE:
        raise ticket_requests.nothing_to(ticket, "refresh")
    ticket_requests.refuse_while_quarantined(ticket, "refresh")
    if failures := ticket.consecutive_failures:
        # A pass here counts toward quarantine (decision 31); the loop retries it on its own.
        checks = "check" if failures == 1 else f"{failures} checks"
        raise Conflict(
            f"The last {checks} of this ticket failed. It is checked again automatically.",
            code="failing",
        )
    requested_at = await wake_for_request(session, ticket)
    # Never None: a failing ticket, the one case that wakes nothing, was refused above.
    assert requested_at is not None
    landed = await await_pass(session, ticket.id, requested_at)
    await session.refresh(ticket)
    if not landed:
        response.status_code = status.HTTP_202_ACCEPTED
    return RefreshOut(
        landed=landed, requested_at=requested_at, ticket=await _ticket_detail(session, ticket)
    )


async def _require_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = (
        await session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    return ticket


@router.post(
    "/tickets/{ticket_id}/exit", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def request_exit(
    ticket_id: int, session: SessionDep, principal: PrincipalDep, body: ExitIn | None = None
) -> RequestOut:
    """Ask the loop to take this ticket off the factory floor (decision 29), closing its issue
    too when `close_issue` (decision 79). 202, not 200: what is accepted is a request
    (`api.services.tickets.request_exit`)."""
    ticket = await _require_ticket(session, ticket_id)
    result = await ticket_requests.request_exit(
        session,
        ticket,
        Actor.dashboard(principal),
        close_issue=body is not None and body.close_issue,
    )
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/tickets/{ticket_id}/requeue", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def requeue_ticket(
    ticket_id: int, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Put a quarantined ticket back into the loop. The one write here that is not a request
    (`api.services.tickets.request_requeue` says why)."""
    ticket = await _require_ticket(session, ticket_id)
    result = await ticket_requests.request_requeue(session, ticket, Actor.dashboard(principal))
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/tickets/{ticket_id}/reenter", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def request_reentry(
    ticket_id: int, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Ask the loop to bring an exited ticket back (`api.services.tickets.request_reentry`)."""
    ticket = await _require_ticket(session, ticket_id)
    result = await ticket_requests.request_reentry(session, ticket, Actor.dashboard(principal))
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/tickets/{ticket_id}/unblock", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def request_unblock(
    ticket_id: int, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Ask the loop to release an escalation, as removing `factorial:blocked` would
    (`api.services.controls.request_unblock`)."""
    ticket = await _require_ticket(session, ticket_id)
    result = await controls.request_unblock(session, ticket, Actor.dashboard(principal))
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/tickets/{ticket_id}/restart", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def request_restart(
    ticket_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    body: RestartIn | None = None,
) -> RequestOut:
    """Ask the loop to restart this ticket from scratch on its lane's current process
    (`api.services.restarts.request_restart`). The body names the lane head the caller saw;
    without one, the head at the time."""
    ticket = await _require_ticket(session, ticket_id)
    asked = body or RestartIn()
    result = await restarts.request_restart(
        session,
        ticket,
        Actor.dashboard(principal),
        process_version_id=asked.process_version_id,
        allow_current_pin=asked.allow_current_pin,
        allow_merged=asked.allow_merged,
    )
    return RequestOut(status=result.status, landed=result.landed)


@router.post(
    "/tickets/{ticket_id}/redo", status_code=status.HTTP_202_ACCEPTED, response_model=RequestOut
)
@needs(Permission.TICKETS_OPERATE)
async def request_redo(
    ticket_id: int, body: RedoIn, session: SessionDep, principal: PrincipalDep
) -> RequestOut:
    """Ask the loop to redo a step and every step after it from a clean start
    (`api.services.redos.request_redo`). Dashboard only."""
    ticket = await _require_ticket(session, ticket_id)
    result = await redos.request_redo(
        session, ticket, Actor.dashboard(principal), target_step=body.target_step, note=body.note
    )
    return RequestOut(status=result.status, landed=result.landed)


@router.get("/tickets/{ticket_id}/restart/preview", response_model=RestartPreviewOut)
@needs(Permission.ORG_READ)
async def get_restart_preview(
    ticket_id: int, session: SessionDep, read_repo: RepoReaderDep
) -> RestartPreviewOut:
    """What restarting this ticket now would cancel, close and delete, and what refuses it
    (`api.services.restarts.restart_preview`). Read live off GitHub, and advisory: the worker
    reads GitHub again when it answers the request."""
    ticket = await _require_ticket(session, ticket_id)
    return await restarts.restart_preview(session, ticket, read_repo)


@router.get("/projects/{project_id}/tickets/{external_key}", response_model=TicketDetail)
@needs(Permission.ORG_READ)
async def get_ticket_by_external_key(
    project_id: int, external_key: str, session: SessionDep
) -> TicketDetail:
    """The same detail, addressed the way an operator holds a ticket: by the issue number.

    A distinct path rather than type-sniffing on `/tickets/{ticket_id}` — an all-digits external
    key must never be readable as an internal id. Unique per project: one ticket source per
    project, and `(ticket_source_id, external_key)` is unique.
    """
    ticket = (
        await session.execute(
            select(Ticket).where(
                Ticket.project_id == project_id, Ticket.external_key == external_key
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    return await _ticket_detail(session, ticket)


@router.get("/tickets/{ticket_id}/process", response_model=TicketProcessOut)
@needs(Permission.ORG_READ)
async def get_ticket_process(ticket_id: int, session: SessionDep) -> TicketProcessOut:
    """The process snapshot this ticket is pinned to.

    Pinned, never latest: editing config in the repo writes a new version row and leaves tickets
    in flight alone (design.md §2.8), so a graph drawn from the project's newest config would
    show a viewer steps their ticket will never run. One join, because a ticket without its
    snapshot is a foreign key violation, not a state the reader has to handle.
    """
    row = (
        await session.execute(
            select(Ticket, ProcessVersion)
            .join(ProcessVersion, ProcessVersion.id == Ticket.process_version_id)
            .where(Ticket.id == ticket_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    ticket, version = row
    return TicketProcessOut(
        ticket_id=ticket.id,
        process_version_id=version.id,
        process_key=version.process_key,
        source_commit_sha=version.source_commit_sha,
        content=version.content,
    )


@router.get("/tickets/{ticket_id}/issue", response_model=TicketIssueOut)
@needs(Permission.ORG_READ)
async def get_ticket_issue(
    ticket_id: int, session: SessionDep, read_issue: IssueReaderDep
) -> TicketIssueOut:
    """The issue this ticket was admitted from, as its ticket source has it right now.

    The description is not in the database and deliberately is not: GitHub owns the issue text and
    the orchestrator observes it (design.md §2 decision 1), so this reads through rather than
    serving a copy that goes stale the moment somebody edits the issue.

    `project.github_repo` is the repo asked, matching `worker.reconcile` — the ticket source's
    `external_ref` selects what to *admit*, but an admitted ticket's issue is observed in the
    project's repo, and the dashboard's issue links are built from the same field.
    """
    row = (
        await session.execute(
            select(Ticket, Project)
            .join(Project, Project.id == Ticket.project_id)
            .where(Ticket.id == ticket_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    ticket, project = row
    source = await session.get(TicketSource, ticket.ticket_source_id)
    kind = source.kind if source is not None else TicketSourceKind.GITHUB

    # Non-numeric keys cannot exist for these sources, but the column is free text and a future
    # source kind may not number its tickets. Say which key could not be read rather than letting
    # `int()` surface as a 500.
    if not ticket.external_key.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Ticket key {ticket.external_key!r} is not an issue number.",
        )

    if kind == TicketSourceKind.GITLAB:
        from orchestrator.gitlab.client import GitLabError
        from orchestrator.gitlab.observer import repo_issue as gitlab_issue
        from orchestrator.gitlab.project_client import client_for_project

        client = await client_for_project(session, project)
        if client is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "GitLab is not configured for this project. Set the GitLab URL and GITLAB_TOKEN.",
            )
        try:
            issue = await gitlab_issue(client, project.github_repo, int(ticket.external_key))
        except GitLabError as exc:
            code = exc.status_code
            raise HTTPException(
                status.HTTP_404_NOT_FOUND if code == 404 else status.HTTP_502_BAD_GATEWAY,
                f"Factorial couldn't read issue {ticket.external_key} from "
                f"{project.github_repo} (HTTP {code or 'unknown'}).",
            ) from exc
    else:
        if read_issue is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, GITHUB_NOT_CONNECTED)
        try:
            issue = await read_issue(project.github_repo, int(ticket.external_key))
        except RequestFailed as exc:
            code = exc.response.status_code
            detail = (
                f"Factorial couldn't read issue {ticket.external_key} from {project.github_repo} "
                f"(HTTP {code})."
            )
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND if code == 404 else status.HTTP_502_BAD_GATEWAY
                ),
                detail=detail,
            ) from exc

    return TicketIssueOut(
        ticket_id=ticket.id,
        repo=project.github_repo,
        number=issue.number,
        title=issue.title,
        body=issue.body,
        state=issue.state,
    )


@router.get("/tickets/{ticket_id}/gate-pull-request", response_model=GatePullRequestOut)
@needs(Permission.ORG_READ)
async def get_ticket_gate_pull_request(
    ticket_id: int,
    session: SessionDep,
    github_for: GitHubForDep,
) -> GatePullRequestOut:
    """The pull/merge request the ticket's gate is watching now, if it is waiting on one.

    Unlike `gate_waiting`'s stored `pr_number`, this is recomputed from the pinned gate's
    head/base globs and the forge listing — the same pick `_gate_step` uses each tick.
    """
    row = (
        await session.execute(
            select(Ticket, Project)
            .join(Project, Project.id == Ticket.project_id)
            .where(Ticket.id == ticket_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    ticket, project = row
    try:
        github = await github_for(project)
        pull_request = await observed_gate_pull_request(
            session, ticket, project, github_client=None if github is None else github.client
        )
    except Exception:
        # The page falls back to the number `gate_waiting` recorded; a forge outage must not
        # turn its poll into a stream of 500s.
        logger.warning("gate pull request lookup failed for ticket %s", ticket.id, exc_info=True)
        pull_request = None
    if pull_request is None:
        return GatePullRequestOut(ticket_id=ticket.id)
    return GatePullRequestOut(
        ticket_id=ticket.id,
        number=pull_request.number,
        url=pull_request.url,
        head_ref=pull_request.head_ref,
        state=pull_request.state,
        merged=pull_request.merged,
    )


@router.get("/tickets/{ticket_id}/timeline", response_model=TimelineResponse)
@needs(Permission.ORG_READ)
async def get_timeline(ticket_id: int, session: SessionDep) -> TimelineResponse:
    """The audit trail for one ticket: every event, in log order."""
    entries = await ticket_timeline(session, ticket_id)
    return TimelineResponse(
        ticket_id=ticket_id,
        entries=[TimelineEntryOut.model_validate(entry) for entry in entries],
    )
