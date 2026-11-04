"""What a GitHub run is handed of its project's connection: a `GH_TOKEN` from the project's own
client and the project's bot login, and never a connection secret under its own name."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import AgentManifest
from orchestrator.events import EventType, append
from orchestrator.github import LABEL_INTERRUPTED
from orchestrator.github.client import GitHubClient
from orchestrator.github.connection import CONNECTION_SECRETS, GitHubConnection
from orchestrator.secrets import store
from tests.reconcile_world import (
    MANIFEST,
    RESOLVED_AGENT,
    World,
    _build_world,
    run_tick,
    step_runs,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

BOT = "acme-factorial[bot]"

#: An agent that declares every connection secret by name, as a hostile or careless manifest
#: could, beside a project secret of its own named `GITHUB_TOKEN`.
GREEDY_AGENT = replace(
    RESOLVED_AGENT,
    manifest=AgentManifest.model_validate(
        MANIFEST.model_dump(mode="json", by_alias=True)
        | {
            "secrets": [
                {"key": key}
                for key in sorted({"GH_TOKEN", "GITHUB_TOKEN", *CONNECTION_SECRETS})
            ]
        }
    ),
)


def _values(secrets: dict[str, SecretStr]) -> dict[str, str]:
    return {name: value.get_secret_value() for name, value in secrets.items()}


async def _interrupt_then_release(world: World) -> None:
    (run,) = await step_runs(world)
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            requested_via="dashboard",
        )
    await run_tick(world)
    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels - {LABEL_INTERRUPTED}
    )


async def test_an_app_project_s_run_gets_a_minted_token_and_its_bot_login(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT)

    await run_tick(world)

    (spec,) = world.adapter.spawns
    secrets = _values(dict(spec.secrets))
    assert secrets["GH_TOKEN"] == "ghs_faketoken"
    assert secrets["FACTORIAL_BOT_LOGIN"] == BOT
    assert [repo for repo, _ in world.minted] == [world.repo]


async def test_a_token_project_s_run_gets_the_stored_token_as_gh_token(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login="octo-operator")
    connection = GitHubConnection(
        mode="token",
        app_id="",
        private_key=None,
        token=SecretStr("ghp_stored"),
        webhook_secret=None,
        bot_login="octo-operator",
        installations={},
    )
    world.use_github(client=GitHubClient(connection))

    await run_tick(world)

    (spec,) = world.adapter.spawns
    secrets = _values(dict(spec.secrets))
    assert secrets["GH_TOKEN"] == "ghp_stored"
    assert secrets["FACTORIAL_BOT_LOGIN"] == "octo-operator"
    assert not CONNECTION_SECRETS & secrets.keys()


async def test_connection_secrets_never_reach_a_run_under_their_own_names(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world = await _build_world(maker, bot_login=BOT, resolved_agent=GREEDY_AGENT)
    async with world.sessions() as session:
        for key in CONNECTION_SECRETS:
            await store.put(session, world.project_id, key, f"stored-{key}")
        await store.put(session, world.project_id, "GITHUB_TOKEN", "the-project-s-own")

    await run_tick(world)
    await _interrupt_then_release(world)
    await run_tick(world)

    (spec,) = world.adapter.spawns
    for handed in (dict(spec.secrets), world.adapter.resumed_secrets):
        secrets = _values(handed)
        assert not CONNECTION_SECRETS & secrets.keys()
        assert not any(value.startswith("stored-") for value in secrets.values())
        assert secrets["GH_TOKEN"] == "ghs_faketoken"
        assert secrets["GITHUB_TOKEN"] == "the-project-s-own"
