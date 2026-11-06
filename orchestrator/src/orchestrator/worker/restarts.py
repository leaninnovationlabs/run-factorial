"""Restart (tech-stack.md decision 67): put a ticket on its lane's current snapshot and start it
fresh at that process's entry step.

The tick's one transition, answering `restart_requested`. Fresh means nothing from before reaches
the new generation: live work is cancelled, previews are stopped, the ticket's pull requests are
closed and its branches deleted, and from then on the loop decides with the new generation's runs
alone (`tick_context._runs_for`). The log keeps everything."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, ProcessVersion, StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType
from orchestrator.events.workspaces import WHY_NOT_SERVABLE
from orchestrator.github import (
    INTERRUPTED_LABELS,
    LABEL_INTERRUPTED,
    LABEL_WAITING,
    WAITING_LABELS,
)
from orchestrator.logs import note
from orchestrator.worker.exits import LiveRunStop, cancel_live_run, pending_request
from orchestrator.worker.footprint import (
    Footprint,
    footprint,
    footprint_lines,
    remove_footprint,
)
from orchestrator.worker.previews import stop_awake_previews
from orchestrator.worker.tick_context import _enter_step, _load_pinned_config, _Tick

__all__ = [
    "RESTART_CANCEL_REASON",
    "RESTART_EVENTS",
    "RestartRefusal",
    "hold_park_label",
    "pending_restart_request",
    "refuse_restart",
    "restart",
    "run_pair",
]

#: A restart request, then what answers it.
RESTART_EVENTS: Final = (
    EventType.RESTART_REQUESTED,
    EventType.TICKET_RESTARTED,
    EventType.RESTART_REFUSED,
)

#: What a restart writes on the run it stops.
RESTART_CANCEL_REASON: Final = "cancelled: ticket restarted"


class RestartRefusal(StrEnum):
    ISSUE_CLOSED = "issue_closed"
    LANE_RETIRED = "lane_retired"
    MERGED_OUTSIDE = "merged_outside"
    CANCEL_FAILED = "cancel_failed"
    EXITED = "exited"


async def pending_restart_request(session: AsyncSession, ticket_id: int) -> Event | None:
    """The newest restart request nothing has answered since."""
    return await pending_request(session, ticket_id, *RESTART_EVENTS)


async def refuse_restart(
    tick: _Tick, request: Event, reason: RestartRefusal, detail: str
) -> None:
    await tick.emit(
        EventType.RESTART_REFUSED,
        step=tick.ticket.current_step,
        attempt=request.details.get("attempt"),
        request_id=request.id,
        reason=reason.value,
        detail=detail,
    )
    tick.plan.comments.append(f"Restart refused: {detail}")
    hold_park_label(tick)


def hold_park_label(tick: _Tick) -> None:
    """For a refusal: nothing changed, so the park the issue shows stands. `_steady_label` would
    put `factorial:blocked` over an interrupted or waiting session."""
    if tick.ticket.status != TicketStatus.BLOCKED:
        return
    if tick.issue.labels & INTERRUPTED_LABELS:
        tick.plan.label = LABEL_INTERRUPTED
    elif tick.issue.labels & WAITING_LABELS:
        tick.plan.label = LABEL_WAITING


async def restart(tick: _Tick, request: Event) -> None:
    """Answer `request`. Every refusal comes before the first write, so a refused restart leaves
    the ticket exactly as it was; past them, a GitHub failure raises and the next tick redoes the
    idempotent steps (decision 31 bounds it)."""
    target = await tick.session.get(
        ProcessVersion, int(request.details["target_process_version_id"])
    )
    if tick.issue.state == "closed":
        await refuse_restart(
            tick,
            request,
            RestartRefusal.ISSUE_CLOSED,
            "the issue is closed. Reopen it, then restart.",
        )
        return
    if (
        target is None
        or target.retired_at is not None
        or target.source_repo != tick.project.github_repo
    ):
        await refuse_restart(
            tick,
            request,
            RestartRefusal.LANE_RETIRED,
            "the process version it asked for is no longer available. Pick a current one and "
            "restart again.",
        )
        return
    plan = footprint(
        tick.config,
        tick.values,
        await tick.source.branches(),
        await tick.source.pull_requests(),
    )
    # A merge is all this sees, never what `main` holds now (the change may since be reverted),
    # so whether the merged work still matters is the asker's call, made explicitly.
    if plan.merged_outside and not request.details.get("allow_merged"):
        merged = ", ".join(f"#{pr.number} into `{pr.base_ref}`" for pr in plan.merged_outside)
        await refuse_restart(
            tick,
            request,
            RestartRefusal.MERGED_OUTSIDE,
            f"this ticket's work is already merged ({merged}), and a fresh start may repeat it. "
            "Restart again and confirm the merged work to go ahead.",
        )
        return
    stop = await cancel_live_run(tick, reason=RESTART_CANCEL_REASON, force=False)
    if stop.unstoppable is not None and stop.latest is not None:
        await refuse_restart(
            tick,
            request,
            RestartRefusal.CANCEL_FAILED,
            f"the running agent (`{stop.latest.runtime_run_id}`) couldn't be stopped. Exit the "
            "ticket, which works even then, and restart it.",
        )
        return
    abandoned = (
        stop.latest
        if stop.latest is not None
        and StepRunStatus(stop.latest.status) == StepRunStatus.INTERRUPTED
        else None
    )
    await stop_awake_previews(
        tick.session, tick.ticket, tick.deps, reason=WHY_NOT_SERVABLE["restarted"]
    )
    await _clean_up(tick, plan, target)
    await _start_fresh(tick, request, target, plan, stop, abandoned)


async def _clean_up(tick: _Tick, plan: Footprint, target: ProcessVersion) -> None:
    tick.host().forge.contract(tick.project.github_repo)
    await remove_footprint(
        tick,
        plan,
        f"Closed by a restart of #{tick.ticket.external_key} on its process at "
        f"`{target.source_commit_sha[:7]}`. The ticket starts again from scratch.",
    )


async def _start_fresh(
    tick: _Tick,
    request: Event,
    target: ProcessVersion,
    plan: Footprint,
    stop: LiveRunStop,
    abandoned: StepRun | None,
) -> None:
    ticket = tick.ticket
    from_id, from_sha, from_step = ticket.process_version_id, tick.config_sha, ticket.current_step
    config, pins, sha, repo = await _load_pinned_config(tick.session, target.id)
    ticket.generation += 1
    ticket.process_version_id = target.id
    ticket.current_step = config.entry.step
    note(
        step=ticket.current_step, process_version_id=target.id, step_run_id=None, attempt=None
    )
    if tick.issue.assignees:
        ticket.owner_at_admission = tick.issue.assignees[0]
    ticket.status = TicketStatus.RUNNING
    ticket.closed_at = None
    # `_project` reads the step label off `tick.config`, and the new entry step may not exist in
    # the old snapshot.
    tick.config, tick.agent_pins, tick.config_sha, tick.config_repo = config, pins, sha, repo
    # Before `step_entered`, as admission orders `ticket_admitted`: the timeline files the restart
    # block ahead of the new generation's first one.
    await tick.emit(
        EventType.TICKET_RESTARTED,
        step=config.entry.step,
        attempt=None,
        request_id=request.id,
        requested_by=request.details.get("requested_by"),
        requested_via=request.details.get("requested_via"),
        process=target.process_key,
        from_process_version_id=from_id,
        from_sha=from_sha,
        to_process_version_id=target.id,
        to_sha=sha,
        generation=ticket.generation,
        entry_step=config.entry.step,
        cancelled=run_pair(stop.cancelled),
        abandoned=run_pair(abandoned),
        closed_prs=[pr.number for pr in plan.close],
        deleted_branches=list(plan.branches),
        owner=ticket.owner_at_admission,
    )
    await _enter_step(
        tick.session, ticket, config, step=config.entry.step, from_step=from_step, reason="restart"
    )
    tick.plan.comments.append(_comment(target, from_sha, config.entry.step, plan, stop, abandoned))


def run_pair(run: StepRun | None) -> dict[str, object] | None:
    return None if run is None else {"step": run.step_name, "attempt": run.attempt}


def _comment(
    target: ProcessVersion,
    from_sha: str,
    entry: str,
    plan: Footprint,
    stop: LiveRunStop,
    abandoned: StepRun | None,
) -> str:
    lines = [
        f"Restarted on process `{target.process_key}` at `{target.source_commit_sha[:7]}` "
        f"(was `{from_sha[:7]}`), from `{entry}`. Nothing from before the restart is reused."
    ]
    if stop.cancelled is not None:
        lines.append(
            f"- Canceled `{stop.cancelled.step_name}` attempt {stop.cancelled.attempt}."
        )
    if abandoned is not None:
        lines.append(
            f"- Abandoned the interrupted session of `{abandoned.step_name}` attempt "
            f"{abandoned.attempt}."
        )
    lines.extend(footprint_lines(plan))
    return "\n".join(lines)
