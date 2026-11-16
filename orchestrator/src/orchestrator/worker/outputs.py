"""A step's declared files (tech-stack decision 71): what each run of a step that declares
`preview.files` changed on its branch, recorded once per run as `run_outputs_observed`. A step
that awaits review (`request_approval()`) is read while it waits: its run is recorded again at
each idle spell with its postconditions unmet, whenever its branch moves off the commit the
latest record read (on an idle tick, or a parked run's re-check), and once more at its end, and
the reader takes the latest. A spell GitHub would not answer for is asked again on each later
tick of that spell (`observe_waiting_outputs`); an end it would not answer for keeps the last
spell's record, since the later ticks ask only for a run with no record. A failed look-up that
found the branch keeps its `commit`, so the same answer is not asked for again until it moves.

The record is the step's footprint, wider than what it declares: every file this generation's
attempts changed on the run's branch, read at the branch's head when recorded: the run's end
commit, or where a waiting review run's branch stood when it stopped. The API selects the declared
files from it (`events/outputs.py`), so showing every file later needs nothing new here. A run's
changes start where its branch stood at spawn (`agent_spawned`'s `branch_heads`), else at the
base: a step working on a branch another step pushed is not credited with that step's files.

The agent pushes; this module only looks: `_poll_run` asks as soon as a run ends,
`observe_pending_outputs` asks again on later ticks for a run with no record at all, and once the
step's next attempt exists a run GitHub would not answer for is recorded `lookup_failed`. Like
every preview, the record is informational: nothing here raises into the ticket's pass or holds up
acceptance.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import func, select

from orchestrator.config import AgentStep
from orchestrator.config.conditions import awaits_review
from orchestrator.db.models import Event, StepRun, current_runs
from orchestrator.events import EventType
from orchestrator.events.outputs import outputs_observation, spawn_heads
from orchestrator.events.read_models import latest_run_event
from orchestrator.github import TreeEntry
from orchestrator.github.contents import ComparedFile, ContentsNotFound, TreeTruncated
from orchestrator.predicates.globs import matches_file_pattern
from orchestrator.predicates.words import ref
from orchestrator.worker.branches import (
    declares_files,
    exact,
    named_branches,
    recorded,
    run_slug,
    unrecorded_runs,
)
from orchestrator.worker.templates import _glob, _slug_between, _slug_shape
from orchestrator.worker.tick_context import _Tick

logger = logging.getLogger(__name__)

__all__ = ["FOOTPRINT_MAX", "observe_outputs", "observe_pending_outputs", "observe_waiting_outputs"]

#: The most paths one record keeps, GitHub's own limit on a comparison. A declared file is never
#: the one dropped, and a pattern's files go before undeclared ones.
FOOTPRINT_MAX: Final = 300


async def observe_outputs(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """Record what `run` changed, unless that is already recorded and the step does not await
    review, whose every record replaces the last. A GitHub error records nothing: a later tick
    asks again for a run with no record, or for a waiting review run's spell
    (`observe_waiting_outputs`), and never for a review run's end."""
    if (
        not declares_files(step)
        or tick.bindings is None
        or (
            not awaits_review(step.postconditions)
            and await recorded(tick, run, EventType.RUN_OUTPUTS_OBSERVED)
        )
    ):
        return
    try:
        async with tick.session.begin_nested():
            details = await _look_up(tick, step, run)
            await tick.emit(
                EventType.RUN_OUTPUTS_OBSERVED,
                run=run,
                step=step.name,
                attempt=run.attempt,
                **details,
            )
    except Exception:
        logger.warning(
            "ticket %s run %s: the output look-up failed; a later tick retries it",
            tick.ticket.external_key,
            run.id,
            exc_info=True,
        )


async def observe_waiting_outputs(tick: _Tick, step: AgentStep, run: StepRun) -> None:
    """Record a review run waiting at its prompt, unless its latest record is newer than its
    latest `agent_idle` and its branch still ends where that record read it: once per spell and
    again whenever the branch moves in it, asked again on each tick until GitHub answers."""
    if not declares_files(step):
        return
    idle, observed = (
        await tick.session.execute(
            select(
                *(
                    latest_run_event(run.ticket_id, run.id, [kind], Event.id).scalar_subquery()
                    for kind in (EventType.AGENT_IDLE, EventType.RUN_OUTPUTS_OBSERVED)
                )
            )
        )
    ).one()
    if observed is None or (idle is not None and observed < idle) or await _moved(tick, step, run):
        await observe_outputs(tick, step, run)


async def _moved(tick: _Tick, step: AgentStep, run: StepRun) -> bool:
    """Does the run's branch end on another commit than its latest record read? A push, a bot or
    "Update branch" can move it while the person reads the files. The tick's branch listing is
    memoized and the run's postconditions already read it, so this asks the forge nothing new."""
    if tick.bindings is None:
        return False
    try:
        branches = await tick.source.branches()
    except Exception:
        logger.warning(
            "ticket %s run %s: the branch listing failed; the files stay as recorded",
            tick.ticket.external_key,
            run.id,
            exc_info=True,
        )
        return False
    found = named_branches(
        branches, step.workspace.branch, tick.values, await run_slug(tick, step, run)
    )
    head = found[0].sha if len(found) == 1 else None
    return head != (await outputs_observation(tick.session, run) or {}).get("commit")


async def observe_pending_outputs(tick: _Tick) -> None:
    """Ask again for each ended run of a step that declares files with nothing recorded."""
    steps = {
        step.name: step
        for step in tick.config.steps
        if isinstance(step, AgentStep) and declares_files(step)
    }
    if not steps or tick.bindings is None:
        return
    for run, superseded in await unrecorded_runs(
        tick, list(steps), EventType.RUN_OUTPUTS_OBSERVED
    ):
        if superseded:
            await tick.emit(
                EventType.RUN_OUTPUTS_OBSERVED,
                run=run,
                step=run.step_name,
                attempt=run.attempt,
                status="lookup_failed",
                reason="GitHub could not be read before the step's next attempt started",
            )
            continue
        await observe_outputs(tick, steps[run.step_name], run)


async def _contents(tick: _Tick):
    """The forge's trees/compare for this ticket's repo."""
    forge = tick.host().forge
    return forge.trees, forge.compare


async def _look_up(tick: _Tick, step: AgentStep, run: StepRun) -> dict[str, Any]:
    trees, compare = await _contents(tick)
    repo = tick.project.github_repo
    slug = await run_slug(tick, step, run)
    branches = await tick.source.branches()

    found = named_branches(branches, step.workspace.branch, tick.values, slug)
    if not found:
        return {"status": "no_branch", "branch": _glob(step.workspace.branch, tick.values)}
    if len(found) > 1:
        return {"status": "ambiguous", "matches": sorted(branch.name for branch in found)}
    (branch,) = found
    if slug is None and (shape := _slug_shape(step.workspace.branch, tick.values)) is not None:
        # The one branch the glob found names the slug: a run that has delivered no branch yet,
        # such as one waiting for review, still resolves `{slug}` in its declared files.
        slug = _slug_between(branch.name, shape)

    start = (await spawn_heads(tick.session, run)).get(branch.name)
    at_spawn = start is not None
    if start is None:
        bases = named_branches(branches, step.workspace.base, tick.values, tick.values.get("slug"))
        if len(bases) != 1:
            base = ref(_glob(step.workspace.base, tick.values))
            return {
                "status": "lookup_failed",
                "commit": branch.sha,
                "reason": f"the base branch {base} doesn't exist"
                if not bases
                else f"{len(bases)} branches match the base branch {base}",
            }
        start = bases[0].sha

    changed: dict[str, ComparedFile] = {}
    cut_off = False
    tree: dict[str, TreeEntry] | None
    try:
        if start != branch.sha:
            comparison = await compare(repo, start, branch.sha)
            changed = {entry.path: entry for entry in comparison.files}
            cut_off = comparison.cut_off
        try:
            tree = {entry.path: entry for entry in await trees(repo, branch.sha)}
        except TreeTruncated:
            tree = None
    except ContentsNotFound as exc:
        # GitHub's answer, not an outage: asking again on every tick gets the same one.
        return {"status": "lookup_failed", "commit": branch.sha, "reason": str(exc)}

    earlier = await _earlier_files(tick, run, branch.name)
    preview = step.preview
    assert preview is not None  # `observe_outputs` checked `declares_files`
    literals = {
        path
        for item in preview.files
        if not item.is_pattern and (path := exact(item.path, tick.values, slug)) is not None
    }
    patterns = [
        exact(item.path, tick.values, slug) or _glob(item.path, tick.values)
        for item in preview.files
        if item.is_pattern
    ]
    order = dict.fromkeys([*(item["path"] for item in earlier), *changed])
    if cut_off and tree is not None:
        # Past GitHub's limit the comparison may have left a declared file out: the tree has it.
        order.update(dict.fromkeys(path for path in sorted(literals) if path in tree))
    paths = _capped(list(order), literals, patterns)

    prior = {item["path"]: item for item in earlier}
    files = [_entry(path, tree, changed.get(path), prior.get(path), cut_off) for path in paths]
    return {
        "status": "observed",
        "branch": branch.name,
        "commit": branch.sha,
        "start": start,
        "start_at_spawn": at_spawn,
        "slug": slug,
        "cut_off": cut_off or len(paths) < len(order),
        "files": files,
    }


def _capped(paths: Sequence[str], literals: set[str], patterns: Sequence[str]) -> list[str]:
    """At most `FOOTPRINT_MAX` paths, in order, keeping every literal declared one, then a
    pattern's files while there is room, then the rest."""
    if len(paths) <= FOOTPRINT_MAX:
        return list(paths)
    matched = [
        path
        for path in paths
        if path not in literals and any(matches_file_pattern(p, path) for p in patterns)
    ]
    room = max(FOOTPRINT_MAX - sum(1 for path in paths if path in literals), 0)
    kept = literals | set(matched[:room])
    room -= min(len(matched), room)
    for path in paths:
        if room == 0:
            break
        if path not in kept:
            kept.add(path)
            room -= 1
    return [path for path in paths if path in kept]


def _entry(
    path: str,
    tree: Mapping[str, TreeEntry] | None,
    compared: ComparedFile | None,
    prior: Mapping[str, Any] | None,
    cut_off: bool,
) -> dict[str, Any]:
    """One path at the run's end commit. The tree is the authority on what is there; without it
    the comparison speaks for a changed file and the earlier record for an unchanged one."""
    if tree is not None:
        at_end = tree.get(path)
        blob, size = (at_end.sha, at_end.size) if at_end is not None else (None, None)
    elif compared is not None:
        blob, size = (None if compared.status == "removed" else compared.blob), None
    else:
        blob, size = (prior or {}).get("blob"), None
    changed = True if compared is not None else (None if cut_off else False)
    return {"path": path, "blob": blob, "size": size, "changed": changed}


async def _earlier_files(tick: _Tick, run: StepRun, branch: str) -> list[Mapping[str, Any]]:
    """The files of each earlier current attempt of this step on `branch`, oldest first, from
    each attempt's latest record. An attempt on another branch changed nothing here: a rework
    that chose a new slug starts its footprint again."""
    newest = (
        select(func.max(Event.id))
        .join(StepRun, Event.step_run_id == StepRun.id)
        .where(
            Event.ticket_id == run.ticket_id,
            StepRun.ticket_id == run.ticket_id,
            StepRun.step_name == run.step_name,
            current_runs(run.generation),
            StepRun.id < run.id,
            Event.type == EventType.RUN_OUTPUTS_OBSERVED,
        )
        .group_by(Event.step_run_id)
    )
    records = await tick.session.scalars(
        select(Event.details).where(Event.id.in_(newest)).order_by(Event.step_run_id)
    )
    return [
        item
        for details in records
        if details.get("status") == "observed" and details.get("branch") == branch
        for item in details.get("files") or []
    ]
