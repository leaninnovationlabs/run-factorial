"""`runtime: <slug>` on a process (the runtime registry, design.md §7.5's shape for
substrates): the worker spawns on the operator-defined Runtime the pinned snapshot names,
every later verb reaches the SAME adapter instance, the runtime's credentials ride the RunSpec,
and every refusal is permanent — an operator must act, and a retry would only re-read the same
snapshot. A process without the key is byte-for-byte the pre-registry path; the routing tests
in `test_reconcile_agents.py` stay its proof.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import AgentManifest
from orchestrator.db.models import Project, Runtime, StepRunStatus, TicketStatus
from orchestrator.events import EventType, append
from orchestrator.github import LABEL_BLOCKED, LABEL_EXIT, LABEL_INTERRUPTED
from orchestrator.secrets import store
from orchestrator.secrets.crypto import DecryptionError
from orchestrator.worker import agent_steps as agent_steps_module
from orchestrator.worker import runtime_adapters as runtime_adapters_module
from orchestrator.worker.context import SessionPolicy
from orchestrator.worker.runtime_adapters import (
    RuntimeAdapters,
    RuntimeRefused,
    registry_build,
)
from runtime.contract import AdapterUnavailable, SecretRoute
from tests.reconcile_world import (
    MANIFEST,
    RESOLVED_AGENT,
    FakeAdapter,
    World,
    _build_world,
    _process_on_runtime,
    _runtime_slug,
    event_rows,
    event_types,
    interactive_world,
    remove_label,
    run_tick,
    step_runs,
    ticket_row,
)


def postgres(test: Any) -> Any:
    """Every worker test here needs the database, on the session loop its fixtures live on;
    the cache tests above the worker section need neither."""
    return pytest.mark.asyncio(loop_scope="session")(pytest.mark.postgres(test))


CODEX_MANIFEST = AgentManifest.model_validate(
    MANIFEST.model_dump(mode="json", by_alias=True)
    | {"runtime": {"kind": "codex"}, "model": "gpt-5.1-codex"}
)
CODEX_AGENT = replace(RESOLVED_AGENT, manifest=CODEX_MANIFEST)


# ------------------------------------------------------------------------- the cache itself


def _row(rt: str, *, id: int = 7, slug: str = "team") -> Runtime:
    return Runtime(id=id, slug=slug, name=slug, type=rt)


def test_the_cache_hands_back_one_instance_per_runtime() -> None:
    """Adapters hold supervisors and children, so the one that spawned a run must be the one
    every later verb reaches — a second build for the same row would be a stranger to it."""
    built: list[tuple[int, str]] = []

    def build(row: Runtime, kind: str) -> Any:
        built.append((row.id, kind))
        return FakeAdapter()

    adapters = RuntimeAdapters(build=build)
    row = _row("codex_api")

    first = adapters.for_runtime(row)
    assert adapters.for_runtime(row) is first
    assert adapters.for_runtime(_row("codex_api", id=7, slug="team")) is first  # by id
    assert adapters.cached(7, "codex_cli") is first
    assert adapters.cached(8, "codex_cli") is None
    assert adapters.for_runtime(_row("claude_api", id=8, slug="other")) is not first
    assert built == [(7, "codex_cli"), (8, "claude_cli")]


def test_a_run_keeps_the_kind_it_was_spawned_on() -> None:
    """A row's details can change which kind it builds. A run spawned before the change must
    keep reaching the instance of its own kind — the other one never heard of it."""
    adapters = RuntimeAdapters(build=lambda row, kind: FakeAdapter())
    row = _row("claude_api")

    spawned_on = adapters.for_runtime(row, "claude_cli")

    assert adapters.for_runtime(row, "claude_e2b") is not spawned_on
    assert adapters.for_runtime(row, "claude_cli") is spawned_on


class _Sandboxed(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.workspaces: list[tuple[str | None, str | None]] = []

    def use_workspace(self, *, api_key: SecretStr | None, template: str | None = None) -> None:
        self.workspaces.append((api_key.get_secret_value() if api_key else None, template))


def test_an_e2b_runtime_spawns_into_its_own_team_on_the_one_instance() -> None:
    """The instance is re-pointed, never rebuilt: a second one would adopt the first's live
    runs. A key removed is removed: the adapter must not go on spawning into the old team."""
    built: list[_Sandboxed] = []
    adapters = RuntimeAdapters(build=lambda row, kind: built.append(_Sandboxed()) or built[-1])
    row = Runtime(
        id=9,
        slug="team-b",
        name="team-b",
        type="claude_api",
        details={"isolation": "e2b", "template": "factorial-claude:1234"},
    )

    first = adapters.for_runtime(row, held_key=SecretStr("e2b_team_b"), repoint=True)
    again = adapters.for_runtime(row, held_key=None, repoint=True)

    assert again is first and built == [first]
    assert built[0].workspaces == [
        ("e2b_team_b", "factorial-claude:1234"),
        (None, "factorial-claude:1234"),
    ]


def test_a_not_implemented_type_is_refused_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every type is built today, so the set is empty; the mechanism stays for the next type
    that reaches the vocabulary before its adapter does, and is exercised by filling it."""
    monkeypatch.setattr(
        runtime_adapters_module, "NOT_IMPLEMENTED_TYPES", frozenset({"claude_subscription"})
    )

    def build(row: Runtime, kind: str) -> Any:
        raise AssertionError("must not build a runtime the worker cannot run")

    adapters = RuntimeAdapters(build=build)
    with pytest.raises(RuntimeRefused, match=r"claude_subscription, which Factorial can't run yet"):
        adapters.for_runtime(_row("claude_subscription"))
    assert adapters.cached(7, "claude_cli") is None


@pytest.mark.parametrize(
    ("rt", "kind", "auth_mode"),
    [
        ("codex_api", "codex_cli", "api_key"),
        ("codex_subscription", "codex_cli", "subscription"),
        ("codex_bedrock", "codex_cli", "bedrock"),
        ("claude_api", "claude_cli", "api_key"),
        ("claude_subscription", "claude_cli", "subscription"),
    ],
)
def test_the_registry_build_constructs_each_type_on_its_family_kind(
    rt: str, kind: str, auth_mode: str
) -> None:
    """`create_adapter(registry_kind, **adapter_options(kind), auth_mode=...)`: the deployment's
    own per-kind options, then the row's auth mode on top — both adapters take the keyword, and
    the row's type is what decides it, never the deployment's default."""
    created: list[tuple[str, dict[str, Any]]] = []

    def create(kind_arg: str, **options: Any) -> Any:
        created.append((kind_arg, options))
        return FakeAdapter()

    build = registry_build(
        lambda kind_arg: {"binary": f"/bin/{kind_arg}", "auth_mode": "deployment-default"},
        create=create,
    )
    build(_row(rt), kind)

    ((built_kind, options),) = created
    assert built_kind == kind
    assert options["binary"] == f"/bin/{kind}"
    assert options["auth_mode"] == auth_mode


# ------------------------------------------------------------------------------ the worker


async def _codex_world(
    maker: async_sessionmaker[AsyncSession],
    *,
    rt: str = "codex_subscription",
    agent: Any = CODEX_AGENT,
) -> tuple[World, FakeAdapter, str]:
    slug = _runtime_slug()
    adapter = FakeAdapter()
    world = await _build_world(
        maker,
        resolved_agent=agent,
        config=_process_on_runtime(f"proc-{slug}", slug),
        runtimes={slug: (rt, adapter)},
    )
    return world, adapter, slug


async def _runtime_id(world: World, slug: str) -> int:
    async with world.sessions() as session:
        return (await session.execute(select(Runtime.id).where(Runtime.slug == slug))).scalar_one()


async def _spawned(world: World) -> dict[str, Any]:
    (event,) = [e for e in await event_rows(world) if e.type == EventType.AGENT_SPAWNED]
    return dict(event.details)


@postgres
async def test_a_runtime_slug_spawns_on_that_runtimes_adapter(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The row is what the snapshot's slug resolves to: its adapter spawns, the run is stamped
    with its id, and the spawn event names the slug and type beside the executing kind."""
    world, adapter, slug = await _codex_world(maker)

    await run_tick(world)

    assert len(adapter.spawns) == 1
    assert world.adapter.spawns == []
    (run,) = await step_runs(world)
    assert run.runtime_id == await _runtime_id(world, slug)
    assert run.runtime_kind == "codex"
    spawned = await _spawned(world)
    assert spawned["runtime"] == "codex_cli"
    assert spawned["runtime_slug"] == slug
    assert spawned["runtime_type"] == "codex_subscription"


@postgres
async def test_poll_interrupt_and_resume_follow_the_run_to_its_runtime(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every post-spawn verb is keyed off the persisted `step_run.runtime_id`, never the
    declared-kind route the same manifest would otherwise take."""
    world, adapter, _slug = await _codex_world(maker)
    await run_tick(world)  # spawn
    (run,) = await step_runs(world)

    await run_tick(world)  # poll
    assert adapter.status_calls == [run.runtime_run_id]
    assert world.adapter.status_calls == []

    async with world.sessions() as session:
        await append(
            session,
            type=EventType.INTERRUPT_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            requested_via="dashboard",
        )
    await run_tick(world)  # interrupt
    assert adapter.interrupted == [run.runtime_run_id]
    assert world.adapter.interrupted == []
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED

    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels - {LABEL_INTERRUPTED}
    )
    await run_tick(world)  # resume
    assert [rid for rid, _turn in adapter.resumed] == [run.runtime_run_id]
    assert world.adapter.resumed == []
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.RUNNING


@postgres
async def test_an_exit_cancels_the_run_on_its_runtime(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter, _slug = await _codex_world(maker)
    await run_tick(world)
    (run,) = await step_runs(world)

    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels | {LABEL_EXIT}
    )
    await run_tick(world)

    assert adapter.cancelled == [run.runtime_run_id]
    assert world.adapter.cancelled == []
    (row,) = await step_runs(world)
    assert row.status == StepRunStatus.CANCELLED


@postgres
async def test_a_cold_cache_rebuilds_from_the_persisted_runtime_id(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A worker restart empties the cache; the next poll loads the row by the run's own
    `runtime_id` and builds the same runtime's adapter — the run does not fall back to the
    declared-kind route, whose substrate has never heard of its run id."""
    world, adapter, slug = await _codex_world(maker)
    await run_tick(world)
    (run,) = await step_runs(world)

    rebuilt: list[str] = []

    def build(row: Runtime, kind: str) -> Any:
        rebuilt.append(row.slug)
        return adapter

    world.deps = replace(world.deps, runtime_adapters=RuntimeAdapters(build=build))
    await run_tick(world)

    assert rebuilt == [slug]
    assert adapter.status_calls == [run.runtime_run_id]
    assert world.adapter.status_calls == []


async def _assert_refused_permanently(world: World, adapter: FakeAdapter, *needles: str) -> None:
    """One FAILED row, escalated on the tick that refused it, nothing spawned anywhere, and no
    retry owed — the second tick, past the backoff, starts nothing."""
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.runtime_run_id is None
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("misconfiguration: ")
    for needle in needles:
        assert needle in run.failure_reason, (needle, run.failure_reason)
    assert adapter.spawns == []
    assert world.adapter.spawns == []
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    types = await event_types(world)
    assert EventType.ESCALATED in types
    assert EventType.RETRY_SCHEDULED not in types
    (escalated,) = [e for e in await event_rows(world) if e.type == EventType.ESCALATED]
    assert escalated.details["permanent"] is True

    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    assert len(await step_runs(world)) == 1


@postgres
async def test_a_slug_with_no_runtime_row_is_permanent(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The snapshot names a runtime an operator never created (or has since deleted). A retry
    re-reads the same pinned snapshot, so it can only delay the escalation."""
    slug = _runtime_slug()
    adapter = FakeAdapter()
    world = await _build_world(
        maker,
        resolved_agent=CODEX_AGENT,
        config=_process_on_runtime(f"proc-{slug}", slug),
        runtimes={},
    )

    await run_tick(world)

    await _assert_refused_permanently(world, adapter, slug, "which doesn't exist")


@postgres
async def test_a_not_implemented_type_is_permanent(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set is empty today (see the cache test above); filled here to keep the refusal's
    shape — permanent, escalated, nothing minted — under test for the next unbuilt type."""
    monkeypatch.setattr(
        runtime_adapters_module, "NOT_IMPLEMENTED_TYPES", frozenset({"claude_subscription"})
    )
    claude_manifest = AgentManifest.model_validate(
        MANIFEST.model_dump(mode="json", by_alias=True) | {"runtime": {"kind": "claude_managed"}}
    )
    world, adapter, slug = await _codex_world(
        maker, rt="claude_subscription", agent=replace(RESOLVED_AGENT, manifest=claude_manifest)
    )

    await run_tick(world)

    await _assert_refused_permanently(world, adapter, slug, "which Factorial can't run yet")
    assert world.minted == []


@postgres
async def test_runtime_credentials_ride_the_run_spec_and_win_over_project_secrets(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """The subscription bundle lives on the runtime under `CODEX_AUTH_JSON` (the key the adapter
    already reads). Every runtime credential is injected, and one sharing a name with a project
    secret wins: the runtime is the more specific owner of a credential shaped for its adapter.
    The minted `GH_TOKEN` is untouched — that channel stays the orchestrator's (S7)."""
    world, adapter, slug = await _codex_world(maker)
    runtime_id = await _runtime_id(world, slug)
    async with world.sessions() as session:
        await store.put(session, world.project_id, "PRODUCT_DOCS_TOKEN", "from-the-project")
        await store.put_for_runtime(session, runtime_id, "CODEX_AUTH_JSON", '{"tokens": 1}')
        await store.put_for_runtime(session, runtime_id, "PRODUCT_DOCS_TOKEN", "from-the-runtime")
        await store.put_for_runtime(session, runtime_id, "E2B_API_KEY", "e2b_never_a_run_secret")

    await run_tick(world)

    (spec,) = adapter.spawns
    secrets = {name: value.get_secret_value() for name, value in spec.secrets.items()}
    assert secrets["CODEX_AUTH_JSON"] == '{"tokens": 1}'
    assert secrets["PRODUCT_DOCS_TOKEN"] == "from-the-runtime"
    assert secrets["GH_TOKEN"] == "ghs_faketoken"
    # The E2B key is the adapter's client credential; a run's secrets are rendered for the agent.
    assert "e2b_never_a_run_secret" not in secrets.values()


@postgres
async def test_a_resume_carries_the_runtime_credentials_too(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """A resume mints a fresh `GH_TOKEN` because the old one may have expired while the ticket
    sat parked; the runtime's own credentials are re-read for the same reason."""
    world, adapter, slug = await _codex_world(maker)
    runtime_id = await _runtime_id(world, slug)
    async with world.sessions() as session:
        await store.put_for_runtime(session, runtime_id, "CODEX_AUTH_JSON", '{"tokens": 2}')
    await run_tick(world)
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

    await run_tick(world)

    assert adapter.resumed_secrets["CODEX_AUTH_JSON"].get_secret_value() == '{"tokens": 2}'


CODEX_KEY_MANIFEST = AgentManifest.model_validate(
    CODEX_MANIFEST.model_dump(mode="json", by_alias=True)
    | {"secrets": [{"key": "OPENAI_API_KEY"}, {"key": "GH_TOKEN"}]}
)
CODEX_KEY_AGENT = replace(RESOLVED_AGENT, manifest=CODEX_KEY_MANIFEST)


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


@postgres
async def test_a_bound_runtime_is_the_sole_owner_of_its_vendor_key(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """When the process names a runtime, that runtime's credential is the only source of the
    vendor key its type names: a project secret of the same name is not even read, however the
    manifest declares it (decision 36). Every other declared key still comes from the project."""
    world, adapter, slug = await _codex_world(maker, rt="codex_api", agent=CODEX_KEY_AGENT)
    runtime_id = await _runtime_id(world, slug)
    async with world.sessions() as session:
        await store.put(session, world.project_id, "OPENAI_API_KEY", "project-copy")
        await store.put(session, world.project_id, "PRODUCT_DOCS_TOKEN", "from-the-project")
        await store.put_for_runtime(session, runtime_id, "OPENAI_API_KEY", "runtime-copy")

    await run_tick(world)

    (spec,) = adapter.spawns
    secrets = {name: value.get_secret_value() for name, value in spec.secrets.items()}
    assert secrets["OPENAI_API_KEY"] == "runtime-copy"
    assert secrets["PRODUCT_DOCS_TOKEN"] == "from-the-project"


@postgres
async def test_a_bound_runtime_without_its_key_leaves_the_project_copy_unread(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """The project's copy is not a fallback: with the runtime holding nothing, the key is absent
    from the spawn AND from a later resume — both go through `run_secrets`, and the adapter's
    refusal is what tells the operator to store it on the Runtime page."""
    world, adapter, _ = await _codex_world(maker, rt="codex_api", agent=CODEX_KEY_AGENT)
    async with world.sessions() as session:
        await store.put(session, world.project_id, "OPENAI_API_KEY", "project-copy")

    await run_tick(world)

    (spec,) = adapter.spawns
    assert "OPENAI_API_KEY" not in spec.secrets
    assert "GH_TOKEN" in spec.secrets

    await _interrupt_then_release(world)
    await run_tick(world)

    assert "OPENAI_API_KEY" not in adapter.resumed_secrets
    assert "GH_TOKEN" in adapter.resumed_secrets


@postgres
async def test_a_process_naming_no_runtime_still_reads_the_project_store(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    """The default path, byte for byte: no runtime row, so nothing is subtracted."""
    world = await _build_world(maker, resolved_agent=CODEX_KEY_AGENT)
    async with world.sessions() as session:
        await store.put(session, world.project_id, "OPENAI_API_KEY", "project-copy")

    await run_tick(world)

    (spec,) = world.adapter.spawns
    assert spec.secrets["OPENAI_API_KEY"].get_secret_value() == "project-copy"


@postgres
# --------------------------------------------------------------------------- E2B isolation


async def _on_e2b(
    world: World, slug: str, *, permit: bool, egress: object = None, template: str = ""
) -> None:
    """Move the runtime into a sandbox, and set what the project's operator permits."""
    async with world.sessions() as session:
        row = (await session.execute(select(Runtime).where(Runtime.slug == slug))).scalar_one()
        row.details = {"isolation": "e2b", **({"template": template} if template else {})}
        project = await session.get(Project, world.project_id)
        assert project is not None
        settings = dict(project.settings)
        if permit:
            settings["runtime_allowlist"] = [slug]
        if egress is not None:
            settings["egress_allowlist"] = egress
        project.settings = settings
        await session.commit()


@postgres
async def test_an_e2b_runtime_the_project_does_not_permit_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The repo selects, the operator permits: a `.factorial/` commit alone cannot send a
    project's code to a third party."""
    world, adapter, slug = await _codex_world(maker, rt="codex_api")
    await _on_e2b(world, slug, permit=False)

    await run_tick(world)

    await _assert_refused_permanently(world, adapter, slug, "permit it under Permits")


@postgres
async def test_a_permitted_e2b_runtime_spawns_on_its_sandbox_kind_with_the_projects_egress(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    pin = "factorial-codex-0-159-0:0f1e2d3c-0000-4000-8000-000000000001"
    world, adapter, slug = await _codex_world(maker, rt="codex_api")
    await _on_e2b(world, slug, permit=True, egress=["registry.npmjs.org"], template=pin)

    await run_tick(world)

    (spec,) = adapter.spawns
    assert spec.egress == ("registry.npmjs.org",)
    spawned = await _spawned(world)
    assert spawned["runtime"] == "codex_e2b"
    assert spawned["template"] == pin, "the build, and so the CLI version, this run booted"
    assert spawned["cli_pin"] == pin


@postgres
async def test_an_e2b_key_that_does_not_decrypt_fails_the_attempt_permanently(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read before the attempt's row exists, the key's failure must still fail that attempt and
    escalate: raised there, every tick would roll back and the ticket loop to quarantine."""

    async def undecryptable(*_: object) -> SecretStr:
        raise DecryptionError("the blob did not authenticate")

    monkeypatch.setattr(agent_steps_module, "workspace_key", undecryptable)
    world, adapter, slug = await _codex_world(maker, rt="codex_api")
    await _on_e2b(world, slug, permit=True, template="factorial-codex-0-159-0:pin")

    await run_tick(world)

    await _assert_refused_permanently(world, adapter, "did not authenticate")


@postgres
async def test_a_malformed_egress_allowlist_is_refused_not_ignored(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter, slug = await _codex_world(maker, rt="codex_api")
    await _on_e2b(world, slug, permit=True, egress=["https://registry.npmjs.org/"])

    await run_tick(world)

    await _assert_refused_permanently(world, adapter, "egress_allowlist")


RAILWAY_MANIFEST = AgentManifest.model_validate(
    CODEX_MANIFEST.model_dump(mode="json", by_alias=True)
    | {
        "secrets": [
            {"key": "GH_TOKEN"},
            {"key": "OPENAI_API_KEY"},
            {"key": "RAILWAY_TOKEN_X"},
            {"key": "DB_PASSWORD"},
            {"key": "GITLAB_TOKEN"},
        ]
    }
)
RAILWAY_ROUTE = SecretRoute(
    name="RAILWAY_TOKEN_X", host="backboard.railway.com", header="project-access-token"
)


async def _delivering_world(
    maker: async_sessionmaker[AsyncSession], *, sandbox: bool
) -> tuple[World, FakeAdapter]:
    """A routed key, an exposed key, and three keys that are never the project's to deliver: a
    GitLab run's `GITLAB_TOKEN` is the minted project token, like `GH_TOKEN`."""
    slug = _runtime_slug()
    adapter = FakeAdapter()
    world = await _build_world(
        maker,
        resolved_agent=replace(RESOLVED_AGENT, manifest=RAILWAY_MANIFEST),
        config=_process_on_runtime(f"proc-{slug}", slug),
        runtimes={slug: ("codex_api", adapter)},
    )
    if sandbox:
        await _on_e2b(world, slug, permit=True, egress=["backboard.railway.com"])
    header = store.Delivery(
        mode="header", host=RAILWAY_ROUTE.host, header=RAILWAY_ROUTE.header
    )
    async with world.sessions() as session:
        await store.put(session, world.project_id, "RAILWAY_TOKEN_X", "rw-value", header)
        # No choice: a secret is an environment variable unless the operator chose a header.
        await store.put(session, world.project_id, "DB_PASSWORD", "db-value")
        await store.put(session, world.project_id, "GH_TOKEN", "stale")
        await store.put(session, world.project_id, "GITLAB_TOKEN", "pat")
        await store.put(session, world.project_id, "OPENAI_API_KEY", "copy")
    return world, adapter


@postgres
async def test_a_sandbox_run_carries_each_project_secrets_chosen_delivery(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world, adapter = await _delivering_world(maker, sandbox=True)

    await run_tick(world)

    (spec,) = adapter.spawns
    assert spec.secret_routes == (RAILWAY_ROUTE,)
    assert spec.secret_env == ("DB_PASSWORD",), "never the minted token or the runtime's key"


@postgres
async def test_a_host_run_carries_no_sandbox_delivery(
    maker: async_sessionmaker[AsyncSession], master_key: None
) -> None:
    world, adapter = await _delivering_world(maker, sandbox=False)

    await run_tick(world)

    (spec,) = adapter.spawns
    assert spec.secret_routes == () and spec.secret_env == ()
    assert spec.secrets["DB_PASSWORD"].get_secret_value() == "db-value"


# ----------------------------------------------------------------- interactive sessions


@postgres
@pytest.mark.parametrize("terminal_enabled", [False, True], ids=["switch-off", "switch-on"])
async def test_every_spawn_is_a_session_whatever_the_terminal_switch(
    maker: async_sessionmaker[AsyncSession], terminal_enabled: bool
) -> None:
    """The terminal switch cannot start a headless run: it gates only a browser's attach, typing
    and reopen."""
    slug = _runtime_slug()
    adapter = FakeAdapter()
    world = await _build_world(
        maker,
        config=_process_on_runtime(f"proc-{slug}", slug),
        runtimes={slug: ("claude_api", adapter)},
        sessions=SessionPolicy(terminal_enabled=terminal_enabled),
    )

    await run_tick(world)

    assert "terminal" in await _spawned(world)


@postgres
async def test_a_spawn_says_when_its_token_was_minted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An adapter never mints, so a rollover reads the token's age from here."""
    world, adapter = await interactive_world(maker)

    await run_tick(world)

    spawned = await _spawned(world)
    assert spawned["minted_at"] == world.clock.now.isoformat()
    assert spawned["cli_pin"] == adapter.pinned_version


#: A full sentence, as the adapters now word their refusals: the comment ends it only once.
_DRIFT = "claude reported 2.1.290; the pin is 2.1.284: install the pin on the worker's host."


async def _refused(maker: async_sessionmaker[AsyncSession]) -> tuple[World, FakeAdapter]:
    world, adapter = await interactive_world(maker)
    adapter.refusal = _DRIFT
    await run_tick(world)
    return world, adapter


def _comments(world: World) -> list[str]:
    return [body for kind, body in world.projector.calls if kind == "comment"]


async def _refusals(world: World) -> list[dict[str, Any]]:
    return [dict(e.details) for e in await event_rows(world) if e.type == EventType.SPAWN_REFUSED]


@postgres
async def test_a_refused_spawn_blocks_with_its_fix_and_spends_no_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """CLI drift after an image rebuild refuses every spawn on the host: a refusal that spent an
    attempt would escalate every ticket for good over a fix the operator makes once."""
    world, _ = await _refused(maker)

    assert await step_runs(world) == []
    assert world.minted == []
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert LABEL_BLOCKED in world.github.issue.labels
    (refused,) = await _refusals(world)
    assert refused == {"step": "plan", "runtime": refused["runtime"], "refusal": _DRIFT}
    assert refused["runtime"].startswith("rt-")
    (comment,) = _comments(world)
    assert f"{_DRIFT.removesuffix('.')}. Factorial checks again" in comment
    assert ".." not in comment
    assert "every 5 minutes" in comment
    assert EventType.ESCALATED not in await event_types(world)


@postgres
async def test_a_refused_spawn_is_asked_again_only_every_parked_recheck(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A parked ticket ticks every 30 seconds, and on E2B each ask is a read of the vendor's
    API with the runtime's key."""
    world, adapter = await _refused(maker)
    assert adapter.refusal_asks == 1

    await run_tick(world)
    assert adapter.refusal_asks == 1
    world.clock.advance(world.deps.sessions.parked_recheck - timedelta(seconds=1))
    await run_tick(world)
    assert adapter.refusal_asks == 1
    world.clock.advance(timedelta(seconds=1))
    await run_tick(world)
    assert adapter.refusal_asks == 2

    assert await step_runs(world) == []
    assert len(_comments(world)) == 1
    assert len(await _refusals(world)) == 1
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


@postgres
async def test_a_cleared_refusal_continues_the_ticket_by_itself_on_attempt_one(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The memo of the last ask is process memory, so a worker restart reads the log: a ticket
    whose block is a refused spawn is asked again at once."""
    world, adapter = await _refused(maker)
    adapter.refusal = None
    world.deps.spawn_rechecks.clear()

    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    assert _comments(world)[-1] == "The runtime can run a session now: starting `plan` (attempt 1)."
    await run_tick(world)

    assert len(adapter.spawns) == 1
    (run,) = await step_runs(world)
    assert run.attempt == 1


@postgres
async def test_a_refusal_whose_runtime_is_gone_stays_blocked_until_a_person_releases_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing was asked, so nothing says the runtime can run a session: the release is a
    person's, and the attempt it grants fails permanently saying why."""
    world, adapter = await _refused(maker)
    slug = (await _refusals(world))[0]["runtime"]
    async with world.sessions() as session:
        row = (await session.execute(select(Runtime).where(Runtime.slug == slug))).scalar_one()
        await session.delete(row)
        await session.commit()

    world.clock.advance(world.deps.sessions.parked_recheck)
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    assert len(_comments(world)) == 1
    assert EventType.UNBLOCKED not in await event_types(world)

    remove_label(world, LABEL_BLOCKED)
    await run_tick(world)
    await run_tick(world)
    await _assert_refused_permanently(world, adapter, slug, "which doesn't exist")


@postgres
async def test_unblocking_a_refused_spawn_asks_again_at_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await _refused(maker)
    adapter.refusal = None

    remove_label(world, LABEL_BLOCKED)
    await run_tick(world)
    await run_tick(world)

    assert len(adapter.spawns) == 1


@postgres
async def test_an_e2b_outage_while_checking_the_verdict_is_retried_not_escalated(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world, adapter = await interactive_world(maker)
    adapter.refusal_error = AdapterUnavailable("E2B did not answer")

    with pytest.raises(AdapterUnavailable):
        await run_tick(world)

    assert await step_runs(world) == []
    assert world.minted == []
    assert (await ticket_row(world)).status != TicketStatus.BLOCKED

    adapter.refusal_error = None
    await run_tick(world)

    assert len(adapter.spawns) == 1
