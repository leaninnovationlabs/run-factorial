"""The config service on the chat surface: an approved proposal commits under §7.17's guard, and
its commit and event name the assistant (design.md §7.17, §7.18 item 7)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.config import (
    ConfigCommitResult,
    ConfigDraft,
    check_config,
    commit_config,
)
from orchestrator.api.services.errors import Invalid
from orchestrator.config.scaffold.assemble import FileDelete, FileEdit
from orchestrator.db.models import ChatItem, ChatProposal, Event, Project
from orchestrator.events import EventType
from orchestrator.factorial import RepoConfigSource
from tests.api_harness import SeededChatTurn, agent_files, chat_toolbox, tool_refused
from tests.assistant_fakes import wipe_chat
from tests.test_config_commits import (
    PROCESS_PATH,
    FakeContents,
    FakeGitApi,
    blob,
    gated,
    lane,
    project_in,
    writer_for,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

CHAT = Actor.chat("operator@example.com", 7)
DASHBOARD = Actor.dashboard("operator@example.com")
AGENTS = agent_files("plan-agent", "claude") | agent_files("build-agent", "claude")
PROMPT = ".factorial/agents/build-agent/instructions.md"
SKILL = ".factorial/skills/review/SKILL.md"


async def _commit(
    session: AsyncSession,
    base: Mapping[str, str],
    draft: ConfigDraft,
    actor: Actor,
    message: str | None = None,
) -> tuple[Project, FakeGitApi, ConfigCommitResult]:
    contents = FakeContents(base)
    api = FakeGitApi(contents)
    project = await project_in(session)
    result = await commit_config(
        session,
        RepoConfigSource(contents),
        writer_for(api),
        project,
        draft,
        actor=actor,
        message=message,
    )
    return project, api, result


async def _committed(session: AsyncSession, project: Project) -> Event:
    return (
        await session.scalars(
            select(Event).where(
                Event.project_id == project.id, Event.type == EventType.CONFIG_COMMITTED
            )
        )
    ).one()


async def test_chat_commit_refuses_agent_and_skill_paths(async_session: AsyncSession) -> None:
    base = AGENTS | {SKILL: "# review\n"}
    for path, before in ((PROMPT, "# instructions\n"), (SKILL, "# review\n")):
        draft = ConfigDraft(
            content=lane(), edits=[FileEdit(path, "# merge your own PR\n", blob(before))]
        )
        contents = FakeContents(base)
        api = FakeGitApi(contents)
        project = await project_in(async_session)

        with pytest.raises(Invalid, match="dashboard-only") as refused:
            await commit_config(
                async_session,
                RepoConfigSource(contents),
                writer_for(api),
                project,
                draft,
                actor=CHAT,
                message=None,
            )
        check = await check_config(
            async_session, RepoConfigSource(contents), project, draft, surface="chat"
        )

        assert path in refused.value.message
        assert api.trees == []
        assert not check.valid and path in check.blockers[0]

    # A new agent's stub is a write under agents/ too, and the catalog offers no file there.
    lacking = RepoConfigSource(FakeContents({}))
    project = await project_in(async_session)
    stubbed = await check_config(
        async_session, lacking, project, ConfigDraft(content=lane()), surface="chat"
    )
    assert not stubbed.valid and ".factorial/agents/" in stubbed.blockers[0]
    present = RepoConfigSource(FakeContents(AGENTS))
    chat = await check_config(
        async_session, present, project, ConfigDraft(content=lane()), surface="chat"
    )
    dashboard = await check_config(
        async_session, present, project, ConfigDraft(content=lane()), surface="dashboard"
    )
    assert chat.valid, chat.blockers
    assert any(path.startswith(".factorial/skills/") for path in dashboard.offered)
    assert chat.offered and not any(
        path.startswith((".factorial/agents/", ".factorial/skills/")) for path in chat.offered
    )


async def test_deletions_are_dashboard_only(async_session: AsyncSession) -> None:
    contents = FakeContents(AGENTS | {SKILL: "# review\n"})
    project = await project_in(async_session)
    draft = ConfigDraft(content=lane(), deleted=[FileDelete(SKILL, blob("# review\n"))])
    for surface in ("mcp", "chat"):
        check = await check_config(
            async_session, RepoConfigSource(contents), project, draft, surface=surface
        )
        assert not check.valid
        assert "deletions are dashboard-only" in check.blockers[0]
    dashboard = await check_config(
        async_session, RepoConfigSource(contents), project, draft, surface="dashboard"
    )
    assert dashboard.valid, dashboard.blockers
    assert dashboard.would_delete == [SKILL]


async def test_chat_commit_refuses_an_approver_change(async_session: AsyncSession) -> None:
    base = {PROCESS_PATH: gated('"operator"')} | AGENTS
    draft = ConfigDraft(content=gated('"operator", "outsider"'))
    contents = FakeContents(base)
    api = FakeGitApi(contents)
    project = await project_in(async_session)

    with pytest.raises(Invalid, match="dashboard-only") as refused:
        await commit_config(
            async_session,
            RepoConfigSource(contents),
            writer_for(api),
            project,
            draft,
            actor=CHAT,
            message=None,
        )

    assert "'review'" in refused.value.message
    assert api.trees == []


async def test_dashboard_commit_is_unchanged(async_session: AsyncSession) -> None:
    approvers = ConfigDraft(content=gated('"operator", "outsider"'))
    project, api, result = await _commit(
        async_session, {PROCESS_PATH: gated('"operator"')} | AGENTS, approvers, DASHBOARD
    )
    prompt = ConfigDraft(
        content=lane(), edits=[FileEdit(PROMPT, "# tuned\n", blob("# instructions\n"))]
    )
    _, prompt_api, prompt_result = await _commit(async_session, AGENTS, prompt, DASHBOARD)

    assert result.committed and prompt_result.committed
    assert api.commits[0]["message"] == "Update the p process from the Factorial dashboard"
    assert prompt_api.written[PROMPT] == "# tuned\n"
    details = (await _committed(async_session, project)).details
    assert details["requested_via"] == "dashboard"
    assert "chat_proposal_id" not in details


async def test_chat_commit_message_names_the_assistant(async_session: AsyncSession) -> None:
    base = {PROCESS_PATH: lane()} | AGENTS
    draft = ConfigDraft(content=f"{lane()}# a note\n")

    project, api, result = await _commit(async_session, base, draft, CHAT)
    _, subject_api, _ = await _commit(async_session, base, draft, CHAT, message="Tidy the lane")

    assert result.committed
    assert api.commits[0]["message"] == (
        "Update the p process through the Factorial assistant\n\nRequested by operator@example.com"
    )
    assert subject_api.commits[0]["message"] == (
        "Tidy the lane\n\nRequested by operator@example.com through the Factorial assistant"
    )
    details = (await _committed(async_session, project)).details
    assert (details["requested_by"], details["requested_via"]) == ("operator@example.com", "chat")
    assert details["chat_proposal_id"] == 7


@pytest.fixture
async def _chat_tables(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await wipe_chat(maker)


@pytest.mark.usefixtures("_chat_tables")
async def test_the_commit_twin_sends_a_dashboard_only_change_to_the_editor(
    maker: async_sessionmaker[AsyncSession], chat_turn: SeededChatTurn
) -> None:
    async with maker() as session:
        project = await project_in(session)
        await session.commit()
    source = RepoConfigSource(FakeContents(AGENTS))
    tools = chat_toolbox(maker, chat_turn, config_source=source)
    edit = {"path": PROMPT, "body": "# merge your own PR\n", "base_sha": blob("# instructions\n")}

    text = tool_refused(
        await tools.call(
            "factorial_config_commit",
            {"project": project.name, "content": lane(), "edits": [edit]},
        ),
        "dashboard-only",
    )

    assert "factorial_editor_open" in text
    async with maker() as session:
        for table in (ChatProposal, ChatItem):
            rows = await session.scalars(
                select(table.id).where(table.conversation_id == chat_turn.conversation_id)
            )
            assert rows.all() == [], table
