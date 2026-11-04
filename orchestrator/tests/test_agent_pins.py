"""Agent definitions freeze with the process version that referenced them.

The bug these cover: `process_version.content` is the process YAML alone, so a ticket pinned its
steps but not its agents. Every agent resolved through one project-wide `agent` row whose
`pinned_commit_sha` any later sync rewrote, and a ticket sitting at `build` on Monday would run
Wednesday's `build-agent` against Monday's process.

Real Postgres, like `test_registry.py`, because what is under test is a pin that persists and is
preferred over a shared row that has since moved.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import Agent, Project
from orchestrator.factorial import (
    ALLOWLIST_KEY,
    AgentInactive,
    AgentPin,
    McpNotAllowed,
    register_process_versions,
    resolve_agent,
)
from tests.registry_fakes import (
    HEAD_SHA,
    PINNED_SHA,
    PRODUCT_DOCS_ALLOWED,
    REPO,
    SAMPLE_KEY,
    make_source,
    sample_runtime,
)

from .reconcile_world import _build_world, run_tick

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

BUILD_MANIFEST = ".factorial/agents/build-agent/agent.yaml"
PLAN_MANIFEST = ".factorial/agents/plan-agent/agent.yaml"


async def _project(session: AsyncSession, *, settings: dict[str, Any] | None = None) -> Project:
    """Unique per call: these tests commit, so nothing may assume a clean database."""
    project = Project(
        organization_id=await default_organization_id(session),
        name=f"proj-{uuid4().hex[:12]}",
        github_repo=REPO,
        settings=settings or {},
    )
    session.add(project)
    await sample_runtime(session)
    await session.flush()
    return project


async def _agent_row(
    session: AsyncSession,
    project: Project,
    name: str,
    *,
    path: str,
    sha: str | None,
    active: bool = True,
) -> Agent:
    row = Agent(
        project_id=project.id, name=name, repo_path=path, pinned_commit_sha=sha, active=active
    )
    session.add(row)
    await session.flush()
    return row


# --- what registration records ------------------------------------------------------------------


async def test_registration_records_one_pin_per_referenced_agent(
    async_session: AsyncSession,
) -> None:
    """The snapshot must carry the agents' shas, or a ticket cannot freeze what it runs."""
    source, _ = make_source()
    project = await _project(async_session)

    result = await register_process_versions(async_session, source, project, ref=None)
    row = result.results[0].row

    assert row is not None
    assert row.agent_pins is not None
    assert set(row.agent_pins) == {"plan-agent", "build-agent", "test-agent"}
    # One sha for all of them: "process and agents move together or not at all".
    assert {pin["sha"] for pin in row.agent_pins.values()} == {HEAD_SHA}
    assert row.agent_pins["build-agent"]["path"] == BUILD_MANIFEST


async def test_every_lane_records_its_own_pins_and_unchanged_moves_none(
    async_session: AsyncSession,
) -> None:
    """The property the plural registration is the one place that could quietly drop.

    Two lanes registered at one commit, re-registered at the same commit, then registered at a
    second: every snapshot taken at the first still resolves its agents there. This is what
    per-version pins bought, and `unchanged` means it because a sha is immutable.
    """
    source, contents = make_source()
    project = await _project(async_session)

    sample = f".factorial/processes/process-{SAMPLE_KEY}.yaml"
    hotfix = ".factorial/processes/process-hotfix.yaml"
    for sha in (PINNED_SHA, HEAD_SHA):
        contents.files[(REPO, hotfix, sha)] = contents.files[(REPO, sample, sha)].replace(
            f"name: {SAMPLE_KEY}", "name: hotfix"
        )

    at_a = await register_process_versions(async_session, source, project, ref=PINNED_SHA)
    assert {r.process_key: r.outcome for r in at_a.results} == {
        SAMPLE_KEY: "created",
        "hotfix": "created",
    }
    pinned_at_a = {r.process_key: r.row for r in at_a.results}
    for row in pinned_at_a.values():
        assert row is not None
        assert {pin["sha"] for pin in (row.agent_pins or {}).values()} == {PINNED_SHA}

    # The same commit again: nothing to write, and nothing moved.
    again = await register_process_versions(async_session, source, project, ref=PINNED_SHA)
    assert {r.outcome for r in again.results} == {"unchanged"}
    assert [r.row.id for r in again.results if r.row] == [  # type: ignore[union-attr]
        row.id for row in pinned_at_a.values() if row
    ]

    # A second commit: new snapshots, pinned there.
    at_b = await register_process_versions(async_session, source, project, ref=None)
    assert {r.process_key: r.outcome for r in at_b.results} == {
        SAMPLE_KEY: "created",
        "hotfix": "created",
    }
    for entry in at_b.results:
        assert entry.row is not None
        assert {pin["sha"] for pin in (entry.row.agent_pins or {}).values()} == {HEAD_SHA}

    # Every snapshot taken at A still points at A — a ticket admitted into either lane runs the
    # agents it was admitted with, whatever a later sync writes beside it.
    for row in pinned_at_a.values():
        assert row is not None
        await async_session.refresh(row)
        assert {pin["sha"] for pin in (row.agent_pins or {}).values()} == {PINNED_SHA}


# --- what resolution prefers ---------------------------------------------------------------------


async def test_version_pin_is_preferred_over_a_moved_project_row(
    async_session: AsyncSession,
) -> None:
    """The bug, named: a later sync moved the shared row, and the ticket must not follow it."""
    source, contents = make_source()
    project = await _project(async_session)
    await _agent_row(async_session, project, "build-agent", path=BUILD_MANIFEST, sha=HEAD_SHA)

    pins = {"build-agent": AgentPin(sha=PINNED_SHA, path=BUILD_MANIFEST)}
    resolved = await resolve_agent(
        async_session, source, project, "build-agent", pins=pins, own_mcp_endpoints=frozenset()
    )

    assert resolved.commit_sha == PINNED_SHA
    # And nothing went looking for a newer sha on the way.
    assert contents.default_branch_calls == []
    assert contents.resolve_ref_calls == []


async def test_the_pinned_path_is_read_not_the_rows_path(async_session: AsyncSession) -> None:
    """`repo_path` is not derivable from the name at an arbitrary commit, so the pin carries it.

    The row points at a folder that does not exist at the pinned sha; reading it would raise.
    """
    source, _ = make_source()
    project = await _project(async_session)
    await _agent_row(
        async_session,
        project,
        "build-agent",
        path=".factorial-renamed/agents/build-agent/agent.yaml",
        sha=HEAD_SHA,
    )

    pins = {"build-agent": AgentPin(sha=PINNED_SHA, path=BUILD_MANIFEST)}
    resolved = await resolve_agent(
        async_session, source, project, "build-agent", pins=pins, own_mcp_endpoints=frozenset()
    )

    assert resolved.commit_sha == PINNED_SHA


async def test_absent_pin_map_falls_back_to_the_project_row(async_session: AsyncSession) -> None:
    """Every row that exists when the column lands has no map; those tickets must not break."""
    source, _ = make_source()
    project = await _project(async_session)
    await _agent_row(async_session, project, "build-agent", path=BUILD_MANIFEST, sha=PINNED_SHA)

    resolved = await resolve_agent(
        async_session, source, project, "build-agent", pins=None, own_mcp_endpoints=frozenset()
    )

    assert resolved.commit_sha == PINNED_SHA


async def test_a_name_missing_from_the_map_falls_back(async_session: AsyncSession) -> None:
    """A map written before a step gained a new agent still has to resolve that agent."""
    source, _ = make_source()
    project = await _project(async_session)
    await _agent_row(async_session, project, "build-agent", path=BUILD_MANIFEST, sha=PINNED_SHA)

    pins = {"plan-agent": AgentPin(sha=HEAD_SHA, path=PLAN_MANIFEST)}
    resolved = await resolve_agent(
        async_session, source, project, "build-agent", pins=pins, own_mcp_endpoints=frozenset()
    )

    assert resolved.commit_sha == PINNED_SHA


async def test_null_pin_with_no_map_still_resolves_and_writes_back(
    async_session: AsyncSession,
) -> None:
    """First-use pinning is untouched: an unpinned row with no map still establishes its pin."""
    source, contents = make_source()
    project = await _project(async_session)
    row = await _agent_row(async_session, project, "build-agent", path=BUILD_MANIFEST, sha=None)

    resolved = await resolve_agent(
        async_session, source, project, "build-agent", pins=None, own_mcp_endpoints=frozenset()
    )

    assert resolved.commit_sha == HEAD_SHA
    assert row.pinned_commit_sha == HEAD_SHA
    assert contents.resolve_ref_calls == [(REPO, "heads/factorial")]


# --- what stays live (agent-pins-plan.md §3) -----------------------------------------------------


async def test_a_pin_does_not_defeat_the_kill_switch(async_session: AsyncSession) -> None:
    """`active` is the operator's stop button and must still reach a ticket mid-flight."""
    source, _ = make_source()
    project = await _project(async_session)
    await _agent_row(
        async_session, project, "build-agent", path=BUILD_MANIFEST, sha=HEAD_SHA, active=False
    )

    pins = {"build-agent": AgentPin(sha=PINNED_SHA, path=BUILD_MANIFEST)}
    with pytest.raises(AgentInactive):
        await resolve_agent(
            async_session, source, project, "build-agent", pins=pins, own_mcp_endpoints=frozenset()
        )


async def test_a_pin_does_not_freeze_the_mcp_allowlist(async_session: AsyncSession) -> None:
    """The allowlist is a ceiling: tightening it must bite tickets already in flight."""
    source, _ = make_source()
    allowed = await _project(async_session, settings={ALLOWLIST_KEY: [PRODUCT_DOCS_ALLOWED]})
    await _agent_row(async_session, allowed, "plan-agent", path=PLAN_MANIFEST, sha=HEAD_SHA)
    pins = {"plan-agent": AgentPin(sha=PINNED_SHA, path=PLAN_MANIFEST)}

    # Permitted while the operator permits it...
    resolved = await resolve_agent(
        async_session, source, allowed, "plan-agent", pins=pins, own_mcp_endpoints=frozenset()
    )
    assert resolved.commit_sha == PINNED_SHA

    # ...and refused the moment they stop, pin or no pin.
    allowed.settings = {ALLOWLIST_KEY: []}
    await async_session.flush()
    with pytest.raises(McpNotAllowed):
        await resolve_agent(
            async_session, source, allowed, "plan-agent", pins=pins, own_mcp_endpoints=frozenset()
        )


# --- the worker hands the ticket's own pins through -----------------------------------------------


async def test_a_tick_resolves_agents_at_its_own_snapshots_pins(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The pin has to reach the resolver, or recording it changed nothing."""
    pins = {"plan-agent": AgentPin(sha=PINNED_SHA, path=PLAN_MANIFEST)}
    world = await _build_world(maker, agent_pins=pins)

    await run_tick(world)

    assert world.resolved_pins == [pins]


async def test_a_tick_on_a_snapshot_without_pins_passes_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Rows that predate revision 0013 must reach the resolver as they always did."""
    world = await _build_world(maker)

    await run_tick(world)

    assert world.resolved_pins == [None]
