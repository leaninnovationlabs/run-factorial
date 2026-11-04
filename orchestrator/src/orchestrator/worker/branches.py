"""How the worker finds a run's own branch on GitHub: where the step's branches stood at spawn,
and, once the run ended, the slug the run itself delivered, the concrete name a template takes
with it, and the observed branches that name covers. Also when each ended run's record is still
to be written, for its output record (`worker/outputs.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import select

from orchestrator.config import AgentStep, expand
from orchestrator.db.models import Artifact, ArtifactDirection, Event, StepRun, StepRunStatus
from orchestrator.events import EventType
from orchestrator.github import BranchState
from orchestrator.predicates.globs import resolve_refs
from orchestrator.worker.templates import (
    _glob,
    _ref_name,
    _slug_between,
    _slug_shape,
    _without_slug,
)
from orchestrator.worker.tick_context import _observed, _Tick
from runtime.contract import ArtifactKind

__all__ = [
    "ENDED",
    "branch_heads",
    "declares_files",
    "exact",
    "named_branches",
    "recorded",
    "run_slug",
    "unrecorded_runs",
]

#: An interrupted run has not ended: its resume may still push (decision 26).
ENDED: Final = (
    StepRunStatus.SUCCEEDED,
    StepRunStatus.FAILED,
    StepRunStatus.TIMEOUT,
    StepRunStatus.CANCELLED,
)


def named_branches(
    branches: Sequence[BranchState],
    template: str,
    values: Mapping[str, str],
    slug: str | None,
) -> list[BranchState]:
    """The branches `template` names: the one `slug` makes it, else every match of its glob."""
    concrete = exact(template, values, slug)
    found = [branch for branch in branches if branch.name == concrete]
    return found or list(resolve_refs(branches, _glob(template, values)))


def exact(template: str, values: Mapping[str, str], slug: str | None) -> str | None:
    """The concrete name `template` gives with this run's own slug, or None while the run named
    none — `{slug}` is then only a glob."""
    literal = _without_slug(values)
    if "{slug}" in expand(template, literal):
        if slug is None:
            return None
        literal["slug"] = slug
    return expand(template, literal)


async def run_slug(tick: _Tick, step: AgentStep, run: StepRun) -> str | None:
    """The slug this run delivered under its own `workspace.branch`, from its artifact rows; None
    unless it named exactly one. The run's own answer, never the ticket-wide `{slug}`: a rework
    that chose a new slug must not be shown the old attempt's files."""
    shape = _slug_shape(step.workspace.branch, tick.values)
    if shape is None:
        return None
    locators = await tick.session.scalars(
        select(Artifact.locator).where(
            Artifact.step_run_id == run.id,
            Artifact.direction == ArtifactDirection.OUTPUT,
            Artifact.kind.in_((ArtifactKind.BRANCH, ArtifactKind.PR)),
        )
    )
    slugs: set[str] = set()
    for locator in locators:
        name = _ref_name(locator)
        candidate = _slug_between(name, shape) if name else None
        if candidate:
            slugs.add(candidate)
    return next(iter(slugs)) if len(slugs) == 1 else None


def declares_files(step: AgentStep) -> bool:
    """Whether the step's enabled `preview:` declares files (tech-stack decision 71)."""
    preview = step.preview
    return preview is not None and preview.enabled and bool(preview.files)


async def branch_heads(tick: _Tick, step: AgentStep) -> list[dict[str, str]]:
    """Where each observed branch the step's `workspace.branch` covers stands now: what a run
    spawned on this tick starts from, so its output record counts only the commits it pushed.

    A list, never a map keyed by name: a branch name is data, and as an event key one like
    `spec/7-api-key` reads to the credential guard as a field name and fails the spawn's event."""
    branches = await _observed(
        tick.source.branches(), what=f"step {step.name}: the branches at spawn"
    )
    return [
        {"name": branch.name, "sha": branch.sha}
        for branch in resolve_refs(branches, _glob(step.workspace.branch, tick.values))
    ]


async def recorded(tick: _Tick, run: StepRun, kind: EventType) -> bool:
    """Whether `run` already has an event of `kind`: the output record is written once per
    run."""
    found = await tick.session.scalar(
        select(Event.id)
        .where(
            Event.ticket_id == run.ticket_id,
            Event.step_run_id == run.id,
            Event.type == kind,
        )
        .limit(1)
    )
    return found is not None


async def unrecorded_runs(
    tick: _Tick, step_names: Sequence[str], kind: EventType
) -> list[tuple[StepRun, bool]]:
    """Each ended run of `step_names` with no `kind` event yet, oldest first, and whether its
    step's next attempt already exists. By then the branch may hold the newer attempt's push, so
    the caller records the older run as not looked up rather than pin the newer work to it."""
    done = select(Event.step_run_id).where(
        Event.ticket_id == tick.ticket.id,
        Event.type == kind,
        Event.step_run_id.is_not(None),
    )
    runs = (
        await tick.session.scalars(
            select(StepRun)
            .where(
                StepRun.ticket_id == tick.ticket.id,
                StepRun.step_name.in_(step_names),
                StepRun.status.in_(ENDED),
                StepRun.id.not_in(done),
            )
            .order_by(StepRun.id)
        )
    ).all()
    pending: list[tuple[StepRun, bool]] = []
    for run in runs:
        newer = await tick.session.scalar(
            select(StepRun.id)
            .where(
                StepRun.ticket_id == tick.ticket.id,
                StepRun.step_name == run.step_name,
                StepRun.id > run.id,
            )
            .limit(1)
        )
        pending.append((run, newer is not None))
    return pending
