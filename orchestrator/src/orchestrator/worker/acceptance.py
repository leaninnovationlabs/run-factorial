"""Acceptance of an agent run's output: the postconditions decide, never the agent's
report (design.md §2.4), and delivered artifacts are persisted verbatim as observed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from orchestrator.config import (
    AgentStep,
    expand,
)
from orchestrator.config.conditions import awaits_review, iter_calls
from orchestrator.db.models import (
    Artifact,
    ArtifactDirection,
    StepRun,
    StepRunStatus,
)
from orchestrator.events import EventType
from orchestrator.events.approvals import latest_approval
from orchestrator.github.state import CheckRunState
from orchestrator.predicates import EvalContext, ReviewFacts, Unmet, evaluate, failures, needs
from orchestrator.predicates.globs import matches_ref
from orchestrator.predicates.library import ci_check_runs
from orchestrator.worker.failures import _fail_run
from orchestrator.worker.rework import _latest_details, _rejecting_gates
from orchestrator.worker.templates import _glob, _pr_for, _pr_values, _ref_name
from orchestrator.worker.tick_context import _advance, _observed, _Tick
from runtime.contract import Artifact as RunArtifact
from runtime.contract import ArtifactKind


def _delivered_branch(artifacts: Sequence[RunArtifact]) -> str | None:
    """The ref a run's delivery names, when it names exactly one.

    The timeline's only branch until now was the spawn's, which is the DIRECTIVE the agent was
    given — still `spec/{ticket}-{slug}` on the attempt that creates the branch, because the
    agent has not chosen the name yet. This is the observed answer to it.

    Same fail-safe as `{slug}` resolution and for the same reason: a run reporting two names has
    not said which branch it delivered, so the log says nothing rather than picking one.
    """
    names: set[str] = set()
    for artifact in artifacts:
        if artifact.kind not in (ArtifactKind.BRANCH, ArtifactKind.PR):
            continue
        name = _ref_name(artifact.locator)
        if name:
            names.add(name)
    return next(iter(names)) if len(names) == 1 else None


def _write_artifacts(tick: _Tick, run: StepRun, artifacts: Sequence[RunArtifact]) -> None:
    """Locator metadata only; the content stays in GitHub (data-model.md, `artifact`).

    Cost and token counts are NOT written here — they belong to the run.
    """
    for artifact in artifacts:
        tick.session.add(
            Artifact(
                step_run_id=run.id,
                direction=ArtifactDirection.OUTPUT,
                kind=str(artifact.kind),
                locator=dict(artifact.locator),
                url=artifact.url,
            )
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    """`met` is the predicates' word alone; `unmoved` is `_unmoved_rejections`, which no
    predicate sees and no person may override (decision 78)."""

    met: bool
    failures: tuple[str, ...]
    unmoved: tuple[str, ...]
    #: `failures` in words (`predicates.needs`), for what a person reads.
    needs: tuple[str, ...] = ()


#: The prefix of an unmet postcondition's `failure_summary`.
MISSING: Final = "Expected results missing: "


async def acceptance_verdict(
    tick: _Tick, step: AgentStep, run: StepRun, *, every_leaf: bool = False
) -> Verdict:
    """`run`'s step's postconditions now (`evaluate`'s `every_leaf`), and its rework's unmoved
    rejections. A step with none is met. An unreadable verdict raises `_EvaluationUnavailable`."""
    met, reasons, words = True, (), ()
    if step.postconditions is not None:
        ctx = await _review_context(tick, step, run)
        result = await _observed(
            evaluate(step.postconditions, ctx, every_leaf=every_leaf),
            what=f"step {step.name} attempt {run.attempt}: postconditions",
        )
        met, reasons, words = result.value, failures(result), needs(result)
    return Verdict(met, reasons, await _unmoved_rejections(tick, step, run.attempt), needs=words)


async def _review_context(tick: _Tick, step: AgentStep, run: StepRun) -> EvalContext:
    """The tick's context, plus `run`'s approval when the step awaits one. Keyed on the run, so a
    retry, a redo or a restart never inherits an earlier run's."""
    if not awaits_review(step.postconditions):
        return tick.ctx
    approval = await latest_approval(tick.session, tick.ticket.id, run.id)
    review = ReviewFacts(
        branch=_glob(step.workspace.branch, tick.values),
        approved_sha=None if approval is None else approval.head_sha,
    )
    return replace(tick.ctx, review=review)


async def postconditions_met(tick: _Tick, step: AgentStep, run: StepRun) -> tuple[bool, Unmet]:
    """Whether `run`'s step's postconditions hold now, and what is unmet when they do not. A step
    with none is met, unless it is a rework that has not moved the pull request its gate rejected
    (`_unmoved_rejections`). An unreadable verdict raises `_EvaluationUnavailable`: neither
    answer."""
    verdict = await acceptance_verdict(tick, step, run)
    # An unmoved rejection is already words, so it is in both.
    unmet = Unmet((*verdict.failures, *verdict.unmoved), (*verdict.needs, *verdict.unmoved))
    return verdict.met and not verdict.unmoved, unmet


async def _unmoved_rejections(tick: _Tick, step: AgentStep, attempt: int) -> tuple[str, ...]:
    """Why a rework has not answered its gates: a pull request a gate rejected is open and still
    ends at the commit it rejected. A rework's other postconditions held before it began, so they
    cannot tell an answer from nothing. Every gate in the window must be answered.

    Silent for a gate with nothing to compare: a rejection recorded before the gate kept its
    commit, no such pull request open now, or one on a branch the step does not push, which in a
    stacked lane moves only after the step is accepted. The PR counts as moved once its branch
    has, since GitHub updates a PR's head after the push lands on the branch."""
    own_branch = _glob(step.workspace.branch, tick.values)
    reasons: list[str] = []
    for rejection in await _rejecting_gates(tick, step, attempt):
        rejected = await _latest_details(tick, rejection.run, EventType.GATE_REJECTED)
        rejected_sha = rejected.get("head_sha")
        if not isinstance(rejected_sha, str):
            continue
        pull_request = await _pr_for(
            tick,
            rejection.gate.gate.pr,
            what=f"step {step.name} attempt {attempt}: the rejected pull request",
        )
        if (
            pull_request is None
            or pull_request.state != "open"
            or pull_request.head_sha != rejected_sha
            or not matches_ref(own_branch, pull_request.head_ref)
        ):
            continue
        branches = await _observed(
            tick.source.branches(),
            what=f"step {step.name} attempt {attempt}: the rejected pull request's branch",
        )
        if any(b.name == pull_request.head_ref and b.sha != rejected_sha for b in branches):
            continue
        reasons.append(
            f"the pull request #{pull_request.number} still ends at {rejected_sha[:7]}, "
            "the commit the gate rejected"
        )
    return tuple(reasons)


async def ci_checks(tick: _Tick, step: AgentStep, attempt: int) -> tuple[bool, datetime | None]:
    """Whether a check run is still going on a head the step's `ci_green` calls read, and when
    the last completed one did. None at all reads as settled. Never accepts a run: `ci_green`
    decides that.

    From the same memoized listing `ci_green` reads; when it was not evaluated this tick, or
    stopped early, this is a fresh read."""

    async def read() -> list[CheckRunState]:
        runs: list[CheckRunState] = []
        for call in iter_calls(step.postconditions):
            if call.name == "ci_green":
                (ref,) = (expand(arg, tick.ctx.values) for arg in call.args)
                async for found in ci_check_runs(tick.ctx, ref):
                    runs.extend(found)
        return runs

    runs = await _observed(read(), what=f"step {step.name} attempt {attempt}: check runs")
    pending = any(run.status != "completed" for run in runs)
    return pending, max((run.completed_at for run in runs if run.completed_at), default=None)


async def _accept(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """The acceptance gate: postconditions, never the agent's self-report (design.md §2.4)."""
    # A raise here arrives after `_poll_run` marked the run SUCCEEDED and emitted
    # AGENT_COMPLETED — exactly the writes the act-phase SAVEPOINT takes back. An unreadable
    # verdict is neither acceptance nor failure: the run keeps its status and the next tick
    # re-polls and asks again.
    met, unmet = await postconditions_met(tick, step, run)
    if not met:
        await tick.emit(
            EventType.POSTCONDITION_FAILED,
            run=run,
            step=step.name,
            attempt=run.attempt,
            failures=list(unmet.rules),
            needs=list(unmet.words),
        )
        # The agent said it succeeded. The predicates say otherwise, and they decide. The reason
        # stays the rule code `inputs.retry_briefing` compares against `failures`.
        await _fail_run(
            tick,
            step,
            run,
            StepRunStatus.FAILED,
            "; ".join(unmet.rules),
            summary=MISSING + "; ".join(unmet.words),
        )
        return
    await accepted(tick, step, run)


async def accepted(
    tick: _Tick, step: AgentStep, run: StepRun, *, extra: Mapping[str, str] | None = None
) -> None:
    """`_accept`'s met path: complete the run and move the ticket on, for a caller that has
    decided to accept it, by the postconditions or a person's force (decision 78). `extra` is the
    outcome's `{pr_url}` (`accepted_values`) when the caller read it before a write it cannot take
    back."""
    if extra is None:
        extra = await accepted_values(tick, step)
    await _advance(tick, step, run, step.on_success, extra=extra)


async def accepted_values(tick: _Tick, step: AgentStep) -> dict[str, str]:
    """What `accepted` reads from GitHub, and can raise `_EvaluationUnavailable` on."""
    return await _pr_values(tick, step.workspace.branch, None)
