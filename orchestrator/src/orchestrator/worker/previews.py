"""Runtime-served previews (tech-stack decision 54): the part of a pass that honours Start and
Stop, watches an awake preview and starts one again after its sandbox's renewal, and the record
of a rework taking its previous attempt's preview over (`carry_preview`).

The pass runs beside the ticket's one transition, never instead of it, and ahead of the parks: an
interrupted ticket can still be previewed. A preview changes no state the loop owns, so each
run's work sits in its own SAVEPOINT, and a failure is answered with `preview_failed` or logged,
never raised into the ticket's pass. The API records requests and never calls a substrate; this
module is where the substrate is called.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep, Preview
from orchestrator.config.models import SECRET_REF
from orchestrator.db.models import Event, StepRun, Ticket
from orchestrator.events import EventType, append
from orchestrator.events.previews import (
    LOOP_REQUESTER,
    MAX_AWAKE_WORKSPACES,
    PreviewLink,
    awake_workspaces,
    preview_keep_until,
    preview_lifecycle,
    publish_preview_links,
)
from orchestrator.events.workspaces import LIVE_STATUSES, WHY_NOT_SERVABLE, kept_workspaces
from orchestrator.secrets.store import resolve_for_run
from orchestrator.settings import get_settings
from orchestrator.worker.runtime_adapters import RuntimeRefused
from orchestrator.worker.tick_context import _load_pinned_config
from runtime.contract import (
    PREVIEW_CONTINUED,
    PREVIEW_IDLE,
    PREVIEW_RENEWAL,
    PreviewHost,
    PreviewPhase,
    PreviewServiceSpec,
    PreviewSpec,
    PreviewState,
    PreviewUnavailable,
    UnknownRunError,
)

if TYPE_CHECKING:
    from orchestrator.worker.context import ReconcileDeps

logger = logging.getLogger(__name__)

__all__ = ["carry_preview", "reconcile_previews", "stop_awake_previews"]


class _Refused(Exception):
    """A request the loop cannot honour, answered with `preview_failed` and this reason."""


#: A preview whose latest lifecycle event is one of these is starting, stopping or serving. Every
#: lifecycle opens with one: a request, or what a rework took over.
_AWAKE: Final = (
    EventType.PREVIEW_REQUESTED,
    EventType.PREVIEW_STOP_REQUESTED,
    EventType.PREVIEW_STARTED,
)


def _renewed(latest: Event | None) -> bool:
    """Stopped for its sandbox's renewal, and so owed a start. Exactly: a park that failed and
    killed the sandbox says why after the word, and nothing is left to start it in."""
    return (
        latest is not None
        and latest.type == EventType.PREVIEW_STOPPED
        and latest.details.get("reason") == PREVIEW_RENEWAL
    )


async def _latest_by_run(session: AsyncSession, ticket: Ticket) -> list[tuple[StepRun, Event]]:
    """The ticket's runs that ever had a runtime-served preview, with its latest lifecycle
    event."""
    runs = (
        await session.scalars(
            select(StepRun)
            .where(
                StepRun.ticket_id == ticket.id,
                StepRun.id.in_(
                    select(Event.step_run_id).where(
                        Event.ticket_id == ticket.id, Event.type.in_(_AWAKE)
                    )
                ),
            )
            .order_by(StepRun.id)
        )
    ).all()
    pairs: list[tuple[StepRun, Event]] = []
    for run in runs:
        _, latest = await preview_lifecycle(session, run)
        if latest is not None:
            pairs.append((run, latest))
    return pairs


async def reconcile_previews(
    session: AsyncSession, ticket: Ticket, deps: ReconcileDeps, *, now: datetime
) -> None:
    for run, latest in await _latest_by_run(session, ticket):
        if latest.type in _AWAKE:
            try:
                async with session.begin_nested():
                    await _one(session, ticket, deps, run, latest, now)
            except (_Refused, PreviewUnavailable, RuntimeRefused) as exc:
                # Nothing can serve it: answered, so the request stops being pending and the
                # page says why.
                await _answer(session, ticket, run, EventType.PREVIEW_FAILED, reason=str(exc))
            except Exception:
                # Anything else may pass — an E2B outage — while the sandbox still serves: the
                # next pass looks again, so an awake preview is still parked at its limit. A
                # preview never blocks the ticket's own pass.
                logger.exception(
                    "ticket %s run %s: the preview pass failed", ticket.external_key, run.id
                )
            # A renewal seen now is answered now: a rework spawned later in this tick then finds
            # the request, and moves it to the run that continues (`carry_preview`).
            _, latest = await preview_lifecycle(session, run)
        if _renewed(latest):
            try:
                async with session.begin_nested():
                    await _renew(session, ticket, run, now)
            except Exception:
                logger.exception(
                    "ticket %s run %s: restarting a renewed preview failed",
                    ticket.external_key,
                    run.id,
                )


async def stop_awake_previews(
    session: AsyncSession,
    ticket: Ticket,
    deps: ReconcileDeps,
    *,
    reason: str,
    runs: Collection[int] | None = None,
) -> None:
    """Park every awake preview of a ticket leaving the loop, or of the `runs` a redo set aside.
    No later pass would park it, and E2B's own timeout kills the sandbox the keep rule keeps; the
    adapter's stop pauses it. An outage raises, so the transition waits for the next tick rather
    than leave one serving."""
    for run, latest in await _latest_by_run(session, ticket):
        if latest.type not in _AWAKE or (runs is not None and run.id not in runs):
            continue
        try:
            adapter = await deps.adapter_for_run(session, run)
            if isinstance(adapter, PreviewHost) and run.runtime_run_id is not None:
                await adapter.preview_stop(run.runtime_run_id)
        except (RuntimeRefused, UnknownRunError):
            pass  # Nothing can reach the sandbox to stop it.
        await _answer(session, ticket, run, EventType.PREVIEW_STOPPED, reason=reason)


async def _one(
    session: AsyncSession,
    ticket: Ticket,
    deps: ReconcileDeps,
    run: StepRun,
    latest: Event,
    now: datetime,
) -> None:
    adapter = await deps.adapter_for_run(session, run)
    if not isinstance(adapter, PreviewHost):
        raise _Refused("this run's runtime can't serve a preview")
    run_id = run.runtime_run_id
    if run_id is None:
        raise _Refused("this run never started, so there is nothing to preview")
    why = (
        await kept_workspaces(session, now, deps.sessions.retention, ticket_id=ticket.id)
    ).unservable(run.id)

    if latest.type == EventType.PREVIEW_STOP_REQUESTED:
        await adapter.preview_stop(run_id)
        who = latest.details.get("requested_by", "someone")
        await _answer(session, ticket, run, EventType.PREVIEW_STOPPED, reason=f"stopped by {who}")
        return
    if why is not None:
        await adapter.preview_stop(run_id)
        await _answer(session, ticket, run, EventType.PREVIEW_STOPPED, reason=WHY_NOT_SERVABLE[why])
        return

    preview = await _served(session, ticket, run)
    state = await adapter.preview_status(
        run_id, keep_until=await _keep_until(session, run, preview, now)
    )
    if latest.type == EventType.PREVIEW_REQUESTED and state.token != str(latest.id):
        # A live run keeps its sandbox awake anyway, and the cap does not count its preview.
        live = run.status in LIVE_STATUSES
        if not live and len(await awake_workspaces(session) - {run.id}) >= MAX_AWAKE_WORKSPACES:
            raise _Refused(
                f"{MAX_AWAKE_WORKSPACES} previews and open sessions are already running in this "
                "deployment; stop one first"
            )
        spec, env = await _preview_spec(session, ticket, run, preview)
        state = await adapter.preview_start(run_id, spec, env, token=str(latest.id))
    await _observe(session, ticket, run, latest, state)


async def _observe(
    session: AsyncSession, ticket: Ticket, run: StepRun, latest: Event, state: PreviewState
) -> None:
    if state.phase is PreviewPhase.READY and latest.type == EventType.PREVIEW_REQUESTED:
        await _publish(session, run, [(url.service, url.url) for url in state.urls])
        await _answer(
            session,
            ticket,
            run,
            EventType.PREVIEW_STARTED,
            services=[url.service for url in state.urls],
            head_sha=state.head_sha,
            uncommitted=state.uncommitted,
        )
    elif state.phase is PreviewPhase.FAILED:
        await _answer(session, ticket, run, EventType.PREVIEW_FAILED, reason=state.detail)
    elif state.phase is PreviewPhase.STOPPED:
        await _answer(session, ticket, run, EventType.PREVIEW_STOPPED, reason=state.detail)


async def _renew(session: AsyncSession, ticket: Ticket, run: StepRun, now: datetime) -> None:
    """Start again a preview its sandbox's renewal stopped: the loop's own request, which the
    next pass answers as any Start. Once: the request is then the latest lifecycle event. When
    its idle close came first, it stays closed for idle, as the substrate would have closed it
    had it read the preview first."""
    keep_until = await _keep_until(session, run, await _served(session, ticket, run), now)
    if keep_until is not None and keep_until <= now:
        await _answer(session, ticket, run, EventType.PREVIEW_STOPPED, reason=PREVIEW_IDLE)
        return
    await _request(session, ticket, run)


async def carry_preview(
    session: AsyncSession,
    ticket: Ticket,
    adapter: object,
    *,
    previous_id: int,
    run: StepRun,
    now: datetime,
) -> None:
    """A rework that adopted its previous attempt's sandbox takes over the preview a person had
    there, so each URL stays bound to one run. The substrate moves a ready one; one it could not
    move, renewed or still starting, the loop starts again on the rework; a Stop not yet honoured
    moves with it, to stop what the rework now serves.

    Never raises: the rework is launched, and a rolled-back tick would spawn it again into the
    same workspace."""
    run_id = run.runtime_run_id
    if not isinstance(adapter, PreviewHost) or not run_id:
        return
    try:
        async with session.begin_nested():
            pending = await _pending(session, previous_id)
    except Exception:
        logger.exception(
            "ticket %s run %s: reading run %s's preview failed",
            ticket.external_key,
            run.id,
            previous_id,
        )
        return
    if pending is None:
        return
    previous, latest = pending
    try:
        async with session.begin_nested():
            await _carry(session, ticket, adapter, previous, latest, run, run_id, now)
    except Exception:
        logger.exception(
            "ticket %s run %s: carrying run %s's preview over failed",
            ticket.external_key,
            run.id,
            previous.id,
        )
        # The substrate may have moved it already: without a record, the rework would serve it
        # with nothing to watch it by.
        try:
            async with session.begin_nested():
                await _hand_over(session, ticket, previous, latest, run)
        except Exception:
            logger.exception(
                "ticket %s run %s: handing a preview over failed", ticket.external_key, run.id
            )


async def _pending(session: AsyncSession, previous_id: int) -> tuple[StepRun, Event] | None:
    """The earlier run, and the latest event of a preview a person had going there: starting,
    serving, stopping, or stopped for a renewal the loop has yet to answer."""
    previous = await session.get(StepRun, previous_id)
    if previous is None:
        return None
    _, latest = await preview_lifecycle(session, previous)
    if latest is None or not (latest.type in _AWAKE or _renewed(latest)):
        return None
    return previous, latest


async def _carry(
    session: AsyncSession,
    ticket: Ticket,
    adapter: PreviewHost,
    previous: StepRun,
    latest: Event,
    run: StepRun,
    run_id: str,
    now: datetime,
) -> None:
    stopping = latest.type == EventType.PREVIEW_STOP_REQUESTED
    state = None if stopping else await _carried(session, ticket, adapter, run, run_id, now)
    if state is None or state.phase is not PreviewPhase.READY:
        await _hand_over(session, ticket, previous, latest, run)
        return
    await _answer(session, ticket, previous, EventType.PREVIEW_STOPPED, reason=PREVIEW_CONTINUED)
    await _publish(session, run, [(url.service, url.url) for url in state.urls])
    await _answer(
        session,
        ticket,
        run,
        EventType.PREVIEW_STARTED,
        carried_from=previous.id,
        services=[url.service for url in state.urls],
        head_sha=state.head_sha,
        uncommitted=state.uncommitted,
    )


async def _hand_over(
    session: AsyncSession, ticket: Ticket, previous: StepRun, latest: Event, run: StepRun
) -> None:
    """Record the earlier run's preview as continued, whatever the substrate says of it (its
    `renewal` must not start a preview on a run a later attempt continues), and leave the
    rework what is owed: the person's Stop, or else the loop's start."""
    await _answer(session, ticket, previous, EventType.PREVIEW_STOPPED, reason=PREVIEW_CONTINUED)
    if latest.type == EventType.PREVIEW_STOP_REQUESTED:
        await _answer(
            session,
            ticket,
            run,
            EventType.PREVIEW_STOP_REQUESTED,
            requested_by=latest.details.get("requested_by", "someone"),
            carried_from=previous.id,
        )
        return
    await _request(session, ticket, run)


async def _carried(
    session: AsyncSession,
    ticket: Ticket,
    adapter: PreviewHost,
    run: StepRun,
    run_id: str,
    now: datetime,
) -> PreviewState | None:
    """What the rework's preview is doing, read with the rework's keep-until: until one is
    given, the earlier run's idle close stands, and this read would apply it. None when the
    substrate cannot say, and the loop starts it again."""
    keep_until = await _keep_until(session, run, await _served(session, ticket, run), now)
    try:
        return await adapter.preview_status(run_id, keep_until=keep_until)
    except Exception:
        logger.warning("run %s: the carried preview was not read", run_id, exc_info=True)
        return None


async def _request(session: AsyncSession, ticket: Ticket, run: StepRun) -> None:
    """The loop's own Start. `renewal` keeps it off the idle clock, which counts a person's."""
    await _answer(
        session,
        ticket,
        run,
        EventType.PREVIEW_REQUESTED,
        requested_by=LOOP_REQUESTER,
        renewal=True,
    )


async def _answer(
    session: AsyncSession, ticket: Ticket, run: StepRun, kind: EventType, **details: object
) -> None:
    if kind in (EventType.PREVIEW_FAILED, EventType.PREVIEW_STOPPED):
        await _publish(session, run, [])
    if kind == EventType.PREVIEW_FAILED:
        # The first line alone: what follows is the sandbox's log text, which a service run with
        # the preview's secrets may have printed, and only the event keeps it.
        first = str(details.get("reason") or "").partition("\n")[0]
        logger.warning(
            "ticket %s run %s: the preview failed: %s", ticket.external_key, run.id, first
        )
    await append(
        session,
        type=kind,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        **details,
    )


async def _publish(session: AsyncSession, run: StepRun, links: list[PreviewLink]) -> None:
    await publish_preview_links(
        session, run, links, settings=get_settings(), published_via="worker"
    )


async def _served(session: AsyncSession, ticket: Ticket, run: StepRun) -> Preview | None:
    """The pinned step's `preview:`, when the runtime serves it."""
    config, *_ = await _load_pinned_config(session, ticket.process_version_id)
    step = config.find_step(run.step_name)
    preview = step.preview if isinstance(step, AgentStep) else None
    return preview if preview is not None and preview.services else None


async def _keep_until(
    session: AsyncSession, run: StepRun, preview: Preview | None, now: datetime
) -> datetime | None:
    """When the run's preview closes for idle, by the pinned step's `stopAfter`."""
    if preview is None:
        return None
    return await preview_keep_until(session, run, preview.stop_after, now=now)


async def _preview_spec(
    session: AsyncSession, ticket: Ticket, run: StepRun, preview: Preview | None
) -> tuple[PreviewSpec, dict[str, SecretStr]]:
    """`preview` (`_served`) as the contract's spec, and its env with every `{secrets.KEY}`
    rendered from the project's store."""
    if preview is None:
        raise _Refused(f"the pinned process gives {run.step_name!r} no preview services")
    spec = PreviewSpec(
        setup=preview.setup,
        services=tuple(
            PreviewServiceSpec(
                name=service.name,
                command=service.command,
                port=service.port,
                ready_path=service.ready.path,
                ready_timeout=service.ready.timeout,
            )
            for service in preview.services
        ),
        stop_after=preview.stop_after,
    )
    return spec, await _render_env(session, ticket.project_id, preview.env)


async def _render_env(
    session: AsyncSession, project_id: int, env: Mapping[str, str]
) -> dict[str, SecretStr]:
    keys = sorted({key for value in env.values() for key in SECRET_REF.findall(value)})
    stored = await resolve_for_run(session, project_id, keys)
    missing = [key for key in keys if key not in stored]
    if missing:
        raise _Refused(
            f"preview.env references secret(s) {', '.join(missing)}, which the project has not "
            "stored"
        )
    values = {key: value.get_secret_value() for key, value in stored.items()}
    return {
        name: SecretStr(SECRET_REF.sub(lambda match: values[match.group(1)], value))
        for name, value in env.items()
    }
