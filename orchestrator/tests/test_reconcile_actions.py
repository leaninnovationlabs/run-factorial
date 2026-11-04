"""Action steps: execution, retries, idempotence, recovery, and permanent
failures on the action path.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.github import (
    BranchState,
    PullRequestState,
)
from orchestrator.worker import action_steps as action_steps_module
from orchestrator.worker.reconcile import reconcile_ticket
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    _build_world,
    _FakeRequestFailed,
    _process_with_action,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# --------------------------------------------------------------------------- action steps


def _tests_pr(
    *, number: int = 7, base_ref: str = "spec/12-widget", head_sha: str = "c0ffee00" * 5
) -> PullRequestState:
    return PullRequestState(
        number=number,
        head_ref="tests/12-widget",
        base_ref=base_ref,
        head_sha=head_sha,
        state="open",
        merged=False,
        url=f"https://github.test/pr/{number}",
    )


async def test_action_step_executes_and_advances_to_next(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, config=_process_with_action("proc-action"))
    world.github.branches.append(BranchState(name="spec/12-widget", sha="a" * 40))
    world.github.pulls.append(_tests_pr())

    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.current_step == "done"
    assert ticket.status == TicketStatus.RUNNING

    (run,) = await step_runs(world)
    assert run.kind == StepKind.ACTION
    assert run.status == StepRunStatus.SUCCEEDED
    assert run.attempt == 1
    # Actions are not manifests: `runtime_kind` (0003_runtime_kinds) stays NULL for this row,
    # unlike an agent run's (`test_a_manifest_without_permissions_mints_the_default_profile`
    # exercises the agent side of the same column indirectly via the spawned RunSpec).
    assert run.runtime_kind is None

    assert await event_types(world) == [
        "step_started",
        "action_executed",
        "step_completed",
        "step_entered",
    ]
    assert world.contract_github.github.tags["contract/12-widget"] == "c0ffee00" * 5

    calls = dict(world.projector.calls)
    assert calls["set_labels"] == frozenset({"factorial:fixed"})
    assert ("c0ffee00" * 5) in calls["comment"]


async def test_action_failure_retries_then_escalates(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """No spec branch is ever observed, so `fix_the_contract` fails every attempt — the same
    `_fail_run`/`_retry_or_escalate` machinery `test_retry_exhaustion_escalates` exercises for an
    agent step, driven here by an action instead."""
    world = await _build_world(
        maker, config=_process_with_action("proc-action-fail"), assignees=("bot-user",)
    )
    await run_tick(world)  # attempt 1: fails immediately (synchronous, no spawn/poll split)

    (run,) = await step_runs(world)
    assert run.kind == StepKind.ACTION
    assert run.status == StepRunStatus.FAILED
    failed = next(row for row in await event_rows(world) if row.type == "action_failed")
    assert "expected exactly one branch" in (failed.details.get("failure_reason") or "")
    assert "retry_scheduled" in await event_types(world)

    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)  # attempt 2: fails again -> escalate (2 == maxAttempts)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "escalated" in await event_types(world)

    calls = dict(world.projector.calls)
    assert "factorial:blocked" in calls["set_labels"]
    assert calls["set_assignees"] == ("operator",)
    assert "failed after 2 attempt(s)" in calls["comment"]

    runs = await step_runs(world)
    assert len(runs) == 2
    assert all(run.kind == StepKind.ACTION for run in runs)
    assert [run.attempt for run in runs] == [1, 2]


async def test_action_spawn_is_idempotent_under_concurrency(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same `UNIQUE (ticket_id, step_name, attempt)` an agent spawn relies on
    (`test_spawn_is_idempotent_under_concurrency`) arbitrates an action's start too — only the
    winner ever calls the action at all, so `fix_the_contract`'s GitHub mutations fire once.

    Both runners choose attempt 1 before either inserts. One that read the runs or chose the
    attempt after the other committed would start attempt 2, a key the constraint never sees;
    the ticket's lease rules that out in production, and left to the scheduler it made this test
    flake on CI."""
    world = await _build_world(maker, config=_process_with_action("proc-action-idempotent"))
    world.github.branches.append(BranchState(name="spec/12-widget", sha="a" * 40))
    world.github.pulls.append(_tests_pr())

    both_chose = asyncio.Barrier(2)
    next_attempt = action_steps_module._next_attempt

    async def choose_then_wait(*args: Any, **kwargs: Any) -> int:
        attempt = await next_attempt(*args, **kwargs)
        await both_chose.wait()
        return attempt

    monkeypatch.setattr(action_steps_module, "_next_attempt", choose_then_wait)

    async def one() -> None:
        async with world.sessions() as session:
            ticket = await session.get(Ticket, world.ticket_id)
            assert ticket is not None
            # `_start_action` never renews (only an agent spawn does, decision 41) — the lease
            # owner/ttl are still required kwargs, unused on this path.
            await reconcile_ticket(
                session, ticket, world.deps, lease_owner=_LEASE_OWNER, lease_ttl=_LEASE_TTL
            )

    async with asyncio.timeout(30):
        await asyncio.gather(one(), one())

    runs = await step_runs(world)
    assert len(runs) == 1
    types = await event_types(world)
    assert types.count("action_executed") == 1
    assert types.count("step_started") == 1
    # Exactly one tag write — a second, racing execution would have written it twice.
    assert world.contract_github.calls.count(("create_or_move_tag", "contract/12-widget")) == 1


async def test_running_action_row_from_an_interrupted_worker_is_recovered(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An action executes synchronously — a RUNNING row can only mean a previous tick crashed
    mid-execution, not work still in flight (there is no adapter to poll the way `_poll_run`
    polls an agent). Seed exactly that row by hand and prove the next tick fails it and feeds it
    into the ordinary retry machinery, rather than leaving it wedged forever."""
    world = await _build_world(maker, config=_process_with_action("proc-action-running"))

    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        session.add(
            StepRun(
                ticket_id=world.ticket_id,
                step_name="fix",
                kind=StepKind.ACTION,
                attempt=1,
                status=StepRunStatus.RUNNING,
                started_at=world.clock.now,
            )
        )

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.failure_reason is not None and "interrupted worker" in run.failure_reason
    failed = next(row for row in await event_rows(world) if row.type == "action_failed")
    assert "interrupted worker" in (failed.details.get("failure_reason") or "")
    assert "retry_scheduled" in await event_types(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING  # not yet escalated — attempt 1 of 2


# ------------------------------------------------------------------ permanent failures, action path


async def test_permanent_action_wiring_failure_escalates_without_spinning(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_start_action`'s own PERMANENT case: no `contract_github` surface wired in —
    the action-path twin of `test_permanent_missing_agent_source_wiring_escalates_without_
    spinning`. Scoped to the PRE-tag region: `fix_the_contract` never even starts, so the tag is
    never touched."""
    world = await _build_world(
        maker, config=_process_with_action("proc-action-permanent"), assignees=("bot-user",)
    )
    world.use_github(contract_github=None)

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.kind == StepKind.ACTION
    assert run.status == StepRunStatus.FAILED
    assert run.attempt == 1
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("misconfiguration: ")
    assert "no GitHub action surface is configured" in run.failure_reason

    types = await event_types(world)
    assert types == ["step_started", "action_failed", "escalated"]
    assert "retry_scheduled" not in types

    failed = next(row for row in await event_rows(world) if row.type == "action_failed")
    assert failed.details.get("reason") == "permanent"
    escalated = next(row for row in await event_rows(world) if row.type == "escalated")
    assert escalated.details.get("permanent") is True

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert world.contract_github.calls == []  # never reached the tag


async def test_infrastructure_tag_write_failure_rolls_back_byte_for_byte(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A `RequestFailed`-shaped failure AT the tag write itself stays INFRASTRUCTURE:
    `_start_action`'s wrap covers registry lookups and `ContractGitHub` calls BEFORE the tag,
    never the tag write or anything after it — `fix_the_contract`'s own irreversible-act boundary
    (its docstring's step 8) is untouched, and the best-effort semantics for what happens
    after a SUCCESSFUL tag are simply never reached from this failure at all."""
    world = await _build_world(maker, config=_process_with_action("proc-action-infra"))
    world.github.branches.append(BranchState(name="spec/12-widget", sha="a" * 40))
    world.github.pulls.append(_tests_pr())
    world.contract_github.fail_create_or_move_tag = _FakeRequestFailed("500 from GitHub")

    with pytest.raises(_FakeRequestFailed):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


async def test_unknown_step_kind_raises(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed-set discipline `_resolve_input_refs` established for `BareInputSource`:
    `reconcile_ticket`'s three-way dispatch raises rather than mis-dispatching an unrecognized
    step kind into `_gate_step` — the bare `else` this replaced would have (`_gate_step`
    dereferences `step.gate`, which a non-GateStep does not have)."""

    class _WeirdStep:
        name = "weird"

    original_step = ProcessConfig.step

    def fake_step(self: ProcessConfig, name: str) -> Any:
        return _WeirdStep() if name == "weird" else original_step(self, name)

    monkeypatch.setattr(ProcessConfig, "step", fake_step)

    world = await _build_world(maker, config=_process_with_action("proc-unknown-kind"))
    async with world.sessions() as session:
        await session.execute(
            update(Ticket).where(Ticket.id == world.ticket_id).values(current_step="weird")
        )

    with pytest.raises(ValueError, match="dispatch does not handle"):
        await run_tick(world)
