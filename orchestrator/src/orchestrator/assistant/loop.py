"""The assistant's process: claim queued turns, run them, and keep the chat tables clean
(tech-stack decisions 3 and 63).

At start it runs each backend's preflight and writes the outcome, per backend, to
`app_setting["assistant_status"]`, which `/readyz` and the Assistant settings page read. A
backend that fails is refused, and the API refuses a send for an organization on it, rather than
queue turns nothing will run; the other backends still serve. When every backend fails, the
process stays up but idle until it is stopped, and the row names why.
Then it serves. A `LISTEN` on the queue channel wakes it early, and the tick is the guarantee
(decision 4's shape). Each tick interrupts the turns whose lease ran out (a process that died,
this one's predecessor included), expires the proposals nobody decided in time, and claims turns
while fewer than `concurrency` run here; the first tick, and then one an hour, also deletes the
conversations past retention.

On stop it asks each running turn's backend to stop and gives the turns `STOP_GRACE` to end;
then it cancels what is left. A turn stopped either way ends interrupted, with the restart
notice, as a turn the next process recovers would.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Final

from sqlalchemy.exc import IntegrityError

from orchestrator.api.services.assistant_settings import save_assistant_status
from orchestrator.assistant.proposals import expire_stale
from orchestrator.assistant.retention import purge_expired
from orchestrator.assistant.runner import RunnerDeps, run_turn
from orchestrator.assistant.turns import claim_turn, recover_lost_turns
from orchestrator.assistant.vocabulary import BACKENDS
from orchestrator.events.channels import CHAT_QUEUE_CHANNEL
from orchestrator.events.listener import listen_forever
from orchestrator.settings import Settings
from runtime.assistant.contract import ChatBackend
from runtime.contract import AdapterUnavailable

__all__ = ["PURGE_EVERY_SECONDS", "STOP_GRACE", "serve"]

log = logging.getLogger(__name__)

STOP_GRACE: Final = 10.0
#: Retention is counted in days; the purge needs no tighter clock than this.
PURGE_EVERY_SECONDS: Final = 3600.0


def _owner() -> str:
    """Unique per process, so a restarted process never mistakes its predecessor's leases for
    its own."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def serve(deps: RunnerDeps, *, stop: asyncio.Event, settings: Settings) -> None:
    failed = await _preflight(deps)
    if len(failed) == len(BACKENDS):
        await stop.wait()
        return
    ready = [kind for kind in BACKENDS if kind not in failed]
    if failed:
        deps = replace(deps, backend_for=_refusing(deps.backend_for, failed))
    process = settings.assistant
    owner = _owner()
    wake = asyncio.Event()
    running: dict[int, asyncio.Task[None]] = {}

    def heard(_payload: str) -> None:
        wake.set()
        deps.wakes.fire()

    def connected() -> None:
        heard("")

    listener = asyncio.create_task(
        listen_forever(
            settings.database_url,
            CHAT_QUEUE_CHANNEL,
            on_notify=heard,
            on_connect=connected,
            stopping=stop,
            probe_seconds=max(process.tick_seconds, 15.0),
        )
    )

    def start(turn_id: int) -> None:
        task = asyncio.create_task(run_turn(turn_id, deps, owner=owner))
        running[turn_id] = task

        def finished(done: asyncio.Task[None]) -> None:
            running.pop(turn_id, None)
            wake.set()
            if not done.cancelled() and done.exception() is not None:
                log.error("assistant turn %s raised", turn_id, exc_info=done.exception())

        task.add_done_callback(finished)

    log.info("assistant %s serving: concurrency=%d", owner, process.concurrency)
    clock = asyncio.get_running_loop()
    purged_at: float | None = None
    try:
        while not stop.is_set():
            # Cleared before the tick: a NOTIFY that lands during it must shorten the next wait.
            wake.clear()
            if purged_at is None or clock.time() - purged_at >= PURGE_EVERY_SECONDS:
                # Stamped first: a purge that keeps failing is retried hourly, not every tick.
                purged_at = clock.time()
                try:
                    async with deps.sessions() as session:
                        await purge_expired(
                            session, now=deps.clock(), retain_days=process.retain_days
                        )
                except Exception:
                    log.exception("assistant retention purge failed")
            try:
                await _tick(deps, settings, owner=owner, running=running, start=start, stop=stop)
            except Exception:
                log.exception("assistant tick failed")
            await _wait(wake, stop, process.tick_seconds)
    finally:
        stop.set()
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await _drain(deps, running, ready)
        log.info("assistant %s stopped", owner)


async def _tick(
    deps: RunnerDeps,
    settings: Settings,
    *,
    owner: str,
    running: dict[int, asyncio.Task[None]],
    start: Callable[[int], None],
    stop: asyncio.Event,
) -> None:
    process = settings.assistant
    async with deps.sessions() as session:
        await recover_lost_turns(session, now=deps.clock())
    try:
        async with deps.sessions() as session:
            await expire_stale(session, now=deps.clock())
    except Exception:
        # The claims below must not wait on a proposal that cannot be expired.
        log.exception("assistant proposal expiry failed")
    lease = timedelta(seconds=process.lease_seconds)
    while len(running) < process.concurrency and not stop.is_set():
        try:
            async with deps.sessions() as session:
                turn = await claim_turn(session, owner=owner, lease=lease, now=deps.clock())
        except IntegrityError:
            # `chat_turn_one_running`: another process's claim of the same conversation landed
            # first. The scope has rolled this one back; the next tick looks again.
            log.info("assistant claim lost a race to another process")
            return
        if turn is None:
            return
        start(turn.id)


async def _preflight(deps: RunnerDeps) -> dict[str, str]:
    """Each backend's check, written to the status row: the backends that failed, by kind, with
    why. A send is refused only when its organization's backend is among them."""
    problems: dict[str, str] = {}
    versions: list[str] = []
    for kind in BACKENDS:
        try:
            backend = deps.backend_for(kind)
            versions.append(f"{kind} {backend.pinned_version}")
            await backend.preflight()
        except Exception as exc:
            problems[kind] = str(exc)
    status = {
        "ok": not problems,
        "message": "; ".join(f"{kind}: {why}" for kind, why in problems.items()) or None,
        "version": ", ".join(versions),
        "at": deps.clock().isoformat(),
        "backends": {
            kind: {"ok": kind not in problems, "message": problems.get(kind)} for kind in BACKENDS
        },
    }
    async with deps.sessions() as session:
        await save_assistant_status(session, status)
    if len(problems) == len(BACKENDS):
        log.error("assistant preflight failed; serving no turns: %s", status["message"])
    elif problems:
        log.error(
            "assistant preflight failed for some backends; refusing their turns: %s",
            status["message"],
        )
    return problems


def _refusing(
    backend_for: Callable[[str], ChatBackend], failed: dict[str, str]
) -> Callable[[str], ChatBackend]:
    """`backend_for`, refusing the backends that failed their check: the runner fails such a
    turn with the reason rather than starting a backend that cannot run it."""

    def backend(kind: str) -> ChatBackend:
        if kind in failed:
            raise AdapterUnavailable(f"The {kind} backend failed its check: {failed[kind]}")
        return backend_for(kind)

    return backend


async def _drain(
    deps: RunnerDeps, running: dict[int, asyncio.Task[None]], ready: list[str]
) -> None:
    tasks = list(running.items())
    if not tasks:
        return
    # A turn's backend is not known here, so each ready backend is asked once per turn; two kinds
    # served by one object are one backend.
    backends = list({id(backend): backend for backend in map(deps.backend_for, ready)}.values())
    for turn_id, _ in tasks:
        for backend in backends:
            with contextlib.suppress(Exception):
                await backend.cancel(str(turn_id))
    _, left = await asyncio.wait([task for _, task in tasks], timeout=STOP_GRACE)
    for task in left:
        task.cancel()
    await asyncio.gather(*left, return_exceptions=True)


async def _wait(wake: asyncio.Event, stop: asyncio.Event, seconds: float) -> None:
    waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(stop.wait())]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
