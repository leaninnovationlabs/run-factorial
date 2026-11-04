"""The assistant's tools as plain calls in its process (design.md §7.18): the set, the schemas
`/mcp/` serves for the same functions, strict arguments, refusals rendered as `/mcp/` renders
them, everything else masked, and a chat tool's turn checked on every call."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.assistant import toolbox as toolbox_module
from orchestrator.assistant.github_token import NO_TOKEN, ReadTokenCache, tool_deps
from orchestrator.assistant.toolbox import CHAT_TOOLSET, TurnToolbox, registry
from orchestrator.db.models import ChatItem, ChatTurn, ChatTurnStatus
from orchestrator.mcp_server.app import build_server
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.mcp_server.chat.tools import CHAT_TOOLS
from orchestrator.mcp_server.chat.twins import TWINS
from orchestrator.mcp_server.tokens import READ_SCOPE, WRITE_SCOPE
from orchestrator.mcp_server.tools import ALL_TOOLS
from tests.api_harness import (
    SeededChatTurn,
    chat_toolbox,
    mcp_deps_for,
    mcp_settings,
    seed_project,
    tool_data,
    tool_refused,
)
from tests.assistant_fakes import sessions_of

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_READS = {spec.name for spec in ALL_TOOLS if spec.scope == READ_SCOPE}
_WRITES = {spec.name for spec in ALL_TOOLS if spec.scope == WRITE_SCOPE}
_CHAT = {spec.name for spec in CHAT_TOOLS}


async def _items(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> int:
    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ChatItem)
            .where(ChatItem.conversation_id == conversation_id)
        )
    return count or 0


async def test_the_toolset_is_the_reads_the_chat_tools_and_the_twins_and_no_write() -> None:
    assert set(registry()) == _READS | _CHAT | _WRITES
    originals = {spec.fn for spec in ALL_TOOLS if spec.scope == WRITE_SCOPE}
    assert not any(spec.fn in originals for spec in CHAT_TOOLSET)
    assert not any(spec.scope == WRITE_SCOPE for spec in CHAT_TOOLSET)
    assert {twin.name for twin in TWINS} == _WRITES


async def test_each_schema_is_the_one_mcp_serves_for_the_same_function() -> None:
    operator = build_server(mcp_deps_for(mcp_settings(), None))
    served = {tool.name: tool for tool in await operator.local_provider.list_tools()}
    entries = registry()
    for name in _READS:
        assert entries[name].definition.input_schema == served[name].parameters, name
        assert entries[name].definition.description == (served[name].description or ""), name
        assert entries[name].definition.read_only, name
    for name in _WRITES:
        # A twin takes its original's input exactly (test_chat_twins.py holds the twins' side).
        assert entries[name].definition.input_schema == served[name].parameters, name
        assert not entries[name].definition.read_only and not entries[name].definition.destructive
    for name in _CHAT:
        assert not entries[name].definition.read_only and not entries[name].definition.destructive


async def test_an_unknown_tool_or_bad_arguments_never_reach_the_database() -> None:
    tools = TurnToolbox(mcp_deps_for(mcp_settings(), None), ChatScope(1, 1, "a", 1))
    tool_refused(await tools.call("factorial_secrets_get", {}), "no tool named")
    tool_refused(await tools.call("factorial_tickets_list", {"limit": "ten"}), "limit")
    tool_refused(await tools.call("factorial_tickets_list", {"limit": "10"}), "limit")
    tool_refused(await tools.call("factorial_tickets_list", {"nonsense": 1}), "nonsense")
    for value in (float("inf"), float("nan"), float("-inf")):
        chart = {
            "kind": "line",
            "title": "t",
            "unit": "USD",
            "x": ["a"],
            "series": [{"name": "s", "values": [value]}],
            "source": "factorial_metrics_get",
        }
        tool_refused(await tools.call("factorial_chart", chart), "finite")


async def test_a_refusal_reads_as_mcp_renders_it(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)
    text = tool_refused(
        await tools.call(
            "factorial_reference_read", {"name": "process-yaml", "section": "no such part"}
        ),
        "has no section",
    )
    assert "\nValid: " in text


async def test_any_other_failure_is_masked_and_logged(
    maker: async_sessionmaker[AsyncSession],
    chat_turn: SeededChatTurn,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = registry()["factorial_vocabulary_get"]

    async def boom(**_: Any) -> Any:
        raise RuntimeError("a detail the model must not read")

    broken = dataclasses.replace(entry, spec=dataclasses.replace(entry.spec, fn=boom))
    monkeypatch.setattr(toolbox_module, "registry", lambda: {"factorial_vocabulary_get": broken})
    with caplog.at_level(logging.ERROR, logger=toolbox_module.__name__):
        outcome = await chat_toolbox(maker, chat_turn).call("factorial_vocabulary_get", {})
    text = tool_refused(outcome, "factorial_vocabulary_get")
    assert "detail" not in text
    assert "a detail the model must not read" in caplog.text


async def test_a_chat_tool_draws_in_its_running_turn_only(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    tools = chat_toolbox(maker, chat_turn)
    shown = tool_data(await tools.call("factorial_ui_show", {"path": "/overview"}))
    async with maker() as session:
        card = await session.get_one(ChatItem, shown["card_item_id"])
        assert (card.conversation_id, card.turn_id) == (
            chat_turn.conversation_id,
            chat_turn.turn_id,
        )
        turn = await session.get_one(ChatTurn, chat_turn.turn_id)
        turn.status = ChatTurnStatus.DONE
        await session.commit()

    tool_refused(await tools.call("factorial_ui_show", {"path": "/overview"}), "not running")
    assert await _items(maker, chat_turn.conversation_id) == 1


async def test_a_chat_tool_refuses_a_turn_named_for_someone_else(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    other = dataclasses.replace(chat_turn, owner="outsider@example.com")
    tool_refused(
        await chat_toolbox(maker, other).call("factorial_ui_show", {"path": "/overview"}),
        "another person",
    )
    assert await _items(maker, chat_turn.conversation_id) == 0


async def test_a_github_tool_without_a_token_says_so(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    """The API was down when the turn started: the tool must say so, not ask for App settings."""

    async def never() -> Any:
        raise AssertionError("no fetch in this test")

    async with maker() as session:
        project = await seed_project(session)
        await session.commit()
    settings = mcp_settings()
    deps = tool_deps(lambda: settings, sessions_of(maker), ReadTokenCache(never))
    outcome = await chat_toolbox(maker, chat_turn, deps=deps).call(
        "factorial_config_read", {"project": project.name}
    )
    assert tool_refused(outcome) == NO_TOKEN
