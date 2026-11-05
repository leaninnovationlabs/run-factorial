"""Approve and decline: a proposal runs once, as its owner, through the dashboard's own services
(design.md §7.18 items 1 and 6). Also the drafts the process editor opens, and expiry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastmcp.tools.base import ToolResult
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.deps import get_db_session, settings_dependency
from orchestrator.api.main import create_app
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.assistant_settings import save_assistant_settings
from orchestrator.api.services.controls import request_resume
from orchestrator.api.services.errors import Conflict
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.services.refs import resolve_ticket
from orchestrator.assistant import loop as assistant_loop
from orchestrator.assistant import proposals
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.items import append_item
from orchestrator.assistant.loop import serve
from orchestrator.assistant.proposals import expire_stale, outcome_line, record_proposal
from orchestrator.assistant.turns import enqueue_turn
from orchestrator.db.base import JsonDict
from orchestrator.db.models import (
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatSpend,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    Event,
    McpAccess,
    StepRun,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.factorial import CONFIG_BRANCH, RepoConfigSource
from orchestrator.mcp_server.context import ToolContext
from orchestrator.mcp_server.tools.controls import run_ticket_exit
from orchestrator.worker.context import SessionPolicy
from runtime.assistant.contract import TurnDone, TurnStop
from runtime.contract import ParkReason
from tests.api_harness import (
    DASHBOARD,
    EXAMPLE,
    SeededChatTurn,
    agent_files,
    build_app,
    client_for,
    deps_for,
    eventually,
    mcp_deps_for,
    mcp_settings,
    port,
    process_settings,
    seed_mcp_token,
    seed_project,
    seed_running_chat,
    seed_ticket,
)
from tests.assistant_fakes import FakeBackend, seed_conversation, wipe_chat
from tests.reconcile_world import (
    BOT_LOGIN,
    World,
    event_rows,
    interactive_world,
    retry_waiting,
    run_tick,
    running_run,
    step_runs,
    ticket_row,
)
from tests.test_config_commits import (
    BASE_SHA,
    PROCESS_PATH,
    FakeContents,
    FakeGitApi,
    lane,
    project_in,
    writer_for,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

API = "/api/assistant"
#: Who a bearer-token request is (decision 12), so whose conversations these are.
ME = "api-token"


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Approve queues turns, and a claim anywhere takes the oldest."""
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


def _per_request(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[], AsyncIterator[AsyncSession]]:
    async def session() -> AsyncIterator[AsyncSession]:
        async with maker() as opened:
            try:
                yield opened
            except BaseException:
                await opened.rollback()
                raise
            else:
                await opened.commit()

    return session


def _app(
    maker: async_sessionmaker[AsyncSession], *, config: FakeContents | None = None
) -> tuple[FastAPI, FakeGitApi | None]:
    """A session per request, as in production, so two requests can race on the locks. The app
    is built with its MCP deps, as `api.main` builds it; `config` is the repo an approved config
    commit reads and writes."""
    settings = mcp_settings()
    deps = mcp_deps_for(settings, maker)
    api: FakeGitApi | None = None
    if config is not None:
        api = FakeGitApi(config)
        writer = writer_for(api)
        deps = replace(
            deps,
            config_source=port(RepoConfigSource(config)),
            commit_writer=port(writer),
        )

    app = create_app(mcp=deps)
    app.dependency_overrides[get_db_session] = _per_request(maker)
    app.dependency_overrides[settings_dependency] = lambda: settings
    return app, api


@pytest.fixture
async def client(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    app, _ = _app(maker)
    async with client_for(app) as client:
        yield client


@pytest.fixture
async def mine(maker: async_sessionmaker[AsyncSession]) -> SeededChatTurn:
    async with maker() as session:
        seeded = await seed_running_chat(session, owner=ME)
        await session.commit()
    return seeded


async def propose(
    maker: async_sessionmaker[AsyncSession],
    chat: SeededChatTurn,
    tool: str,
    args: JsonDict,
    *,
    ttl: timedelta = timedelta(hours=24),
) -> ChatProposal:
    async with maker() as session:
        proposal = await record_proposal(
            session,
            scope=chat.scope,
            tool=tool,
            args=args,
            preview={"ticket": args.get("ticket")},
            ttl=ttl,
            now=datetime.now(UTC),
        )
        await session.commit()
    return proposal


@pytest.fixture
async def pending_retry(
    maker: async_sessionmaker[AsyncSession], world: World, mine: SeededChatTurn
) -> ChatProposal:
    await retry_waiting(world)
    return await propose(
        maker, mine, "factorial_ticket_retry", {"ticket": f"id:{world.ticket_id}", "project": None}
    )


async def a_running_ticket(maker: async_sessionmaker[AsyncSession]) -> Ticket:
    async with maker() as session:
        ticket = await seed_ticket(session, await seed_project(session))
        await session.commit()
    return ticket


async def proposal_row(maker: async_sessionmaker[AsyncSession], proposal_id: int) -> ChatProposal:
    async with maker() as session:
        return await session.get_one(ChatProposal, proposal_id)


async def card_of(maker: async_sessionmaker[AsyncSession], proposal: ChatProposal) -> JsonDict:
    async with maker() as session:
        return (await session.get_one(ChatItem, proposal.item_id)).payload


async def latest_event(maker: async_sessionmaker[AsyncSession], kind: str) -> Event:
    async with maker() as session:
        return (
            await session.scalars(select(Event).where(Event.type == kind).order_by(Event.id.desc()))
        ).first() or pytest.fail(f"no {kind} event")


async def events_for(
    maker: async_sessionmaker[AsyncSession], kind: str, proposal_id: int
) -> list[Event]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(Event).where(
                        Event.type == kind,
                        Event.details["chat_proposal_id"].as_integer() == proposal_id,
                    )
                )
            ).all()
        )


async def queued_turns(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> list[str]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ChatTurn.trigger).where(
                        ChatTurn.conversation_id == conversation_id,
                        ChatTurn.status == ChatTurnStatus.QUEUED,
                    )
                )
            ).all()
        )


async def notices(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> list[str]:
    async with maker() as session:
        return [
            payload["text"]
            for payload in (
                await session.scalars(
                    select(ChatItem.payload)
                    .where(
                        ChatItem.conversation_id == conversation_id,
                        ChatItem.kind == ChatItemKind.NOTICE,
                    )
                    .order_by(ChatItem.id)
                )
            ).all()
        ]


def approve_url(proposal: ChatProposal) -> str:
    return f"{API}/proposals/{proposal.id}/approve"


def decline_url(proposal: ChatProposal) -> str:
    return f"{API}/proposals/{proposal.id}/decline"


async def test_approve_runs_the_stored_arguments_as_chat(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    world: World,
    pending_retry: ChatProposal,
) -> None:
    r = await client.post(approve_url(pending_retry), headers=auth | DASHBOARD)

    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
    event = await latest_event(maker, "retry_requested")
    assert event.details["requested_via"] == "chat"
    assert event.details["chat_proposal_id"] == pending_retry.id
    assert event.details["requested_by"] == ME
    assert event.ticket_id == world.ticket_id
    body = r.json()
    assert (body["proposal_id"], body["card_item_id"]) == (pending_retry.id, pending_retry.item_id)
    assert (body["result"]["action"], body["result"]["status"]) == ("retry", "requested")
    row = await proposal_row(maker, pending_retry.id)
    assert (row.status, row.decided_by, row.result) == ("succeeded", ME, body["result"])
    assert row.decided_at is not None
    card = await card_of(maker, pending_retry)
    assert (card["status"], card["decided_by"], card["result"]) == ("succeeded", ME, body["result"])


async def test_double_approve_runs_once(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    pending_retry: ChatProposal,
) -> None:
    racing = await asyncio.gather(
        client.post(approve_url(pending_retry), headers=auth | DASHBOARD),
        client.post(approve_url(pending_retry), headers=auth | DASHBOARD),
    )
    again = await client.post(approve_url(pending_retry), headers=auth | DASHBOARD)

    assert sorted(r.status_code for r in racing) == [200, 409]
    assert again.status_code == 409
    assert again.json()["code"] == "proposal_decided"
    assert len(await events_for(maker, "retry_requested", pending_retry.id)) == 1


async def test_non_owner_gets_404(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    chat_turn: SeededChatTurn,
) -> None:
    ticket = await a_running_ticket(maker)
    theirs = await propose(
        maker, chat_turn, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    for url in (approve_url(theirs), decline_url(theirs), f"{API}/proposals/999999999/approve"):
        response = await client.post(url, headers=auth | DASHBOARD)
        assert response.status_code == 404, url

    assert (await proposal_row(maker, theirs.id)).status == ChatProposalStatus.PENDING
    assert await events_for(maker, "exit_requested", theirs.id) == []


async def test_missing_origin_gets_403(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    for headers in (auth, auth | {"Origin": "https://evil.example"}):
        for url in (approve_url(proposal), decline_url(proposal)):
            assert (await client.post(url, headers=headers)).status_code == 403, (url, headers)
    # A chat-access token, a history of the old mount, is no way in: these are dashboard routes
    # behind the API's gate.
    async with maker() as session:
        _, chat_jwt = await seed_mcp_token(session, access=McpAccess.CHAT)
        await session.commit()
    chat_token = {"Authorization": f"Bearer {chat_jwt}"}
    as_the_model = await client.post(approve_url(proposal), headers=chat_token | DASHBOARD)
    assert as_the_model.status_code == 401

    assert (await proposal_row(maker, proposal.id)).status == ChatProposalStatus.PENDING
    assert await events_for(maker, "exit_requested", proposal.id) == []


async def test_expired_proposal_is_refused(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker,
        mine,
        "factorial_ticket_exit",
        {"ticket": f"id:{ticket.id}", "project": None},
        ttl=timedelta(seconds=-1),
    )

    first = await client.post(approve_url(proposal), headers=auth | DASHBOARD)
    again = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert (first.status_code, first.json()["code"]) == (410, "proposal_expired")
    assert (again.status_code, again.json()["code"]) == (410, "proposal_expired")
    row = await proposal_row(maker, proposal.id)
    assert (row.status, row.decided_by) == (ChatProposalStatus.EXPIRED, None)
    assert (await card_of(maker, proposal))["status"] == "expired"
    assert await events_for(maker, "exit_requested", proposal.id) == []
    assert await queued_turns(maker, mine.conversation_id) == []


async def test_approve_after_state_moved_records_refused(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    """Review Focus 2: someone exited the ticket between the proposal and the click."""
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )
    async with maker() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == ticket.id).values(status=TicketStatus.EXITED)
        )
        await session.commit()

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200, r.text
    reason = "This ticket has already exited."
    assert r.json()["status"] == "refused"
    assert r.json()["result"] == {"error": reason}
    row = await proposal_row(maker, proposal.id)
    assert (row.status, row.result, row.decided_by) == ("refused", {"error": reason}, ME)
    card = await card_of(maker, proposal)
    assert (card["status"], card["result"]) == ("refused", {"error": reason})
    assert any(reason in text for text in await notices(maker, mine.conversation_id))
    assert await events_for(maker, "exit_requested", proposal.id) == []
    assert await queued_turns(maker, mine.conversation_id) == [ChatTurnTrigger.APPROVAL]


async def test_approve_after_the_branch_moved_records_refused(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn
) -> None:
    """Someone committed to the factorial branch between the card and the click. The card's diff
    was drawn against the old head, so the commit is refused rather than written over theirs."""
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    app, api = _app(
        maker,
        config=FakeContents(
            agent_files("plan-agent", "claude") | agent_files("build-agent", "claude")
        ),
    )
    assert api is not None
    args = {
        "project": project.name,
        "content": lane(),
        "files": [{"path": EXAMPLE, "body": None}],
        "edits": None,
        "message": None,
        "expected_head": BASE_SHA,
    }
    proposal = await propose(maker, mine, "factorial_config_commit", args)
    theirs = "d" * 40
    api.contents.files[theirs] = api.contents.files[BASE_SHA] | {PROCESS_PATH: lane()}
    api.contents.head = theirs
    async with maker() as session:
        await append(
            session,
            type=EventType.CONFIG_COMMITTED,
            project_id=project.id,
            branch=CONFIG_BRANCH,
            sha=theirs,
        )
        await session.commit()

    async with client_for(app) as client:
        r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200, r.text
    reason = (
        "The factorial branch moved since this was proposed. "
        "Ask the assistant to read it again and propose again."
    )
    assert (r.json()["status"], r.json()["result"]) == ("refused", {"error": reason})
    row = await proposal_row(maker, proposal.id)
    assert (row.status, row.result, row.decided_by) == ("refused", {"error": reason}, ME)
    card = await card_of(maker, proposal)
    assert (card["status"], card["result"]) == ("refused", {"error": reason})
    assert (api.trees, api.commits, api.refs) == ([], [], [])
    assert await events_for(maker, "config_committed", proposal.id) == []
    assert await notices(maker, mine.conversation_id) == [f"Approved, but refused: {reason}"]
    assert await queued_turns(maker, mine.conversation_id) == [ChatTurnTrigger.APPROVAL]


async def test_approve_queues_a_continuation_turn(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
    pending_retry: ChatProposal,
) -> None:
    ticket = await a_running_ticket(maker)
    exit_ = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    first = await client.post(approve_url(pending_retry), headers=auth | DASHBOARD)
    assert await queued_turns(maker, mine.conversation_id) == [ChatTurnTrigger.APPROVAL]
    second = await client.post(approve_url(exit_), headers=auth | DASHBOARD)

    assert (first.status_code, second.status_code) == (200, 200)
    # Two approvals in a row share one continuation, which reads both decisions.
    assert await queued_turns(maker, mine.conversation_id) == [ChatTurnTrigger.APPROVAL]
    assert len(await events_for(maker, "exit_requested", exit_.id)) == 1
    lines = await notices(maker, mine.conversation_id)
    assert len(lines) == 2
    assert lines[0].startswith("Approved: Retry a failed run now for ")
    assert lines[1].startswith("Approved: Exit a ticket for ")
    # Nobody runs the worker here, so neither request has landed; the person reads that in
    # their words, and the tool's advice to the model stays out of the conversation.
    for line in lines:
        assert line.endswith(". The worker has not acted on it yet."), line
        assert "factorial_" not in line, line


async def test_decline_queues_nothing_and_records_who(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
    pending_retry: ChatProposal,
) -> None:
    r = await client.post(decline_url(pending_retry), headers=auth | DASHBOARD)
    again = await client.post(decline_url(pending_retry), headers=auth | DASHBOARD)
    late = await client.post(approve_url(pending_retry), headers=auth | DASHBOARD)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": pending_retry.id,
        "status": "declined",
        "card_item_id": pending_retry.item_id,
        "result": None,
    }
    assert (again.status_code, late.status_code) == (409, 409)
    row = await proposal_row(maker, pending_retry.id)
    assert (row.status, row.decided_by) == (ChatProposalStatus.DECLINED, ME)
    assert row.decided_at is not None
    card = await card_of(maker, pending_retry)
    assert (card["status"], card["decided_by"]) == ("declined", ME)
    assert await queued_turns(maker, mine.conversation_id) == []
    assert await events_for(maker, "retry_requested", pending_retry.id) == []


async def test_drafts_are_readable_by_their_owner_only(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
    chat_turn: SeededChatTurn,
) -> None:
    draft: dict[str, Any] = {"project_id": 1, "process": "p", "content": "kind: Process\n"}
    async with maker() as session:

        async def item(chat: SeededChatTurn, kind: ChatItemKind) -> int:
            written = await append_item(
                session,
                conversation_id=chat.conversation_id,
                turn_id=chat.turn_id,
                kind=kind,
                payload=draft,
            )
            return written.id

        own = await item(mine, ChatItemKind.EDITOR_DRAFT)
        theirs = await item(chat_turn, ChatItemKind.EDITOR_DRAFT)
        not_a_draft = await item(mine, ChatItemKind.CHART)
        await session.commit()

    read = await client.get(f"{API}/drafts/{own}", headers=auth)

    assert read.status_code == 200 and read.json() == draft
    for item_id in (theirs, not_a_draft, 999_999_999):
        assert (await client.get(f"{API}/drafts/{item_id}", headers=auth)).status_code == 404
    assert (await client.get(f"{API}/drafts/{own}")).status_code == 401


async def test_expire_stale_ends_only_pending_proposals_past_their_time(
    maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn
) -> None:
    args = {"ticket": "id:1", "project": None}
    stale = await propose(maker, mine, "factorial_ticket_exit", args, ttl=timedelta(seconds=-1))
    fresh = await propose(maker, mine, "factorial_ticket_exit", args)
    decided = await propose(maker, mine, "factorial_ticket_exit", args, ttl=timedelta(seconds=-1))
    async with maker() as session:
        await session.execute(
            update(ChatProposal)
            .where(ChatProposal.id == decided.id)
            .values(status=ChatProposalStatus.DECLINED)
        )
        await session.commit()

    async with maker() as session:
        assert await expire_stale(session, now=datetime.now(UTC)) == 1
        await session.commit()

    statuses = [(await proposal_row(maker, p.id)).status for p in (stale, fresh, decided)]
    assert statuses == ["expired", "pending", "declined"]
    assert (await card_of(maker, stale))["status"] == "expired"
    assert (await proposal_row(maker, stale.id)).decided_at is not None
    assert await queued_turns(maker, mine.conversation_id) == []


async def queued_message(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session:
        conversation = await seed_conversation(session)
        turn = await enqueue_turn(session, conversation.id, trigger=ChatTurnTrigger.USER)
        assert turn is not None
        await append_item(
            session,
            conversation_id=conversation.id,
            turn_id=turn.id,
            kind=ChatItemKind.USER_TEXT,
            payload={"text": "hello"},
        )
        await session.commit()
    return turn.id


async def claimed(maker: async_sessionmaker[AsyncSession], turn_id: int) -> bool:
    async with maker() as session:
        return (await session.get_one(ChatTurn, turn_id)).status != ChatTurnStatus.QUEUED


@asynccontextmanager
async def assistant_serving(
    maker: async_sessionmaker[AsyncSession], database_url: str
) -> AsyncIterator[None]:
    settings = process_settings(database_url, tick_seconds=0.05)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    stop = asyncio.Event()
    serving = asyncio.create_task(
        serve(deps_for(maker, settings, backend), stop=stop, settings=settings)
    )
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(serving, timeout=15)


async def test_the_assistant_loop_expires_a_proposal_for_a_tool_since_renamed(
    maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn, database_url: str
) -> None:
    """A later deploy may rename a tool a pending proposal names: it still expires, and the
    claims behind it go on."""
    args = {"ticket": "id:1", "project": None}
    stale = await propose(maker, mine, "factorial_ticket_exit", args, ttl=timedelta(seconds=-1))
    async with maker() as session:
        await session.execute(
            update(ChatProposal)
            .where(ChatProposal.id == stale.id)
            .values(tool="factorial_ticket_retired")
        )
        await session.commit()
    turn_id = await queued_message(maker)

    async with assistant_serving(maker, database_url):

        async def expired() -> bool:
            return (await proposal_row(maker, stale.id)).status == ChatProposalStatus.EXPIRED

        await eventually(expired)
        await eventually(lambda: claimed(maker, turn_id))

    card = await card_of(maker, stale)
    assert (card["status"], card["title"]) == ("expired", "factorial_ticket_retired")


async def test_an_expiry_that_fails_does_not_stop_the_claims(
    maker: async_sessionmaker[AsyncSession], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_session: AsyncSession, *, now: datetime) -> int:
        raise RuntimeError("expiry broke")

    monkeypatch.setattr(assistant_loop, "expire_stale", broken)
    turn_id = await queued_message(maker)

    async with assistant_serving(maker, database_url):
        await eventually(lambda: claimed(maker, turn_id))


async def test_an_approved_resume_cannot_lift_a_ceiling_park(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Decision 56: only the dashboard's Resume lifts a session ceiling. No twin proposes one,
    but a park can reach its ceiling between the proposal and the click."""
    world, adapter = await interactive_world(
        maker,
        bot_login=BOT_LOGIN,
        sessions=SessionPolicy(terminal_enabled=True, session_ceiling=timedelta(minutes=30)),
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]

    (parked,) = await step_runs(world)
    async with world.sessions() as session:
        run = await session.get_one(StepRun, parked.id)
        with pytest.raises(Conflict, match="session time limit"):
            await request_resume(session, run, Actor.chat(ME, 1), answer=None)

    assert "session_resume_requested" not in {row.type for row in await event_rows(world)}


async def test_the_worker_honours_an_approved_exit_from_the_assistant(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    world: World,
    mine: SeededChatTurn,
) -> None:
    await running_run(world)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{world.ticket_id}", "project": None}
    )

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)
    await run_tick(world)

    assert r.json()["status"] == "succeeded", r.text
    assert (await ticket_row(world)).status == TicketStatus.EXITED
    comments = [body for kind, body in world.projector.calls if kind == "comment"]
    assert any("(from the assistant)" in body for body in comments), comments


async def test_approve_commits_a_config_draft_as_stored(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn
) -> None:
    """The twin stored `files` as JSON; the commit gets them back as the tool's own models."""
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    app, api = _app(
        maker,
        config=FakeContents(
            agent_files("plan-agent", "claude") | agent_files("build-agent", "claude")
        ),
    )
    assert api is not None
    args = {
        "project": project.name,
        "content": lane(),
        "files": [{"path": EXAMPLE, "body": None}],
        "edits": None,
        "message": "Add the lane",
    }
    proposal = await propose(maker, mine, "factorial_config_commit", args)

    async with client_for(app) as client:
        r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
    assert r.json()["result"]["committed"] is True
    assert {PROCESS_PATH, EXAMPLE} <= set(api.written)
    assert api.commits[0]["message"] == (
        "Add the lane\n\nRequested by api-token through the Factorial assistant"
    )
    (event,) = await events_for(maker, "config_committed", proposal.id)
    assert (event.details["requested_via"], event.project_id) == ("chat", project.id)


class _Broken(FakeContents):
    async def resolve_ref(self, repo: str, ref: str) -> str:
        raise RuntimeError("the repo read broke")


async def test_an_approve_that_crashes_is_never_run_again(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn
) -> None:
    """Not a refusal but a fault: the approval was committed before the run, so the proposal
    stays approved and a second click finds it decided."""
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    app, _ = _app(maker, config=_Broken({}))
    args = {"project": project.name, "content": lane(), "files": None, "edits": None}
    proposal = await propose(maker, mine, "factorial_config_commit", args | {"message": None})

    async with client_for(app) as client:
        with pytest.raises(RuntimeError, match="the repo read broke"):
            await client.post(approve_url(proposal), headers=auth | DASHBOARD)
        again = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert (again.status_code, again.json()["code"]) == (409, "proposal_decided")
    row = await proposal_row(maker, proposal.id)
    # The outcome is unknown, so the status stays approved; the card and the model hear why.
    assert row.status == ChatProposalStatus.APPROVED
    assert row.result == {"error": "the run failed unexpectedly (RuntimeError)"}
    card = await card_of(maker, proposal)
    assert (card["status"], card["result"]) == ("approved", row.result)
    assert await notices(maker, mine.conversation_id) == [
        "Approved, but the run failed unexpectedly; check the factorial branch before asking again."
    ]
    assert await queued_turns(maker, mine.conversation_id) == [ChatTurnTrigger.APPROVAL]


async def test_without_the_mcp_deps_approve_runs_nothing(
    auth: dict[str, str], maker: async_sessionmaker[AsyncSession], mine: SeededChatTurn
) -> None:
    """An API built without the MCP server has no ports to run a proposal with; it must not
    make a second set of its own."""
    app = build_app(mcp_settings())
    app.dependency_overrides[get_db_session] = _per_request(maker)
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    async with client_for(app) as client:
        r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 503
    assert (await proposal_row(maker, proposal.id)).status == ChatProposalStatus.PENDING


async def test_a_spent_daily_budget_runs_the_write_but_queues_no_reply(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )
    async with maker() as session:
        session.add(
            ChatSpend(
                organization_id=await default_organization_id(session),
                owner=ME,
                day=datetime.now(UTC).date(),
                cost_usd=Decimal("20.00"),
            )
        )
        await session.commit()

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
    assert len(await events_for(maker, "exit_requested", proposal.id)) == 1
    assert await queued_turns(maker, mine.conversation_id) == []
    (line,) = await notices(maker, mine.conversation_id)
    assert line.endswith(
        "Your daily budget for the assistant is spent, so it will not reply until tomorrow."
    )


async def test_a_spent_organization_budget_runs_the_write_but_queues_no_reply(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )
    async with maker() as session:
        ours = await default_organization_id(session)
        capped = AssistantSettings(organization_daily_budget_usd=Decimal("30.00"))
        await save_assistant_settings(session, ours, capped, by="test")
        session.add(
            ChatSpend(
                organization_id=ours,
                owner="someone-else@example.com",
                day=datetime.now(UTC).date(),
                cost_usd=Decimal("30.00"),
            )
        )
        await session.commit()

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
    assert await queued_turns(maker, mine.conversation_id) == []
    (line,) = await notices(maker, mine.conversation_id)
    assert line.endswith(
        "The organization's daily budget for the assistant is spent, so it will not reply until"
        " tomorrow."
    )


async def _write_then_refuse(
    tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
) -> ToolResult:
    """A service that wrote, then refused without committing."""
    row = await resolve_ticket(tc.session, tc.organization_id, ticket, project)
    await tc.session.execute(update(Ticket).where(Ticket.id == row.id).values(title="half done"))
    raise Conflict("refused after a write")


async def test_a_refusal_keeps_nothing_its_service_wrote(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proposals, "EXECUTORS", {**proposals.EXECUTORS, "factorial_ticket_exit": _write_then_refuse}
    )
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.json()["status"] == "refused" and r.json()["result"] == {
        "error": "refused after a write"
    }
    async with maker() as session:
        assert (await session.get_one(Ticket, ticket.id)).title is None


async def test_a_tool_this_deployment_lacks_is_refused_and_the_card_can_be_declined(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
) -> None:
    """A later deploy renamed the tool a pending proposal names. Approve refuses before it
    decides, so the card stays pending and the person can still decline it."""
    proposal = await propose(
        maker, mine, "factorial_ticket_renamed", {"ticket": "id:1", "project": None}
    )

    approve = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert approve.status_code == 409, approve.text
    assert approve.json() == {
        "code": "proposal_unrunnable",
        "detail": "This proposal names a tool this deployment no longer has.",
    }
    row = await proposal_row(maker, proposal.id)
    assert (row.status, row.decided_by) == (ChatProposalStatus.PENDING, None)
    assert (await card_of(maker, proposal))["status"] == "pending"
    assert await queued_turns(maker, mine.conversation_id) == []
    decline = await client.post(decline_url(proposal), headers=auth | DASHBOARD)
    assert (decline.status_code, decline.json()["status"]) == (200, "declined")


async def test_a_write_whose_conversation_was_deleted_as_it_ran_answers_its_result(
    client: AsyncClient,
    auth: dict[str, str],
    maker: async_sessionmaker[AsyncSession],
    mine: SeededChatTurn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person deleted the conversation in another tab while Approve ran. The write is done,
    so the click answers with its result rather than 404; only the card is gone."""

    async def exit_then_delete(
        tc: ToolContext, actor: Actor, *, ticket: str, project: str | None
    ) -> ToolResult:
        done = await run_ticket_exit(tc, actor, ticket=ticket, project=project)
        async with maker() as other:
            await other.execute(
                delete(ChatConversation).where(ChatConversation.id == mine.conversation_id)
            )
            await other.commit()
        return done

    monkeypatch.setattr(
        proposals, "EXECUTORS", {**proposals.EXECUTORS, "factorial_ticket_exit": exit_then_delete}
    )
    ticket = await a_running_ticket(maker)
    proposal = await propose(
        maker, mine, "factorial_ticket_exit", {"ticket": f"id:{ticket.id}", "project": None}
    )

    r = await client.post(approve_url(proposal), headers=auth | DASHBOARD)

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["proposal_id"], body["status"], body["card_item_id"]) == (
        proposal.id,
        "succeeded",
        proposal.item_id,
    )
    assert (body["result"]["action"], body["result"]["status"]) == ("exit", "requested")
    assert len(await events_for(maker, "exit_requested", proposal.id)) == 1


@pytest.mark.parametrize(
    ("tool", "result", "line"),
    [
        (
            "factorial_ticket_retry",
            {"ref": "acme/api#212", "landed": True, "ticket_status": "running"},
            "Approved: Retry a failed run now for acme/api#212. Done: the ticket is running.",
        ),
        (
            "factorial_ticket_exit",
            {"ref": "acme/api#212", "landed": False, "ticket_status": "running"},
            "Approved: Exit a ticket for acme/api#212. The worker has not acted on it yet.",
        ),
        (
            "factorial_ticket_create",
            {"ref": "acme/api#9", "admitted": True},
            "Approved: Open an issue for the factory for acme/api#9. It is a ticket now.",
        ),
        (
            "factorial_ticket_create",
            {"ref": "acme/api#9", "admitted": False},
            "Approved: Open an issue for the factory for acme/api#9. It is not a ticket yet.",
        ),
        (
            "factorial_config_commit",
            {"sha": "c" * 40, "committed": True, "sync_error": None},
            "Approved: Commit config. Committed ccccccc.",
        ),
        (
            "factorial_config_commit",
            {"sha": "c" * 40, "committed": False, "sync_error": "boom"},
            "Approved: Commit config. The branch already had this config. The sync failed; "
            "the card shows why.",
        ),
    ],
)
async def test_the_notice_says_the_outcome_in_a_persons_words(
    tool: str, result: JsonDict, line: str
) -> None:
    assert outcome_line(tool, result) == line
