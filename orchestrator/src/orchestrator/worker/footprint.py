"""What a restart or a redo removes from GitHub (tech-stack.md decisions 67, 76): the ticket's
branches and the pull requests opened from them; for a redo, only those of the steps it discards
that no kept step's template also names.

`footprint` is pure over what the caller observed, so the API's preview and the worker's restart
compute the same answer from their own reads."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from orchestrator.config import AgentStep, GateStep, ProcessConfig
from orchestrator.config.conditions import iter_calls
from orchestrator.github.state import BranchState, PullRequestState
from orchestrator.predicates.globs import matches_ref
from orchestrator.worker.templates import _glob
from orchestrator.worker.tick_context import _Tick

__all__ = ["Footprint", "footprint", "footprint_lines", "remove_footprint"]


@dataclass(frozen=True, slots=True)
class Footprint:
    branches: tuple[str, ...]
    #: Open pull requests whose head is one of the ticket's branches.
    close: tuple[PullRequestState, ...]
    #: Merged pull requests of the ticket's whose base is NOT one of its branches: work that
    #: already left the ticket, which a fresh start would duplicate.
    merged_outside: tuple[PullRequestState, ...]
    #: Observed branches a discarded step's template names and a kept step's names too: left as
    #: they are, with their pull requests.
    kept: tuple[str, ...] = ()
    #: Merged pull requests from the discarded steps' branches alone: nothing un-merges them.
    merged: tuple[PullRequestState, ...] = ()


#: The delivery predicates and where each takes the branch it reads: the globs the substrate's
#: delivery probe records refs by, and that a fresh generation would find already satisfied.
_REF_ARGUMENT: Final[Mapping[str, int]] = {
    "branch_exists": 0,
    "pr_open": 0,
    "pr_comment_contains": 0,
}


async def remove_footprint(tick: _Tick, plan: Footprint, comment: str) -> None:
    """Close `plan`'s pull requests with `comment` and delete its branches. The caller has
    checked the project's `contract_github` is wired."""
    github = tick.host().forge.contract(tick.project.github_repo)
    for pr in plan.close:
        await github.close_pull_request(pr.number, comment=comment)
    for name in plan.branches:
        await github.delete_branch(name)


def footprint_lines(plan: Footprint) -> list[str]:
    """The issue comment's lines for what `remove_footprint` removed."""
    lines: list[str] = []
    if plan.close:
        lines.append("- Closed " + ", ".join(f"#{pr.number}" for pr in plan.close) + ".")
    if plan.branches:
        lines.append("- Deleted " + ", ".join(f"`{name}`" for name in plan.branches) + ".")
    return lines


def footprint(
    config: ProcessConfig,
    values: Mapping[str, str],
    branches: Sequence[BranchState],
    pulls: Sequence[PullRequestState],
    *,
    steps: Collection[str] | None = None,
) -> Footprint:
    """The ticket's branches are the observed ones a `{ticket}`-bearing glob of `config` matches,
    with `{slug}` as `*`: an agent step's `workspace.branch`, a delivery postcondition's branch, a
    gate's pull request head. That is what a run cancelled or interrupted after pushing leaves,
    which records no artifact, and what a fresh generation's postconditions would otherwise find.

    A glob without `{ticket}` attributes nothing, and neither do the runs' recorded artifacts:
    the delivery probe records every ref its glob matches, so under a glob without `{ticket}` it
    records every ticket's, and a restart must never close or delete another ticket's work.

    `steps` scopes it to a redo's discard set: a ref the other steps' globs also match stays."""
    discard = [
        _glob(template, values) for template in _templates(config, steps) if "{ticket}" in template
    ]
    # Every kept template counts, `{ticket}` or not: it can only keep more, never delete a kept
    # step's branch that a discarded step's `{ticket}` glob also matches.
    keep = (
        [
            _glob(template, values)
            for template in _templates(config, set(config.step_names) - set(steps))
        ]
        if steps is not None
        else []
    )

    def matched(patterns: Sequence[str], ref: str) -> bool:
        return any(matches_ref(pattern, ref) for pattern in patterns)

    def ours(ref: str) -> bool:
        return matched(discard, ref) and not matched(keep, ref)

    mine = sorted((pr for pr in pulls if ours(pr.head_ref)), key=lambda pr: pr.number)
    return Footprint(
        branches=tuple(sorted({branch.name for branch in branches if ours(branch.name)})),
        close=tuple(pr for pr in mine if pr.state == "open" and not pr.merged),
        merged_outside=tuple(pr for pr in mine if pr.merged and not ours(pr.base_ref)),
        kept=tuple(
            sorted(
                {
                    branch.name
                    for branch in branches
                    if matched(discard, branch.name) and matched(keep, branch.name)
                }
            )
        ),
        merged=tuple(pr for pr in mine if pr.merged),
    )


def _templates(config: ProcessConfig, steps: Collection[str] | None = None) -> Iterator[str]:
    for step in config.steps:
        if steps is not None and step.name not in steps:
            continue
        if isinstance(step, AgentStep):
            yield step.workspace.branch
            for call in iter_calls(step.postconditions):
                position = _REF_ARGUMENT.get(call.name)
                if position is not None and len(call.args) > position:
                    yield call.args[position]
        elif isinstance(step, GateStep):
            yield step.gate.pr.head
