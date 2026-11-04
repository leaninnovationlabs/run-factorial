"""One viewer's connection: the handshake, the relay, the queue to the socket, the recheck, the
writer seat, and the audit (decision 57, design.md §7.14).

The handshake runs in order and refuses as early as it can: the Origin, then the credential —
the `sf_session` cookie, else a ticket in `Sec-WebSocket-Protocol` that fits this run and was
never used — then, for the cookie, `tickets:terminal` in the run's organization (design.md
§7.19), all before `accept()`, so a cross-site page, a bare bearer token or a stranger's
session never reaches a socket; then the admission rule after it, so the dashboard can say
why. A ticket's permission was checked by the guarded route that minted it. The recheck asks
again for either credential while the viewer stays, so a person removed or demoted, or one who
signs out everywhere, loses an open terminal within `RECHECK_SECONDS`, not when their session
expires a week later.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocket

from orchestrator.access import Permission
from orchestrator.access.accounts import session_ended, verified_since
from orchestrator.access.decision import allowed_on_run, caller_named
from orchestrator.api.sessions import (
    SESSION_COOKIE,
    TICKET_PROTOCOL,
    TICKET_SECONDS,
    TerminalTicket,
    read_terminal_ticket,
    verified_session,
    verify_session,
)
from orchestrator.events.terminals import Admission, TerminalRun, terminal_admission
from orchestrator.settings import Settings
from orchestrator.terminal.audit import InputBursts, ViewerAudit
from orchestrator.terminal.protocol import (
    AUDIT_MAX,
    AUDIT_QUIET,
    MAX_VIEWERS,
    RECHECK_SECONDS,
    SEAT_IDLE,
    VIEWER_QUEUE,
    Claim,
    CloseCode,
    Release,
    Resize,
    close_reason,
    exit_frame,
    hello_frame,
    parse_frame,
    parse_size,
    seat_frame,
    status_frame,
)
from runtime.contract import TerminalTarget
from runtime.terminal import TerminalRelay, TerminalStream, TerminalUnavailable

__all__ = ["Gateway", "Registry", "Relays", "TicketLedger", "Timing", "Viewer", "serve"]

log = logging.getLogger(__name__)

#: Seconds a close frame, or the last output before an `exit`, may take to reach a client.
_SEND_GRACE: Final = 2.0

RESTARTING: Final = "the terminal service is restarting"
MOVED: Final = "this run's terminal moved"
NO_ACCESS: Final = "you no longer have access to this run's terminal"
UNREACHABLE: Final = "the terminal cannot be reached now"
UNCHECKED: Final = "couldn't check this run's terminal just now"
HELD: Final = "{by} has the keyboard"
IDLE: Final = "the keyboard was freed after {seconds:g} seconds without typing"


class Relays(Protocol):
    async def relay_for(
        self, session: AsyncSession, run: TerminalRun, target: TerminalTarget
    ) -> TerminalRelay:
        """The relay for `target`'s substrate. `ValueError` for one the gateway will not
        attach to; `TerminalUnavailable` when it cannot reach it now."""
        ...


@dataclass(frozen=True, slots=True)
class Timing:
    recheck: float = RECHECK_SECONDS
    audit_quiet: float = AUDIT_QUIET
    audit_max: float = AUDIT_MAX
    queue: int = VIEWER_QUEUE
    seat_idle: float = SEAT_IDLE


class Viewer:
    """A connection's outbound frames. `offer` is called from the relay's output callback, so it
    never waits: a viewer that falls `maxsize` frames behind is closed, and the others, each on
    a tmux client and queue of its own, never notice."""

    def __init__(self, *, maxsize: int, close: Callable[[CloseCode, str], None]) -> None:
        self._queue: asyncio.Queue[bytes | str | None] = asyncio.Queue(maxsize)
        self._close = close
        self.overflowed = False

    def offer(self, frame: bytes | str) -> None:
        if self.overflowed:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.overflowed = True
            self._close(CloseCode.TOO_SLOW, "this viewer fell behind the terminal's output")

    def finish(self) -> None:
        """The sender returns once it has sent what is queued, if it fits."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    async def send(
        self,
        first: str,
        send_bytes: Callable[[bytes], Awaitable[None]],
        send_text: Callable[[str], Awaitable[None]],
    ) -> None:
        await send_text(first)
        while (frame := await self._queue.get()) is not None:
            if isinstance(frame, bytes):
                await send_bytes(frame)
            else:
                await send_text(frame)


class Credential(Protocol):
    via: ClassVar[str]
    #: The 4401 reason once `valid` turns false.
    ended: ClassVar[str]

    @property
    def by(self) -> str: ...

    def valid(self, api_token: str, now: datetime) -> bool:
        """Still good, re-checked every `RECHECK_SECONDS` while the viewer is attached."""
        ...


@dataclass(frozen=True, slots=True)
class SessionCookie:
    value: str
    by: str
    verified: bool
    epoch: int
    via: ClassVar[str] = "cookie"
    ended: ClassVar[str] = "your session expired: sign in again"

    def valid(self, api_token: str, now: datetime) -> bool:
        return verify_session(self.value, api_token=api_token, now=now) is not None


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket: TerminalTicket
    via: ClassVar[str] = "ticket"
    ended: ClassVar[str] = "the session that opened this terminal expired: sign in again"

    @property
    def by(self) -> str:
        return self.ticket.by

    @property
    def minted(self) -> datetime:
        return datetime.fromtimestamp(self.ticket.expires - TICKET_SECONDS, UTC)

    def valid(self, api_token: str, now: datetime) -> bool:
        # The ticket's 30 seconds bound only its handshake. The API token cannot change under a
        # running gateway, which reads it once at start.
        return self.ticket.lasts(now)


class TicketLedger:
    """The nonces of tickets already used, each kept until its ticket expires. One gateway
    process serves the deployment, so this is the whole ledger; a restart forgets it, so a ticket
    used just before one can open one more connection within its 30 seconds."""

    def __init__(self) -> None:
        self._spent: dict[str, int] = {}

    def spend(self, ticket: TerminalTicket, now: datetime) -> bool:
        """False when the ticket was used before. No await between the check and the spend, so
        two handshakes racing with one ticket cannot both pass."""
        stamp = now.timestamp()
        for nonce in [n for n, expires in self._spent.items() if expires <= stamp]:
            del self._spent[nonce]
        if ticket.nonce in self._spent:
            return False
        self._spent[ticket.nonce] = ticket.expires
        return True


def _offered_ticket(websocket: WebSocket) -> str | None:
    """The ticket from `Sec-WebSocket-Protocol: factorial.terminal, <ticket>`, and nothing else
    of any other shape."""
    offered = websocket.scope.get("subprotocols") or []
    if len(offered) == 2 and offered[0] == TICKET_PROTOCOL:
        return offered[1]
    return None


class Registry:
    """Who is attached to which run, in this process, and who holds each run's writer seat. One
    gateway process serves the deployment, so the counts are whole and a run has one seat."""

    def __init__(self) -> None:
        self._runs: defaultdict[int, set[Connection]] = defaultdict(set)
        #: Connections holding one of a run's `MAX_VIEWERS` places, attached or attaching.
        self._held: defaultdict[int, set[Connection]] = defaultdict(set)
        self.seat: dict[int, Connection] = {}

    def holder(self, step_run_id: int) -> Connection | None:
        return self.seat.get(step_run_id)

    def take(self, connection: Connection) -> None:
        self.seat[connection.step_run_id] = connection

    def free(self, connection: Connection) -> bool:
        """False when `connection` did not hold its run's seat."""
        if self.seat.get(connection.step_run_id) is not connection:
            return False
        del self.seat[connection.step_run_id]
        return True

    def reserve(self, connection: Connection) -> bool:
        held = self._held[connection.step_run_id]
        if len(held) >= MAX_VIEWERS:
            return False
        held.add(connection)
        return True

    def join(self, connection: Connection) -> None:
        self._runs[connection.step_run_id].add(connection)
        self.changed(connection.step_run_id)

    def leave(self, connection: Connection) -> None:
        self.free(connection)
        held = self._held.get(connection.step_run_id)
        if held is not None:
            held.discard(connection)
            if not held:
                del self._held[connection.step_run_id]
        peers = self._runs.get(connection.step_run_id)
        if peers is None or connection not in peers:
            return
        peers.discard(connection)
        if not peers:
            del self._runs[connection.step_run_id]
        self.changed(connection.step_run_id)

    def counts(self, step_run_id: int) -> tuple[int, int]:
        return len(self._runs.get(step_run_id, set())), int(step_run_id in self.seat)

    def names(self, step_run_id: int) -> list[str]:
        """Who is attached: a name per viewer, so one person in two tabs is named twice."""
        return sorted(peer.by for peer in self._runs.get(step_run_id, set()))

    def changed(self, step_run_id: int) -> None:
        for peer in self._runs.get(step_run_id, set()):
            peer.push_status()


@dataclass(slots=True)
class Gateway:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    relays: Relays
    timing: Timing = field(default_factory=Timing)
    registry: Registry = field(default_factory=Registry)
    #: Every connection past the handshake, until its teardown has finished.
    connections: set[Connection] = field(default_factory=set)
    tickets: TicketLedger = field(default_factory=TicketLedger)

    async def admit(self, step_run_id: int) -> Admission:
        async with self.session_factory() as session:
            return await terminal_admission(
                session,
                step_run_id,
                now=datetime.now(UTC),
                retention=self.settings.workspace.retention,
                enabled=self.settings.terminal.enabled,
            )

    @property
    def api_token(self) -> str:
        return self.settings.api_token.get_secret_value()

    async def reaches(self, credential: Credential, step_run_id: int) -> bool:
        """Whether the credential's holder may use terminals in the run's organization: at the
        handshake, and again at every recheck, so a removal or a demotion ends an open terminal
        too. A run that does not exist is left to the admission rule, which says so."""
        async with self.session_factory() as session:
            if isinstance(credential, SessionCookie) and await session_ended(
                session, email=credential.by, verified=credential.verified, epoch=credential.epoch
            ):
                return False
            if isinstance(credential, Ticket) and (
                await verified_since(session, email=credential.by, moment=credential.minted)
                or (
                    credential.ticket.epoch is not None
                    and await session_ended(
                        session, email=credential.by, verified=True, epoch=credential.ticket.epoch
                    )
                )
            ):
                return False
            return await allowed_on_run(
                session, caller_named(credential.by), step_run_id, Permission.TICKETS_TERMINAL
            )

    def credential(self, websocket: WebSocket, step_run_id: int) -> Credential | None:
        """The cookie, else a ticket for this run, unexpired and never used. The nonce is spent
        last, so a ticket refused for another reason stays usable."""
        now = datetime.now(UTC)
        cookie = websocket.cookies.get(SESSION_COOKIE)
        signed = verified_session(cookie, api_token=self.api_token, now=now)
        if cookie is not None and signed is not None:
            return SessionCookie(cookie, signed.email, signed.verified, signed.epoch)
        value = _offered_ticket(websocket)
        ticket = read_terminal_ticket(value, api_token=self.api_token)
        if (
            value is None
            or ticket is None
            or ticket.step_run_id != step_run_id
            or not ticket.unexpired(now)
            or not ticket.lasts(now)
            or not self.tickets.spend(ticket, now)
        ):
            return None
        return Ticket(ticket)

    async def shutdown(self) -> None:
        """Closes every connection 1012 and returns once each has closed its tmux client: on
        E2B a client left behind is a root pty with no time limit."""
        for connection in list(self.connections):
            connection.request_close(CloseCode.RESTARTING, RESTARTING)
        await asyncio.gather(*(c.finished.wait() for c in list(self.connections)))


async def _deny(websocket: WebSocket) -> None:
    """A close before `accept()` is an HTTP 403. Not `send_denial_response`: a browser shows no
    body, and uvicorn's sansio protocol logs every denial response as an error."""
    await websocket.close()


async def serve(gateway: Gateway, websocket: WebSocket, step_run_id: int) -> None:
    if not gateway.settings.admits_origin(websocket.headers.get("origin")):
        # Starlette's CORS middleware skips WebSockets: without this a page on any site could
        # open a terminal with the person's cookie.
        await _deny(websocket)
        return
    credential = gateway.credential(websocket, step_run_id)
    if credential is None or (
        isinstance(credential, SessionCookie)
        and not await gateway.reaches(credential, step_run_id)
    ):
        await _deny(websocket)
        return
    # A browser that offered subprotocols fails a handshake whose answer names none of them.
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol=TICKET_PROTOCOL if TICKET_PROTOCOL in offered else None)

    size = parse_size(websocket.query_params.get("cols"), websocket.query_params.get("rows"))
    if size is None:
        await _close(
            websocket,
            CloseCode.MALFORMED,
            "a terminal's columns and rows must each be between 1 and 65535",
        )
        return
    connection = Connection(gateway, websocket, step_run_id, size, credential=credential)
    gateway.connections.add(connection)
    try:
        await connection.run()
    finally:
        gateway.registry.leave(connection)
        gateway.connections.discard(connection)
        connection.finished.set()


async def _through_cancel(task: asyncio.Task[None]) -> None:
    """Awaits `task` to its end even through cancels, then re-raises the cancel: a connection
    counts as finished only once its tmux client is closed, and shutdown waits for that."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait([task])
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    task.result()


async def _closed(stream: TerminalStream) -> None:
    try:
        await stream.close()
    except Exception:
        log.exception("closing a terminal client failed; it may outlive its viewer")


class _Unattached(Exception):
    """The relay would not attach; its own words are in the log."""

    def __init__(self, code: CloseCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _close(websocket: WebSocket, code: CloseCode | int, reason: str) -> None:
    try:
        await asyncio.wait_for(websocket.close(int(code), close_reason(reason)), _SEND_GRACE)
    except Exception:
        log.debug("closing a terminal socket failed; the client is gone", exc_info=True)


class Connection:
    """A viewer, attached on a read-only tmux client. While it holds its run's writer seat its
    client is writable and its keystrokes reach the session; whoever else is attached stays
    read-only at tmux, a lock behind the gateway's own drop of their keystrokes."""

    def __init__(
        self,
        gateway: Gateway,
        websocket: WebSocket,
        step_run_id: int,
        size: tuple[int, int],
        *,
        credential: Credential,
    ) -> None:
        self._gateway = gateway
        self._ws = websocket
        self.step_run_id = step_run_id
        self._size = size
        self._credential = credential
        self.viewer_id = secrets.token_hex(6)
        self._loop = asyncio.get_running_loop()
        self._closing: asyncio.Future[tuple[int, str]] = self._loop.create_future()
        self._client_gone = False
        self._exited = False
        self._status: str | None = None
        self._last_status: str | None = None
        self._viewer = Viewer(maxsize=gateway.timing.queue, close=self.request_close)
        self.finished = asyncio.Event()
        #: The loop time the socket was accepted, for the log of how long the open took.
        self.accepted = self._loop.time()

        self._run: TerminalRun | None = None
        self._kind: str | None = None
        self._target: TerminalTarget | None = None
        self._audit: ViewerAudit | None = None
        #: Why this viewer may not type, from the latest admission; None when it may.
        self._read_only: str | None = None
        self._stream: TerminalStream | None = None
        self._writable = False
        #: Bumped as a client is replaced, so the old one's last output is dropped.
        self._generation = 0
        self._swap = asyncio.Lock()
        #: Set while this viewer has no writable client, attached or attaching.
        self._read_only_client = asyncio.Event()
        self._read_only_client.set()
        #: The session exited under a swap; the old client's watch reports it.
        self._gone = False
        #: Granted the seat, its taking recorded.
        self._seated = False
        self._claiming: asyncio.Task[None] | None = None
        #: Done once this viewer's latest taking of the seat is recorded, or will not be.
        self._taking: asyncio.Future[None] | None = None
        #: Cleared by a release, so a claim still checking its admission stands down.
        self._wants_seat = False
        #: Seats this viewer has let go, so a taking can tell its own seat was released under it.
        self._releases = 0
        #: Set exactly while this viewer's keystrokes reach the session: seated, on a writable
        #: client.
        self._bursts: InputBursts | None = None
        self._last_input = 0.0
        self._idle: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        #: Audit writes teardown waits for before it records the detach.
        self._writes: list[asyncio.Task[None]] = []

    @property
    def by(self) -> str:
        return self._credential.by

    def request_close(self, code: CloseCode | int, reason: str) -> None:
        """The first request wins; the connection tears down once."""
        if not self._closing.done():
            self._closing.set_result((int(code), reason))

    def _status_frame(self) -> str:
        registry = self._gateway.registry
        viewers, writers = registry.counts(self.step_run_id)
        holder = registry.holder(self.step_run_id)
        return status_frame(
            status=self._status,
            viewers=viewers,
            writers=writers,
            holder=holder.by if holder is not None else None,
            yours=self._bursts is not None,
            names=registry.names(self.step_run_id),
        )

    def push_status(self) -> None:
        frame = self._status_frame()
        if frame != self._last_status:
            self._last_status = frame
            self._viewer.offer(frame)

    async def run(self) -> None:
        try:
            admission = await self._gateway.admit(self.step_run_id)
        except Exception:
            log.exception("step run %s: the terminal's admission failed", self.step_run_id)
            await _close(self._ws, CloseCode.RESTARTING, UNCHECKED)
            return
        if admission.refusal is not None:
            await _close(self._ws, admission.refusal.code, admission.refusal.sentence)
            return
        run, kind, target = admission.run, admission.kind, admission.target
        assert run is not None and kind is not None and target is not None
        self._run, self._kind, self._target = run, kind, target
        self._read_only = admission.read_only
        self._status = admission.status
        if not self._gateway.registry.reserve(self):
            await _close(self._ws, CloseCode.CROWDED, f"this run already has {MAX_VIEWERS} viewers")
            return

        try:
            stream = await self._attach(writable=False, history=True)
        except _Unattached as e:
            await _close(self._ws, e.code, e.reason)
            return
        audit = ViewerAudit(self._gateway.session_factory, run, by=self.by, viewer=self.viewer_id)
        self._audit = audit
        try:
            await audit.attached(writable=False, target=kind, via=self._credential.via)
        except BaseException as e:
            # Even when cancelled: on E2B a client left attached is a pty with no time limit.
            await _through_cancel(self._loop.create_task(_closed(stream)))
            if not isinstance(e, Exception):
                raise
            log.exception("step run %s: recording a terminal attach failed", self.step_run_id)
            await _close(
                self._ws, CloseCode.INTERNAL, "couldn't record that you opened the terminal"
            )
            return
        await self._live(stream, audit)

    async def _attach(self, *, writable: bool, history: bool) -> TerminalStream:
        run, target = self._run, self._target
        assert run is not None and target is not None
        generation = self._generation

        def offer(chunk: bytes) -> None:
            if generation == self._generation:
                self._viewer.offer(chunk)

        try:
            async with self._gateway.session_factory() as session:
                relay = await self._gateway.relays.relay_for(session, run, target)
            return await relay.attach(
                target,
                writable=writable,
                cols=self._size[0],
                rows=self._size[1],
                on_output=offer,
                history=history,
            )
        # The relay's own words stay in the log: they can name paths on the host.
        except TerminalUnavailable as e:
            log.warning("step run %s: the terminal is unreachable: %s", self.step_run_id, e)
            raise _Unattached(CloseCode.UNREACHABLE, UNREACHABLE) from e
        except ValueError as e:
            # The size is checked before here, so this is the target or its runtime: a recorded
            # name the relay will not pass to tmux, a substrate with no relay, or an E2B runtime
            # with no key. Retrying cannot change it.
            log.exception("step run %s: the relay refused its recorded target", self.step_run_id)
            raise _Unattached(
                CloseCode.NO_TERMINAL,
                "this run's terminal can't be opened",
            ) from e
        except Exception as e:
            log.exception("step run %s: attaching a terminal failed", self.step_run_id)
            raise _Unattached(CloseCode.INTERNAL, "attaching the terminal failed") from e

    def _use(self, stream: TerminalStream, *, writable: bool) -> None:
        self._stream, self._writable = stream, writable
        self._spawn(self._watch_ended(stream))

    async def _live(self, stream: TerminalStream, audit: ViewerAudit) -> None:
        assert self._kind is not None
        started = self._loop.time()
        self._use(stream, writable=False)
        registry = self._gateway.registry
        registry.join(self)
        viewers, writers = registry.counts(self.step_run_id)
        holder = registry.holder(self.step_run_id)
        hello = hello_frame(
            status=self._status,
            target=self._kind,
            viewers=viewers,
            writers=writers,
            holder=holder.by if holder is not None else None,
            names=registry.names(self.step_run_id),
        )
        self._last_status = self._status_frame()
        sender = self._loop.create_task(
            self._viewer.send(hello, self._ws.send_bytes, self._ws.send_text)
        )
        sender.add_done_callback(self._sender_done)
        log.info(
            "step run %s: terminal hello queued %.2fs after the socket was accepted",
            self.step_run_id,
            self._loop.time() - self.accepted,
        )
        watchers = [self._loop.create_task(w) for w in (self._receive(), self._recheck())]
        for task in watchers:
            task.add_done_callback(self._watcher_done)
        try:
            await self._closing
        finally:
            if not self._closing.done():
                self.request_close(CloseCode.RESTARTING, RESTARTING)
            await _through_cancel(
                self._loop.create_task(self._teardown(audit, sender, watchers, started))
            )

    def _sender_done(self, sender: asyncio.Task[None]) -> None:
        if sender.cancelled():
            return
        if sender.exception() is not None:
            # The socket broke under a send: the client is gone.
            self._client_gone = True
            self.request_close(1006, "")

    def _watcher_done(self, task: asyncio.Task[None]) -> None:
        # Without this a task that broke would leave the connection open and deaf.
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("step run %s: a terminal task failed", self.step_run_id, exc_info=error)
            self.request_close(
                CloseCode.INTERNAL, "the terminal stopped working; reconnect to try again"
            )

    def _spawn(self, work: Coroutine[None, None, None]) -> asyncio.Task[None]:
        task = self._loop.create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._watcher_done)
        return task

    async def _receive(self) -> None:
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                self._client_gone = True
                self.request_close(message.get("code") or 1005, "")
                return
            data: bytes | None = message.get("bytes")
            text: str | None = message.get("text")
            if data is not None:
                # Only the seat holder's keystrokes go on, recorded in bursts. Once a burst fails
                # to record the holder is closed and nothing more it sends is passed on; that
                # burst itself goes unrecorded.
                bursts, stream = self._bursts, self._stream
                if bursts is None or stream is None or bursts.failed or self._closing.done():
                    continue
                self._last_input = self._loop.time()
                bursts.count(len(data))
                await stream.write(data)
                continue
            if text is None:
                continue
            match parse_frame(text):
                case Resize(cols, rows):
                    # Kept for the next attach, and sent to this viewer's own client. tmux ignores
                    # a read-only client's size only while a writable client is attached; with
                    # every client read-only it sizes the window from the latest of them.
                    self._size = (cols, rows)
                    if self._stream is not None:
                        await self._stream.resize(cols, rows)
                case Claim(force):
                    # Off this loop, so keys sent before the grant meet no bursts and drop, and
                    # a release or a disconnect never waits on a re-attach. A claim already in
                    # flight is answered with the seat as it stands, and gives its own answer.
                    self._wants_seat = True
                    if self._claiming is not None and not self._claiming.done():
                        self._viewer.offer(self._seat_now())
                    else:
                        self._claiming = self._spawn(self._claim(force=force))
                case Release():
                    self._wants_seat = False
                    if not self._release():
                        self._viewer.offer(self._seat_now())
                case None:
                    pass

    def _seat_now(self, reason: str | None = None) -> str:
        holder = self._gateway.registry.holder(self.step_run_id)
        return seat_frame(
            holder=holder.by if holder is not None else None,
            yours=self._bursts is not None,
            reason=reason,
        )

    async def _claim(self, *, force: bool) -> None:
        why = await self._why_not_typing()
        if self._closing.done() or not self._wants_seat:
            return
        if why is not None:
            self._viewer.offer(self._seat_now(why))
            return
        registry = self._gateway.registry
        # One gateway process on one loop, and no await from reading the seat to taking it: two
        # claims at once cannot both win.
        holder = registry.holder(self.step_run_id)
        if holder is self:
            self._viewer.offer(self._seat_now())
            return
        if holder is not None and not force:
            self._viewer.offer(self._seat_now(HELD.format(by=holder.by)))
            return
        registry.take(self)
        releases = self._releases
        taking = self._taking = self._loop.create_future()
        try:
            let_go = None
            if holder is not None:
                let_go = holder.unseat(seat_frame(holder=self.by, yours=False, taken=True))
            registry.changed(self.step_run_id)
            if let_go is not None:
                # Its last burst and its own taking are recorded, and its writable client
                # closed, before this taking is recorded and this viewer's client can be writable.
                await asyncio.shield(let_go)
            if self._releases != releases:
                # Let go before it was ever held, whatever was claimed since: a claim that came
                # meanwhile was coalesced into this one, so this answers it.
                self._viewer.offer(self._seat_now())
                return
            assert self._audit is not None
            recorded = self._loop.create_task(
                self._audit.seat_taken(
                    holder=holder.by if holder is not None else None, forced=holder is not None
                )
            )
            self._writes.append(recorded)
            try:
                await asyncio.shield(recorded)
            except Exception:
                log.exception("step run %s: recording a seat taken failed", self.step_run_id)
                self.request_close(
                    CloseCode.INTERNAL, "couldn't record that you took the keyboard"
                )
                return
        finally:
            taking.set_result(None)
        if registry.holder(self.step_run_id) is not self:
            # Taken from it meanwhile; the taker's frame said so.
            return
        self._seated = True
        self._last_input = self._loop.time()
        await self._settle()
        if self._bursts is not None:
            self._viewer.offer(self._seat_now())
            registry.changed(self.step_run_id)

    async def _why_not_typing(self) -> str | None:
        try:
            admission = await self._gateway.admit(self.step_run_id)
        except Exception:
            log.exception("step run %s: checking a claim failed", self.step_run_id)
            return UNCHECKED
        if admission.refusal is not None:
            return admission.refusal.sentence
        if not self._same_terminal(admission):
            return MOVED
        # Only ever tightened here: a recheck that ran after this query began may know better.
        self._read_only = self._read_only or admission.read_only
        return self._read_only

    def unseat(self, frame: str) -> asyncio.Task[None]:
        """This viewer lost the seat: its keystrokes stop here, at once, and `frame` tells it.
        Returns a task that ends once its pending burst is recorded under it and it has no
        writable client; its client is re-attached read-only meanwhile."""
        self._seated = False
        bursts, self._bursts = self._bursts, None
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        self._viewer.offer(frame)
        flushed = None
        if bursts is not None:
            flushed = self._loop.create_task(bursts.stop())
            self._writes.append(flushed)
        self._spawn(self._settle())
        return self._loop.create_task(self._let_go(flushed, self._taking))

    async def _let_go(
        self, flushed: asyncio.Task[None] | None, taking: asyncio.Future[None] | None
    ) -> None:
        for write in (flushed, taking):
            if write is not None:
                await asyncio.shield(write)
        await self._read_only_client.wait()

    def _release(self, reason: str | None = None) -> bool:
        """Frees the seat if this viewer holds it; False when it did not."""
        if not self._gateway.registry.free(self):
            return False
        self._releases += 1
        self.unseat(seat_frame(holder=None, yours=False, reason=reason))
        self._gateway.registry.changed(self.step_run_id)
        return True

    async def _settle(self) -> None:
        """Re-attaches this viewer's client writable while it is seated and may type, and
        read-only otherwise; tmux redraws. The viewer's socket and queue stay."""
        async with self._swap:
            while (
                not self._closing.done()
                and not self._gone
                and (self._stream is None or self._typable() != self._writable)
            ):
                self._generation += 1
                old, self._stream, self._writable = self._stream, None, False
                if old is not None:
                    # One step a cancel cannot split: a client dropped here and never closed
                    # would outlive the viewer, on E2B as a root pty with no time limit.
                    try:
                        await _through_cancel(self._loop.create_task(_closed(old)))
                    finally:
                        self._read_only_client.set()
                    if old.ended.done() and old.ended.result() == "exited":
                        self._gone = True
                        return
                # Decided after the close, not before: the seat may have been taken meanwhile,
                # by a taker that has already seen this viewer with no writable client.
                writable = self._typable()
                if writable:
                    self._read_only_client.clear()
                try:
                    stream = await self._attach(writable=writable, history=False)
                except BaseException as e:
                    self._read_only_client.set()
                    if not isinstance(e, _Unattached):
                        raise
                    self.request_close(e.code, e.reason)
                    return
                self._use(stream, writable=writable)
            if self._writable and self._bursts is None and not self._closing.done():
                assert self._audit is not None
                self._bursts = InputBursts(
                    self._audit.input,
                    quiet=self._gateway.timing.audit_quiet,
                    max_wait=self._gateway.timing.audit_max,
                    on_failure=lambda: self.request_close(
                        CloseCode.INTERNAL, "couldn't record your typing"
                    ),
                )
                self._idle = self._loop.create_task(self._watch_idle())
                self._idle.add_done_callback(self._watcher_done)

    def _same_terminal(self, admission: Admission) -> bool:
        return (admission.kind, admission.target) == (self._kind, self._target)

    def _typable(self) -> bool:
        return self._seated and self._kind == "session" and self._read_only is None

    async def _watch_idle(self) -> None:
        quiet = self._gateway.timing.seat_idle
        while True:
            left = self._last_input + quiet - self._loop.time()
            if left <= 0:
                break
            await asyncio.sleep(left)
        self._idle = None
        self._release(IDLE.format(seconds=quiet))

    async def _recheck(self) -> None:
        while True:
            await asyncio.sleep(self._gateway.timing.recheck)
            if not self._credential.valid(self._gateway.api_token, datetime.now(UTC)):
                self.request_close(CloseCode.SIGNED_OUT, self._credential.ended)
                return
            try:
                reaches = await self._gateway.reaches(self._credential, self.step_run_id)
                admission = await self._gateway.admit(self.step_run_id)
            except Exception:
                log.exception("step run %s: re-checking a terminal failed", self.step_run_id)
                self.request_close(CloseCode.RESTARTING, "couldn't re-check this run's terminal")
                return
            if not reaches:
                self.request_close(CloseCode.FORBIDDEN, NO_ACCESS)
                return
            if admission.refusal is not None:
                self.request_close(admission.refusal.code, admission.refusal.sentence)
                return
            if not self._same_terminal(admission):
                self.request_close(CloseCode.RESTARTING, MOVED)
                return
            self._status = admission.status
            self._read_only = admission.read_only
            if self._read_only is not None:
                self._release(self._read_only)
            self.push_status()

    async def _watch_ended(self, stream: TerminalStream) -> None:
        # Shielded: cancelling this watcher must not cancel the stream's own future.
        reason = await asyncio.shield(stream.ended)
        # A client a swap retired ends as closed; one whose session ended under it still says so.
        if self._closing.done() or (stream is not self._stream and reason != "exited"):
            return
        # A session that ended because the run parked says so, not merely that it ended.
        try:
            admission = await self._gateway.admit(self.step_run_id)
        except Exception:
            log.exception("step run %s: checking an ended terminal failed", self.step_run_id)
            admission = None
        if admission is not None and admission.refusal is not None:
            self.request_close(admission.refusal.code, admission.refusal.sentence)
        elif admission is not None and not self._same_terminal(admission):
            self.request_close(CloseCode.RESTARTING, MOVED)
        elif reason == "exited":
            self._exited = True
            self.request_close(CloseCode.ENDED, "the session ended")
        else:
            log.warning("step run %s: its terminal client ended: %s", self.step_run_id, reason)
            self.request_close(CloseCode.UNREACHABLE, UNREACHABLE)

    async def _teardown(
        self,
        audit: ViewerAudit,
        sender: asyncio.Task[None],
        watchers: list[asyncio.Task[None]],
        started: float,
    ) -> None:
        code, reason = self._closing.result()
        registry = self._gateway.registry
        if registry.free(self):
            registry.changed(self.step_run_id)
        tasks = [*watchers, *self._background, *filter(None, [self._idle])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._stream is not None:
            await _closed(self._stream)
        self._read_only_client.set()
        bursts, self._bursts = self._bursts, None
        if bursts is not None:
            await bursts.stop()
        await asyncio.gather(*self._writes, return_exceptions=True)
        registry.leave(self)
        try:
            await audit.detached(seconds=self._loop.time() - started, code=code)
        except Exception:
            log.exception("step run %s: recording a terminal detach failed", self.step_run_id)

        if not self._client_gone and self._exited:
            self._viewer.offer(exit_frame(reason))
            self._viewer.finish()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(sender), _SEND_GRACE)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        if not self._client_gone:
            await _close(self._ws, code, reason)
