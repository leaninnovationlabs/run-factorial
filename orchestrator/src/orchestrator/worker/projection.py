"""Projection (design.md §3.1): the ticket state a tick writes back to the issue as labels,
assignees and comments, and the issue title it keeps."""

from __future__ import annotations

from orchestrator.config import GateStep, expand
from orchestrator.db.models import Ticket, TicketStatus
from orchestrator.github import (
    BLOCKED_LABELS,
    DONE_LABELS,
    EXIT_LABELS,
    INTERRUPTED_LABELS,
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_EXIT,
    WAITING_LABELS,
    desired_status_labels,
    step_from_label,
    step_label,
)
from orchestrator.github.state import IssueState
from orchestrator.worker.tick_context import _Tick


def _observe_title(ticket: Ticket, issue: IssueState) -> None:
    """Record the issue title the tick observed — the one copy of issue text the orchestrator keeps.

    Called at the tick's exits rather than beside the `source.issue()` read that supplies it, and
    that ordering is load-bearing. Dirtying the `ticket` row earlier makes SQLAlchemy autoflush an
    `UPDATE ticket` before the act phase, which takes a row lock and turns two concurrent ticks
    into a queue instead of a race. The `step_run` unique constraint would then never arbitrate:
    the second tick would wait, observe the first attempt as finished, and run the step AGAIN — an
    action's GitHub mutations firing twice. Written last, both ticks still collide on the insert
    and the loser rolls its SAVEPOINT back.

    Assigned only on a change, so a steady tick issues no UPDATE at all. The
    `_EvaluationUnavailable` path is the other reason this is late: its `session.refresh(ticket)`
    would discard an assignment made at the top of the tick.
    """
    if ticket.title != issue.title:
        ticket.title = issue.title


async def _project(tick: _Tick) -> None:
    """Write the ticket's state back to GitHub — and only what actually differs.

    GitHub is a projection of state the orchestrator owns (design.md §2.2). Labels are computed as
    a whole desired set so human-owned labels survive, and compared against what was
    observed so an unchanged tick issues no calls at all.
    """
    desired = _desired_labels(tick)
    write_labels = desired != frozenset(tick.issue.labels)
    write_assignees = tick.plan.assign is not None and sorted(tick.plan.assign) != sorted(
        tick.issue.assignees
    )
    # Only close an issue GitHub still reports as open, so a human reopening a done ticket is
    # not fought every tick (design.md §2.2: a human edit is observed input, not a command).
    write_close = tick.plan.close_issue and tick.issue.state == "open"
    write_comments = bool(tick.plan.comments or tick.plan.pr_comments)
    if not (write_labels or write_assignees or write_close or write_comments):
        return

    projector = tick.projector()
    number = tick.issue.number
    if write_labels:
        await projector.set_labels(number, sorted(desired))
    if write_assignees and tick.plan.assign is not None:
        await projector.set_assignees(number, tick.plan.assign)
    for body in tick.plan.comments:
        await projector.comment(number, body)
    # GitHub's issue-comment endpoint takes a pull request's number as well.
    for pr_number, body in tick.plan.pr_comments:
        await projector.comment(pr_number, body)
    if write_close:
        # Last: the comment and the `factorial:done` label must land on the issue before it closes.
        await projector.close_issue(number)


def _desired_labels(tick: _Tick) -> frozenset[str]:
    label = tick.plan.label or _steady_label(tick)
    # Membership, not equality: a pinned config still spells its status labels the old way, and
    # `step_from_label` would otherwise read `factorial:done` as a step named "done" — which
    # `step_label` refuses as reserved, turning a routine tick into a raise.
    if label in DONE_LABELS:
        return desired_status_labels(tick.issue.labels, done=True)
    if label in BLOCKED_LABELS:
        return desired_status_labels(tick.issue.labels, step=_status_step(tick), blocked=True)
    if label in INTERRUPTED_LABELS:
        # Beside the step label, like `blocked` — so a parked ticket still says WHERE it stopped.
        return desired_status_labels(tick.issue.labels, step=_status_step(tick), interrupted=True)
    if label in WAITING_LABELS:
        return desired_status_labels(tick.issue.labels, step=_status_step(tick), waiting=True)
    if label in EXIT_LABELS:
        return desired_status_labels(tick.issue.labels, step=_status_step(tick), exited=True)
    step = step_from_label(label)
    if step is not None:
        return desired_status_labels(tick.issue.labels, step=step)
    # An outcome may name a label outside the factory's namespace; keep it, and keep the status
    # label the state implies alongside it.
    return desired_status_labels(tick.issue.labels, step=_status_step(tick)) | {label}


def _steady_label(tick: _Tick) -> str:
    """The label the ticket's *state* implies, when no outcome fired this tick."""
    if tick.ticket.status == TicketStatus.EXITED:
        return LABEL_EXIT
    if tick.ticket.status == TicketStatus.DONE or tick.ticket.current_step == (
        tick.config.terminal.step
    ):
        return LABEL_DONE
    if tick.ticket.status == TicketStatus.BLOCKED:
        return LABEL_BLOCKED
    step = tick.config.step(tick.ticket.current_step)
    if isinstance(step, GateStep) and step.gate.waiting is not None:
        waiting = step.gate.waiting.ticket_label
        if waiting is not None:
            return expand(waiting, tick.values)
    return step_label(step.name)


def _status_step(tick: _Tick) -> str | None:
    """The `factorial:<step>` part to keep alongside `factorial:blocked`."""
    if tick.ticket.current_step == tick.config.terminal.step:
        return None
    return tick.ticket.current_step
