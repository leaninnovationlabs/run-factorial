"""Unit tests for the closed action library (design.md §4).

No network and no database: `fix_the_contract` is exercised against a `SnapshotState` (the same
fake `StateSource` the predicate tests use) and a hand-written fake `ContractGitHub` — never
`orchestrator.github.contract.GitHubContractOps`, which talks to real githubkit.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

import pytest

from orchestrator.actions import ACTIONS, ActionContext, fix_the_contract
from orchestrator.config import ACTION_NAMES
from orchestrator.github.state import BranchState, IssueState, PullRequestState, SnapshotState

TICKET = "12"
ISSUE = IssueState(
    number=12, title="Add a widget", body="", state="open", labels=frozenset(), assignees=()
)


@dataclass
class FakeContractGitHub:
    """Records every mutation; `tags` is pre-seedable to model "gate 1 already ran"."""

    tags: dict[str, str] = field(default_factory=dict)
    closed: list[tuple[int, str]] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    removed_labels: list[tuple[int, str]] = field(default_factory=list)

    async def tag_sha(self, tag: str) -> str | None:
        return self.tags.get(tag)

    async def create_or_move_tag(self, tag: str, sha: str) -> None:
        self.tags[tag] = sha

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        self.closed.append((number, comment))

    async def delete_branch(self, name: str) -> None:
        self.deleted_branches.append(name)

    async def remove_label(self, pr_number: int, label: str) -> None:
        self.removed_labels.append((pr_number, label))


def _ctx(
    *,
    branches: tuple[BranchState, ...] = (),
    pull_requests: tuple[PullRequestState, ...] = (),
    github: FakeContractGitHub | None = None,
) -> tuple[ActionContext, FakeContractGitHub]:
    fake_github = github if github is not None else FakeContractGitHub()
    source = SnapshotState(
        repo="acme/widgets",
        issue_state=ISSUE,
        branch_states=branches,
        pull_request_states=pull_requests,
    )
    return ActionContext(source=source, values={"ticket": TICKET}, github=fake_github), fake_github


def _tests_pr(
    *,
    number: int = 55,
    base_ref: str = "spec/12-widget",
    state: str = "open",
    head_sha: str = "c0ffee00" * 5,
    merged: bool = False,
) -> PullRequestState:
    return PullRequestState(
        number=number,
        head_ref="tests/12-widget",
        base_ref=base_ref,
        head_sha=head_sha,
        state=state,
        merged=merged,
        url=f"https://github.test/pr/{number}",
    )


@dataclass
class RaisingContractGitHub(FakeContractGitHub):
    """Same recording behaviour as `FakeContractGitHub`, except the named write verb raises —
    for proving `fix_the_contract`'s post-tag calls are best effort."""

    raises_from: str = ""

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        if self.raises_from == "close_pull_request":
            raise RuntimeError("simulated close failure")
        await super().close_pull_request(number, comment=comment)

    async def delete_branch(self, name: str) -> None:
        if self.raises_from == "delete_branch":
            raise RuntimeError("simulated delete failure")
        await super().delete_branch(name)

    async def remove_label(self, pr_number: int, label: str) -> None:
        if self.raises_from == "remove_label":
            raise RuntimeError("simulated unlabel failure")
        await super().remove_label(pr_number, label)


# --------------------------------------------------------------------------- the registry


def test_action_registry_is_the_closed_library() -> None:
    assert set(ACTIONS) == set(ACTION_NAMES) == {"fix_the_contract"}
    assert ACTIONS["fix_the_contract"] is fix_the_contract


def test_action_registry_guard_fires_on_disagreement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors `predicates/library.py`'s own import-time assert: a name declared
    (`config.models.ACTION_NAMES`) but not implemented (`actions.registry.ACTIONS`) must fail at
    import, not at dispatch. Proven by actually reloading the module under a patched declaration
    table, the same drift the two-table discipline exists to catch."""
    from orchestrator.actions import registry as actions_registry
    from orchestrator.config import models as config_models

    monkeypatch.setattr(
        config_models, "ACTION_NAMES", frozenset({"fix_the_contract", "ghost_action"})
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            importlib.reload(actions_registry)
        message = str(excinfo.value)
        assert "action tables disagree" in message
        assert "ghost_action" in message
    finally:
        # Restore the real declaration table BEFORE reloading again, so the module (and every
        # other test importing `orchestrator.actions`) sees the correct, agreeing tables.
        monkeypatch.undo()
        importlib.reload(actions_registry)


# --------------------------------------------------------------------------- fix_the_contract


async def test_fails_when_no_spec_branch_is_observed() -> None:
    ctx, _github = _ctx()
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "spec/12-*" in result.reason


async def test_fails_when_more_than_one_spec_branch_matches() -> None:
    ctx, _github = _ctx(
        branches=(
            BranchState(name="spec/12-widget", sha="a" * 40),
            BranchState(name="spec/12-revision", sha="b" * 40),
        )
    )
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "found 2" in result.reason


async def test_fails_when_no_tests_pull_request_exists() -> None:
    ctx, _github = _ctx(branches=(BranchState(name="spec/12-widget", sha="a" * 40),))
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "no tests pull request" in result.reason


async def test_closed_tests_pr_with_no_existing_tag_is_never_fixed() -> None:
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(state="closed"),),
    )
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "never fixed" in result.reason
    assert github.closed == []
    assert github.tags == {}


async def test_closed_tests_pr_with_an_existing_tag_is_already_fixed() -> None:
    """The retry case: gate 1 already ran for this slug — closed-and-tagged reads as "already
    fixed", not "never fixed"."""
    github = FakeContractGitHub(tags={"contract/12-widget": "d" * 40})
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(state="closed"),),
        github=github,
    )
    result = await fix_the_contract(ctx)
    assert result.ok is True
    assert result.details == {"contract_sha": "d" * 40, "slug": "12-widget"}
    # A retry does not re-close, re-tag, or re-delete anything — the irreversible part already
    # happened.
    assert github.closed == []
    assert github.deleted_branches == []


async def test_merged_tests_pr_fails_even_with_an_existing_tag() -> None:
    """#19 ("contract-ref blindness"): a MERGED tests PR must never read as "already fixed", even
    when a tag exists from an earlier, legitimate run — a merge means the acceptance tests are
    already sitting in the spec branch's git history, exactly what gate 1 exists to prevent. This
    is the scenario the closed-and-tagged retry arm (above) must NOT catch."""
    github = FakeContractGitHub(tags={"contract/12-widget": "d" * 40})
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(state="closed", merged=True),),
        github=github,
    )
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None
    assert "merged" in result.reason.lower()
    assert "55" in result.reason
    # Never treated as a retry: no GitHub call of any kind was made.
    assert github.closed == []
    assert github.deleted_branches == []


async def test_fails_when_the_tests_pr_targets_the_wrong_base() -> None:
    ctx, _github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(base_ref="main"),),
    )
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "targets 'main'" in result.reason


async def test_fails_when_the_tests_pr_has_no_head_commit() -> None:
    ctx, _github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(head_sha=""),),
    )
    result = await fix_the_contract(ctx)
    assert result.ok is False
    assert result.reason is not None and "no head commit" in result.reason


async def test_success_tags_closes_deletes_and_unlabels() -> None:
    spec_pr = PullRequestState(
        number=44,
        head_ref="spec/12-widget",
        base_ref="main",
        head_sha="a" * 40,
        state="open",
        merged=False,
        url="https://github.test/pr/44",
    )
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(head_sha="c0ffee00" * 5), spec_pr),
    )
    result = await fix_the_contract(ctx)

    assert result.ok is True
    assert result.details == {"contract_sha": "c0ffee00" * 5, "slug": "12-widget"}
    assert github.tags["contract/12-widget"] == "c0ffee00" * 5
    (closed_number, comment) = github.closed[0]
    assert closed_number == 55
    assert "contract/12-widget" in comment and "c0ffee00" in comment
    assert github.deleted_branches == ["tests/12-widget"]
    assert github.removed_labels == [(44, "writing-tests")]


async def test_success_with_no_open_spec_pr_skips_the_label_removal() -> None:
    """Best effort: a missing spec PR does not undo the irreversible tag/close/
    delete that already happened."""
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(head_sha="c0ffee00" * 5),),
    )
    result = await fix_the_contract(ctx)

    assert result.ok is True
    assert github.removed_labels == []


async def test_the_highest_numbered_tests_pr_wins_the_tie() -> None:
    """Two tests PRs for the same slug (should not happen in practice — gate 1 runs once — but
    an operator retrying by hand could open a second): the highest-numbered one wins, mirroring
    `gh pr list`'s default CREATED_AT-DESC ordering."""
    older = _tests_pr(number=10, head_sha="a" * 40)
    newer = _tests_pr(number=20, head_sha="b" * 40)
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="c" * 40),),
        pull_requests=(older, newer),
    )
    result = await fix_the_contract(ctx)

    assert result.ok is True
    assert result.details["contract_sha"] == "b" * 40
    (closed_number, _comment) = github.closed[0]
    assert closed_number == 20


@pytest.mark.parametrize("raises_from", ["close_pull_request", "delete_branch", "remove_label"])
async def test_post_tag_failures_are_best_effort(raises_from: str) -> None:
    """A failure on any of these three calls is logged and the sha still returned, never
    `None` — the tag write is the one irreversible act; nothing after it may turn success back
    into failure."""
    spec_pr = PullRequestState(
        number=44,
        head_ref="spec/12-widget",
        base_ref="main",
        head_sha="a" * 40,
        state="open",
        merged=False,
        url="https://github.test/pr/44",
    )
    github = RaisingContractGitHub(raises_from=raises_from)
    ctx, github = _ctx(
        branches=(BranchState(name="spec/12-widget", sha="a" * 40),),
        pull_requests=(_tests_pr(head_sha="c0ffee00" * 5), spec_pr),
        github=github,
    )
    result = await fix_the_contract(ctx)

    assert result.ok is True
    assert result.details == {"contract_sha": "c0ffee00" * 5, "slug": "12-widget"}
    assert github.tags["contract/12-widget"] == "c0ffee00" * 5  # the irreversible act, unaffected


async def test_missing_ticket_in_context_values_raises_loudly() -> None:
    """A caller-side wiring bug, not a business outcome `ActionResult(ok=False)` should absorb —
    `reconcile_ticket` always sets `values['ticket']` (worker/tick_context.py's `_Tick.values`)."""
    ctx, _github = _ctx()
    ctx = ActionContext(source=ctx.source, values={}, github=ctx.github)
    with pytest.raises(KeyError, match="ticket"):
        await fix_the_contract(ctx)
