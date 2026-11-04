"""The closed action library (design.md §4) — `kind: action`'s only executable meaning.

Mirrors the predicate library's two-table discipline (`orchestrator.predicates.library`):
`config.models.ACTION_NAMES` is what config validation checks an `action:` name against at parse
time, and `ACTIONS` below is what runs it. The consistency check at the bottom of this module
fails at import if the two drift — a name in one table only is either config accepting a call
nothing can execute, or dead code.

Unlike predicates, actions take no arguments from config: `action:` is a bare name, so there is
no arity table to keep in step.

`fix_the_contract` (gate 1's contract fix) is the only registered action; `deploy` joins with
the App Runtime (design.md §3.3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol

from orchestrator.config.models import ACTION_NAMES
from orchestrator.github.state import StateSource
from orchestrator.predicates.globs import resolve_refs

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "Action",
    "ActionContext",
    "ActionResult",
    "ContractGitHub",
    "UnknownActionError",
    "fix_the_contract",
]


class UnknownActionError(LookupError):
    """An `action:` name reached the executor that the library cannot serve — a programming error:
    `config.models.ActionStep._check_registry` already refuses an unregistered name at parse time,
    so reaching here means the two tables drifted apart, not that config is wrong."""


class ContractGitHub(Protocol):
    """The narrow GitHub mutation/read surface `fix_the_contract` needs.

    Not `worker.context.Projector`: gate 1 acts on git refs (a tag, a branch delete) that the
    loop's per-tick projection never touches, and `GitHubProjector`'s five verbs are the loop's
    label/assignee/comment/close/merge vocabulary. Implemented by
    `orchestrator.github.contract.GitHubContractOps`.
    """

    async def tag_sha(self, tag: str) -> str | None:
        """The sha `refs/tags/<tag>` currently points at, or `None` if it does not exist."""
        ...

    async def create_or_move_tag(self, tag: str, sha: str) -> None:
        """Create `refs/tags/<tag>` at `sha`, or force-move it there if it already exists."""
        ...

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        """Post `comment`, then close the pull request WITHOUT merging."""
        ...

    async def delete_branch(self, name: str) -> None:
        """Delete `refs/heads/<name>`. A branch already gone is not an error."""
        ...

    async def remove_label(self, pr_number: int, label: str) -> None:
        """Remove one label from a pull request, leaving the rest untouched."""
        ...


@dataclass(frozen=True, slots=True)
class ActionContext:
    """One dispatch's inputs: the tick's observed state, its template values, and the GitHub
    mutation surface an action may need. One call signature keeps `ACTIONS` uniform.

    `github` is `ContractGitHub`-shaped because the only registered action needs exactly that; a
    differently-shaped action may need this context to grow.
    """

    source: StateSource
    values: Mapping[str, str]
    github: ContractGitHub


@dataclass(frozen=True, slots=True)
class ActionResult:
    """A verdict and why — `reason` exists because a failed result with nothing to read leaves a
    blocked ticket unexplained.

    `details` feeds both the `action_executed` event and an `onSuccess` outcome's template values
    (`worker.action_steps._action_step`).
    """

    ok: bool
    reason: str | None = None
    details: Mapping[str, str] = field(default_factory=dict)


class Action(Protocol):
    """A library entry: this dispatch's context in, a result out."""

    async def __call__(self, ctx: ActionContext, /) -> ActionResult: ...


# --- fix_the_contract ----------------------------------------------------------------------------


async def fix_the_contract(ctx: ActionContext) -> ActionResult:
    """Gate 1's contract-fixing step — freeze the acceptance tests at a tag off the spec branch.

    1. Find the one branch matching `spec/{ticket}-*` in `ctx.source.branches()`. Zero or more
       than one match fails rather than guessing — `ActionStep` takes no arguments, so the branch
       has to be derived.
    2. Derive `slug` from it and find the PR whose head is `tests/<slug>` among all observed PRs
       (`ctx.source.pull_requests()` fetches every state; closed ones matter here).
    3. Merged: the acceptance tests are already in the spec branch's history, which is what gate 1
       exists to prevent (#19, "contract-ref blindness"). Fail unconditionally, and check this
       BEFORE the closed arm — an existing tag from an earlier legitimate fix does not make a
       merge retry-safe.
    4. Closed, contract tag exists: a retry. Gate 1 closes the PR the moment it fixes the
       contract, so reading "closed" as "never fixed" would refuse every retry forever. Succeed
       with the existing tag's sha.
    5. Closed with no tag, or a base ref that is not the spec branch: fail.
    6. Open on the right base with a head commit: tag `contract/<slug>` there. That is the
       irreversible act — closing the tests PR, deleting its branch and unlabeling the spec PR are
       all best-effort afterwards, and a retry re-tags (a no-op) then finishes whatever did not.

    Not yet wired into any process: no sample or live config references `action:
    fix_the_contract`. Exercised by `test_actions.py` against a fake `ContractGitHub`.
    """
    if "ticket" not in ctx.values:
        # Every `reconcile_ticket` tick sets this (`worker/tick_context.py`'s `_Tick.values`), so
        # its absence is a caller bug, not a business outcome `ActionResult(ok=False)` should
        # absorb.
        raise KeyError(
            "ActionContext.values has no 'ticket'. Every reconcile_ticket tick sets it, so "
            "a caller built an ActionContext without it"
        )
    ticket = ctx.values["ticket"]
    spec_glob = f"spec/{ticket}-*"
    matches = resolve_refs(await ctx.source.branches(), spec_glob)
    if len(matches) != 1:
        return ActionResult(
            ok=False,
            reason=f"expected exactly one branch matching {spec_glob!r}, found {len(matches)}",
        )
    spec_branch = matches[0].name
    slug = spec_branch.removeprefix("spec/")
    tests_branch = f"tests/{slug}"

    tests_prs = [pr for pr in await ctx.source.pull_requests() if pr.head_ref == tests_branch]
    if not tests_prs:
        return ActionResult(
            ok=False,
            reason=f"no tests pull request for {tests_branch!r}, so the contract was never written",
        )
    # Newest wins (PR numbers are monotonic per repo), not `worker.templates._find_pr`'s
    # open-first tie-break: gate 1 always closes the tests PR it fixes, so state cannot break the
    # tie the way it can for an ordinary head/base match.
    pr = max(tests_prs, key=lambda candidate: candidate.number)

    if pr.merged:
        # #19 ("contract-ref blindness"). Checked before the closed-retry arm: a merged PR must
        # never read as "already fixed" just because a tag happens to exist.
        return ActionResult(
            ok=False,
            reason=(
                f"tests pull request #{pr.number} was merged into {spec_branch!r}, so the "
                "acceptance tests are already in its history. Gate 1 can't fix this contract"
            ),
        )
    if pr.state == "closed":
        # "Closed" reads as "never fixed" unless gate 1 already tagged this slug, in which case
        # closed-and-tagged is a retry of an already-successful fix.
        existing = await ctx.github.tag_sha(f"contract/{slug}")
        if existing:
            return ActionResult(ok=True, details={"contract_sha": existing, "slug": slug})
        return ActionResult(
            ok=False,
            reason=f"tests pull request #{pr.number} was closed, so the contract was never fixed",
        )
    if pr.base_ref != spec_branch:
        return ActionResult(
            ok=False,
            reason=(
                f"tests pull request #{pr.number} targets {pr.base_ref!r}, not "
                f"{spec_branch!r}, so the contract is not on that branch"
            ),
        )
    if pr.state != "open":
        # Defensive: `PullRequestState.state` is typed `str`, not a two-value Literal, so an
        # unexpected value fails here instead of falling through to the success path.
        return ActionResult(
            ok=False, reason=f"tests pull request #{pr.number} is {pr.state}, not open"
        )
    sha = pr.head_sha
    if not sha:
        return ActionResult(ok=False, reason=f"tests pull request #{pr.number} has no head commit")

    # The irreversible act. Nothing past this point may turn a written tag back into a failure.
    await ctx.github.create_or_move_tag(f"contract/{slug}", sha)

    try:
        await ctx.github.close_pull_request(
            pr.number,
            comment=(
                f"Contract fixed at `contract/{slug}` ({sha[:8]}). The tests stay frozen at that "
                f"tag, off {spec_branch}, and are never merged into it."
            ),
        )
    except Exception:
        logger.warning(
            "fix_the_contract: contract/%s tagged at %s, but closing tests PR #%d failed "
            "(best effort)",
            slug,
            sha[:8],
            pr.number,
            exc_info=True,
        )

    try:
        await ctx.github.delete_branch(tests_branch)
    except Exception:
        logger.warning(
            "fix_the_contract: contract/%s tagged at %s, but deleting %s failed (best effort)",
            slug,
            sha[:8],
            tests_branch,
            exc_info=True,
        )

    # Best effort: a missing or already-closed spec PR is not a reason to fail gate 1 once the
    # tag is written.
    spec_prs = [
        candidate
        for candidate in await ctx.source.pull_requests()
        if candidate.head_ref == spec_branch and candidate.state == "open"
    ]
    if spec_prs:
        try:
            await ctx.github.remove_label(spec_prs[0].number, "writing-tests")
        except Exception:
            logger.warning(
                "fix_the_contract: contract/%s tagged at %s, but removing writing-tests from "
                "PR #%d failed (best effort)",
                slug,
                sha[:8],
                spec_prs[0].number,
                exc_info=True,
            )

    return ActionResult(ok=True, details={"contract_sha": sha, "slug": slug})


#: name -> implementation. `worker.action_steps._start_action` resolves `action:` calls through this
#: table and nothing else.
ACTIONS: Final[Mapping[str, Action]] = MappingProxyType({"fix_the_contract": fix_the_contract})

# The library is declared twice — ACTION_NAMES for parse-time validation, ACTIONS for execution.
# Fail at import if they disagree; see the module docstring.
if set(ACTIONS) != set(ACTION_NAMES):
    declared = set(ACTION_NAMES) - set(ACTIONS)
    implemented = set(ACTIONS) - set(ACTION_NAMES)
    raise RuntimeError(
        "action tables disagree (design.md §4): "
        f"declared but not implemented: {sorted(declared) or '-'}; "
        f"implemented but not declared: {sorted(implemented) or '-'}"
    )
