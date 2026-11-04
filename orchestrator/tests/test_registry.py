"""The registry half: pins that get established and written back, and append-only snapshots.

Real Postgres, because what is being tested is persistence and the unique constraint on
`(project_id, process_key, source_commit_sha)` — including that a lost insert race leaves the
session usable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.config import AgentManifest, ConfigError
from orchestrator.db.models import Agent, ProcessVersion, Project
from orchestrator.events import EventType, append
from orchestrator.factorial import (
    ALLOWLIST_KEY,
    CEILING_KEY,
    DEFAULT_KEY,
    AgentInactive,
    AgentNotRegistered,
    AgentResolutionError,
    ConfigCommitRefused,
    DefaultProcessMoved,
    McpNotAllowed,
    PermissionNotAllowed,
    RepoConfigSource,
    adopters,
    register_process_versions,
    resolve_agent,
    upsert_agent_rows,
)
from orchestrator.factorial.registry import (
    RuntimeBindingInvalid,
    check_runtime_bindings,
    is_known_commit,
    known_base,
)
from orchestrator.github import MergeProvenance
from tests.registry_fakes import (
    HEAD_SHA,
    PINNED_SHA,
    PRODUCT_DOCS_ALLOWED,
    REPO,
    SAMPLE_KEY,
    SAMPLE_TEXT,
    make_source,
    sample_runtime,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def _sample_process(source: RepoConfigSource, sha: str) -> Any:
    """The sample's one lane. It lives under `processes/` now, so it is read by key."""
    return (await source.load_processes(REPO, sha))[SAMPLE_KEY]


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


async def _project_allowing_mcp(session: AsyncSession) -> Project:
    return await _project(session, settings={ALLOWLIST_KEY: [PRODUCT_DOCS_ALLOWED]})


# --- resolve_agent ------------------------------------------------------------------------------


async def test_null_pin_is_resolved_and_written_back(async_session: AsyncSession) -> None:
    """Nothing may run against floating config: first use establishes the pin, permanently."""
    source, contents = make_source()
    project = await _project_allowing_mcp(async_session)  # the sample's plan-agent declares MCP
    row = Agent(
        project_id=project.id,
        name="plan-agent",
        repo_path=".factorial/agents/plan-agent/agent.yaml",
        pinned_commit_sha=None,
    )
    async_session.add(row)
    await async_session.flush()

    resolved = await resolve_agent(
        async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
    )
    assert resolved.commit_sha == HEAD_SHA
    assert resolved.system_prompt.strip()
    assert contents.resolve_ref_calls == [(REPO, "heads/factorial")]

    # Re-read from the database, not the identity map: the point is that the pin persisted.
    row_id = row.id  # read before expiring; an expired attribute would lazy-load synchronously
    await async_session.commit()
    async_session.expire_all()
    stored = (await async_session.execute(select(Agent).where(Agent.id == row_id))).scalar_one()
    assert stored.pinned_commit_sha == HEAD_SHA


async def test_an_existing_pin_is_never_re_resolved(async_session: AsyncSession) -> None:
    source, contents = make_source()
    project = await _project(async_session)
    async_session.add(
        Agent(
            project_id=project.id,
            name="build-agent",
            repo_path=".factorial/agents/build-agent/agent.yaml",
            pinned_commit_sha=PINNED_SHA,
        )
    )
    await async_session.flush()

    resolved = await resolve_agent(
        async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
    )
    assert resolved.commit_sha == PINNED_SHA
    assert contents.default_branch_calls == []
    assert contents.resolve_ref_calls == []


async def test_unregistered_agent_is_an_operator_error(async_session: AsyncSession) -> None:
    source, _ = make_source()
    project = await _project(async_session)

    with pytest.raises(AgentNotRegistered) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    message = str(exc.value)
    assert project.name in message and "plan-agent" in message
    # The message must say where the definition is expected to live.
    assert ".factorial/agents/plan-agent/agent.yaml" in message


async def test_inactive_agent_refuses(async_session: AsyncSession) -> None:
    source, _ = make_source()
    project = await _project(async_session)
    async_session.add(
        Agent(
            project_id=project.id,
            name="plan-agent",
            repo_path=".factorial/agents/plan-agent/agent.yaml",
            pinned_commit_sha=PINNED_SHA,
            active=False,
        )
    )
    await async_session.flush()

    with pytest.raises(AgentInactive) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    assert isinstance(exc.value, AgentResolutionError)
    assert "inactive" in str(exc.value)


# --- the MCP allowlist (design.md §7.5) ---------------------------------------------------------


async def _plan_agent_row(session: AsyncSession, project: Project) -> None:
    session.add(
        Agent(
            project_id=project.id,
            name="plan-agent",
            repo_path=".factorial/agents/plan-agent/agent.yaml",
            pinned_commit_sha=PINNED_SHA,
        )
    )
    await session.flush()


async def test_a_declared_server_needs_an_allowlist_entry(async_session: AsyncSession) -> None:
    """Silence means "none": a project with no allowlist runs no MCP server, however the repo
    config reads. `.factorial/` is in the repo agents write to, so it selects and never grants."""
    source, _ = make_source()
    project = await _project(async_session)  # no settings at all
    await _plan_agent_row(async_session, project)

    with pytest.raises(McpNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    assert "product-docs" in str(exc.value) and ALLOWLIST_KEY in str(exc.value)


async def test_an_allowlisted_server_resolves(async_session: AsyncSession) -> None:
    source, _ = make_source()
    project = await _project_allowing_mcp(async_session)
    await _plan_agent_row(async_session, project)

    resolved = await resolve_agent(
        async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
    )
    server = next(item for item in resolved.mcp_servers if item.name == "product-docs")
    assert server.scope == "shared"
    assert server.manifest.tools.allow == ("search_docs", "get_doc")
    # The MCP server's secret joins the agent's own, because the adapter renders `{secrets.KEY}`
    # from what the orchestrator injected — an unresolved reference is a spawn failure.
    assert "PRODUCT_DOCS_TOKEN" in resolved.secret_keys()


async def test_a_key_an_mcp_server_references_needs_no_sandbox_delivery(
    async_session: AsyncSession,
) -> None:
    """It reaches the run as that server's header, so a sandbox does not also put it in the
    agent's environment (tech-stack decision 75)."""
    source, _ = make_source()
    project = await _project_allowing_mcp(async_session)
    await _plan_agent_row(async_session, project)

    resolved = await resolve_agent(
        async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
    )
    declares = [{"key": key} for key in ("GH_TOKEN", "PRODUCT_DOCS_TOKEN", "DB_PASSWORD")]
    manifest = AgentManifest.model_validate(
        resolved.manifest.model_dump(mode="json", by_alias=True) | {"secrets": declares}
    )
    agent = replace(resolved, manifest=manifest)
    assert agent.env_secret_keys() == ("GH_TOKEN", "DB_PASSWORD")


async def test_a_url_the_allowlist_does_not_name_is_refused(async_session: AsyncSession) -> None:
    """The attack this blocks: same trusted name, different endpoint."""
    source, _ = make_source()
    entry = {**PRODUCT_DOCS_ALLOWED, "url": "https://mcp.internal.example.com/other-team/mcp"}
    project = await _project(async_session, settings={ALLOWLIST_KEY: [entry]})
    await _plan_agent_row(async_session, project)

    with pytest.raises(McpNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    assert "other-team" in str(exc.value)


async def test_a_tool_outside_the_allowlist_is_refused(async_session: AsyncSession) -> None:
    source, _ = make_source()
    entry = {**PRODUCT_DOCS_ALLOWED, "tools": ["search_docs"]}  # sample also asks for get_doc
    project = await _project(async_session, settings={ALLOWLIST_KEY: [entry]})
    await _plan_agent_row(async_session, project)

    with pytest.raises(McpNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    assert "get_doc" in str(exc.value)


async def test_an_unusable_allowlist_blocks_rather_than_permits(
    async_session: AsyncSession,
) -> None:
    """A malformed allowlist must not fail open — an operator typo is not a grant."""
    source, _ = make_source()
    project = await _project(async_session, settings={ALLOWLIST_KEY: {"name": "product-docs"}})
    await _plan_agent_row(async_session, project)

    with pytest.raises(McpNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "plan-agent", own_mcp_endpoints=frozenset()
        )
    assert "unusable" in str(exc.value)


async def test_an_agent_without_mcp_never_consults_the_allowlist(
    async_session: AsyncSession,
) -> None:
    """The common case stays free: build-agent declares no servers, so a broken allowlist —
    or none — is irrelevant to it."""
    source, _ = make_source()
    project = await _project(async_session, settings={ALLOWLIST_KEY: "nonsense"})
    async_session.add(
        Agent(
            project_id=project.id,
            name="build-agent",
            repo_path=".factorial/agents/build-agent/agent.yaml",
            pinned_commit_sha=PINNED_SHA,
        )
    )
    await async_session.flush()

    resolved = await resolve_agent(
        async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
    )
    assert resolved.mcp_servers == ()


# --- the permission ceiling (design.md §7.1 amendment, decision S7) -----------------------------


async def _build_agent_row(session: AsyncSession, project: Project) -> None:
    """`build-agent` declares no MCP servers, so its resolution never touches the MCP
    allowlist — permission-ceiling tests can set `project.settings` without also having to
    satisfy `ALLOWLIST_KEY`."""
    session.add(
        Agent(
            project_id=project.id,
            name="build-agent",
            repo_path=".factorial/agents/build-agent/agent.yaml",
            pinned_commit_sha=PINNED_SHA,
        )
    )
    await session.flush()


async def test_a_manifest_without_permissions_key_passes_with_no_ceiling_configured(
    async_session: AsyncSession,
) -> None:
    """The back-compat position: the sample's build-agent declares no `permissions:` key, and
    no ceiling is configured — resolution must succeed exactly as it did before this feature."""
    source, _ = make_source()
    project = await _project(async_session)  # no settings at all
    await _build_agent_row(async_session, project)

    resolved = await resolve_agent(
        async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
    )
    assert resolved.manifest.permissions is None


async def test_an_over_ceiling_request_is_refused_with_every_violation_named(
    async_session: AsyncSession,
) -> None:
    source, _ = make_source()
    # build-agent's manifest declares no `permissions:` key, so it requests the default profile
    # (contents/pull_requests/issues write + metadata read) — narrower than that on every scope.
    project = await _project(async_session, settings={CEILING_KEY: {"contents": "read"}})
    await _build_agent_row(async_session, project)

    with pytest.raises(PermissionNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
        )
    message = str(exc.value)
    assert CEILING_KEY in message
    # The default profile requests four scopes; only `contents` is even mentioned in the
    # ceiling, so every other scope is refused too — all of them named, not just the first.
    assert "contents" in message and "issues" in message
    assert "pull_requests" in message and "metadata" in message


async def test_fail_closed_on_a_malformed_permission_ceiling(
    async_session: AsyncSession,
) -> None:
    """A malformed ceiling must not fail open — an operator typo is not a grant."""
    source, _ = make_source()
    project = await _project(async_session, settings={CEILING_KEY: "nonsense"})
    await _build_agent_row(async_session, project)

    with pytest.raises(PermissionNotAllowed) as exc:
        await resolve_agent(
            async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
        )
    assert "unusable" in str(exc.value)


async def test_a_manifest_declaring_workflows_is_refused_at_parse_independent_of_any_ceiling(
    async_session: AsyncSession,
) -> None:
    """`workflows` is banned in the pydantic model itself (design.md §7.2) — it never reaches
    the ceiling check, so this fails even though no ceiling is configured at all."""
    source, contents = make_source()
    project = await _project(async_session)  # no settings — a ceiling would not even apply
    await _build_agent_row(async_session, project)

    key = (REPO, ".factorial/agents/build-agent/agent.yaml", PINNED_SHA)
    contents.files[key] = contents.files[key] + "\npermissions:\n  workflows: write\n"

    with pytest.raises(ConfigError) as exc:
        await resolve_agent(
            async_session, source, project, "build-agent", own_mcp_endpoints=frozenset()
        )
    assert "workflows" in str(exc.value)


# --- upsert_agent_rows --------------------------------------------------------------------------


async def _agent_rows(session: AsyncSession, project: Project) -> Sequence[Agent]:
    """The registry rows themselves, by name — `upsert_agent_rows` now returns pins, not rows."""
    return list(
        (
            await session.execute(
                select(Agent).where(Agent.project_id == project.id).order_by(Agent.name)
            )
        )
        .scalars()
        .all()
    )


async def test_upsert_creates_one_row_per_referenced_agent_and_is_idempotent(
    async_session: AsyncSession,
) -> None:
    source, _ = make_source()
    project = await _project(async_session)
    process = await _sample_process(source, PINNED_SHA)

    pins = await upsert_agent_rows(async_session, source, project, process, PINNED_SHA)
    assert sorted(pins) == ["build-agent", "plan-agent", "test-agent"]
    assert [pins[name]["path"] for name in sorted(pins)] == [
        ".factorial/agents/build-agent/agent.yaml",
        ".factorial/agents/plan-agent/agent.yaml",
        ".factorial/agents/test-agent/agent.yaml",
    ]
    assert {pin["sha"] for pin in pins.values()} == {PINNED_SHA}
    rows = await _agent_rows(async_session, project)
    assert {r.pinned_commit_sha for r in rows} == {PINNED_SHA}
    first_ids = sorted(r.id for r in rows)

    again = await upsert_agent_rows(async_session, source, project, process, HEAD_SHA)
    assert {pin["sha"] for pin in again.values()} == {HEAD_SHA}
    rows = await _agent_rows(async_session, project)
    assert sorted(r.id for r in rows) == first_ids  # updated, never duplicated
    assert {r.pinned_commit_sha for r in rows} == {HEAD_SHA}  # re-pinned

    count = (
        await async_session.execute(
            select(func.count()).select_from(Agent).where(Agent.project_id == project.id)
        )
    ).scalar_one()
    assert count == 3


async def test_upsert_records_the_folder_the_pinned_sha_actually_has(
    async_session: AsyncSession,
) -> None:
    """`repo_path` is read back at `pinned_commit_sha` to find the manifest, so it has to name
    the folder that commit contains — not whatever this source defaults to."""
    source, _ = make_source(config_dir=".adlc")
    project = await _project(async_session)
    process = await _sample_process(source, PINNED_SHA)

    pins = await upsert_agent_rows(async_session, source, project, process, PINNED_SHA)
    assert [pins[name]["path"] for name in sorted(pins)] == [
        ".adlc/agents/build-agent/agent.yaml",
        ".adlc/agents/plan-agent/agent.yaml",
        ".adlc/agents/test-agent/agent.yaml",
    ]
    # The row is read back at its pin, so it has to carry the same path the map does.
    assert [r.repo_path for r in await _agent_rows(async_session, project)] == [
        ".adlc/agents/build-agent/agent.yaml",
        ".adlc/agents/plan-agent/agent.yaml",
        ".adlc/agents/test-agent/agent.yaml",
    ]


# --- register_process_versions ------------------------------------------------------------------


async def test_register_stores_the_sha_the_snapshot_and_the_agents(
    async_session: AsyncSession,
) -> None:
    source, _ = make_source()
    project = await _project(async_session)

    result = await register_process_versions(async_session, source, project, ref=None)
    assert result.sha == HEAD_SHA
    assert [(r.process_key, r.outcome) for r in result.results] == [(SAMPLE_KEY, "created")]
    row = result.results[0].row
    assert row is not None
    assert (row.process_key, row.source_commit_sha) == (SAMPLE_KEY, HEAD_SHA)
    # The snapshot keeps the YAML spelling, so it round-trips into the same models.
    assert row.content["specVersion"] == "factorial/v1"
    assert row.content["entry"]["step"] == "plan"
    assert [step["name"] for step in row.content["steps"]] == [
        "plan",
        "spec_review_gate",
        "build",
        "test",
        "code_review_gate",
    ]

    pins = (
        await async_session.execute(
            select(Agent.name, Agent.pinned_commit_sha).where(Agent.project_id == project.id)
        )
    ).all()
    assert sorted(pins) == [
        ("build-agent", HEAD_SHA),
        ("plan-agent", HEAD_SHA),
        ("test-agent", HEAD_SHA),
    ]


_UNMERGED = MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)


_HUMAN = MergeProvenance(merged_pr=7, merged_by="operator", merged_by_human=True)
_AGENT_PUSH = "e" * 40


@pytest.mark.parametrize(
    ("provenance", "base"),
    [
        (_UNMERGED, None),
        (MergeProvenance(merged_pr=7, merged_by="factorial[bot]", merged_by_human=False), None),
        (_HUMAN, _AGENT_PUSH),
    ],
    ids=["no_merged_pr", "bot_merged", "human_merged_onto_an_agent_push"],
)
async def test_a_commit_no_human_merged_into_the_config_branch_is_refused_before_any_write(
    async_session: AsyncSession, provenance: MergeProvenance, base: str | None
) -> None:
    """Agents act as the App and set their own git author, so only a human merge vouches, and
    only onto a known base: a merge on top of an agent's push would carry the push."""
    source, contents = make_source()
    contents.provenance = provenance
    if base is not None:
        contents.parents[HEAD_SHA] = (base,)
        contents.provenances[base] = _UNMERGED
    project = await _project(async_session)

    with pytest.raises(ConfigCommitRefused, match=HEAD_SHA[:7]):
        await register_process_versions(async_session, source, project, ref=None)
    written = (
        await async_session.execute(
            select(func.count())
            .select_from(ProcessVersion)
            .where(ProcessVersion.project_id == project.id)
        )
    ).scalar_one()
    assert written == 0


@pytest.mark.parametrize(
    "vouched_by",
    ["human_merge", EventType.CONFIG_BRANCH_CREATED, EventType.CONFIG_COMMITTED],
)
async def test_a_commit_is_synced_when_a_human_merged_it_or_the_orchestrator_wrote_it(
    async_session: AsyncSession, vouched_by: str
) -> None:
    source, contents = make_source()
    project = await _project(async_session)
    if vouched_by == "human_merge":
        contents.provenance = _HUMAN
    else:
        contents.provenance = _UNMERGED
        await append(
            async_session,
            type=EventType(vouched_by),
            project_id=project.id,
            branch="factorial",
            sha=HEAD_SHA,
        )

    result = await register_process_versions(async_session, source, project, ref=None)
    assert [r.outcome for r in result.results] == ["created"]


async def test_an_adopted_head_is_known_and_a_push_on_top_is_refused_again(
    async_session: AsyncSession,
) -> None:
    """An operator's adoption vouches for exactly the head it names: the next direct push is
    as unaccounted for as the first, and the reset target for it is the adopted commit."""
    source, contents = make_source()
    project = await _project(async_session)
    seeded, pushed, again = "1" * 40, "2" * 40, "3" * 40
    unmerged = MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)
    contents.provenances = {seeded: unmerged, pushed: unmerged, again: unmerged}
    contents.parents = {pushed: (seeded,), again: (pushed,)}
    await append(
        async_session,
        type=EventType.CONFIG_BRANCH_CREATED,
        project_id=project.id,
        branch="factorial",
        sha=seeded,
    )
    assert not await is_known_commit(async_session, source, project, pushed)

    await append(
        async_session,
        type=EventType.CONFIG_HEAD_ADOPTED,
        project_id=project.id,
        branch="factorial",
        sha=pushed,
        known_base=seeded,
        commits=1,
        requested_by="p@example.com",
        requested_via="dashboard",
    )

    assert await is_known_commit(async_session, source, project, pushed)
    assert not await is_known_commit(async_session, source, project, again)
    assert await known_base(async_session, source, project, again) == pushed
    assert await adopters(async_session, project.id) == {pushed: "p@example.com"}


async def test_re_registering_the_same_commit_is_unchanged_not_a_refusal(
    async_session: AsyncSession,
) -> None:
    """The outcome that makes the route usable with several lanes: a commit touching one lane
    leaves every other at a commit this project already holds, which as a refusal would 409 the
    whole call. No content comparison is needed to be sure — a sha is immutable."""
    source, _ = make_source()
    project = await _project(async_session)
    first = await register_process_versions(async_session, source, project, ref=None)

    again = await register_process_versions(async_session, source, project, ref=None)
    assert [(r.process_key, r.outcome) for r in again.results] == [(SAMPLE_KEY, "unchanged")]
    assert again.results[0].row is not None
    assert again.results[0].row.id == first.results[0].row.id  # type: ignore[union-attr]

    versions = (
        await async_session.execute(
            select(func.count())
            .select_from(ProcessVersion)
            .where(ProcessVersion.project_id == project.id)
        )
    ).scalar_one()
    assert versions == 1  # never rewritten, never doubled


async def test_a_new_commit_is_a_new_snapshot_with_no_number_to_bump(
    async_session: AsyncSession,
) -> None:
    """What replaced the "you forgot to bump `version`" refusal: editing the file produces a new
    commit, and a new commit is simply a new row. Nothing to bump, nothing to refuse."""
    source, contents = make_source()
    project = await _project(async_session)
    first = await register_process_versions(async_session, source, project, ref=PINNED_SHA)
    assert [r.outcome for r in first.results] == ["created"]

    path = f".factorial/processes/process-{SAMPLE_KEY}.yaml"
    contents.files[(REPO, path, HEAD_SHA)] = contents.files[(REPO, path, HEAD_SHA)].replace(
        "description: Implement the approved spec",
        "description: Implement the approved spec, carefully",
    )
    second = await register_process_versions(async_session, source, project, ref=None)
    assert [(r.process_key, r.outcome) for r in second.results] == [(SAMPLE_KEY, "created")]

    rows = (
        (
            await async_session.execute(
                select(ProcessVersion).where(ProcessVersion.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(r.source_commit_sha for r in rows) == sorted([PINNED_SHA, HEAD_SHA])


async def test_a_lane_the_commit_no_longer_offers_is_retired(async_session: AsyncSession) -> None:
    """Version history is not a menu: deleting a lane's file must stop it being selectable, or
    the key would stay offerable for ever (design.md §4)."""
    source, contents = make_source()
    project = await _project(async_session)
    await register_process_versions(async_session, source, project, ref=None)

    path = f".factorial/processes/process-{SAMPLE_KEY}.yaml"
    other = path.replace(SAMPLE_KEY, "hotfix")
    contents.files[(REPO, other, HEAD_SHA)] = contents.files[(REPO, path, HEAD_SHA)].replace(
        f"name: {SAMPLE_KEY}", "name: hotfix"
    )
    added = await register_process_versions(async_session, source, project, ref=None)
    assert sorted((r.process_key, r.outcome) for r in added.results) == [
        ("hotfix", "created"),
        ("simple", "unchanged"),
    ]

    del contents.files[(REPO, other, HEAD_SHA)]
    removed = await register_process_versions(async_session, source, project, ref=None)
    assert ("hotfix", "retired") in [(r.process_key, r.outcome) for r in removed.results]
    retired = (
        await async_session.execute(
            select(ProcessVersion.retired_at).where(
                ProcessVersion.project_id == project.id, ProcessVersion.process_key == "hotfix"
            )
        )
    ).scalar_one()
    assert retired is not None

    # Putting the file back offers the lane again, keeping every commit it already had.
    contents.files[(REPO, other, HEAD_SHA)] = contents.files[(REPO, path, HEAD_SHA)].replace(
        f"name: {SAMPLE_KEY}", "name: hotfix"
    )
    back = await register_process_versions(async_session, source, project, ref=None)
    assert ("hotfix", "unchanged") in [(r.process_key, r.outcome) for r in back.results]
    rows = (
        (
            await async_session.execute(
                select(ProcessVersion).where(
                    ProcessVersion.project_id == project.id,
                    ProcessVersion.process_key == "hotfix",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(r.source_commit_sha, r.retired_at) for r in rows] == [(HEAD_SHA, None)]


async def test_retiring_the_default_lane_moves_the_default(async_session: AsyncSession) -> None:
    """A project whose default names nothing admits no issue that does not label itself, which
    is a whole factory stopped by a file deletion. Refusing the sync did not help: the setting
    can only be re-pointed at a lane the project offers, and this commit has just taken it away.
    So the default follows the rename, and the result says where it went."""
    source, contents = make_source()
    project = await _project(async_session, settings={DEFAULT_KEY: SAMPLE_KEY})
    await register_process_versions(async_session, source, project, ref=None)

    del contents.files[(REPO, f".factorial/processes/process-{SAMPLE_KEY}.yaml", HEAD_SHA)]
    contents.files[(REPO, ".factorial/processes/process-hotfix.yaml", HEAD_SHA)] = (
        SAMPLE_TEXT.replace(f"name: {SAMPLE_KEY}", "name: hotfix")
    )
    result = await register_process_versions(async_session, source, project, ref=None)

    assert result.default_moved == DefaultProcessMoved(previous=SAMPLE_KEY, now="hotfix")
    assert project.settings[DEFAULT_KEY] == "hotfix"
    assert sorted((r.process_key, r.outcome) for r in result.results) == [
        ("hotfix", "created"),
        (SAMPLE_KEY, "retired"),
    ]


async def test_a_default_that_survives_the_commit_is_left_alone(
    async_session: AsyncSession,
) -> None:
    """The move is for a default the commit retired, not for every sync: a project that keeps its
    lane keeps its setting, and `default_moved` is None so nothing is reported."""
    source, contents = make_source()
    project = await _project(async_session, settings={DEFAULT_KEY: SAMPLE_KEY})
    await register_process_versions(async_session, source, project, ref=None)

    contents.files[(REPO, ".factorial/processes/process-hotfix.yaml", HEAD_SHA)] = (
        SAMPLE_TEXT.replace(f"name: {SAMPLE_KEY}", "name: hotfix")
    )
    result = await register_process_versions(async_session, source, project, ref=None)

    assert result.default_moved is None
    assert project.settings[DEFAULT_KEY] == SAMPLE_KEY


async def test_register_records_the_ref_it_was_asked_for(async_session: AsyncSession) -> None:
    """The request, not the resolution (revision 0006).

    Both syncs land on the same sha here, which is the point: a sha cannot say which ref was
    read, so `source_ref` is the only thing that can. No ref is recorded as the config branch it
    read; NULL is left meaning "the default branch", for rows before issue #248.
    """
    source, _ = make_source()
    default = (
        (
            await register_process_versions(
                async_session, source, await _project(async_session), ref=None
            )
        )
        .results[0]
        .row
    )
    named = (
        (
            await register_process_versions(
                async_session, source, await _project(async_session), ref="staging"
            )
        )
        .results[0]
        .row
    )

    assert default is not None and named is not None
    assert (default.source_ref, default.source_commit_sha) == ("factorial", HEAD_SHA)
    assert (named.source_ref, named.source_commit_sha) == ("staging", HEAD_SHA)


# --- runtime bindings ---------------------------------------------------------------------------


async def _lanes(runtimes: dict[str, str | None]) -> dict[str, Any]:
    source, _ = make_source()
    sample = await _sample_process(source, HEAD_SHA)
    return {key: sample.model_copy(update={"runtime": slug}) for key, slug in runtimes.items()}


async def test_sync_names_every_missing_runtime_at_once(async_session: AsyncSession) -> None:
    configs = await _lanes(
        {"canary": "claude-api-on-e2b", "primary": "claude-api-on-e2b", "other": "codex-sub"}
    )
    organization_id = await default_organization_id(async_session)

    with pytest.raises(RuntimeBindingInvalid) as raised:
        await check_runtime_bindings(async_session, organization_id, configs)

    assert raised.value.missing == ("claude-api-on-e2b", "codex-sub")
    assert "'claude-api-on-e2b' (canary, primary)" in str(raised.value)
    assert "'codex-sub' (other)" in str(raised.value)


async def test_sync_reports_a_lane_with_no_runtime_beside_a_missing_one(
    async_session: AsyncSession,
) -> None:
    configs = await _lanes({"bare": None, "ghost": "ghost-rt"})
    organization_id = await default_organization_id(async_session)

    with pytest.raises(RuntimeBindingInvalid) as raised:
        await check_runtime_bindings(async_session, organization_id, configs)

    assert raised.value.missing == ("ghost-rt",)
    assert "'ghost-rt'" in str(raised.value)
    assert "The process 'bare' names no runtime" in str(raised.value)
