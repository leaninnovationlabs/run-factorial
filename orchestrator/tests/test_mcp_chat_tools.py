"""The assistant's own tools: each draws one card in the turn's conversation, or reads a
reference, and none of them writes to the factory."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import (
    ChatItem,
    ChatItemKind,
    ChatTurn,
    ChatTurnStatus,
)
from orchestrator.events.channels import CHAT_ITEMS_CHANNEL
from orchestrator.factorial import RepoConfigSource
from orchestrator.mcp_server import teaching as operator_teaching
from orchestrator.mcp_server.chat import teaching
from tests.api_harness import SeededChatTurn, chat_toolbox, tool_data, tool_refused
from tests.test_config_commits import BASE_SHA, FakeContents, lane, project_in
from tests.wake_probe import listening

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _item(maker: async_sessionmaker[AsyncSession], item_id: int) -> ChatItem:
    async with maker() as session:
        return await session.get_one(ChatItem, item_id)


async def _items(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> int:
    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ChatItem)
            .where(ChatItem.conversation_id == conversation_id)
        )
    return count or 0


def _chart(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "line",
        "title": "Cost per week",
        "unit": "USD",
        "x": ["W1", "W2", "W3"],
        "series": [{"name": "acme/api", "values": [1.5, 2, 0.25]}],
        "source": "factorial_metrics_get",
    } | overrides


async def test_chat_instructions_fit_and_keep_the_operator_rules() -> None:
    assert len(teaching.INSTRUCTIONS) < 2000

    def rules(text: str) -> list[str]:
        return [" ".join(rule.split()) for rule in text.split("\n") if rule[:1].isdigit()]

    operator = [rule.split(". ", 1)[1] for rule in rules(operator_teaching.INSTRUCTIONS)]
    chat = [rule.split(". ", 1)[1] for rule in rules(teaching.INSTRUCTIONS)]
    for number in (1, 2, 4, 8):
        assert operator[number - 1] in chat


async def test_ui_show_writes_a_card_and_refuses_an_unknown_route(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn, database_url: str
) -> None:
    tools = chat_toolbox(maker, chat_turn)

    async with listening(database_url, CHAT_ITEMS_CHANNEL) as heard:
        shown = tool_data(
            await tools.call(
                "factorial_ui_show",
                {"path": "/tickets/212", "anchor": "ticket.actions", "note": "Retry is here"},
            )
        )
        paged = tool_data(
            await tools.call(
                "factorial_ui_show", {"path": "/projects/3/configuration/ticket-source"}
            )
        )
        tool_refused(
            await tools.call("factorial_ui_show", {"path": "/admin/secrets"}), "/tickets/:ticketId"
        )
        tool_refused(
            await tools.call("factorial_ui_show", {"path": "/projects/3?tab=configuration"}),
            "/projects/:projectId",
        )
        # The dashboard reads :projectId and :ticketId as numbers: a name there opens a page
        # that says it is not found.
        for named in ("/projects/acme-reporting/tickets", "/tickets/acme-7"):
            tool_refused(
                await tools.call("factorial_ui_show", {"path": named}), "/projects/:projectId"
            )
        assert await asyncio.wait_for(heard.get(), 5) == str(chat_turn.conversation_id)

    item = await _item(maker, shown["card_item_id"])
    assert (item.kind, item.conversation_id, item.turn_id) == (
        ChatItemKind.SHOW_ME,
        chat_turn.conversation_id,
        chat_turn.turn_id,
    )
    assert item.payload == {
        "path": "/tickets/212",
        "anchor": "ticket.actions",
        "note": "Retry is here",
    }
    assert (await _item(maker, paged["card_item_id"])).payload["path"] == (
        "/projects/3/configuration/ticket-source"
    )
    assert await _items(maker, chat_turn.conversation_id) == 2


async def test_ui_show_refuses_an_unknown_anchor(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)

    tool_refused(
        await tools.call(
            "factorial_ui_show", {"path": "/tickets/212", "anchor": "ticket.secret_key"}
        ),
        r"ticket\.actions",
    )

    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_chart_refuses_more_than_six_series_and_non_finite_numbers(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)
    seven = [{"name": f"s{n}", "values": [1, 2, 3]} for n in range(7)]
    six = seven[:6]

    drawn = tool_data(await tools.call("factorial_chart", _chart(kind="grouped_bar", series=six)))
    tool_refused(await tools.call("factorial_chart", _chart(series=seven)))
    tool_refused(
        await tools.call(
            "factorial_chart", _chart(series=[{"name": "acme/api", "values": [1, 2]}])
        ),
        "acme/api",
    )
    for value in (float("inf"), float("nan"), float("-inf")):
        chart = _chart(series=[{"name": "acme/api", "values": [1.5, 2, value]}])
        tool_refused(await tools.call("factorial_chart", chart), "finite")

    item = await _item(maker, drawn["card_item_id"])
    assert item.kind == ChatItemKind.CHART
    assert item.payload == _chart(kind="grouped_bar", series=six)
    assert await _items(maker, chat_turn.conversation_id) == 1


async def test_chart_source_must_be_a_read_tool(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)

    for source in ("factorial_ticket_retry", "factorial_chart", "a guess"):
        tool_refused(
            await tools.call("factorial_chart", _chart(source=source)), "factorial_costs_get"
        )
    assert (await tools.call("factorial_chart", _chart(source="factorial_runs_list"))).ok

    assert await _items(maker, chat_turn.conversation_id) == 1


async def test_reference_read_returns_a_process_yaml_section(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)

    section = tool_data(
        await tools.call(
            "factorial_reference_read", {"name": "process-yaml", "section": "templates"}
        )
    )
    whole = tool_data(await tools.call("factorial_reference_read", {"name": "process-yaml"}))
    skill = tool_data(await tools.call("factorial_reference_read", {"name": "operator-skill"}))
    tool_refused(
        await tools.call(
            "factorial_reference_read", {"name": "process-yaml", "section": "no such part"}
        ),
        "Templates",
    )

    assert section["text"].startswith("## Templates")
    assert "### `{slug}` is the one with rules" in section["text"]
    assert "## Postconditions" not in section["text"]
    assert not section["truncated"]
    assert whole["truncated"]
    assert whole["text"].startswith("# Writing a process file")
    assert len(whole["text"]) < 40_200
    assert whole["text"].endswith("pass section to read one part]")
    assert any(name.startswith("Templates") for name in whole["sections"])
    assert "factorial_tickets_list" in skill["text"]
    assert not skill["truncated"]
    # A read draws nothing.
    assert await _items(maker, chat_turn.conversation_id) == 0


@pytest.fixture
def stub_config_source() -> RepoConfigSource:
    """`factorial` at BASE_SHA, with no agent folders: a lane that names an agent gets a stub
    under agents/, which only the dashboard may commit."""
    return RepoConfigSource(FakeContents({}))


async def test_editor_open_stores_the_draft_for_the_owner(
    maker: async_sessionmaker[AsyncSession],
    chat_turn: SeededChatTurn,
    stub_config_source: RepoConfigSource,
) -> None:
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    tools = chat_toolbox(maker, chat_turn, config_source=stub_config_source)

    opened = tool_data(
        await tools.call("factorial_editor_open", {"project": project.name, "content": lane()})
    )
    tool_refused(
        await tools.call("factorial_editor_open", {"project": project.name, "content": "kind: ["}),
        "parse",
    )

    item = await _item(maker, opened["card_item_id"])
    assert (item.kind, item.conversation_id, item.turn_id) == (
        ChatItemKind.EDITOR_DRAFT,
        chat_turn.conversation_id,
        chat_turn.turn_id,
    )
    payload = item.payload
    assert payload["project_id"] == project.id
    assert payload["process"] == "p"
    assert payload["content"] == lane()
    assert payload["base_sha"] == BASE_SHA
    assert (payload["files"], payload["edits"]) == ([], [])
    origins = {write["path"]: write["origin"] for write in payload["would_write"]}
    assert origins[".factorial/processes/process-p.yaml"] == "process"
    # Dashboard-only, so a chat commit would refuse it: handing it to a person is the point.
    assert any(
        path.startswith(".factorial/agents/") and origin == "stub"
        for path, origin in origins.items()
    )
    assert opened["blockers"] == []
    assert await _items(maker, chat_turn.conversation_id) == 1


async def test_chat_tools_answer_only_while_the_turn_runs(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        turn = await session.get_one(ChatTurn, chat_turn.turn_id)
        turn.status = ChatTurnStatus.DONE
        await session.commit()
    tools = chat_toolbox(maker, chat_turn)

    tool_refused(await tools.call("factorial_ui_show", {"path": "/overview"}), "not running")

    assert await _items(maker, chat_turn.conversation_id) == 0
