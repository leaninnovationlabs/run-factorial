"""The terminal gateway end to end: uvicorn in-process, a real WebSocket client, a real tmux
server behind `HostTmuxRelay`, and rows in Postgres. tmux is required, never skipped: CI
installs it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import cast, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import Text
from starlette.applications import Starlette
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Origin, Subprotocol

from orchestrator.access.decision import organization_of
from orchestrator.api import sessions
from orchestrator.api.auth import BEARER_PRINCIPAL
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import (
    SESSION_COOKIE,
    TICKET_PROTOCOL,
    TICKET_SECONDS,
    mint_session,
    mint_terminal_ticket,
    read_terminal_ticket,
)
from orchestrator.db.models import (
    AppUser,
    Event,
    OrganizationMember,
    Runtime,
    StepRun,
    StepRunStatus,
    Ticket,
)
from orchestrator.events import EventType
from orchestrator.events.terminals import READ_ONLY_MOVED, READ_ONLY_VIEW, TerminalRun
from orchestrator.secrets import store
from orchestrator.settings import (
    APP_PREFIX,
    AuthSettings,
    ManagedSandbox,
    Settings,
    TerminalSettings,
)
from orchestrator.terminal.app import WS_PROTOCOL, SubstrateRelays, create_app
from orchestrator.terminal.audit import ViewerAudit
from orchestrator.terminal.connection import Gateway, Timing, Viewer
from orchestrator.terminal.protocol import SEAT_IDLE, CloseCode
from runtime.contract import TerminalTarget
from runtime.terminal import TMUX_LOCKDOWN, HostTmuxRelay
from tests.api_harness import TOKEN, build_app, client_for, make_settings
from tests.members import ensure_member, viewer_role
from tests.terminal_rows import record, seed_run

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

DASHBOARD = "https://factorial.test"
EMAIL = "operator@example.com"
TEAMMATE = "teammate@example.com"
TEAMMATE_2 = "teammate-2@example.com"
SESSION = "run"


@pytest.fixture(autouse=True)
def _tmux_installed() -> None:
    if shutil.which("tmux") is None:
        pytest.fail("tmux is not installed; these tests drive a real tmux server")


def _env_outside_tmux() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


@dataclass
class Tmux:
    socket: str

    def run(self, *args: str) -> str:
        done = subprocess.run(
            ["tmux", "-S", self.socket, *args],
            capture_output=True,
            text=True,
            env=_env_outside_tmux(),
        )
        if done.returncode != 0:
            pytest.fail(f"tmux {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout

    def pane(self) -> str:
        return self.run("capture-pane", "-p", "-t", f"={SESSION}:")

    def print_in_pane(self, data: bytes) -> None:
        tty = self.run("display-message", "-p", "-t", f"={SESSION}:", "#{pane_tty}")
        Path(tty.strip()).write_bytes(data)

    def clients(self) -> int:
        return len(self.run("list-clients").splitlines())

    def read_only(self) -> list[bool]:
        """Whether each attached client is read-only, as tmux itself has it."""
        flags = self.run("list-clients", "-F", "#{client_readonly}").split()
        return sorted(flag == "1" for flag in flags)

    @property
    def target(self) -> TerminalTarget:
        return TerminalTarget(substrate="host", socket=self.socket, session=SESSION)


@pytest.fixture
def tmux() -> Iterator[Tmux]:
    # pytest's tmp_path on macOS is longer than a unix socket path may be.
    d = Path(tempfile.mkdtemp(dir="/tmp"))
    conf = d / "tmux.conf"
    conf.write_text(TMUX_LOCKDOWN)
    server = Tmux(socket=str(d / "s"))
    new_session = ["new-session", "-d", "-s", SESSION, "-x", "100", "-y", "30", "cat"]
    subprocess.run(
        ["tmux", "-S", server.socket, "-f", str(conf), *new_session],
        check=True,
        env=_env_outside_tmux(),
    )
    try:
        yield server
    finally:
        subprocess.run(["tmux", "-S", server.socket, "kill-server"], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


def _settings() -> Settings:
    return make_settings(auth=AuthSettings(external_url=DASHBOARD)).model_copy(
        update={"terminal": TerminalSettings(enabled=True)}
    )


@dataclass
class Running:
    port: int
    server: uvicorn.Server
    task: asyncio.Task[None]
    gateway: Gateway

    def url(self, run_id: int, mode: str = "watch") -> str:
        return (
            f"ws://127.0.0.1:{self.port}{APP_PREFIX}/terminal/runs/{run_id}"
            f"?mode={mode}&cols=100&rows=30"
        )

    async def stop(self) -> None:
        self.server.should_exit = True
        await self.task


FAST = Timing(recheck=0.2, audit_quiet=0.2, audit_max=1.0)


@asynccontextmanager
async def _started(app: Starlette) -> AsyncIterator[Running]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, ws=WS_PROTOCOL, lifespan="on", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    running = Running(sock.getsockname()[1], server, task, app.state.gateway)
    try:
        yield running
    finally:
        if not task.done():
            await running.stop()


@pytest.fixture
async def gateway(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Running]:
    async with _started(create_app(_settings(), maker, timing=FAST)) as running:
        yield running


def _cookie(email: str = EMAIL, *, max_age: int = 3600) -> str:
    value = mint_session(
        email, verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=max_age
    )
    return f"{SESSION_COOKIE}={value}"


def _open(
    url: str,
    *,
    origin: str | None = DASHBOARD,
    headers: dict[str, str] | None = None,
    ticket: str | None = None,
    email: str = EMAIL,
) -> Any:
    """With a cookie by default; with `ticket`, the ticket alone, as a token-only reader does."""
    if ticket is not None and headers is None:
        headers = {}
    return connect(
        url,
        origin=Origin(origin) if origin is not None else None,
        additional_headers={"Cookie": _cookie(email)} if headers is None else headers,
        subprotocols=(
            [Subprotocol(TICKET_PROTOCOL), Subprotocol(ticket)] if ticket is not None else None
        ),
    )


def _ticket(
    run_id: int,
    *,
    by: str = BEARER_PRINCIPAL,
    until: int | None = None,
    api_token: str = TOKEN,
    now: datetime | None = None,
) -> str:
    return mint_terminal_ticket(
        run_id, by, until=until, api_token=api_token, now=now or datetime.now(UTC)
    )[0]


#: The people these tests sign in, each a member of the run's organization: a session cookie
#: reaches only a run its account's membership reaches (design.md §7.19).
MEMBERS: Final = (EMAIL, TEAMMATE, TEAMMATE_2, "teammate-3@example.com")


async def _seed(maker: async_sessionmaker[AsyncSession], **kwargs: Any) -> StepRun:
    async with maker() as session:
        run = await seed_run(session, **kwargs)
        organization_id = await organization_of(session, {"step_run_id": str(run.id)})
        for email in MEMBERS:
            await ensure_member(session, email, organization_id=organization_id)
        await session.commit()
    return run


async def _events(maker: async_sessionmaker[AsyncSession], run_id: int) -> list[Event]:
    async with maker() as session:
        rows = await session.execute(
            select(Event)
            .where(Event.step_run_id == run_id, Event.type.like("terminal\\_%"))
            .order_by(Event.id)
        )
        return list(rows.scalars())


async def eventually(check: Callable[[], Any], what: str, *, within: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def _detached(maker: async_sessionmaker[AsyncSession], run_id: int) -> list[Event]:
    async def seen() -> bool:
        return any(e.type == EventType.TERMINAL_DETACHED for e in await _events(maker, run_id))

    await eventually(seen, "the detach recorded")
    return await _events(maker, run_id)


async def _hello(ws: ClientConnection) -> dict[str, Any]:
    frame = await asyncio.wait_for(ws.recv(), 5)
    assert isinstance(frame, str)
    hello = json.loads(frame)
    assert hello["type"] == "hello"
    return hello


async def _frame(ws: ClientConnection, kind: str, *, within: float = 5) -> dict[str, Any]:
    """The next text frame of `kind`, past any output and other frames."""

    async def read() -> dict[str, Any]:
        while True:
            frame = await ws.recv()
            if isinstance(frame, str) and (parsed := json.loads(frame))["type"] == kind:
                return parsed

    return await asyncio.wait_for(read(), within)


async def _claim(ws: ClientConnection, *, force: bool = False) -> dict[str, Any]:
    """The `seat` frame that answers a claim."""
    await ws.send(json.dumps({"type": "claim", **({"force": True} if force else {})}))
    return await _frame(ws, "seat")


async def _seated(ws: ClientConnection) -> None:
    seat = await _claim(ws)
    assert seat["yours"] is True, seat


async def _closed(ws: ClientConnection, *, within: float = 5) -> tuple[int | None, str | None]:
    async def drain() -> None:
        try:
            while True:
                await ws.recv()
        except ConnectionClosed:
            return

    await asyncio.wait_for(drain(), within)
    return ws.close_code, ws.close_reason


async def _output_until(ws: ClientConnection, needle: bytes) -> None:
    seen = bytearray()

    async def read() -> None:
        while needle not in seen:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                seen.extend(frame)

    await asyncio.wait_for(read(), 5)


@pytest.mark.parametrize("origin", ["https://evil.test", "null", "http://factorial.test", None])
async def test_a_foreign_or_missing_origin_is_refused_before_accept_and_leaves_no_event(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], origin: str | None
) -> None:
    """Silent and costly: Starlette's CORS middleware skips WebSockets, so without this check a
    page on any site could open a terminal with a signed-in person's cookie."""
    run = await _seed(maker, target=tmux.target)

    with pytest.raises(InvalidStatus) as refused:
        async with _open(gateway.url(run.id, "type"), origin=origin):
            pass

    assert refused.value.response.status_code == 403
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {TOKEN}"},
        {"Cookie": f"{SESSION_COOKIE}=forged.0.0"},
    ],
    ids=["nothing", "bearer token", "forged cookie"],
)
async def test_without_a_signed_in_session_the_terminal_is_refused(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], headers: dict[str, str]
) -> None:
    """A bearer token in a header is no credential here: only a ticket, which names who minted
    it, stands in for the cookie."""
    run = await _seed(maker, target=tmux.target)

    with pytest.raises(InvalidStatus) as refused:
        async with _open(gateway.url(run.id, "type"), headers=headers):
            pass

    assert refused.value.response.status_code == 403
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


async def test_a_session_outside_the_runs_organization_is_refused_before_accept(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: a valid cookie is not membership, and without this check anyone signed
    in to the deployment could watch any organization's run by its id."""
    run = await _seed(maker, target=tmux.target)
    stranger = f"stranger-{uuid4().hex[:8]}@example.com"

    with pytest.raises(InvalidStatus) as refused:
        async with _open(gateway.url(run.id), email=stranger):
            pass

    assert refused.value.response.status_code == 403
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


async def test_a_role_without_tickets_terminal_is_refused_before_accept(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: the session holds the run's secrets, so membership alone must not
    open it (revision 0037). The built-in roles all hold the permission since 0038; a role of
    the organization's own may leave it out."""
    run = await _seed(maker, target=tmux.target)
    viewer = f"viewer-{uuid4().hex[:8]}@example.com"
    async with maker() as session:
        organization_id = await organization_of(session, {"step_run_id": str(run.id)})
        await ensure_member(
            session,
            viewer,
            organization_id=organization_id,
            role_id=await viewer_role(session, organization_id),
        )
        await session.commit()

    with pytest.raises(InvalidStatus) as refused:
        async with _open(gateway.url(run.id), email=viewer):
            pass

    assert refused.value.response.status_code == 403
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


@pytest.mark.parametrize("mode", ["watch", "type"])
async def test_every_connection_attaches_read_only_and_records_no_input(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], mode: str
) -> None:
    """Silent and costly: a dashboard from before the seat still asks for `mode=type`, and must
    get a viewer, read-only at tmux too, never a writer."""
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id, mode)) as ws:
        hello = await _hello(ws)
        assert (hello["writers"], hello["seat"]) == (0, {"holder": None, "yours": False})
        assert tmux.read_only() == [True]
        await ws.send(b"typed-by-watcher\r")
        tmux.print_in_pane(b"from-the-pane\r\n")
        await _output_until(ws, b"from-the-pane")
        await asyncio.sleep(0.3)
        assert "typed-by-watcher" not in tmux.pane()

    events = await _detached(maker, run.id)
    assert not [e for e in events if e.type == EventType.TERMINAL_INPUT]


async def test_a_claim_on_an_open_session_is_refused_and_the_viewer_stays(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Open session relaunches the agent's own TUI; a writer could drive it with the run's model
    key and, on a host, run `!` commands as `factorial`. No one may take its seat."""
    async with maker() as session:
        run = await seed_run(session, target=None, status=StepRunStatus.SUCCEEDED)
        await record(session, run, EventType.INSPECT_STARTED, terminal=tmux.target.model_dump())
        await session.commit()

    async with _open(gateway.url(run.id, "type")) as ws:
        assert (await _hello(ws))["target"] == "inspect"
        refused = await _claim(ws, force=True)
        assert refused == _seat(None, reason=READ_ONLY_VIEW)
        tmux.print_in_pane(b"still-watching\r\n")
        await _output_until(ws, b"still-watching")
        assert tmux.read_only() == [True]


async def test_a_claim_on_a_session_its_ticket_has_moved_past_is_refused(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target, ticket_step="review")

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        refused = await _claim(ws)
        assert (refused["yours"], refused["reason"]) == (False, READ_ONLY_MOVED)
        assert tmux.read_only() == [True]
    events = await _detached(maker, run.id)
    assert not [e for e in events if e.type == EventType.TERMINAL_SEAT_TAKEN]


async def test_the_holder_loses_the_seat_when_its_ticket_moves_past_the_session(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        await _seated(ws)
        async with maker() as session:
            await session.execute(
                update(Ticket).where(Ticket.id == run.ticket_id).values(current_step="review")
            )
            await session.commit()
        lost = await _frame(ws, "seat", within=2)
        assert (lost["holder"], lost["yours"], lost["reason"]) == (None, False, READ_ONLY_MOVED)
        await eventually(lambda: tmux.read_only() == [True], "the stream read-only again")
        await ws.send(b"after-the-move\r")
        await asyncio.sleep(0.3)
        assert "after-the-move" not in tmux.pane()


async def test_input_is_audited_as_a_byte_count_never_its_content(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: a terminal is where a person pastes a credential, and the event log
    is kept for ever."""
    run = await _seed(maker, target=tmux.target)
    marker = f"marker-{uuid4().hex}"
    typed = [f"{marker[:10]}".encode(), f"{marker[10:]}\r".encode()]

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        await _seated(ws)
        for chunk in typed:
            await ws.send(chunk)
        await eventually(lambda: marker in tmux.pane(), "the typed marker in the pane")

    events = await _detached(maker, run.id)
    inputs = [e for e in events if e.type == EventType.TERMINAL_INPUT]
    assert inputs
    assert sum(e.details["bytes"] for e in inputs) == sum(len(c) for c in typed)
    assert {e.details["by"] for e in inputs} == {EMAIL}
    async with maker() as session:
        leaked = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(cast(Event.details, Text).contains(marker[:10]))
        )
    assert leaked == 0


async def test_a_slow_viewer_is_closed_4408_while_another_keeps_receiving() -> None:
    """The relay's output callback must never wait on a socket: one stalled browser would stall
    every viewer of the session, and the envd stream behind it."""
    closed: dict[str, int] = {}
    received: list[bytes] = []
    never = asyncio.Event()

    async def stuck(_: object) -> None:
        await never.wait()

    async def text(_: str) -> None:
        return None

    async def into(frame: bytes) -> None:
        received.append(frame)

    slow = Viewer(maxsize=256, close=lambda code, _: closed.update(slow=code))
    healthy = Viewer(maxsize=256, close=lambda code, _: closed.update(healthy=code))
    senders = [
        asyncio.create_task(slow.send("hello", stuck, stuck)),
        asyncio.create_task(healthy.send("hello", into, text)),
    ]
    try:
        for n in range(300):
            chunk = f"{n}".encode()
            slow.offer(chunk)
            healthy.offer(chunk)
            await asyncio.sleep(0)
        await eventually(lambda: len(received) == 300, "every chunk at the healthy viewer")
    finally:
        for task in senders:
            task.cancel()

    assert closed == {"slow": CloseCode.TOO_SLOW}


async def test_a_parked_session_with_no_shell_is_closed_4409(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target, status=StepRunStatus.INTERRUPTED)

    async with _open(gateway.url(run.id)) as ws:
        code, reason = await _closed(ws)

    assert code == CloseCode.PARKED
    assert reason and "Resume" in reason
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


@pytest.mark.parametrize("case", ["quarantined", "switched off", "finished"])
async def test_a_refused_run_closes_with_its_code(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], case: str
) -> None:
    run = await _seed(
        maker,
        target=tmux.target,
        quarantined=case == "quarantined",
        status=StepRunStatus.SUCCEEDED if case == "finished" else StepRunStatus.RUNNING,
    )
    if case == "switched off":
        gateway.gateway.settings = _settings().model_copy(update={"terminal": TerminalSettings()})

    async with _open(gateway.url(run.id)) as ws:
        code, _ = await _closed(ws)

    expected = CloseCode.NO_TERMINAL if case == "finished" else CloseCode.FORBIDDEN
    assert code == expected
    assert tmux.clients() == 0


async def test_a_session_that_parks_while_watched_is_closed_4409_within_the_recheck(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        async with maker() as session:
            await session.execute(
                update(StepRun).where(StepRun.id == run.id).values(status=StepRunStatus.INTERRUPTED)
            )
            await session.commit()
        code, _ = await _closed(ws, within=2)

    assert code == CloseCode.PARKED
    await eventually(lambda: tmux.clients() == 0, "the viewer's tmux client gone")


async def test_attach_and_detach_are_recorded_with_the_viewer_counts(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id)) as writer:
        hello = await _hello(writer)
        assert (hello["viewers"], hello["writers"], hello["target"]) == (1, 0, "session")
        assert hello["status"] == StepRunStatus.RUNNING
        await _seated(writer)
        async with _open(gateway.url(run.id)) as watcher:
            hello = await _hello(watcher)
            assert (hello["viewers"], hello["writers"]) == (2, 1)
            assert hello["seat"] == {"holder": EMAIL, "yours": False}
            await eventually(lambda: tmux.clients() == 2, "both tmux clients")
        await _detached(maker, run.id)

    async def both_detached() -> bool:
        detached = [e for e in await _events(maker, run.id) if e.type == "terminal_detached"]
        return len(detached) == 2

    await eventually(both_detached, "both detaches recorded")
    events = await _events(maker, run.id)
    attached = {e.details["viewer"]: e for e in events if e.type == EventType.TERMINAL_ATTACHED}
    detached = {e.details["viewer"]: e for e in events if e.type == EventType.TERMINAL_DETACHED}
    assert attached.keys() == detached.keys() and len(attached) == 2
    assert [e.details["writable"] for e in attached.values()] == [False, False]
    for viewer, event in attached.items():
        assert event.details["by"] == EMAIL
        assert event.details["target"] == "session"
        assert (event.details["step"], event.details["attempt"]) == ("develop", 1)
        assert detached[viewer].details["code"] == 1000
    async with maker() as session:
        ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None and {e.project_id for e in events} == {ticket.project_id}


async def _until_names(ws: ClientConnection, names: list[str]) -> dict[str, Any]:
    while (status := await _frame(ws, "status"))["names"] != names:
        pass
    return status


async def test_every_viewer_is_told_who_is_watching_as_they_come_and_go(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id), email="teammate-3@example.com") as teammate_3:
        assert (await _hello(teammate_3))["names"] == ["teammate-3@example.com"]
        async with _open(gateway.url(run.id), email="operator@example.com") as operator:
            # Sorted, so the list does not reorder as people come and go.
            both = ["operator@example.com", "teammate-3@example.com"]
            assert (await _hello(operator))["names"] == both
            await _until_names(teammate_3, both)
        await _until_names(teammate_3, ["teammate-3@example.com"])


async def test_shutting_down_closes_every_viewer_1012_after_its_tmux_client_is_gone(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: on E2B a client left attached is a root pty with no time limit, and a
    session with a client attached never counts as idle."""
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id, "type")) as ws:
        await _hello(ws)
        assert tmux.clients() == 1
        stopping = asyncio.create_task(gateway.stop())
        code, _ = await _closed(ws)
        await stopping

    assert code == CloseCode.RESTARTING
    await eventually(lambda: tmux.clients() == 0, "the viewer's tmux client gone")
    detached = [e for e in await _events(maker, run.id) if e.type == "terminal_detached"]
    assert [e.details["code"] for e in detached] == [CloseCode.RESTARTING]


class SlowClosing:
    """A stream whose close takes a while, as killing an E2B pty does."""

    def __init__(self) -> None:
        self.ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.closed = False

    async def write(self, data: bytes) -> None:
        return None

    async def resize(self, cols: int, rows: int) -> None:
        return None

    async def close(self) -> None:
        await asyncio.sleep(0.5)
        self.closed = True
        if not self.ended.done():
            self.ended.set_result("closed")


class SlowRelay:
    def __init__(self) -> None:
        self.streams: list[SlowClosing] = []
        self.handlers: list[asyncio.Task[Any]] = []

    async def attach(
        self,
        target: TerminalTarget,
        *,
        writable: bool,
        cols: int,
        rows: int,
        on_output: Callable[[bytes], None],
        history: bool = True,
    ) -> SlowClosing:
        handler = asyncio.current_task()
        assert handler is not None
        self.handlers.append(handler)
        self.streams.append(SlowClosing())
        return self.streams[-1]


@pytest.mark.parametrize("cancelled", [False, True], ids=["shutdown", "handler cancelled"])
async def test_shutdown_returns_only_once_every_client_has_closed(
    maker: async_sessionmaker[AsyncSession], cancelled: bool
) -> None:
    """Silent and costly: on E2B a client left attached is a root pty with no time limit. A
    cancel that reaches the connection mid-teardown must not let it count as finished."""
    relay = SlowRelay()
    app = create_app(_settings(), maker, SubstrateRelays(host=relay), timing=FAST)
    target = TerminalTarget(substrate="host", socket="/tmp/unused", session=SESSION)
    run = await _seed(maker, target=target)

    async with _started(app) as running, _open(running.url(run.id, "type")) as ws:
        await _hello(ws)
        shutdown = asyncio.create_task(running.gateway.shutdown())
        if cancelled:
            await asyncio.sleep(0.1)
            relay.handlers[0].cancel()
        await asyncio.wait_for(shutdown, 5)
        assert relay.streams[0].closed


async def test_a_writer_whose_typing_cannot_be_recorded_is_closed_1011_and_types_no_more(
    gateway: Running,
    tmux: Tmux,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: typing that cannot be charged to a person stops reaching the session."""
    failed = asyncio.Event()

    async def unrecordable(self: ViewerAudit, count: int) -> None:
        failed.set()
        raise RuntimeError("the database is down")

    monkeypatch.setattr(ViewerAudit, "input", unrecordable)
    run = await _seed(maker, target=tmux.target)
    later = f"later-{uuid4().hex}"

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        await _seated(ws)
        await ws.send(b"first\r")
        await asyncio.wait_for(failed.wait(), 5)
        with contextlib.suppress(ConnectionClosed):
            await ws.send(f"{later}\r".encode())
        code, _ = await _closed(ws)

    assert code == CloseCode.INTERNAL
    await asyncio.sleep(0.3)
    assert "first" in tmux.pane()
    assert later not in tmux.pane()


async def test_a_session_cookie_that_expires_mid_connection_closes_4401(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id), headers={"Cookie": _cookie(max_age=1)}) as ws:
        await _hello(ws)
        code, _ = await _closed(ws, within=4)

    assert code == CloseCode.SIGNED_OUT


@pytest.mark.parametrize("change", ["removed", "demoted"])
async def test_an_open_terminal_closes_4403_once_its_viewer_loses_access(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], change: str
) -> None:
    """Silent and costly: the session holds the run's secrets, and the cookie lasts a week, so a
    removal or a demotion must end a terminal already open, not only refuse the next one."""
    run = await _seed(maker, target=tmux.target)
    person = f"viewer-{uuid4().hex[:8]}@example.com"
    async with maker() as session:
        organization_id = await organization_of(session, {"step_run_id": str(run.id)})
        await ensure_member(session, person, organization_id=organization_id)
        await session.commit()

    async with _open(gateway.url(run.id), email=person) as ws:
        await _hello(ws)
        async with maker() as session:
            user_id = select(AppUser.id).where(AppUser.email == person).scalar_subquery()
            mine = (
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
            if change == "removed":
                await session.execute(delete(OrganizationMember).where(*mine))
            else:
                await session.execute(
                    update(OrganizationMember)
                    .where(*mine)
                    .values(role_id=await viewer_role(session, organization_id))
                )
            await session.commit()
        code, reason = await _closed(ws, within=4)

    assert code == CloseCode.FORBIDDEN
    assert reason is not None and "no longer have access" in reason


async def test_a_ticket_minted_before_an_sso_sign_in_took_the_account_back_closes_4403(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: a ticket names only an email, so without this the stranger who
    registered the address keeps a terminal its real owner's sign-in should have ended."""
    run = await _seed(maker, target=tmux.target)
    person = f"squatter-{uuid4().hex[:8]}@example.com"
    async with maker() as session:
        organization_id = await organization_of(session, {"step_run_id": str(run.id)})
        await ensure_member(session, person, organization_id=organization_id)
        await session.commit()

    async with _open(gateway.url(run.id), ticket=_ticket(run.id, by=person)) as ws:
        await _hello(ws)
        async with maker() as session:
            await session.execute(
                update(AppUser)
                .where(AppUser.email == person)
                .values(email_verified_at=datetime.now(UTC) + timedelta(seconds=1))
            )
            await session.commit()
        code, _ = await _closed(ws, within=4)

    assert code == CloseCode.FORBIDDEN


# --------------------------------------------------------------------------- the writer seat


def _seat_events(events: list[Event]) -> list[tuple[str, str, object]]:
    """Each seat change and input burst in order: who, and from whom or how many bytes."""
    return [
        (e.type, e.details["by"], e.details["from"] if e.type == "terminal_seat_taken" else n)
        for e in events
        if e.type in (EventType.TERMINAL_SEAT_TAKEN, EventType.TERMINAL_INPUT)
        for n in [e.details.get("bytes")]
    ]


def _seat(
    holder: str | None, *, yours: bool = False, taken: bool = False, reason: str | None = None
) -> dict[str, Any]:
    return {"type": "seat", "holder": holder, "yours": yours, "taken": taken, "reason": reason}


async def _until_status(ws: ClientConnection, holder: str | None) -> dict[str, Any]:
    while (status := await _frame(ws, "status"))["seat"]["holder"] != holder:
        pass
    return status


async def test_a_claim_makes_its_stream_writable_and_is_recorded(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id)) as ws, _open(gateway.url(run.id), email=TEAMMATE) as peer:
        await _hello(ws)
        await _hello(peer)
        seat = await _claim(ws)
        assert seat == _seat(EMAIL, yours=True)
        status = await _until_status(peer, EMAIL)
        assert (status["writers"], status["seat"]) == (1, {"holder": EMAIL, "yours": False})
        await eventually(lambda: tmux.read_only() == [False, True], "one writable client")
        await ws.send(b"typed-by-holder\r")
        await peer.send(b"typed-by-peer\r")
        await eventually(lambda: "typed-by-holder" in tmux.pane(), "the holder's typing")
        await asyncio.sleep(0.3)
        assert "typed-by-peer" not in tmux.pane()

    await _detached(maker, run.id)
    events = await _events(maker, run.id)
    taken = [e for e in events if e.type == EventType.TERMINAL_SEAT_TAKEN]
    assert [(e.details["by"], e.details["from"], e.details["forced"]) for e in taken] == [
        (EMAIL, None, False)
    ]


async def test_two_claims_at_once_seat_one_and_the_other_is_told_who_holds_it(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with (
        _open(gateway.url(run.id)) as operator,
        _open(gateway.url(run.id), email=TEAMMATE) as teammate,
    ):
        await _hello(operator)
        await _hello(teammate)
        answers = await asyncio.gather(_claim(operator), _claim(teammate))

    assert sorted(a["yours"] for a in answers) == [False, True]
    holder = EMAIL if answers[0]["yours"] else TEAMMATE
    refused = next(a for a in answers if not a["yours"])
    assert refused["holder"] == holder
    assert holder in refused["reason"]
    await _detached(maker, run.id)
    taken = [e for e in await _events(maker, run.id) if e.type == "terminal_seat_taken"]
    assert [e.details["by"] for e in taken] == [holder]


async def test_a_forced_claim_records_the_old_holders_typing_under_it_and_drops_its_rest(
    maker: async_sessionmaker[AsyncSession], tmux: Tmux
) -> None:
    """Silent and costly: typing charged to the wrong person, or a displaced holder still
    reaching the session, would make the input audit lie."""
    # A burst that waits this long is recorded only by the takeover's flush, or at the detach.
    timing = Timing(recheck=0.2, audit_quiet=30, audit_max=60)
    run = await _seed(maker, target=tmux.target)
    first, later = f"operator-{uuid4().hex[:8]}\r".encode(), f"late-{uuid4().hex[:8]}"

    async with (
        _started(create_app(_settings(), maker, timing=timing)) as running,
        _open(running.url(run.id)) as operator,
        _open(running.url(run.id), email=TEAMMATE) as teammate,
    ):
        await _hello(operator)
        await _hello(teammate)
        await _seated(operator)
        await operator.send(first)
        await eventually(lambda: first.decode().strip() in tmux.pane(), "operator's typing")
        refused = await _claim(teammate)
        assert (refused["yours"], refused["holder"], refused["taken"]) == (False, EMAIL, False)
        taken = await _claim(teammate, force=True)
        assert (taken["yours"], taken["holder"]) == (True, TEAMMATE)
        lost = await _frame(operator, "seat")
        assert lost == _seat(TEAMMATE, taken=True)
        await operator.send(f"{later}\r".encode())
        await teammate.send(b"teammate\r")
        await eventually(
            lambda: tmux.read_only() == [False, True], "teammate writable, operator not"
        )
        await asyncio.sleep(0.3)
        assert later not in tmux.pane()

    await _detached(maker, run.id)
    await eventually(
        lambda: _count(maker, run.id, EventType.TERMINAL_DETACHED, 2), "both detaches recorded"
    )
    events = await _events(maker, run.id)
    assert _seat_events(events) == [
        (EventType.TERMINAL_SEAT_TAKEN, EMAIL, None),
        (EventType.TERMINAL_INPUT, EMAIL, len(first)),
        (EventType.TERMINAL_SEAT_TAKEN, TEAMMATE, EMAIL),
        (EventType.TERMINAL_INPUT, TEAMMATE, len(b"teammate\r")),
    ]
    forced = [e.details["forced"] for e in events if e.type == EventType.TERMINAL_SEAT_TAKEN]
    assert forced == [False, True]


async def _count(
    maker: async_sessionmaker[AsyncSession], run_id: int, type: EventType, n: int
) -> bool:
    return sum(e.type == type for e in await _events(maker, run_id)) == n


async def test_the_seat_frees_once_its_holder_stops_typing_for_the_idle_time(
    maker: async_sessionmaker[AsyncSession], tmux: Tmux
) -> None:
    """Silent and costly: a holder who walked away would keep everyone else out of the
    session until someone took it from them on purpose."""
    idle = 0.8
    timing = Timing(recheck=0.2, audit_quiet=0.2, audit_max=1.0, seat_idle=idle)
    run = await _seed(maker, target=tmux.target)

    async with (
        _started(create_app(_settings(), maker, timing=timing)) as running,
        _open(running.url(run.id)) as operator,
        _open(running.url(run.id), email=TEAMMATE) as teammate,
    ):
        await _hello(operator)
        await _hello(teammate)
        await _seated(operator)
        for _ in range(8):
            await operator.send(b".")
            await asyncio.sleep(idle / 4)
        assert running.gateway.registry.holder(run.id) is not None

        lost = await _frame(operator, "seat", within=idle * 3)
        assert (lost["holder"], lost["yours"], lost["taken"]) == (None, False, False)
        assert lost["reason"]
        await _until_status(teammate, None)
        await eventually(lambda: tmux.read_only() == [True, True], "both clients read-only")
        assert (await _claim(teammate))["yours"] is True
    assert SEAT_IDLE == Timing().seat_idle == 120.0


async def test_the_seat_frees_when_its_holder_releases_it_or_detaches(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id), email=TEAMMATE) as teammate:
        await _hello(teammate)
        async with _open(gateway.url(run.id)) as operator:
            await _hello(operator)
            await _seated(operator)
            await _until_status(teammate, EMAIL)
            await operator.send(json.dumps({"type": "release"}))
            released = await _frame(operator, "seat")
            assert released == _seat(None)
            await _until_status(teammate, None)
            await _seated(operator)
            await _until_status(teammate, EMAIL)
            await teammate.send(json.dumps({"type": "release"}))
            assert await _frame(teammate, "seat") == _seat(EMAIL)
            assert running_holder(gateway, run.id) == EMAIL
        status = await _until_status(teammate, None)
        assert status["writers"] == 0
        assert (await _claim(teammate))["yours"] is True


def running_holder(running: Running, run_id: int) -> str | None:
    holder = running.gateway.registry.holder(run_id)
    return holder.by if holder is not None else None


async def test_a_claim_that_cannot_be_recorded_closes_1011_and_is_never_writable(
    gateway: Running,
    tmux: Tmux,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a seat that cannot be charged to a person never reaches the session."""
    recording, fail = asyncio.Event(), asyncio.Event()

    async def unrecordable(self: ViewerAudit, **_: object) -> None:
        recording.set()
        await fail.wait()
        raise RuntimeError("the database is down")

    monkeypatch.setattr(ViewerAudit, "seat_taken", unrecordable)
    run = await _seed(maker, target=tmux.target)

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        await ws.send(json.dumps({"type": "claim"}))
        await asyncio.wait_for(recording.wait(), 5)
        assert running_holder(gateway, run.id) == EMAIL
        assert tmux.read_only() == [True]
        fail.set()
        code, _ = await _closed(ws)

    assert code == CloseCode.INTERNAL
    events = await _detached(maker, run.id)
    assert not [e for e in events if e.type == EventType.TERMINAL_SEAT_TAKEN]


async def test_keys_sent_before_a_claim_is_granted_are_dropped_not_held_for_after(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: keys held back and passed on once the grant lands would reach the
    session before the dashboard showed the person as typing into it."""
    run = await _seed(maker, target=tmux.target)
    early, late = f"early-{uuid4().hex[:8]}", f"late-{uuid4().hex[:8]}"

    async with _open(gateway.url(run.id)) as ws:
        await _hello(ws)
        await ws.send(json.dumps({"type": "claim"}))
        await ws.send(f"{early}\r".encode())
        assert (await _frame(ws, "seat"))["yours"] is True
        await ws.send(f"{late}\r".encode())
        await eventually(lambda: late in tmux.pane(), "the typing after the grant")
        assert early not in tmux.pane()

    events = await _detached(maker, run.id)
    inputs = [e.details["bytes"] for e in events if e.type == EventType.TERMINAL_INPUT]
    assert sum(inputs) == len(late) + 1


class FakeClient:
    def __init__(self, writable: bool, close_delay: float) -> None:
        self.writable = writable
        self.closed = False
        self.typed: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self._close_delay = close_delay
        self.ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def write(self, data: bytes) -> None:
        self.typed.append(data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def close(self) -> None:
        await asyncio.sleep(self._close_delay)
        self.closed = True
        if not self.ended.done():
            self.ended.set_result("closed")


class SeatRelay:
    """Fake tmux clients, keeping what tmux would know: which are open, and which writable."""

    def __init__(self, *, writable_close: float = 0.0, read_only_close: float = 0.0) -> None:
        self.clients: list[FakeClient] = []
        #: How many writable clients were open as each writable one was made.
        self.overlaps: list[int] = []
        #: While set to an unset event, a writable attach waits on it.
        self.hold: asyncio.Event | None = None
        self.holding = False
        self._close = {True: writable_close, False: read_only_close}
        #: Whether each attach asked for the pane's history.
        self.histories: list[bool] = []

    async def attach(
        self,
        target: TerminalTarget,
        *,
        writable: bool,
        cols: int,
        rows: int,
        on_output: Callable[[bytes], None],
        history: bool = True,
    ) -> FakeClient:
        self.histories.append(history)
        if writable and self.hold is not None:
            self.holding = True
            await self.hold.wait()
        if writable:
            self.overlaps.append(sum(c.writable and not c.closed for c in self.clients))
        self.clients.append(FakeClient(writable, self._close[writable]))
        return self.clients[-1]

    def open_writable(self) -> int:
        return sum(c.writable and not c.closed for c in self.clients)


@asynccontextmanager
async def _faked(
    maker: async_sessionmaker[AsyncSession], relay: SeatRelay
) -> AsyncIterator[tuple[Running, int]]:
    app = create_app(_settings(), maker, SubstrateRelays(host=relay), timing=FAST)
    target = TerminalTarget(substrate="host", socket="/tmp/unused", session=SESSION)
    run = await _seed(maker, target=target)
    async with _started(app) as running:
        yield running, run.id


async def test_the_displaced_holders_client_is_closed_before_the_takers_is_writable(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Only the holder's client is writable, at every instant: two writable clients would leave
    the displaced viewer behind the gateway's drop alone, with tmux's lock open."""
    relay = SeatRelay(writable_close=0.5)

    async with (
        _faked(maker, relay) as (running, run_id),
        _open(running.url(run_id)) as operator,
        _open(running.url(run_id), email=TEAMMATE) as teammate,
    ):
        await _hello(operator)
        await _hello(teammate)
        await _seated(operator)
        assert (await _claim(teammate, force=True))["yours"] is True

    assert relay.overlaps == [0, 0]


async def test_a_holder_displaced_during_its_own_claim_never_gets_a_writable_client(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The loser is still swapping its read-only client out when the seat is taken from it: it
    must not finish that swap writable."""
    relay = SeatRelay(read_only_close=0.5, writable_close=1.0)

    async with (
        _faked(maker, relay) as (running, run_id),
        _open(running.url(run_id)) as operator,
        _open(running.url(run_id), email=TEAMMATE) as teammate,
    ):
        await _hello(operator)
        await _hello(teammate)
        await operator.send(json.dumps({"type": "claim"}))
        # Inside the half second operator's claim takes to close her read-only client.
        await asyncio.sleep(0.2)
        assert (await _claim(teammate, force=True))["yours"] is True

    assert relay.overlaps == [0]


async def test_a_read_only_viewers_resize_reaches_its_own_client(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    relay = SeatRelay()

    async with _faked(maker, relay) as (running, run_id), _open(running.url(run_id)) as operator:
        await _hello(operator)
        await operator.send(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        await eventually(
            lambda: relay.clients[0].sizes == [(100, 30)], "the read-only client's resize"
        )


async def test_a_claim_after_a_release_that_withdrew_another_is_answered(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    admitting = asyncio.Event()
    admitting.set()
    admit = Gateway.admit

    async def held(self: Gateway, step_run_id: int) -> Any:
        await admitting.wait()
        return await admit(self, step_run_id)

    monkeypatch.setattr(Gateway, "admit", held)
    relay = SeatRelay()

    async with _faked(maker, relay) as (running, run_id), _open(running.url(run_id)) as operator:
        await _hello(operator)
        admitting.clear()
        try:
            for frame in ({"type": "claim"}, {"type": "release"}, {"type": "claim"}):
                await operator.send(json.dumps(frame))
            assert await _frame(operator, "seat", within=1) == _seat(None)
            assert await _frame(operator, "seat", within=1) == _seat(None)
        finally:
            admitting.set()
        assert await _frame(operator, "seat") == _seat(EMAIL, yours=True)
        assert running_holder(running, run_id) == EMAIL
        await eventually(lambda: relay.open_writable() == 1, "the holder's writable client")
        assert relay.histories == [True] + [False] * (len(relay.clients) - 1)


async def _takings(
    maker: async_sessionmaker[AsyncSession], run_id: int, viewers: int
) -> list[tuple[str, str | None]]:
    await eventually(
        lambda: _count(maker, run_id, EventType.TERMINAL_DETACHED, viewers), "every detach"
    )
    events = await _events(maker, run_id)
    return [
        (e.details["by"], e.details["from"])
        for e in events
        if e.type == EventType.TERMINAL_SEAT_TAKEN
    ]


@pytest.mark.parametrize("then", ["teammate takes it from operator", "operator releases it"])
async def test_a_taking_is_recorded_after_the_one_it_displaced_and_never_once_released(
    maker: async_sessionmaker[AsyncSession], then: str
) -> None:
    """Silent and costly: read in order, the takings say who held the keyboard when; one
    recorded out of order, or for a seat let go before it was held, names the wrong person."""
    # Teammate-2's writable client takes a second to close, which operator's taking waits for.
    relay = SeatRelay(writable_close=1.0)

    async with (
        _faked(maker, relay) as (running, run_id),
        _open(running.url(run_id), email=TEAMMATE_2) as teammate_2,
        _open(running.url(run_id)) as operator,
        _open(running.url(run_id), email=TEAMMATE) as teammate,
    ):
        for ws in (teammate_2, operator, teammate):
            await _hello(ws)
        await _seated(teammate_2)
        await operator.send(json.dumps({"type": "claim", "force": True}))
        await asyncio.sleep(0.2)
        if then == "operator releases it":
            await operator.send(json.dumps({"type": "release"}))
            await eventually(lambda: running_holder(running, run_id) is None, "the seat free")
            await asyncio.sleep(1.2)
        else:
            assert (await _claim(teammate, force=True))["yours"] is True

    takings = await _takings(maker, run_id, 3)
    if then == "operator releases it":
        assert takings == [(TEAMMATE_2, None)]
    else:
        assert takings == [(TEAMMATE_2, None), (EMAIL, TEAMMATE_2), (TEAMMATE, EMAIL)]


async def test_a_seat_released_before_its_taking_is_recorded_is_not_recorded_on_a_new_claim(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Released, then claimed again, while the taking still waits on the old holder: the new
    claim must not make the first one record a seat its viewer let go, and is answered."""
    relay = SeatRelay(writable_close=1.0)

    async with (
        _faked(maker, relay) as (running, run_id),
        _open(running.url(run_id), email=TEAMMATE_2) as teammate_2,
        _open(running.url(run_id)) as operator,
    ):
        await _hello(teammate_2)
        await _hello(operator)
        await _seated(teammate_2)
        await operator.send(json.dumps({"type": "claim", "force": True}))
        await asyncio.sleep(0.2)
        await operator.send(json.dumps({"type": "release"}))
        await operator.send(json.dumps({"type": "claim"}))
        answers = [await _frame(operator, "seat", within=3) for _ in range(3)]
        assert answers == [_seat(None)] * 3
        assert running_holder(running, run_id) is None

    assert await _takings(maker, run_id, 2) == [(TEAMMATE_2, None)]


async def test_a_release_is_answered_while_a_claims_reattach_is_pending(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A slow E2B attach must not hold the seat, or the keys sent meanwhile, for a viewer that
    has let go."""
    relay = SeatRelay()
    relay.hold = asyncio.Event()

    async with _faked(maker, relay) as (running, run_id), _open(running.url(run_id)) as operator:
        await _hello(operator)
        await operator.send(json.dumps({"type": "claim"}))
        await eventually(lambda: relay.holding, "the writable attach pending")
        try:
            await operator.send(b"while-pending")
            await operator.send(json.dumps({"type": "release"}))
            assert await _frame(operator, "seat", within=1) == _seat(None)
        finally:
            relay.hold.set()
        await eventually(lambda: relay.open_writable() == 0, "no writable client left")
        assert running_holder(running, run_id) is None
    assert [c.typed for c in relay.clients] == [[]] * len(relay.clients)


async def test_shutdown_during_a_seat_swap_returns_once_every_client_is_closed(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Silent and costly: on E2B a client a swap left behind is a root pty with no time limit,
    and a writable one if the swap was demoting a holder."""
    relay = SeatRelay(read_only_close=0.5)

    async with _faked(maker, relay) as (running, run_id), _open(running.url(run_id)) as operator:
        await _hello(operator)
        await operator.send(json.dumps({"type": "claim"}))
        # Inside the half second the swap takes to close the viewer's read-only client.
        await asyncio.sleep(0.2)
        await asyncio.wait_for(running.gateway.shutdown(), 5)
        assert all(c.closed for c in relay.clients)


# --------------------------------------------------------------------------- tickets


def _forged_with_the_session_key(run_id: int) -> str:
    """A ticket's shape signed with the session cookie's key: separate keys refuse it."""
    genuine = read_terminal_ticket(_ticket(run_id), api_token=TOKEN)
    assert genuine is not None
    claims = {"r": run_id, "b": "api-token", "e": genuine.expires, "n": "forged"}
    payload = f"v1.{sessions._b64(json.dumps(claims).encode())}"
    return f"{payload}.{sessions._sign(payload.encode(), TOKEN, sessions._SESSION_KEY)}"


async def test_a_ticket_attaches_once_and_a_second_use_is_refused_before_accept(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Silent and costly: a ticket that could be replayed is a bearer credential that outlives
    its 30 seconds in whatever captured it."""
    run = await _seed(maker, target=tmux.target)
    ticket = _ticket(run.id)

    async with _open(gateway.url(run.id), ticket=ticket) as ws:
        assert ws.subprotocol == TICKET_PROTOCOL
        await _hello(ws)
        with pytest.raises(InvalidStatus) as replayed:
            async with _open(gateway.url(run.id), ticket=ticket):
                pass
    await _detached(maker, run.id)

    assert replayed.value.response.status_code == 403
    attached = [e for e in await _events(maker, run.id) if e.type == EventType.TERMINAL_ATTACHED]
    assert [(e.details["by"], e.details["via"]) for e in attached] == [("api-token", "ticket")]


_REFUSED = [
    "expired",
    "another run",
    "foreign origin",
    "another api token",
    "signed with the session key",
    "session cookie as the ticket",
    "ticket as the session cookie",
    "ticket in the url",
    "ticket without the protocol",
    "minting session over",
]


@pytest.mark.parametrize("case", _REFUSED)
async def test_a_ticket_that_does_not_fit_is_refused_before_accept(
    gateway: Running, tmux: Tmux, maker: async_sessionmaker[AsyncSession], case: str
) -> None:
    """Silent and costly: each of these would open a terminal, or type into one, that the
    ticket's holder was never given. The second use of a ticket is the test above."""
    run = await _seed(maker, target=tmux.target)
    other = await _seed(maker, target=tmux.target)
    now = datetime.now(UTC)
    url, origin = gateway.url(run.id), DASHBOARD
    headers: dict[str, str] = {}
    protocols: list[str] | None = None
    match case:
        case "expired":
            protocols = [_ticket(run.id, now=now - timedelta(seconds=TICKET_SECONDS + 1))]
        case "another run":
            protocols = [_ticket(other.id)]
        case "foreign origin":
            origin = "https://evil.test"
            protocols = [_ticket(run.id)]
        case "another api token":
            protocols = [_ticket(run.id, api_token="the-token-before-rotation")]
        case "signed with the session key":
            protocols = [_forged_with_the_session_key(run.id)]
        case "session cookie as the ticket":
            protocols = [_cookie().split("=", 1)[1]]
        case "ticket as the session cookie":
            headers = {"Cookie": f"{SESSION_COOKIE}={_ticket(run.id)}"}
        case "ticket in the url":
            url = f"{url}&ticket={_ticket(run.id)}"
        case "ticket without the protocol":
            protocols = [_ticket(run.id)]
        case "minting session over":
            protocols = [_ticket(run.id, by=EMAIL, until=int(now.timestamp()) - 1)]
        case _:
            raise AssertionError(case)
    if case != "ticket without the protocol" and protocols is not None:
        protocols = [TICKET_PROTOCOL, *protocols]

    with pytest.raises(InvalidStatus) as refused:
        async with connect(
            url,
            origin=Origin(origin),
            additional_headers=headers,
            subprotocols=[Subprotocol(p) for p in protocols] if protocols is not None else None,
        ):
            pass

    assert refused.value.response.status_code == 403
    assert tmux.clients() == 0
    assert await _events(maker, run.id) == []


_HOSTILE = [
    "",
    "v1",
    "v1..",
    "v1.e30.",
    "v2.e30.00",
    "v1.a.b.c",
    "v1.é.ü",
    "\x00" * 64,
    "v1." + "A" * 5000 + ".00",
]


def _signed(body: str) -> str:
    payload = f"v1.{body}"
    return f"{payload}.{sessions._sign(payload.encode(), TOKEN, sessions._TICKET_KEY)}"


def _signed_claims(**claims: object) -> str:
    return _signed(sessions._b64(json.dumps(claims).encode()))


async def test_a_malformed_ticket_reads_as_none_and_never_raises() -> None:
    """Silent and costly: the gateway reads this from a stranger's handshake, before any
    credential is known, and an exception there is a 500 rather than a refusal."""
    good = {"r": 7, "m": "type", "b": "api-token", "e": 2**40, "n": "nonce"}
    genuine = _ticket(7)
    hostile = [
        *_HOSTILE,
        genuine[:-1] + ("0" if genuine[-1] != "0" else "1"),
        genuine[:-8],
        _signed("!!!!"),
        _signed(sessions._b64(b"not json")),
        _signed(sessions._b64(b"[1, 2]")),
        _signed(sessions._b64(b"[" * 100_000 + b"]" * 100_000)),
        _signed_claims(**{**good, "r": True}),
        _signed_claims(**{**good, "r": "7"}),
        _signed_claims(**{**good, "b": ""}),
        _signed_claims(**{**good, "b": 7}),
        _signed_claims(**{**good, "u": "later"}),
        _signed_claims(**{k: v for k, v in good.items() if k != "n"}),
    ]

    assert [read_terminal_ticket(value, api_token=TOKEN) for value in hostile] == [None] * len(
        hostile
    )
    assert read_terminal_ticket(_signed_claims(**good), api_token=TOKEN) is not None


async def test_typing_through_a_bearer_minted_ticket_is_recorded_as_the_api_token(
    gateway: Running,
    tmux: Tmux,
    maker: async_sessionmaker[AsyncSession],
    async_session: AsyncSession,
) -> None:
    """Silent and costly: the terminal is where a person pastes a credential, and the token
    names no one, so the record says so rather than a name the token could forge."""
    run = await _seed(maker, target=tmux.target)
    api = build_app(_settings(), async_session)
    marker = f"marker-{uuid4().hex}"

    async with client_for(api) as client:
        minted = await client.post(
            f"/api/step-runs/{run.id}/terminal-ticket",
            # Who a ticket names is the caller's, never the body's.
            json={"mode": "type", "b": "victim@example.com", "by": "victim@example.com", "u": 1},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert minted.status_code == 201
    ticket = minted.json()["ticket"]
    claims = read_terminal_ticket(ticket, api_token=TOKEN)
    assert claims is not None and (claims.by, claims.until) == ("api-token", None)
    async with _open(gateway.url(run.id, "type"), ticket=ticket) as ws:
        await _hello(ws)
        await _seated(ws)
        await ws.send(f"{marker}\r".encode())
        await eventually(lambda: marker in tmux.pane(), "the typed marker in the pane")

    events = await _detached(maker, run.id)
    inputs = [e for e in events if e.type == EventType.TERMINAL_INPUT]
    attached = [e for e in events if e.type == EventType.TERMINAL_ATTACHED]
    assert [(e.details["by"], e.details["via"]) for e in attached] == [("api-token", "ticket")]
    assert {e.details["by"] for e in inputs} == {"api-token"}
    assert sum(e.details["bytes"] for e in inputs) == len(marker) + 1
    async with maker() as session:
        leaked = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(cast(Event.details, Text).contains(marker))
        )
    assert leaked == 0


async def test_a_session_minted_ticket_names_the_person_and_ends_with_the_session(
    gateway: Running,
    tmux: Tmux,
    maker: async_sessionmaker[AsyncSession],
    async_session: AsyncSession,
) -> None:
    """Silent and costly: without `until` a ticket minted in a session's last second would keep
    a terminal open for as long as the gateway stayed up."""
    run = await _seed(maker, target=tmux.target)
    api = build_app(_settings(), async_session)
    cookie = mint_session(EMAIL, verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=2)

    async with client_for(api) as client:
        minted = await client.post(
            f"/api/step-runs/{run.id}/terminal-ticket",
            json={"mode": "watch"},
            headers={"Cookie": f"{SESSION_COOKIE}={cookie}", "Sec-Fetch-Site": "same-origin"},
        )
    assert minted.status_code == 201
    ticket = minted.json()["ticket"]
    claims = read_terminal_ticket(ticket, api_token=TOKEN)
    assert claims is not None and claims.by == EMAIL
    assert claims.until == int(cookie.split(".")[1])

    async with _open(gateway.url(run.id), ticket=ticket) as ws:
        await _hello(ws)
        code, _ = await _closed(ws, within=5)

    assert code == CloseCode.SIGNED_OUT


@pytest.mark.parametrize("case", ["no credential", "switched off"])
async def test_the_ticket_route_refuses_no_credential_and_a_switched_off_terminal(
    maker: async_sessionmaker[AsyncSession], async_session: AsyncSession, case: str
) -> None:
    run = await _seed(maker, target=None)
    settings = _settings()
    if case == "switched off":
        settings = settings.model_copy(update={"terminal": TerminalSettings()})
    headers = {} if case == "no credential" else {"Authorization": f"Bearer {TOKEN}"}

    async with client_for(build_app(settings, async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{run.id}/terminal-ticket", json={"mode": "watch"}, headers=headers
        )

    assert response.status_code == (401 if case == "no credential" else 409)
    assert "ticket" not in response.json()


async def test_a_managed_runtimes_sandbox_is_reached_with_the_managed_key(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """Never the stale key the row still stores: that names another team."""
    dialed: list[str] = []
    relays = SubstrateRelays(
        host=HostTmuxRelay(),
        e2b=lambda key: dialed.append(key) or HostTmuxRelay(),
        managed=ManagedSandbox(api_key=SecretStr("e2b_managed_pool")),
    )
    async with maker() as session:
        row = Runtime(
            organization_id=await default_organization_id(session),
            slug=f"rt-{uuid4().hex[:10]}",
            name="managed",
            type="claude_api",
            details={"isolation": "e2b", "sandbox_key": "managed"},
        )
        session.add(row)
        await session.flush()
        await store.put_for_runtime(session, row.id, "E2B_API_KEY", "e2b_stale_own_key")
        run = TerminalRun(
            step_run_id=1,
            project_id=1,
            ticket_id=1,
            step="develop",
            attempt=1,
            status=StepRunStatus.RUNNING,
            runtime_id=row.id,
        )
        target = TerminalTarget(substrate="e2b", socket="/tmp/s", session=SESSION, sandbox_id="x")

        await relays.relay_for(session, run, target)

    assert dialed == ["e2b_managed_pool"]
