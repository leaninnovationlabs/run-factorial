"""The worker process: tick on an interval, wake early on NOTIFY (tech-stack decisions 3, 4).

The interval tick is the guarantee; the `LISTEN` connection only shortens latency. A payload that
names a ticket (`events.wake_ticket`, a dashboard refresh) gets that one ticket reconciled now,
beside the fleet loop; one that names an issue that is no ticket yet (`events.wake_admission`, a
webhook) gets that issue judged for admission now, and a ticket it admits refreshed at once; one
that asks for the inspect pass (`events.wake_inspect`) runs that pass, which serves the whole
deployment beside the ticks (`worker.inspect`); anything else wakes the whole fleet. Either way
the payload only says where
to look, and the pass it starts is one the timer would have run anyway, so a dropped, duplicated
or out-of-order notification is harmless (design.md §2.1). That is also why a failed `LISTEN`
connection is a warning, not a fatal error.

A fleet pass never starts within `worker.min_pass_gap_seconds` of the last one's start, woken or
not: a webhook delivery that names no ticket is a fleet wake, so a burst coalesces into one pass
instead of running passes back to back. A one-ticket refresh, a delivery that names a ticket, and
an admission check are not held to the gap: each costs one ticket's or one issue's reads, and
wakes for one still pending merge.

Each ticket is reconciled in its OWN transaction. One poisoned ticket — a config that no longer
parses, a GitHub 500, an adapter that hangs — must not roll back the others, and must not write an
event: an infrastructure failure is not a state transition, and the next tick retries it.

That retry is bounded (decision 31). `_record_failure` counts consecutive raising ticks, and at
`worker.quarantine_after` in a row stamps `ticket.quarantined_at`, which is what `acquire_leases`
excludes on — without it the same tick is attempted every interval for ever, and its side effects
outside the transaction (an `adapter.cancel`) are repeated every time.

A spawn is the one side effect a rollback would otherwise lose track of: its `step_run` row is the
only record of the run, and the next tick, finding none, launches another. So each pass records
its launches and, once its transaction has ended, cancels any whose row did not commit
(decision 87). A worker killed mid-pass never gets there: only E2B's reaper kills a live orphan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.backoff import ConnectionBackoff
from orchestrator.db.models import (
    Event,
    Project,
    StepRun,
    StepRunStatus,
    Ticket,
)
from orchestrator.db.session import SessionScope, session_scope
from orchestrator.events import WAKE_CHANNEL, WAKE_LISTENER, EventType, append
from orchestrator.events.listener import listen_forever
from orchestrator.github.client import is_rate_limited
from orchestrator.logs import bind
from orchestrator.settings import Settings, get_settings
from orchestrator.worker.admission import (
    CandidateFactory,
    admit_all,
    admit_issue,
)
from orchestrator.worker.context import ReconcileDeps, adapter_options, github_once_per_tick
from orchestrator.worker.inspect import inspect_forever
from orchestrator.worker.leases import acquire_lease, acquire_leases, awaiting_pass, release
from orchestrator.worker.parked import ParkedFactory, ParkedListings
from orchestrator.worker.reaper import Reapers, reap_forever
from orchestrator.worker.reconcile import reconcile_ticket
from orchestrator.worker.runtime_adapters import workspace_keys
from orchestrator.worker.tick_context import Launches, _load_project
from runtime.adapters import create_adapter
from runtime.contract import (
    AgentRuntimeError,
    CredentialSweeper,
    RuntimeAdapter,
    SubstrateUnavailable,
    WorkspaceReaper,
)

__all__ = ["DEFAULT_CONCURRENCY", "run_forever", "tick"]

logger = logging.getLogger(__name__)

#: Tickets reconciled at once. Bounded because each one holds a DB session and talks to GitHub.
#: The lease batch is a claim size, not a cap: a tick claims until the table has no more to give.
DEFAULT_CONCURRENCY = 8

#: An exception's text is untrusted and unbounded — a githubkit error carries a whole response
#: body. `events.writer` rejects credential-shaped keys but nothing caps a value's length.
_MAX_ERROR_CHARS = 500

#: Refresh passes at once, beside the fleet tick's own `DEFAULT_CONCURRENCY`.
REFRESH_CONCURRENCY = 2
#: How long a refresh keeps asking for a lease another pass holds. Longer than the API waits
#: (`api.wakes.WAIT_SECONDS`), so a pass that lands late still shows on the
#: page's next poll.
_REFRESH_LEASE_WAIT_SECONDS = 60.0
_REFRESH_RETRY_SECONDS = 1.0

#: Admission checks at once, beside the refreshes and the fleet tick.
ADMISSION_CONCURRENCY = 2
#: Issues one project may have waiting for a check. A request past it wakes the fleet instead,
#: whose pass lists every open issue: one tenant's signed flood must not grow the queue
#: (decision 83).
ADMISSION_QUEUE_PER_PROJECT = 20
#: A delivery's issue number is the sender's text.
_MAX_ISSUE_DIGITS = 12

#: First wait before reconnecting a lost `LISTEN`; it doubles up to the poll interval.
_LISTEN_RETRY_SECONDS = 1.0


async def tick(
    deps: ReconcileDeps,
    *,
    sessions: SessionScope | None = None,
    settings: Settings | None = None,
    owner: str | None = None,
    limit: int | None = None,
    ttl: timedelta | None = None,
    candidates: CandidateFactory | None = None,
    max_concurrency: int = DEFAULT_CONCURRENCY,
    parked: ParkedFactory | None = None,
) -> int:
    """One pass: admit (if a candidate source is wired), lease, reconcile, release.

    Returns the number of tickets reconciled — the loop's only progress signal.
    """
    resolved = settings if settings is not None else get_settings()
    scope = sessions if sessions is not None else session_scope
    lease_owner = owner if owner is not None else resolved.worker.name
    batch = limit if limit is not None else resolved.worker.batch_size
    lease_ttl = ttl if ttl is not None else timedelta(seconds=resolved.worker.lease_seconds)

    with github_once_per_tick():
        try:
            await admit_all(scope, deps, candidates)
        except Exception:
            logger.exception("admission pass failed")

        # Claim until a claim comes back short. One claim per tick would serve the same lowest
        # ids every tick — `acquire_leases` orders by id, the tick releases at its end — and
        # starve everything past the cap while gates and parked tickets stay not-done
        # (2026-08-31 incident). Each claim commits, so the next moves past the rows this tick
        # already holds.
        ticket_ids: list[int] = []
        try:
            while True:
                async with scope() as session:
                    leased = await acquire_leases(session, lease_owner, batch, lease_ttl)
                ticket_ids.extend(ticket.id for ticket in leased)
                if not leased or len(leased) < batch:
                    break
        except Exception:
            # The earlier batches are committed. Left leased they would sit out `lease_seconds`,
            # and every refresh on them would wait for a pass that never comes.
            with contextlib.suppress(Exception):
                async with scope() as session:
                    for ticket_id in ticket_ids:
                        await release(session, ticket_id, lease_owner)
            raise

        if not ticket_ids:
            return 0

        # One cache per tick: a listing is true for one tick, and `deps` outlives many.
        listings = ParkedListings(parked) if parked is not None else None

        semaphore = asyncio.Semaphore(max(1, min(max_concurrency, len(ticket_ids))))

        async def one(ticket_id: int) -> None:
            async with semaphore:
                await _reconcile_leased(
                    ticket_id,
                    deps,
                    scope=scope,
                    lease_owner=lease_owner,
                    lease_ttl=lease_ttl,
                    listings=listings,
                    quarantine_after=resolved.worker.quarantine_after,
                )

        await asyncio.gather(*(one(ticket_id) for ticket_id in ticket_ids))
        if listings is not None and listings.listings:
            logger.info(
                "parked listings: %d calls (%d failed), %d tickets served, %d fell back",
                listings.listings,
                listings.failed,
                listings.served,
                listings.fell_back,
            )
        return len(ticket_ids)


async def _reconcile_leased(
    ticket_id: int,
    deps: ReconcileDeps,
    *,
    scope: SessionScope,
    lease_owner: str,
    lease_ttl: timedelta,
    listings: ParkedListings | None,
    quarantine_after: int,
) -> None:
    """Reconcile one ticket the caller has leased, count a raise toward quarantine, release.

    The fleet tick and a refresh both run this, so a refresh is never a way around decision 31.
    """
    launches = Launches()
    with bind(ticket_id=ticket_id):
        try:
            try:
                async with scope() as session:
                    ticket = await session.get(Ticket, ticket_id)
                    if ticket is not None:
                        # The caller's own owner/ttl: `_start_attempt`'s mid-spawn renewal must
                        # renew the lease this pass actually holds.
                        await reconcile_ticket(
                            session,
                            ticket,
                            deps,
                            lease_owner=lease_owner,
                            lease_ttl=lease_ttl,
                            listings=listings,
                            launches=launches,
                        )
                        _clear_failures(ticket)
            finally:
                # Once the transaction has ended its run is committed or gone, and the count
                # sees what is true.
                deps.sandbox_starts.release(ticket_id)
                # After the commit or rollback, so no lock is held while a cancel blocks, and
                # before the release below, so no other pass launches while it does.
                await launches.settle(scope)
        except Exception as exc:
            if is_rate_limited(exc):
                logger.warning("reconciling ticket id=%s waits for its forge: %s", ticket_id, exc)
            else:
                logger.exception("reconciling ticket id=%s failed", ticket_id)
            # Its own unit of work, like the release below: the reconcile transaction has
            # already rolled back, so a counter written inside it would have vanished.
            error: str | None = None
            with contextlib.suppress(Exception):
                async with scope() as session:
                    quarantined = await _record_failure(
                        session, ticket_id, exc, deps=deps, limit=quarantine_after
                    )
                # Assigned only once that scope has COMMITTED, not inside it: a commit that
                # raises would otherwise announce a quarantine that never landed.
                error = quarantined
            if error is not None:
                # A third unit of work, after the row and the event are COMMITTED. See
                # `_announce_quarantine`: this call reaches GitHub, and the transaction
                # above holds `FOR UPDATE` on the very row an operator's requeue writes.
                with contextlib.suppress(Exception):
                    async with scope() as session:
                        await _announce_quarantine(session, ticket_id, deps=deps, error=error)
        finally:
            # Its own unit of work: the reconcile transaction may have rolled back, and the
            # lease must be dropped either way so the next pass can pick the ticket up.
            with contextlib.suppress(Exception):
                async with scope() as session:
                    await release(session, ticket_id, lease_owner)


def _clear_failures(ticket: Ticket) -> None:
    """Forget the failures a ticket has survived — the count is CONSECUTIVE ticks, not a total.

    Assigned only when there is something to clear: a healthy fleet is the common case, and an
    unconditional assignment would make every tick UPDATE every ticket to re-assert a zero.
    """
    if ticket.consecutive_failures:
        ticket.consecutive_failures = 0


async def _record_failure(
    session: AsyncSession,
    ticket_id: int,
    exc: BaseException,
    *,
    deps: ReconcileDeps,
    limit: int,
) -> str | None:
    """Count one raising pass and, at `limit` in a row, take the ticket out of the loop.

    Returns the truncated error when THIS call is the one that quarantined the ticket, so the
    caller can announce it once the row and the event have committed; None otherwise.

    Called from `_reconcile_leased`'s handler in a session of its own; see the module docstring.
    Already quarantined is a no-op, so the event is written once however the loop arrives here
    again.
    `with_for_update` closes the window a lease expiry opens: two runners whose error handlers
    overlap on the same ticket would otherwise both read `limit - 1` and both quarantine it.
    """
    ticket = await session.get(Ticket, ticket_id, with_for_update=True)
    if ticket is None or ticket.quarantined_at is not None or is_rate_limited(exc):
        # A backoff is the connection's, not the ticket's: counted, one tenant's spent token
        # would quarantine every ticket it holds.
        return None
    if isinstance(exc, SubstrateUnavailable):
        # Not the ticket's fault and not counted; said once per outage, not once per tick. An
        # outage ends at the ticket's next spawn, whatever else was logged in between.
        last = (
            await session.execute(
                select(Event.type)
                .where(
                    Event.ticket_id == ticket.id,
                    Event.type.in_([EventType.SUBSTRATE_UNAVAILABLE, EventType.AGENT_SPAWNED]),
                )
                .order_by(Event.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last != EventType.SUBSTRATE_UNAVAILABLE:
            await append(
                session,
                type=EventType.SUBSTRATE_UNAVAILABLE,
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                error=str(exc)[:_MAX_ERROR_CHARS],
            )
        return None
    ticket.consecutive_failures += 1
    if ticket.consecutive_failures < limit:
        return None

    ticket.quarantined_at = deps.now()
    error = str(exc)[:_MAX_ERROR_CHARS]
    await append(
        session,
        type=EventType.TICKET_QUARANTINED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        error=error,
        failures=ticket.consecutive_failures,
    )
    return error


async def _announce_quarantine(
    session: AsyncSession, ticket_id: int, *, deps: ReconcileDeps, error: str
) -> None:
    """Say on the issue that the loop has stopped picking this ticket up.

    In a session of its own, after `_record_failure`'s has committed. That call holds
    `SELECT … FOR UPDATE` on the ticket, so announcing inside it would keep the row locked for
    as long as GitHub takes to answer or time out — and `POST
    /requeue` is what an operator reaches for at that moment. `acquire_leases` is `SKIP LOCKED`
    and would survive; the requeue's UPDATE would queue behind the hang.
    """
    # Best effort by design. The case that motivated quarantine is GitHub itself refusing the
    # write (an archived repo), so this must never be what decides whether a ticket is
    # quarantined — the row and the event are the record; this is only the courtesy. The
    # SAVEPOINT keeps a failed read here from deactivating this session's transaction.
    with contextlib.suppress(Exception):
        async with session.begin_nested():
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                return
            project = await _load_project(session, ticket.project_id)
            bindings = await deps.bindings_for(session, project)
            if bindings is None:
                return
            projector = bindings.projector(project.github_repo)
            await projector.comment(
                int(ticket.external_key),
                "Factorial stopped working on this ticket after "
                f"{ticket.consecutive_failures} failed attempts in a row. Last error: `{error}`. "
                "Press Requeue ticket on the dashboard to try again.",
            )


async def run_forever(
    deps: ReconcileDeps,
    *,
    sessions: SessionScope | None = None,
    settings: Settings | None = None,
    candidates: CandidateFactory | None = None,
    parked: ParkedFactory | None = None,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    """Tick every `poll_interval_seconds`, or sooner when the API wakes us, but never within
    `min_pass_gap_seconds` of the last tick's start; serve refreshes beside the ticks."""
    resolved = settings if settings is not None else get_settings()
    # The sweep and the probe are hygiene: a worker that cannot run them still ticks.
    hosts: list[RuntimeAdapter] = []
    try:
        hosts = _host_adapters(resolved)
        # Before the listener and the refresher start, so no resume can be writing what it
        # deletes.
        _sweep_credentials(hosts)
    except Exception:
        logger.exception("the startup credential sweep failed")
    stopping = stop if stop is not None else asyncio.Event()
    wake = asyncio.Event()
    inspect_wake = asyncio.Event()
    refreshes = _Refreshes()
    admissions = _Admissions()
    scope = sessions if sessions is not None else session_scope
    try:
        await _sweep_run_credentials(scope, deps)
    except Exception:
        # Same hygiene as the credential-file sweep: a worker that cannot run it still ticks.
        logger.exception("the startup run-credential sweep failed")

    handlers = _install_signals(stopping) if install_signal_handlers else ()
    # After the handlers: a SIGTERM during its wait stops the worker once it returns.
    try:
        await _log_spawn_refusals(hosts)
    except Exception:
        logger.exception("the startup spawn-refusal probe failed")
    # A task copies the context it was created in, so each keeps its `task` field for life.
    with bind(task="listen"):
        listener = asyncio.create_task(
            _listen_for_wakeups(
                resolved,
                wake,
                refreshes,
                stopping,
                inspect_wake=inspect_wake,
                admissions=admissions,
            )
        )
    with bind(task="admit"):
        admitter = asyncio.create_task(
            _serve_admissions(deps, admissions, refreshes, stopping, scope=scope)
        )
    with bind(task="refresh"):
        refresher = asyncio.create_task(
            _serve_refreshes(
                deps,
                refreshes,
                stopping,
                scope=scope,
                settings=resolved,
            )
        )
    with bind(task="inspect"):
        inspecting = asyncio.create_task(
            inspect_forever(
                scope, deps, stopping, inspect_wake, interval=resolved.worker.poll_interval_seconds
            )
        )
    reapers = _reapers(resolved, deps.now)
    reaping: asyncio.Task[None] | None = None
    if reapers is not None:
        with bind(task="reap"):
            reaping = asyncio.create_task(
                reap_forever(
                    scope,
                    reapers,
                    stopping,
                    now=deps.now,
                    retention=resolved.workspace.retention,
                    retain=timedelta(days=resolved.runtime.e2b.retain_days),
                )
            )
    logger.info(
        "worker %s started: interval=%.0fs gap=%.0fs batch=%d",
        resolved.worker.name,
        resolved.worker.poll_interval_seconds,
        resolved.worker.min_pass_gap_seconds,
        resolved.worker.batch_size,
    )
    clock = asyncio.get_running_loop()
    try:
        while not stopping.is_set():
            # Cleared *before* the tick: a notification that lands while we are working must
            # survive and shorten the next wait, not be swallowed by this one.
            wake.clear()
            started = clock.time()
            try:
                await tick(
                    deps,
                    sessions=sessions,
                    settings=resolved,
                    candidates=candidates,
                    parked=parked,
                )
            except Exception:
                logger.exception("tick failed")
            await _wait(wake, stopping, interval=resolved.worker.poll_interval_seconds)
            # A wake inside the gap stays set, so the pass that ends the gap answers it.
            remaining = started + resolved.worker.min_pass_gap_seconds - clock.time()
            if remaining > 0:
                await _pause(stopping, remaining)
    finally:
        # SIGTERM finishes the in-flight tick (which releases its own leases) before we get here.
        stopping.set()
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        # And the refresh passes in flight, which release theirs the same way. A ticket admitted
        # during the stop is left to the next worker's first pass.
        await asyncio.gather(admitter, refresher, inspecting, return_exceptions=True)
        if reaping is not None:
            await asyncio.gather(reaping, return_exceptions=True)
        _remove_signals(handlers)
        logger.info("worker %s stopped", resolved.worker.name)


#: How long past `workspace.retention` the reaper keeps listing a team whose key no runtime
#: holds any more: its last kept sandboxes are released within the retention, and may then
#: still be inside `REAP_GRACE`, or have failed a kill.
RETIRED_TEAM_SLACK: Final = timedelta(days=1)


def _reapers(settings: Settings, now: Callable[[], datetime]) -> Reapers | None:
    """The workspace reapers, or None when this deployment keeps no workspace.

    The E2B ones when this deployment keeps E2B mirrors and names itself: first one without a
    key, which only prunes the mirrors and so cannot fail on a team; then one per E2B team:
    every key a runtime holds, the managed key while it is set, and a key replaced or removed
    in this process's life for `workspace.retention` and `RETIRED_TEAM_SLACK` after. Either E2B
    kind reaps both harnesses: sandboxes are fenced by deployment, not by harness. Then one per
    host kind with a `local_root`, each reading only its own runs' state, as the two may share a
    root. A host root has no deployment fence: it belongs to one deployment, whose keep set is
    the only one its reaper hears."""
    hosts: list[WorkspaceReaper] = [
        adapter for adapter in _host_adapters(settings) if isinstance(adapter, WorkspaceReaper)
    ]
    e2b = settings.runtime.e2b
    if not (e2b.deployment and e2b.state_root is not None):
        if not hosts:
            return None

        async def host_reapers(session: AsyncSession) -> list[WorkspaceReaper]:
            return hosts

        return host_reapers
    options = adapter_options(settings, "claude_e2b")
    last_held: dict[str, datetime] = {}
    held_for = settings.workspace.retention + RETIRED_TEAM_SLACK

    async def reapers(session: AsyncSession) -> list[WorkspaceReaper]:
        at = now()
        held = {key.get_secret_value() for key in await workspace_keys(session, e2b.managed)}
        last_held.update(dict.fromkeys(held, at))
        for key, seen in list(last_held.items()):
            if at - seen > held_for:
                del last_held[key]
        built = [
            create_adapter("claude_e2b", **{**options, "api_key": key})
            for key in [None, *sorted(last_held)]
        ]
        return [adapter for adapter in built if isinstance(adapter, WorkspaceReaper)] + hosts

    return reapers


def _host_adapters(settings: Settings) -> list[RuntimeAdapter]:
    """One adapter per host kind whose `local_root` is set."""
    built: list[RuntimeAdapter] = []
    for kind in ("claude_cli", "codex_cli"):
        options = adapter_options(settings, kind)
        if options["local_root"] is not None:
            built.append(create_adapter(kind, **options))
    return built


def _sweep_credentials(hosts: Sequence[RuntimeAdapter]) -> int:
    """Delete what credential files earlier ends left on the host (decision 54): those of runs
    that ended before every end deleted them, and of any park that failed."""
    deleted = sum(
        adapter.sweep_credentials() for adapter in hosts if isinstance(adapter, CredentialSweeper)
    )
    logger.info("startup sweep deleted %d credential file(s) of finished runs", deleted)
    return deleted


async def _log_spawn_refusals(hosts: Sequence[RuntimeAdapter]) -> None:
    """Ask each host kind once at boot, so CLI drift shows before the first ticket refuses."""
    answers = await asyncio.gather(
        *(adapter.spawn_refusal() for adapter in hosts), return_exceptions=True
    )
    for adapter, why in zip(hosts, answers, strict=True):
        if isinstance(why, AgentRuntimeError):
            why = str(why)
        elif isinstance(why, BaseException):
            raise why
        if why is not None:
            logger.warning(
                "%s cannot run a session on this host, so every spawn on it is refused: %s",
                type(adapter).__name__,
                why,
            )
async def _sweep_run_credentials(scope: SessionScope, deps: ReconcileDeps) -> None:
    """Revoke per-run forge credentials left by terminal runs while preserving live sessions.

    Each project sweeps in its own session, and a failure skips that project only: one broken
    connection must not leave every later project's run tokens unrevoked.
    """
    async with scope() as session:
        project_ids = list(await session.scalars(select(Project.id).order_by(Project.id)))
    deleted = 0
    for project_id in project_ids:
        try:
            async with scope() as session:
                deleted += await _sweep_project(session, deps, project_id)
        except ConnectionBackoff as exc:
            logger.warning("the run-credential sweep skips project id=%s: %s", project_id, exc)
        except Exception:
            logger.exception("the run-credential sweep skips project id=%s", project_id)
    logger.info("startup sweep revoked %d stale run credential(s)", deleted)


async def _sweep_project(session: AsyncSession, deps: ReconcileDeps, project_id: int) -> int:
    project = await session.get(Project, project_id)
    if project is None:
        return 0
    bindings = await deps.bindings_for(session, project)
    if bindings is None:
        return 0
    active = frozenset(
        await session.scalars(
            select(StepRun.id)
            .join(Ticket, Ticket.id == StepRun.ticket_id)
            .where(
                Ticket.project_id == project.id,
                StepRun.status.in_([StepRunStatus.RUNNING, StepRunStatus.IDLE]),
            )
        )
    )
    return await bindings.forge.sweep_run_credentials(project.github_repo, active_run_ids=active)


def _requested_inspect(payload: str) -> bool:
    """Whether a wake asks for the inspect pass (`events.wake_inspect`)."""
    try:
        body = json.loads(payload)
    except ValueError:
        return False
    # `type(...) is int`: a bool is an int too.
    return isinstance(body, dict) and type(body.get("inspect")) is int


def _requested_ticket(payload: str) -> tuple[int, datetime] | None:
    """The one ticket a wake asks about (`events.wake_ticket`), or None for a fleet wake."""
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    ticket_id, requested_at = body.get("ticket_id"), body.get("requested_at")
    # `type(...) is int`: a bool is an int too.
    if type(ticket_id) is not int or not isinstance(requested_at, str):
        return None
    try:
        at = datetime.fromisoformat(requested_at)
    except ValueError:
        return None
    # Compared with `ticket.last_leased_at`, so only the database's own aware time will do.
    return (ticket_id, at) if at.tzinfo is not None else None


def _requested_admission(payload: str) -> tuple[int, int] | None:
    """The project and issue number a wake asks admission about (`events.wake_admission`)."""
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    project_id, issue = body.get("admit"), body.get("issue")
    # `type(...) is int`: a bool is an int too.
    if type(project_id) is not int or not isinstance(issue, str):
        return None
    if not (issue.isascii() and issue.isdigit() and len(issue) <= _MAX_ISSUE_DIGITS):
        return None
    return project_id, int(issue)


class _Admissions:
    """Issues asked about and not yet taken up, per project, oldest first."""

    def __init__(self) -> None:
        self.pending: dict[int, dict[int, None]] = {}
        self.arrived = asyncio.Event()

    def add(self, project_id: int, number: int) -> bool:
        """False when the project's queue is full and the request is dropped."""
        waiting = self.pending.setdefault(project_id, {})
        if number not in waiting and len(waiting) >= ADMISSION_QUEUE_PER_PROJECT:
            logger.debug("project id=%s's admission queue is full; dropped #%s", project_id, number)
            return False
        waiting[number] = None
        self.arrived.set()
        return True

    def take(self, project_id: int, number: int) -> bool:
        waiting = self.pending.get(project_id)
        if waiting is None or number not in waiting:
            return False
        del waiting[number]
        if not waiting:
            del self.pending[project_id]
        return True


async def _serve_admissions(
    deps: ReconcileDeps,
    admissions: _Admissions,
    refreshes: _Refreshes,
    stopping: asyncio.Event,
    *,
    scope: SessionScope,
) -> None:
    """Judge each reported issue as it arrives, one task per issue at a time.

    A request is taken only once a slot is free, so the per-project cap bounds what waits, and
    one that lands mid-check runs again after it: the label it reports may be what the check
    just missed. On stop it takes up nothing new and waits for the checks in flight.
    """
    slots = asyncio.Semaphore(ADMISSION_CONCURRENCY)
    serving: dict[tuple[int, int], asyncio.Task[None]] = {}

    async def serve(project_id: int, number: int) -> None:
        try:
            while not stopping.is_set():
                async with slots:
                    if stopping.is_set() or not admissions.take(project_id, number):
                        break
                    try:
                        await _admit_reported(project_id, number, deps, refreshes, scope=scope)
                    except Exception:
                        logger.exception(
                            "admitting issue #%s of project id=%s failed", number, project_id
                        )
        finally:
            serving.pop((project_id, number), None)

    while True:
        await _wait(admissions.arrived, stopping, interval=None)
        if stopping.is_set():
            break
        admissions.arrived.clear()
        for project_id, waiting in list(admissions.pending.items()):
            for number in list(waiting):
                if (project_id, number) not in serving:
                    serving[project_id, number] = asyncio.create_task(serve(project_id, number))
    await asyncio.gather(*serving.values(), return_exceptions=True)


async def _admit_reported(
    project_id: int,
    number: int,
    deps: ReconcileDeps,
    refreshes: _Refreshes,
    *,
    scope: SessionScope,
) -> None:
    """Admit the issue if it is ready, and start what it admits at once: without the refresh, its
    first step would wait for a fleet pass, gap and all."""
    with github_once_per_tick():
        admitted = await admit_issue(scope, deps, project_id, number)
    if not admitted:
        return
    async with scope() as session:
        requested_at = (await session.execute(select(func.statement_timestamp()))).scalar_one()
    for ticket_id in admitted:
        refreshes.add(ticket_id, requested_at)


class _Refreshes:
    """Refreshes asked for and not yet taken up, with the newest request time per ticket."""

    def __init__(self) -> None:
        self.pending: dict[int, datetime] = {}
        self.arrived = asyncio.Event()

    def add(self, ticket_id: int, requested_at: datetime) -> None:
        current = self.pending.get(ticket_id)
        if current is None or requested_at > current:
            self.pending[ticket_id] = requested_at
        self.arrived.set()


def _refresh_owner(worker_name: str) -> str:
    # Its own lease owner, so `ticket.lease_owner` says which path holds a ticket.
    return f"{worker_name}/refresh"


async def _serve_refreshes(
    deps: ReconcileDeps,
    refreshes: _Refreshes,
    stopping: asyncio.Event,
    *,
    scope: SessionScope,
    settings: Settings,
) -> None:
    """Take up refreshes as they arrive, one task per ticket at a time.

    On stop it takes up nothing new and waits for the passes in flight.
    """
    slots = asyncio.Semaphore(REFRESH_CONCURRENCY)
    serving: dict[int, asyncio.Task[None]] = {}

    async def serve(ticket_id: int) -> None:
        try:
            # A request that arrives mid-pass is taken up by this loop, not by a second task —
            # so a failed attempt must not end the loop with that request still queued.
            while not stopping.is_set():
                requested_at = refreshes.pending.pop(ticket_id, None)
                if requested_at is None:
                    break
                try:
                    await _refresh(
                        ticket_id,
                        requested_at,
                        deps,
                        scope=scope,
                        settings=settings,
                        slots=slots,
                        stopping=stopping,
                    )
                except Exception:
                    logger.exception("refreshing ticket id=%s failed", ticket_id)
        finally:
            serving.pop(ticket_id, None)

    while True:
        await _wait(refreshes.arrived, stopping, interval=None)
        if stopping.is_set():
            break
        refreshes.arrived.clear()
        for ticket_id in list(refreshes.pending):
            if ticket_id not in serving:
                serving[ticket_id] = asyncio.create_task(serve(ticket_id))
    await asyncio.gather(*serving.values(), return_exceptions=True)


async def _refresh(
    ticket_id: int,
    requested_at: datetime,
    deps: ReconcileDeps,
    *,
    scope: SessionScope,
    settings: Settings,
    slots: asyncio.Semaphore,
    stopping: asyncio.Event,
) -> None:
    """One pass over one ticket, unless a pass that claimed it since `requested_at` answered it.

    A lease another pass holds is waited out: the fleet tick leases every ticket before it
    reconciles any, so a click mid-tick usually lands on a held ticket.
    """
    owner = _refresh_owner(settings.worker.name)
    ttl = timedelta(seconds=settings.worker.lease_seconds)
    clock = asyncio.get_running_loop()
    give_up_at = clock.time() + _REFRESH_LEASE_WAIT_SECONDS
    while True:
        async with slots:
            # Checked after the wait for a slot, which can outlast a SIGTERM: like the fleet
            # tick, a stopping worker finishes what it holds and claims nothing new.
            if stopping.is_set():
                return
            async with scope() as session:
                claimed = await acquire_lease(
                    session, ticket_id, owner, ttl, requested_at=requested_at
                )
            if claimed is not None:
                with github_once_per_tick():
                    await _reconcile_leased(
                        ticket_id,
                        deps,
                        scope=scope,
                        lease_owner=owner,
                        lease_ttl=ttl,
                        listings=None,
                        quarantine_after=settings.worker.quarantine_after,
                    )
                return
        async with scope() as session:
            unanswered = await awaiting_pass(session, ticket_id, requested_at)
        if not unanswered or stopping.is_set() or clock.time() >= give_up_at:
            return
        await _pause(stopping, _REFRESH_RETRY_SECONDS)


async def _pause(stopping: asyncio.Event, seconds: float) -> None:
    """Sleep `seconds`, or less if told to stop."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


async def _wait(wake: asyncio.Event, stopping: asyncio.Event, *, interval: float | None) -> None:
    """Sleep until woken, told to stop, or the interval (if any) elapses."""
    waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(stopping.wait())]
    try:
        await asyncio.wait(waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _listen_for_wakeups(
    settings: Settings,
    wake: asyncio.Event,
    refreshes: _Refreshes,
    stopping: asyncio.Event,
    *,
    inspect_wake: asyncio.Event | None = None,
    admissions: _Admissions | None = None,
) -> None:
    """Hold a `LISTEN factorial_wake` connection, and reconnect when it drops.

    Latency only — never authority over state: while it is down, the interval ticks on.
    """

    def on_notify(payload: str) -> None:
        # A hint about where to look; whatever pass it starts re-polls regardless.
        if inspect_wake is not None and _requested_inspect(payload):
            inspect_wake.set()
            return
        if admissions is not None and (asked := _requested_admission(payload)) is not None:
            if not admissions.add(*asked):
                wake.set()
            return
        requested = _requested_ticket(payload)
        if requested is None:
            wake.set()
        else:
            refreshes.add(*requested)

    connected_before = False

    def on_connect() -> None:
        # Whatever was sent while the connection was down is gone; one fleet pass and one
        # inspect pass cover it. The first connect has missed nothing the first tick will not see.
        nonlocal connected_before
        if connected_before:
            wake.set()
            if inspect_wake is not None:
                inspect_wake.set()
        connected_before = True

    await listen_forever(
        settings.database_url,
        WAKE_CHANNEL,
        on_notify=on_notify,
        on_connect=on_connect,
        stopping=stopping,
        probe_seconds=settings.worker.poll_interval_seconds,
        retry_seconds=_LISTEN_RETRY_SECONDS,
        application_name=WAKE_LISTENER,
        log=logger,
    )


def _install_signals(stopping: asyncio.Event) -> tuple[signal.Signals, ...]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Not the main thread, or a platform without signal handlers — the loop still runs.
            continue
        installed.append(sig)
    return tuple(installed)


def _remove_signals(signals: tuple[signal.Signals, ...]) -> None:
    if not signals:
        return
    loop = asyncio.get_running_loop()
    for sig in signals:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)
