"""A merging gate's conflict arm (`handle_conflict`): whether the gate's pull request can merge,
and sending the work back to an agent step to fix it when it cannot.

A send-back writes `gate_conflict_sent_back`, never `gate_rejected`: it is not a reviewer's
rejection, and the rework window, the answered reviews and the gate metrics read only the latter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep, GateStep
from orchestrator.db.models import Event, StepRun, StepRunStatus
from orchestrator.events import EventType
from orchestrator.github import PullRequestState
from orchestrator.github.state import Mergeability
from orchestrator.worker.templates import _pr_values
from orchestrator.worker.tick_context import _apply_outcome, _move_to, _observed, _runs_for, _Tick

#: Automatic send-backs in a row at one gate before it waits for a person. The button has none.
AUTOMATIC_FIX_LIMIT: Final = 3

_STUCK: Final = frozenset({Mergeability.CONFLICTING, Mergeability.BEHIND})

_REQUEST_EVENTS: Final = (
    EventType.CONFLICT_FIX_REQUESTED,
    EventType.CONFLICT_FIX_REFUSED,
    EventType.GATE_CONFLICT_SENT_BACK,
)


class ConflictRefusal(StrEnum):
    """`conflict_fix_refused.reason`."""

    NO_FIX_STEP = "no_fix_step"
    #: No open pull request: none matches, or the one that does closed without a merge.
    NO_PULL_REQUEST = "no_pull_request"
    ALREADY_MERGED = "already_merged"
    MERGEABLE = "mergeable"
    #: The request names a gate run that a newer run of the gate has replaced.
    NEWER_ATTEMPT = "newer_attempt"
    #: A branch name the fix's instructions cannot hold (`plain_refs`).
    UNSAFE_BRANCH = "unsafe_branch"


#: Git allows `;`, `$`, `&`, a backtick and quotes in a ref, and a ref can start with a dash.
_PLAIN_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+@#=,%-]*")


def is_plain_ref(ref: str) -> bool:
    """Whether a fix run's instructions can hold `ref`: the agent writes it on a command line."""
    return _PLAIN_REF.fullmatch(ref) is not None


def plain_refs(pull_request: PullRequestState) -> bool:
    return is_plain_ref(pull_request.head_ref) and is_plain_ref(pull_request.base_ref)


class _Fix(StrEnum):
    """What the gate does about a conflict this tick, which the conflict comment tells."""

    SENT = "sent"
    NO_STEP = "no_step"
    BUTTON = "button"
    SPENT = "spent"
    #: The forge keeps an approval across a push, so only a person starts a fix.
    HELD = "held"


async def gate_mergeability(
    tick: _Tick, step: GateStep, run: StepRun, pull_request: PullRequestState
) -> Mergeability:
    """The pull request's mergeability, reused for `deps.mergeability_recheck` on the same head.
    UNKNOWN is never kept, so the next tick asks again."""
    seen = tick.deps.mergeability_seen.get(run.id)
    if seen is not None:
        head_sha, state, read_at = seen
        fresh = tick.now < read_at + tick.deps.mergeability_recheck
        if head_sha == pull_request.head_sha and fresh:
            return state
    state = await _observed(
        tick.source.pr_mergeability(pull_request.number),
        what=f"gate {step.name}: mergeability of PR #{pull_request.number}",
    )
    if state == Mergeability.UNKNOWN:
        tick.deps.mergeability_seen.pop(run.id, None)
    else:
        tick.deps.mergeability_seen[run.id] = (pull_request.head_sha, state, tick.now)
    return state


def forget_mergeability(tick: _Tick, run: StepRun) -> None:
    tick.deps.mergeability_seen.pop(run.id, None)


async def _last_reading(session: AsyncSession, run: StepRun) -> tuple[str, Mergeability] | None:
    """The head and state of the run's latest `gate_mergeability`, on any head."""
    row = (
        await session.execute(
            select(Event.details["head_sha"].astext, Event.details["state"].astext)
            .where(
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type == EventType.GATE_MERGEABILITY,
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (row[0], Mergeability(row[1]))


async def last_mergeability(
    session: AsyncSession, run: StepRun, head_sha: str
) -> Mergeability | None:
    """The state the run last recorded for `head_sha`, or None when it recorded none."""
    state = await session.scalar(
        select(Event.details["state"].astext)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            Event.type == EventType.GATE_MERGEABILITY,
            Event.details["head_sha"].astext == head_sha,
        )
        .order_by(Event.id.desc())
        .limit(1)
    )
    return None if state is None else Mergeability(state)


def _unanswered(rows: Sequence[Event]) -> list[Event]:
    answered = {
        row.details.get("request_id")
        for row in rows
        if row.type != EventType.CONFLICT_FIX_REQUESTED
    }
    return [
        row
        for row in rows
        if row.type == EventType.CONFLICT_FIX_REQUESTED and row.id not in answered
    ]


async def pending_conflict_fix(
    session: AsyncSession, ticket_id: int, gate_run_id: int
) -> Event | None:
    """The oldest conflict-fix request against this gate run that no send-back or refusal
    answered."""
    rows = (
        await session.scalars(
            select(Event)
            .where(
                Event.ticket_id == ticket_id,
                Event.step_run_id == gate_run_id,
                Event.type.in_(_REQUEST_EVENTS),
            )
            .order_by(Event.id)
        )
    ).all()
    return next(iter(_unanswered(rows)), None)


async def conflict_fix_by_key(
    session: AsyncSession, ticket_id: int, idempotency_key: str
) -> Event | None:
    return (
        await session.scalars(
            select(Event).where(
                Event.ticket_id == ticket_id,
                Event.type == EventType.CONFLICT_FIX_REQUESTED,
                Event.details["idempotency_key"].as_string() == idempotency_key,
            )
        )
    ).first()


async def conflict_fix_outcome(
    session: AsyncSession, ticket_id: int, request_id: int
) -> tuple[Literal["pending", "accepted", "refused"], str | None]:
    """What became of a request: the send-back that honoured it, the refusal that answered it
    (with its reason), or neither yet."""
    answer = (
        await session.execute(
            select(Event.type, Event.details)
            .where(
                Event.ticket_id == ticket_id,
                Event.type.in_((EventType.GATE_CONFLICT_SENT_BACK, EventType.CONFLICT_FIX_REFUSED)),
                Event.details["request_id"].as_integer() == request_id,
            )
            .limit(1)
        )
    ).first()
    if answer is None:
        return "pending", None
    kind, details = answer
    if kind == EventType.GATE_CONFLICT_SENT_BACK:
        return "accepted", None
    reason = details.get("reason")
    return "refused", reason if isinstance(reason, str) else None


async def _requests_at(tick: _Tick, step: GateStep) -> list[tuple[Event, StepRun]]:
    """Every unanswered request against any run of this gate, every generation's, with its run.
    A request can outlive its run: the gate decided before the loop read it."""
    rows = (
        await tick.session.execute(
            select(Event, StepRun)
            .join(StepRun, StepRun.id == Event.step_run_id)
            .where(
                Event.ticket_id == tick.ticket.id,
                Event.type.in_(_REQUEST_EVENTS),
                StepRun.step_name == step.name,
            )
            .order_by(Event.id)
        )
    ).all()
    runs = {event.id: run for event, run in rows}
    return [(event, runs[event.id]) for event in _unanswered([event for event, _ in rows])]


async def _refuse(
    tick: _Tick, step: GateStep, run: StepRun, request: Event, reason: ConflictRefusal
) -> None:
    await tick.emit(
        EventType.CONFLICT_FIX_REFUSED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        request_id=request.id,
        reason=reason,
    )


async def refuse_pending_as_merged(tick: _Tick, step: GateStep, run: StepRun) -> None:
    """Answer every request still pending on an approved run: the gate never ticks again once it
    advances, so nothing else would."""
    while (request := await pending_conflict_fix(tick.session, tick.ticket.id, run.id)) is not None:
        await _refuse(tick, step, run, request, ConflictRefusal.ALREADY_MERGED)


async def handle_conflict(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState | None,
) -> bool:
    """Read the merging gate's pull request, record and announce a change of state, and send
    the work back when a person asked for it or `onConflict` allows it for a conflict. True when
    the gate was sent back, and `_gate_step` must return.

    UNKNOWN decides nothing: it is not recorded, not announced, and leaves a request pending."""
    request: Event | None = None
    for asked, asked_run in await _requests_at(tick, step):
        if asked_run.id != run.id:
            await _refuse(tick, step, asked_run, asked, ConflictRefusal.NEWER_ATTEMPT)
        elif request is None:
            request = asked

    if pull_request is None or pull_request.state != "open" or pull_request.merged:
        if request is not None:
            merged = pull_request is not None and pull_request.merged
            await _refuse(
                tick,
                step,
                run,
                request,
                ConflictRefusal.ALREADY_MERGED if merged else ConflictRefusal.NO_PULL_REQUEST,
            )
        return False

    fixer = tick.config.conflict_fix_step(step.name)
    if request is not None and fixer is None:
        await _refuse(tick, step, run, request, ConflictRefusal.NO_FIX_STEP)
        request = None
    # Asked here, where the names are first read, so the ticket stays at its gate and a merge by
    # hand clears the state with nothing else to undo.
    if not plain_refs(pull_request):
        if request is not None:
            await _refuse(tick, step, run, request, ConflictRefusal.UNSAFE_BRANCH)
            request = None
        fixer = None
    if request is not None:
        # A person's request gets the forge's answer now, not a reading up to the recheck old.
        forget_mergeability(tick, run)

    state = await gate_mergeability(tick, step, run, pull_request)
    if state == Mergeability.UNKNOWN:
        return False
    changed = await _record(tick, step, run, pull_request, state)
    if state == Mergeability.CLEAN:
        if request is not None:
            await _refuse(tick, step, run, request, ConflictRefusal.MERGEABLE)
        return False

    if request is not None:
        fix = _Fix.SENT
    elif fixer is None:
        fix = _Fix.NO_STEP
    elif step.gate.on_conflict is None or state == Mergeability.BEHIND:
        # A branch only behind its target waits for a person: an automatic start would run an
        # agent on every merge to the target, and discard an approval already given.
        fix = _Fix.BUTTON
    elif not tick.source.approvals_bound_to_head:
        # An approval given while an unattended fix ran would count on its merge.
        fix = _Fix.HELD
    elif await _automatic_streak(tick, step, run) >= AUTOMATIC_FIX_LIMIT:
        fix = _Fix.SPENT
    else:
        fix = _Fix.SENT
    if changed:
        tick.plan.comments.append(_conflict_comment(step, pull_request, state, fixer, fix))
    if fix != _Fix.SENT:
        return False
    assert fixer is not None
    await _send_back_for_conflict(tick, step, run, pull_request, state, fixer, request)
    return True


async def _record(
    tick: _Tick, step: GateStep, run: StepRun, pull_request: PullRequestState, state: Mergeability
) -> bool:
    """Record `state` when it changed, and say whether it is news to announce: a change to
    CONFLICTING or BEHIND, on this head.

    CLEAN is recorded only after a CONFLICTING or BEHIND on any head, so the state clears when a
    person pushes a fix by hand."""
    last = await _last_reading(tick.session, run)
    if state == Mergeability.CLEAN:
        if last is None or last[1] == Mergeability.CLEAN:
            return False
    elif last == (pull_request.head_sha, state):
        return False
    await tick.emit(
        EventType.GATE_MERGEABILITY,
        run=run,
        step=step.name,
        attempt=run.attempt,
        pr_number=pull_request.number,
        head_sha=pull_request.head_sha,
        base_ref=pull_request.base_ref,
        state=state,
    )
    return state in _STUCK


async def _automatic_streak(tick: _Tick, step: GateStep, run: StepRun) -> int:
    """How many of the gate's current runs before `run`, newest first, ended in a send-back the
    loop started, up to the first that ended any other way."""
    earlier = [r for r in await _runs_for(tick.session, tick.ticket, step.name) if r.id != run.id]
    if not earlier:
        return 0
    rows = await tick.session.execute(
        select(Event.step_run_id, Event.details).where(
            Event.ticket_id == tick.ticket.id,
            Event.step_run_id.in_([r.id for r in earlier]),
            Event.type == EventType.GATE_CONFLICT_SENT_BACK,
        )
    )
    automatic = {run_id for run_id, details in rows if details.get("request_id") is None}
    streak = 0
    for ended in reversed(earlier):
        if ended.id not in automatic:
            break
        streak += 1
    return streak


async def _send_back_for_conflict(
    tick: _Tick,
    step: GateStep,
    run: StepRun,
    pull_request: PullRequestState,
    state: Mergeability,
    fixer: AgentStep,
    request: Event | None,
) -> None:
    run.status = StepRunStatus.REJECTED
    run.ended_at = tick.now
    await tick.emit(
        EventType.GATE_CONFLICT_SENT_BACK,
        run=run,
        step=step.name,
        attempt=run.attempt,
        pr_number=pull_request.number,
        head_ref=pull_request.head_ref,
        base_ref=pull_request.base_ref,
        head_sha=pull_request.head_sha,
        state=state,
        target_step=fixer.name,
        request_id=None if request is None else request.id,
        requested_by=None if request is None else request.details.get("requested_by"),
    )
    forget_mergeability(tick, run)
    _apply_outcome(tick, step.gate.on_conflict, extra=await _pr_values(tick, None, pull_request))
    await _move_to(tick, fixer.name, from_step=step.name, reason="conflict_fix")


async def was_conflict_send_back(session: AsyncSession, run: StepRun) -> bool:
    """`run` was REJECTED by a conflict send-back, not by a reviewer's `gate_rejected`."""
    latest = await session.scalar(
        select(Event.type)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            Event.type.in_((EventType.GATE_REJECTED, EventType.GATE_CONFLICT_SENT_BACK)),
        )
        .order_by(Event.id.desc())
        .limit(1)
    )
    return latest == EventType.GATE_CONFLICT_SENT_BACK


def _state_words(pull_request: PullRequestState, state: Mergeability) -> str:
    if state == Mergeability.CONFLICTING:
        return f"{pull_request.url} conflicts with `{pull_request.base_ref}`"
    return (
        f"{pull_request.url} is behind `{pull_request.base_ref}`, and the repository merges only "
        "a branch that is up to date"
    )


def _how_to_fix(
    pull_request: PullRequestState, state: Mergeability, fixer: AgentStep | None
) -> str:
    by_hand = f"merge `{pull_request.base_ref}` into `{pull_request.head_ref}` by hand"
    if not plain_refs(pull_request):
        return (
            "An agent cannot fix a branch whose name has characters a command line acts on, so "
            f"{by_hand}."
        )
    if fixer is None:
        return f"This lane has no step to send the work to, so {by_hand}."
    button = "Fix conflicts" if state == Mergeability.CONFLICTING else "Update branch"
    return f"Press {button} on the ticket page to send it to `{fixer.name}`, or {by_hand}."


def _conflict_comment(
    step: GateStep,
    pull_request: PullRequestState,
    state: Mergeability,
    fixer: AgentStep | None,
    fix: _Fix,
) -> str:
    said = _state_words(pull_request, state)
    if fix == _Fix.SENT:
        assert fixer is not None
        return (
            f"{said}. `{step.name}` sends it to `{fixer.name}` to merge "
            f"`{pull_request.base_ref}` into `{pull_request.head_ref}`, then waits for an "
            "approval of the new head."
        )
    how = _how_to_fix(pull_request, state, fixer)
    if fix == _Fix.SPENT:
        assert fixer is not None
        return (
            f"{said}. `{fixer.name}` fixed conflicts at `{step.name}` {AUTOMATIC_FIX_LIMIT} "
            f"times in a row, so `{step.name}` now waits for a person. {how}"
        )
    if fix == _Fix.HELD:
        return (
            f"{said}. An approval can still count after a push on this forge, so the fix does not "
            f"start by itself. {how}"
        )
    return f"{said}. {how}"


def refused_merge_note(
    tick: _Tick, step: GateStep, pull_request: PullRequestState, state: Mergeability | None
) -> str | None:
    """What a refused merge's comment says after the forge's message when the head was last read
    as conflicting or behind, which no retry of the merge clears. None otherwise."""
    if state is None or state not in _STUCK:
        return None
    fixer = tick.config.conflict_fix_step(step.name)
    return f"{_state_words(pull_request, state)}. {_how_to_fix(pull_request, state, fixer)}"

