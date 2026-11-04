"""The proposal twins among the assistant's tools: every write tool's name, title and input,
recording a proposal and its card instead of writing to the factory."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import FastMCP
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.assistant.proposals import EXECUTORS
from orchestrator.db.models import (
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    Event,
    TicketStatus,
)
from orchestrator.events.channels import CHAT_ITEMS_CHANNEL
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import BranchState
from orchestrator.mcp_server.chat.twins import PRECHECKS, TWINS
from orchestrator.mcp_server.registry import ToolSpec, register
from orchestrator.mcp_server.tokens import CHAT_SCOPE, READ_SCOPE, WRITE_SCOPE
from orchestrator.mcp_server.tools import ALL_TOOLS
from orchestrator.worker.context import SessionPolicy
from runtime.contract import ParkReason
from tests.api_harness import (
    EXAMPLE,
    SeededChatTurn,
    agent_files,
    chat_toolbox,
    mcp_deps_for,
    mcp_settings,
    observed_repo,
    seed_project,
    seed_ticket,
    tool_data,
    tool_refused,
)
from tests.assistant_fakes import wipe_chat
from tests.reconcile_world import (
    BOT_LOGIN,
    World,
    event_rows,
    interactive_world,
    retry_waiting,
    run_tick,
)
from tests.test_config_commits import (
    BASE_SHA,
    PROCESS_PATH,
    FakeContents,
    lane,
    project_in,
)
from tests.test_routes_restart import SPEC, behind, on_lane_p, pull
from tests.wake_probe import listening

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_WRITES = {spec.name for spec in ALL_TOOLS if spec.scope == WRITE_SCOPE}
_CHAT_TOOLS = {
    "factorial_ui_show",
    "factorial_chart",
    "factorial_editor_open",
    "factorial_reference_read",
}


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """A proposal lives in a conversation, and the chat tables outlive the test."""
    yield
    await wipe_chat(maker)


async def _proposals(
    maker: async_sessionmaker[AsyncSession], conversation_id: int
) -> list[ChatProposal]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ChatProposal).where(ChatProposal.conversation_id == conversation_id)
                )
            ).all()
        )


async def _items(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> int:
    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ChatItem)
            .where(ChatItem.conversation_id == conversation_id)
        )
    return count or 0


async def test_every_write_tool_has_a_twin_and_an_executor() -> None:
    assert {t.name for t in TWINS} == _WRITES == set(EXECUTORS) == set(PRECHECKS)
    originals = {spec.name: spec for spec in ALL_TOOLS}
    for twin in TWINS:
        original = originals[twin.name]
        assert (twin.scope, twin.title) == (CHAT_SCOPE, original.title)
        assert not twin.destructive and not twin.idempotent
        assert twin.open_world == original.open_world
        described, originally = inspect.getdoc(twin.fn), inspect.getdoc(original.fn)
        assert described is not None and originally is not None
        assert described.startswith(originally), twin.name
        assert described.endswith(
            "Here this records a proposal the person approves; do not call it again for the "
            "same action."
        )


async def test_each_twin_takes_exactly_its_originals_input_schema() -> None:
    server_a, server_b = FastMCP("a"), FastMCP("b")
    register(server_a, [s for s in ALL_TOOLS if s.scope == WRITE_SCOPE])
    register(server_b, TWINS)
    a = {t.name: t.parameters for t in await server_a.local_provider.list_tools()}
    b = {t.name: t.parameters for t in await server_b.local_provider.list_tools()}
    assert a == b
    assert len(a) == len(_WRITES)


async def test_twin_call_records_a_pending_proposal_and_a_card(
    maker: async_sessionmaker[AsyncSession],
    chat_turn: SeededChatTurn,
    world: World,
    database_url: str,
) -> None:
    await retry_waiting(world)
    tools = chat_toolbox(maker, chat_turn)
    before = datetime.now(UTC)

    async with listening(database_url, CHAT_ITEMS_CHANNEL) as heard:
        out = tool_data(
            await tools.call("factorial_ticket_retry", {"ticket": f"id:{world.ticket_id}"})
        )
        assert await asyncio.wait_for(heard.get(), 5) == str(chat_turn.conversation_id)

    (proposal,) = await _proposals(maker, chat_turn.conversation_id)
    assert out == {
        "proposal_id": proposal.id,
        "status": "pending",
        "card_item_id": proposal.item_id,
        "result": None,
    }
    assert (proposal.tool, proposal.status) == (
        "factorial_ticket_retry",
        ChatProposalStatus.PENDING,
    )
    assert proposal.args == {"ticket": f"id:{world.ticket_id}", "project": None}
    assert (proposal.turn_id, proposal.mcp_token_id) == (chat_turn.turn_id, None)
    assert before + timedelta(hours=24) <= proposal.expires_at
    assert proposal.expires_at <= datetime.now(UTC) + timedelta(hours=24)
    assert proposal.decided_at is None and proposal.result is None
    ref = f"{world.repo}#{world.issue_number}"
    assert proposal.preview["ticket"] == ref
    assert proposal.preview["status"] == TicketStatus.RUNNING
    async with maker() as session:
        card = await session.get_one(ChatItem, proposal.item_id)
    assert (card.kind, card.turn_id) == (ChatItemKind.PROPOSAL, chat_turn.turn_id)
    assert card.payload["proposal_id"] == proposal.id
    assert card.payload["tool"] == "factorial_ticket_retry"
    assert card.payload["title"] == "Retry a failed run now"
    assert card.payload["status"] == "pending"
    assert card.payload["preview"] == proposal.preview
    # A twin never writes to the factory: the retry is the person's to approve.
    assert "retry_requested" not in {row.type for row in await event_rows(world)}


async def test_twin_for_an_impossible_action_makes_no_card(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        exited = await seed_ticket(session, await seed_project(session), status=TicketStatus.EXITED)
        await session.commit()
    tools = chat_toolbox(maker, chat_turn)

    tool_refused(
        await tools.call("factorial_ticket_retry", {"ticket": f"id:{exited.id}"}),
        "factorial_ticket_reenter",
    )

    assert await _proposals(maker, chat_turn.conversation_id) == []
    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_config_commit_twin_on_invalid_yaml_returns_errors_and_no_card(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    source = RepoConfigSource(FakeContents({}))
    tools = chat_toolbox(maker, chat_turn, config_source=source)

    tool_refused(
        await tools.call(
            "factorial_config_commit", {"project": project.name, "content": "kind: ["}
        ),
        "does not parse",
    )

    assert await _proposals(maker, chat_turn.conversation_id) == []
    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_config_commit_twin_previews_the_files_and_the_diff(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    before = lane()
    after = before.replace("name: p\n", "name: p\n# a note for the reviewer\n", 1)
    agents = agent_files("plan-agent", "claude") | agent_files("build-agent", "claude")
    source = RepoConfigSource(FakeContents({PROCESS_PATH: before} | agents))
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    tools = chat_toolbox(maker, chat_turn, config_source=source)

    added = [{"path": EXAMPLE, "body": None}]

    out = tool_data(
        await tools.call(
            "factorial_config_commit", {"project": project.name, "content": after, "files": added}
        )
    )

    (proposal,) = await _proposals(maker, chat_turn.conversation_id)
    assert out["proposal_id"] == proposal.id
    # Stored as JSON: the tool received `files` as its models. Approve checks the base.
    assert proposal.args == {
        "project": project.name,
        "content": after,
        "files": added,
        "edits": None,
        "message": None,
        "expected_head": BASE_SHA,
    }
    preview = proposal.preview
    assert preview["base_sha"] == BASE_SHA
    assert preview["would_write"] == [
        {"path": EXAMPLE, "origin": "file"},
        {"path": PROCESS_PATH, "origin": "process"},
    ]
    assert f"--- a/{PROCESS_PATH}" in preview["diff"]
    assert "+# a note for the reviewer\n" in preview["diff"]
    assert not any(
        line.startswith(("-", "+")) and not line.startswith(("---", "+++"))
        for line in preview["diff"].splitlines()
        if "a note for the reviewer" not in line
    )


async def test_the_toolbox_offers_the_twins_as_neither_read_only_nor_destructive(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    offered = {d.name: d for d in chat_toolbox(maker, chat_turn).definitions()}
    reads = {spec.name for spec in ALL_TOOLS if spec.scope == READ_SCOPE}
    assert set(offered) == reads | _CHAT_TOOLS | _WRITES
    for name in _WRITES:
        assert (offered[name].read_only, offered[name].destructive) == (False, False), name


#: What a twin stores beside its tool's input, for Approve to check.
_PINNED = {"factorial_config_commit": {"expected_head"}}


@pytest.mark.parametrize("twin", TWINS, ids=lambda twin: twin.name)
async def test_each_executor_takes_exactly_what_its_twin_stores(twin: ToolSpec) -> None:
    """Approve calls `EXECUTORS[tool](tc, actor, **stored)`: a parameter one side adds or
    renames would fail every stored proposal of that tool. A pinned one is a check the MCP
    write tool never makes, so it defaults to None."""
    pinned = _PINNED.get(twin.name, set())
    stored = set(inspect.signature(twin.fn).parameters) - {"tc"} | pinned
    run = inspect.signature(EXECUTORS[twin.name]).parameters
    assert list(run)[:2] == ["tc", "actor"]
    assert {name for name in list(run)[2:]} == stored
    assert all(run[name].kind is inspect.Parameter.KEYWORD_ONLY for name in stored)
    assert all(run[name].default is None for name in pinned)


async def test_the_resume_twin_refuses_a_ceiling_park(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    """Decision 56: only the dashboard's Resume lifts a session ceiling, so no card offers it."""
    world, adapter = await interactive_world(
        maker,
        bot_login=BOT_LOGIN,
        sessions=SessionPolicy(terminal_enabled=True, session_ceiling=timedelta(minutes=30)),
    )
    await run_tick(world)
    world.clock.advance(timedelta(minutes=30))
    await run_tick(world)
    assert adapter.interrupt_reasons == [ParkReason.CEILING]
    tools = chat_toolbox(maker, chat_turn)

    tool_refused(
        await tools.call(
            "factorial_ticket_resume", {"ticket": f"id:{world.ticket_id}", "answer": "go on"}
        ),
        "factorial_ticket_resume is not possible now",
    )

    assert await _proposals(maker, chat_turn.conversation_id) == []
    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_the_restart_twin_previews_the_footprint_and_pins_the_lane_head(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    """The card shows what Approve would cancel, close and delete, and the proposal keeps the
    lane head it was drawn against: a sync before Approve then refuses instead of restarting
    onto a snapshot the person never saw."""
    async with maker() as session:
        ticket, head = await behind(session)
        await session.commit()
    reader = observed_repo(branches=[BranchState(name=SPEC, sha="c" * 40)], pulls=[pull(5)])
    deps = mcp_deps_for(mcp_settings(), maker, repo_reader=reader)
    tools = chat_toolbox(maker, chat_turn, deps=deps)

    out = tool_data(await tools.call("factorial_ticket_restart", {"ticket": f"id:{ticket.id}"}))

    (proposal,) = await _proposals(maker, chat_turn.conversation_id)
    assert out["proposal_id"] == proposal.id
    assert proposal.args == {
        "ticket": f"id:{ticket.id}",
        "project": None,
        "process_version_id": head.id,
        "allow_current_pin": False,
        "allow_merged": False,
    }
    preview = proposal.preview
    assert preview["process"] == "From aaaaaaa to bbbbbbb"
    assert (preview["live_run"], preview["parked_run"]) == (None, None)
    assert preview["close_prs"] == [
        {"number": 5, "url": "https://github.test/acme/pull/5", "head": SPEC, "base": "main"}
    ]
    assert preview["delete_branches"] == [SPEC]
    async with maker() as session:
        recorded = await session.scalars(select(Event.type).where(Event.ticket_id == ticket.id))
        assert recorded.all() == []


async def test_the_restart_twin_asks_before_repeating_merged_work(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    """A pull request of the ticket's already merged into `main` is the person's call: without
    `allow_merged` the twin refuses and says so; with it, the card names the merged work."""
    async with maker() as session:
        ticket, _ = await behind(session)
        await session.commit()
    merged = pull(5, state="closed", merged=True)
    reader = observed_repo(branches=[BranchState(name=SPEC, sha="c" * 40)], pulls=[merged])
    deps = mcp_deps_for(mcp_settings(), maker, repo_reader=reader)
    tools = chat_toolbox(maker, chat_turn, deps=deps)

    tool_refused(
        await tools.call("factorial_ticket_restart", {"ticket": f"id:{ticket.id}"}),
        "allow_merged",
    )
    out = tool_data(
        await tools.call(
            "factorial_ticket_restart", {"ticket": f"id:{ticket.id}", "allow_merged": True}
        )
    )

    (proposal,) = await _proposals(maker, chat_turn.conversation_id)
    assert out["proposal_id"] == proposal.id
    assert proposal.args["allow_merged"] is True
    assert proposal.preview["merged_outside"] == [
        {"number": 5, "url": "https://github.test/acme/pull/5", "head": SPEC, "base": "main"}
    ]


async def test_the_restart_twin_refuses_what_approve_would_refuse(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        on_the_head = await on_lane_p(session)
        closed, _ = await behind(session)
        await session.commit()
    deps = mcp_deps_for(mcp_settings(), maker, repo_reader=observed_repo(issue_state="closed"))
    tools = chat_toolbox(maker, chat_turn, deps=deps)

    tool_refused(
        await tools.call("factorial_ticket_restart", {"ticket": f"id:{on_the_head.id}"}),
        "already runs aaaaaaa",
    )
    tool_refused(
        await tools.call("factorial_ticket_restart", {"ticket": f"id:{closed.id}"}),
        "Issue #12 is closed",
    )

    assert await _proposals(maker, chat_turn.conversation_id) == []
    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_the_restart_twin_needs_a_way_to_read_github(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        ticket, _ = await behind(session)
        await session.commit()

    tool_refused(
        await chat_toolbox(maker, chat_turn).call(
            "factorial_ticket_restart", {"ticket": f"id:{ticket.id}"}
        ),
        "no GitHub connection",
    )

    assert await _proposals(maker, chat_turn.conversation_id) == []
