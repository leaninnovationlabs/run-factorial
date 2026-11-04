"""The ids each service binds (tech-stack decision 81): a line the worker logs while it handles
a run names the ticket, project, organization, process version, step and run, and a line the API
logs while it answers a request names the request, which the response hands back.

`tests/log_probe.py` stands in for the process's root handler, so nothing here configures the
test runner's logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orchestrator.api import chat_stream
from orchestrator.api.chat_stream import ChatStreamHub
from orchestrator.api.main import RequestIds, create_app, create_site
from orchestrator.config import ProcessConfig
from orchestrator.db.models import Project, Ticket
from orchestrator.logs import FIELDS, bind
from orchestrator.worker.loop import _reconcile_leased
from runtime.contract import RunStatus, SpawnError
from tests.log_probe import _Lines, probing
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    World,
    _build_world,
    _ensure_leased,
    _fail_observation,
    _process,
    step_runs,
)


@pytest.fixture
def probe() -> Iterator[tuple[logging.Logger, _Lines]]:
    with probing("tests.probe") as handler:
        yield logging.getLogger("tests.probe"), handler


def _bound(line: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in line.items() if k in FIELDS}


async def _ticket_fields(world: World) -> dict[str, Any]:
    async with world.sessions() as session:
        ticket = await session.get_one(Ticket, world.ticket_id)
        project = await session.get_one(Project, world.project_id)
        return {
            "ticket_id": world.ticket_id,
            "project_id": world.project_id,
            "org_id": project.organization_id,
            "process_version_id": ticket.process_version_id,
            "step": ticket.current_step,
        }


async def _reconcile(world: World) -> None:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _ensure_leased(session, ticket)
    await _reconcile_leased(
        world.ticket_id,
        world.deps,
        scope=world.sessions,
        lease_owner=_LEASE_OWNER,
        lease_ttl=_LEASE_TTL,
        listings=None,
        quarantine_after=3,
    )


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_a_spawn_and_its_poll_log_with_the_ticket_and_the_run(
    world: World, probe: tuple[logging.Logger, _Lines], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = probe
    adapter = world.adapter
    spawn, status = adapter.spawn, adapter.status

    async def logged_spawn(*args: Any, **kwargs: Any) -> Any:
        logger.info("spawning")
        return await spawn(*args, **kwargs)

    async def logged_status(*args: Any, **kwargs: Any) -> Any:
        logger.info("polling")
        return await status(*args, **kwargs)

    monkeypatch.setattr(adapter, "spawn", logged_spawn)
    monkeypatch.setattr(adapter, "status", logged_status)
    expected = await _ticket_fields(world)

    await _reconcile(world)
    await _reconcile(world)

    (run,) = await step_runs(world)
    on_run = {**expected, "step_run_id": run.id, "attempt": run.attempt}
    assert [(line["msg"], _bound(line)) for line in handler.lines] == [
        ("spawning", on_run),
        ("polling", on_run),
    ]
    logger.info("after")
    assert _bound(handler.lines[-1]) == {}


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_spawn_names_no_run_the_rollback_erased(world: World) -> None:
    expected = await _ticket_fields(world)
    world.adapter.fail_spawn(SpawnError("busy: run abc123 is still using the one agent slot"))

    with probing("orchestrator.worker.loop") as handler:
        await _reconcile(world)

    (failed,) = [line for line in handler.lines if line["msg"].startswith("reconciling ticket id=")]
    assert _bound(failed) == expected
    assert await step_runs(world) == []


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_an_evaluation_failure_keeps_the_polled_runs_id(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The rollback erases the acceptance, not the run it polled: GitHub's 403 lines must still
    name it."""
    config = _process(f"ci-{uuid4().hex[:8]}").model_dump(mode="json", by_alias=True)
    config["steps"][0]["postconditions"]["all"].append('ci_green("spec/{ticket}-*")')
    world = await _build_world(maker, config=ProcessConfig.model_validate(config))
    await _reconcile(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    _fail_observation(world, "check_runs")

    with probing("orchestrator.worker.reconcile") as handler:
        await _reconcile(world)

    (run,) = await step_runs(world)
    (failed,) = [line for line in handler.lines if "evaluation failed" in line["msg"]]
    assert (failed["step_run_id"], failed["attempt"]) == (run.id, run.attempt)


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_a_promotion_logs_the_rest_of_its_tick_on_the_new_step(
    world: World, probe: tuple[logging.Logger, _Lines], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = probe
    set_labels = world.projector.set_labels

    async def logged_set_labels(*args: Any, **kwargs: Any) -> Any:
        logger.info("labelling")
        return await set_labels(*args, **kwargs)

    monkeypatch.setattr(world.projector, "set_labels", logged_set_labels)
    await _reconcile(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, summary="done")
    await _reconcile(world)

    last = [line for line in handler.lines if line["msg"] == "labelling"][-1]
    assert last["step"] == "spec_gate"
    assert not {"step_run_id", "attempt"} & last.keys()


async def _logs(request: Request) -> PlainTextResponse:
    logging.getLogger("tests.probe").info("answering %s", request.url.path)
    return PlainTextResponse("ok")


async def test_a_request_logs_with_the_id_its_response_carries(
    probe: tuple[logging.Logger, _Lines],
) -> None:
    _, handler = probe
    app = RequestIds(Starlette(routes=[Route("/x", _logs)]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/x")
        second = await client.get("/x")

    ids = [first.headers["x-request-id"], second.headers["x-request-id"]]
    assert [line["request_id"] for line in handler.lines] == ids
    assert ids[0] != ids[1]
    assert all(len(i) == 16 and int(i, 16) >= 0 for i in ids)


async def test_the_served_site_answers_a_request_id() -> None:
    from orchestrator.api.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
    assert len(response.headers["x-request-id"]) == 16


async def _serve(site: ASGIApp, path: str) -> list[Message]:
    """Call `site` as uvicorn does and return what it sends. An exception it lets out fails the
    test: uvicorn would log it a second time, outside the bind."""
    sent: list[Message] = []
    requested = False

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 5),
        "server": ("test", 80),
    }
    async with asyncio.timeout(5):
        await site(scope, receive, send)
    return sent


def _answer(sent: list[Message]) -> tuple[int, str, bytes]:
    start = sent[0]
    assert start["type"] == "http.response.start"
    request_id = dict(start["headers"])[b"x-request-id"].decode()
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], request_id, body


async def _boom() -> None:
    raise RuntimeError("boom")


async def test_an_unhandled_error_is_logged_once_with_its_request_id() -> None:
    api = create_app()
    api.add_api_route("/boom", _boom)
    with probing("orchestrator.api.main") as handler:
        status, request_id, body = _answer(await _serve(create_site(api), "/factorial/boom"))

    assert (status, json.loads(body)) == (500, {"detail": "internal server error"})
    (line,) = [line for line in handler.lines if "exc" in line]
    assert line["request_id"] == request_id


async def test_an_error_outside_the_api_answers_500_with_its_request_id() -> None:
    async def raising(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("outside the api")

    site = create_site(create_app())
    site.router.default = raising
    with probing("orchestrator.api.main") as handler:
        status, request_id, body = _answer(await _serve(site, "/elsewhere"))

    assert (status, json.loads(body)) == (500, {"detail": "internal server error"})
    (line,) = [line for line in handler.lines if "exc" in line]
    assert (line["level"], line["request_id"]) == ("ERROR", request_id)


async def _stream() -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"a"
        raise RuntimeError("midway")

    return StreamingResponse(chunks())


async def test_a_stream_that_fails_midway_logs_once() -> None:
    api = create_app()
    api.add_api_route("/stream", _stream)
    with probing("orchestrator.api.main") as handler:
        status, request_id, body = _answer(await _serve(create_site(api), "/factorial/stream"))

    assert (status, body) == (200, b"a")
    (line,) = [line for line in handler.lines if "exc" in line]
    assert line["request_id"] == request_id


async def test_the_chat_listener_carries_no_request_id(
    probe: tuple[logging.Logger, _Lines], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first stream to open starts the listener, which outlives it by hours."""
    logger, handler = probe

    async def listening(*args: Any, stopping: asyncio.Event, **kwargs: Any) -> None:
        logger.info("listening")
        await stopping.wait()

    monkeypatch.setattr(chat_stream, "listen_forever", listening)
    hub = ChatStreamHub(database_url=lambda: "unused")
    await hub.start()
    with bind(request_id="a1b2c3d4e5f60718"):
        async with hub.subscribe(1):
            await asyncio.sleep(0)
    await hub.stop()

    (line,) = handler.lines
    assert line["msg"] == "listening"
    assert "request_id" not in line
