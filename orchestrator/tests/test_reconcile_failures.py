"""Failure paths: retry and escalation, permanent failures, spawn refusals
with reasons, and un-block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from githubkit.exception import RequestFailed
from githubkit.response import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.controls import request_unblock
from orchestrator.config import ConfigError, ProcessConfig
from orchestrator.db.models import (
    Project,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.types import RequestedVia
from orchestrator.factorial import (
    McpNotAllowed,
    PermissionNotAllowed,
    ResolvedAgent,
)
from orchestrator.github import (
    TokenScopeNotGranted,
)
from orchestrator.github.client import AppCredentialError, RateLimited
from orchestrator.worker.failures import first_paragraph, spoken
from orchestrator.worker.loop import _reconcile_leased
from runtime.contract import (
    AdapterUnavailable,
    RunStatus,
    SpawnError,
    SpawnReason,
)
from tests.reconcile_world import (
    _LEASE_OWNER,
    _LEASE_TTL,
    World,
    _build_world,
    _ensure_leased,
    _escalate_the_plan_step,
    _NoMcpAdapter,
    _process,
    _process_with_a_branch_guard,
    _process_with_action,
    _process_without_gate_reject,
    _process_without_retry,
    _remove_blocked_label,
    advance_to_gate,
    event_rows,
    event_types,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# --------------------------------------------------------------------------- failure paths


async def _held_tick(world: World) -> None:
    """One pass the way the loop runs it: leased, reconciled, and its raise counted, not raised."""
    async with world.sessions() as session:
        await _ensure_leased(session, await session.get_one(Ticket, world.ticket_id))
    await _reconcile_leased(
        world.ticket_id,
        world.deps,
        scope=world.sessions,
        lease_owner=_LEASE_OWNER,
        lease_ttl=_LEASE_TTL,
        listings=None,
        quarantine_after=20,
    )


async def test_a_launch_its_pass_did_not_commit_is_cancelled(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise after the spawn rolls the run's row back; the run itself must not keep going, or
    the next pass, finding no row, launches another (ticket 205 left about 35)."""
    world = await _build_world(maker)

    async def github_502(issue_number: int, labels: Any) -> None:
        raise RuntimeError("github 502")

    monkeypatch.setattr(world.projector, "set_labels", github_502)
    await _held_tick(world)

    assert await step_runs(world) == []
    assert world.adapter.cancelled == ["run-1"]

    monkeypatch.undo()
    await _held_tick(world)

    (run,) = await step_runs(world)
    assert run.runtime_run_id == "run-2"
    assert world.adapter.cancelled == ["run-1"]


async def test_timeout_cancels_the_run_and_marks_it(world: World) -> None:
    await run_tick(world)
    world.clock.advance(timedelta(hours=2))  # past `defaults.timeout: 1h`
    await run_tick(world)

    run = (await step_runs(world))[0]
    assert run.status == StepRunStatus.TIMEOUT
    assert world.adapter.cancelled == ["run-1"]
    failed = next(row for row in await event_rows(world) if row.type == "agent_failed")
    assert failed.details["reason"] == "timeout"
    assert "retry_scheduled" in await event_types(world)


async def _request_retry(world: World, run: StepRun) -> None:
    """What `POST /step-runs/{id}/retry` records, written directly: this is the loop's half."""
    async with world.sessions() as session:
        await append(
            session,
            type=EventType.RETRY_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=run.id,
            step=run.step_name,
            attempt=run.attempt,
            next_attempt=run.attempt + 1,
            requested_by="operator@example.com",
        )


async def _failed_once(world: World) -> StepRun:
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled, 10m out
    world.adapter.restart()
    await run_tick(world)  # still inside the backoff: nothing starts
    (failed,) = await step_runs(world)
    return failed


async def test_a_requested_retry_starts_without_waiting_out_its_backoff(world: World) -> None:
    failed = await _failed_once(world)

    await _request_retry(world, failed)
    await run_tick(world)

    runs = [(run.attempt, run.status) for run in await step_runs(world)]
    assert runs == [(1, StepRunStatus.FAILED), (2, StepRunStatus.RUNNING)]


async def test_a_retry_request_answers_only_the_run_it_named(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The request named attempt 1. Attempt 2 failing in its turn owes its own backoff."""
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    raw["defaults"]["retry"]["maxAttempts"] = 3
    world = await _build_world(maker, config=ProcessConfig.model_validate(raw))
    failed = await _failed_once(world)
    await _request_retry(world, failed)
    await run_tick(world)  # attempt 2 starts
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up again")
    await run_tick(world)  # attempt 2 fails -> retry scheduled
    world.adapter.restart()
    await run_tick(world)

    assert [run.attempt for run in await step_runs(world)] == [1, 2]


async def test_retry_exhaustion_escalates(maker: async_sessionmaker[AsyncSession]) -> None:
    # The issue currently sits with someone other than `{ticket_owner}`, so the escalation's
    # `assign` is a real change rather than a no-op the projection would (correctly) skip.
    world = await _build_world(maker, assignees=("bot-user",))
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled

    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 2 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up again")
    world.projector.calls.clear()
    await run_tick(world)  # attempt 2 fails -> escalate

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "escalated" in await event_types(world)

    calls = dict(world.projector.calls)
    assert "factorial:blocked" in calls["set_labels"]
    assert calls["set_assignees"] == ("operator",)  # back to {ticket_owner}
    assert "failed after 2 attempt(s): agent gave up again" in calls["comment"]

    # A blocked ticket waits on a human: further ticks change nothing.
    before = await event_types(world)
    await run_tick(world)
    assert await event_types(world) == before


# --------------------------------------------------------------------------- permanent failures


def _raising_resolve_agent(exc: Exception) -> Any:
    async def resolve(
        _session: AsyncSession,
        _project: Project,
        _agent: str,
        _pins: Any = None,
        _repo: str | None = None,
    ) -> ResolvedAgent:
        raise exc

    return resolve


def _raising_mint_token(exc: Exception) -> Any:
    async def mint(_repo: str, _permissions: Mapping[str, str]) -> str:
        raise exc

    return mint


@pytest.mark.parametrize(
    ("attach", "exc", "reason_substring"),
    [
        (
            "resolve_agent",
            PermissionNotAllowed(
                "agent 'plan-agent' requests permission(s) this project's ceiling does not permit"
            ),
            "ceiling does not permit",
        ),
        (
            "resolve_agent",
            McpNotAllowed("agent 'plan-agent' declares MCP server(s) this project does not permit"),
            "does not permit",
        ),
        (
            "resolve_agent",
            ConfigError(
                "acme/widgets@abc1234:.factorial/agents/plan-agent/agent.yaml",
                "manifest name 'plan-agent' does not match its folder 'plan-agent-typo'",
            ),
            "does not match its folder",
        ),
        (
            "mint_token",
            AppCredentialError(
                "the GitHub App private key is present but unusable: it could not sign the App "
                "JWT (InvalidKeyError: Could not parse the provided public key). Paste the App's "
                "private key again in the project's GitHub connection on Ticket source. Retrying "
                "won't help, because the same key fails the same way"
            ),
            "present but unusable",
        ),
        (
            "mint_token",
            TokenScopeNotGranted(
                "GitHub minted a token for acme/widgets that does not match the request: "
                "contents: requested write, not granted at all"
            ),
            "not granted at all",
        ),
    ],
    ids=[
        "permission-not-allowed",
        "mcp-not-allowed",
        "config-error",
        # A silent-retry cause, at the boundary that now escalates it: a
        # PRESENT-but-malformed App key. `github/client.py` reads the githubkit
        # `RequestError`'s PyJWT cause and raises this `RuntimeError` subclass, which the
        # bare `RuntimeError` member of `_PERMANENT_CONFIG_ERRORS` already catches; left
        # raw, the `RequestError` is a plain `Exception` that would roll the tick back to
        # nothing, forever. `test_github_client.py` carries the classification half.
        "malformed-app-private-key",
        "token-scope-not-granted",
    ],
)
async def test_permanent_build_spec_failure_escalates_without_spinning(
    maker: async_sessionmaker[AsyncSession],
    attach: str,
    exc: Exception,
    reason_substring: str,
) -> None:
    """Every PERMANENT class, `ConfigError` included (a manifest
    that will not parse — the same 'only an operator can fix this' shape as the two named
    `AgentResolutionError` subclasses): a misconfiguration a retry cannot fix must fail the run
    VISIBLY and escalate IMMEDIATELY — never spin at attempt 1 forever.
    `assignees=("bot-user",)` (not `{ticket_owner}`) so the escalation's own `assign` outcome is
    a real, observable change, matching `test_retry_exhaustion_escalates`'s own setup."""
    world = await _build_world(maker, assignees=("bot-user",))
    if attach == "resolve_agent":
        world.deps = replace(world.deps, resolve_agent=_raising_resolve_agent(exc))
    else:
        world.mint_with(_raising_mint_token(exc))

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.attempt == 1  # no spin: never advances past the first attempt
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("misconfiguration: ")
    assert reason_substring in run.failure_reason

    types = await event_types(world)
    assert types == ["step_started", "agent_failed", "escalated"]
    assert "retry_scheduled" not in types  # escalated NOW, not queued for later

    failed = next(row for row in await event_rows(world) if row.type == "agent_failed")
    assert failed.details.get("reason") == "permanent"
    escalated = next(row for row in await event_rows(world) if row.type == "escalated")
    assert escalated.details.get("permanent") is True

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels
    assert world.adapter.spawns == []  # nothing was ever launched

    # Parity with the spawn-refusal family below: a permanent failure is
    # only actually visible if the human-facing surface carries the reason. Asserting the row's
    # `failure_reason` alone would pass even if `{failure_reason}` never reached the comment.
    calls = dict(world.projector.calls)
    assert reason_substring in calls["comment"]

    # A blocked ticket waits on a human: further ticks change nothing (same as an ordinary
    # escalation — `test_retry_exhaustion_escalates`'s own closing assertion).
    before = await event_types(world)
    await run_tick(world)
    assert await event_types(world) == before


async def test_permanent_missing_agent_source_wiring_escalates_without_spinning(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The exact `RuntimeError` `_build_run_spec` raises itself when no `resolve_agent` callback
    was ever wired in (`tick.deps.resolve_agent is None`) — a deployment bug, not a `.factorial/`
    problem, but the SAME 'a retry cannot fix this' shape every other PERMANENT class has, and
    the site the task names explicitly ('the config RuntimeError raised in `_build_run_spec`
    when the manifest is unresolvable')."""
    world = await _build_world(maker, assignees=("bot-user",))
    world.deps = replace(world.deps, resolve_agent=None)

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.attempt == 1
    assert run.failure_reason is not None
    assert "no agent source is configured" in run.failure_reason

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert world.adapter.spawns == []


async def test_a_rate_limit_during_agent_resolution_is_not_a_misconfiguration(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A connection's spent limit is not the ticket's setup: once a `RuntimeError`, it was caught
    by the bare `RuntimeError` the permanent catch carries, and escalated the ticket."""
    world = await _build_world(maker, assignees=("bot-user",))
    spent = RateLimited(world.clock() + timedelta(hours=1))
    world.deps = replace(world.deps, resolve_agent=_raising_resolve_agent(spent))

    with pytest.raises(RateLimited):
        await run_tick(world)

    assert await step_runs(world) == []
    assert (await ticket_row(world)).status == TicketStatus.RUNNING


async def test_mcp_capability_mismatch_escalates_without_spinning(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reachable on any adapter whose `supports_mcp` is False — none ships today, but the
    contract allows one. `RESOLVED_AGENT` (this module's own default) already declares one MCP
    server (`MCP_SERVER`), so pairing it with `_NoMcpAdapter` reproduces the mismatch directly:
    without this pre-check, the manifest clears `_enforce_mcp_allowlist` and only dies inside
    `adapter.spawn()` as a bare, INFRASTRUCTURE-classified `SpawnError` — an invisible spin with
    NO human-facing surface at all, since the ticket never reaches BLOCKED."""
    world = await _build_world(maker, assignees=("bot-user",), adapter_cls=_NoMcpAdapter)

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.attempt == 1
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("misconfiguration: ")
    assert "product-docs" in run.failure_reason  # names the declared server
    assert "can't connect MCP servers" in run.failure_reason

    types = await event_types(world)
    assert types == ["step_started", "agent_failed", "escalated"]
    assert "retry_scheduled" not in types

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels
    assert world.adapter.spawns == []  # spawn() was never reached


async def test_mcp_capable_adapter_spawns_normally(world: World) -> None:
    """The positive control for the MCP capability pre-check: the module's
    default `FakeAdapter.supports_mcp` is True (matching `claude_cli`), so the SAME MCP-
    declaring `RESOLVED_AGENT` every fixture in this module shares must still spawn normally —
    the same ground `test_the_run_spec_carries_declared_mcp_servers` already covers, asserted
    again here as the check's own explicit control so the two tests read side by side."""
    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    assert world.adapter.spawns
    (server,) = world.adapter.spawns[0].mcp
    assert server.name == "product-docs"


async def test_infrastructure_spawn_failure_rolls_back_byte_for_byte(world: World) -> None:
    """A `SpawnError` (here: a `busy` condition) is NOT a config problem: the same spec, retried
    later, may well succeed once the substrate frees up. `adapter.spawn(spec)` is deliberately
    left unwrapped (see the comment at that call site in `agent_steps.py`), so
    this must keep TODAY's exact rollback shape: no row, no events, the exception itself
    propagating out of `reconcile_ticket` for `loop.py` to log and retry."""
    world.adapter.fail_spawn(SpawnError("busy: run abc123 is still using the one agent slot"))

    with pytest.raises(SpawnError):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []
    assert world.adapter.spawns == []


async def test_spawn_error_inside_the_wrap_still_rolls_back(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The structural `except (LeaseLost, AgentRuntimeError): raise`
    guard, proven directly — a `SpawnError` raised from INSIDE the wrapped region (here: the
    run-token mint, which `_build_run_spec` calls) is a `RuntimeError` subclass too
    (`AgentRuntimeError(RuntimeError)`) and would otherwise match `_PERMANENT_CONFIG_ERRORS`'s
    bare `RuntimeError` member. It must still roll back byte for byte — no row, no events — the
    same shape `test_infrastructure_spawn_failure_rolls_back_byte_for_byte` proves for the
    ordinary spawn-path case, never escalate as a misconfiguration."""
    world = await _build_world(maker)
    world.mint_with(_raising_mint_token(SpawnError("busy: run abc123 is still using the slot")))

    with pytest.raises(SpawnError):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


# -------------------------------------------------------------- spawn refusals carry a reason


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        (
            SpawnReason.CONFIG,
            "agent 'plan-agent' declares MCP server(s) product-docs, and the adapter's "
            "`supports_mcp` is False",
        ),
        (
            SpawnReason.CREDENTIAL,
            "no CODEX_AUTH_JSON in the project store, and cannot read ~/.codex/auth.json",
        ),
        (
            # The bootstrap doc's claude_cli wedge — a base ref that is not there.
            # `docs/bootstrap.md`, "Do not put `{slug}` in `workspace.base`".
            SpawnReason.WORKSPACE,
            "workspace.base 'spec/12-never-pushed' resolves to no commit",
        ),
    ],
    ids=["config", "credential", "workspace"],
)
async def test_a_permanent_spawn_refusal_escalates_instead_of_spinning(
    maker: async_sessionmaker[AsyncSession], reason: SpawnReason, detail: str
) -> None:
    """BEFORE: all three of these were one flat `SpawnError`, which this module classified
    INFRASTRUCTURE by construction: the exception propagated, the tick's rollback erased the
    `step_run` row and its `STEP_STARTED` event, and the next tick started "attempt 1" again —
    forever, with nothing in the event log, the timeline API, or the UI, and no `factorial:blocked`
    label for a human or for un-block to react to. A worker log line was the entire
    diagnostic (`test_infrastructure_spawn_failure_rolls_back_byte_for_byte`, which still pins
    that shape for the retryable case).

    AFTER: the adapter says WHICH kind of refusal it is, and a permanent one takes the identical
    path a `ConfigError` takes — one visible FAILED row at attempt 1, an `agent_failed` event
    carrying `reason="permanent"`, an immediate `escalated`, `factorial:blocked`, and a comment that
    names the refusal. Retry budget is skipped on purpose: re-running the same manifest against
    the same substrate fails the same way.
    """
    world = await _build_world(maker, assignees=("bot-user",))
    world.adapter.fail_spawn(SpawnError(detail, reason=reason))

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.attempt == 1  # no spin
    assert run.runtime_run_id is None  # nothing was launched, so nothing is pollable
    assert run.failure_reason == f"misconfiguration: {detail}"

    types = await event_types(world)
    assert types == ["step_started", "agent_failed", "escalated"]
    assert "retry_scheduled" not in types

    failed = next(row for row in await event_rows(world) if row.type == "agent_failed")
    assert failed.details.get("reason") == "permanent"
    assert failed.details.get("failure_summary") == f"Setup problem: {detail}"
    escalated = next(row for row in await event_rows(world) if row.type == "escalated")
    assert escalated.details.get("permanent") is True
    assert escalated.details.get("failure_summary") == f"Setup problem: {detail}"

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels

    # The ticket comment carries the adapter's own words — which is the whole difference
    # between "the pipeline is quiet" and "the pipeline told me what is wrong".
    calls = dict(world.projector.calls)
    assert detail in calls["comment"]
    assert calls["set_assignees"] == ("operator",)  # the escalation hands the ticket back

    # Blocked means blocked: further ticks add nothing and never retry the refused spawn.
    before = await event_types(world)
    await run_tick(world)
    assert await event_types(world) == before


async def test_an_unclassified_mint_failure_still_rolls_back_byte_for_byte(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The orchestrator-side twin of `test_github_client.py`'s 403/5xx controls.

    `AppCredentialError` escalates because it is a `RuntimeError`; every OTHER githubkit failure
    out of the mint stays a plain `Exception`, so `_PERMANENT_CONFIG_ERRORS` never sees it and the
    tick rolls back to nothing — which is right for a 403 (a suspended installation an operator
    fixes in GitHub's UI) and for a 5xx. Pinned here so the classification cannot be widened by
    class later without this failing: a `RequestFailed` reaching this boundary must NOT block a
    ticket.
    """
    world = await _build_world(maker, assignees=("bot-user",))
    world.mint_with(_raising_mint_token(_a_request_failed(403)))

    with pytest.raises(RequestFailed):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


def _a_request_failed(status: int) -> RequestFailed:
    """A real `RequestFailed`, built the way githubkit builds one (`core.py:445`) — from a
    `Response` over an httpx response. Not a stand-in: the assertion above is about what the
    permanent-failure boundary does with the concrete class githubkit raises."""
    request = httpx.Request("POST", "https://api.github.test/app/installations/2/access_tokens")
    raw = httpx.Response(status, request=request, json={"message": "Resource not accessible"})
    return RequestFailed(Response(raw, Any))  # pyright: ignore[reportArgumentType]


async def test_a_reasonless_spawn_error_still_rolls_back_byte_for_byte(world: World) -> None:
    """The backward-compatibility half, at the boundary that consumes the reason.

    An adapter that has not classified a site raises `SpawnError(msg)`, which reads
    `INFRASTRUCTURE`, and must behave exactly as it always has — no row, no event, the
    exception propagating for `loop.py` to log and the next tick to retry. Written as a SEPARATE
    test from `test_infrastructure_spawn_failure_rolls_back_byte_for_byte` because that one pins
    the shape and this one pins the DEFAULT: delete the default and this fails while that passes.
    """
    error = SpawnError("could not start container: boom")
    assert error.reason is SpawnReason.INFRASTRUCTURE  # the default, stated
    world.adapter.fail_spawn(error)

    with pytest.raises(SpawnError):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


async def test_an_explicit_infrastructure_refusal_is_never_escalated(world: World) -> None:
    """The negative control for `_permanent_spawn`. A busy substrate is the one thing that must
    NOT become a misconfiguration comment: escalating it would block a live ticket on a condition
    that clears itself, which is strictly worse than the silence it replaced."""
    world.adapter.fail_spawn(
        SpawnError(
            "run-abc still holds the one agent slot",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    )

    with pytest.raises(SpawnError):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


async def test_a_non_spawn_adapter_error_is_never_reclassified(world: World) -> None:
    """`AdapterUnavailable` and `IsolationError` are `AgentRuntimeError`s that are NOT
    `SpawnError`s: they carry no reason, and the narrow `except SpawnError` at the call site is
    what keeps them propagating. Widening that clause to `AgentRuntimeError` would make this
    fail — which is the point of pinning it."""
    world.adapter.fail_spawn(AdapterUnavailable("the claude binary is not on PATH"))

    with pytest.raises(AdapterUnavailable):
        await run_tick(world)

    assert await step_runs(world) == []
    assert await event_rows(world) == []


# -------------------------------------------------------------------------------------- un-block


async def test_unblock_after_label_removed_resumes_with_a_fresh_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels
    spawns_before = len(world.adapter.spawns)
    assert spawns_before == 2  # attempts 1 and 2, both exhausted

    _remove_blocked_label(world)
    before = await event_types(world)
    world.projector.calls.clear()
    await run_tick(world)  # this tick's one transition: BLOCKED -> RUNNING

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.current_step == "plan"  # untouched — resumes where it was blocked
    events_this_tick = (await event_types(world))[len(before) :]
    assert events_this_tick == ["unblocked"]
    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.step_run_id is None  # no run is the subject
    assert unblocked.details.get("attempt") == 2  # the LATEST (exhausted) run's attempt
    # No spawn on this tick: un-blocking is its own transition (`reconcile_ticket`'s "at most one
    # transition per tick" rule) — the step's own dispatch runs on the FOLLOWING tick.
    assert len(world.adapter.spawns) == spawns_before

    # Every successful unblock posts ONE acknowledgment comment naming
    # the step and the attempt about to be tried.
    calls = dict(world.projector.calls)
    assert calls["comment"] == "Unblocked: retrying `plan` (attempt 3) shortly."

    # The next tick's ordinary `_agent_step` dispatch does the actual work: a genuinely fresh
    # attempt (3) — one past the exhausted `maxAttempts: 2`. `_agent_step`'s FAILED-status branch
    # gates a retry through `_retry_ready`, which requires BOTH a retry policy
    # (`_retry_policy(...) is not None` — the condition `_grant_attempt`'s Path-A refusal exists
    # for) AND backoff elapsed (`_retry_due`), never on `max_attempts` again (only
    # `_retry_or_escalate`, at failure time, checks that) — so this attempt is real, not a
    # special case invented for un-block. It is still subject to the SAME backoff as any other
    # retry, though — `_retry_due` reads `latest.ended_at`, not anything about un-block — so a
    # tick immediately after this one would still see no spawn; the clock has to clear the
    # escalating attempt's own backoff window first, exactly as it would for an ordinary retry.
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    plan_runs = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert [r.attempt for r in plan_runs] == [1, 2, 3]
    assert plan_runs[-1].status == StepRunStatus.RUNNING
    assert len(world.adapter.spawns) == spawns_before + 1


async def test_unblock_books_the_attempt_it_grants(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`UNBLOCKED` must name the attempt it GRANTS, not only the one that failed.

    The ticket page groups the timeline into one block per `step#attempt`
    (`ui/.../activity.ts::segmentsFrom`), keyed off the event's own attempt number. Filed under
    the exhausted attempt alone, the un-block row joins the FAILED block and the granted attempt
    has nothing on the page at all until `step_started` lands — a full poll interval at best, and
    the rest of the failed run's backoff at worst (`_agent_step` still gates on `_retry_due`,
    which reads `ended_at + backoff`, so it is 10m here). Through that window the graph reads
    "running, nothing wrong" while the newest block on the timeline is the failure: the two-nows
    split `step_entered` closed for a step CHANGE, reopened for an attempt change.

    `retry_scheduled` already solved this and is the shape copied here: `attempt` keeps its audit
    meaning, `next_attempt` opens the block, and `due_at` is what the block counts down to. The
    due time is measured from the FAILED RUN'S END, never from the un-block, because that is what
    `_retry_due` reads — a human who unblocks two minutes after the escalation still waits out
    the remaining eight.
    """
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)
    escalated_at = world.clock.now  # the escalating tick stamped `ended_at` with this

    # Four minutes pass before the human notices. The due time must not move with them, which is
    # what tells `ended_at + backoff` apart from `tick.now + backoff` — the two are the same
    # instant on a tick that un-blocks the moment the escalation lands.
    world.clock.advance(timedelta(minutes=4))
    _remove_blocked_label(world)
    await run_tick(world)

    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.details["attempt"] == 2  # unchanged: the LATEST (exhausted) run's attempt
    assert unblocked.details["next_attempt"] == 3
    assert unblocked.details["due_at"] == (escalated_at + timedelta(minutes=10)).isoformat()

    # The prediction is redeemed: the attempt the next tick actually starts is the one booked
    # here, so the un-block row and that run share one block on the page.
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    plan_runs = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert plan_runs[-1].attempt == unblocked.details["next_attempt"]


async def test_unblock_with_unmet_preconditions_books_the_attempt_but_no_due_time(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Path B books its block, and deliberately no countdown.

    The wait here is not backoff-bound: `_start_attempt` declines on the precondition every tick
    however long the backoff has been clear, so a clock ticking down to a time nothing happens at
    would be a second, more prominent version of the mistake the acknowledgment comment's own
    wording avoids ("Lead with the true cause"). The block still opens — the reader needs
    somewhere for the un-block to live — it just says the un-block and stops.
    """
    world = await _build_world(
        maker,
        assignees=("bot-user",),
        config=_process_with_a_branch_guard(f"branch-guard-{uuid4().hex[:8]}"),
    )
    await run_tick(world)  # attempt 1 spawns — no branch pushed yet, precondition still open
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled

    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 2 spawns
    world.github.deliver_spec(world.issue_number)  # the branch that wedges the precondition
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent died mid-task")
    await run_tick(world)  # attempt 2 fails -> escalate

    _remove_blocked_label(world)
    await run_tick(world)

    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.details["next_attempt"] == 3
    assert unblocked.details["due_at"] is None


async def test_unblock_refuses_when_no_retry_policy_is_configured(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Path A: a process config with no retry policy at all
    (`defaults.retry` and the step's own `retry` both unset — this parses fine) makes
    `_agent_step`'s FAILED branch decline forever (`retry is not None` never holds). Left
    unhandled, un-blocking such a ticket would leave it RUNNING forever with the label gone, no
    event, and no spawn. `_grant_attempt` must refuse instead:
    the ticket stays BLOCKED, one comment names the step and the reason, and the SAME tick's
    projection restores `factorial:blocked` from `_steady_label`'s BLOCKED fallback."""
    world = await _build_world(
        maker,
        assignees=("bot-user",),
        config=_process_without_retry(f"no-retry-{uuid4().hex[:8]}"),
    )
    await run_tick(world)  # attempt 1 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> escalates immediately (no retry policy at all)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels

    _remove_blocked_label(world)
    before_events = await event_types(world)
    before_spawns = len(world.adapter.spawns)
    world.projector.calls.clear()
    await run_tick(world)  # refused: stays BLOCKED, one comment, label restored

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED  # never flipped to RUNNING
    assert await event_types(world) == before_events  # no `unblocked` — nothing transitioned
    assert len(world.adapter.spawns) == before_spawns  # never even considered spawning

    calls = dict(world.projector.calls)
    assert "refused" in calls["comment"]
    assert "plan" in calls["comment"]
    assert "no retry rule" in calls["comment"]
    assert "the process file in `.factorial/processes/`" in calls["comment"]
    assert calls["set_labels"] == frozenset({"factorial:plan", "factorial:blocked"})
    assert "factorial:blocked" in world.github.issue.labels  # restored, not just claimed

    # Re-comment boundedness: the label is back, so `reconcile_ticket`'s own stays-blocked
    # early-return takes over on the NEXT tick — `_grant_attempt` (and its comment) is never reached
    # again for the SAME human gesture.
    world.projector.calls.clear()
    before_events = await event_types(world)
    await run_tick(world)
    assert world.projector.calls == []
    assert await event_types(world) == before_events


async def test_unblock_names_an_unmet_precondition_and_becomes_fixable(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Path B: a retry policy DOES exist, so un-blocking proceeds — but
    `_start_attempt`'s own precondition guard (`not: branch_exists(...)`, a shape the real sample
    no longer carries — `_process_with_a_branch_guard` still exercises it) would
    otherwise decline silently forever once a failed attempt has already pushed a branch. The
    acknowledgment comment names the unmet predicate instead of
    trying to guess whether it is "stuck" or merely "waiting" — visibility is the fix. Once a
    human actually clears it (deletes the stale branch), the next tick spawns for real."""
    world = await _build_world(
        maker,
        assignees=("bot-user",),
        config=_process_with_a_branch_guard(f"branch-guard-{uuid4().hex[:8]}"),
    )
    await run_tick(world)  # attempt 1 spawns — no branch pushed yet, precondition still open
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled (1 < maxAttempts: 2)

    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 2 spawns — still no branch, precondition still open
    world.github.deliver_spec(world.issue_number)  # the agent pushed a branch before dying
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent died mid-task")
    await run_tick(world)  # attempt 2 fails -> escalate (2 == maxAttempts), branch left behind

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED

    _remove_blocked_label(world)
    world.projector.calls.clear()
    await run_tick(world)  # unblock PROCEEDS (a retry policy exists) — but names the unmet guard

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING  # Path B does not refuse, only Path A does
    assert "unblocked" in await event_types(world)

    calls = dict(world.projector.calls)
    assert "plan" in calls["comment"]
    assert "still waiting for no branch starting with spec/12-" in calls["comment"]
    assert "branch_exists" not in calls["comment"]

    # The dispatch is genuinely wedged until a human acts: a tick right now still spawns
    # nothing — `_start_attempt`'s own precondition guard declines silently, exactly the
    # mechanism that made Path B dangerous before this comment existed.
    world.clock.advance(timedelta(minutes=11))
    spawns_before = len(world.adapter.spawns)
    await run_tick(world)
    assert len(world.adapter.spawns) == spawns_before

    # The human's actual fix: delete the stale branch, satisfying `not: branch_exists(...)`.
    world.github.branches = [b for b in world.github.branches if not b.name.startswith("spec/")]
    await run_tick(world)
    plan_runs = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert [r.attempt for r in plan_runs] == [1, 2, 3]
    assert plan_runs[-1].status == StepRunStatus.RUNNING
    assert len(world.adapter.spawns) == spawns_before + 1


async def test_action_step_unblock_resumes_with_a_fresh_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_grant_attempt`'s shared machinery covers `kind: action` too —
    `_action_step`'s FAILED branch is the identical `retry is not None and _retry_due(...)` gate
    `_agent_step` uses, and `ActionStep` has no `preconditions` field at all, so only the normal
    (Path A/B-free) proceed path is reachable here. Pinning it working end to end, not just by
    code-reading."""
    world = await _build_world(
        maker,
        assignees=("bot-user",),
        config=_process_with_action(f"action-unblock-{uuid4().hex[:8]}"),
    )
    await run_tick(world)  # attempt 1 fails immediately (no branch ever observed)
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)  # attempt 2 fails -> escalate (2 == maxAttempts), BLOCKED

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert ticket.current_step == "fix"

    _remove_blocked_label(world)
    before = await event_types(world)
    world.projector.calls.clear()
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED, acknowledgment comment

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.current_step == "fix"
    assert (await event_types(world))[len(before) :] == ["unblocked"]
    calls = dict(world.projector.calls)
    assert calls["comment"] == "Unblocked: retrying `fix` (attempt 3) shortly."

    # The FOLLOWING tick's ordinary `_action_step` dispatch does the actual work: a genuinely
    # fresh action attempt (3) — one past the exhausted `maxAttempts: 2`. It fails too (the
    # action's own preconditions are never satisfiable in this fixture) and, since attempt 3 is
    # already past `maxAttempts`, re-escalates immediately in the SAME tick — the identical
    # re-block cycle the agent-step tests exercise, pinned here for actions.
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)
    action_runs = [r for r in await step_runs(world) if r.step_name == "fix"]
    assert [r.attempt for r in action_runs] == [1, 2, 3]
    assert action_runs[-1].status == StepRunStatus.FAILED
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


async def test_blocked_ticket_with_label_still_present_stays_blocked(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`reconcile_ticket`'s stays-blocked early-return must still hold when nothing has changed:
    the label the machine wrote is still there, so the ticket keeps waiting — no event, no spawn."""
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert "factorial:blocked" in world.github.issue.labels

    before_events = await event_types(world)
    before_spawns = len(world.adapter.spawns)
    await run_tick(world)

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert await event_types(world) == before_events
    assert len(world.adapter.spawns) == before_spawns


async def _ask_to_unblock(world: World) -> int:
    """What `factorial_ticket_unblock` does: the service, for an MCP caller. Returns the
    request's event id."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        result = await request_unblock(
            session, ticket, Actor(by="ops@example.com", via=RequestedVia.MCP)
        )
    assert result.status == "requested"
    return [r.id for r in await event_rows(world) if r.type == EventType.UNBLOCK_REQUESTED][-1]


async def test_an_unblock_request_releases_an_escalation_the_label_still_holds(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The request form of removing `factorial:blocked`. If the loop misses it, the press is
    recorded and nothing answers it: the label is still there, the stays-blocked check keeps the
    ticket parked, and the caller hears only that it was requested."""
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)
    assert "factorial:blocked" in world.github.issue.labels

    request_id = await _ask_to_unblock(world)
    world.projector.calls.clear()
    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.RUNNING
    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.details["via"] == "mcp"
    assert unblocked.details["request_id"] == request_id
    assert unblocked.details["next_attempt"] == 3
    assert "factorial:blocked" not in world.github.issue.labels
    assert dict(world.projector.calls)["comment"] == (
        "Unblocked: retrying `plan` (attempt 3) shortly."
    )


async def test_an_unblock_request_older_than_the_escalation_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A press recorded while the label's own un-block was landing outlives that release. The
    granted attempt fails and the ticket escalates again. Honouring the old press now would
    start a paid attempt against an escalation nobody has read."""
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)
    _remove_blocked_label(world)
    await run_tick(world)  # un-blocked by the label
    async with world.sessions() as session:
        # Written directly: the service, asked now, meets a running ticket and refuses.
        stale = (
            await append(
                session,
                type=EventType.UNBLOCK_REQUESTED,
                project_id=world.project_id,
                ticket_id=world.ticket_id,
                requested_by="ops@example.com",
                requested_via="dashboard",
                step="plan",
                attempt=2,
            )
        ).id
    world.clock.advance(timedelta(minutes=11))
    await run_tick(world)  # attempt 3 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="still broken")
    await run_tick(world)  # attempt 3 fails -> escalates again
    spawns = len(world.adapter.spawns)
    world.projector.calls.clear()

    await run_tick(world)

    assert (await ticket_row(world)).status == TicketStatus.BLOCKED
    refused = [row.details for row in await event_rows(world) if row.type == "unblock_refused"]
    assert [(d["request_id"], d["reason"]) for d in refused] == [(stale, "stale")]
    assert world.projector.calls == []
    assert "factorial:blocked" in world.github.issue.labels

    await run_tick(world)
    assert len(world.adapter.spawns) == spawns
    assert [row.type for row in await event_rows(world)].count("unblocked") == 1


async def test_unblock_then_a_second_failure_re_escalates(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The full cycle: un-block grants exactly one fresh attempt: if it fails too, the ticket
    re-blocks with a fresh escalate comment — correct behavior (the human's fix did not work),
    not a bug. `Retry.backoff` between attempts is what keeps this from being a tight loop; this
    re-block happens on the FIRST failure after un-block, with no bonus retry in between, because
    `_retry_or_escalate` compares the exhausted attempt count against `max_attempts` again."""
    world = await _build_world(maker, assignees=("bot-user",))
    await _escalate_the_plan_step(world)

    _remove_blocked_label(world)
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED
    world.clock.advance(timedelta(minutes=11))  # clear attempt 2's own backoff window
    await run_tick(world)  # fresh attempt 3 spawns

    world.adapter.complete(RunStatus.FAILED, failure_reason="still broken")
    world.projector.calls.clear()
    await run_tick(world)  # attempt 3 fails -> re-escalates immediately, no bonus retry

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    plan_runs = [r for r in await step_runs(world) if r.step_name == "plan"]
    assert [r.attempt for r in plan_runs] == [1, 2, 3]
    assert plan_runs[-1].status == StepRunStatus.FAILED

    types = await event_types(world)
    assert types[-2:] == ["agent_failed", "escalated"]  # no retry_scheduled in between

    calls = dict(world.projector.calls)
    assert "factorial:blocked" in calls["set_labels"]
    assert "failed after 3 attempt(s): still broken" in calls["comment"]

    # Re-blocked and waiting again: a further tick with the label present changes nothing.
    before = await event_types(world)
    await run_tick(world)
    assert await event_types(world) == before


async def test_unblock_after_permanent_failure_reescalates(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A permanently-failed, BLOCKED ticket whose label a human removes
    resumes — the process HAS a retry policy, so `_grant_attempt`'s Path-A refusal
    (`test_unblock_refuses_when_no_retry_policy_is_configured`) never fires, since that guard only
    inspects `StepRunStatus` (FAILED), never WHY the run failed — into one bonus attempt against
    the SAME manifest/ceiling/App, which fails for the IDENTICAL reason and re-escalates visibly
    rather than wedging. Mirrors `test_unblock_then_a_second_failure_re_escalates`'s ordinary-
    failure version, for a permanent one instead."""
    world = await _build_world(maker, assignees=("bot-user",))
    permission_denied = PermissionNotAllowed(
        "agent 'plan-agent' requests permission(s) this project's ceiling does not permit"
    )
    world.deps = replace(world.deps, resolve_agent=_raising_resolve_agent(permission_denied))

    await run_tick(world)  # attempt 1: _build_run_spec raises -> FAILED, escalate now -> BLOCKED

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    (run,) = await step_runs(world)
    assert run.attempt == 1

    _remove_blocked_label(world)
    await run_tick(world)  # un-block: BLOCKED -> RUNNING, current_step untouched

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.current_step == "plan"
    assert len(await step_runs(world)) == 1  # un-block itself starts nothing

    # The bonus attempt still waits on the SAME backoff any other retry would (`_retry_due` reads
    # `latest.ended_at`; un-block does not special-case a permanent failure's own backoff).
    world.clock.advance(timedelta(minutes=11))
    before = await event_types(world)
    await run_tick(world)  # attempt 2 spawns... and immediately fails+escalates again

    runs = await step_runs(world)
    assert [r.attempt for r in runs] == [1, 2]
    assert runs[-1].status == StepRunStatus.FAILED
    events_this_tick = (await event_types(world))[len(before) :]
    assert events_this_tick == ["step_started", "agent_failed", "escalated"]
    assert "retry_scheduled" not in events_this_tick  # re-escalated, not re-queued

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert world.adapter.spawns == []  # never once reached the substrate


async def test_gate_blocked_ticket_unblocked_reopens_the_gate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_reject`'s no-onReject arm blocks from a gate context, `current_step` still the gate
    (unlike the rerunStep case, which moves `current_step` back to the step being reworked). This
    proves un-block from THAT state does not wedge: it reopens a fresh gate attempt, the same
    "rework is a new attempt" path `test_gate_rejection_loops_back_with_a_new_attempt` exercises
    for the rerunStep case, minus the step change — and it re-evaluates the CURRENT observed PR,
    not the stale one that caused the rejection, so a human who actually fixed the PR sees the
    gate wait for a fresh review rather than reject on stale state."""
    world = await _build_world(
        maker, config=_process_without_gate_reject(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected, no onReject configured -> blocks

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    assert ticket.current_step == "spec_gate"
    gate = next(r for r in await step_runs(world) if r.step_name == "spec_gate")
    assert gate.status == StepRunStatus.REJECTED
    assert gate.attempt == 1

    # The human's actual fix: a new commit on the PR — and only then remove the label the
    # machine wrote. The changes-requested review stays the reviewer's latest, as GitHub keeps it
    # after new commits, but the rejection already answered it.
    world.github.push(101, "sha-2")
    _remove_blocked_label(world)
    world.projector.calls.clear()
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.RUNNING
    assert ticket.current_step == "spec_gate"  # untouched — resumes at the gate
    # A gate unblock posts an acknowledgment comment too — `GateStep`
    # has no retry-policy/precondition concept to refuse or name, so it is always this simple one.
    calls = dict(world.projector.calls)
    assert calls["comment"] == "Unblocked: reopening `spec_gate` for a fresh review."

    # The FOLLOWING tick's `_gate_step` dispatch sees the latest run REJECTED — already one of
    # the statuses it treats as "open a new gate attempt" — and does exactly that. The PR is
    # unapproved and its one change request answered, so the fresh attempt genuinely waits rather
    # than being re-rejected in the same tick.
    await run_tick(world)
    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [r.attempt for r in gates] == [1, 2]
    assert gates[-1].status == StepRunStatus.WAITING
    assert (await ticket_row(world)).status == TicketStatus.WAITING_GATE

    # The un-block is filed under the attempt it grants — the one `_open_gate` just created —
    # so the row and the gate that follows it share one block on the ticket page. No `due_at`:
    # a gate has no retry policy to wait on and reopens on the very next tick, so a countdown
    # would be counting down to nothing.
    unblocked = next(row for row in await event_rows(world) if row.type == "unblocked")
    assert unblocked.details["next_attempt"] == gates[-1].attempt
    assert unblocked.details["due_at"] is None


async def test_gate_unblocked_without_a_real_fix_re_blocks_in_the_same_follow_up_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The mirror of the agent-side re-escalation test: if the human only removed the label and
    never actually fixed the PR, so the reviewer requests changes again, `_gate_step` opens the
    fresh attempt and immediately re-evaluates it against that new request in the one follow-up
    tick — no onReject, so it re-rejects and re-blocks right there. Not a wedge (the ticket does
    not hang RUNNING forever) and not a tight loop (a further tick with the label back changes
    nothing, same as every other blocked ticket)."""
    world = await _build_world(
        maker, config=_process_without_gate_reject(f"gate-reject-{uuid4().hex[:8]}")
    )
    await advance_to_gate(world)
    world.github.request_changes("reviewer-1")
    await run_tick(world)  # rejected, no onReject configured -> blocks

    _remove_blocked_label(world)
    await run_tick(world)  # BLOCKED -> RUNNING, UNBLOCKED
    world.github.request_changes("reviewer-1")  # a new review: the first one is answered
    await run_tick(world)  # _gate_step opens attempt 2, then re-rejects it in the same tick

    ticket = await ticket_row(world)
    assert ticket.status == TicketStatus.BLOCKED
    gates = [r for r in await step_runs(world) if r.step_name == "spec_gate"]
    assert [r.attempt for r in gates] == [1, 2]
    assert gates[-1].status == StepRunStatus.REJECTED

    types = await event_types(world)
    assert types[-2:] == ["gate_rejected", "escalated"]

    # Re-blocked and waiting again: a further tick with the label present changes nothing.
    before = await event_types(world)
    await run_tick(world)
    assert await event_types(world) == before


def test_a_failure_summary_is_its_reasons_first_paragraph() -> None:
    """Raw output an adapter appends after a blank line stays under Details: the summary on the
    timeline and in `{failure_summary}` is the sentence before it, on one line."""
    reason = "The Claude CLI stopped before it reported a result (exit 1).\n\nTraceback …\n  line"
    assert first_paragraph(reason) == "The Claude CLI stopped before it reported a result (exit 1)."
    assert first_paragraph("one\nline") == "one line"
    # A runtime's machine code stays in the reason for code to match, not in the sentence.
    assert first_paragraph("host_lost: The server restarted.") == "The server restarted."
    assert first_paragraph("misconfiguration: x") == "misconfiguration: x"


@pytest.mark.parametrize(
    ("span", "said"),
    [
        (timedelta(hours=1), "1 hour"),
        (timedelta(minutes=30), "30 minutes"),
        (timedelta(minutes=90), "90 minutes"),
        (timedelta(seconds=45), "45 seconds"),
    ],
)
def test_a_duration_reads_as_a_person_says_it(span: timedelta, said: str) -> None:
    assert spoken(span) == said

