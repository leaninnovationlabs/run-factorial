"""Restarting a ticket from scratch on its lane's current process (#321).

Transport-neutral, like `services.tickets`: the dashboard's routes and the MCP tool both call
these, so a refusal reads the same on either surface. The request appends `restart_requested`,
commits, and waits for the pass it wakes (`api.wakes`); on that pass the worker cancels the
ticket's live work, closes its pull requests, deletes its branches and enters the new process's
entry step (`worker.restarts`). The request answers from Postgres.

The preview reads the repo's branches and pull requests live, through the observer a worker tick
builds, and is advisory: the worker recomputes the footprint from its own observation when it
answers.
"""

from __future__ import annotations

from typing import Final

from githubkit.exception import RequestFailed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import RepoReader
from orchestrator.api.schemas import (
    PinOut,
    RestartBlockerOut,
    RestartPreviewOut,
    RestartPrOut,
    RestartRunOut,
)
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict, Invalid, NotFound, UpstreamFailed
from orchestrator.api.services.refs import current_run
from orchestrator.api.services.results import RequestResult
from orchestrator.api.services.tickets import (
    _latest_attempt,
    nothing_to,
    refuse_while_quarantined,
)
from orchestrator.api.wakes import wake_and_wait
from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    ProcessVersion,
    Project,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.factorial.registry import lane_head
from orchestrator.github import IssueState, PullRequestState
from orchestrator.worker.footprint import footprint
from orchestrator.worker.restarts import pending_restart_request

__all__ = ["pin_of", "request_restart", "restart_preview", "restart_target"]

#: The run a restart cancels: the latest at the current step while it is one of these, as an
#: exit's (`worker.exits`).
_LIVE: Final = frozenset({StepRunStatus.RUNNING, StepRunStatus.IDLE, StepRunStatus.WAITING})


def pin_of(version: ProcessVersion) -> PinOut:
    return PinOut(process_version_id=version.id, sha=version.source_commit_sha)


async def _pinned(session: AsyncSession, ticket: Ticket) -> ProcessVersion:
    return (
        await session.execute(
            select(ProcessVersion).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()


async def restart_target(
    session: AsyncSession,
    ticket: Ticket,
    *,
    process_version_id: int | None,
    allow_current_pin: bool,
) -> ProcessVersion:
    """The snapshot a restart of `ticket` lands on: its lane's head.

    Refused for a done ticket, a quarantined one (nothing leases it to read the request), a
    retired lane, and a `process_version_id` the lane has moved past: the caller decided on a
    snapshot that is no longer the head. A ticket already on the head is refused unless
    `allow_current_pin`, for every caller: the dashboard never sends it, so restarting onto the
    config a ticket already runs is always asked for by name. On a ticket that is behind it
    changes nothing.
    """
    if ticket.status == TicketStatus.DONE:
        raise nothing_to(ticket, "restart")
    refuse_while_quarantined(ticket, "restart")
    pinned = await _pinned(session, ticket)
    head = await lane_head(session, ticket.project_id, pinned.process_key)
    if head is None:
        raise Conflict(
            f"The {pinned.process_key} process was removed, so there is no current version to "
            "restart this ticket on.",
            hint=f"Restore .factorial/processes/process-{pinned.process_key}.yaml and sync the "
            "project.",
            code="process_retired",
        )
    if process_version_id is not None and process_version_id != head.id:
        raise Conflict(
            f"The {pinned.process_key} process has changed since you looked: its current "
            f"version is {head.source_commit_sha[:7]}. Reload, then restart onto that version.",
            code="process_moved",
            tool_hint=f"Pass process_version_id={head.id}, or omit it.",
        )
    if head.id == pinned.id and not allow_current_pin:
        # No person's hint: the dashboard has no restart onto the same version to offer.
        raise Conflict(
            f"This ticket already runs {head.source_commit_sha[:7]}, the current version of the "
            f"{pinned.process_key} process.",
            code="already_current",
            tool_hint="allow_current_pin=true",
        )
    return head


async def request_restart(
    session: AsyncSession,
    ticket: Ticket,
    actor: Actor,
    *,
    process_version_id: int | None,
    allow_current_pin: bool,
    allow_merged: bool = False,
) -> RequestResult:
    """Ask the loop to restart this ticket from scratch on its lane's head (`restart_target`).

    The request names the snapshot it restarts onto. While one request is unanswered another
    records nothing, and `status` is then `pending`. The worker may still refuse an accepted
    request: an exit on the same pass wins, and it checks the issue, the lane and the ticket's
    pull requests again from what it observes (`worker.restarts`). `allow_merged` is recorded,
    not checked: GitHub is the worker's to read, and it refuses merged work without it.
    """
    target = await restart_target(
        session, ticket, process_version_id=process_version_id, allow_current_pin=allow_current_pin
    )
    if await pending_restart_request(session, ticket.id) is not None:
        landed = await wake_and_wait(session, ticket)
        return RequestResult(ticket_id=ticket.id, action="restart", status="pending", landed=landed)
    await append(
        session,
        type=EventType.RESTART_REQUESTED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=None,
        **actor.fields(),
        target_process_version_id=target.id,
        target_sha=target.source_commit_sha,
        allow_current_pin=allow_current_pin,
        allow_merged=allow_merged,
        step=ticket.current_step,
        attempt=await _latest_attempt(session, ticket),
    )
    await session.commit()
    landed = await wake_and_wait(session, ticket)
    return RequestResult(ticket_id=ticket.id, action="restart", status="requested", landed=landed)


async def restart_preview(
    session: AsyncSession, ticket: Ticket, reader: RepoReader
) -> RestartPreviewOut:
    """What restarting this ticket now would cancel, abandon, close and delete, and what refuses
    it. Every blocker is a refusal of the request (`restart_target`) or of the worker's answer,
    named before anyone confirms.

    The footprint is `worker.footprint`'s, over the PINNED config and the repo as `reader` sees
    it now: the branches the old process's `{ticket}` templates match, and every branch the
    ticket recorded, with the pull requests whose head is one of them.
    """
    pinned = await _pinned(session, ticket)
    head = await lane_head(session, ticket.project_id, pinned.process_key)
    repo = (
        await session.execute(select(Project.github_repo).where(Project.id == ticket.project_id))
    ).scalar_one()
    if not ticket.external_key.isdigit():
        raise Invalid(f"Ticket key {ticket.external_key!r} is not a GitHub issue number.")
    observed = reader(repo, int(ticket.external_key))
    try:
        issue = await observed.issue()
        branches = await observed.branches()
        pulls = await observed.pull_requests()
    except RequestFailed as exc:
        code = exc.response.status_code
        message = f"Factorial couldn't read {repo}#{ticket.external_key} from GitHub (HTTP {code})."
        if code == 404:
            raise NotFound(message) from exc
        raise UpstreamFailed(message) from exc
    found = footprint(
        ProcessConfig.model_validate(pinned.content),
        {"ticket": ticket.external_key, "ticket_owner": ticket.owner_at_admission, "repo": repo},
        branches,
        pulls,
    )
    run = await current_run(session, ticket)
    return RestartPreviewOut(
        current=pin_of(pinned),
        target=None if head is None else pin_of(head),
        current_pin=head is not None and head.id == pinned.id,
        live_run=_run_out(run) if run is not None and run.status in _LIVE else None,
        parked_run=(
            _run_out(run) if run is not None and run.status == StepRunStatus.INTERRUPTED else None
        ),
        close_prs=[_pr_out(pr) for pr in found.close],
        delete_branches=list(found.branches),
        merged_outside=[_pr_out(pr) for pr in found.merged_outside],
        blockers=_blockers(ticket, pinned, head, issue),
    )


def _run_out(run: StepRun) -> RestartRunOut:
    return RestartRunOut(step=run.step_name, attempt=run.attempt, kind=run.kind)


def _pr_out(pr: PullRequestState) -> RestartPrOut:
    return RestartPrOut(number=pr.number, url=pr.url, head=pr.head_ref, base=pr.base_ref)


def _blockers(
    ticket: Ticket,
    pinned: ProcessVersion,
    head: ProcessVersion | None,
    issue: IssueState,
) -> list[RestartBlockerOut]:
    blockers: list[RestartBlockerOut] = []
    if ticket.status == TicketStatus.DONE:
        blockers.append(RestartBlockerOut(reason="done", detail="This ticket is done."))
    if ticket.quarantined_at is not None:
        blockers.append(
            RestartBlockerOut(
                reason="quarantined",
                detail="This ticket is stopped after repeated errors. Requeue it first.",
            )
        )
    if head is None:
        blockers.append(
            RestartBlockerOut(
                reason="lane_retired", detail=f"The {pinned.process_key} process was removed."
            )
        )
    if issue.state == "closed":
        blockers.append(
            RestartBlockerOut(
                reason="issue_closed", detail=f"Issue #{issue.number} is closed. Reopen it first."
            )
        )
    return blockers
