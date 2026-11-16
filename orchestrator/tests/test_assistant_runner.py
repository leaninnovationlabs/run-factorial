"""One assistant turn, run against a scripted backend: the items it writes, the usage it records,
the settings and token it runs with, and every way it ends."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.credential import CredentialFetch, SettingsChanged
from orchestrator.assistant.github_token import OrganizationTokens
from orchestrator.assistant.items import SUMMARY_CAP, append_item
from orchestrator.assistant.prompt import system_prompt
from orchestrator.assistant.proposals import decline, expire_stale, record_proposal
from orchestrator.assistant.runner import RunnerDeps, run_turn
from orchestrator.assistant.toolbox import registry
from orchestrator.assistant.turns import (
    RESTART_NOTICE,
    claim_turn,
    enqueue_turn,
    recover_lost_turns,
)
from orchestrator.db.models import (
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    McpToken,
)
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.settings import AssistantProcessSettings
from runtime.assistant.adapters.bedrock_responses import BedrockResponsesBackend
from runtime.assistant.contract import (
    ChatBackend,
    ChatEvent,
    HistoryItem,
    TextDelta,
    TextDone,
    ToolCall,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)
from tests.api_harness import (
    CHAT_OWNER,
    eventually,
    mcp_deps_for,
    mcp_settings,
    seed_project,
)
from tests.assistant_fakes import (
    FakeBackend,
    FakeClock,
    FakeTokenFetch,
    Hook,
    StoredCredential,
    enable_assistant,
    renewed,
    seed_conversation,
    sessions_of,
    wipe_chat,
)
from tests.log_probe import probing

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

OWNER = "host:1"
Deps = Callable[..., RunnerDeps]
#: Streams recorded from Bedrock: a reply that calls `factorial_projects_list`, then its answer.
BEDROCK_STREAMS = Path(__file__).parents[2] / "runtime/tests/fixtures/bedrock-responses-2026-11-10"


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession], master_key: None) -> AsyncIterator[None]:
    await wipe_chat(maker)
    yield
    await wipe_chat(maker)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def deps_with(maker: async_sessionmaker[AsyncSession]) -> Deps:
    def build(
        backend: FakeBackend,
        *,
        clock: Callable[[], datetime] | None = None,
        fetch: FakeTokenFetch | None = None,
        credential: CredentialFetch | None = None,
        **process: Any,
    ) -> RunnerDeps:
        settings = mcp_settings().model_copy(
            update={"assistant": AssistantProcessSettings(**process)}
        )
        return RunnerDeps(
            sessions=sessions_of(maker),
            settings=lambda: settings,
            backend_for=lambda _kind: backend,
            clock=clock or (lambda: datetime.now(UTC)),
            tools=lambda _cache: mcp_deps_for(settings, maker),
            github_tokens=OrganizationTokens(lambda _org: fetch or FakeTokenFetch()),
            credential=credential or StoredCredential(maker),
        )

    return build


async def sent(
    maker: async_sessionmaker[AsyncSession],
    text: str = "hello",
    *,
    page: str | None = None,
    conversation_id: int | None = None,
) -> ChatTurn:
    """A message sent and its turn claimed by `OWNER`, as the loop would hand it over."""
    async with maker() as session:
        if conversation_id is None:
            conversation_id = (await seed_conversation(session, owner=CHAT_OWNER)).id
        turn = await enqueue_turn(session, conversation_id, trigger=ChatTurnTrigger.USER)
        assert turn is not None
        await append_item(
            session,
            conversation_id=conversation_id,
            turn_id=turn.id,
            kind=ChatItemKind.USER_TEXT,
            payload={"text": text, "page": page},
        )
        await session.commit()
    return await _claim(maker)


async def _claim(maker: async_sessionmaker[AsyncSession]) -> ChatTurn:
    async with maker() as session:
        claimed = await claim_turn(
            session, owner=OWNER, lease=timedelta(seconds=60), now=datetime.now(UTC)
        )
        await session.commit()
    assert claimed is not None
    return claimed


async def items_of(maker: async_sessionmaker[AsyncSession], conversation_id: int) -> list[ChatItem]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ChatItem)
                    .where(ChatItem.conversation_id == conversation_id)
                    .order_by(ChatItem.id)
                )
            ).all()
        )


async def kinds(
    maker: async_sessionmaker[AsyncSession], conversation_id: int, kind: ChatItemKind
) -> list[ChatItem]:
    return [item for item in await items_of(maker, conversation_id) if item.kind == kind]


async def row(maker: async_sessionmaker[AsyncSession], turn_id: int) -> ChatTurn:
    async with maker() as session:
        return await session.get_one(ChatTurn, turn_id)


async def request_cancel(maker: async_sessionmaker[AsyncSession], turn_id: int) -> None:
    async with maker() as session:
        await session.execute(
            update(ChatTurn).where(ChatTurn.id == turn_id).values(cancel_requested=True)
        )
        await session.commit()


def usage(cost: str = "0.0123", session_id: str | None = "sess-1") -> Usage:
    return Usage(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=20,
        cost_usd=Decimal(cost),
        cost_reported=False,
        session_id=session_id,
    )


async def test_events_become_items_and_usage_lands_on_the_turn(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    script: list[ChatEvent | Hook] = [
        TextDelta(block="m1:0", text="Two "),
        TextDelta(block="m1:0", text="tickets "),
        TextDelta(block="m1:0", text="wait."),
        TextDone(block="m1:0", text="Two tickets wait."),
        ToolCall(call_id="c1", name="factorial_tickets_list", input={"state": "blocked"}),
        ToolResult(call_id="c1", ok=True, summary="2 tickets", structured={"tickets": []}),
        usage(),
        TurnDone(stop=TurnStop.END),
    ]

    await run_turn(turn.id, deps_with(FakeBackend(script)), owner=OWNER)

    items = await items_of(maker, turn.conversation_id)
    assert [item.kind for item in items] == ["user_text", "assistant_text", "tool"]
    text, tool = items[1], items[2]
    assert (text.payload, text.final, text.turn_id) == (
        {"text": "Two tickets wait."},
        True,
        turn.id,
    )
    assert tool.payload == {
        "call_id": "c1",
        "name": "factorial_tickets_list",
        "input": {"state": "blocked"},
        "status": "done",
        "ok": True,
        "summary": "2 tickets",
    }
    done = await row(maker, turn.id)
    assert (done.status, done.stop_reason, done.failure_reason) == ("done", "end", None)
    assert (done.cost_usd, done.cost_source) == (Decimal("0.0123"), "estimated")
    assert (done.input_tokens, done.cached_input_tokens, done.output_tokens) == (100, 40, 20)
    assert done.model == "claude-opus-5-5"
    assert done.ended_at is not None and done.lease_expires_at is None
    async with maker() as session:
        conversation = await session.get_one(ChatConversation, turn.conversation_id)
    assert conversation.backend_session_id == "sess-1"


async def test_text_is_flushed_at_most_every_200ms(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps, fake_clock: FakeClock
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    seen: list[tuple[str, bool] | None] = []

    async def look() -> None:
        texts = await kinds(maker, turn.conversation_id, ChatItemKind.ASSISTANT_TEXT)
        seen.append((texts[0].payload["text"], texts[0].final) if texts else None)

    def wait(ms: int) -> Hook:
        async def hook() -> None:
            fake_clock.advance(milliseconds=ms)

        return hook

    script: list[ChatEvent | Hook] = [
        look,
        TextDelta(block="b", text="a"),
        look,
        wait(50),
        TextDelta(block="b", text="b"),
        look,
        wait(149),
        TextDelta(block="b", text="c"),
        look,
        wait(1),
        TextDelta(block="b", text="d"),
        look,
        wait(10),
        TextDelta(block="b", text="e"),
        look,
        TextDone(block="b", text="abcde"),
        look,
        TurnDone(stop=TurnStop.END),
    ]

    await run_turn(turn.id, deps_with(FakeBackend(script), clock=fake_clock), owner=OWNER)

    assert seen == [
        None,
        ("a", False),
        ("a", False),
        ("a", False),
        ("abcd", False),
        ("abcd", False),
        ("abcde", True),
    ]


async def test_tool_item_summary_is_capped(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    yaml = "steps:\n" + "  - name: step\n" * 3000
    assert len(yaml) > 40_000
    script: list[ChatEvent | Hook] = [
        ToolCall(call_id="c1", name="factorial_config_validate", input={"content": yaml}),
        ToolResult(call_id="c1", ok=True, summary=yaml, structured={"content": yaml}),
        TurnDone(stop=TurnStop.END),
    ]

    await run_turn(turn.id, deps_with(FakeBackend(script)), owner=OWNER)

    [tool] = await kinds(maker, turn.conversation_id, ChatItemKind.TOOL)
    assert tool.payload["summary"] == yaml[:SUMMARY_CAP]
    assert "input" not in tool.payload
    assert tool.payload["input_preview"] == json.dumps({"content": yaml})[:SUMMARY_CAP]
    assert len(json.dumps(tool.payload)) < 3 * SUMMARY_CAP


async def test_next_turn_reads_current_settings(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker, model="claude-opus-5-5")
    backend = FakeBackend(
        [
            TextDone(block="b", text="a reply"),
            usage(session_id="sess-1"),
            TurnDone(stop=TurnStop.END),
        ]
    )
    deps = deps_with(backend)
    first = await sent(maker, "first")
    await run_turn(first.id, deps, owner=OWNER)

    await enable_assistant(
        maker,
        credential="sk-ant-second",
        model="claude-sonnet-4-5",
        effort="high",
        max_steps=7,
        turn_budget_usd="2.50",
    )
    second = await sent(maker, "second", conversation_id=first.conversation_id)
    await run_turn(second.id, deps, owner=OWNER)

    before, after = backend.specs
    assert (before.model, after.model) == ("claude-opus-5-5", "claude-sonnet-4-5")
    assert (before.effort, after.effort) == ("medium", "high")
    assert (after.limits.max_steps, after.limits.max_budget_usd) == (7, Decimal("2.50"))
    assert before.credential.get_secret_value() == "sk-ant-test"
    assert after.credential.get_secret_value() == "sk-ant-second"
    assert (before.message, after.message) == ("first", "second")
    assert before.history == ()
    assert after.history == (
        HistoryItem(role="user", text="first"),
        HistoryItem(role="assistant", text="a reply"),
    )
    assert (before.resume_session, after.resume_session) == (None, "sess-1")
    assert (await row(maker, second.id)).model == "claude-sonnet-4-5"


class _Sessionless(FakeBackend):
    supports_resume: ClassVar[bool] = False


async def test_a_backend_that_keeps_no_session_is_handed_none_to_resume(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """A conversation's row can name a session its backend no longer keeps: Codex turns reported
    a thread whose rollout was deleted with each turn. Handing that over asks the backend to
    continue what does not exist."""
    await enable_assistant(maker)
    backend = _Sessionless([TurnDone(stop=TurnStop.END)])
    turn = await sent(maker)
    async with maker() as session:
        await session.execute(
            update(ChatConversation)
            .where(ChatConversation.id == turn.conversation_id)
            .values(backend_kind=backend.kind, backend_session_id="thread-1")
        )
        await session.commit()

    await run_turn(turn.id, deps_with(backend), owner=OWNER)

    [spec] = backend.specs
    assert spec.resume_session is None


@pytest.mark.parametrize("setup", ["never_set_up", "disabled", "no_credential"])
async def test_disabled_or_unconfigured_assistant_fails_the_turn_with_config(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps, setup: str
) -> None:
    if setup == "disabled":
        await enable_assistant(maker, enabled=False)
    elif setup == "no_credential":
        await enable_assistant(maker)
        async with maker() as session:
            ours = await default_organization_id(session)
            await svc.clear_assistant_credential(session, ours, "claude_sdk", "api_key")
            await session.commit()
    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend), owner=OWNER)

    failed = await row(maker, turn.id)
    assert (failed.status, failed.failure_reason) == ("failed", "config")
    assert backend.specs == []
    [notice] = await kinds(maker, turn.conversation_id, ChatItemKind.NOTICE)
    assert notice.payload == {
        "text": "Set up the assistant in Assistant settings.",
        "reason": "config",
    }


async def test_a_turn_runs_on_the_credential_the_api_hands_it(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """Nothing is stored: this process holds no master key, so the API opens the credential."""
    async with maker() as session:
        await svc.save_assistant_settings(
            session, await default_organization_id(session), AssistantSettings(enabled=True), by="t"
        )
        await session.commit()
    asked: list[tuple[str, str, str]] = []

    async def from_api(org: str, assistant: AssistantSettings) -> SecretStr | None:
        asked.append((org, assistant.backend, assistant.auth_mode))
        return SecretStr("sk-ant-from-api")

    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend, credential=from_api), owner=OWNER)

    [spec] = backend.specs
    assert spec.credential.get_secret_value() == "sk-ant-from-api"
    assert asked == [("default", "claude_sdk", "api_key")]


async def test_a_turn_whose_settings_changed_says_so_and_not_set_up(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)

    async def switched(org: str, assistant: AssistantSettings) -> SecretStr | None:
        raise SettingsChanged

    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend, credential=switched), owner=OWNER)

    failed = await row(maker, turn.id)
    assert (failed.status, failed.failure_reason) == ("failed", "config")
    assert backend.specs == []
    [notice] = await kinds(maker, turn.conversation_id, ChatItemKind.NOTICE)
    assert notice.payload == {
        "text": "The assistant's settings changed during the turn. Send it again.",
        "reason": "config",
    }


async def test_a_turn_the_api_cannot_answer_fails_without_asking_for_set_up(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)

    async def down(org: str, assistant: AssistantSettings) -> SecretStr | None:
        raise httpx.ConnectError("the API is down")

    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend, credential=down), owner=OWNER)

    failed = await row(maker, turn.id)
    assert (failed.status, failed.failure_reason) == ("failed", "infrastructure")
    assert backend.specs == []
    assert all(
        "Set up the assistant" not in str(notice.payload)
        for notice in await kinds(maker, turn.conversation_id, ChatItemKind.NOTICE)
    )


@pytest.mark.parametrize("kind", ["codex_cli", "bedrock_responses"])
async def test_a_bedrock_turn_mints_its_own_bearer_and_asks_the_api_for_nothing(
    maker: async_sessionmaker[AsyncSession],
    deps_with: Deps,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(
        "orchestrator.assistant.runner.mint_bearer", lambda **_: "bedrock-api-key-test"
    )
    async with maker() as session:
        await svc.save_assistant_settings(
            session,
            await default_organization_id(session),
            AssistantSettings(
                enabled=True, backend=kind, auth_mode="bedrock", model="us.xai.grok-4.6"
            ),
            by="t",
        )
        await session.commit()
    stored = StoredCredential(maker)
    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend, credential=stored), owner=OWNER)

    [spec] = backend.specs
    assert spec.credential.get_secret_value() == "bedrock-api-key-test"
    assert (spec.auth_mode, spec.bedrock_region) == ("bedrock", "us-east-1")
    assert stored.asked == []


async def test_a_bedrock_turn_reads_the_factory_through_the_turn_s_own_tools(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path, on streams recorded from Bedrock: the backend that calls it, the model's
    call answered from the database by the turn's toolbox, and the items the Drawer shows."""
    monkeypatch.setattr(
        "orchestrator.assistant.runner.mint_bearer", lambda **_: "bedrock-api-key-test"
    )
    async with maker() as session:
        await svc.save_assistant_settings(
            session,
            await default_organization_id(session),
            AssistantSettings(
                enabled=True,
                backend="bedrock_responses",
                auth_mode="bedrock",
                model="us.xai.grok-4.6",
                bedrock_region="us-east-1",
            ),
            by="t",
        )
        await session.commit()
    replies = [
        (BEDROCK_STREAMS / "tool-call.sse").read_bytes(),
        (BEDROCK_STREAMS / "tool-answer.sse").read_bytes(),
    ]
    requests: list[httpx.Request] = []

    async def bedrock(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=replies.pop(0)
        )

    backend = BedrockResponsesBackend(transport=httpx.MockTransport(bedrock))
    turn = await sent(maker, "which projects are there?")

    await run_turn(turn.id, deps_with(backend), owner=OWNER)

    done = await row(maker, turn.id)
    assert (done.status, done.stop_reason, done.failure_message) == ("done", "end", None)
    assert (done.input_tokens, done.output_tokens, done.cost_usd) == (7579 + 7642, 43 + 63, None)
    items = await items_of(maker, turn.conversation_id)
    assert [item.kind for item in items] == [
        "user_text",
        "assistant_text",
        "tool",
        "assistant_text",
    ]
    assert items[1].payload == {"text": "I'll list the factory projects now."}
    tool = items[2].payload
    assert (tool["name"], tool["input"], tool["status"], tool["ok"]) == (
        "factorial_projects_list",
        {},
        "done",
        True,
    )
    first, second = (json.loads(request.content) for request in requests)
    assert requests[0].headers["authorization"] == "Bearer bedrock-api-key-test"
    assert str(requests[0].url) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses"
    )
    assert {tool["name"] for tool in first["tools"]} == set(registry())
    assert first["input"][0]["content"][0]["text"] == system_prompt()
    # The model's next request carries all the tool read; the Drawer's item keeps its start.
    handed = second["input"][-1]
    assert (handed["type"], handed["call_id"]) == ("function_call_output", tool["call_id"])
    assert '{"projects":' in handed["output"]
    assert handed["output"].startswith(tool["summary"])


async def test_a_turn_that_cannot_start_logs_its_turn_and_conversation(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)

    def no_backend(kind: str) -> ChatBackend:
        raise RuntimeError("no backend")

    deps = dataclasses.replace(deps_with(FakeBackend([])), backend_for=no_backend)
    with probing("orchestrator.assistant.runner") as handler:
        await run_turn(turn.id, deps, owner=OWNER)

    async with maker() as session:
        organization_id = await default_organization_id(session)
    (line,) = [line for line in handler.lines if "could not start" in line["msg"]]
    assert (line["turn_id"], line["conversation_id"], line["org_id"]) == (
        turn.id,
        turn.conversation_id,
        organization_id,
    )


async def test_wall_clock_cancels_and_fails_with_timeout(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend([TextDelta(block="b", text="thinking")], hang=True)

    await asyncio.wait_for(
        run_turn(turn.id, deps_with(backend, turn_wall_clock_seconds=0.2), owner=OWNER),
        timeout=5,
    )

    failed = await row(maker, turn.id)
    assert (failed.status, failed.failure_reason) == ("failed", "timeout")
    assert backend.cancelled == [str(turn.id)]
    [text] = await kinds(maker, turn.conversation_id, ChatItemKind.ASSISTANT_TEXT)
    assert (text.payload["text"], text.final) == ("thinking", True)


async def test_cancel_requested_reaches_the_backend(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend(hang=True)
    deps = deps_with(backend)
    running = asyncio.create_task(run_turn(turn.id, deps, owner=OWNER))
    await backend.started()
    await renewed(maker, turn.id)

    await request_cancel(maker, turn.id)
    # The loop's LISTEN fires this on the cancel route's NOTIFY. The next renewal is 20 s away,
    # so only the wake can bring the check this soon.
    deps.wakes.fire()
    await asyncio.wait_for(running, timeout=5)

    assert backend.cancelled == [str(turn.id)]
    cancelled = await row(maker, turn.id)
    assert (cancelled.status, cancelled.stop_reason) == ("cancelled", "cancelled")


async def test_a_cancel_before_the_backend_starts_never_runs_it(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    await request_cancel(maker, turn.id)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend), owner=OWNER)

    assert backend.specs == []
    assert (await row(maker, turn.id)).status == "cancelled"


async def test_a_turn_runs_its_tools_in_process_and_mints_no_token(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker, "where is retry?", page="/tickets/42")
    backend = FakeBackend(hang=True)
    deps = deps_with(backend)
    running = asyncio.create_task(run_turn(turn.id, deps, owner=OWNER))
    await backend.started()
    [spec], [tools] = backend.specs, backend.toolboxes

    assert (spec.turn_id, spec.conversation_id) == (str(turn.id), str(turn.conversation_id))
    assert spec.message == "The person is viewing /tickets/42 (ticket id:42).\nwhere is retry?"
    assert spec.system_prompt == system_prompt()
    assert (spec.auth_mode, spec.credential.get_secret_value()) == ("api_key", "sk-ant-test")
    assert spec.limits.wall_clock == timedelta(seconds=300)
    shown = await tools.call("factorial_ui_show", {"path": "/tickets/42"})
    assert shown.ok, shown.text
    [card] = await kinds(maker, turn.conversation_id, ChatItemKind.SHOW_ME)
    assert card.turn_id == turn.id

    await request_cancel(maker, turn.id)
    deps.wakes.fire()
    await asyncio.wait_for(running, timeout=5)
    # The control is the call above: the same toolbox, while its turn ran.
    late = await tools.call("factorial_ui_show", {"path": "/tickets/42"})
    assert not late.ok and "not running" in late.text
    async with maker() as session:
        minted = await session.scalar(
            select(func.count()).select_from(McpToken).where(McpToken.chat_turn_id == turn.id)
        )
    assert minted == 0


async def test_turns_share_one_github_token_for_the_factorys_repos(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    async with maker() as session:
        await seed_project(session, "acme/runner-token")
        await session.commit()
    fetch = FakeTokenFetch()
    deps = deps_with(FakeBackend([TurnDone(stop=TurnStop.END)]), fetch=fetch)

    await run_turn((await sent(maker)).id, deps, owner=OWNER)
    await run_turn((await sent(maker)).id, deps, owner=OWNER)

    assert fetch.calls == 1
    async with maker() as session:
        default = await default_organization_id(session)
    assert deps.github_tokens.for_organization(default, "default").held


async def test_a_turn_runs_when_no_github_token_can_be_had(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    async with maker() as session:
        await seed_project(session, "acme/runner-token-down")
        await session.commit()
    fetch = FakeTokenFetch(fails=True)
    turn = await sent(maker)
    deps = deps_with(FakeBackend([TurnDone(stop=TurnStop.END)]), fetch=fetch)

    await run_turn(turn.id, deps, owner=OWNER)

    assert fetch.calls == 1
    assert (await row(maker, turn.id)).status == ChatTurnStatus.DONE


async def test_a_stream_that_ends_without_a_result_fails_the_turn(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)

    await run_turn(
        turn.id, deps_with(FakeBackend([TextDelta(block="b", text="half")])), owner=OWNER
    )

    failed = await row(maker, turn.id)
    assert (failed.status, failed.failure_reason) == ("failed", "infrastructure")
    [notice] = await kinds(maker, turn.conversation_id, ChatItemKind.NOTICE)
    assert notice.payload["reason"] == "infrastructure"


async def test_a_failed_turn_says_why_and_a_limit_says_where_it_stopped(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    refused = await sent(maker)
    await run_turn(
        refused.id,
        deps_with(
            FakeBackend([TurnFailed(reason=TurnFailure.AUTH, message="401 invalid x-api-key")])
        ),
        owner=OWNER,
    )
    limited = await sent(maker)
    await run_turn(
        limited.id, deps_with(FakeBackend([TurnDone(stop=TurnStop.BUDGET)])), owner=OWNER
    )

    failed = await row(maker, refused.id)
    assert (failed.status, failed.failure_reason, failed.failure_message) == (
        "failed",
        "auth",
        "401 invalid x-api-key",
    )
    [auth] = await kinds(maker, refused.conversation_id, ChatItemKind.NOTICE)
    assert auth.payload == {"text": "The Claude credential was rejected.", "reason": "auth"}
    stopped = await row(maker, limited.id)
    assert (stopped.status, stopped.stop_reason) == ("done", "budget")
    [limit] = await kinds(maker, limited.conversation_id, ChatItemKind.NOTICE)
    assert limit.payload == {"text": "Stopped at the per-turn limit.", "reason": "budget"}


async def test_a_turn_the_backend_fails_logs_why(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """A backend that reports its own failure raises nothing, so no traceback is logged. The
    reason went to the turn's row alone, and whoever watched the process saw a failed turn and
    no word of why."""
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend(
        [TurnFailed(reason=TurnFailure.INFRASTRUCTURE, message="codex exited 1: no rollout")]
    )

    with probing("orchestrator.assistant.runner") as handler:
        await run_turn(turn.id, deps_with(backend), owner=OWNER)

    (line,) = [line for line in handler.lines if "codex exited 1: no rollout" in line["msg"]]
    assert line["level"] == "WARNING"
    assert "infrastructure" in line["msg"]
    assert (line["turn_id"], line["conversation_id"]) == (turn.id, turn.conversation_id)


async def test_a_turn_refused_before_its_backend_starts_logs_why(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    turn = await sent(maker)

    with probing("orchestrator.assistant.runner") as handler:
        await run_turn(turn.id, deps_with(FakeBackend([])), owner=OWNER)

    (line,) = [line for line in handler.lines if "off or has no credential" in line["msg"]]
    assert line["level"] == "WARNING"
    assert "config" in line["msg"]
    assert (line["turn_id"], line["conversation_id"]) == (turn.id, turn.conversation_id)


async def test_an_approval_turn_tells_the_model_what_was_decided(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    deps = deps_with(backend)
    first = await sent(maker, "retry both")
    await run_turn(first.id, deps, owner=OWNER)
    first_started = (await row(maker, first.id)).started_at
    assert first_started is not None

    async with maker() as session:
        card = await append_item(
            session,
            conversation_id=first.conversation_id,
            turn_id=first.id,
            kind=ChatItemKind.PROPOSAL,
            payload={},
        )
        for status, decided_at, result in [
            (ChatProposalStatus.SUCCEEDED, first_started - timedelta(hours=1), {"old": True}),
            (ChatProposalStatus.SUCCEEDED, first_started + timedelta(seconds=1), {"landed": False}),
            (ChatProposalStatus.REFUSED, first_started + timedelta(seconds=2), {"error": "moved"}),
            (ChatProposalStatus.PENDING, None, None),
        ]:
            session.add(
                ChatProposal(
                    conversation_id=first.conversation_id,
                    turn_id=first.id,
                    item_id=card.id,
                    tool="factorial_ticket_retry",
                    args={},
                    preview={},
                    status=status,
                    expires_at=first_started + timedelta(days=1),
                    decided_at=decided_at,
                    decided_by=None if decided_at is None else CHAT_OWNER,
                    result=result,
                )
            )
        await session.flush()
        proposals = (
            await session.scalars(
                select(ChatProposal.id)
                .where(ChatProposal.conversation_id == first.conversation_id)
                .order_by(ChatProposal.id)
            )
        ).all()
        continuation = await enqueue_turn(
            session, first.conversation_id, trigger=ChatTurnTrigger.APPROVAL
        )
        await session.commit()
    assert continuation is not None
    await run_turn((await _claim(maker)).id, deps, owner=OWNER)

    assert backend.specs[1].message == (
        f'The person decided proposal P{proposals[1]}: succeeded. Result: {{"landed": false}}.\n'
        f'The person decided proposal P{proposals[2]}: refused. Result: {{"error": "moved"}}.'
    )


async def test_a_user_turn_hears_the_decisions_made_since_the_last_turn(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """A decline or an expiry queues no turn: the person's next message carries it to the
    model, ahead of what they wrote, and the message after that does not repeat it."""
    await enable_assistant(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    deps = deps_with(backend)
    first = await sent(maker, "exit both tickets")
    await run_turn(first.id, deps, owner=OWNER)
    async with maker() as session:
        scope = ChatScope(
            first.id, first.conversation_id, CHAT_OWNER, await default_organization_id(session)
        )
        proposed = [
            await record_proposal(
                session,
                scope=scope,
                tool="factorial_ticket_exit",
                args={"ticket": f"id:{n}", "project": None},
                preview={},
                ttl=ttl,
                now=datetime.now(UTC),
            )
            for n, ttl in ((1, timedelta(hours=24)), (2, timedelta(seconds=-1)))
        ]
        await session.commit()
    declined, stale = proposed
    async with maker() as session:
        await decline(session, declined.id, caller=CHAT_OWNER, now=datetime.now(UTC))
    async with maker() as session:
        assert await expire_stale(session, now=datetime.now(UTC)) == 1
        await session.commit()

    second = await sent(maker, "what now?", conversation_id=first.conversation_id)
    await run_turn(second.id, deps, owner=OWNER)
    third = await sent(maker, "thanks", conversation_id=first.conversation_id)
    await run_turn(third.id, deps, owner=OWNER)

    assert backend.specs[0].message == "exit both tickets"
    assert backend.specs[1].message == (
        f"The person decided proposal P{declined.id}: declined.\n"
        f"The person decided proposal P{stale.id}: expired.\n"
        "what now?"
    )
    assert backend.specs[2].message == "thanks"


async def test_a_decision_is_told_again_after_a_turn_that_never_reached_the_model(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """The continuation that carried an approval failed, so the model never read it: the next
    turn carries it again, counted from the last turn that ended done."""
    await enable_assistant(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    deps = deps_with(backend)
    first = await sent(maker, "retry it")
    await run_turn(first.id, deps, owner=OWNER)
    async with maker() as session:
        proposal = await record_proposal(
            session,
            scope=ChatScope(
                first.id, first.conversation_id, CHAT_OWNER, await default_organization_id(session)
            ),
            tool="factorial_ticket_retry",
            args={"ticket": "id:1", "project": None},
            preview={},
            ttl=timedelta(hours=24),
            now=datetime.now(UTC),
        )
        proposal.status = ChatProposalStatus.SUCCEEDED
        proposal.decided_by = CHAT_OWNER
        proposal.decided_at = datetime.now(UTC)
        proposal.result = {"landed": False}
        queued = await enqueue_turn(
            session, first.conversation_id, trigger=ChatTurnTrigger.APPROVAL
        )
        await session.commit()
    assert queued is not None
    limited = FakeBackend([TurnFailed(reason=TurnFailure.RATE_LIMIT, message="429")])
    continuation = await _claim(maker)
    await run_turn(continuation.id, deps_with(limited), owner=OWNER)
    assert (await row(maker, continuation.id)).status == ChatTurnStatus.FAILED

    later = await sent(maker, "did it work?", conversation_id=first.conversation_id)
    await run_turn(later.id, deps, owner=OWNER)

    told = f'The person decided proposal P{proposal.id}: succeeded. Result: {{"landed": false}}.'
    assert limited.specs[0].message == told
    assert backend.specs[1].message == f"{told}\ndid it work?"


async def _waiting_on_a_lock(maker: async_sessionmaker[AsyncSession]) -> bool:
    async with maker() as session:
        waiting = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
    return bool(waiting)


async def test_a_starting_turn_takes_its_conversation_lock_first(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """0024's lock order: the turn takes its conversation's row before it writes its own row,
    as a delete does, so the two queue on it rather than deadlock."""
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])
    async with maker() as deleting:
        await deleting.execute(
            select(ChatConversation.id)
            .where(ChatConversation.id == turn.conversation_id)
            .with_for_update()
        )
        running = asyncio.create_task(run_turn(turn.id, deps_with(backend), owner=OWNER))
        await eventually(lambda: _waiting_on_a_lock(maker))
        assert backend.specs == []
        await deleting.rollback()
    await asyncio.wait_for(running, timeout=5)

    assert (await row(maker, turn.id)).status == ChatTurnStatus.DONE


async def test_a_turn_this_process_no_longer_holds_is_left_alone(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend([TurnDone(stop=TurnStop.END)])

    await run_turn(turn.id, deps_with(backend), owner="someone-else:9")

    assert backend.specs == []
    assert (await row(maker, turn.id)).status == ChatTurnStatus.RUNNING


async def test_a_turn_recovered_mid_run_is_not_overwritten(
    maker: async_sessionmaker[AsyncSession], deps_with: Deps
) -> None:
    """Another process took the turn as lost while its backend still ran here. When the backend
    stops, the runner must leave the recovery's end and its one notice as they are."""
    await enable_assistant(maker)
    turn = await sent(maker)
    backend = FakeBackend(hang=True)
    running = asyncio.create_task(run_turn(turn.id, deps_with(backend), owner=OWNER))
    await backend.started()
    async with maker() as session:
        assert await recover_lost_turns(session, now=datetime.now(UTC) + timedelta(hours=1)) == [
            turn.id
        ]
        await session.commit()

    await backend.cancel(str(turn.id))
    await asyncio.wait_for(running, timeout=5)

    ended = await row(maker, turn.id)
    assert (ended.status, ended.stop_reason) == ("interrupted", None)
    notices = await kinds(maker, turn.conversation_id, ChatItemKind.NOTICE)
    assert [notice.payload for notice in notices] == [{"text": RESTART_NOTICE}]
