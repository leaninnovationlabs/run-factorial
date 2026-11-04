"""Template values against observed state: `{slug}` carry-forward, predicate-derived
expected artifacts, and the globbed matchers (`_glob`, `_find_pr`, `_pr_values`) the
orchestrator's own lookups use. See reconcile.py's module docstring for the tick's rules."""


from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import (
    AgentStep,
    ProcessConfig,
    PrRef,
    expand,
    globbed,
)
from orchestrator.config.conditions import (
    DELIVERY_PREDICATE_NAMES,
    PredicateCall,
    iter_conjunctive,
)
from orchestrator.db.models import (
    Artifact,
    ArtifactDirection,
    StepRun,
    Ticket,
    current_runs,
)
from orchestrator.github import (
    PullRequestState,
)
from orchestrator.predicates.globs import matches_ref
from orchestrator.worker.tick_context import _observed, _Tick
from runtime.contract import (
    SELECTOR_HEAD_GLOB,
    SELECTOR_MARKER,
    SELECTOR_WHERE,
    WHERE_PR,
    ArtifactKind,
    ExpectedArtifact,
)

logger = logging.getLogger(__name__)

#
# `{slug}` is the part of a branch name an agent chooses at runtime.
# `acceptance._write_artifacts` persists the observed name in `Artifact.locator`; this section
# reads it back.


def _slug_prefixes(config: ProcessConfig, values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The literal `(prefix, suffix)` pairs `{slug}` sits between, one per distinct shape the
    pinned config's `workspace.branch` templates declare — never a hardcoded `spec/`/`impl/` guess.

    `_slug_shape` expands with every value but `slug`, so any other placeholder still expands and
    `{slug}` is guaranteed to survive as the marker to split on. Steps whose template carries no
    `{slug}` are skipped; identical shapes collapse into one pair, insertion order preserved.
    """
    seen: dict[tuple[str, str], None] = {}
    for step in config.steps:
        if not isinstance(step, AgentStep):
            continue
        shape = _slug_shape(step.workspace.branch, values)
        if shape is not None:
            seen.setdefault(shape, None)
    return tuple(seen)


def _without_slug(values: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in values.items() if key != "slug"}


def _slug_shape(template: str, values: Mapping[str, str]) -> tuple[str, str] | None:
    """The literal `(prefix, suffix)` `{slug}` sits between in `template`, or None when it has
    no `{slug}`."""
    expanded = expand(template, _without_slug(values))
    if "{slug}" not in expanded:
        return None
    prefix, _, suffix = expanded.partition("{slug}")
    return prefix, suffix


def _ref_name(locator: Mapping[str, Any]) -> str | None:
    # BRANCH artifacts carry `branch`; PR artifacts carry `head` — same observed ref name.
    return locator.get("branch") or locator.get("head")


def _slug_between(name: str, shape: tuple[str, str]) -> str | None:
    """The part of an observed ref name that `{slug}` stands for, or None if `shape` does not
    describe this name. An empty slug is not a slug an agent could have chosen."""
    prefix, suffix = shape
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    end = len(name) - len(suffix) if suffix else len(name)
    return name[len(prefix) : end] or None


async def _resolve_slug(
    session: AsyncSession, ticket: Ticket, config: ProcessConfig, values: Mapping[str, str]
) -> str | None:
    """The observed slug as a template value — read per tick from durable rows, never cached
    across ticks (restart-safe, the same reason `StateSource` is re-fetched every tick).
    Recomputed from scratch each call, so resolution is not monotonic.

    Fail-safe, never a guess: exactly one distinct candidate resolves; zero leaves `{slug}`
    literal and globbed; more than one also leaves it literal, with a debug log naming every
    candidate.

    What counts as a candidate is decided per BRANCH SHAPE — one `workspace.branch` template —
    and only the newest run that saw anything for that shape gets a vote. Two attempts of one
    step are not two live answers: a rejected spec whose agent opens a fresh branch instead of
    pushing to the one it had leaves an abandoned name on record, and counting it made the whole
    remaining ticket ambiguous. Whichever branch the newest attempt landed on is the one the next
    attempt must rework.

    Across shapes the fail-safe is intact, and that is the violation it was written for: every
    step's agent picking the same slug for one ticket is a prompt convention (§6), never an
    enforced fact, so a build that disagrees with its own plan degrades to globbed behaviour
    rather than resolving to the wrong branch. A single run reporting two names under one shape
    is the same live disagreement and reads the same way.

    Only current runs' artifacts vote: a restart or a redo deletes the branches the runs before it
    named, and its agent chooses afresh.
    """
    prefixes = _slug_prefixes(config, values)
    if not prefixes:
        return None

    rows = await session.execute(
        select(StepRun.id, Artifact.locator)
        .join(StepRun, Artifact.step_run_id == StepRun.id)
        .where(
            StepRun.ticket_id == ticket.id,
            current_runs(ticket.generation),
            Artifact.direction == ArtifactDirection.OUTPUT,
            Artifact.kind.in_((ArtifactKind.BRANCH, ArtifactKind.PR)),
        )
        # `step_run.id` is the ticket's run order, so the last write for a shape wins below.
        .order_by(StepRun.id)
    )

    newest: dict[tuple[str, str], tuple[int, dict[str, None]]] = {}
    for run_id, locator in rows:
        name = _ref_name(locator)
        if not name:
            continue
        for shape in prefixes:
            candidate = _slug_between(name, shape)
            if candidate is None:
                continue
            seen_at, seen = newest.get(shape, (run_id, {}))
            if seen_at != run_id:  # a later run speaks for this shape; the earlier one is history
                seen_at, seen = run_id, {}
            seen.setdefault(candidate, None)
            newest[shape] = (seen_at, seen)

    candidates: dict[str, None] = {}
    for _, seen in newest.values():
        candidates.update(seen)

    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        logger.debug(
            "ticket %s: %d distinct {slug} candidates observed (%s); leaving {slug} literal "
            "rather than guessing",
            ticket.external_key,
            len(candidates),
            ", ".join(sorted(candidates)),
        )
    return None


#: Which postcondition predicates state a delivery, and what artifact kind each names.
#: `branch_exists`/`pr_open` derive a selector from their head-glob first argument; `pr_open`'s
#: `base` is dropped because the expectation is the substrate's delivery probe, not the acceptance
#: gate, which still evaluates the predicate in full. The comment predicates derive a COMMENT
#: selector from their marker — see `_derive_selector` for each argument shape.
#:
#: The rest of the closed library says nothing about delivery: `pr_merged`/`pr_approved`/
#: `pr_changes_requested` are a human gate's business and no agent may cause them, `ci_green`/
#: `ticket_label`/`ticket_has_assignee` are not artifacts, and no adapter can verify a FILE, so
#: deriving `file_exists` would refuse every spawn of the canonical plan step. Postconditions
#: still check all of them; only the delivery probe narrows.
_DERIVABLE_PREDICATES: Final[Mapping[str, ArtifactKind]] = {
    "branch_exists": ArtifactKind.BRANCH,
    "pr_open": ArtifactKind.PR,
    "issue_comment_contains": ArtifactKind.COMMENT,
    "pr_comment_contains": ArtifactKind.COMMENT,
}
# The NAMES are declared once, in the config layer, where lint also reads them; only the
# artifact-kind mapping is this module's. Diverging tables are an import error, not drift —
# raised, not asserted, so python -O cannot strip the guard.
if set(_DERIVABLE_PREDICATES) != DELIVERY_PREDICATE_NAMES:
    raise RuntimeError(
        "worker._DERIVABLE_PREDICATES diverged from config DELIVERY_PREDICATE_NAMES"
    )


def _expected_artifacts(step: AgentStep, values: Mapping[str, str]) -> tuple[ExpectedArtifact, ...]:
    """The step's postconditions, read as the delivery the substrate should look for.

    Derived, never configured: the postconditions already say what the step must produce, and a
    second place to say it would be a second thing to keep in sync.

    An unresolved `{slug}` survives `expand` and is then replaced with `*`, as every adapter does
    to `workspace.branch` — left as written it would be a glob no branch can match, and under the
    closure rule a guaranteed false `failed` on a run that did deliver. A glob already spelling
    the wildcard passes through untouched. Markers are expanded the same way but never globbed:
    they are matched as literal substrings, so a placeholder standing in one is kept as written.

    Deduplication keys on the kind and the whole selector: two COMMENT expectations sharing a
    `where`/glob but carrying different markers are two distinct deliveries, not duplicates.
    """
    derived: dict[tuple[ArtifactKind, tuple[tuple[str, str], ...]], ExpectedArtifact] = {}
    for call in iter_conjunctive(step.postconditions):
        kind = _DERIVABLE_PREDICATES.get(call.name)
        if kind is None or not call.args:
            continue
        selector = _derive_selector(call, values)
        if selector is None:
            continue
        key = (kind, tuple(sorted(selector.items())))
        derived.setdefault(key, ExpectedArtifact(kind=kind, selector=selector))
    return tuple(derived.values())


def _derive_selector(call: PredicateCall, values: Mapping[str, str]) -> dict[str, str] | None:
    """The `ExpectedArtifact.selector` one derivable predicate call implies — predicate-name-aware
    because `branch_exists`/`pr_open` and the comment predicates put their glob and marker in
    different argument positions.

    A marker that EXPANDS to empty (e.g. `issue_comment_contains("{ticket_owner}")` on a ticket
    admitted with no assignee) is not the literal `""` that `config/conditions.py` refuses at
    parse time: that check only sees unexpanded source text. Building an `ExpectedArtifact` from
    one would raise on every spawn attempt of the step, uncaught except by `worker/loop.py`'s
    per-ticket rollback, which retries the same tick forever writing no event. Returning `None`
    derives no expectation this tick instead; the predicate itself already reads false for an
    empty marker, so the postcondition fails visibly through the normal retry/escalate machinery.
    """
    match call.name:
        case "branch_exists" | "pr_open":
            return {SELECTOR_HEAD_GLOB: globbed(expand(call.args[0], values))}
        case "issue_comment_contains":
            (marker,) = call.args
            expanded_marker = expand(marker, values)
            return {SELECTOR_MARKER: expanded_marker} if expanded_marker else None
        case "pr_comment_contains":
            glob, marker = call.args
            expanded_marker = expand(marker, values)
            if not expanded_marker:
                return None
            return {
                SELECTOR_MARKER: expanded_marker,
                SELECTOR_WHERE: WHERE_PR,
                SELECTOR_HEAD_GLOB: globbed(expand(glob, values)),
            }
        case _:  # pragma: no cover - guarded by `_DERIVABLE_PREDICATES` above
            return None


def _glob(template: str, values: Mapping[str, str]) -> str:
    """Expand a template into a MATCHER: `{slug}` becomes `*`, and stays `*` even once a slug has
    been resolved.

    Every call site reads observed pull requests back to recover something the orchestrator itself
    decided (which PR to comment `{pr_url}` on, which PR a gate is reviewing) — never to gate an
    agent's delivery. A matcher must only widen against what happened, because narrowing loses
    observed truth: expanding a resolved slug here would make an agent that legitimately chose a
    different one unfindable, and an outcome's `{pr_url}` — rendered once, permanently, into a
    human-visible comment — would post with the placeholder still literal. Predicate ARGUMENTS are
    the opposite case, a config author's explicit choice, and keep full resolution."""
    return expand(template, _without_slug(values)).replace("{slug}", "*")


def _find_pr(
    pull_requests: Sequence[PullRequestState], head: str, base: str
) -> PullRequestState | None:
    matches = [
        pr
        for pr in pull_requests
        if matches_ref(head, pr.head_ref) and matches_ref(base, pr.base_ref)
    ]
    if not matches:
        return None
    # Prefer the one still open, then the newest — a re-run can leave an older closed PR behind.
    return max(matches, key=lambda pr: (pr.state == "open", pr.number))


async def _pr_for(tick: _Tick, ref: PrRef, *, what: str) -> PullRequestState | None:
    return _find_pr(
        await _observed(tick.source.pull_requests(), what=what),
        _glob(ref.head, tick.values),
        _glob(ref.base, tick.values),
    )


async def _pr_values(
    tick: _Tick, branch_template: str | None, pull_request: PullRequestState | None
) -> dict[str, str]:
    """`{pr_url}` for an outcome's comment, resolved from what was observed. The fetch is memoized
    per snapshot (github/state.py), so asking again costs nothing.

    The listing goes through `_observed` for `_accept`'s call, where an `onSuccess` `{pr_url}` may
    have no `pr_open` postcondition to have resolved the memo first — unwrapped, a raise there
    takes label projection down with it. The gate passes its already-resolved pick instead, so no
    listing happens on that path at all.
    """
    if pull_request is None and branch_template is not None:
        head = _glob(branch_template, tick.values)
        pull_request = _find_pr(
            await _observed(tick.source.pull_requests(), what="pr_url lookup"), head, "*"
        )
    return {} if pull_request is None else {"pr_url": pull_request.url}
