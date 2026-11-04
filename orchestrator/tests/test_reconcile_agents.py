"""Agent-step run specs: what a spawn carries (MCP servers, runtime kind, minted
permissions) and the artifact expectations derived from postconditions.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import AgentManifest
from orchestrator.db.models import ProcessVersion, Ticket
from orchestrator.github import (
    APP_PERMISSIONS,
)
from orchestrator.gitlab import project_client
from orchestrator.gitlab.ca_file import CA_ENV_KEYS
from orchestrator.settings import GitLabSettings, Settings
from runtime.contract import (
    ArtifactKind,
)
from tests.reconcile_world import (
    MANIFEST,
    RESOLVED_AGENT,
    World,
    _build_world,
    event_rows,
    run_tick,
    step_runs,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_the_run_spec_carries_declared_mcp_servers(world: World) -> None:
    """The seam this covers: `.factorial/` config onto the runtime contract, credential-free."""
    await run_tick(world)

    (server,) = world.adapter.spawns[0].mcp
    assert (server.name, server.transport.value) == ("product-docs", "http")
    assert server.tools_allow == ("search_docs",)
    # The reference travels; the value does not. A RunSpec reaches logs, events and error
    # payloads, so rendering here would be the leak `McpServerSpec` exists to avoid (§7.5).
    assert server.headers == {"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"}


async def test_the_run_spec_carries_the_pinned_config_commit(world: World) -> None:
    await run_tick(world)

    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        version = await session.get(ProcessVersion, ticket.process_version_id)
    assert version is not None
    assert world.adapter.spawns[0].workspace.config_sha == version.source_commit_sha

async def test_agent_run_records_its_runtimes_family(world: World) -> None:
    """`step_run.runtime_kind` is the family of the Runtime's type, whatever the manifest still
    declares: later polls and a view of the session read the vendor off it."""
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.runtime_kind == "claude"


async def test_the_spawn_event_records_the_substrate_that_actually_ran_it(world: World) -> None:
    """`agent_spawned.details["runtime"]` is the registry kind the row built, which the family on
    `step_run.runtime_kind` cannot say."""
    await run_tick(world)

    (spawned,) = [e for e in await event_rows(world) if e.type == "agent_spawned"]
    assert spawned.details["runtime"] == "claude_cli"


async def test_a_manifest_without_permissions_mints_the_default_profile(world: World) -> None:
    """Absence requests the default profile, never "nothing" (design.md §7.1 item 5).

    `MANIFEST` declares no `permissions:` key — the shape every manifest written before S7 has —
    and the token it gets must be exactly the profile such a manifest has always run with.
    """
    await run_tick(world)

    (repo, permissions) = world.minted[0]
    assert repo == world.repo
    assert permissions == dict(APP_PERMISSIONS)
    assert world.adapter.spawns[0].secrets["GH_TOKEN"].get_secret_value() == "ghs_faketoken"


async def test_a_declared_permissions_block_is_what_gets_minted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The narrowing S7 exists for: a review-shaped agent asks for `contents: read` and its token
    cannot write the repo it reviews. What is minted is the manifest's own block, not the default
    — and not a superset of it."""
    manifest = AgentManifest.model_validate(
        MANIFEST.model_dump(mode="json", by_alias=True)
        | {"permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"}}
    )
    world = await _build_world(maker, resolved_agent=replace(RESOLVED_AGENT, manifest=manifest))
    await run_tick(world)

    (_repo, permissions) = world.minted[0]
    assert permissions == {
        "contents": "read",
        "pull_requests": "write",
        "metadata": "read",
    }
    # The default profile's `issues: write` is precisely what this agent asked not to have.
    assert "issues" not in permissions


# -------------------------------------------------------------------- derived artifact expectations


async def test_the_run_spec_derives_its_expectations_from_the_postconditions(
    world: World,
) -> None:
    """The whole orchestrator side of derived expectations: the step already says what it must
    produce, so the substrate is told to go and look for exactly that — no new `.factorial` key."""
    await run_tick(world)

    assert [
        (item.kind, dict(item.selector)) for item in world.adapter.spawns[0].expected_artifacts
    ] == [
        (ArtifactKind.BRANCH, {"head_glob": "spec/12-*"}),
        (ArtifactKind.PR, {"head_glob": "spec/12-*"}),
    ]


async def test_on_start_fires_when_the_attempt_actually_spawns(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`onStart` is applied on the spawn tick — after preconditions hold and the adapter
    accepted — never while a precondition merely waits, and once per attempt with `{attempt}`
    expanded. The issue otherwise learns nothing between admission and the run's end."""
    from orchestrator.config import ProcessConfig
    from tests.reconcile_world import _process, ticket_row

    cfg = _process("proc-on-start").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["onStart"] = {
        "comment": "{step} agent starting (attempt {attempt})."
    }
    world = await _build_world(
        maker, config=ProcessConfig.model_validate(cfg), assignees=()
    )

    await run_tick(world)  # ticket_has_assignee() is unmet: no spawn, so no announcement
    assert await step_runs(world) == []
    assert all(call != "comment" for call, _ in world.projector.calls)

    world.github.issue = replace(world.github.issue, assignees=("operator",))
    await run_tick(world)  # preconditions hold: the attempt spawns and announces itself

    (run,) = await step_runs(world)
    assert run.attempt == 1
    comments = [body for call, body in world.projector.calls if call == "comment"]
    assert comments == ["plan agent starting (attempt 1)."]
    assert (await ticket_row(world)).current_step == "plan"


async def test_a_github_run_gets_nothing_of_gitlabs_in_a_worker_configured_for_both(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both forges share one worker. A GitHub run's credential is still the App's mint, and none
    of a GitLab run's token, host or CA file reaches it: the CA variables point curl, git and
    Node at one file, and GITLAB_HOST or GIT_HTTP_HOST move its clone and delivery checks."""
    ca = tmp_path / "private-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    gitlab = GitLabSettings(
        base_url="https://gitlab.example.com", token=SecretStr("glpat-test"), ca_bundle=ca
    )
    monkeypatch.setattr(
        project_client,
        "get_settings",
        lambda: Settings(_env_file=None, gitlab=gitlab),  # pyright: ignore[reportCallIssue]
    )
    world = await _build_world(maker)
    await run_tick(world)

    secrets = world.adapter.spawns[0].secrets
    assert not {*CA_ENV_KEYS, "GIT_HTTP_HOST", "GITLAB_HOST", "GITLAB_TOKEN"} & set(secrets)
    assert secrets["GH_TOKEN"].get_secret_value() == "ghs_faketoken"
