"""Redo (decision 76): a person throws away one step and every step after it, and the ticket
starts that step again from a clean start on its pinned config.

The tick's one transition, answering `redo_requested`. A redo reaches two targets: the step the
ticket is on, and, at a waiting gate, the step that gate reviews. What it discards is the target
and every step its `next` edges reach, never an `onReject` edge, which points back. Their runs end
and are set aside (`StepRun.superseded_at`), so the loop decides without them, while attempt
numbers, answered reviews and spend still count them. Their pull requests close and their branches
go, except what a kept step's templates also name. Nothing un-merges: a merged pull request stays
merged, and `step_redone` names it. Every refusal comes before the first write, so a refused redo
leaves the ticket exactly as it was. `_stop` runs before the GitHub clean-up, in a restart's order,
so a GitHub failure raises and the next tick runs the whole redo again."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep, GateStep, ProcessConfig
from orchestrator.db.models import (
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    TicketStatus,
    current_runs,
)
from orchestrator.events import EventType
from orchestrator.events.previews import pending_change_request
from orchestrator.events.workspaces import WHY_NOT_SERVABLE
from orchestrator.worker.completions import (
    CompletionRefusal,
    pending_completion_request,
    refuse_completion,
)
from orchestrator.worker.exits import pending_request
from orchestrator.worker.footprint import (
    Footprint,
    footprint,
    footprint_lines,
    remove_footprint,
)
from orchestrator.worker.inputs import release_gitlab_run_token
from orchestrator.worker.previews import stop_awake_previews
from orchestrator.worker.restarts import hold_park_label, run_pair
from orchestrator.worker.runtime_adapters import RuntimeRefused
from orchestrator.worker.tick_context import _move_to, _Tick, latest_current_run
from runtime.contract import AgentRuntimeError

__all__ = [
    "REDO_EVENTS",
    "RedoRefusal",
    "discard_set",
    "pending_redo_request",
    "redo",
    "redo_target",
    "refuse_redo",
]

logger = logging.getLogger(__name__)

#: A redo request, then what answers it.
REDO_EVENTS: Final = (
    EventType.REDO_REQUESTED,
    EventType.STEP_REDONE,
    EventType.REDO_REFUSED,
)

#: What a redo writes on every run it ends.
_REDO_CANCEL_REASON: Final = "cancelled: step redone"

#: A run a redo must end before it sets the run aside: live, waiting on a gate, or parked.
_ENDS: Final = frozenset(
    {
        StepRunStatus.RUNNING,
        StepRunStatus.IDLE,
        StepRunStatus.WAITING,
        StepRunStatus.INTERRUPTED,
    }
)


class RedoRefusal(StrEnum):
    ISSUE_CLOSED = "issue_closed"
    EXITED = "exited"
    STALE = "stale"
    CANCEL_FAILED = "cancel_failed"


def discard_set(config: ProcessConfig, target: str) -> tuple[str, ...]:
    """`target` and every step its `next` edges reach, in order."""
    found: list[str] = []
    name = target
    while name != config.terminal.step and name not in found:
        step = config.find_step(name)
        if step is None:
            break
        found.append(name)
        name = step.next
    return tuple(found)


def redo_target(config: ProcessConfig, current_step: str, status: TicketStatus | str) -> str | None:
    """The step a Redo pressed now would start again, or None when Redo is not offered.

    At a waiting gate it is the step the gate reviews: its `onReject.rerunStep`, or else the one
    agent step whose branch template is the gate's head. Either way the gate must be among what
    the redo discards, or it would stay WAITING and current while the ticket moved back."""
    step = config.find_step(current_step)
    if step is None or TicketStatus(status) in (TicketStatus.DONE, TicketStatus.EXITED):
        return None
    if not isinstance(step, GateStep):
        return current_step
    if TicketStatus(status) != TicketStatus.WAITING_GATE:
        return None
    rerun = config.rerun_agent_step(current_step)
    if rerun is not None:
        target = rerun.name
    else:
        head = step.gate.pr.head.replace("{slug}", "*")
        makers = [
            agent.name
            for agent in config.steps
            if isinstance(agent, AgentStep)
            and agent.workspace.branch.replace("{slug}", "*") == head
        ]
        if len(makers) != 1:
            return None
        target = makers[0]
    return target if current_step in discard_set(config, target) else None


async def pending_redo_request(session: AsyncSession, ticket_id: int) -> Event | None:
    """The newest redo request nothing has answered since."""
    return await pending_request(session, ticket_id, *REDO_EVENTS)


async def refuse_redo(tick: _Tick, request: Event, reason: RedoRefusal, detail: str) -> None:
    """Comments on the issue, where a completion or retry refusal does not: a redo is a
    ticket-level act, as a restart is, and a restart's refusal comments too."""
    await tick.emit(
        EventType.REDO_REFUSED,
        step=tick.ticket.current_step,
        attempt=request.details.get("attempt"),
        request_id=request.id,
        target_step=request.details.get("target_step"),
        reason=reason.value,
        detail=detail,
    )
    tick.plan.comments.append(f"Redo refused: {detail}")
    hold_park_label(tick)


async def redo(tick: _Tick, request: Event) -> None:
    """Answer `request`. Past the refusals, a GitHub failure raises and the next tick redoes the
    idempotent steps (decision 31 bounds it)."""
    ticket = tick.ticket
    target = request.details.get("target_step")
    if ticket.status == TicketStatus.EXITED:
        await refuse_redo(
            tick, request, RedoRefusal.EXITED, "the ticket is exited. Re-enter it, then redo."
        )
        return
    if tick.issue.state == "closed":
        await refuse_redo(
            tick, request, RedoRefusal.ISSUE_CLOSED, "the issue is closed. Reopen it, then redo."
        )
        return
    latest = await latest_current_run(tick.session, ticket, ticket.current_step)
    if (
        not isinstance(target, str)
        or request.details.get("generation") != ticket.generation
        or request.details.get("step") != ticket.current_step
        or request.details.get("attempt") != (latest.attempt if latest is not None else None)
        or redo_target(tick.config, ticket.current_step, ticket.status) != target
    ):
        await refuse_redo(
            tick,
            request,
            RedoRefusal.STALE,
            "the ticket moved on after the redo was asked. Look again, then redo.",
        )
        return
    steps = discard_set(tick.config, target)
    branches, pulls = await asyncio.gather(tick.source.branches(), tick.source.pull_requests())
    plan = footprint(tick.config, tick.values, branches, pulls, steps=steps)
    if (plan.close or plan.branches) and (
        tick.github is None or tick.github.contract_github is None
    ):
        raise RuntimeError(
            "a redo closes pull requests and deletes branches, and this project has no "
            "contract_github wired"
        )
    runs = list(
        (
            await tick.session.scalars(
                select(StepRun)
                .where(
                    StepRun.ticket_id == ticket.id,
                    StepRun.step_name.in_(steps),
                    current_runs(ticket.generation),
                )
                .order_by(StepRun.id)
            )
        ).all()
    )
    ending = [run for run in runs if StepRunStatus(run.status) in _ENDS]
    unstoppable = await _stop(tick, ending)
    if unstoppable is not None:
        run, _ = unstoppable
        await refuse_redo(
            tick,
            request,
            RedoRefusal.CANCEL_FAILED,
            f"the running agent (`{run.runtime_run_id}`) couldn't be stopped. Exit the ticket, "
            "which works even then, and redo it.",
        )
        return

    ended = [(run, StepRunStatus(run.status)) for run in ending]
    for run, _ in ended:
        run.status = StepRunStatus.CANCELLED
        run.ended_at = run.ended_at or tick.now
        run.failure_reason = _REDO_CANCEL_REASON
        await release_gitlab_run_token(tick, run.id)
    await _answer_overtaken(tick, ended)
    await stop_awake_previews(
        tick.session,
        ticket,
        tick.deps,
        reason=WHY_NOT_SERVABLE["redone"],
        runs={run.id for run in runs},
    )
    await _clean_up(tick, target, plan)
    for run in runs:
        run.superseded_at = tick.now

    cancelled = next((run for run, was in ended if was != StepRunStatus.INTERRUPTED), None)
    parked = next((run for run, was in ended if was == StepRunStatus.INTERRUPTED), None)
    note = request.details.get("note")
    from_step = ticket.current_step
    # Before `step_entered`, as a restart orders `ticket_restarted`.
    await tick.emit(
        EventType.STEP_REDONE,
        step=target,
        attempt=None,
        request_id=request.id,
        requested_by=request.details.get("requested_by"),
        requested_via=request.details.get("requested_via"),
        target_step=target,
        from_step=from_step,
        generation=ticket.generation,
        discarded_steps=list(steps),
        superseded_runs=[run_pair(run) for run in runs],
        cancelled=run_pair(cancelled),
        ended_parked=run_pair(parked),
        closed_prs=[pr.number for pr in plan.close],
        deleted_branches=list(plan.branches),
        kept_branches=list(plan.kept),
        merged_prs_left=[pr.number for pr in plan.merged],
        has_note=isinstance(note, str) and bool(note.strip()),
    )
    await _move_to(tick, target, from_step=from_step, reason="redo")
    tick.plan.comments.append(_comment(target, plan, cancelled, parked))


async def _stop(tick: _Tick, runs: Sequence[StepRun]) -> tuple[StepRun, str] | None:
    """Stop each agent run on its substrate: finish a parked session, which on a host also
    releases the branch a fresh attempt checks out, and cancel a live one. A gate's or a check's
    wait has nothing to stop. The first run the substrate could not stop, with its error."""
    for run in runs:
        if run.kind != StepKind.AGENT or run.runtime_run_id is None:
            continue
        try:
            adapter = await tick.deps.adapter_for_run(tick.session, run)
            if StepRunStatus(run.status) == StepRunStatus.INTERRUPTED:
                await adapter.finish(run.runtime_run_id)
            else:
                await adapter.cancel(run.runtime_run_id)
        except (AgentRuntimeError, RuntimeRefused) as exc:
            logger.exception(
                "ticket %s: a redo could not stop run %s",
                tick.ticket.external_key,
                run.runtime_run_id,
            )
            return run, str(exc)
    return None


async def _answer_overtaken(tick: _Tick, ended: Sequence[tuple[StepRun, StepRunStatus]]) -> None:
    """Answer the requests that name a run the redo ends, which their own answers match by
    `request_id`, so `step_redone` alone would leave them reading `pending` for ever. A Resume,
    an un-block or a step retry is answered by `step_redone` itself."""
    for run, was in ended:
        if run.kind != StepKind.HUMAN_GATE or was != StepRunStatus.WAITING:
            continue
        while asked := await pending_change_request(tick.session, tick.ticket.id, run.id):
            await tick.emit(
                EventType.STEP_CHANGES_REFUSED,
                run=run,
                step=run.step_name,
                attempt=run.attempt,
                request_id=asked.id,
                reason="redone",
            )
    completion = await pending_completion_request(tick.session, tick.ticket.id)
    if completion is not None:
        await refuse_completion(tick, completion, CompletionRefusal.REDONE, "the step was redone")


async def _clean_up(tick: _Tick, target: str, plan: Footprint) -> None:
    if not (plan.close or plan.branches):
        return
    await remove_footprint(
        tick,
        plan,
        f"Closed by a redo of `{target}` on #{tick.ticket.external_key}; `{target}` runs again "
        "from scratch.",
    )


def _comment(
    target: str, plan: Footprint, cancelled: StepRun | None, parked: StepRun | None
) -> str:
    """Never the note: it is the person's words to the agent, and an input carries them."""
    lines = [f"Redid `{target}` from a clean start, with every step after it."]
    if cancelled is not None:
        lines.append(f"- Canceled `{cancelled.step_name}` attempt {cancelled.attempt}.")
    if parked is not None:
        lines.append(
            f"- Ended the paused session of `{parked.step_name}` attempt {parked.attempt}."
        )
    lines.extend(footprint_lines(plan))
    lines.extend(f"- Kept `{name}`: an earlier step uses it." for name in plan.kept)
    lines.extend(
        f"- Left merged #{pr.number}: nothing un-merges, and a later check can still see it."
        for pr in plan.merged
    )
    return "\n".join(lines)
