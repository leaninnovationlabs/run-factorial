"""The failure paths shared by agent, action and check steps: permanent misconfiguration vs
retryable infrastructure, and the retry/escalate ladder."""


from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from orchestrator.config import (
    ActionStep,
    AgentStep,
    CheckStep,
    ConfigError,
)
from orchestrator.db.models import (
    StepRun,
    StepRunStatus,
    TicketStatus,
)
from orchestrator.events import EventType
from orchestrator.factorial import AgentResolutionError
from orchestrator.github import (
    LABEL_BLOCKED,
)
from orchestrator.worker.inputs import release_gitlab_run_token
from orchestrator.worker.tick_context import _apply_outcome, _ordinal, _retry_policy, _Tick
from runtime.contract import (
    SpawnError,
)

#
# An exception out of `_build_run_spec`/`spawn` (or `_start_action`'s pre-tag region) rolls back
# the `step_run` row it was raised inside and every event with it. For an INFRASTRUCTURE fault
# that is right — the next tick retries and a GitHub 500 or a hung adapter may clear up. For a
# PERMANENT one (wrong manifest, permission ceiling, unconfigured App credential) it produces an
# invisible attempt-1-forever spin: the next tick finds no row, starts clean, and fails
# identically, with nothing in the event log or UI to show the ticket is wedged.
#
# A present-but-malformed private key is classified where the evidence lives, not by widening the
# tuple below: `github/client.py`'s `_app_credential_faults_named` reads the `RequestError`'s
# `__cause__` (PyJWT means the key will not sign, `httpx` means the network) and raises
# `AppCredentialError`, a `RuntimeError` subclass. The same path converts a 401/404 on the mint
# route specifically — wrong App key or wrong installation id — and a 401 or 404 on the
# installation lookup — wrong key, or the App not installed on the owner. A `RequestError` from
# any other cause, and 403/5xx on those routes, stay retryable.


#: Config/App-setup problems a retry can never fix, caught at the two boundaries that can raise
#: them (`_start_attempt`'s `_build_run_spec` call, `_start_action`'s pre-tag region) and turned
#: into a visible, immediately-escalated failure via `_fail_run`'s `permanent` parameter rather
#: than letting the tick's rollback erase the row.
#:
#: * `AgentResolutionError` — all four subclasses; an unregistered or deactivated agent needs an
#:   operator just as much as a ceiling or allowlist violation does.
#: * `ConfigError` — a manifest, prompt, skill, or MCP-server yaml missing or unparseable.
#: * `RuntimeError` — every `RuntimeError` either boundary can raise is config/App-setup shaped:
#:   `TokenScopeNotGranted`, `GitHubClient._require_connection`'s "no GitHub connection",
#:   this module's own never-wired-in dependency errors, `SecretsConfigurationError` /
#:   `DecryptionError`, and `worker.runtime_adapters.RuntimeRefused` (a process naming a
#:   runtime slug with no row, a type not implemented yet, a manifest of the other family).
#:   No network or other transient failure in either call graph raises
#:   `RuntimeError` — `githubkit`'s `RequestFailed` is a plain `Exception` — which is what keeps
#:   this catch from swallowing a genuine infrastructure fault.
#:
#: Not here: `runtime.contract.SpawnError` and its `AgentRuntimeError` siblings are also
#: `RuntimeError` subclasses, and matching them by class would catch every cause alike. A
#: `SpawnError` is classified by the reason its adapter attached (`_permanent_spawn`), at the one
#: call site that can produce one.
_PERMANENT_CONFIG_ERRORS: Final[tuple[type[Exception], ...]] = (
    AgentResolutionError,
    ConfigError,
    RuntimeError,
)


def _permanent_spawn(exc: SpawnError) -> bool:
    """Does this refusal mean an operator must act, rather than "try again in 30 seconds"?

    The single consumption point for `SpawnReason`, and a one-line read of `reason.is_permanent`
    and nothing else: sniffing the adapter's message text is untestable (wording is adapter-owned)
    and re-implementing each adapter's capability checks here duplicates a layer's job. An adapter
    that has classified nothing reports `INFRASTRUCTURE`, so this returns False and the default
    rollback-and-retry stands.
    """
    return exc.reason.is_permanent


#: Prefixed onto a permanent failure's `failure_reason` so the escalation comment on the issue
#: tells a human "this needs an operator, not a retry" without cross-referencing the event log.
#: The structured half of the same distinction is `_fail_run`'s `reason="permanent"` and
#: `_retry_or_escalate`'s `permanent` detail on `ESCALATED`; `_PERMANENT_SUMMARY_PREFIX` is the
#: same prefix in the `failure_summary` a person reads.
_PERMANENT_FAILURE_PREFIX: Final = "misconfiguration: "
_PERMANENT_SUMMARY_PREFIX: Final = "Setup problem: "


#: A machine code a reason opens with for code and tests to match (`host_lost: …`): kept in
#: `failure_reason`, dropped from the summary a person reads.
_REASON_CODE: Final = re.compile(r"^[a-z]+(?:_[a-z]+)+:\s+")


def first_paragraph(text: str) -> str:
    """A failure reason's summary, as the dashboard's `snippet` shows it: the text before its
    first blank line, on one line, without a leading `_REASON_CODE`. Raw output an adapter
    appends goes after a blank line."""
    first = " ".join(re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0].split())
    return _REASON_CODE.sub("", first, count=1)


def template_summary(summary: str) -> str:
    """`{failure_summary}` as an escalation template gets it: without its closing period, because
    the template ends its own sentence (`…: {failure_summary}.`)."""
    return summary.removesuffix(".")


def spoken(span: timedelta) -> str:
    """A duration as a person says it: `1 hour`, `30 minutes`, `45 seconds`."""
    seconds = int(span.total_seconds())
    for unit, size in (("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


async def _fail_permanently(
    tick: _Tick,
    step: AgentStep | ActionStep | CheckStep,
    run: StepRun,
    exc: Exception,
    *,
    event_type: EventType = EventType.AGENT_FAILED,
) -> None:
    """Fail `run` visibly and escalate on this tick, instead of letting the rollback erase it.

    Shared by the three boundaries that can raise a permanent failure (`_start_attempt`'s
    spec-build and spawn wraps, `_start_action`'s pre-tag wrap). The prefix, `reason="permanent"`
    and `permanent=True` are one decision; hand-copying it would let the escalation comment differ
    by which boundary caught the failure.
    """
    await _fail_run(
        tick,
        step,
        run,
        StepRunStatus.FAILED,
        f"{_PERMANENT_FAILURE_PREFIX}{exc}",
        reason="permanent",
        event_type=event_type,
        permanent=True,
        summary=f"{_PERMANENT_SUMMARY_PREFIX}{first_paragraph(str(exc))}",
    )


async def _fail_run(
    tick: _Tick,
    step: AgentStep | ActionStep | CheckStep,
    run: StepRun,
    status: StepRunStatus,
    failure_reason: str,
    *,
    reason: str = "failed",
    event_type: EventType = EventType.AGENT_FAILED,
    permanent: bool = False,
    stream_warnings: Sequence[str] = (),
    session_id: str | None = None,
    summary: str | None = None,
) -> None:
    """Shared by agent runs, actions and checks — `event_type` differs because an `agent_failed`
    event on an action or check row would misname what ran. `permanent=True` skips straight to
    escalation, past `_retry_or_escalate`'s `attempt < max_attempts` branch.

    `failure_reason` is the whole story, rule code and raw output included: the retry briefing,
    the runtime page's refused-credential match and the MCP tools read it. `summary` is the one
    plain sentence a person is shown first, on the timeline and as `{failure_summary}` in the
    escalation comment; without one it is `failure_reason`'s first paragraph.
    """
    summary = summary or first_paragraph(failure_reason)
    run.status = status
    run.failure_reason = failure_reason
    run.ended_at = tick.now
    await release_gitlab_run_token(tick, run.id)
    await tick.emit(
        event_type,
        run=run,
        step=step.name,
        attempt=run.attempt,
        run_id=run.runtime_run_id,
        reason=reason,
        failure_reason=failure_reason,
        failure_summary=summary,
        **({"stream_warnings": list(stream_warnings)} if stream_warnings else {}),
        **({"session_id": session_id} if session_id else {}),
    )
    await _retry_or_escalate(tick, step, run, failure_reason, permanent=permanent, summary=summary)


async def _retry_or_escalate(
    tick: _Tick,
    step: AgentStep | ActionStep | CheckStep,
    run: StepRun,
    failure_reason: str,
    *,
    permanent: bool = False,
    summary: str | None = None,
) -> None:
    """Failure is modelled, not implied (design.md §2.6).

    `permanent=True` bypasses retry scheduling even with `max_attempts` budget left: a retry
    re-runs the same pinned manifest against the same ceiling and mints from the same
    misconfigured App, so it only delays the escalation a human needs to see.
    """
    retry = _retry_policy(tick.config, step)
    max_attempts = retry.max_attempts if retry is not None else 1
    # The generation's own count: a restart grants the whole budget again (decision 67), while
    # the attempt number keeps counting for the event log.
    tried = await _ordinal(tick.session, run)
    if not permanent and retry is not None and tried < max_attempts:
        # No sleeping: the due time is recorded in the event and recomputed from `ended_at` on the
        # tick that acts on it, so a worker restart cannot lose a scheduled retry.
        due_at = tick.now + retry.backoff
        await tick.emit(
            EventType.RETRY_SCHEDULED,
            run=run,
            step=step.name,
            attempt=run.attempt,
            next_attempt=run.attempt + 1,
            max_attempts=max_attempts,
            due_at=due_at.isoformat(),
        )
        return

    on_failure = step.on_failure or tick.config.defaults.on_failure
    summary = summary or first_paragraph(failure_reason)
    extra = {
        "step": step.name,
        "attempt": str(tried),
        "failure_reason": failure_reason,
        "failure_summary": template_summary(summary),
    }
    if on_failure is not None:
        _apply_outcome(tick, on_failure.escalate, extra=extra)
    if tick.plan.label is None:
        tick.plan.label = LABEL_BLOCKED
    tick.ticket.status = TicketStatus.BLOCKED
    await tick.emit(
        EventType.ESCALATED,
        run=run,
        step=step.name,
        attempt=run.attempt,
        max_attempts=max_attempts,
        failure_reason=failure_reason,
        failure_summary=summary,
        permanent=permanent,
    )
