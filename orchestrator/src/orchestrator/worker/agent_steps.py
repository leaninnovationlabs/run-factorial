"""Agent steps: dispatch, the spawn path (`_start_attempt`), the RunSpec build, and run
polling."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import (
    AgentStep,
    expand,
)
from orchestrator.db.models import (
    Event,
    Runtime,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.factorial import ResolvedAgent
from orchestrator.factorial.runtime_policy import (
    SettingsInvalid,
    egress_allowlist,
    runtime_permitted,
)
from orchestrator.factorial.runtimes import (
    ISOLATION_PLACES,
    RuntimeDetailsInvalid,
    bedrock_model_of,
    declared_kind_for,
    isolation_of,
    requires_stored_credential,
    row_registry_kind,
    template_of,
)
from orchestrator.github import LABEL_BLOCKED
from orchestrator.predicates import Unmet, evaluate
from orchestrator.worker.acceptance import (
    _accept,
    _delivered_branch,
    _write_artifacts,
    accepted,
    postconditions_met,
)
from orchestrator.worker.attachments import attachment_details
from orchestrator.worker.branches import branch_heads, declares_files
from orchestrator.worker.context import ReconcileDeps
from orchestrator.worker.failures import (
    _PERMANENT_CONFIG_ERRORS,
    _fail_permanently,
    _fail_run,
    _permanent_spawn,
    spoken,
)
from orchestrator.worker.inputs import (
    RetryBriefing,
    _resolve_inputs,
    release_run_credential,
    run_secrets,
    sandbox_delivery,
)
from orchestrator.worker.interrupts import (
    _comments_since,
    _honour_interrupt,
    _interrupt_pending,
    _latest_park,
)
from orchestrator.worker.leases import renew
from orchestrator.worker.outputs import observe_outputs
from orchestrator.worker.previews import carry_preview
from orchestrator.worker.pricing import record_usage
from orchestrator.worker.rework import Adopt, Skip, SkipReason, _resume_plan
from orchestrator.worker.runtime_adapters import RuntimeRefused, workspace_key
from orchestrator.worker.sessions import _active_since, _poll_session, _spoken, _terminal
from orchestrator.worker.templates import _expected_artifacts
from orchestrator.worker.tick_context import (
    _FAILED_STATUSES,
    _RUN_STATUS,
    Launch,
    LeaseLost,
    _agent_id,
    _apply_outcome,
    _current,
    _insert_run,
    _next_attempt,
    _observed,
    _retry_ready,
    _runs_for,
    _Tick,
    _timeout,
    note_next_step_waiting,
)
from runtime.contract import (
    AgentPrompts,
    AgentRuntimeError,
    AgentSkill,
    McpServerSpec,
    McpTransport,
    NoBranchHeld,
    ParkReason,
    PreviewHost,
    RunInput,
    RunResult,
    RunSpec,
    RunStatus,
    RuntimeAdapter,
    SessionUnavailable,
    ShownFile,
    SkillScope,
    SpawnError,
)
from runtime.contract import Workspace as RunWorkspace

logger = logging.getLogger(__name__)


async def _agent_step(tick: _Tick, step: AgentStep) -> None:
    """Start, poll or retry this step's current attempt."""
    runs = await _runs_for(tick.session, tick.ticket, step.name)
    latest = _current(runs)

    if latest is None:
        await _start_attempt(
            tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
        )
        return

    status = StepRunStatus(latest.status)
    if status in (StepRunStatus.RUNNING, StepRunStatus.IDLE):
        await _poll_run(tick, step, latest)
        return
    if status == StepRunStatus.SUCCEEDED:
        # The ticket is sitting on a step it already completed: a gate sent it back here
        # (`gate.onReject.rerunStep`). Rework is a new attempt, not a re-read of the old one,
        # though it continues the old one's session (`worker.rework`).
        await _start_attempt(
            tick,
            step,
            attempt=await _next_attempt(tick.session, tick.ticket.id, step.name),
            rework_of=latest,
        )
        return
    if status in _FAILED_STATUSES:
        # Escalation already fired in the tick that recorded the failure, so reaching here means a
        # retry is still owed; it starts once the backoff has elapsed, or when the dashboard asks.
        if await _retry_ready(tick, step, latest):
            await _start_attempt(
                tick, step, attempt=await _next_attempt(tick.session, tick.ticket.id, step.name)
            )
        return


async def _start_attempt(
    tick: _Tick,
    step: AgentStep,
    *,
    attempt: int,
    rework_of: StepRun | None = None,
    briefing: RetryBriefing | None = None,
) -> StepRun | None:
    """Check preconditions, then create the `(ticket, step, attempt)` row and spawn.
    `rework_of` is the attempt a gate sent back, or a person retried with a `briefing`, whose
    session the new one continues. Returns the inserted row, also when it then failed
    permanently, or None when no row was inserted."""
    if step.preconditions is not None:
        # `_grant_attempt` evaluates this same guard so its acknowledgment can name an unmet
        # precondition — keep the two sites in step. Their error behaviour differs: this one
        # aborts through `_observed`, `_grant_attempt` catches locally (see its evaluate site).
        result = await _observed(
            evaluate(step.preconditions, tick.ctx),
            what=f"step {step.name} attempt {attempt}: preconditions",
        )
        if not result.value:
            # Not one event per tick: `note_next_step_waiting` writes once per entry, and only
            # after a forced step.
            logger.debug(
                "ticket %s step %s attempt %d: preconditions not met (%s)",
                tick.ticket.external_key,
                step.name,
                attempt,
                result.reason,
            )
            await note_next_step_waiting(tick, step.name, Unmet.of(result))
            return None

    placement = await _placement(tick)
    if (refusal := await _spawn_refusal(tick, placement)) is not None:
        await _block_on_refusal(tick, step, refusal)
        return None

    run = StepRun(
        ticket_id=tick.ticket.id,
        step_name=step.name,
        kind=StepKind.AGENT,
        attempt=attempt,
        agent_id=await _agent_id(tick.session, tick.ticket.project_id, step.agent),
        status=StepRunStatus.RUNNING,
        started_at=tick.now,
    )
    if not await _insert_run(tick, run):
        return None

    try:
        # Placed again only when the row did not read before the insert: this raises why.
        placement = placement or await _place(tick)
        spec = await _build_run_spec(
            tick,
            step,
            attempt,
            placement,
            run.id,
            entered_by=run.entered_by,
            extra_inputs=(briefing.failure,) if briefing is not None else (),
        )
    except (LeaseLost, AgentRuntimeError):
        await release_run_credential(tick, run.id)
        # A structural guard, not a note. Both are `RuntimeError` subclasses, so without this the
        # bare `RuntimeError` in `_PERMANENT_CONFIG_ERRORS` is protected only by call-site
        # ordering — a future reorder (a lease renewal inside `_build_run_spec`, a helper that
        # spawns) would silently report a lost lease or an infrastructure spawn fault as
        # "misconfiguration, escalate now". Re-raise unconditionally.
        raise
    except _PERMANENT_CONFIG_ERRORS as exc:
        # Caught, not propagated: the tick's rollback would erase `run` and its `STEP_STARTED`
        # and spin at attempt 1 for ever (`failures.py`'s banner).
        #
        # Session purity: every member is a Python-level validation, HTTP-response or crypto
        # failure, never a failed SQL statement, so the session stays usable. `resolve_agent`'s
        # pin-write `flush()` always succeeds before any of these can raise, so there is no
        # partial write to undo — but the pin then PERSISTS through the failure. For a
        # `ConfigError`, whose remedy is a repo edit at the sha the agent is already pinned to,
        # an operator must re-register the process version before a new attempt sees the fix.
        await _fail_permanently(tick, step, run, exc)
        return run
    except Exception:
        await release_run_credential(tick, run.id)
        raise
    # The one write site that knows `runtime_kind`: the resolved agent's manifest, already folded
    # into `RunSpec.runtime_options`. A gate, action or check row leaves it NULL.
    kind = spec.runtime_options.get("kind")
    run.runtime_kind = kind if isinstance(kind, str) else None
    # The result never names the model, and `_poll_run` prices a codex run per model.
    run.model = spec.model
    runtime = placement.runtime
    # Stamped before the spawn so every later verb's `adapter_for_run` keys off the same column.
    run.runtime_id = runtime.id

    # Decision 41: an ownership assertion immediately before the blocking call, NOT a horizon
    # restart for the spawn about to happen — `leases._expires_in` computes Postgres `now()`, the
    # enclosing transaction's start time, and the renewed row stays invisible to other sessions
    # until this tick commits after `spawn()` returns. The lease that has to span everything from
    # `acquire_leases` to here is the acquire-time one (settings.py's `lease_seconds` carries the
    # floor arithmetic), and what actually stops a concurrent second spawn is the
    # `(ticket_id, step_name, attempt)` unique constraint above, not this lease.
    #
    # A `False` is still a worthwhile early exit: this runner no longer owns the ticket, and it
    # must never spawn against someone else's. Raising `LeaseLost` uncaught unwinds the whole tick
    # via `session_scope`'s rollback, erasing `run` and its `STEP_STARTED` event. That is wanted: a
    # row with no `runtime_run_id` has no adapter behind it, so `_poll_run` could never advance it
    # and it would strand in RUNNING forever. Erased, the next tick's owner starts clean through
    # `_agent_step`'s `latest is None` branch. No event marks the abort — a lost lease is not a
    # state transition — but `worker.loop._reconcile_leased` logs the exception.
    #
    # Two different scopes, easily confused: `worker/singleton.py`'s advisory lock (decision 40)
    # is a process-level fence (at most one worker at all); this renewal is an advisory
    # cross-process ownership CHECK that detects a steal but prevents nothing.
    if not await renew(tick.session, tick.ticket.id, tick.lease_owner, tick.lease_ttl):
        raise LeaseLost(
            f"ticket {tick.ticket.external_key} step {step.name} attempt {attempt}: lease "
            f"renewal failed for owner {tick.lease_owner!r}, so the spawn stopped"
        )

    # `adapter.spawn(spec)` is deliberately NOT in `_PERMANENT_CONFIG_ERRORS`: matching
    # `SpawnError` by class would catch every cause alike (`busy`, `duplicate`, `checkout_failed`
    # and a wrong manifest are one class). Instead the adapter says which it is —
    # `SpawnError.reason` is a closed vocabulary the contract owns — so this site reads a fact:
    #
    # * a permanent reason (`config` / `credential` / `workspace`) fails `run` and escalates on
    #   this tick, identically to a `ConfigError`.
    # * `infrastructure`, including the default a reason-less `SpawnError` carries, re-raises
    #   unchanged: the tick rolls back, no row, no event, next tick retries clean.
    #
    # `AdapterUnavailable` and `IsolationError` are `AgentRuntimeError`s but not `SpawnError`s,
    # carry no reason, and keep propagating. Never widen this except to `AgentRuntimeError`.
    adapter = placement.adapter
    assert adapter is not None  # `_build_run_spec` raised `refused`
    # An E2B run's template build, whose name carries the CLI version it installed: the row
    # can be re-pointed later, so only the event says what this run booted.
    template = template_of(runtime.details) if isolation_of(runtime.details) == "e2b" else None
    pin = _cli_pin(template, adapter)
    plan = (
        await _resume_plan(
            tick, rework_of, runtime, pin, mode="retry" if briefing is not None else "rework"
        )
        if rework_of is not None
        else None
    )
    # A fresh start is told what the failed attempt delivered; a continued session has it.
    delivered = briefing.delivered if briefing is not None else None
    fresh = (
        spec.model_copy(update={"inputs": (*spec.inputs, delivered)})
        if delivered is not None
        else spec
    )
    if isinstance(plan, Adopt):
        spec = spec.model_copy(update={"resume_from": plan.resume_from})
    else:
        spec = fresh
    # Read before the spawn: a read that fails after it costs the run it just launched.
    heads = await branch_heads(tick, step) if declares_files(step) else None
    try:
        try:
            handle = await adapter.spawn(spec)
        except SessionUnavailable as exc:
            if not isinstance(plan, Adopt):
                raise
            # Caught before `_permanent_spawn`, which escalates its WORKSPACE reason: the
            # session only made the rework cheaper, and a fresh one does the same work.
            reason = SkipReason.NO_BRANCH if isinstance(exc, NoBranchHeld) else SkipReason.NOT_KEPT
            plan = Skip(reason, plan.previous_step_run_id)
            spec = fresh
            handle = await adapter.spawn(spec)
    except SpawnError as exc:
        await release_run_credential(tick, run.id)
        if not _permanent_spawn(exc):
            raise
        # `run` is flushed inside the SAVEPOINT above, so failing it here keeps it: the row, both
        # events and the escalation commit together. `runtime_run_id` stays NULL because nothing
        # was launched — `_poll_run` must never find this row pollable.
        await _fail_permanently(tick, step, run, exc)
        return run
    except Exception:
        await release_run_credential(tick, run.id)
        raise
    # First, before anything that can still raise: from here a rollback erases the only record
    # of a live run, and the pass's settle cancels it (decision 87).
    if tick.launches is not None:
        tick.launches.record(
            Launch(
                adapter=adapter,
                runtime_run_id=handle.run_id,
                step_run_id=run.id,
                ticket_id=tick.ticket.id,
                step=step.name,
                attempt=attempt,
                repo=tick.project.github_repo,
                forge=tick.bindings.forge if tick.bindings is not None else None,
            )
        )
    run.runtime_run_id = handle.run_id
    adopted = plan if isinstance(plan, Adopt) else None
    await tick.emit(
        EventType.AGENT_SPAWNED,
        run=run,
        step=step.name,
        attempt=attempt,
        agent=step.agent,
        run_id=handle.run_id,
        branch=spec.workspace.branch,
        model=spec.model,
        # `run_secrets` minted this run's token no earlier than `tick.now`; an adapter never
        # mints (§7.1).
        minted_at=tick.now.isoformat(),
        cli_pin=pin,
        # The adapter that ran it, which `step_run.runtime_kind` (the family alone) cannot say,
        # and the row can be re-pointed later.
        runtime=row_registry_kind(runtime.type, runtime.details),
        # Names only, never the config: the event log answers "what tools did this run have?"
        # without becoming a place credentials can land (design.md §5).
        mcp=[server.name for server in spec.mcp],
        # References, never bytes: what the run was shown, answerable from the log (§7.11).
        **attachment_details(spec.attachments, spec.missing_attachments),
        # Read back by the keep rule (`events.workspaces`), which parses no snapshot: whether
        # the sandbox was created public for this step's preview, and the workspace the run
        # works in, its own unless it adopted the previous attempt's.
        public=spec.public,
        workspace_run_id=adopted.workspace_run_id if adopted is not None else handle.run_id,
        **({"branch_heads": heads} if heads is not None else {}),
        **(
            {
                "resumed_from": {
                    "step_run_id": adopted.previous_step_run_id,
                    "run_id": adopted.resume_from.run_id,
                    # `rework_of` is set whenever a session was adopted.
                    "attempt": rework_of.attempt if rework_of is not None else None,
                }
            }
            if adopted is not None
            else {}
        ),
        **(await _log_offset(adapter, handle.run_id)),
        **(await _terminal(adapter, handle.run_id)),
        # Which registry row, by the name the process wrote and the type it had — the id on the
        # row is SET NULL on delete, so the event is what keeps answering after one goes.
        runtime_slug=runtime.slug,
        runtime_type=runtime.type,
        **({"template": template} if template else {}),
    )
    if isinstance(plan, Skip):
        await tick.emit(
            EventType.SESSION_RESUME_SKIPPED,
            run=run,
            step=step.name,
            attempt=attempt,
            reason=plan.reason,
            previous_step_run_id=plan.previous_step_run_id,
        )
    if adopted is not None:
        await carry_preview(
            tick.session,
            tick.ticket,
            adapter,
            previous_id=adopted.previous_step_run_id,
            run=run,
            now=tick.now,
        )
    # After the spawn, not before: a refused or lost-lease spawn must not announce a start that
    # never happened, and the idempotency loser returned long ago. The one outcome that fires on
    # something other than a completed transition — the start IS the transition it reports.
    _apply_outcome(tick, step.on_start, extra={"step": step.name, "attempt": str(attempt)})
    return run


async def _log_offset(adapter: RuntimeAdapter, run_id: str) -> dict[str, int]:
    """Where a run's slice of its session log starts, in bytes of the raw log: one
    log can span a rework. Recorded only; the adapter already gives each run its own slice.

    Unread is better than raised, whatever the failure: the run is launched, and a rolled-back
    tick would spawn it again into the same workspace.
    """
    try:
        state = await adapter.session_state(run_id)
    except Exception:
        logger.warning("run %s: session log offset unread", run_id, exc_info=True)
        return {}
    return {"log_offset": state.log_offset}


def _cli_pin(template: str | None, adapter: RuntimeAdapter) -> str:
    """The CLI version a run boots: an E2B build's name carries it, a host adapter pins it."""
    return template or adapter.pinned_version


async def _runtime_row(tick: _Tick) -> Runtime:
    """The registry row the pinned snapshot's `runtime: <slug>` names.

    Refused permanently (`RuntimeRefused` is a `RuntimeError`) when it names none, which sync
    refuses now, or a slug with no row: the snapshot is pinned, so a retry would read the same
    slug against the same registry, and only an operator creating the runtime — or
    re-registering the process — changes the answer.
    """
    slug = tick.config.runtime
    if slug is None:
        raise RuntimeRefused(
            "process names no runtime: add `runtime: <slug>` with a Runtime from the Runtimes "
            "page and sync, then restart the ticket"
        )
    if tick.deps.load_runtime is None or tick.deps.runtime_adapters is None:
        raise RuntimeError(
            f"process names runtime {slug!r} but this worker has no runtime registry wired "
            "(see orchestrator.worker.context.production_deps)"
        )
    row = await tick.deps.load_runtime(tick.session, tick.project.organization_id, slug)
    if row is None:
        raise RuntimeRefused(
            f"the process names the runtime {slug}, which doesn't exist: create it on the "
            "Runtimes page, or point the process at one that exists"
        )
    return row


@dataclass(frozen=True, slots=True)
class _Placement:
    """The runtime row a spawn goes on and its adapter, resolved once before the attempt's row
    exists. A refusal is kept for `_build_run_spec` to raise where it always did, after the
    agent resolves, so an attempt still fails on the first error in that order."""

    runtime: Runtime
    isolation: str | None = None
    adapter: RuntimeAdapter | None = None
    #: The row's details or the project's permit list did not read: raised before the egress
    #: list is.
    unreadable: SettingsInvalid | RuntimeDetailsInvalid | None = None
    #: Not permitted, no adapter for the row, or its key did not read: raised after it.
    refused: Exception | None = None


async def _place(tick: _Tick) -> _Placement:
    """Raises what `_runtime_row` does, and an `AgentRuntimeError`, which the tick retries."""
    runtime = await _runtime_row(tick)
    try:
        isolation = isolation_of(runtime.details)
        permitted = runtime_permitted(tick.project.settings, runtime.slug, isolation)
    except (SettingsInvalid, RuntimeDetailsInvalid) as exc:
        return _Placement(runtime, unreadable=exc)
    if not permitted:
        # The repo selects, the operator permits (decision 25): a commit to `.factorial/`
        # cannot move a project's code onto a substrate its operator did not name.
        refused = RuntimeRefused(
            f"the runtime {runtime.slug} runs {ISOLATION_PLACES[isolation]}, which this project "
            "doesn't permit: permit it under Permits in the project's Runtime settings"
        )
        return _Placement(runtime, isolation, refused=refused)
    assert tick.deps.runtime_adapters is not None  # `_runtime_row` checked
    try:
        adapter = tick.deps.runtime_adapters.for_runtime(
            runtime, held_key=await workspace_key(tick.session, runtime), repoint=True
        )
    except AgentRuntimeError:
        raise
    except (RuntimeError, RuntimeDetailsInvalid, SettingsInvalid) as exc:
        # A key that does not decrypt included: its attempt fails permanently, saying why.
        return _Placement(runtime, isolation, refused=exc)
    return _Placement(runtime, isolation, adapter)


async def _placement(tick: _Tick) -> _Placement | None:
    """`_place`, or None when there is no row to place on yet."""
    if tick.deps.load_runtime is None or tick.deps.runtime_adapters is None:
        return None
    try:
        return await _place(tick)
    except RuntimeRefused:
        return None


async def _build_run_spec(
    tick: _Tick,
    step: AgentStep,
    attempt: int,
    placement: _Placement,
    run_id: int,
    *,
    entered_by: str | None,
    extra_inputs: tuple[RunInput, ...] = (),
) -> RunSpec:
    """Everything the substrate needs — and nothing about labels, promotion or merging.

    The mapping from `ResolvedAgent` (repo config, read at a pinned sha) onto the runtime
    contract's `AgentPrompts` / `AgentSkill` lives here on purpose: `orchestrator.factorial`
    must not
    import `runtime.contract`, and the runtime must not know how config is resolved. This
    function is the one seam that knows both (CLAUDE.md, division of labor).
    """
    if tick.deps.resolve_agent is None:
        raise RuntimeError(
            "no agent source is configured; the worker cannot build a RunSpec "
            "(see orchestrator.worker.context.production_deps)"
        )
    repo = expand(step.workspace.repo, tick.values)
    resolved = await tick.deps.resolve_agent(
        tick.session, tick.project, step.agent, tick.agent_pins
    )
    manifest = resolved.manifest

    # Refused before the secrets resolution and token mint below: anything refusable without a
    # side effect is refused first, so a capability mismatch never mints a live token to throw
    # away.
    mcp_servers = _mcp_servers(resolved)
    runtime = placement.runtime
    if placement.unreadable is not None:
        raise RuntimeRefused(str(placement.unreadable)) from placement.unreadable
    egress: tuple[str, ...] = ()
    try:
        # Only a sandbox filters egress, so a host run is never refused over this list.
        if placement.isolation != "local_cli":
            egress = egress_allowlist(tick.project.settings)
    except (SettingsInvalid, RuntimeDetailsInvalid) as exc:
        raise RuntimeRefused(str(exc)) from exc
    if placement.refused is not None:
        raise placement.refused
    adapter = placement.adapter
    assert adapter is not None
    if mcp_servers and not adapter.supports_mcp:
        # Reachable on any adapter whose `supports_mcp` is False, so a manifest declaring
        # `mcp.shared`/`mcp.local` that runs there hits this.
        # Without the check the manifest clears the operator allowlist and then dies inside
        # `adapter.spawn()` as a bare `SpawnError`, classified INFRASTRUCTURE — an invisible
        # attempt-1-forever spin the ticket never surfaces, since it never reaches BLOCKED.
        #
        # This reads `supports_mcp`, a capability the contract already asks every adapter to
        # publish; it does not re-implement the adapter's own `_reject_mcp` enforcement, which
        # still fires for any other caller of `spawn()`.
        #
        # No mirror check for `supports_blind_workspace`: the orchestrator cannot construct
        # `isolation="blind"` today, so it would be an untestable dead branch. It belongs with the
        # Complex lane's blind-builder re-entry.
        names = ", ".join(server.name for server in mcp_servers)
        raise RuntimeError(
            f"agent {step.agent!r} declares MCP server(s) ({names}), but this project's runtime "
            "can't connect MCP servers. Remove the mcp block from the agent's manifest, or run "
            "this project on a runtime that supports MCP"
        )

    preview = step.preview
    # Public only where the substrate can serve a preview: a host adapter refuses the request,
    # and a producer-published preview needs no workspace (decision 54).
    public = (
        preview is not None
        and preview.enabled
        and preview.served_by_runtime
        and isinstance(adapter, PreviewHost)
    )
    public_ports = tuple(service.port for service in preview.services) if public and preview else ()

    # Before the mint, for the same reason as the capability checks above: a run whose images
    # this adapter cannot take is refused before a token exists to throw away.
    resolved_inputs = await _resolve_inputs(tick, step, attempt, entered_by)
    if resolved_inputs.attachments and not adapter.supports_attachments:
        raise RuntimeError(
            f"the inputs of step {step.name!r} include {len(resolved_inputs.attachments)} "
            "image(s), but this project's runtime can't pass images to the agent. Run this "
            "project on a runtime that can"
        )

    run_secret_set = await run_secrets(
        tick,
        resolved,
        repo,
        runtime_id=runtime.id,
        run_id=run_id,
    )
    secrets = run_secret_set.values
    # Only a sandbox keeps secrets out of the agent's environment, so only it needs telling.
    secret_routes, secret_env = (
        await sandbox_delivery(tick, resolved, runtime.type, run_secret_set)
        if placement.isolation != "local_cli"
        else ((), ())
    )

    return RunSpec(
        agent=step.agent,
        ticket=tick.ticket.external_key,
        workspace=RunWorkspace(
            repo=repo,
            base=expand(step.workspace.base, tick.values),
            # `{slug}` survives expansion on purpose: the agent chooses it (design.md §6).
            branch=expand(step.workspace.branch, tick.values),
            config_sha=tick.config_sha,
        ),
        # Contents, not paths, and read at `resolved.commit_sha` — what runs is exactly what the
        # registry pinned, not whatever the substrate would have checked out.
        prompts=AgentPrompts(system=resolved.system_prompt, instructions=resolved.instructions),
        skills=tuple(
            AgentSkill(name=skill.name, content=skill.content, scope=SkillScope(skill.scope))
            for skill in resolved.skills
        ),
        mcp=mcp_servers,
        expected_artifacts=_expected_artifacts(step, tick.values),
        inputs=(*resolved_inputs.inputs, *extra_inputs),
        attachments=resolved_inputs.attachments,
        missing_attachments=resolved_inputs.missing,
        secrets=secrets,
        secret_routes=secret_routes,
        secret_env=secret_env,
        model=(
            bedrock_model_of(runtime.details)
            if not requires_stored_credential(runtime.type)
            else manifest.model
        ),
        max_turns=manifest.limits.max_turns,
        # The family alone: the row's type says it, whatever the manifest still declares.
        runtime_options={"kind": declared_kind_for(runtime.type)},
        egress=egress,
        public=public,
        public_ports=public_ports,
        shown_files=_shown_files(step, tick.values),
    )


def _shown_files(step: AgentStep, values: Mapping[str, str]) -> tuple[ShownFile, ...]:
    """`preview.files` as the run is told them, expanded exactly as `workspace.branch` is:
    `{slug}` stays for the agent to choose until it resolves."""
    preview = step.preview
    if preview is None or not preview.enabled:
        return ()
    return tuple(
        ShownFile(path=expand(declared.path, values), label=declared.display_label)
        for declared in preview.files
    )


async def _spawn_refusal(tick: _Tick, placement: _Placement | None) -> str | None:
    """Why the runtime cannot run this spawn's session now, asked before the row exists so a
    refusal spends no attempt. None also for a runtime refused for another reason: the permanent
    path after the insert names it. An `AgentRuntimeError` (the substrate cannot say)
    propagates, and the tick retries."""
    adapter = placement.adapter if placement is not None else None
    refusal = await adapter.spawn_refusal() if adapter is not None else None
    if refusal is None:
        tick.deps.spawn_rechecks.pop(tick.ticket.id, None)
    else:
        tick.deps.spawn_rechecks[tick.ticket.id] = tick.now
    return refusal


async def _block_on_refusal(tick: _Tick, step: AgentStep, refusal: str) -> None:
    tick.ticket.status = TicketStatus.BLOCKED
    tick.plan.label = LABEL_BLOCKED
    await tick.emit(
        EventType.SPAWN_REFUSED, step=step.name, runtime=tick.config.runtime, refusal=refusal
    )
    tick.plan.comments.append(
        f"Step `{step.name}` can't start: {refusal.rstrip('.')}. Factorial checks again every "
        f"{_spoken(tick.deps.sessions.parked_recheck)} and continues by itself once the runtime "
        f"can run a session; remove `{LABEL_BLOCKED}` to check at once."
    )


#: What a blocked ticket's latest of these says about its block: only a refused spawn is
#: re-asked.
_BLOCKED_BY: Final = (
    EventType.SPAWN_REFUSED,
    EventType.STEP_STARTED,
    EventType.ESCALATED,
    EventType.AGENT_PARKED,
    EventType.AGENT_INTERRUPTED,
)


async def spawn_recheck_due(
    session: AsyncSession, deps: ReconcileDeps, ticket: Ticket, now: datetime
) -> bool:
    """Is this blocked ticket held by a refused spawn, unasked for `terminal.parked_recheck`?
    The log is read once per ticket per process, after which the memo answers."""
    if ticket.id not in deps.spawn_rechecks:
        latest = await session.scalar(
            select(Event.type)
            .where(Event.ticket_id == ticket.id, Event.type.in_(_BLOCKED_BY))
            .order_by(Event.id.desc())
            .limit(1)
        )
        if latest != EventType.SPAWN_REFUSED:
            deps.spawn_rechecks[ticket.id] = None
            return False
        return True
    last = deps.spawn_rechecks[ticket.id]
    return last is not None and now - last >= deps.sessions.parked_recheck


async def spawn_cleared(tick: _Tick) -> bool:
    """Ask the runtime again for a ticket a refused spawn blocked. A substrate that cannot say
    counts as still refused, and the ask still waits out the interval. So does a runtime with no
    adapter to ask, gone or no longer permitted: a person's release reaches the permanent
    failure that says why."""
    tick.deps.spawn_rechecks[tick.ticket.id] = tick.now
    try:
        placement = await _placement(tick)
        if placement is None or placement.adapter is None:
            return False
        return await _spawn_refusal(tick, placement) is None
    except AgentRuntimeError as exc:
        logger.warning(
            "ticket %s: spawn refusal not asked again: %s", tick.ticket.external_key, exc
        )
        return False


def _mcp_servers(resolved: ResolvedAgent) -> tuple[McpServerSpec, ...]:
    """`.factorial/mcp/*.yaml`, as resolved and allowlisted, onto the runtime contract.

    Header and env values travel with their `{secrets.KEY}` references intact — rendering them
    here would put credentials in a `RunSpec`, which reaches logs, events and error payloads
    (design.md §7.5). The adapter renders them at injection time and nowhere else.
    """
    servers: list[McpServerSpec] = []
    for server in resolved.mcp_servers:
        http, stdio = server.manifest.http, server.manifest.stdio
        servers.append(
            McpServerSpec(
                name=server.name,
                transport=McpTransport(server.manifest.transport),
                url=http.url if http is not None else None,
                command=stdio.command if stdio is not None else None,
                args=stdio.args if stdio is not None else (),
                headers=dict(http.headers) if http is not None else {},
                env=dict(stdio.env) if stdio is not None else {},
                tools_allow=server.manifest.tools.allow,
                required=server.manifest.required,
            )
        )
    return tuple(servers)


async def _poll_run(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """Ask the adapter where the run got to. Polling is the fallback path by design (§3.2)."""
    if run.runtime_run_id is None:  # spawn recorded but never returned a handle
        await _fail_run(
            tick,
            step,
            run,
            StepRunStatus.FAILED,
            "spawn produced no run id",
            summary="The agent didn't start.",
        )
        return

    # The adapter that spawned this run is the only one whose substrate knows its run id: the
    # registry row it was stamped with.
    adapter = await tick.deps.adapter_for_run(tick.session, run)
    status = await adapter.status(run.runtime_run_id)
    # INTERRUPTED too: the tick that honoured the request rolled back after `interrupt`, and the
    # stop must be recorded as the human's, not failed or taken for an idle park.
    unrecorded_stop = not status.is_terminal or status is RunStatus.INTERRUPTED
    if unrecorded_stop and await _interrupt_pending(tick, run):
        await _honour_interrupt(tick, step, run)
        return

    polled = await _poll_session(tick, step, run, adapter, status)
    if polled is None:
        return
    status = polled

    if not status.is_terminal:
        if status is RunStatus.IDLE:
            return
        timeout = _timeout(tick.config, step)
        deadline = (run.started_at or tick.now) + timeout
        if tick.now > deadline:
            # Only a run past its spawn deadline pays for the event read: activity moves it on.
            deadline = (await _active_since(tick, run) or tick.now) + timeout
        if tick.now > deadline:
            await adapter.cancel(run.runtime_run_id)
            await observe_outputs(tick, step, run)
            await _fail_run(
                tick,
                step,
                run,
                StepRunStatus.TIMEOUT,
                f"timed out after {timeout}",
                reason="timeout",
                summary=f"The run passed its time limit of {spoken(timeout)} and was stopped.",
            )
        return

    if await _interrupt_pending(tick, run):
        # Terminal already, so there is nothing to stop — but the request is in the log and only
        # an answer keeps it from reading as one the loop ignored.
        await tick.emit(
            EventType.INTERRUPT_OVERTAKEN,
            run=run,
            step=step.name,
            attempt=run.attempt,
            run_id=run.runtime_run_id,
        )

    result = await adapter.result(run.runtime_run_id)
    if result.outcome is RunStatus.INTERRUPTED and result.park_reason is ParkReason.EXITED:
        # A quit the loop chose not to park (`_settle_exit`), or one a finish it sent raced:
        # judged as the clean end it was before a quit parked.
        result = result.model_copy(
            update={"outcome": RunStatus.SUCCEEDED, "failure_reason": None, "park_reason": None}
        )
    run.ended_at = tick.now
    await _record_result(tick, step, run, result)

    if result.outcome != RunStatus.SUCCEEDED:
        await _fail_run(
            tick,
            step,
            run,
            _RUN_STATUS.get(result.outcome, StepRunStatus.FAILED),
            result.failure_reason or f"run {result.outcome}",
            stream_warnings=result.stream_warnings,
            session_id=result.session_id,
            summary=None if result.failure_reason else "The run ended without giving a reason.",
        )
        return

    run.status = StepRunStatus.SUCCEEDED
    await release_run_credential(tick, run.id)
    await _emit_completed(tick, step, run, result)
    await _accept(tick, step, run)


async def _record_result(tick: _Tick, step: AgentStep, run: StepRun, result: RunResult) -> None:
    # Over whatever a live session wrote: a measured figure wins, and a substrate that reports
    # none gets the list price, subscription or not (decision 34).
    record_usage(
        run,
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        cache_write_input_tokens=result.cache_write_input_tokens,
        output_tokens=result.output_tokens,
        reported=result.cost_usd,
    )
    _write_artifacts(tick, run, result.artifacts)
    # Before the outcome: a run that fails can park its ticket, and a parked ticket's tick stops
    # before `observe_pending_outputs` until a human releases it.
    await observe_outputs(tick, step, run)


async def _emit_completed(
    tick: _Tick, step: AgentStep, run: StepRun, result: RunResult, **extra: object
) -> None:
    await tick.emit(
        EventType.AGENT_COMPLETED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        summary=result.summary,
        # The concrete branch, against the spawn event's template — see `_delivered_branch`.
        branch=_delivered_branch(result.artifacts),
        cost_usd=None if run.cost_usd is None else str(run.cost_usd),
        cost_source=run.cost_source,
        input_tokens=run.input_tokens,
        cached_input_tokens=run.cached_input_tokens,
        output_tokens=run.output_tokens,
        **({"stream_warnings": list(result.stream_warnings)} if result.stream_warnings else {}),
        # What a rework continues (`worker.rework`).
        **({"session_id": result.session_id} if result.session_id else {}),
        **extra,
    )


def parked_recheck_due(deps: ReconcileDeps, run: StepRun, now: datetime) -> bool:
    """Is `run` parked, within the keep rule's retention, and unread for
    `terminal.parked_recheck`? Reads nothing: it runs on every parked tick."""
    if run.id is None or run.status != StepRunStatus.INTERRUPTED or run.ended_at is None:
        return False
    if run.ended_at < now - deps.sessions.retention:
        return False
    last = deps.parked_rechecks.get(run.id)
    return last is None or now - last >= deps.sessions.parked_recheck


#: Parks that may catch a session mid-turn: re-checked only when it sat at its prompt.
_PROMPT_OR_TURN: Final = frozenset({ParkReason.CEILING, ParkReason.ROLLOVER})


async def recheck_parked(tick: _Tick, ticket: Ticket, run: StepRun) -> bool:
    """Accept a loop-parked session whose postconditions have come to hold, from the result its
    substrate already holds: no resume, no mint (decision 56). True when it was accepted.

    Never a person's stop (an Interrupt or a quit from the terminal), nor a park taken mid-turn:
    there a PR can be open with CI green and half the turn undone.
    A person's comment newer than the park holds it: their release decides.
    """
    assert run.id is not None and run.ended_at is not None
    rechecks = tick.deps.parked_rechecks
    # A stamp the interval has passed answers `parked_recheck_due` as no stamp does.
    interval = tick.deps.sessions.parked_recheck
    for stale in [key for key, at in rechecks.items() if tick.now - at >= interval]:
        del rechecks[stale]
    # First: a read that raises still waits out the interval.
    rechecks[run.id] = tick.now
    if await _interrupt_pending(tick, run):
        # A person's stop the park overtook: theirs is the newer word.
        return False
    park = await _latest_park(tick, run)
    if park is None or not (park.idle or (park.reason in _PROMPT_OR_TURN and park.at_prompt)):
        return False
    step = tick.config.find_step(ticket.current_step)
    if not isinstance(step, AgentStep) or run.runtime_run_id is None:
        return False
    try:
        adapter = await tick.deps.adapter_for_run(tick.session, run)
        result = await adapter.result(run.runtime_run_id)
    except (AgentRuntimeError, RuntimeRefused) as exc:
        logger.warning(
            "ticket %s step %s: parked run %s not re-checked: %s",
            ticket.external_key,
            step.name,
            run.runtime_run_id,
            exc,
        )
        return False
    met, _ = await postconditions_met(tick, step, run.attempt)
    if not met:
        return False
    newer = await _observed(
        _comments_since(tick, replace(park, cutoff=run.ended_at)),
        what=f"step {step.name} attempt {run.attempt}: comments since the park",
    )
    if newer:
        return False
    try:
        # Ends the park, which on a host holds the branch a later fresh attempt checks out.
        await adapter.finish(run.runtime_run_id)
    except AgentRuntimeError as exc:
        logger.warning(
            "ticket %s step %s: parked run %s accepted with its park kept: %s",
            ticket.external_key,
            step.name,
            run.runtime_run_id,
            exc,
        )

    await _record_result(tick, step, run, result)
    run.status = StepRunStatus.SUCCEEDED
    run.failure_reason = None
    ticket.status = TicketStatus.RUNNING
    await _emit_completed(tick, step, run, result, while_parked=park.reason)
    tick.plan.comments.append(
        f"`{step.name}` (attempt {run.attempt}) is accepted while paused: its expected results "
        "are there now. The session was not resumed, and its workspace is kept."
    )
    await accepted(tick, step, run)
    return True
