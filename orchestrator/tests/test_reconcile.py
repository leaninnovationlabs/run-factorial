"""The reconciliation loop, driven end to end against real Postgres.

Everything except GitHub and the agent substrate is real: the pinned `ProcessConfig`, the closed
predicate library, the event writer, the unique constraint that makes spawns idempotent, and the
lease. GitHub is a mutable `FakeGitHub` that hands out `SnapshotState` snapshots and absorbs the
projector's writes — so "reconcile twice with unchanged observed state" means what it says: the
second tick observes the first tick's projection, exactly as the next real poll would.

The clock is injected (`Clock`); no test sleeps.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    Event,
    Project,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events.read_models import cost_per_ticket, factory_overview, spend_by_ticket
from orchestrator.github import IssueState
from orchestrator.settings import Settings, WorkerSettings
from orchestrator.worker import admission, leases
from orchestrator.worker import agent_steps as agent_steps_module
from orchestrator.worker import reconcile as reconcile_module
from orchestrator.worker.admission import (
    InMemoryCandidates,
    admit_all,
    admit_gitlab_all,
    admit_issue,
    admit_project,
)
from orchestrator.worker.loop import _sweep_run_credentials, run_forever, tick
from orchestrator.worker.reconcile import reconcile_ticket
from orchestrator.worker.tick_context import LeaseLost
from runtime.contract import (
    Artifact,
    ArtifactKind,
    RunStatus,
)
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    MANIFEST,
    RESOLVED_AGENT,
    World,
    _build_world,
    _ensure_leased,
    _fail_observation,
    _FakeRequestFailed,
    _grant_lease,
    _legacy_process,
    _process,
    _process_with_a_branch_guard,
    _process_with_gate,
    _remove_blocked_label,
    advance_to_gate,
    entered_events,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# --------------------------------------------------------------------------- leases


async def test_lease_is_exclusive(maker: async_sessionmaker[AsyncSession]) -> None:
    """Two runners never get the same ticket; an expired lease is reclaimable; renew/release by
    a non-holder write nothing (worker/leases.py)."""
    world = await _build_world(maker)
    ttl = timedelta(seconds=300)
    # Every leasable ticket, not a page: the database outlives each test, the claim takes the
    # oldest first, and this world's ticket is the newest.
    every = 1_000_000

    first = maker()
    second = maker()
    try:
        mine = [t.id for t in await leases.acquire_leases(first, "runner-a", every, ttl)]
        assert world.ticket_id in mine

        # `first` has not committed: SKIP LOCKED must make the row invisible, not block.
        theirs = [t.id for t in await leases.acquire_leases(second, "runner-b", every, ttl)]
        assert world.ticket_id not in theirs
        await first.commit()
        await second.commit()

        # Still leased and unexpired -> still not available to anyone else.
        again = [t.id for t in await leases.acquire_leases(second, "runner-b", every, ttl)]
        assert world.ticket_id not in again
        await second.commit()

        # A non-holder may neither extend nor drop someone else's lease.
        assert await leases.renew(second, world.ticket_id, "runner-b", ttl) is False
        assert await leases.release(second, world.ticket_id, "runner-b") is False
        await second.commit()

        assert await leases.renew(first, world.ticket_id, "runner-a", ttl) is True
        await first.commit()

        # An expired lease is reclaimable — and its old owner may no longer write.
        await second.execute(
            update(Ticket)
            .where(Ticket.id == world.ticket_id)
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
        await second.commit()
        assert await leases.renew(second, world.ticket_id, "runner-a", ttl) is False
        assert await leases.release(second, world.ticket_id, "runner-a") is False
        reclaimed = [t.id for t in await leases.acquire_leases(second, "runner-b", every, ttl)]
        assert world.ticket_id in reclaimed
        await second.commit()

        assert await leases.release(second, world.ticket_id, "runner-b") is True
        await second.commit()
    finally:
        await first.close()
        await second.close()


async def test_tick_reconciles_and_releases_the_lease(world: World) -> None:
    reconciled = await tick(
        world.deps,
        sessions=world.sessions,
        owner="runner-tick",
        limit=50,
        ttl=timedelta(seconds=60),
    )
    assert reconciled >= 1
    ticket = await ticket_row(world)
    assert ticket.lease_owner is None
    assert [run.step_name for run in await step_runs(world)] == ["plan"]


async def test_tick_drains_every_reconcilable_ticket_not_just_the_first_batch(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`acquire_leases` hands back `limit` rows in id order and the tick releases them at its end,
    so a tick that claims once serves the same lowest ids every time and whatever sits past the
    cap never gets a tick at all. Three fresh tickets put the newest past a batch of two."""
    older = [await _build_world(maker) for _ in range(2)]
    newest = await _build_world(maker)

    reconciled = await tick(
        newest.deps,
        sessions=newest.sessions,
        owner="runner-drain",
        limit=2,
        ttl=timedelta(seconds=60),
    )

    assert reconciled >= 3
    assert [run.step_name for run in await step_runs(newest)] == ["plan"]
    for world in older:
        assert (await ticket_row(world)).lease_owner is None


async def test_run_forever_ticks_on_the_interval_and_stops_cleanly(
    world: World, migrated_database: str
) -> None:
    """The interval tick is the guarantee; NOTIFY only shortens it (tech-stack decision 4).

    A `LISTEN` connection is opened against the real database here, so the degrade-to-interval
    path is exercised for real rather than mocked away — and shutdown must not leave it behind.
    """
    settings = Settings(
        database_url=migrated_database,
        worker=WorkerSettings(
            poll_interval_seconds=0.05, min_pass_gap_seconds=0, name="runner-forever", batch_size=50
        ),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            world.deps,
            sessions=world.sessions,
            settings=settings,
            stop=stop,
            install_signal_handlers=False,
        )
    )
    try:
        for _ in range(100):  # bounded poll instead of a sleep-and-hope
            await asyncio.sleep(0.05)
            if await step_runs(world):
                break
        assert [run.step_name for run in await step_runs(world)] == ["plan"]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    assert (await ticket_row(world)).lease_owner is None


# --------------------------------------------------------------------------- spawning


async def test_spawn_is_idempotent_under_concurrency(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UNIQUE (ticket_id, step_name, attempt)` arbitrates, not the loop (design.md §3.1).

    Both runners choose attempt 1 before either inserts: one that chose after the other committed
    would insert attempt 2, which the constraint never sees, and the scheduler alone does not
    rule that out."""
    both_chose = asyncio.Barrier(2)
    next_attempt = agent_steps_module._next_attempt

    async def choose_then_wait(*args: Any, **kwargs: Any) -> int:
        attempt = await next_attempt(*args, **kwargs)
        await both_chose.wait()
        return attempt

    monkeypatch.setattr(agent_steps_module, "_next_attempt", choose_then_wait)

    async def one() -> None:
        async with world.sessions() as session:
            ticket = await session.get(Ticket, world.ticket_id)
            assert ticket is not None
            await reconcile_ticket(
                session, ticket, world.deps, lease_owner=_LEASE_OWNER, lease_ttl=_LEASE_TTL
            )

    # Granted once, up front: both racing calls renew the SAME lease (decision 41) rather than each
    # trying to acquire it, which is not what this test means to race.
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _ensure_leased(session, ticket)

    async with asyncio.timeout(30):
        await asyncio.gather(one(), one())

    runs = await step_runs(world)
    assert len(runs) == 1
    types = await event_types(world)
    assert types.count("agent_spawned") == 1
    assert types.count("step_started") == 1


# ---------------------------------------------------------------------- lease renewal (decision 41)


async def test_a_spawn_tick_renews_the_lease_before_spawning(world: World) -> None:
    """`_start_attempt` renews immediately before `adapter.spawn` (decision 41) — proven by granting
    a much shorter initial lease and checking it comes out the other side extended to the full
    configured ttl, not left at its original, soon-to-expire horizon."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _grant_lease(session, ticket, owner=_LEASE_OWNER, ttl=timedelta(seconds=5))

    before = await ticket_row(world)
    assert before.lease_expires_at is not None

    await run_tick(world)

    after = await ticket_row(world)
    assert after.lease_expires_at is not None
    # Renewed out to (about) `_LEASE_TTL` (300s) from "now" — far past the original 5s horizon,
    # so this can only be the renewal, not a leftover from the initial grant.
    assert after.lease_expires_at > before.lease_expires_at + timedelta(seconds=60)
    assert world.adapter.spawns, "renewal must not have blocked the spawn it exists to cover"


async def test_the_renew_ttl_is_the_configured_lease_seconds(world: World) -> None:
    """The ttl `_start_attempt` renews with is whatever the caller threaded through as
    `lease_ttl` — here, `run_tick`'s `_LEASE_TTL` (300s) — not a hardcoded value or one
    re-read from settings (settings.py's own default is 3600s).

    Granted with a much SHORTER initial ttl (5s) on purpose: the post-renewal expiry can then
    only be explained by an actual renewal using `_LEASE_TTL`. Asserting against a lease already
    granted at `_LEASE_TTL` would pass even with the renew call deleted entirely — measuring the
    initial grant instead of the renewal — which is exactly the gap this replaces."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _grant_lease(session, ticket, owner=_LEASE_OWNER, ttl=timedelta(seconds=5))

    # DB clock on both sides of the comparison — the one invariant leases.py states for itself.
    async with world.sessions() as session:
        before = (await session.execute(select(func.now()))).scalar_one()
    await run_tick(world)
    after = await ticket_row(world)

    assert after.lease_expires_at is not None
    delta = after.lease_expires_at - before
    assert timedelta(seconds=250) < delta < timedelta(seconds=350)


async def test_a_failed_renewal_aborts_the_spawn_and_leaves_no_row(world: World) -> None:
    """A lease held by someone else (expired and reclaimed, or stolen) must never let this tick
    spawn against the ticket (decision 41: "never spawn on someone else's ticket"). The row `_start_
    attempt` created inside its `begin_nested()` SAVEPOINT before the renewal check is flushed,
    not committed — raising `LeaseLost` unwinds the whole tick transaction and erases it, so
    nothing survives for `_poll_run` to find with no `runtime_run_id` behind it."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        # Someone else's lease — `_LEASE_OWNER` (what `run_tick` renews as) does not match.
        await _grant_lease(session, ticket, owner="a-different-worker", ttl=_LEASE_TTL)

    with pytest.raises(LeaseLost):
        await run_tick(world)

    assert world.adapter.spawns == []
    assert await step_runs(world) == []
    assert await event_rows(world) == []


# --------------------------------------------------------------------------- events


async def test_the_spawn_event_records_which_servers_a_run_had(world: World) -> None:
    """Which tools a run had is answered from the event log, like everything else (§5)."""
    await run_tick(world)

    spawned = next(row for row in await event_rows(world) if row.type == "agent_spawned")
    assert spawned.details["mcp"] == ["product-docs"]


async def test_preconditions_not_met_is_not_an_event(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A step that is not ready has not done anything: no run, no event, no GitHub call."""
    world = await _build_world(maker, assignees=())  # `ticket_has_assignee()` is false
    await run_tick(world)
    assert await step_runs(world) == []
    assert await event_types(world) == []
    # The status label is still projected — the ticket is on `plan`, and that is observable state.
    assert world.projector.kinds() == ["set_labels"]


# --------------------------------------------------------------------------- acceptance


async def test_successful_run_with_failing_postconditions_does_not_advance(world: World) -> None:
    """The agent says it succeeded and the predicates disagree. The predicates decide
    (design.md §2.4) — the single most important behaviour in the repo."""
    await run_tick(world)  # spawn
    # The agent even claims artifacts. Nothing was actually pushed, so `branch_exists` is false.
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        summary="pushed the spec",
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
        cost_usd=Decimal("1.25"),
        input_tokens=100,
        output_tokens=200,
    )
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "plan"  # did NOT advance
    assert ticket.status == TicketStatus.RUNNING

    types = await event_types(world)
    assert types == [
        "step_started",
        "agent_spawned",
        "agent_completed",
        "postcondition_failed",
        "agent_failed",
        "retry_scheduled",
    ]
    failed = next(row for row in await event_rows(world) if row.type == "postcondition_failed")
    assert any("branch_exists" in reason for reason in failed.details["failures"])
    # Beside the rule code, the same leaves in words, for the timeline and the issue.
    assert "a branch starting with spec/12-" in failed.details["needs"]
    agent_failed = next(row for row in await event_rows(world) if row.type == "agent_failed")
    # The reason stays the joined rule code `retry_briefing` compares against `failures`.
    assert agent_failed.details["failure_reason"] == "; ".join(failed.details["failures"])
    assert agent_failed.details["failure_summary"] == "Expected results missing: " + "; ".join(
        failed.details["needs"]
    )

    run = (await step_runs(world))[0]
    assert run.status == StepRunStatus.FAILED
    assert run.cost_usd == Decimal("1.25")  # per-run figures live on step_run, not on artifacts
    assert run.input_tokens == 100

    # ... and the retry happens once the backoff has elapsed.
    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)
    assert [(r.step_name, r.attempt) for r in await step_runs(world)] == [("plan", 1), ("plan", 2)]


# --------------------------------------------------------------------------- cost


async def test_a_codex_run_is_priced_at_list_and_labelled_an_estimate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Codex reports tokens and no dollar figure (decision 34). The worker prices the run from
    the model stamped on the row at spawn — the substrate's report never names it — and says
    so with `cost_source`, so a reader can tell a bill from an estimate."""
    codex = replace(RESOLVED_AGENT, manifest=MANIFEST.model_copy(update={"model": "gpt-5.3-codex"}))
    world = await _build_world(maker, resolved_agent=codex)
    await run_tick(world)  # spawn
    run = (await step_runs(world))[0]
    assert run.model == "gpt-5.3-codex"
    spawned = next(row for row in await event_rows(world) if row.type == "agent_spawned")
    assert spawned.details["model"] == "gpt-5.3-codex"

    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        cache_write_input_tokens=0,
        output_tokens=100_000,
    )
    await run_tick(world)

    run = (await step_runs(world))[0]
    # 600k x $1.75 + 400k x $0.175 + 100k x $14.00 per million (test_pricing.py).
    assert run.cost_usd == Decimal("2.52")
    assert run.cost_source == "estimated"
    assert run.cached_input_tokens == 400_000
    completed = next(row for row in await event_rows(world) if row.type == "agent_completed")
    assert completed.details["cost_usd"] == "2.5200"
    assert completed.details["cost_source"] == "estimated"
    assert completed.details["cached_input_tokens"] == 400_000


async def test_a_reported_figure_is_kept_and_labelled_reported(world: World) -> None:
    """Claude's CLI measures its own figure; the worker never re-prices one it was given."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
        cost_usd=Decimal("1.25"),
        input_tokens=100,
        cached_input_tokens=60,
        output_tokens=200,
    )
    await run_tick(world)

    run = (await step_runs(world))[0]
    assert run.model == MANIFEST.model
    assert run.cost_usd == Decimal("1.25")
    assert run.cost_source == "reported"
    assert run.cached_input_tokens == 60
    completed = next(row for row in await event_rows(world) if row.type == "agent_completed")
    assert completed.details["cost_source"] == "reported"


async def test_an_oversized_report_is_held_to_its_columns(world: World) -> None:
    """A session's figures come off a log the agent writes. One a column refuses fails the end's
    write on every tick, and the ticket never moves again."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
        cost_usd=Decimal("12345678.9"),
        input_tokens=2**64,
        cached_input_tokens=2**64,
        output_tokens=2**64,
    )
    await run_tick(world)

    run = (await step_runs(world))[0]
    assert run.status == "succeeded"
    assert (run.cost_usd, run.cost_source) == (Decimal("999999.9999"), "reported")
    assert run.input_tokens == run.cached_input_tokens == run.output_tokens == 2**63 - 1

    # Two columns held at BIGINT's maximum overflow a BIGINT sum, and the factory board, cost per
    # issue and the dashboard would fail for every project while this run is in their window.
    async with world.sessions() as session:
        rows = await factory_overview(session, 7, await default_organization_id(session))
        [board] = [r for r in rows if r.ticket_id == world.ticket_id]
        [costs] = await cost_per_ticket(session, world.project_id)
        [spend] = (await spend_by_ticket(session, world.project_id, since=None, limit=5)).top
    assert board.tokens == costs.tokens == spend.tokens == 2 * (2**63 - 1)


async def test_an_unpriced_model_with_no_report_leaves_cost_empty(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Neither measured nor in the table: both columns stay NULL rather than a zero that
    would read as a free run."""
    unpriced = replace(RESOLVED_AGENT, manifest=MANIFEST.model_copy(update={"model": "gpt-4o"}))
    world = await _build_world(maker, resolved_agent=unpriced)
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
        input_tokens=100,
        output_tokens=200,
    )
    await run_tick(world)

    run = (await step_runs(world))[0]
    assert run.cost_usd is None
    assert run.cost_source is None
    completed = next(row for row in await event_rows(world) if row.type == "agent_completed")
    assert completed.details["cost_usd"] is None
    assert completed.details["cost_source"] is None


async def test_agent_completed_records_the_branch_the_run_actually_delivered(
    world: World,
) -> None:
    """The spawn event records the branch DIRECTIVE, and on the attempt that creates the branch
    that is still `spec/{ticket}-{slug}` — nothing in the timeline named the concrete branch the
    agent chose. The completion does, off the artifacts the run reported."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        summary="done",
        artifacts=(
            Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),
            Artifact(kind=ArtifactKind.PR, locator={"number": "101", "head": "spec/12-widget"}),
        ),
    )
    await run_tick(world)

    rows = {row.type: row for row in await event_rows(world)}
    assert rows["agent_spawned"].details["branch"] == "spec/12-{slug}"
    assert rows["agent_completed"].details["branch"] == "spec/12-widget"


async def test_agent_completed_names_no_branch_when_the_run_delivered_two(world: World) -> None:
    """Same fail-safe as `{slug}` resolution: a run that reported two names has not said which
    branch it delivered, and the timeline says nothing rather than picking one."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        summary="done",
        artifacts=(
            Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),
            Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-gadget"}),
        ),
    )
    await run_tick(world)

    rows = {row.type: row for row in await event_rows(world)}
    assert rows["agent_completed"].details["branch"] is None


async def test_postconditions_hold_advances_and_projects_on_success(world: World) -> None:
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, summary="done")
    world.projector.calls.clear()
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_gate"
    types = await event_types(world)
    assert types[-3:] == ["agent_completed", "step_completed", "step_entered"]

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:spec-review"})
    assert calls["set_assignees"] == ("reviewer-1",)
    assert calls["comment"] == "Spec ready for review: https://github.test/pr/101"


async def test_a_snapshot_pinned_before_the_rename_still_drives_a_ticket(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Issue #138: an `adlc/v1` snapshot labelled `adlc:` is read as `factorial:`, and the
    projection writes the new spelling."""
    world = await _build_world(
        maker,
        labels=frozenset({"adlc:new"}),
        config=_legacy_process(f"legacy-{uuid4().hex[:8]}"),
    )
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, summary="done")
    world.projector.calls.clear()
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "spec_gate"
    assert dict(world.projector.calls)["set_labels"] == frozenset({"factorial:spec-review"})


async def test_advancing_records_the_step_being_entered(world: World) -> None:
    """The page draws its graph from `current_step` and its timeline from the event log. Without
    this row the graph names a step the log has not mentioned until that step's first run row
    exists — which a substrate with no free slot delays by minutes."""
    await run_tick(world)
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, summary="done")
    await run_tick(world)

    assert (await ticket_row(world)).current_step == "spec_gate"
    # Nothing has run at the entered step: the entry IS the whole record of the move.
    assert [r.step_name for r in await step_runs(world)] == ["plan"]
    assert (await entered_events(world))[-1] == {
        "step": "spec_gate",
        "from_step": "plan",
        "reason": "next",
        "attempt": 1,
        "kind": "human_gate",
    }


async def test_the_entered_attempt_is_the_one_the_run_then_takes(world: World) -> None:
    """`step_entered.attempt` is stamped before any row exists, so it is a prediction. It has to
    match the run that follows, or the ticket page files the two in separate `step#attempt`
    blocks and one attempt reads as two."""
    await advance_to_gate(world)
    entry = next(d for d in await entered_events(world) if d["step"] == "spec_gate")
    gate = next(r for r in await step_runs(world) if r.step_name == "spec_gate")
    assert entry["attempt"] == gate.attempt


async def test_a_human_label_survives_projection(maker: async_sessionmaker[AsyncSession]) -> None:
    """A human's labels are not the factory's; observing one is not a licence to delete it
    (§2.2)."""
    world = await _build_world(maker, labels=frozenset({"factorial:new", "needs-triage"}))
    await run_tick(world)
    assert world.projector.calls[0] == ("set_labels", frozenset({"needs-triage", "factorial:plan"}))


# --------------------------------------------------------------------------- idempotence


async def test_unchanged_state_produces_no_events_and_no_projector_calls(world: World) -> None:
    """The property that makes a 30-second loop safe."""
    await run_tick(world)  # spawn; projects `factorial:plan`
    baseline_events = await event_types(world)
    baseline_calls = list(world.projector.calls)

    for _ in range(3):  # the run is still going; nothing observable changed
        await run_tick(world)

    assert await event_types(world) == baseline_events
    assert world.projector.calls == baseline_calls


# --------------------------------------------------------------------------- dry run


# ------------------------------------------------------------- projection survives predicate errors
#
# The live blackout these tests replay (shadow drive-log, 2026-08-21): the App was never granted
# `Checks: Read-only`, so `ci_green`'s check-runs read answered 403 on every tick — and because
# the raise aborted `reconcile_ticket` BEFORE `_project`, the ticket's entire label/comment
# intent stream went dark for days while the error repeated. Admission and events kept flowing:
# an outage that looks like silence. The reshape: the act phase runs in a SAVEPOINT; a failed
# EVALUATION read rolls back the half-made transition, logs loudly, and still projects the last
# persisted state.


def _capture_exception_logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Patched `logger.exception`, the module's own logging-assert idiom
    (`test_resolve_slug_with_two_distinct_candidates_is_unresolved_and_logs` has the caplog
    history) — the LOUDNESS half of the contract is that this record survives."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        reconcile_module.logger, "exception", lambda *args, **_kwargs: calls.append(args)
    )
    return calls


def _process_with_a_ci_postcondition(name: str) -> ProcessConfig:
    """`_process()` with `ci_green("spec/{ticket}-*")` appended LAST in `plan`'s postconditions,
    so `all` reaches it only after `branch_exists`/`pr_open` hold — the evaluation dies exactly
    where the live incident's did (`predicates.library.ci_green` -> `source.check_runs`)."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["postconditions"]["all"].append('ci_green("spec/{ticket}-*")')
    return ProcessConfig.model_validate(cfg)


async def test_a_predicate_error_no_longer_silences_projection(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE blackout regression, live path: the check-runs source raises mid-`_accept`, and the
    tick must (1) log the error, (2) still hand the projector the label set for the CURRENT
    persisted state, and (3) fabricate nothing — no completed step, no outcome, the run still
    RUNNING so the next tick re-polls. Pre-reshape, the raise propagated out of
    `reconcile_ticket` before `_project` and the projector received nothing at all."""
    world = await _build_world(
        maker, config=_process_with_a_ci_postcondition(f"ci-{uuid4().hex[:8]}")
    )
    await run_tick(world)  # plan spawns; the steady projection sets factorial:plan
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    _fail_observation(world, "check_runs")  # the 403 arrives
    # Drift the observed labels so the erroring tick HAS a correction to write — `_project`
    # writes only diffs, so an already-converged issue would prove nothing either way.
    world.github.issue = replace(world.github.issue, labels=frozenset({"factorial:new"}))
    errors = _capture_exception_logs(monkeypatch)
    world.projector.calls.clear()

    await run_tick(world)  # must NOT raise: the loop's isolation no longer absorbs this

    # LOUD: the log line is the record (a log, not an event — an infrastructure failure is not
    # a state transition, and POSTCONDITION_FAILED would assert a verdict nothing reached).
    assert any("evaluation failed" in args[0] for args in errors)
    # The projector still received the label set for the persisted state.
    assert ("set_labels", frozenset({"factorial:plan"})) in world.projector.calls
    # Nothing fabricated: the acceptance-in-progress was rolled back whole.
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert await event_types(world) == ["step_started", "agent_spawned"]
    ticket = await ticket_row(world)
    assert ticket.current_step == "plan"
    assert ticket.status == TicketStatus.RUNNING

    # An unreadable verdict is an outage, not a verdict: once the 403 clears, the SAME attempt
    # accepts — nothing was failed, retried, or lost meanwhile.
    world.use_github(state_source=world.github.snapshot)
    world.github.mark_ci_green("sha-1")
    errors.clear()
    await run_tick(world)
    assert errors == []
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.SUCCEEDED
    assert (await ticket_row(world)).current_step == "spec_gate"


@pytest.mark.parametrize("failing_read", ["pull_requests", "pr_review_verdict"])
async def test_a_predicate_error_during_gate_evaluation_still_projects(
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failing_read: str,
) -> None:
    """Gates evaluate through direct observed-state reads, not `evaluate()` — so the
    same 403 shape on the PR listing, or on the pick's review-verdict round trip, must behave
    identically: loud, no fabricated decision, and the waiting gate's label still projected."""
    world = await _build_world(maker)
    await advance_to_gate(world)
    _fail_observation(world, failing_read)
    world.github.issue = replace(world.github.issue, labels=frozenset({"factorial:new"}))
    errors = _capture_exception_logs(monkeypatch)
    before = await event_types(world)
    world.projector.calls.clear()

    await run_tick(world)

    assert any("evaluation failed" in args[0] for args in errors)
    assert ("set_labels", frozenset({"factorial:spec-review"})) in world.projector.calls
    assert await event_types(world) == before  # no approval/rejection was fabricated
    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_gate"
    assert ticket.status == TicketStatus.WAITING_GATE


async def test_a_failed_permission_lookup_keeps_the_gate_waiting_and_is_loud(
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that errors says nothing about the reviewer, so it must not read as "no access"
    or as access: the tick decides nothing, logs, and still projects. The next clean poll
    decides."""
    world = await _build_world(
        maker, config=_process_with_gate("proc", approvers=[], approverPermission="write")
    )
    await advance_to_gate(world)
    world.github.permissions["teammate"] = "write"
    world.github.approve("teammate")
    _fail_observation(world, "repo_permission")
    errors = _capture_exception_logs(monkeypatch)
    before = await event_types(world)

    await run_tick(world)

    assert any("evaluation failed" in args[0] for args in errors)
    assert await event_types(world) == before
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE

    world.use_github(state_source=world.github.snapshot)
    await run_tick(world)
    assert (await ticket_row(world)).current_step == "done"


def _process_with_a_full_waiting_outcome(name: str) -> ProcessConfig:
    """`_process()` with `spec_gate.waiting` declaring all three intents an outcome can carry
    (ticketLabel + comment + assign). The `pr_review_verdict` pin below needs a gate-OPENING tick
    that has already QUEUED plan intent when the verdict read fails — `_process()`'s own waiting
    block sets only a label, which the steady-state fallback masks (the catch's plan reset would
    be unobservable with it)."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["gate"]["waiting"] = {
        "ticketLabel": "adlc:spec-review",
        "comment": "Awaiting review: {pr_url}",
        "assign": ["{roles.reviewers}"],
    }
    return ProcessConfig.model_validate(cfg)


@pytest.mark.parametrize("failing_read", ["pull_requests", "pr_review_verdict"])
async def test_a_predicate_error_on_the_gate_opening_tick_still_projects(
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failing_read: str,
) -> None:
    """Both failing reads, on the tick that OPENS the gate (latest is None) — the tick the
    WAITING-gate test above never reaches:

    * [pull_requests] `_open_gate`'s `{pr_url}` fetch is the tick's FIRST listing call, ahead of
      the gate arm's own wrapped reads. Unwrapped (the reviewed gap), a route-scoped 403 there
      reproduced the incident permanently on the gate path: savepoint erases the fresh gate row,
      the raise skips `_project`, and the next tick repeats the identical sequence. The wrap now
      lives INSIDE `_pr_values`, covering `_accept`'s call site by the same stroke.
    * [pr_review_verdict] the listing succeeds, so `_open_gate` has already QUEUED the waiting
      outcome's label + comment + assign before the verdict read fails. The catch's
      `tick.plan = _Plan()` is what keeps those aborted-transition intents from posting — this
      parametrization is that line's mutation pin: delete it and this fails on the comment and
      the assignees leaking through for a gate attempt that no longer exists.

    Either way: the labels for the persisted state still project, no gate row survives, no event
    is written, and the ticket stays RUNNING — the WAITING_GATE flip rolled back with the row.
    The next clean tick opens the gate for real."""
    world = await _build_world(
        maker, config=_process_with_a_full_waiting_outcome(f"gate-open-{uuid4().hex[:8]}")
    )
    await run_tick(world)  # plan spawns
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED)
    await run_tick(world)  # plan accepted -> ticket sits on spec_gate; the gate is NOT yet open
    assert [r.step_name for r in await step_runs(world)] == ["plan"]

    _fail_observation(world, failing_read)
    # Drift labels AND assignees so both a steady correction (wanted) and a leaked outcome
    # write (the mutation's tell) would be visible as calls.
    world.github.issue = replace(
        world.github.issue, labels=frozenset({"factorial:new"}), assignees=("operator",)
    )
    errors = _capture_exception_logs(monkeypatch)
    before_events = await event_types(world)
    world.projector.calls.clear()

    await run_tick(world)  # the OPENING tick — must neither raise nor black out

    assert any("evaluation failed" in args[0] for args in errors)
    verbs = world.projector.kinds()
    assert "set_labels" in verbs
    assert "comment" not in verbs  # the aborted attempt's announcement did not post
    assert "set_assignees" not in verbs  # nor its assignment
    assert ("set_labels", frozenset({"factorial:spec-review"})) in world.projector.calls
    assert [r.step_name for r in await step_runs(world)] == ["plan"]  # no gate row survived
    assert await event_types(world) == before_events
    ticket = await ticket_row(world)
    assert ticket.current_step == "spec_gate"
    assert ticket.status == TicketStatus.RUNNING  # not WAITING_GATE — that flip rolled back

    # The error clears; the gate opens for real on the next tick — nothing was lost meanwhile.
    world.use_github(state_source=world.github.snapshot)
    await run_tick(world)
    assert [r.step_name for r in await step_runs(world)] == ["plan", "spec_gate"]
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE
    calls = dict(world.projector.calls)
    assert "Awaiting review" in calls["comment"]


async def test_unblock_survives_a_precondition_evaluation_error(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_grant_attempt`'s evaluate site is the one deliberately NOT routed through the act-phase
    catch: falling through to projection there would re-assert `factorial:blocked` over the human's
    own un-block gesture (and the stays-blocked check would then read the re-added label as
    "keep waiting" forever). Instead the error is caught locally: the un-block proceeds on the
    observed gesture — which nothing about the failed read contradicts — the acknowledgment
    stays generic rather than guessing at preconditions it could not evaluate, and the error is
    loud."""
    world = await _build_world(
        maker,
        assignees=("bot-user",),
        config=_process_with_a_branch_guard(f"guard-{uuid4().hex[:8]}"),
    )
    await run_tick(world)  # attempt 1 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled
    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 2 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent died mid-task")
    await run_tick(world)  # attempt 2 fails -> escalate -> BLOCKED

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    _remove_blocked_label(world)
    _fail_observation(world, "branches")  # the guard's own `branch_exists` read now 403s
    errors = _capture_exception_logs(monkeypatch)
    world.projector.calls.clear()

    await run_tick(world)

    assert any("during un-block" in args[0] for args in errors)
    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING  # the human's gesture won
    assert "unblocked" in await event_types(world)
    calls = dict(world.projector.calls)
    assert "shortly" in calls["comment"]
    assert "still waiting for" not in calls["comment"]  # no guessed naming


async def test_an_observation_error_at_the_top_of_the_tick_keeps_the_rollback_shape(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The reshape's boundary, pinned: `source.issue()` fails before the tick has any observation
    baseline, and projecting is not honestly possible — `_project` computes labels as a whole
    desired set COMPARED AGAINST the observed issue (the module docstring's own rule), and there
    is no observed issue to compare against. So the top-of-tick read keeps today's shape: the
    error propagates for `loop.one()`'s per-ticket isolation to log, nothing is written, the
    next tick retries. (`_resolve_slug` sits outside the reshape for the mirror-image reason:
    it reads only Postgres — no GitHub read to 403 — and a session that cannot SELECT artifact
    rows could not append the suppressed-projection events `_project` writes either.)"""
    world = await _build_world(maker)
    _fail_observation(world, "issue")

    with pytest.raises(_FakeRequestFailed):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []
    assert world.projector.calls == []


# --------------------------------------------------------------------------- admission


async def test_admission_pins_the_config_and_is_idempotent(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, admit=False)
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})

    async def pass_once() -> int:
        async with world.sessions() as session:
            project = await session.get(Project, world.project_id)
            source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
            assert project is not None and source is not None
            admitted = await admit_project(
                session, project, source, candidates, **world.observed_by
            )
        return len(admitted)

    assert await pass_once() == 1
    assert await pass_once() == 0  # (ticket_source_id, external_key) is unique

    async with world.sessions() as session:
        tickets = list(
            (await session.execute(select(Ticket).where(Ticket.project_id == world.project_id)))
            .scalars()
            .all()
        )
        admitted = list(
            (
                await session.execute(
                    select(Event).where(
                        Event.project_id == world.project_id, Event.type == "ticket_admitted"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(tickets) == 1
    assert len(admitted) == 1
    assert tickets[0].current_step == "plan"
    assert tickets[0].owner_at_admission == "operator"
    assert tickets[0].process_version_id is not None


async def test_admission_takes_the_owner_from_the_issue_it_judged_not_the_lagging_listing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The repo-wide listing can trail a fresh assignment while the issue read that passes
    `ticket_has_assignee()` sees it. Recorded as "", the owner is nobody: every gate whose
    approvers are `{ticket_owner}` then refuses every review (sandbox #193). A listing with no
    assignee waits a pass rather than admit with one."""
    world = await _build_world(maker, admit=False)

    async def admit(listed: IssueState) -> list[Ticket]:
        candidates = InMemoryCandidates({world.repo: (listed,)})
        async with world.sessions() as session:
            project = await session.get(Project, world.project_id)
            source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
            assert project is not None and source is not None
            return list(
                await admit_project(session, project, source, candidates, **world.observed_by)
            )

    assert await admit(replace(world.github.issue, assignees=())) == []
    (ticket,) = await admit(replace(world.github.issue, assignees=("someone-else",)))
    assert ticket.owner_at_admission == "operator"


async def test_admission_records_entry_onto_the_first_step(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`ticket_admitted` files under the graph's SYNTHETIC entry node, not under `entry.step`, so
    without this the first real step has nothing in the timeline until its run exists."""
    world = await _build_world(maker, admit=False)
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        admitted = await admit_project(session, project, source, candidates, **world.observed_by)
        assert len(admitted) == 1

    async with world.sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(Event).where(Event.project_id == world.project_id).order_by(Event.id)
                )
            )
            .scalars()
            .all()
        )
    assert [row.type for row in rows] == ["ticket_admitted", "step_entered"]
    assert rows[1].details == {
        "step": "plan",
        "from_step": None,
        "reason": "admission",
        "attempt": 1,
        "kind": "agent",
    }


async def test_admission_skips_issues_the_entry_condition_rejects(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, labels=frozenset({"wontfix"}), admit=False)
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        assert await admit_project(session, project, source, candidates, **world.observed_by) == []


async def test_admit_all_only_reads_active_projects(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`project.active` is the operator's switch: a configured but paused project admits nothing,
    and flipping it on is all it takes for the same pass to admit."""
    world = await _build_world(maker, admit=False)
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})

    async def admitted_here() -> list[Ticket]:
        async with world.sessions() as session:
            return list(
                (await session.execute(select(Ticket).where(Ticket.project_id == world.project_id)))
                .scalars()
                .all()
            )

    # _build_world leaves the project inactive (the column defaults to false).
    await admit_all(world.sessions, world.deps, lambda _client: candidates)
    assert await admitted_here() == []

    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        assert project is not None
        project.active = True

    await admit_all(world.sessions, world.deps, lambda _client: candidates)
    assert len(await admitted_here()) == 1


async def _set_active(world: World, active: bool) -> None:
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        assert project is not None
        project.active = active


async def _admitted_events(world: World) -> list[Event]:
    async with world.sessions() as session:
        return list(
            (
                await session.execute(
                    select(Event).where(Event.project_id == world.project_id).order_by(Event.id)
                )
            )
            .scalars()
            .all()
        )


async def test_admit_issue_admits_the_named_issue_without_listing_the_repo(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A webhook names one issue; judging it must not wait for, or cost, a repo-wide listing
    (#476). No candidate source is wired at all, so a listing would have nothing to return."""
    world = await _build_world(maker, admit=False)
    await _set_active(world, True)

    ids = await admit_issue(world.sessions, world.deps, world.project_id, world.issue_number)

    assert len(ids) == 1
    async with world.sessions() as session:
        ticket = await session.get(Ticket, ids[0])
    assert ticket is not None
    assert ticket.external_key == str(world.issue_number)
    assert ticket.current_step == "plan"
    assert ticket.owner_at_admission == "operator"
    assert [row.type for row in await _admitted_events(world)] == [
        "ticket_admitted",
        "step_entered",
    ]


@pytest.mark.parametrize("status", [TicketStatus.RUNNING, TicketStatus.DONE])
async def test_admit_issue_leaves_an_issue_that_is_a_ticket_alone(
    maker: async_sessionmaker[AsyncSession], status: TicketStatus
) -> None:
    """A finished ticket stays finished: a webhook about its issue never admits it again, and
    a known issue costs no forge read."""
    world = await _build_world(maker)
    await _set_active(world, True)
    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(status=status)
        )

    assert await admit_issue(world.sessions, world.deps, world.project_id, world.issue_number) == []
    assert world.github.issue_reads == 0


async def test_admit_issue_admits_nothing_on_a_paused_project(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, admit=False)  # the project starts inactive

    assert await admit_issue(world.sessions, world.deps, world.project_id, world.issue_number) == []
    assert world.github.issue_reads == 0


async def test_admit_issue_waits_on_an_issue_its_entry_rule_rejects(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, labels=frozenset({"wontfix"}), admit=False)
    await _set_active(world, True)

    assert await admit_issue(world.sessions, world.deps, world.project_id, world.issue_number) == []
    assert await _admitted_events(world) == []


async def test_one_project_s_failed_admission_does_not_cost_the_others_theirs(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every project is admitted in one session. Without a savepoint each, one tenant's 404
    repo rolled back admission for every tenant, every pass."""
    broken = await _build_world(maker, admit=False)
    healthy = await _build_world(maker, admit=False)
    async with healthy.sessions() as session:
        for world in (broken, healthy):
            (await session.get_one(Project, world.project_id)).active = True

    class Candidates:
        async def open_issues(self, repo: str) -> tuple[IssueState, ...]:
            if repo == broken.repo:
                raise RuntimeError("404 Not Found")
            return (healthy.github.issue,) if repo == healthy.repo else ()

    await admit_all(healthy.sessions, healthy.deps, lambda _client: Candidates())

    async with healthy.sessions() as session:
        admitted = (
            await session.execute(
                select(Ticket.project_id).where(
                    Ticket.project_id.in_((broken.project_id, healthy.project_id))
                )
            )
        ).scalars().all()
    assert admitted == [healthy.project_id]


async def test_a_project_whose_admission_times_out_mid_query_costs_the_next_one_nothing(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every project once admitted on one shared session: a deadline that cancelled a query left
    its connection unusable, and every later project in that pass failed with it."""
    stuck = await _build_world(maker, admit=False)
    healthy = await _build_world(maker, admit=False)
    async with healthy.sessions() as session:
        for world in (stuck, healthy):
            (await session.get_one(Project, world.project_id)).active = True
    real_lanes = admission._lanes  # pyright: ignore[reportPrivateUsage]

    async def lanes(session: AsyncSession, project: Project) -> Any:
        if project.id == stuck.project_id:
            await session.execute(text("SELECT pg_sleep(30)"))
        return await real_lanes(session, project)

    monkeypatch.setattr(admission, "_lanes", lanes)
    monkeypatch.setattr(admission, "ADMISSION_DEADLINE_SECONDS", 0.5)
    candidates = InMemoryCandidates(
        {stuck.repo: (stuck.github.issue,), healthy.repo: (healthy.github.issue,)}
    )

    await admit_all(healthy.sessions, healthy.deps, lambda _client: candidates)

    async with healthy.sessions() as session:
        admitted = (
            await session.execute(
                select(Ticket.project_id).where(
                    Ticket.project_id.in_((stuck.project_id, healthy.project_id))
                )
            )
        ).scalars().all()
    assert admitted == [healthy.project_id]


async def test_a_gitlab_project_without_its_own_credentials_is_never_read(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission once fell back to the deployment's GitLab token for such a project, which read
    whatever repository its tenant had named. Its only way to GitLab is its own client."""
    world = await _build_world(maker, admit=False)
    async with world.sessions() as session:
        (await session.get_one(Project, world.project_id)).active = True
        source = await session.get_one(TicketSource, world.source_id)  # type: ignore[attr-defined]
        source.kind = TicketSourceKind.GITLAB
    asked: list[int] = []

    async def no_credentials(_session: AsyncSession, project: Project) -> None:
        asked.append(project.id)

    monkeypatch.setattr(
        "orchestrator.gitlab.project_client.client_for_project", no_credentials
    )
    assert await admit_gitlab_all(world.sessions) == []
    assert asked == [world.project_id]


async def test_a_gitlab_ticket_without_its_project_s_client_never_reads_github(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A GitLab source never drops into the project's GitHub: without a GitLab client of its
    own, the tick holds the ticket rather than observe it through the GitHub connection. The hold
    is said once, with what the project lacks, not once a tick."""
    async with world.sessions() as session:
        source = await session.get_one(TicketSource, world.source_id)  # type: ignore[attr-defined]
        source.kind = TicketSourceKind.GITLAB

    async def no_credentials(_session: AsyncSession, _project: Project) -> None:
        return None

    monkeypatch.setattr(
        "orchestrator.gitlab.project_client.client_for_project", no_credentials
    )
    reads = world.github.issue_reads
    with caplog.at_level(logging.WARNING, logger="orchestrator.worker.context"):
        await run_tick(world)
        await run_tick(world)
    assert world.github.issue_reads == reads
    held = [r.getMessage() for r in caplog.records if "is held" in r.getMessage()]
    assert len(held) == 1
    assert "GitLab is not configured for this project" in held[0]




async def test_the_startup_sweep_goes_on_past_a_project_that_fails(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """One project's broken connection once ended the whole sweep, so every project after it
    kept its stale run tokens."""
    broken = await _build_world(maker)
    later = await _build_world(maker)
    swept: list[str] = []

    class Sweeper:
        async def sweep_run_credentials(
            self, repo: str, *, active_run_ids: frozenset[int]
        ) -> int:
            del active_run_ids
            swept.append(repo)
            return 0

    async def tracker(_session: AsyncSession, _project: Project) -> object:
        return object()

    async def forge(_session: AsyncSession, project: Project) -> Sweeper:
        if project.id == broken.project_id:
            raise RuntimeError("this project's connection is broken")
        return Sweeper()

    deps = replace(later.deps, trackers={"github": tracker}, forges={"github": forge})
    await _sweep_run_credentials(later.sessions, deps)
    assert later.repo in swept


async def test_a_jira_tracker_pairs_with_the_github_forge(world: World) -> None:
    """A tracker-only kind plus the GitHub forge runs a full reconcile tick (issue #420)."""
    from collections.abc import Iterable
    from dataclasses import dataclass

    from orchestrator.adapters.split import SourceTracker, views_from_source
    from orchestrator.adapters.state import SnapshotState
    from orchestrator.github import IssueState as SharedIssue

    jira_issue = SharedIssue(
        number=12,
        title="From Jira",
        body="tracker body",
        state="open",
        labels=frozenset({"factorial:new"}),
        assignees=("alice",),
    )
    writes: list[tuple[str, object]] = []

    class FakeWrites:
        async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None:
            writes.append(("labels", list(labels)))

        async def close_issue(self, issue_number: int) -> None:
            writes.append(("close", issue_number))

        async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None:
            writes.append(("assign", list(logins)))

        async def comment(self, issue_number: int, body: str) -> None:
            writes.append(("comment", body))

        async def merge_pr(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("the tracker does not merge")

    @dataclass
    class FakeJira:
        kind: str = "jira"
        bot_login: str = "jira-bot"

        def observe(self, repo: str, key: str) -> SourceTracker:
            del repo, key
            return SourceTracker(
                SnapshotState(repo="jira", issue_state=jira_issue), bot_login=self.bot_login
            )

        def writes(self, repo: str) -> FakeWrites:
            del repo
            return FakeWrites()

        def media(self, repo: str) -> None:
            del repo
            return None

        def media_refs(self, text: str) -> tuple[()]:
            del text
            return ()

        def is_delivery_author(self, login: str) -> bool:
            return login == self.bot_login

        async def candidates(self, repo: str) -> tuple[SharedIssue, ...]:
            del repo
            return (jira_issue,)

        def views(self, source: object) -> object:
            return views_from_source(source, bot_login=self.bot_login)  # type: ignore[arg-type]

    fake = FakeJira()

    async def factory(_session: AsyncSession, _project: Project) -> FakeJira:
        return fake

    world.deps = replace(world.deps, trackers={"jira": factory})
    async with world.sessions() as session:
        project = await session.get_one(Project, world.project_id)
        project.settings = {**project.settings, "forge": "github"}
        source = await session.get_one(TicketSource, world.source_id)  # type: ignore[attr-defined]
        source.kind = TicketSourceKind.JIRA

    reads = world.github.issue_reads
    await run_tick(world)
    assert (await ticket_row(world)).title == "From Jira"
    assert world.github.issue_reads == reads
    assert any(name == "labels" for name, _ in writes)


# --------------------------------------------------------------------------- misc


@pytest.mark.parametrize("name", ["tick", "run_forever", "reconcile_ticket", "ReconcileDeps"])
async def test_package_reexports_the_loop_surface(name: str) -> None:
    """What `factorial-worker` and every caller outside this package are allowed to import."""
    import orchestrator.worker as pkg

    assert hasattr(pkg, name)
