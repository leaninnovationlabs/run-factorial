"""A session's life on the worker (decision 56): hooks signal, predicates decide.

An IDLE session is alive at its prompt. Its postconditions decide what happens next, and a rework's
include that the pull request its gate rejected has moved, or the page a change request from its
preview was about (`postconditions_met`): met, and no writer input for `terminal.finish_grace`, the
worker calls `finish()` once and accepts the run as any other; unmet, the run stays IDLE,
`agent_idle` is appended once per spell and, when the stop will park (below), the ticket shows
`factorial:waiting`. Outside a rework a step with no postconditions is met by definition, so its
session finishes at its first turn end. A finish still unsettled `terminal.idle_park_after` later
is sent once more. Writer input is the later of what the session reports and the gateway's latest
`terminal_input`; a viewer without the seat writes none.

A spell unanswered for `terminal.idle_park_after` parks for a person when the agent asked, or its
turn failed, in this spell or earlier in the active stretch, or a person worked with the session
(`person_engaged`: typed through the gateway, or released a park). So does an earned finish re-sent
and unsettled that long. Parked, the session is interrupted with reason `idle`, the ticket is
BLOCKED under `factorial:waiting` with one comment carrying the agent's question, the
postconditions still unmet, and a link to the run's terminal when the dashboard's URL is known,
and nothing retries it; while parked, its postconditions are read every `terminal.parked_recheck`,
and met, it is accepted without a resume (`agent_steps.recheck_parked`). Any other unmet stop is
silent: `finish()` is sent with `silent: true`, the ended run fails on its postconditions, and the
step's retry policy applies; a silent finish unsettled after its re-send is cancelled and failed
with `finish_unsettled`. A silent stop's clock waits while a check run on a head the step's
`ci_green` calls name is not completed and restarts when the last one completes, never past the
step timeout (`_idle_waited_out`). Typing into the terminal before a silent finish settles makes
it a park instead, with no re-send, once the typing has been quiet `terminal.idle_park_after`. A
question the CLI would answer itself (`question_expires_at`, codex's question box) parks at the
first tick that sees it, unless a writer typed into the terminal since it was asked. Removing the
label, or the dashboard's Resume, resumes a park (`interrupts`).

A spell is named by the session's `idle_since` and bounded by the latest `agent_active` or
non-rollover `agent_resumed`, all read from the event log, never from the adapter's memory of
what it reported. The step deadline counts active time: it is not checked while IDLE, and runs
from the latest of those two events (`_active_since`).

A token lives an hour, so a session rolls over: 55 minutes after the last mint, or 5 before the
substrate's session cap, it is parked at its next turn end, and at 58 minutes, or 2 before the
cap, mid-turn. Parked with reason `rollover`, it is resumed on the same tick, which mints; the
ticket shows nothing. A finish the postconditions earned wins, and so does an idle park; a
human typing at the prompt, or a finish not yet settled, holds the turn-end rollover off until the
mid-turn time.

The step deadline restarts at whatever the session reports, which its agent can forge; the
ceiling does not. `terminal.session_ceiling` after the spawn or the latest dashboard Resume
(`_live_since`), the session parks with reason `ceiling` under `factorial:interrupted`, mid-turn
or at its prompt, and is not minted for again; only a dashboard Resume releases it
(`interrupts`), or the re-check when it parked at its prompt. It is checked where a rollover
would be, so an earned finish and an idle park still win.

A session nobody stopped whose CLI exits cleanly was ended from its terminal, and the runtime
parks it with reason `exited` (`_settle_exit`). Its postconditions met, `finish()` ends the park
and the run is accepted as any other. Unmet, and the gateway recorded a `terminal_input` in the
active stretch, the quit is a person's: it parks under `factorial:interrupted` with one comment
and spends no attempt; removing the label, or the dashboard's Resume, resumes it, and no
re-check accepts it, since a person's stop is theirs to release. That park waits while a check
run on a head the step's `ci_green` calls name is not completed, never past the step timeout
from the stretch's start (`_ci_holds_exit`), and the postconditions read once the checks settle
decide: met, the run is accepted. Unmet with no typing in the stretch, it waits `EXIT_GRACE`
from the first tick that saw it: the gateway records a typing burst up to `AUDIT_MAX` after its
first key, and the CLI is gone within a second of the Enter that ended it. Then `finish()` ends
the park and the run is judged as a clean end, which fails on its postconditions into the step's
retry. So is a quit after a `finish()` the loop sent in the active stretch: that finish asked for
the end. A person's pending Interrupt is honoured before any of this (`agent_steps._poll_run`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import Row, func, select
from sqlalchemy.orm import aliased

from orchestrator.config import AgentStep
from orchestrator.db.models import Event, StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType, ResumedVia
from orchestrator.events.terminals import (
    ask_this_stretch,
    last_terminal_input,
    question_parks_now,
    stretch_starts,
    typed_since,
    waits_for_a_person,
)
from orchestrator.github import LABEL_INTERRUPTED, LABEL_WAITING
from orchestrator.predicates import Unmet
from orchestrator.terminal.protocol import AUDIT_MAX
from orchestrator.worker.acceptance import ci_checks, postconditions_met
from orchestrator.worker.failures import _fail_run
from orchestrator.worker.inputs import release_gitlab_run_token
from orchestrator.worker.outputs import observe_outputs
from orchestrator.worker.pricing import record_usage
from orchestrator.worker.tick_context import _Tick, _timeout
from runtime.contract import (
    ParkReason,
    RunStatus,
    RuntimeAdapter,
    SessionState,
)

logger = logging.getLogger(__name__)

_NOTHING_UNMET: Final = Unmet()


def _when(run: StepRun, details: dict[str, object], key: str) -> datetime | None:
    value = details.get(key)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("step_run %s has an unreadable %s %r", run.id, key, value)
    return None


async def _terminal(adapter: RuntimeAdapter, run_id: str) -> dict[str, Any]:
    """`terminal` for a run's `agent_spawned` or `agent_resumed`: where the gateway
    attaches, which it reads only from the log (decision 57).

    Omitted rather than raised: the run is already launched, and a rolled-back tick would launch
    it again. The gateway refuses a run with no target.
    """
    try:
        target = await adapter.terminal_target(run_id)
    except Exception:
        logger.warning("run %s: terminal target unread", run_id, exc_info=True)
        return {}
    return {"terminal": target.model_dump()}


async def _active_since(tick: _Tick, run: StepRun) -> datetime | None:
    """When the run's current active stretch began: its latest `agent_active` or non-rollover
    `agent_resumed`, else its spawn. From the spawn, a run that waited longer than the step
    timeout would be cancelled on its first active poll.

    Read from the event rather than a column so `started_at` stays the spawn time the duration
    read model subtracts from.
    """
    row = (
        await tick.session.execute(
            select(Event.type, Event.details)
            .where(Event.step_run_id == run.id, stretch_starts(Event))
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is not None:
        key = "active_at" if row.type == EventType.AGENT_ACTIVE else "resumed_at"
        since = _when(run, row.details, key)
        if since is not None:
            return since
    return run.started_at


@dataclass(frozen=True, slots=True)
class _Spell:
    #: `agent_idle` was appended for this spell.
    announced: bool
    #: When this spell's first `agent_finishing` was sent; None while `finish()` never was.
    finishing_since: datetime | None
    #: When the one re-send was sent; None while it never was.
    retried_since: datetime | None
    #: The finish was sent with the postconditions unmet: nothing was asked and nobody engaged.
    silent: bool = False
    #: The postconditions a silent finish was sent with unmet.
    unmet: Unmet = field(default_factory=Unmet)


async def _stretch_spells(tick: _Tick, run: StepRun) -> Sequence[Row[tuple[str, Any]]]:
    """`(type, details)` of the `agent_idle` and `agent_finishing` events since the active
    stretch began, oldest first."""
    stretch = aliased(Event)
    boundary = (
        select(stretch.id)
        .where(stretch.step_run_id == run.id, stretch_starts(stretch))
        .order_by(stretch.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        await tick.session.execute(
            select(Event.type, Event.details)
            .where(
                Event.step_run_id == run.id,
                Event.type.in_([EventType.AGENT_IDLE, EventType.AGENT_FINISHING]),
                Event.id > func.coalesce(boundary, 0),
            )
            .order_by(Event.id)
        )
    ).all()


async def _spell(tick: _Tick, run: StepRun, idle_since: datetime) -> _Spell:
    """What the log already says about the idle spell `idle_since` names.

    Only events after the active stretch's start count, and only those naming this
    `idle_since`: a session that took a turn between two polls idles again with a new one, and
    that is a new spell however the polls fell.
    """
    rows = await _stretch_spells(tick, run)
    named = [row for row in rows if _when(run, row.details, "idle_since") == idle_since]
    finishing = [row for row in named if row.type == EventType.AGENT_FINISHING]
    sent = [
        (row.details.get("retry") is True, at)
        for row in finishing
        if (at := _when(run, row.details, "sent_at")) is not None
    ]
    first = finishing[0].details if finishing else {}
    return _Spell(
        announced=any(row.type == EventType.AGENT_IDLE for row in named),
        finishing_since=min((at for _, at in sent), default=None),
        retried_since=min((at for retry, at in sent if retry), default=None),
        silent=first.get("silent") is True,
        unmet=Unmet.read(first),
    )


async def _send_finish(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    idle_since: datetime,
    *,
    retry: bool,
    silent: bool = False,
    unmet: Unmet = _NOTHING_UNMET,
) -> RunStatus:
    """`silent`: the postconditions are unmet, and the run ends to fail on them."""
    assert run.runtime_run_id is not None
    await tick.emit(
        EventType.AGENT_FINISHING,
        run=run,
        step=step.name,
        attempt=run.attempt,
        idle_since=idle_since.isoformat(),
        sent_at=tick.now.isoformat(),
        **({"retry": True} if retry else {}),
        **({"silent": True, **unmet.details()} if silent else {}),
    )
    await adapter.finish(run.runtime_run_id)
    status = await adapter.status(run.runtime_run_id)
    # Not terminal yet, it is still the idle spell being finished: a RUNNING read here must not
    # reach the deadline check, which would measure from the stretch before the idle.
    return status if status.is_terminal else RunStatus.IDLE


_QUESTION_LIMIT = 1500


def _fenced(text: str) -> str:
    """`text` as one fenced block, whatever backticks it holds: it is agent-written, so it is
    shown, never rendered as the comment's own markdown."""
    longest = max((len(found) for found in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


async def _park_idle(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    question: str | None,
    *,
    finishing: bool,
    turn_error: str | None = None,
    unmet: Unmet = _NOTHING_UNMET,
) -> None:
    """Interrupt the session with reason `idle` and park the ticket under `factorial:waiting`.
    `unmet` is why the run is not accepted yet, which the agent's own words may not say.

    `agent_parked`, never `agent_interrupted`: `_interrupt_pending` pairs that one with a
    human's `interrupt_requested`, and an unrequested one would hide the next real request.
    """
    assert run.runtime_run_id is not None
    await adapter.interrupt(run.runtime_run_id, reason=ParkReason.IDLE)
    await release_gitlab_run_token(tick, run.id)
    run.status = StepRunStatus.INTERRUPTED
    run.ended_at = tick.now
    run.failure_reason = "parked idle, waiting on a human; the workspace is kept for a resume"
    # BLOCKED, not a failed step: no retry backoff ever sees it (decision 26).
    tick.ticket.status = TicketStatus.BLOCKED
    await tick.emit(
        EventType.AGENT_PARKED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        reason=ParkReason.IDLE,
        at_prompt=True,
    )
    tick.plan.label = LABEL_WAITING
    if finishing:
        why = "Its expected results are there, but the session did not end when asked to finish."
    elif question:
        clipped = question if len(question) <= _QUESTION_LIMIT else question[:_QUESTION_LIMIT] + "…"
        why = f"The agent asked:\n\n{_fenced(clipped)}"
    elif turn_error:
        why = f"Its last reply failed:\n\n{_fenced(turn_error)}"
    else:
        why = "A person worked with this session, so it waits for them."
    if not finishing and unmet:
        why += f"\n\nStill missing: {'; '.join(unmet.said)}."
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) is paused: the agent sat idle at its prompt, "
        f"waiting for a person.\n\n{why}\n\n"
        f"The session and its workspace are kept. To resume it, answer here and remove the "
        f"`{LABEL_WAITING}` label, or press Resume {_terminal_link(tick, run)}. The agent reads "
        "the comments posted since it asked."
    )


def _terminal_link(tick: _Tick, run: StepRun) -> str:
    """Where Resume is: the run's terminal on the dashboard. Named, not linked, without an
    absolute URL: GitHub resolves a relative one against the repository."""
    base = tick.deps.sessions.dashboard_url
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        return "on the run's terminal in the dashboard"
    # Under the ticket's organization: the dashboard's every page is (tech-stack decision 68).
    page = f"{base}/orgs/{tick.project.organization_slug}/tickets/{tick.ticket.id}"
    return f"on [the run's terminal]({page}?terminal={run.id})"


def _last_typed(state: SessionState | None, gateway: datetime | None) -> datetime | None:
    """When a writer last typed: what the session reports, or the gateway's latest
    `terminal_input`, whichever is later."""
    reported = state.last_input_at if state is not None else None
    return max((at for at in (reported, gateway) if at is not None), default=None)


def _quiet(tick: _Tick, typed: datetime | None, since: datetime) -> bool:
    park_after = tick.deps.sessions.idle_park_after
    return tick.now - since >= park_after and (typed is None or tick.now - typed >= park_after)


def _stretch_quiet(tick: _Tick, stretch: datetime | None) -> bool:
    return stretch is None or tick.now - stretch >= tick.deps.sessions.idle_park_after


async def _waited_out(tick: _Tick, run: StepRun, typed: datetime | None, since: datetime) -> bool:
    """Has the session been quiet for `terminal.idle_park_after`, counted from `since` and from
    every later sign of life? A human typing into the terminal is answering, and a session
    released at the prompt it parked at may still report the `idle_since` it parked with."""
    return _quiet(tick, typed, since) and _stretch_quiet(tick, await _active_since(tick, run))


async def _idle_waited_out(
    tick: _Tick, step: AgentStep, run: StepRun, typed: datetime | None, idle_since: datetime
) -> bool:
    """`_waited_out` for a silent stop, whose clock also waits for the check runs on the heads
    the step's `ci_green` calls name to settle, and restarts when the last one completes. The
    step timeout, from the active stretch's start because a rollover re-idles with a fresh
    `idle_since`, bounds that wait. GitHub is read only once the plain clock has run out."""
    if not _quiet(tick, typed, idle_since):
        return False
    stretch = await _active_since(tick, run)
    if not _stretch_quiet(tick, stretch):
        return False
    if tick.now >= (stretch or tick.now) + _timeout(tick.config, step):
        return True
    pending, settled = await ci_checks(tick, step, run.attempt)
    if pending:
        return False
    return settled is None or settled <= idle_since or _quiet(tick, typed, settled)


async def _repark_lost(tick: _Tick, step: AgentStep, run: StepRun, adapter: RuntimeAdapter) -> None:
    """Park again a run the substrate says is INTERRUPTED while the log still holds it IDLE: the
    tick that parked it rolled back after `interrupt`. Left to the terminal path it would fail as
    INTERRUPTED, which nothing retries or releases. A human's lost stop never reaches here:
    `_poll_run` honours a pending interrupt request first."""
    rows = await _stretch_spells(tick, run)
    last = rows[-1] if rows else None
    finishing = last is not None and last.type == EventType.AGENT_FINISHING
    details = last.details if last is not None and not finishing else {}
    question, turn_error = details.get("question"), details.get("turn_error")
    await _park_idle(
        tick,
        step,
        run,
        adapter,
        question if isinstance(question, str) else None,
        finishing=finishing,
        turn_error=turn_error if isinstance(turn_error, str) else None,
        unmet=Unmet.read(details),
    )


@dataclass(frozen=True, slots=True)
class _Rollover:
    after: timedelta
    before_cap: timedelta


_AT_TURN_END: Final = _Rollover(after=timedelta(minutes=55), before_cap=timedelta(minutes=5))
_MID_TURN: Final = _Rollover(after=timedelta(minutes=58), before_cap=timedelta(minutes=2))

#: What a session stopped mid-turn resumes with. At a silent prompt it would idle there, and wait
#: on a human for work it was in the middle of.
_ROLLOVER_TURN: Final = (
    "Your session was stopped mid-turn to renew its credentials, and is now resumed. Continue "
    "the work you were doing."
)

#: What a session whose rollover lost its record resumes with: nothing says whether it was mid-turn
#: or waiting at its prompt, so one turn has to be right in both. "Continue" alone would answer a
#: waiting session's question for the human.
_RECOVERY_TURN: Final = (
    "Your session was stopped to renew its credentials and is now resumed. If you were in the "
    "middle of work, continue it; if you were waiting on a human's answer, ask your question "
    "again and wait."
)


async def _minted_at(tick: _Tick, run: StepRun) -> datetime:
    """When the run's token was minted: its spawn or its latest resume, a rollover's included.
    An adapter never mints (§7.1), so the log is the only record."""
    rows = (
        await tick.session.execute(
            select(Event.type, Event.details).where(
                Event.step_run_id == run.id,
                Event.type.in_([EventType.AGENT_SPAWNED, EventType.AGENT_RESUMED]),
            )
        )
    ).all()
    mints = [
        at
        for row in rows
        if (
            at := _when(
                run,
                row.details,
                "minted_at" if row.type == EventType.AGENT_SPAWNED else "resumed_at",
            )
        )
        is not None
    ]
    return max(mints, default=run.started_at or tick.now)


def _rollover_due(tick: _Tick, minted_at: datetime, state: SessionState, when: _Rollover) -> bool:
    due = minted_at + when.after
    # Only a cap the turn-end window can still act on, for both windows: a cap the last rollover
    # did not move, another would not move either, and each tick would mint again.
    cap = state.session_deadline
    if cap is not None and cap - _AT_TURN_END.before_cap > minted_at:
        due = min(due, cap - when.before_cap)
    return tick.now >= due


def _typing(tick: _Tick, typed: datetime | None) -> bool:
    return typed is not None and tick.now - typed < tick.deps.sessions.finish_grace


async def _roll_over(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    minted_at: datetime,
    *,
    turn: str | None,
    interrupted: bool = False,
    at_prompt: bool,
) -> bool:
    """Park the session with reason `rollover` and resume it on this tick with a fresh token.
    The row keeps the status it had, and the ticket shows nothing. `interrupted`: the tick that
    stopped it rolled back, and the stop is all that survived.

    False when the resume was refused, and the ticket is parked instead. A passing fault raises:
    the tick rolls back, and the next one finds the session stopped with no record of it and
    resumes it again (`_recover_stop`).
    """
    # Here, not at the top: `interrupts` imports this module.
    from orchestrator.worker.interrupts import _resume_session, resume_refusal_words

    assert run.runtime_run_id is not None
    prior = run.status
    if not interrupted:
        await adapter.interrupt(run.runtime_run_id, reason=ParkReason.ROLLOVER)
    await tick.emit(
        EventType.AGENT_PARKED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        reason=ParkReason.ROLLOVER,
        mint_age_seconds=int((tick.now - minted_at).total_seconds()),
        at_prompt=at_prompt,
    )
    why = await _resume_session(
        tick,
        step,
        run,
        None,
        turn=turn,
        steering=(),
        attachments=(),
        missing=(),
        via=ResumedVia.ROLLOVER,
        announce=False,
    )
    if why is not None:
        await _park_unrenewed(tick, step, run, resume_refusal_words(why))
        return False
    run.status = prior
    return True


async def _park_unrenewed(tick: _Tick, step: AgentStep, run: StepRun, why: str) -> None:
    """A rollover that stopped the session and could not start it again, `why` in words."""
    await release_gitlab_run_token(tick, run.id)
    run.status = StepRunStatus.INTERRUPTED
    run.ended_at = tick.now
    run.failure_reason = "parked to renew its token, and could not be resumed"
    tick.ticket.status = TicketStatus.BLOCKED
    tick.plan.label = LABEL_INTERRUPTED
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) is paused. Its session was stopped to renew "
        f"its one-hour GitHub token and could not be resumed: {why}\n\n"
        f"The session and its workspace are kept. Remove the `{LABEL_INTERRUPTED}` label to "
        "resume it."
    )


async def _live_since(tick: _Tick, run: StepRun) -> datetime | None:
    """When the session's ceiling window opened: its spawn, or the latest dashboard Resume. Never
    anything the session reports, which its agent can forge, nor a label release, which any token
    in the repository can make, nor a rollover's resume, which the loop grants itself."""
    resumes = (
        await tick.session.execute(
            select(Event.details).where(
                Event.step_run_id == run.id,
                Event.type == EventType.AGENT_RESUMED,
                Event.details["via"].astext == ResumedVia.DASHBOARD,
            )
        )
    ).scalars()
    starts = [run.started_at, *(_when(run, details, "resumed_at") for details in resumes)]
    return max((at for at in starts if at is not None), default=None)


def _spoken(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes % 60:
        return f"{minutes} minute{'s' * (minutes != 1)}"
    return f"{minutes // 60} hour{'s' * (minutes != 60)}"


async def _parked_at_ceiling(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    *,
    interrupted: bool = False,
    at_prompt: bool,
) -> bool:
    """Park the session with reason `ceiling` if it has lived `terminal.session_ceiling` since
    `_live_since`. `interrupted`: the session is already stopped."""
    ceiling = tick.deps.sessions.session_ceiling
    since = await _live_since(tick, run)
    if since is None or tick.now - since < ceiling:
        return False
    assert run.runtime_run_id is not None
    if not interrupted:
        await adapter.interrupt(run.runtime_run_id, reason=ParkReason.CEILING)
    await release_gitlab_run_token(tick, run.id)
    run.status = StepRunStatus.INTERRUPTED
    run.ended_at = tick.now
    run.failure_reason = "parked at its session ceiling; the workspace is kept for a resume"
    tick.ticket.status = TicketStatus.BLOCKED
    await tick.emit(
        EventType.AGENT_PARKED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        reason=ParkReason.CEILING,
        live_seconds=int((tick.now - since).total_seconds()),
        at_prompt=at_prompt,
    )
    tick.plan.label = LABEL_INTERRUPTED
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) is paused. Its session has run for "
        f"{_spoken(ceiling)} since it started or was last resumed from the dashboard. That is the "
        "longest Factorial lets one session run without a person.\n\n"
        f"The session and its workspace are kept. Press Resume {_terminal_link(tick, run)} to "
        f"resume it for another {_spoken(ceiling)}. Removing the `{LABEL_INTERRUPTED}` label does "
        "not resume it."
    )
    return True


async def _renewed_if_due(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    state: SessionState,
    when: _Rollover,
    *,
    turn: str | None,
    at_prompt: bool,
) -> bool:
    """Roll the session over if its token is due. False when the ticket is parked instead: the
    session reached its ceiling, which no token outlives, or its rollover's resume was refused."""
    if await _parked_at_ceiling(tick, step, run, adapter, at_prompt=at_prompt):
        return False
    minted_at = await _minted_at(tick, run)
    if not _rollover_due(tick, minted_at, state, when):
        return True
    return await _roll_over(tick, step, run, adapter, minted_at, turn=turn, at_prompt=at_prompt)


async def _idle_park_was_due(tick: _Tick, run: StepRun) -> bool:
    """Had the open spell waited out `terminal.idle_park_after` by the log's own times, on a
    stop that parks? The session's own report of input, which only ever delays a park, is not in
    the log, so a stop it delayed reads due; the gateway's `terminal_input` is. A silent stop
    never parks, so its lost stop is a rollover's."""
    rows = await _stretch_spells(tick, run)
    last = rows[-1] if rows else None
    if last is None:
        return False
    if last.type == EventType.AGENT_FINISHING and last.details.get("silent") is True:
        return False
    typed = await last_terminal_input(tick.session, run)
    if last.type == EventType.AGENT_FINISHING:
        since = min(
            (
                at
                for row in rows
                if row.type == EventType.AGENT_FINISHING
                and row.details.get("retry") is True
                and (at := _when(run, row.details, "sent_at")) is not None
            ),
            default=None,
        )
    else:
        reason = last.details.get("reason")
        waits, _ = await waits_for_a_person(
            tick.session, run, reason if isinstance(reason, str) else None, typed
        )
        if not waits:
            return False
        since = _when(run, last.details, "idle_since")
    if since is None:
        return False
    return await _waited_out(tick, run, typed, since)


async def _recover_stop(
    tick: _Tick, step: AgentStep, run: StepRun, adapter: RuntimeAdapter
) -> RunStatus | None:
    """The substrate says INTERRUPTED and the log does not: the tick that stopped the session
    rolled back after `interrupt`. Left to the terminal path it fails as INTERRUPTED, which
    nothing retries or releases. A human's lost stop never reaches here: `_poll_run` honours a
    pending request first.

    A RUNNING row is a rollover's: a first idle poll that rolled back reads RUNNING too. An IDLE
    row is an idle park's only when the log says the park was due and the token was not: the log
    misses typing only the session saw, and a lost idle park taken for a rollover costs one mint
    and a question asked again, where the reverse parks a session on a question already answered.
    """
    assert run.runtime_run_id is not None
    minted_at = await _minted_at(tick, run)
    if (
        run.status == StepRunStatus.IDLE
        and tick.now < minted_at + _AT_TURN_END.after
        and await _idle_park_was_due(tick, run)
    ):
        await _repark_lost(tick, step, run, adapter)
        return None
    # Not at its prompt: the tick that rolled back may have seen a turn start.
    if await _parked_at_ceiling(tick, step, run, adapter, interrupted=True, at_prompt=False):
        return None
    if not await _roll_over(
        tick,
        step,
        run,
        adapter,
        minted_at,
        turn=_RECOVERY_TURN,
        interrupted=True,
        at_prompt=False,
    ):
        return None
    status = await adapter.status(run.runtime_run_id)
    if status is RunStatus.INTERRUPTED:
        logger.warning(
            "ticket %s step %s: run %s still reads interrupted after its rollover resumed",
            tick.ticket.external_key,
            step.name,
            run.runtime_run_id,
        )
        await _park_unrenewed(
            tick, step, run, "the session still reads as stopped after the resume"
        )
        return None
    return await _poll_session(tick, step, run, adapter, status)


EXIT_GRACE: Final = timedelta(seconds=AUDIT_MAX + 5)


async def _settle_exit(
    tick: _Tick, step: AgentStep, run: StepRun, adapter: RuntimeAdapter
) -> RunStatus | None:
    """A session ended from its terminal with nobody asking: park it for the person who quit
    it, or end the park and hand `_poll_run` the INTERRUPTED it judges as a clean end. None
    when it was parked, or waits: for the gateway to record the keys that ended it, or for the
    check runs the step's `ci_green` calls read to settle."""
    assert run.id is not None and run.runtime_run_id is not None
    seen = tick.deps.exits_seen
    # Before any adapter side effect: an unreadable verdict rolls the tick back.
    met, unmet = await postconditions_met(tick, step, run.attempt)
    # A finish the loop sent this stretch asked for this end, and on a host a finish that reaches
    # the ended run ends its park with HEAD detached: parked, it would resume on no branch.
    finishing = any(
        row.type == EventType.AGENT_FINISHING for row in await _stretch_spells(tick, run)
    )
    if not met and not finishing:
        stretch = await _active_since(tick, run)
        typed = await last_terminal_input(tick.session, run)
        # Only keys the gateway saw in this stretch: a release, or typing before it, does not
        # say who ended this one.
        if stretch is not None and typed_since(typed, stretch):
            seen.pop(run.id, None)
            if not await _ci_holds_exit(tick, step, run, stretch):
                await _park_exited(tick, step, run, unmet)
            return None
        if tick.now - seen.setdefault(run.id, tick.now) < EXIT_GRACE:
            return None
    seen.pop(run.id, None)
    await adapter.finish(run.runtime_run_id)
    return RunStatus.INTERRUPTED


async def _ci_holds_exit(tick: _Tick, step: AgentStep, run: StepRun, stretch: datetime) -> bool:
    """Does a person's quit wait for a check run on a head the step's `ci_green` calls name to
    complete? Parked first, a run whose checks then pass would wait on a person to release work
    that is done. Never past the step timeout from the stretch's start, the bound
    `_idle_waited_out` puts on a silent stop."""
    if tick.now >= stretch + _timeout(tick.config, step):
        return False
    pending, _ = await ci_checks(tick, step, run.attempt)
    return pending


async def _park_exited(tick: _Tick, step: AgentStep, run: StepRun, unmet: Unmet) -> None:
    """Park a session a person ended from its terminal under `factorial:interrupted`, as their
    Interrupt would. It is already stopped, so nothing is sent to the substrate."""
    assert run.runtime_run_id is not None
    at_prompt = run.status == StepRunStatus.IDLE
    await release_gitlab_run_token(tick, run.id)
    run.status = StepRunStatus.INTERRUPTED
    run.ended_at = tick.now
    run.failure_reason = (
        "parked: a person ended its session from the terminal; the workspace is kept for a resume"
    )
    tick.ticket.status = TicketStatus.BLOCKED
    await tick.emit(
        EventType.AGENT_PARKED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        reason=ParkReason.EXITED,
        at_prompt=at_prompt,
        **unmet.details(),
    )
    tick.plan.label = LABEL_INTERRUPTED
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) is paused: a person ended its session from the "
        f"terminal before it was accepted (still missing: {'; '.join(unmet.said)}).\n\n"
        f"The session and its workspace are kept. To resume it, remove the `{LABEL_INTERRUPTED}` "
        f"label or press Resume {_terminal_link(tick, run)}. The agent reads the comments posted "
        "since the session last started working."
    )


async def _poll_session(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    adapter: RuntimeAdapter,
    status: RunStatus,
) -> RunStatus | None:
    """Act on a run's IDLE or its return from one, and hand back the status the
    rest of `_poll_run` acts on: terminal after a finish that settled at once. None when
    nothing is left to do with the run this tick: it was parked or failed, or its end waits."""
    assert run.runtime_run_id is not None
    if status is RunStatus.INTERRUPTED:
        result = await adapter.result(run.runtime_run_id)
        if result.park_reason is ParkReason.EXITED:
            return await _settle_exit(tick, step, run, adapter)
        return await _recover_stop(tick, step, run, adapter)
    live = status in (RunStatus.RUNNING, RunStatus.IDLE)
    state = await adapter.session_state(run.runtime_run_id) if live else None
    if state is not None and state.usage is not None:
        # Priced at list and labelled so: the end's `result()` replaces it.
        record_usage(run, **state.usage.model_dump(), reported=None)
    if status is RunStatus.IDLE and state is not None and state.idle_since is None:
        # A turn began between `status()` and `session_state()`: the session is active.
        status = RunStatus.RUNNING
    if status is RunStatus.RUNNING:
        assert state is not None
        if run.status == StepRunStatus.IDLE:
            run.status = StepRunStatus.RUNNING
            await tick.emit(
                EventType.AGENT_ACTIVE,
                run=run,
                step=step.name,
                attempt=run.attempt,
                active_at=tick.now.isoformat(),
            )
        timeout = _timeout(tick.config, step)
        if tick.now > (run.started_at or tick.now) + timeout:
            stretch = await _active_since(tick, run) or tick.now
            if tick.now > stretch + timeout:
                # The deadline check that follows cancels it: a fresh token would go unused.
                return status
        renewed = await _renewed_if_due(
            tick, step, run, adapter, state, _MID_TURN, turn=_ROLLOVER_TURN, at_prompt=False
        )
        return status if renewed else None
    if state is None or state.idle_since is None:
        return status

    run.status = StepRunStatus.IDLE
    spell = await _spell(tick, run, state.idle_since)
    if spell.finishing_since is not None:
        gateway_typed = await last_terminal_input(tick.session, run)
        typed = _last_typed(state, gateway_typed)
        if spell.silent and typed_since(gateway_typed, spell.finishing_since):
            # A person took up the session the loop was ending: it waits for them instead.
            if await _waited_out(tick, run, typed, spell.finishing_since):
                await _park_idle(
                    tick, step, run, adapter, state.question, finishing=False, unmet=spell.unmet
                )
                return None
        elif spell.retried_since is not None:
            if await _waited_out(tick, run, typed, spell.retried_since):
                if spell.silent:
                    # The step timeout's order: the session's outputs are read before the fail.
                    await adapter.cancel(run.runtime_run_id)
                    await observe_outputs(tick, step, run)
                    await _fail_run(
                        tick,
                        step,
                        run,
                        StepRunStatus.FAILED,
                        "stopped at its prompt with its postconditions unmet "
                        f"({'; '.join(spell.unmet.rules)}), and did not end when asked to finish",
                        reason="finish_unsettled",
                        summary="The agent stopped at its prompt and did not finish when asked. "
                        f"Still missing: {'; '.join(spell.unmet.said)}.",
                    )
                    return None
                await _park_idle(tick, step, run, adapter, state.question, finishing=True)
                return None
        elif tick.now - spell.finishing_since >= tick.deps.sessions.idle_park_after:
            # Safe to send twice: `finish` is idempotent by contract.
            return await _send_finish(
                tick,
                step,
                run,
                adapter,
                state.idle_since,
                retry=True,
                silent=spell.silent,
                unmet=spell.unmet,
            )
        # Typing can hold a finishing spell at its prompt for good; its token cannot wait.
        renewed = await _renewed_if_due(
            tick, step, run, adapter, state, _MID_TURN, turn=None, at_prompt=True
        )
        return RunStatus.IDLE if renewed else None

    # Before any adapter side effect: an unreadable verdict rolls the tick back, and a `finish()`
    # already sent is the one write a rollback cannot take back.
    met, unmet = await postconditions_met(tick, step, run.attempt)
    gateway_typed = await last_terminal_input(tick.session, run)
    typed = _last_typed(state, gateway_typed)
    if met:
        if _typing(tick, typed):
            # A human typing at the prompt keeps it until the mid-turn rollover must take it.
            renewed = await _renewed_if_due(
                tick, step, run, adapter, state, _MID_TURN, turn=None, at_prompt=True
            )
            return status if renewed else None
        return await _send_finish(tick, step, run, adapter, state.idle_since, retry=False)

    if not spell.announced:
        await tick.emit(
            EventType.AGENT_IDLE,
            run=run,
            step=step.name,
            attempt=run.attempt,
            reason=state.idle_reason,
            idle_since=state.idle_since.isoformat(),
            **({"question": state.question} if state.question is not None else {}),
            **(
                {"question_expires_at": state.question_expires_at.isoformat()}
                if state.question_expires_at is not None
                else {}
            ),
            **({"turn_error": state.turn_error} if state.turn_error is not None else {}),
            **(unmet.details() if unmet else {}),
        )
    if question_parks_now(state.question_expires_at, gateway_typed, state.idle_since):
        # The CLI answers the question itself at the expiry, likely before the next tick.
        await _park_idle(tick, step, run, adapter, state.question, finishing=False, unmet=unmet)
        return None
    waits, ask = await waits_for_a_person(tick.session, run, state.idle_reason, gateway_typed)
    if (
        await _waited_out(tick, run, typed, state.idle_since)
        if waits
        else await _idle_waited_out(tick, step, run, typed, state.idle_since)
    ):
        if not waits:
            return await _send_finish(
                tick,
                step,
                run,
                adapter,
                state.idle_since,
                retry=False,
                silent=True,
                unmet=unmet,
            )
        if ask is None:
            ask = await ask_this_stretch(tick.session, run) or {}
        await _park_idle(
            tick,
            step,
            run,
            adapter,
            state.question or ask.get("question"),
            finishing=False,
            turn_error=state.turn_error or ask.get("turn_error"),
            unmet=unmet,
        )
        return None
    when = _MID_TURN if _typing(tick, typed) else _AT_TURN_END
    if not await _renewed_if_due(tick, step, run, adapter, state, when, turn=None, at_prompt=True):
        return None
    if waits:
        # A stop that will end and retry is not waiting on anyone.
        tick.plan.label = LABEL_WAITING
    return status
