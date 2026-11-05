"""Interrupt and resume (decision 26): a human stops a running agent, the tick parks the ticket with
the workspace kept, and removing `factorial:interrupted` resumes the agent's own session. An idle
park (`worker/sessions.py`) is released the same way, by removing `factorial:waiting`, and either
park by the dashboard's Resume, whose answer is the resumed turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep
from orchestrator.db.models import Event, StepRun, StepRunStatus, Ticket, TicketStatus
from orchestrator.events import EventType, ResumedVia, append
from orchestrator.events.read_models import latest_run_event
from orchestrator.events.terminals import stretch_starts
from orchestrator.events.types import REQUESTED_VIA_WORDS, RequestedVia, requested_via
from orchestrator.github import LABEL_INTERRUPTED, LABEL_WAITING
from orchestrator.github.media import ItemRef
from orchestrator.github.state import CommentState
from orchestrator.worker.attachments import TextItem, attachment_details, resolve_attachments
from orchestrator.worker.exits import RESETS, pending_request
from orchestrator.worker.inputs import media_for, release_run_credential, run_secrets
from orchestrator.worker.projection import _steady_label
from orchestrator.worker.sessions import _terminal, _terminal_link, _when
from orchestrator.worker.tick_context import _runs_for, _Tick, latest_current_run
from runtime.contract import (
    AgentRuntimeError,
    MissingAttachment,
    ParkReason,
    RunAttachment,
    SubstrateUnavailable,
)


@dataclass(frozen=True, slots=True)
class _Park:
    """What parked a run last, as its release needs it."""

    #: `user` for a human's `agent_interrupted`, else the `agent_parked`'s reason.
    reason: str
    #: Comments from this moment on are the resumed turn's steering.
    cutoff: datetime
    #: The parking event: a resume request older than it cannot answer this park.
    event_id: int
    #: The session sat at its prompt, not mid-turn, when the loop parked it.
    at_prompt: bool

    @property
    def idle(self) -> bool:
        return self.reason == ParkReason.IDLE


async def _interrupt_pending(tick: _Tick, run: StepRun) -> bool:
    """Has a human asked for this run to stop, and has the loop not yet honoured it?

    Two events rather than one, because the log is append-only: a request cannot be marked done
    in place, so "pending" is the absence of the `agent_interrupted` that answers it. Counting
    both and comparing is what makes the check idempotent across ticks — a second request while
    the first is unhonoured does not queue a second stop, and an honoured one stops matching.
    """
    rows = (
        await tick.session.execute(
            select(Event.type, func.count())
            .where(
                Event.step_run_id == run.id,
                Event.type.in_(
                    [
                        EventType.INTERRUPT_REQUESTED,
                        EventType.AGENT_INTERRUPTED,
                        EventType.INTERRUPT_OVERTAKEN,
                    ]
                ),
            )
            .group_by(Event.type)
        )
    ).all()
    seen: dict[str, int] = {str(kind): int(count) for kind, count in rows}
    answered = seen.get(EventType.AGENT_INTERRUPTED, 0) + seen.get(EventType.INTERRUPT_OVERTAKEN, 0)
    return seen.get(EventType.INTERRUPT_REQUESTED, 0) > answered


async def _honour_interrupt(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """Stop the substrate, keep the workspace, park the ticket."""
    adapter = await tick.deps.adapter_for_run(tick.session, run)
    assert run.runtime_run_id is not None
    await adapter.interrupt(run.runtime_run_id)
    await release_run_credential(tick, run.id)
    await park_interrupted(tick, step, run, REQUESTED_VIA_WORDS[await _interrupt_via(tick, run)])


async def park_interrupted(tick: _Tick, step: AgentStep, run: StepRun, words: str) -> None:
    """Record a session already stopped as a person's stop: the run INTERRUPTED with its workspace
    kept, the ticket BLOCKED under `factorial:interrupted`. `words` say through which surface."""
    run.status = StepRunStatus.INTERRUPTED
    run.ended_at = tick.now
    run.failure_reason = f"interrupted {words}; the workspace is kept for a resume"
    # BLOCKED, not a failed step: the retry path must not see this. A human stopped the work and
    # a human releases it, which is the same shape `_grant_attempt` already implements for a blocked
    # ticket — removing the label grants one more attempt.
    tick.ticket.status = TicketStatus.BLOCKED
    await tick.emit(
        EventType.AGENT_INTERRUPTED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
    )
    # Through the tick's plan, not the projector directly: nothing is written until `_project`.
    tick.plan.label = LABEL_INTERRUPTED
    tick.plan.comments.append(
        f"Run interrupted {words} at step `{step.name}` (attempt {run.attempt}).\n\n"
        f"The agent's workspace is kept, so the run can resume instead of starting again. Remove "
        f"the `{LABEL_INTERRUPTED}` label to release the ticket. The next run reads the comments "
        "posted here since the run was interrupted."
    )


async def _interrupt_via(tick: _Tick, run: StepRun) -> RequestedVia:
    return requested_via(
        await tick.session.scalar(
            latest_run_event(tick.ticket.id, run.id, [EventType.INTERRUPT_REQUESTED], Event.details)
        )
    )


async def _resume_parked(tick: _Tick) -> bool:
    """Continue a parked run whose label a human removed. Returns whether this tick handled the
    release.

    False means "this was not a park", and the caller falls through to `_grant_attempt`. Every
    other outcome — resumed, or refused with a reason — is handled here and returns True, because
    a release that resolves to silence is the failure `_grant_attempt`'s own docstring warns about.

    A refusal RE-PARKS rather than starting a fresh run. The human asked to continue a
    conversation; giving them a restart instead would cost a full run and could diverge from work
    the agent had already done, and doing that silently is worse than saying it cannot be done.
    """
    step = tick.config.step(tick.ticket.current_step)
    if not isinstance(step, AgentStep):
        return False
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = runs[-1] if runs else None
    if latest is None or StepRunStatus(latest.status) != StepRunStatus.INTERRUPTED:
        return False

    park = await _latest_park(tick, latest)
    if park is not None and park.reason == ParkReason.CEILING:
        _refuse_ceiling_release(tick, step, latest)
        return True
    if not _can_resume(latest):
        _repark(tick, step, latest, park, ResumeRefusal.NO_SESSION)
        return True

    # Before the secrets and the resume: a transient image failure raises, the tick rolls back,
    # and the next tick retries the release — it never re-parks the ticket. The images follow the
    # steering text's own fence (decision 26).
    steering = await _comments_since(tick, park)
    turn = _steering_turn(park, steering)
    attachments, missing = await _steering_images(tick, latest, turn, steering)

    why = await _resume_session(
        tick,
        step,
        latest,
        park,
        turn=turn,
        steering=steering,
        attachments=attachments,
        missing=missing,
        via=ResumedVia.LABEL,
    )
    if why is not None:
        _repark(tick, step, latest, park, why)
    return True


async def _steering_images(
    tick: _Tick, run: StepRun, turn: str | None, steering: Sequence[CommentState]
) -> tuple[tuple[RunAttachment, ...], tuple[MissingAttachment, ...]]:
    if turn is None:
        return (), ()
    prior = (
        await tick.session.execute(
            select(func.count()).where(
                Event.step_run_id == run.id, Event.type == EventType.AGENT_RESUMED
            )
        )
    ).scalar_one()
    return await resolve_attachments(
        media_for(tick),
        tick.source.repo,
        [
            (
                turn,
                tuple(
                    TextItem("steering", ItemRef("issue_comment", tick.issue.number, c.id), c.body)
                    for c in steering
                    if c.id is not None
                ),
            )
        ],
        prefix=f"resume{prior + 1}-image",
        refs=tick.host().media_refs,
    )


#: A Resume request, then what answers it.
RESUME_EVENTS: Final = (
    EventType.SESSION_RESUME_REQUESTED,
    EventType.AGENT_RESUMED,
    EventType.SESSION_RESUME_REFUSED,
    *RESETS,
)


async def _lock_latest_run(session: AsyncSession, ticket: Ticket) -> StepRun | None:
    """Lock the step's latest run, the row the Resume route locks too, before the tick reads a
    pending Resume, and return it as of the lock. A Resume pressed while this tick releases the
    park by its label then waits for the release and is refused as not parked, rather than
    recorded after the read and silently answered by a resume that never carried its text."""
    return await latest_current_run(session, ticket, ticket.current_step, for_update=True)


async def pending_resume_request(session: AsyncSession, ticket_id: int) -> Event | None:
    """The ticket's dashboard Resume that nothing has answered yet (decision 26).

    Any `agent_resumed` since answers it, a label release included: the human's wish — this
    session continues — has come true either way.
    """
    return await pending_request(session, ticket_id, *RESUME_EVENTS)


class ResumeRefusal(StrEnum):
    """`session_resume_refused.reason` when the loop itself refused; a refusal an exception or
    the adapter explains is that text instead."""

    #: The request named a run that is no longer the parked one.
    NOT_PARKED = "not_parked"
    #: The run never got a substrate id, so there is no session to continue.
    NO_SESSION = "no_session"
    #: The steering comments embed images, and this substrate cannot deliver them.
    NO_ATTACHMENTS = "no_attachments"
    #: No agent source is configured, so the run's secrets cannot be resolved.
    NO_AGENT_SOURCE = "no_agent_source"


#: Each code as the comment on the issue words it; the dashboard has its own table.
_REFUSAL_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        ResumeRefusal.NOT_PARKED: "the run it named is no longer the paused one",
        ResumeRefusal.NO_SESSION: "this run never started a session, so there is none to continue",
        ResumeRefusal.NO_ATTACHMENTS: "this runtime can't pass on the images in the comments",
        ResumeRefusal.NO_AGENT_SOURCE: "this agent's settings couldn't be loaded",
    }
)


def resume_refusal_words(why: str) -> str:
    """Why a resume was refused, as a sentence for the issue."""
    said = _REFUSAL_WORDS.get(why, why).strip()
    return said if said.endswith((".", "!", "?")) else f"{said}."


def _refuse_ceiling_release(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """A session parked at its ceiling is released only by a dashboard Resume: any token in the
    repository can remove a label, the parked agent's own included, and a label release would
    open a new ceiling window for it (decision 56)."""
    tick.ticket.status = TicketStatus.BLOCKED
    tick.plan.label = LABEL_INTERRUPTED
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) stays paused: its session ran for the most time "
        "one session may run without a person, so neither removing the "
        f"`{LABEL_INTERRUPTED}` label nor re-entering the ticket resumes it. Press Resume "
        f"{_terminal_link(tick, run)} to resume it."
    )


async def _resume_requested(tick: _Tick, request: Event) -> bool:
    """Honour an operator's Resume, from the dashboard or through the MCP server: the parked
    session continues, with the answer as its turn.

    Returns False only when the request named a run that is no longer parked — refused, with the
    ticket untouched, so whatever holds the ticket now still holds it. Every other outcome is
    handled here, a refused resume re-parking under the park's own label.
    """
    step = tick.config.step(tick.ticket.current_step)
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    run = runs[-1] if runs else None
    park = (
        await _latest_park(tick, run)
        if isinstance(step, AgentStep) and run is not None and run.id == request.step_run_id
        else None
    )
    if (
        not isinstance(step, AgentStep)
        or run is None
        or park is None
        or StepRunStatus(run.status) != StepRunStatus.INTERRUPTED
        # A request older than the park lost a race with the release before it. Nobody has
        # read the question this park asks, so the request cannot be its answer.
        or park.event_id > request.id
    ):
        await _refuse_request(tick, request, ResumeRefusal.NOT_PARKED)
        return False

    steering = await _comments_since(tick, park)
    answer = request.details.get("answer")
    via = requested_via(request.details)
    turn = _steering_turn(
        park,
        steering,
        answer=None if answer is None else (str(request.details["by"]), via, answer),
    )
    attachments, missing = await _steering_images(tick, run, turn, steering)
    why = await _resume_session(
        tick,
        step,
        run,
        park,
        turn=turn,
        steering=steering,
        attachments=attachments,
        missing=missing,
        via=ResumedVia(via),
        request_id=request.id,
    )
    if why is not None:
        await _refuse_request(tick, request, why)
        _repark(tick, step, run, park, why)
    return True


async def _refuse_request(tick: _Tick, request: Event, why: str) -> None:
    await append(
        tick.session,
        type=EventType.SESSION_RESUME_REFUSED,
        project_id=tick.ticket.project_id,
        ticket_id=tick.ticket.id,
        step_run_id=request.step_run_id,
        step=request.details.get("step"),
        attempt=request.details.get("attempt"),
        request_id=request.id,
        reason=why,
    )


def _can_resume(run: StepRun) -> bool:
    return run.runtime_run_id is not None


async def _resume_session(
    tick: _Tick,
    step: AgentStep,
    run: StepRun,
    park: _Park | None,
    *,
    turn: str | None,
    steering: Sequence[CommentState],
    attachments: tuple[RunAttachment, ...],
    missing: tuple[MissingAttachment, ...],
    via: ResumedVia,
    request_id: int | None = None,
    announce: bool = True,
) -> str | None:
    """Resume a parked run's own session on the SAME attempt. Returns why it could not, a
    `ResumeRefusal` or a reason's text, and then has changed nothing: what a refusal means — a
    re-park, a refused request — is the caller's.

    `steering` is what `turn` was built from, recorded by reference. `announce` is the label and
    the "Resumed" comment, which a rollover's same-tick park and resume has no reason to write.

    An unreachable substrate and a failed token mint raise instead: the tick rolls back and the
    next one retries the release, where a re-park would wait on a human for a passing fault.
    """
    adapter = await tick.deps.adapter_for_run(tick.session, run)
    if not _can_resume(run):
        return ResumeRefusal.NO_SESSION
    assert run.runtime_run_id is not None
    if attachments and not adapter.supports_attachments:
        return ResumeRefusal.NO_ATTACHMENTS

    # Credentials come from the store, never from the worker's own environment: the adapter used
    # to read os.environ, which made the encrypted store authoritative in name only and broke on
    # the first host that kept credentials out of the service environment.
    if tick.deps.resolve_agent is None:
        return ResumeRefusal.NO_AGENT_SOURCE
    try:
        resolved = await tick.deps.resolve_agent(
            tick.session, tick.project, step.agent, tick.agent_pins
        )
    except Exception as exc:
        return f"couldn't load this agent: {exc}"
    secrets = (
        await run_secrets(
            tick,
            resolved,
            tick.project.github_repo,
            runtime_id=run.runtime_id,
            run_id=run.id,
        )
    ).values

    try:
        await adapter.resume(run.runtime_run_id, turn, secrets, attachments, missing)
    except SubstrateUnavailable:
        raise
    except AgentRuntimeError as exc:
        await release_run_credential(tick, run.id)
        return str(exc)

    # A stop requested after the tick that parked the run read its requests: the park overtook
    # it, and left pending it would stop the session it was never about. Not after a rollover,
    # which parks for no human: a stop pending then was pressed during it, and must still land.
    if via is not ResumedVia.ROLLOVER and await _interrupt_pending(tick, run):
        await tick.emit(
            EventType.INTERRUPT_OVERTAKEN,
            run=run,
            step=step.name,
            attempt=run.attempt,
            run_id=run.runtime_run_id,
            reason="parked",
        )

    # The SAME attempt continues: no new `step_run` row, and the unique `(ticket, step, attempt)`
    # that makes spawns idempotent is untouched. A run a human steered is not a run that failed.
    # The park set `ended_at`, and clearing it keeps `started_at`, so the pause is kept here or
    # every duration counts it. A rollover waits on the substrate, not on a person.
    if via is not ResumedVia.ROLLOVER and run.ended_at is not None:
        run.parked_seconds += (tick.now - run.ended_at).total_seconds()
    run.status = StepRunStatus.RUNNING
    run.ended_at = None
    run.failure_reason = None
    tick.ticket.status = TicketStatus.RUNNING
    await tick.emit(
        EventType.AGENT_RESUMED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        # The loop's clock, not the row's `created_at`: the deadline is computed against it.
        resumed_at=tick.now.isoformat(),
        via=via,
        **({"request_id": request_id} if request_id is not None else {}),
        # What the agent was told, by reference. The bodies are already on the ticket for anyone
        # to read; recording the count and authors keeps the log able to answer "who steered this"
        # without copying untrusted text — or a credential someone pasted — into the event store.
        steered_by=sorted({c.author for c in steering}),
        comment_count=len(steering),
        **attachment_details(attachments, missing),
        **(await _terminal(adapter, run.runtime_run_id)),
    )
    if announce:
        kept_by = "the interrupt" if park is None or park.reason == ParkReason.USER else "the pause"
        tick.plan.label = _steady_label(tick)
        tick.plan.comments.append(
            f"Resumed `{step.name}` (attempt {run.attempt}) in the workspace {kept_by} kept. "
            "The agent continues its own session and does not start over."
        )
    return None


def _repark(tick: _Tick, step: AgentStep, run: StepRun, park: _Park | None, why: str) -> None:
    """A release that could not resume: the ticket goes back under the label it was parked by,
    so an idle park still reads as waiting on a human, and a stop as a stop."""
    tick.ticket.status = TicketStatus.BLOCKED
    label = LABEL_WAITING if _parked_idle(park) else LABEL_INTERRUPTED
    tick.plan.label = label
    if why == ResumeRefusal.NO_SESSION:
        again = "Restart the ticket from the dashboard to run it again."
    elif park is not None and park.reason == ParkReason.CEILING:
        again = (
            f"Press Resume {_terminal_link(tick, run)} to try again, or restart the ticket from "
            "the dashboard."
        )
    else:
        again = (
            f"Remove the `{label}` label to try again, or restart the ticket from the dashboard."
        )
    tick.plan.comments.append(
        f"Couldn't resume `{step.name}`: {resume_refusal_words(why)}\n\n"
        f"The ticket stays paused. {again}"
    )


def _parked_idle(park: _Park | None) -> bool:
    return park is not None and park.idle


async def _latest_park(tick: _Tick, run: StepRun) -> _Park | None:
    """What parked this run last: a human's `agent_interrupted`, or the loop's own
    `agent_parked`.

    An idle park's steering starts when its spell did, not at the park: a human answering the
    question on the ticket during the wait is the reply the resumed turn exists to carry. An exit
    park's starts when the stretch the quit ended did: the park waits on the gateway's record of
    the keys, or on CI, and a person who said on the ticket why they quit said it to the turn.
    """
    park = (
        await tick.session.execute(
            select(Event.id, Event.type, Event.details, Event.created_at)
            .where(
                Event.step_run_id == run.id,
                Event.type.in_([EventType.AGENT_INTERRUPTED, EventType.AGENT_PARKED]),
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if park is None:
        return None
    reason = (
        str(park.details.get("reason") or ParkReason.IDLE.value)
        if park.type == EventType.AGENT_PARKED
        else ParkReason.USER.value
    )
    at_prompt = park.details.get("at_prompt") is True
    # A ceiling can park a session at its question too, and an answer posted before it is the
    # turn's.
    if reason not in (ParkReason.IDLE, ParkReason.CEILING, ParkReason.EXITED):
        return _Park(
            reason=reason, cutoff=park.created_at, event_id=park.id, at_prompt=at_prompt
        )
    boundary = (
        await tick.session.execute(
            select(Event.id, Event.type, Event.details, Event.created_at)
            .where(Event.step_run_id == run.id, Event.id < park.id, stretch_starts(Event))
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if boundary is None:
        began = run.started_at
    else:
        key = "active_at" if boundary.type == EventType.AGENT_ACTIVE else "resumed_at"
        began = min(_when(run, boundary.details, key) or boundary.created_at, boundary.created_at)
    if reason == ParkReason.EXITED:
        return _Park(
            reason=reason, cutoff=began or park.created_at, event_id=park.id, at_prompt=at_prompt
        )
    spell = (
        await tick.session.execute(
            select(Event.details, Event.created_at)
            .where(
                Event.step_run_id == run.id,
                Event.type.in_([EventType.AGENT_IDLE, EventType.AGENT_FINISHING]),
                Event.id > (boundary.id if boundary is not None else 0),
                Event.id < park.id,
            )
            .order_by(Event.id)
            .limit(1)
        )
    ).one_or_none()
    if spell is None:
        return _Park(
            reason=reason, cutoff=park.created_at, event_id=park.id, at_prompt=at_prompt
        )
    # The session's clock or ours, whichever is earlier: keeping a steer beats dropping one.
    asked = _when(run, spell.details, "idle_since") or spell.created_at
    # But never before the stretch the spell ended: `idle_since` is the agent's to write, and a
    # forged one would replay the ticket's whole history as the human's answer.
    cutoff = min(asked, spell.created_at)
    if began is not None:
        cutoff = max(cutoff, began)
    return _Park(reason=reason, cutoff=cutoff, event_id=park.id, at_prompt=at_prompt)


async def _comments_since(tick: _Tick, park: _Park | None) -> Sequence[CommentState]:
    """Ticket comments posted since `park`'s cutoff.

    Time-ordered against the log rather than against a marker in the text: a human steering a
    run should not have to remember a syntax, and one who forgets it would be silently ignored,
    which is the worst failure available to a steering feature.

    HUMAN comments only. The cutoff decides which comments count; `bot_login` decides whose.
    """
    # Author-fenced on `bot_login`, the same fence `issue_comment_contains` uses: the projector
    # writes its own "run interrupted" notice onto the ticket, and a time-only cutoff would hand
    # the machine's output back to the agent as human instruction.
    comments = [c for c in await tick.source.issue_comments() if c.author != tick.ctx.bot_login]
    if park is None:
        return comments
    # GitHub stamps whole seconds, so a comment from the cutoff's own second counts: dropping
    # a steer is worse than keeping one posted a moment early.
    cutoff = park.cutoff.replace(microsecond=0)
    return [c for c in comments if datetime.fromisoformat(c.created_at) >= cutoff]


_CEILING_OPENING: Final = (
    "Your session was parked at the most time one session may run without a human, and a human "
    "has resumed it."
)
#: Like `sessions._RECOVERY_TURN`: "Continue" alone would answer a waiting session's question.
_CEILING_TURN: Final = (
    f"{_CEILING_OPENING} If you were in the middle of work, continue it; if you were waiting on a "
    "human's answer, ask your question again and wait."
)

_EXITED_OPENING: Final = "A person ended your session from its terminal, and it is now resumed."
_EXITED_TURN: Final = (
    f"{_EXITED_OPENING} If you were in the middle of work, continue it; if you were waiting on a "
    "human's answer, ask your question again and wait."
)


def _steering_turn(
    park: _Park | None,
    comments: Sequence[CommentState],
    *,
    answer: tuple[str, RequestedVia, str] | None = None,
) -> str | None:
    """The next turn for a resumed conversation: what humans said while the ticket sat parked,
    and last the `(by, via, text)` answer an operator's Resume carried.

    None when nobody wrote to the run: its session comes back at its prompt with
    nothing typed (decision 26), and a canned "continue" would answer its question for the human.

    Untrusted text, and treated as such — it is delivered on stdin like every other prompt, and
    the agent's own constitution already says outside text is data, never instruction. Nothing
    here interprets it; the orchestrator still trusts postconditions rather than the conversation.
    """
    said = [f"@{c.author} wrote:\n{c.body}" for c in comments]
    if answer is not None:
        by, via, text = answer
        said.append(f"{by} answered {REQUESTED_VIA_WORDS[via]}:\n{text}")
    ceiling = park is not None and park.reason == ParkReason.CEILING
    exited = park is not None and park.reason == ParkReason.EXITED
    if not said:
        # A ceiling stops a session mid-turn as readily as at its prompt, and nothing says which;
        # so does a quit.
        if ceiling:
            return _CEILING_TURN
        return _EXITED_TURN if exited else None
    if _parked_idle(park):
        opening = "Your session was parked while you waited on a human, and is now resumed."
    elif ceiling:
        opening = _CEILING_OPENING
    elif exited:
        opening = _EXITED_OPENING
    else:
        opening = "You were interrupted by a human and are now resumed."
    where = " on the ticket" if answer is None else ""
    return (
        f"{opening} They left the following{where} while you were stopped:\n\n"
        + "\n\n".join(said)
        + "\n\nTake it into account and continue."
    )
