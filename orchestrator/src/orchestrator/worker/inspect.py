"""Session views (tech-stack decision 57): the pass that answers Open session and watches every
open view, deployment-wide. A view is the run's own agent TUI reopened read-only on its kept
workspace, started with the run's model credential and never a GitHub token.

No ticket's pass can run it: a done ticket is never leased again, and decision 54 keeps its
workspace so that a merged change can still be opened. So it runs on its own (`inspect_forever`),
woken by the API's `wake_inspect` and otherwise every poll interval, and it reads only the log
and the keep rule. A shell changes no state the loop owns: each run's work holds a lock on its
row and sits in its own SAVEPOINT, committed before the next run's, and a failure is answered
with `inspect_parked` or logged, never raised. The API records requests and never calls a
substrate; this module is where the substrate is called.

The request's event id is the start's token, so a shell the pass started but never recorded is
found again on the next pass rather than started twice. `InspectState.detail` is read from a
machine the agent controlled, so no event carries it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, StepRun, Ticket, TicketStatus
from orchestrator.db.session import SessionScope
from orchestrator.events import EventType, append
from orchestrator.events.previews import (
    INSPECT_LIFECYCLE,
    MAX_AWAKE_WORKSPACES,
    awake_workspaces,
    inspect_lifecycle,
)
from orchestrator.events.read_models import is_one_of
from orchestrator.events.types import aware_time
from orchestrator.events.workspaces import kept_workspaces
from orchestrator.worker.context import ReconcileDeps, SessionPolicy
from orchestrator.worker.inputs import view_credentials
from orchestrator.worker.runtime_adapters import RuntimeRefused
from runtime.contract import (
    InspectHost,
    InspectLoginRefused,
    InspectNoSession,
    InspectPhase,
    InspectState,
    InspectUnavailable,
    RuntimeAdapter,
    UnknownRunError,
)

__all__ = [
    "INSPECT_LIMIT",
    "REQUEST_EXPIRES_AFTER",
    "ViewClosed",
    "inspect_forever",
    "reconcile_inspect_shells",
]

logger = logging.getLogger(__name__)

#: How long a session view stays open: the E2B session cap less its margin, as a preview's
#: `stopAfter`.
INSPECT_LIMIT: Final = timedelta(minutes=55)
#: A request no pass could answer for this long is refused: whoever pressed Open session has gone.
REQUEST_EXPIRES_AFTER: Final = timedelta(minutes=10)
#: Past its limit by this much, a session nothing can reach to stop is recorded as ended anyway:
#: E2B's own session cap has parked its sandbox.
_OVERDUE_AFTER: Final = INSPECT_LIMIT + timedelta(minutes=5)

AdapterForRun = Callable[[AsyncSession, StepRun], Awaitable[RuntimeAdapter]]

_MAX_REASON_CHARS: Final = 500


class ViewClosed(StrEnum):
    """`inspect_parked.reason` when the pass itself closed or refused the view, its parameters
    in details of their own; the dashboard words each. A refusal an exception words, the
    runtime's or the adapter's, is that text instead."""

    #: The terminal is switched off on this deployment.
    TERMINAL_OFF = "terminal_off"
    QUARANTINED = "quarantined"
    #: No pass answered the request within `after_seconds`.
    REQUEST_EXPIRED = "request_expired"
    #: `KeptWorkspaces.uninspectable`'s own three.
    RELEASED = "released"
    LIVE = "live"
    CONTINUED = "continued"
    #: `limit` previews and open sessions are already awake in the deployment.
    TOO_MANY_AWAKE = "too_many_awake"
    #: No model credential is stored for the run's runtime.
    NO_CREDENTIAL = "no_credential"
    #: The run's adapter cannot open a session at all.
    UNSUPPORTED = "unsupported"
    #: The run never got a substrate id.
    NO_SESSION = "no_session"
    #: The substrate could not reach the run's workspace.
    UNREACHABLE = "unreachable"
    #: The substrate no longer has the run's workspace.
    GONE = "gone"
    #: `closed_by` asked to close it.
    CLOSED = "closed"
    START_FAILED = "start_failed"
    ENDED_BEFORE_OPEN = "ended_before_open"
    ENDED = "ended"
    VIEW_FAILED = "view_failed"
    #: It reached `INSPECT_LIMIT`, `limit_seconds`.
    LIMIT = "limit"
    #: Nobody watched it for `after_seconds`.
    UNWATCHED = "unwatched"
    #: It passed its limit, and nothing could reach it to stop it.
    OVERDUE = "overdue"


#: Why an open view closes, and that reason's parameters.
_Closed = tuple[ViewClosed, dict[str, object]]


class _Refused(Exception):
    """Nothing will open this shell, for `reason` and its `details`."""

    def __init__(self, reason: str, **details: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class _Deferred(Exception):
    """Not this pass: the ticket's own pass holds it, and may be adopting the workspace."""


async def reconcile_inspect_shells(
    session: AsyncSession,
    *,
    adapter_for_run: AdapterForRun,
    policy: SessionPolicy,
    now: datetime,
) -> None:
    """Answer every unanswered request and watch every open shell, then commit each run.

    A run whose row another transaction holds — the API recording a request — waits for the
    next pass.
    """
    for step_run_id in await _awaiting(session):
        run = (
            await session.execute(
                select(StepRun)
                .where(StepRun.id == step_run_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is not None:
            await _one(session, run, adapter_for_run, policy, now)
        await session.commit()


async def _awaiting(session: AsyncSession) -> list[int]:
    """Runs whose latest inspect event is a request or a start."""
    latest = (
        select(Event.step_run_id, Event.type)
        .where(is_one_of(INSPECT_LIFECYCLE))
        .distinct(Event.step_run_id)
        .order_by(Event.step_run_id, Event.id.desc())
        .subquery()
    )
    rows = await session.execute(
        select(latest.c.step_run_id)
        .where(latest.c.type.in_([EventType.INSPECT_REQUESTED, EventType.INSPECT_STARTED]))
        .order_by(latest.c.step_run_id)
    )
    return [step_run_id for (step_run_id,) in rows if step_run_id is not None]


async def _one(
    session: AsyncSession,
    run: StepRun,
    adapter_for_run: AdapterForRun,
    policy: SessionPolicy,
    now: datetime,
) -> None:
    # Read again under the lock: the list was read before it.
    status, latest = await inspect_lifecycle(session, run)
    if latest is None or status not in ("starting", "open"):
        return
    ticket = (await session.execute(select(Ticket).where(Ticket.id == run.ticket_id))).scalar_one()
    requested = status == "starting"
    try:
        async with session.begin_nested():
            # Before a start and before a stop alike: a rework adopting the workspace under the
            # lease kills the shell, and a stop now would pause the sandbox it is starting in.
            if ticket.status != TicketStatus.DONE and not await _lease_free(session, ticket):
                raise _Deferred
            if requested:
                await _answer(session, ticket, run, latest, adapter_for_run, policy, now)
            else:
                await _watch(session, ticket, run, latest, adapter_for_run, policy, now)
    except _Deferred:
        logger.debug("step run %s: its ticket is leased; the inspect pass waits", run.id)
    except _Refused as exc:
        await _park(
            session, ticket, run, latest, reason=exc.reason, failed=requested, details=exc.details
        )
    except RuntimeRefused as exc:
        # Orchestrator-written, so it can be shown as it stands.
        await _park(
            session, ticket, run, latest, reason=str(exc)[:_MAX_REASON_CHARS], failed=requested
        )
    except UnknownRunError:
        await _park(session, ticket, run, latest, reason=ViewClosed.GONE, failed=requested)
    except (InspectLoginRefused, InspectNoSession) as exc:
        # Fixed text, never the substrate's: the adapter refusals a person can read as they are.
        await _park(session, ticket, run, latest, reason=str(exc), failed=requested)
    except InspectUnavailable as exc:
        # Its message may carry what the substrate printed: logged, never recorded.
        logger.warning("step run %s: the substrate cannot open a session: %s", run.id, exc)
        await _park(session, ticket, run, latest, reason=ViewClosed.UNREACHABLE, failed=requested)
    except Exception:
        # An E2B outage may pass while the session still runs: the next pass looks again, so an
        # open session is still parked at its limit.
        logger.exception("step run %s: the inspect pass failed", run.id)
        if not requested and _overdue(latest, now):
            await _park(session, ticket, run, latest, reason=ViewClosed.OVERDUE)


async def _answer(
    session: AsyncSession,
    ticket: Ticket,
    run: StepRun,
    request: Event,
    adapter_for_run: AdapterForRun,
    policy: SessionPolicy,
    now: datetime,
) -> None:
    if now - request.created_at >= REQUEST_EXPIRES_AFTER:
        raise _Refused(ViewClosed.REQUEST_EXPIRED, after_seconds=_seconds(REQUEST_EXPIRES_AFTER))
    if not policy.terminal_enabled:
        raise _Refused(ViewClosed.TERMINAL_OFF)
    if ticket.quarantined_at is not None:
        raise _Refused(ViewClosed.QUARANTINED)
    kept = await kept_workspaces(session, now, policy.retention, ticket_id=ticket.id)
    why = kept.uninspectable(run.id)
    if why is not None:
        raise _Refused(ViewClosed(why))
    host = await _host(adapter_for_run, session, run)
    run_id = _runtime_run_id(run)
    if (closer := await _closed_by(session, run, request)) is not None:
        # Stopped as well as parked: a start a crashed pass never recorded may have opened a shell.
        await host.inspect_stop(run_id)
        await _park(
            session, ticket, run, request, reason=ViewClosed.CLOSED, details={"closed_by": closer}
        )
        return
    token = str(request.id)
    state = await host.inspect_status(run_id)
    if state.token != token:
        if len(await awake_workspaces(session) - {run.id}) >= MAX_AWAKE_WORKSPACES:
            raise _Refused(ViewClosed.TOO_MANY_AWAKE, limit=MAX_AWAKE_WORKSPACES)
        credentials = await view_credentials(session, run)
        if not credentials:
            raise _Refused(ViewClosed.NO_CREDENTIAL)
        state = await host.inspect_start(
            run_id, token=token, limit=INSPECT_LIMIT, credentials=credentials
        )
    if state.phase is InspectPhase.OPEN:
        assert state.target is not None
        await _emit(
            session,
            ticket,
            run,
            EventType.INSPECT_STARTED,
            kind="session",
            requested_by=request.details.get("requested_by"),
            request_id=request.id,
            terminal=state.target.model_dump(),
            # The limit is measured from here as well as from the substrate's own clock.
            started_at=now.isoformat(),
        )
    elif state.phase is InspectPhase.FAILED:
        await _park(session, ticket, run, request, reason=ViewClosed.START_FAILED, failed=True)
    elif state.phase is InspectPhase.STOPPED:
        await _park(
            session, ticket, run, request, reason=ViewClosed.ENDED_BEFORE_OPEN, failed=True
        )


async def _lease_free(session: AsyncSession, ticket: Ticket) -> bool:
    """Lock the ticket unless its own pass holds it. A rework adopts the workspace under that
    lease and records the adoption only once its spawn returns, so a shell started or stopped
    meanwhile would act on a workspace a live run is taking over. The database's clock, as
    leases use."""
    free = (
        await session.execute(
            select(Ticket.id)
            .where(
                Ticket.id == ticket.id,
                or_(Ticket.lease_owner.is_(None), Ticket.lease_expires_at <= func.now()),
            )
            .with_for_update(skip_locked=True)
        )
    ).first()
    return free is not None


def _overdue(started: Event, now: datetime) -> bool:
    began = aware_time(started.details.get("started_at"))
    return began is not None and now - began >= _OVERDUE_AFTER


async def _watch(
    session: AsyncSession,
    ticket: Ticket,
    run: StepRun,
    started: Event,
    adapter_for_run: AdapterForRun,
    policy: SessionPolicy,
    now: datetime,
) -> None:
    kept = await kept_workspaces(session, now, policy.retention, ticket_id=ticket.id)
    why = kept.uninspectable(run.id)
    if why in ("live", "continued"):
        # The resume that made a run live in this workspace ended the shell before it wrote a
        # credential, and a stop now would park the sandbox that run works in.
        await _park(session, ticket, run, started, reason=ViewClosed(why))
        return
    host = await _host(adapter_for_run, session, run)
    run_id = _runtime_run_id(run)
    closed: _Closed | None
    if why == "released":
        closed = ViewClosed.RELEASED, {}
    elif (closer := await _closed_by(session, run, started)) is not None:
        closed = ViewClosed.CLOSED, {"closed_by": closer}
    else:
        closed = _why_closed(await host.inspect_status(run_id), started, policy, now)
    if closed is not None:
        reason, details = closed
        await host.inspect_stop(run_id)
        await _park(session, ticket, run, started, reason=reason, details=details)


async def _closed_by(session: AsyncSession, run: StepRun, latest: Event) -> str | None:
    """Who asked to close the view, when that request came after its latest lifecycle event."""
    request = (
        await session.scalars(
            select(Event)
            .where(
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type == EventType.INSPECT_CLOSE_REQUESTED,
                Event.id > latest.id,
            )
            .order_by(Event.id)
            .limit(1)
        )
    ).first()
    return None if request is None else str(request.details.get("requested_by"))


def _why_closed(
    state: InspectState, started: Event, policy: SessionPolicy, now: datetime
) -> _Closed | None:
    if state.phase is InspectPhase.STOPPED:
        return ViewClosed.ENDED, {}
    if state.phase is InspectPhase.FAILED:
        return ViewClosed.VIEW_FAILED, {}
    recorded = aware_time(started.details.get("started_at"))
    began = min((at for at in (state.started_at, recorded) if at is not None), default=None)
    if began is not None and now - began >= INSPECT_LIMIT:
        return ViewClosed.LIMIT, {"limit_seconds": _seconds(INSPECT_LIMIT)}
    active = max((at for at in (state.last_activity, began) if at is not None), default=None)
    if state.clients == 0 and active is not None and now - active >= policy.idle_park_after:
        return ViewClosed.UNWATCHED, {"after_seconds": _seconds(policy.idle_park_after)}
    return None


async def _host(adapter_for_run: AdapterForRun, session: AsyncSession, run: StepRun) -> InspectHost:
    adapter = await adapter_for_run(session, run)
    if not isinstance(adapter, InspectHost):
        raise _Refused(ViewClosed.UNSUPPORTED)
    return adapter


def _runtime_run_id(run: StepRun) -> str:
    if run.runtime_run_id is None:
        raise _Refused(ViewClosed.NO_SESSION)
    return run.runtime_run_id


async def _park(
    session: AsyncSession,
    ticket: Ticket,
    run: StepRun,
    latest: Event,
    *,
    reason: str,
    failed: bool = False,
    details: Mapping[str, object] | None = None,
) -> None:
    request_id = latest.id if latest.type == EventType.INSPECT_REQUESTED else None
    if request_id is None and isinstance(latest.details.get("request_id"), int):
        request_id = latest.details["request_id"]
    await _emit(
        session,
        ticket,
        run,
        EventType.INSPECT_PARKED,
        reason=reason,
        **(details or {}),
        **({"request_id": request_id} if request_id is not None else {}),
        **({"failed": True} if failed else {}),
    )


async def _emit(
    session: AsyncSession, ticket: Ticket, run: StepRun, event_type: EventType, **details: object
) -> None:
    await append(
        session,
        type=event_type,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        **details,
    )


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())


async def inspect_forever(
    scope: SessionScope,
    deps: ReconcileDeps,
    stopping: asyncio.Event,
    wake: asyncio.Event,
    *,
    interval: float,
) -> None:
    """The pass every `interval` seconds, or at once when the API wakes it, until `stopping`."""
    while not stopping.is_set():
        # Cleared before the pass, so a request that lands during it shortens the next wait.
        wake.clear()
        try:
            async with scope() as session:
                await reconcile_inspect_shells(
                    session,
                    adapter_for_run=deps.adapter_for_run,
                    policy=deps.sessions,
                    now=deps.now(),
                )
        except Exception:
            # A missed pass only delays an answer; the next one reads the same log.
            logger.exception("the inspect pass failed")
        waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(stopping.wait())]
        try:
            await asyncio.wait(waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
