"""The closed predicate library (design.md §6) — the only executable meaning config has.

Postconditions double as the acceptance gate for agent output: the orchestrator trusts these
functions, never an agent's self-report. The standing rule is that a predicate which cannot
establish a fact returns false — nothing infers, nothing defaults to true, and an unobserved ref
is indistinguishable from an absent one.

Each implementation takes this tick's `EvalContext` plus already-expanded string arguments. Arity
is not re-checked here: `PREDICATE_ARITY` in `orchestrator.config.conditions` validates it at
parse time, and this module asserts at import that the two tables list the same names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from orchestrator.config.conditions import PREDICATE_ARITY
from orchestrator.github.projector import CHANGE_REQUEST_MARKER, LABEL_PREFIXES, step_from_label
from orchestrator.github.state import CheckRunState, PullRequestState
from orchestrator.predicates.globs import matches_path, matches_ref, resolve_refs

if TYPE_CHECKING:
    # Deferred: `evaluator.py` imports `PREDICATES` from here, so a top-level import would be
    # circular. Safe under `from __future__ import annotations` — nothing introspects these.
    from orchestrator.predicates.evaluator import EvalContext

__all__ = ["PREDICATES", "Predicate", "ci_check_runs"]


class Predicate(Protocol):
    """A library entry: this tick's evaluation context plus expanded arguments in, a verdict out.

    Every implementation takes the full `EvalContext`, not just `ctx.source`, because the comment
    predicates also need `ctx.bot_login` — one signature keeps `PREDICATES` a uniform mapping the
    evaluator never has to branch on by name.
    """

    async def __call__(self, ctx: EvalContext, /, *args: str) -> bool: ...


# --- helpers ------------------------------------------------------------------------------------


def _matching_prs(
    pull_requests: Iterable[PullRequestState], head: str, base: str
) -> Iterator[PullRequestState]:
    """PRs whose head and base both match their argument (either may be a glob)."""
    return (
        pr
        for pr in pull_requests
        if matches_ref(head, pr.head_ref) and matches_ref(base, pr.base_ref)
    )


def _all_green(runs: Iterable[CheckRunState]) -> bool:
    # Zero check runs is not green: `all()` over an empty sequence is true, which would let
    # `ci_green` pass vacuously on a branch whose CI never ran. Keep the emptiness check.
    materialized = tuple(runs)
    if not materialized:
        return False
    return all(run.status == "completed" and run.conclusion == "success" for run in materialized)


# --- predicates ---------------------------------------------------------------------------------


async def branch_exists(ctx: EvalContext, *args: str) -> bool:
    """`branch_exists(name)`: some branch matches `name` (glob or literal)."""
    (name,) = args
    return bool(resolve_refs(await ctx.source.branches(), name))


async def file_exists(ctx: EvalContext, *args: str) -> bool:
    """`file_exists(path, ref)`: some branch matching `ref` holds a path matching `path`."""
    path, ref = args
    for branch in resolve_refs(await ctx.source.branches(), ref):
        # A recursive tree listing is the most expensive call in the loop (github/state.py), so
        # return on the first hit rather than fetching a second branch's tree to learn nothing.
        if any(matches_path(path, candidate) for candidate in await ctx.source.files(branch.name)):
            return True
    return False  # a ref glob that matches nothing proves nothing


async def pr_open(ctx: EvalContext, *args: str) -> bool:
    """`pr_open(head, base)`: a matching pull request is open and not merged."""
    head, base = args
    return any(
        pr.state == "open" and not pr.merged
        for pr in _matching_prs(await ctx.source.pull_requests(), head, base)
    )


async def pr_merged(ctx: EvalContext, *args: str) -> bool:
    """`pr_merged(head, base)`: a matching pull request was merged."""
    head, base = args
    # `merged` is the fact that matters; also testing `state` only adds a way to disagree with
    # GitHub about a merged-then-reopened edge case.
    return any(pr.merged for pr in _matching_prs(await ctx.source.pull_requests(), head, base))


async def pr_approved(ctx: EvalContext, *args: str) -> bool:
    """`pr_approved(head, base, by?)`: a matching, still-open PR has an approval of its current
    head, optionally from `by` (one already-resolved login; `{roles.<name>}` expands in the
    config layer).

    The open filter keeps a gate honest: a merged or closed PR's approval is frozen evidence, so
    without it a gate rerun after rejection re-satisfies on the old PR's stale approval. The cost
    is that this predicate is not durable — it flips back to false once the PR merges, so
    evidence that must survive a merge wants `pr_merged` or a delivery marker (which is why
    `pr_comment_contains` does not filter state). The head filter is the same rule for an open
    PR: an approval given before later commits approved none of them.

    `pr_review_verdict(number)` runs after the open filter: reviews cost one round trip per PR,
    so only a PR this predicate selected pays it (memoized per tick).
    """
    head, base, *rest = args
    by = rest[0] if rest else None
    for pr in _matching_prs(await ctx.source.pull_requests(), head, base):
        if pr.state != "open" or pr.merged:
            continue
        approvals = (await ctx.source.pr_review_verdict(pr.number)).approvals_at(pr.head_sha)
        if not approvals:
            continue
        if by is None:
            return True
        # GitHub logins are case-insensitive; a config that writes `Operator` must match `operator`.
        if by.casefold() in {login.casefold() for login in approvals}:
            return True
    return False


async def pr_changes_requested(ctx: EvalContext, *args: str) -> bool:
    """`pr_changes_requested(head)`: a matching, still-open PR has a login whose latest review
    requested changes.

    The per-login verdict is last-write-wins over non-COMMENTED reviews (`github/observer.py`),
    so a login that requested changes and then approved is absent here by construction. Only
    `head` is taken, matching `ci_green`'s single-glob shape: a gate cares whether any open PR off
    this ticket's branch has outstanding requested changes, not which branch it targets. Same
    lazy-verdict discipline as `pr_approved`.
    """
    (head,) = args
    for pr in await ctx.source.pull_requests():
        if not matches_ref(head, pr.head_ref) or pr.state != "open" or pr.merged:
            continue
        if (await ctx.source.pr_review_verdict(pr.number)).changes_requested:
            return True
    return False


async def ci_check_runs(ctx: EvalContext, ref: str) -> AsyncIterator[Sequence[CheckRunState]]:
    """The check runs of each branch matching `ref`, a branch at a time: `ci_green` stops at
    the first red one."""
    for branch in resolve_refs(await ctx.source.branches(), ref):
        yield await ctx.source.check_runs(branch.sha)


async def ci_green(ctx: EvalContext, *args: str) -> bool:
    """`ci_green(ref)`: every branch matching `ref` has check runs, all completed successfully."""
    (ref,) = args
    matched = False  # nothing observed to be green
    async for runs in ci_check_runs(ctx, ref):
        # Universal over matched branches, unlike the existential glob predicates above: a stale
        # branch still matching the glob blocks and escalates to a human, the safe direction for
        # a gate. Short-circuits, so a red first branch costs one fetch.
        if not _all_green(runs):
            return False
        matched = True
    return matched


async def ticket_label(ctx: EvalContext, *args: str) -> bool:
    """`ticket_label(name)`: exact label match; label names are the orchestrator's, so no globs.

    Exact, with one equivalence: a name in the factory's own namespace matches either spelling
    of it (issue #138). A pinned config still says `adlc:<x>` while the projector now writes
    `factorial:<x>`, so a config that gates a step on a label the orchestrator itself sets
    would never see its own write and would wait for ever. A label outside that namespace is a
    human's and is matched literally — nothing renamed it.
    """
    (name,) = args
    labels = (await ctx.source.issue()).labels
    suffix = step_from_label(name)
    if suffix is None:
        return name in labels
    return any(f"{prefix}{suffix}" in labels for prefix in LABEL_PREFIXES)


async def ticket_has_assignee(ctx: EvalContext, *args: str) -> bool:
    """`ticket_has_assignee()`: the issue has at least one assignee."""
    del args  # takes none; arity is validated when the config is parsed
    return bool((await ctx.source.issue()).assignees)


def _login_matches(login: str, bot_login: str) -> bool:
    return bool(bot_login) and login.casefold() == bot_login.casefold()


def _tracker_delivery_author(ctx: EvalContext, author: str) -> bool:
    """Issue-comment fence: the tracker's own delivery author, else the configured bot login."""
    tracker = getattr(ctx.source, "tracker", None)
    if tracker is not None:
        return tracker.is_delivery_author(author)
    return _login_matches(author, ctx.bot_login)


def _forge_delivery_author(ctx: EvalContext, author: str) -> bool:
    """PR-comment fence: the forge's own delivery author, else the configured bot login."""
    forge = getattr(ctx.source, "forge", None)
    if forge is not None:
        return forge.is_delivery_author(author)
    return _login_matches(author, ctx.bot_login)


async def issue_comment_contains(ctx: EvalContext, *args: str) -> bool:
    """`issue_comment_contains(marker)`: the ticket's conversation has a comment authored by the
    App's own bot login whose body contains `marker`.

    The author fence is a security boundary: `marker` is delivery evidence, and a third party who
    pastes the same text into the ticket must not be able to forge it. The empty-`marker` and
    blank-`bot_login` guards repeat what `config/conditions.py` already refuses, on the same
    principle as `ci_green`'s zero-check-runs case: never assert a fact that cannot be
    established. No GitHub login is ever the empty string, so a blank bot login matches nobody.
    """
    (marker,) = args
    if not marker:
        return False
    if getattr(ctx.source, "tracker", None) is None and not ctx.bot_login:
        return False
    return any(
        _tracker_delivery_author(ctx, comment.author) and marker in comment.body
        for comment in await ctx.source.issue_comments()
    )


async def pr_comment_contains(ctx: EvalContext, *args: str) -> bool:
    """`pr_comment_contains(head, marker)`: a matching pull request carries a comment authored
    by the App's own bot login whose body contains `marker`.

    Same author fence as `issue_comment_contains`, less the reviewer's change requests the
    orchestrator posts under that login (`CHANGE_REQUEST_MARKER`): a person typed those words.
    `head` selects the PR by glob or literal against `head_ref`; PR state is not filtered, unlike
    `pr_changes_requested`, because delivery evidence must stay true of a PR that has since
    merged or closed — narrowing to `open` would make an already-satisfied postcondition flip
    back to false on the next tick.
    """
    head, marker = args
    if not marker:
        return False
    if getattr(ctx.source, "forge", None) is None and not ctx.bot_login:
        return False
    for pr in await ctx.source.pull_requests():
        if not matches_ref(head, pr.head_ref):
            continue
        for comment in await ctx.source.pr_comments(pr.number):
            if comment.body.startswith(CHANGE_REQUEST_MARKER):
                continue
            if _forge_delivery_author(ctx, comment.author) and marker in comment.body:
                return True
    return False


#: name -> implementation. The evaluator resolves calls through this table and nothing else.
PREDICATES: Final[Mapping[str, Predicate]] = MappingProxyType(
    {
        "branch_exists": branch_exists,
        "file_exists": file_exists,
        "pr_open": pr_open,
        "pr_merged": pr_merged,
        "pr_approved": pr_approved,
        "pr_changes_requested": pr_changes_requested,
        "ci_green": ci_green,
        "ticket_label": ticket_label,
        "ticket_has_assignee": ticket_has_assignee,
        "issue_comment_contains": issue_comment_contains,
        "pr_comment_contains": pr_comment_contains,
    }
)

# The library is declared twice: the arity table config validates against, and the
# implementations here. A name in one and not the other is either a call nothing can evaluate or
# dead code — fail at import, not on a live ticket.
if set(PREDICATES) != set(PREDICATE_ARITY):
    declared = set(PREDICATE_ARITY) - set(PREDICATES)
    implemented = set(PREDICATES) - set(PREDICATE_ARITY)
    raise RuntimeError(
        "predicate tables disagree (design.md §6): "
        f"declared but not implemented: {sorted(declared) or '-'}; "
        f"implemented but not declared: {sorted(implemented) or '-'}"
    )
