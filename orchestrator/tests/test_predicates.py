"""Unit tests for the closed predicate library and its evaluator (design.md §6).

No network and no database: every case is a `SnapshotState` built from literals, which is what
the `StateSource` protocol exists for.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence

import pytest

from orchestrator.config import load_process
from orchestrator.config.conditions import (
    PREDICATE_ARITY,
    AllOf,
    AnyOf,
    Condition,
    Not,
    PredicateCall,
)
from orchestrator.config.models import AgentStep
from orchestrator.github import CHANGE_REQUEST_MARKER
from orchestrator.github.state import (
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    PullRequestState,
    RepoPermission,
    ReviewState,
    ReviewVerdict,
    SnapshotState,
    StateSource,
)
from orchestrator.predicates import (
    PREDICATES,
    EvalContext,
    Evaluation,
    ReviewFacts,
    UnknownPredicateError,
    evaluate,
    failures,
    needs,
)
from orchestrator.predicates.words import describe, need, ref
from tests.paths import SAMPLE_PROCESS

TICKET = {"ticket": "12", "ticket_owner": "operator", "repo": "acme/widgets"}

#: The App's own bot login, as a comment predicate's author fence would read it off settings
#: (`EvalContext.bot_login`). GitHub's own convention for an App's identity (`<slug>[bot]`).
BOT_LOGIN = "sentinel[bot]"

ISSUE = IssueState(
    number=12,
    title="Add a widget",
    body="please",
    state="open",
    labels=frozenset({"adlc:new", "bug"}),
    assignees=("operator",),
)

UNASSIGNED = IssueState(
    number=12, title="Add a widget", body="", state="open", labels=frozenset(), assignees=()
)


def snapshot(
    *,
    issue: IssueState = ISSUE,
    branches: tuple[BranchState, ...] = (),
    pull_requests: tuple[PullRequestState, ...] = (),
    files: Mapping[str, Sequence[str]] | None = None,
    texts: Mapping[str, Mapping[str, str]] | None = None,
    check_runs: Mapping[str, Sequence[CheckRunState]] | None = None,
    issue_comments: tuple[CommentState, ...] = (),
    pr_comments: Mapping[int, Sequence[CommentState]] | None = None,
    review_verdicts: Mapping[int, ReviewVerdict] | None = None,
) -> SnapshotState:
    return SnapshotState(
        repo="acme/widgets",
        issue_state=issue,
        branch_states=branches,
        pull_request_states=pull_requests,
        files_by_ref=dict(files or {ref: tuple(paths) for ref, paths in (texts or {}).items()}),
        file_texts_by_ref=dict(texts or {}),
        check_runs_by_sha=dict(check_runs or {}),
        issue_comment_states=issue_comments,
        pr_comment_states_by_number=dict(pr_comments or {}),
        review_verdicts_by_number=dict(review_verdicts or {}),
    )


def pull_request(
    *,
    number: int = 7,
    head: str = "spec/12-widget",
    base: str = "main",
    state: str = "open",
    merged: bool = False,
    head_sha: str = "s1",
) -> PullRequestState:
    # Approvals/changes-requested moved off the listing shape: a test that needs a
    # verdict keys a `ReviewVerdict` by this PR's number in `snapshot(review_verdicts=...)`.
    return PullRequestState(
        number=number,
        head_ref=head,
        base_ref=base,
        head_sha=head_sha,
        state=state,
        merged=merged,
        url=f"https://github.com/acme/widgets/pull/{number}",
    )


def approved(*logins: str, sha: str = "s1") -> ReviewVerdict:
    """A verdict whose `logins` approved `sha` — `pull_request()`'s default head."""
    return ReviewVerdict(approvals=logins, commits=dict.fromkeys(logins, sha))


def comment(
    author: str = BOT_LOGIN, body: str = "", created_at: str = "2026-08-20T00:00:00Z"
) -> CommentState:
    return CommentState(author=author, body=body, created_at=created_at)


def completed(conclusion: str = "success", name: str = "ci") -> CheckRunState:
    return CheckRunState(name=name, status="completed", conclusion=conclusion)


async def check(
    source: Source, call: str, values: Mapping[str, str] = TICKET, bot_login: str = BOT_LOGIN
) -> Evaluation:
    """Evaluate a single predicate call written the way config writes it."""
    return await evaluate(
        PredicateCall.model_validate(call),
        EvalContext(source=_source(source), values=values, bot_login=bot_login),
    )


async def check_condition(
    source: Source,
    condition: Condition,
    values: Mapping[str, str] = TICKET,
    bot_login: str = BOT_LOGIN,
) -> Evaluation:
    return await evaluate(
        condition, EvalContext(source=_source(source), values=values, bot_login=bot_login)
    )


class CountingState:
    """A `StateSource` that records what was actually fetched — the short-circuit assertions."""

    def __init__(self, inner: SnapshotState) -> None:
        self._inner = inner
        self.repo = inner.repo
        self.calls: Counter[str] = Counter()
        self.file_refs: list[str] = []
        self.verdict_numbers: list[int] = []

    async def issue(self) -> IssueState:
        self.calls["issue"] += 1
        return await self._inner.issue()

    async def branches(self) -> Sequence[BranchState]:
        self.calls["branches"] += 1
        return await self._inner.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        self.calls["pull_requests"] += 1
        return await self._inner.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        self.calls["files"] += 1
        self.file_refs.append(ref)
        return await self._inner.files(ref)

    async def file_text(self, ref: str, path: str) -> str | None:
        self.calls["file_text"] += 1
        return await self._inner.file_text(ref, path)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        self.calls["check_runs"] += 1
        return await self._inner.check_runs(sha)

    async def issue_comments(self) -> Sequence[CommentState]:
        self.calls["issue_comments"] += 1
        return await self._inner.issue_comments()

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        self.calls["pr_comments"] += 1
        return await self._inner.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        self.calls["pr_reviews"] += 1
        return await self._inner.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        # Recorded per PR: each entry stands for the one review GET that PR would cost live —
        # the laziness assertions check WHICH PRs were asked, not just how often.
        self.calls["pr_review_verdict"] += 1
        self.verdict_numbers.append(number)
        return await self._inner.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        self.calls["repo_permission"] += 1
        return await self._inner.repo_permission(login)


type Source = SnapshotState | CountingState


def _source(source: Source) -> StateSource:
    """Every case runs through the spy — `SnapshotState` is frozen, so its `repo` is read-only
    and it does not satisfy the (mutable-attribute) `StateSource` protocol for a type checker."""
    return source if isinstance(source, CountingState) else CountingState(source)


# --- the tables agree -----------------------------------------------------------------------


def test_library_matches_the_declared_arity_table() -> None:
    assert set(PREDICATES) == set(PREDICATE_ARITY)


# --- branch_exists --------------------------------------------------------------------------


async def test_branch_exists_matches_the_sample_glob() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    assert (await check(source, 'branch_exists("spec/{ticket}-*")')).value is True


async def test_branch_exists_literal_must_match_exactly() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    assert (await check(source, 'branch_exists("spec/12-widget")')).value is True
    assert (await check(source, 'branch_exists("spec/12")')).value is False


async def test_branch_exists_false_for_another_ticket() -> None:
    source = snapshot(branches=(BranchState(name="spec/99-other", sha="s9"),))
    assert (await check(source, 'branch_exists("spec/{ticket}-*")')).value is False


# --- file_exists ----------------------------------------------------------------------------


async def test_file_exists_matches_the_sample_path_and_ref_globs() -> None:
    source = snapshot(
        branches=(BranchState(name="spec/12-widget", sha="s1"),),
        files={"spec/12-widget": ("README.md", "specs/12-widget/spec.md")},
    )
    result = await check(source, 'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")')
    assert result.value is True


async def test_file_exists_false_when_the_path_is_absent() -> None:
    source = snapshot(
        branches=(BranchState(name="spec/12-widget", sha="s1"),),
        files={"spec/12-widget": ("README.md",)},
    )
    result = await check(source, 'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")')
    assert result.value is False


async def test_file_exists_false_when_the_ref_glob_matches_nothing() -> None:
    source = snapshot(files={"spec/12-widget": ("specs/12-widget/spec.md",)})
    result = await check(source, 'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")')
    assert result.value is False


async def test_file_exists_stops_at_the_first_matching_branch() -> None:
    # A tree listing is the expensive call; the second branch's tree must never be fetched.
    spy = CountingState(
        snapshot(
            branches=(
                BranchState(name="spec/12-a", sha="s1"),
                BranchState(name="spec/12-b", sha="s2"),
            ),
            files={
                "spec/12-a": ("specs/12-a/spec.md",),
                "spec/12-b": ("specs/12-b/spec.md",),
            },
        )
    )
    result = await check(spy, 'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")')
    assert result.value is True
    assert spy.calls["files"] == 1
    assert spy.file_refs == ["spec/12-a"]


# --- checklist_done -------------------------------------------------------------------------

PLAN = "docs/plans/{ticket}-plan.md"
PLAN_BRANCH = "spec/{ticket}-*"


def _plan(*texts: str) -> SnapshotState:
    plans = {f"docs/plans/12-plan{n or ''}.md": text for n, text in enumerate(texts)}
    return snapshot(
        branches=(BranchState(name="spec/12-widget", sha="s1"),), texts={"spec/12-widget": plans}
    )


async def _checklist(source: SnapshotState, path: str = PLAN) -> bool | None:
    return (await check(source, f'checklist_done("{path}", "{PLAN_BRANCH}")')).value


async def test_checklist_done_true_when_every_task_is_ticked() -> None:
    assert await _checklist(_plan("# Plan\n- [x] one\n  * [X] two\n")) is True


async def test_checklist_done_false_with_one_open_task() -> None:
    assert await _checklist(_plan("- [x] one\n- [ ] two\n")) is False


@pytest.mark.parametrize(
    "open_task",
    [
        "+ [ ] a plus bullet",
        "3. [ ] a numbered item",
        "4) [ ] a numbered item",
        "- [ ]",
        "-  [ ] two spaces after the bullet",
        "-\t[ ] a tab after the bullet",
        "5.  [ ] two spaces after the number",
    ],
)
async def test_checklist_done_sees_an_open_task_however_the_item_is_written(
    open_task: str,
) -> None:
    assert await _checklist(_plan(f"- [x] one\n1. [x] two\n{open_task}\n")) is False


async def test_checklist_done_false_without_any_task_box() -> None:
    assert await _checklist(_plan("# Plan\n\nNothing to tick.\n")) is False


async def test_checklist_done_false_without_the_file() -> None:
    assert await _checklist(_plan("- [x] one\n"), path="docs/plans/99-plan.md") is False


async def test_checklist_done_ignores_a_box_inside_a_fenced_block() -> None:
    text = "- [x] one\n```markdown\n- [ ] an example\n```\n~~~\n* [ ] another\n~~~\n"
    assert await _checklist(_plan(text)) is True


async def test_checklist_done_sees_open_tasks_after_a_line_that_starts_with_inline_code() -> None:
    """Backticks later on the line make it prose, not a fence: read as one, it would hide every
    open task after it."""
    text = "- [x] one\n```make check``` runs the suite.\n- [ ] two\n"
    assert await _checklist(_plan(text)) is False


@pytest.mark.parametrize(
    "example",
    [
        # An info string on a backtick line makes it no closing fence.
        "```\n```python\n- [ ] an example\n```\n",
        # A longer fence holds a shorter one as text.
        "````markdown\n```\n- [ ] an example\n```\n````\n",
    ],
)
async def test_checklist_done_reads_a_fence_as_one_block_until_its_own_closer(
    example: str,
) -> None:
    assert await _checklist(_plan(f"- [x] one\n{example}- [x] two\n")) is True
    assert await _checklist(_plan(f"- [x] one\n{example}- [ ] two\n")) is False


@pytest.mark.parametrize(
    ("text", "done"),
    [
        # A fence nested in a list item, indented four spaces: its example is not work left open.
        ("- [x] one\n    ```markdown\n    - [ ] an example\n    ```\n", True),
        # Its closer, less indented than the opener, must not open a fence that hides `two`.
        ("- [x] one\n    ```\n    - [x] an example\n```\n- [ ] two\n", False),
        # A task inside a blockquote is a task.
        ("- [x] one\n> - [ ] quoted\n> > * [ ] quoted twice\n", False),
    ],
)
async def test_checklist_done_reads_nested_fences_and_quoted_tasks(text: str, done: bool) -> None:
    assert await _checklist(_plan(text)) is done


async def test_checklist_done_false_when_one_of_two_matching_files_has_an_open_task() -> None:
    source = _plan("- [x] one\n", "- [ ] two\n")
    assert await _checklist(source, path="docs/plans/12-plan*.md") is False


async def test_checklist_done_false_for_a_file_that_reads_as_no_text() -> None:
    source = snapshot(
        branches=(BranchState(name="spec/12-widget", sha="s1"),),
        files={"spec/12-widget": ("docs/plans/12-plan.md",)},
    )
    assert await _checklist(source) is False


# --- pr_open / pr_merged --------------------------------------------------------------------


async def test_pr_open_true_for_a_matching_open_pr() -> None:
    source = snapshot(pull_requests=(pull_request(),))
    assert (await check(source, 'pr_open("spec/{ticket}-*", "main")')).value is True


async def test_pr_open_false_when_the_base_differs() -> None:
    source = snapshot(pull_requests=(pull_request(base="develop"),))
    assert (await check(source, 'pr_open("spec/{ticket}-*", "main")')).value is False


async def test_pr_open_false_once_merged() -> None:
    source = snapshot(pull_requests=(pull_request(state="closed", merged=True),))
    assert (await check(source, 'pr_open("spec/{ticket}-*", "main")')).value is False


async def test_pr_merged_only_needs_the_merged_flag() -> None:
    source = snapshot(pull_requests=(pull_request(state="open", merged=True),))
    assert (await check(source, 'pr_merged("spec/{ticket}-*", "main")')).value is True


async def test_pr_merged_false_for_a_closed_unmerged_pr() -> None:
    source = snapshot(pull_requests=(pull_request(state="closed", merged=False),))
    assert (await check(source, 'pr_merged("spec/{ticket}-*", "main")')).value is False


# --- pr_approved ----------------------------------------------------------------------------


async def test_pr_approved_true_with_any_approval() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: approved("teammate")},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is True


async def test_pr_approved_false_without_approvals() -> None:
    source = snapshot(pull_requests=(pull_request(),))
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is False


async def test_pr_approved_by_a_specific_login() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: approved("teammate")},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main", "teammate")')).value is True
    approved_by_teammate_2 = 'pr_approved("spec/{ticket}-*", "main", "teammate-2")'
    assert (await check(source, approved_by_teammate_2)).value is False


async def test_pr_approved_ignores_an_approval_of_an_earlier_commit() -> None:
    """A review keeps the commit it was given on. When commits land after it and the base branch
    does not dismiss stale approvals, GitHub still lists it as the login's latest APPROVED
    review — yet it approved none of the new code."""
    source = snapshot(
        pull_requests=(pull_request(head_sha="s2"),),
        review_verdicts={7: approved("teammate", sha="s1")},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is False
    assert (
        await check(source, 'pr_approved("spec/{ticket}-*", "main", "teammate")')
    ).value is False


async def test_pr_approved_ignores_an_approval_with_no_recorded_commit() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: ReviewVerdict(approvals=("teammate",))},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is False


async def test_pr_approved_by_is_case_insensitive() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: approved("Teammate")},
    )
    result = await check(source, 'pr_approved("spec/{ticket}-*", "main", "{ticket_owner}")')
    assert result.value is False  # operator did not approve
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main", "teammate")')).value is True


async def test_pr_approved_ignores_a_merged_prs_frozen_approval() -> None:
    """Gate-approval freshness: after a gate rejection
    and rerun, the OLD PR sits merged with its approval frozen in review history while the
    rework's new PR is open and unapproved. Both match the glob; the gate must wait for a fresh
    approval on the OPEN PR, never re-satisfy on the merged one's stale evidence."""
    stale = pull_request(number=7, state="closed", merged=True)
    rework = pull_request(number=8, head="spec/12-rework")
    source = snapshot(
        pull_requests=(stale, rework),
        review_verdicts={7: approved("teammate")},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is False
    # A fresh approval on the open rework PR — and only that — satisfies the gate.
    fresh = snapshot(
        pull_requests=(stale, rework),
        review_verdicts={
            7: approved("teammate"),
            8: approved("teammate"),
        },
    )
    assert (await check(fresh, 'pr_approved("spec/{ticket}-*", "main")')).value is True


async def test_pr_approved_false_once_the_pr_is_closed() -> None:
    # Closed-without-merge is frozen evidence the same way merged is.
    source = snapshot(
        pull_requests=(pull_request(state="closed", merged=False),),
        review_verdicts={7: approved("teammate")},
    )
    assert (await check(source, 'pr_approved("spec/{ticket}-*", "main")')).value is False


async def test_pr_approved_never_fetches_verdicts_for_unselected_prs() -> None:
    """Verdict laziness at the predicate layer: each `pr_review_verdict` call is one live
    review GET, so only a still-OPEN PR the globs matched may cost one — never the closed
    history, and never another branch's PR, whatever verdicts they might carry."""
    spy = CountingState(
        snapshot(
            pull_requests=(
                pull_request(number=7),  # open + matching: the ONE PR allowed to cost a fetch
                pull_request(number=8, state="closed", merged=True),
                pull_request(number=9, head="impl/12-widget"),
            ),
            review_verdicts={
                7: approved("teammate"),
                8: approved("teammate"),
                9: approved("teammate"),
            },
        )
    )
    assert (await check(spy, 'pr_approved("spec/{ticket}-*", "main")')).value is True
    assert spy.verdict_numbers == [7]


# --- pr_changes_requested ---------------------------------------------------------------------


async def test_pr_changes_requested_true_with_a_pending_request() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: ReviewVerdict(changes_requested=("teammate",))},
    )
    assert (await check(source, 'pr_changes_requested("spec/{ticket}-*")')).value is True


async def test_pr_changes_requested_false_without_one() -> None:
    source = snapshot(pull_requests=(pull_request(),))
    assert (await check(source, 'pr_changes_requested("spec/{ticket}-*")')).value is False


async def test_pr_changes_requested_false_when_the_head_differs() -> None:
    source = snapshot(
        pull_requests=(pull_request(head="impl/12-widget"),),
        review_verdicts={7: ReviewVerdict(changes_requested=("teammate",))},
    )
    assert (await check(source, 'pr_changes_requested("spec/{ticket}-*")')).value is False


async def test_pr_changes_requested_latest_review_wins() -> None:
    """A login that requested changes and then approved does NOT count: by the time the observer
    computes the per-login verdict (`github/observer.py`'s last-write-wins), that login's LATEST
    review is APPROVED, so it appears in `approvals` and not in `changes_requested` — the state
    this predicate reads already reflects the resolved verdict, never the raw review history."""
    source = snapshot(
        pull_requests=(pull_request(),),
        review_verdicts={7: ReviewVerdict(approvals=("teammate",), changes_requested=())},
    )
    assert (await check(source, 'pr_changes_requested("spec/{ticket}-*")')).value is False


async def test_pr_changes_requested_false_once_the_pr_is_closed() -> None:
    source = snapshot(
        pull_requests=(pull_request(state="closed", merged=False),),
        review_verdicts={7: ReviewVerdict(changes_requested=("teammate",))},
    )
    assert (await check(source, 'pr_changes_requested("spec/{ticket}-*")')).value is False


async def test_pr_changes_requested_never_fetches_verdicts_for_closed_prs() -> None:
    """The same laziness rule as `pr_approved`'s: the closed PR's stale requested-changes must
    not even be READ, let alone counted — its verdict fetch never happens."""
    spy = CountingState(
        snapshot(
            pull_requests=(
                pull_request(number=7),
                pull_request(number=8, state="closed", merged=True),
            ),
            review_verdicts={8: ReviewVerdict(changes_requested=("teammate",))},
        )
    )
    assert (await check(spy, 'pr_changes_requested("spec/{ticket}-*")')).value is False
    assert spy.verdict_numbers == [7]


# --- ci_green -------------------------------------------------------------------------------


async def test_ci_green_true_when_every_run_succeeded() -> None:
    source = snapshot(
        branches=(BranchState(name="impl/12-widget", sha="i1"),),
        check_runs={"i1": (completed(), completed(name="lint"))},
    )
    assert (await check(source, 'ci_green("impl/{ticket}-*")')).value is True


async def test_ci_green_false_with_zero_check_runs() -> None:
    # The vacuous-truth trap: no CI at all must never read as green.
    source = snapshot(branches=(BranchState(name="impl/12-widget", sha="i1"),))
    assert (await check(source, 'ci_green("impl/{ticket}-*")')).value is False


async def test_ci_green_false_with_one_failure_among_successes() -> None:
    source = snapshot(
        branches=(BranchState(name="impl/12-widget", sha="i1"),),
        check_runs={"i1": (completed(), completed("failure", name="tests"))},
    )
    assert (await check(source, 'ci_green("impl/{ticket}-*")')).value is False


async def test_ci_green_false_while_a_run_is_still_in_progress() -> None:
    source = snapshot(
        branches=(BranchState(name="impl/12-widget", sha="i1"),),
        check_runs={
            "i1": (completed(), CheckRunState(name="build", status="in_progress", conclusion=None))
        },
    )
    assert (await check(source, 'ci_green("impl/{ticket}-*")')).value is False


async def test_ci_green_false_when_the_ref_matches_no_branch() -> None:
    assert (await check(snapshot(), 'ci_green("impl/{ticket}-*")')).value is False


async def test_ci_green_requires_every_matching_branch_to_be_green() -> None:
    source = snapshot(
        branches=(
            BranchState(name="impl/12-a", sha="i1"),
            BranchState(name="impl/12-b", sha="i2"),
        ),
        check_runs={"i1": (completed(),), "i2": (completed("failure"),)},
    )
    assert (await check(source, 'ci_green("impl/{ticket}-*")')).value is False


# --- issue predicates -----------------------------------------------------------------------


async def test_ticket_label_is_an_exact_match() -> None:
    source = snapshot()
    assert (await check(source, 'ticket_label("adlc:new")')).value is True
    assert (await check(source, 'ticket_label("adlc:done")')).value is False
    # Labels are the orchestrator's own; they deliberately do not glob.
    assert (await check(source, 'ticket_label("adlc:*")')).value is False


async def test_ticket_label_matches_either_vocabulary_of_a_factory_label() -> None:
    """A pinned config still spells its labels `adlc:`, while the projector now writes
    `factorial:`. A config that gates a step on a label the orchestrator itself sets would
    otherwise never see its own write, and the step would wait for ever.

    Only the factory's own namespace is treated this way. A human's label is matched exactly,
    because nothing renamed it.
    """
    source = snapshot(
        issue=dataclasses.replace(ISSUE, labels=frozenset({"factorial:new", "priority:high"}))
    )
    assert (await check(source, 'ticket_label("adlc:new")')).value is True
    assert (await check(source, 'ticket_label("factorial:new")')).value is True
    assert (await check(source, 'ticket_label("adlc:done")')).value is False
    assert (await check(source, 'ticket_label("priority:high")')).value is True
    assert (await check(source, 'ticket_label("priority:low")')).value is False


async def test_ticket_has_assignee() -> None:
    assert (await check(snapshot(), "ticket_has_assignee()")).value is True
    assert (await check(snapshot(issue=UNASSIGNED), "ticket_has_assignee()")).value is False


# --- request_approval -------------------------------------------------------------------------


async def reviewed(branches: tuple[BranchState, ...], review: ReviewFacts | None) -> Evaluation:
    return await evaluate(
        PredicateCall.model_validate("request_approval()"),
        EvalContext(source=_source(snapshot(branches=branches)), values=TICKET, review=review),
    )


async def test_request_approval_true_when_the_branch_ends_at_the_approved_commit() -> None:
    branches = (BranchState(name="spec/12-widget", sha="s1"),)
    review = ReviewFacts(branch="spec/12-*", approved_sha="s1")
    assert (await reviewed(branches, review)).value is True


async def test_request_approval_false_without_review_facts() -> None:
    """Every evaluation but an agent step's acceptance has no facts: admission, preconditions
    and check steps must never read an approval as given."""
    branches = (BranchState(name="spec/12-widget", sha="s1"),)
    assert (await reviewed(branches, None)).value is False


async def test_request_approval_false_once_the_branch_moves_past_the_approved_commit() -> None:
    branches = (BranchState(name="spec/12-widget", sha="s2"),)
    review = ReviewFacts(branch="spec/12-*", approved_sha="s1")
    assert (await reviewed(branches, review)).value is False


async def test_request_approval_false_when_two_branches_match() -> None:
    """Two candidates leave the approved branch unnamed, so neither may carry the approval."""
    branches = (
        BranchState(name="spec/12-widget", sha="s1"),
        BranchState(name="spec/12-gadget", sha="s1"),
    )
    review = ReviewFacts(branch="spec/12-*", approved_sha="s1")
    assert (await reviewed(branches, review)).value is False


async def test_request_approval_false_without_an_approval() -> None:
    branches = (BranchState(name="spec/12-widget", sha="s1"),)
    review = ReviewFacts(branch="spec/12-*", approved_sha=None)
    assert (await reviewed(branches, review)).value is False


# --- comment predicates ----------------------------------------------------------------------
#
# The author fence is the whole point: a marker only counts as
# delivery evidence when the comment carrying it was posted by the App's own bot login.


async def test_issue_comment_contains_true_for_a_bot_authored_marker() -> None:
    source = snapshot(issue_comments=(comment(body="Evidence Pack: done"),))
    assert (await check(source, 'issue_comment_contains("Evidence Pack")')).value is True


async def test_issue_comment_contains_false_for_the_wrong_author() -> None:
    """The marker is present, but a third party posted it — the fake-delivery vector this fence
    exists to close. Must read false, not true."""
    source = snapshot(issue_comments=(comment(author="outsider", body="Evidence Pack: done"),))
    assert (await check(source, 'issue_comment_contains("Evidence Pack")')).value is False


async def test_issue_comment_contains_false_without_the_marker() -> None:
    source = snapshot(issue_comments=(comment(body="looks good to me"),))
    assert (await check(source, 'issue_comment_contains("Evidence Pack")')).value is False


async def test_issue_comment_contains_false_when_the_bot_login_is_unconfigured() -> None:
    """`ctx.bot_login` unset (empty) must never widen into "any author" — it fails closed."""
    source = snapshot(issue_comments=(comment(body="Evidence Pack: done"),))
    result = await check(source, 'issue_comment_contains("Evidence Pack")', bot_login="")
    assert result.value is False


async def test_pr_comment_contains_true_for_a_matching_pr_and_bot_author() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        pr_comments={7: (comment(body="Evidence Pack: done"),)},
    )
    result = await check(source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")')
    assert result.value is True


async def test_pr_comment_contains_skips_a_change_request_the_orchestrator_posted() -> None:
    """The App posts a reviewer's dashboard change request under its own login: words a human
    typed, which happen to contain the marker, are not delivery evidence."""
    body = f"{CHANGE_REQUEST_MARKER}\n**Changes requested**\n\n---\n\nAdd an Evidence Pack section."
    source = snapshot(pull_requests=(pull_request(),), pr_comments={7: (comment(body=body),)})
    result = await check(source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")')
    assert result.value is False


async def test_pr_comment_contains_false_for_the_wrong_author() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        pr_comments={7: (comment(author="outsider", body="Evidence Pack: done"),)},
    )
    result = await check(source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")')
    assert result.value is False


async def test_pr_comment_contains_false_when_the_head_glob_does_not_match() -> None:
    source = snapshot(
        pull_requests=(pull_request(head="impl/12-widget"),),
        pr_comments={7: (comment(body="Evidence Pack: done"),)},
    )
    result = await check(source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")')
    assert result.value is False


async def test_pr_comment_contains_only_fetches_comments_for_a_matching_pr() -> None:
    """Two PRs; only the matching one's comments are worth a fetch."""
    spy = CountingState(
        snapshot(
            pull_requests=(
                pull_request(number=7, head="spec/12-widget"),
                pull_request(number=9, head="impl/12-widget"),
            ),
            pr_comments={7: (comment(body="Evidence Pack: done"),)},
        )
    )
    result = await check(spy, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")')
    assert result.value is True
    assert spy.calls["pr_comments"] == 1


async def test_issue_comment_contains_false_when_author_and_bot_login_are_both_empty() -> None:
    """Pins the `or not ctx.bot_login` guard in `library.py`: without it, `"".casefold() ==
    "".casefold()` is True, so an unset bot login would match a comment with no author at all —
    and `author=""` is not a hypothetical, `observer.py` writes exactly that for a deleted
    GitHub account. Deleting the guard must flip this test from pass to fail."""
    source = snapshot(issue_comments=(comment(author="", body="Evidence Pack: done"),))
    result = await check(source, 'issue_comment_contains("Evidence Pack")', bot_login="")
    assert result.value is False


async def test_pr_comment_contains_false_when_author_and_bot_login_are_both_empty() -> None:
    """The PR-comment mirror of the issue-comment case above — same guard, same reachable
    `author=""`, same requirement that it read false rather than vacuously matching."""
    source = snapshot(
        pull_requests=(pull_request(),),
        pr_comments={7: (comment(author="", body="Evidence Pack: done"),)},
    )
    result = await check(
        source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")', bot_login=""
    )
    assert result.value is False


async def test_issue_comment_contains_is_case_insensitive_on_the_login() -> None:
    """GitHub logins are case-insensitive, mirroring `pr_approved`'s own `by` pin
    (`test_pr_approved_by_is_case_insensitive`); `==` in place of `casefold() == casefold()`
    would break this without breaking anything else."""
    source = snapshot(issue_comments=(comment(author="Sentinel[Bot]", body="Evidence Pack: done"),))
    result = await check(
        source, 'issue_comment_contains("Evidence Pack")', bot_login="sentinel[bot]"
    )
    assert result.value is True


async def test_pr_comment_contains_is_case_insensitive_on_the_login() -> None:
    source = snapshot(
        pull_requests=(pull_request(),),
        pr_comments={7: (comment(author="Sentinel[Bot]", body="Evidence Pack: done"),)},
    )
    result = await check(
        source, 'pr_comment_contains("spec/{ticket}-*", "Evidence Pack")', bot_login="sentinel[bot]"
    )
    assert result.value is True


# --- composition ----------------------------------------------------------------------------


async def test_not_inverts_and_explains_itself() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    condition = Not.model_validate({"not": 'branch_exists("spec/{ticket}-*")'})
    result = await check_condition(source, condition)
    assert result.value is False
    assert failures(result) == (result.reason,)
    assert 'branch_exists("spec/12-*")' in result.reason


async def test_all_requires_every_child() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    condition = AllOf.model_validate(
        {"all": ['branch_exists("spec/{ticket}-*")', "ticket_has_assignee()"]}
    )
    assert (await check_condition(source, condition)).value is True

    condition = AllOf.model_validate(
        {"all": ['branch_exists("spec/{ticket}-*")', 'ticket_label("adlc:done")']}
    )
    result = await check_condition(source, condition)
    assert result.value is False
    assert failures(result) == ('ticket_label("adlc:done") = false',)


async def test_any_needs_one_child() -> None:
    source = snapshot()
    condition = AnyOf.model_validate(
        {"any": ['ticket_label("adlc:done")', 'ticket_label("adlc:new")']}
    )
    assert (await check_condition(source, condition)).value is True

    condition = AnyOf.model_validate(
        {"any": ['ticket_label("adlc:done")', 'ticket_label("adlc:blocked")']}
    )
    result = await check_condition(source, condition)
    assert result.value is False
    # Nothing held, so every leaf is a reason the gate did not pass.
    assert failures(result) == (
        'ticket_label("adlc:done") = false',
        'ticket_label("adlc:blocked") = false',
    )


async def test_all_short_circuits_before_the_expensive_fetch() -> None:
    spy = CountingState(snapshot(branches=(BranchState(name="impl/12-a", sha="i1"),)))
    condition = AllOf.model_validate(
        {"all": ['ticket_label("adlc:done")', 'ci_green("impl/{ticket}-*")']}
    )
    result = await check_condition(spy, condition)
    assert result.value is False
    assert spy.calls["branches"] == 0
    assert spy.calls["check_runs"] == 0
    # The explanation still names the child that decided it, and only the children evaluated.
    assert len(result.children) == 1
    assert 'ticket_label("adlc:done")' in result.reason


async def test_any_short_circuits_on_the_first_truth() -> None:
    spy = CountingState(snapshot(branches=(BranchState(name="impl/12-a", sha="i1"),)))
    condition = AnyOf.model_validate(
        {"any": ["ticket_has_assignee()", 'ci_green("impl/{ticket}-*")']}
    )
    result = await check_condition(spy, condition)
    assert result.value is True
    assert spy.calls["branches"] == 0
    assert len(result.children) == 1
    assert failures(result) == ()


async def test_nested_composition() -> None:
    source = snapshot(
        branches=(BranchState(name="spec/12-widget", sha="s1"),),
        pull_requests=(pull_request(),),
    )
    condition = AllOf.model_validate(
        {
            "all": [
                {"not": 'branch_exists("impl/{ticket}-*")'},
                {
                    "any": [
                        'pr_open("spec/{ticket}-*", "main")',
                        'pr_merged("spec/{ticket}-*", "main")',
                    ]
                },
            ]
        }
    )
    assert (await check_condition(source, condition)).value is True


# --- words ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PREDICATE_ARITY))
def test_every_predicate_reads_as_words_not_as_its_call(name: str) -> None:
    low, _ = PREDICATE_ARITY[name]
    args = ["spec/12-widget", "main", "operator"][:low]
    for negated in (False, True):
        phrase = need(name, args, negated=negated)
        assert name not in phrase
        assert "(" not in phrase


def test_a_glob_or_an_unfilled_placeholder_reads_as_a_star() -> None:
    assert ref("spec/12-*") == "spec/12-*"
    assert ref("spec/12-{slug}") == "spec/12-*"
    assert ref("main") == "main"


def test_describe_expands_the_ticket_as_the_evaluator_does() -> None:
    condition = PredicateCall.model_validate('pr_merged("spec/{ticket}-*", "main")')
    assert describe(condition, TICKET) == (
        "the pull request from a branch starting with spec/12- merged into main"
    )


async def test_needs_words_the_leaves_failures_quotes() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    condition = AllOf.model_validate(
        {"all": ['branch_exists("spec/{ticket}-*")', 'pr_open("spec/{ticket}-*", "main")']}
    )
    result = await check_condition(source, condition)
    assert failures(result) == ('pr_open("spec/12-*", "main") = false',)
    assert needs(result) == ("an open pull request from a branch starting with spec/12- into main",)


async def test_needs_reads_an_unmet_any_as_one_alternative() -> None:
    condition = AnyOf.model_validate(
        {"any": ['ticket_label("adlc:done")', "ticket_has_assignee()"]}
    )
    unassigned = snapshot(issue=dataclasses.replace(ISSUE, assignees=()))
    result = await check_condition(unassigned, condition)
    assert result.value is False
    assert len(failures(result)) == 2
    assert needs(result) == ("the adlc:done label on the issue or an assignee on the issue",)


async def test_needs_words_a_false_not_as_its_negation() -> None:
    condition = Not.model_validate({"not": 'ticket_label("adlc:new")'})
    result = await check_condition(snapshot(), condition)
    assert result.value is False
    assert needs(result) == ("no adlc:new label on the issue",)


async def test_a_holding_condition_needs_nothing() -> None:
    result = await check(snapshot(), "ticket_has_assignee()")
    assert result.value is True
    assert needs(result) == ()


# --- templates ------------------------------------------------------------------------------


async def test_arguments_are_expanded_before_matching() -> None:
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    result = await check(source, 'branch_exists("spec/{ticket}-*")')
    assert result.reason == 'branch_exists("spec/12-*") = true'


async def test_an_unresolved_slug_survives_instead_of_raising() -> None:
    # `{slug}` is chosen by the agent at runtime; lenient expansion leaves it, and the `*` in the
    # config's glob is what actually covers it.
    source = snapshot(branches=(BranchState(name="spec/12-widget", sha="s1"),))
    result = await check(source, 'branch_exists("spec/{ticket}-{slug}")')
    assert result.value is False
    assert result.reason == 'branch_exists("spec/12-{slug}") = false'


# --- programming errors ---------------------------------------------------------------------


async def test_an_unknown_predicate_raises_rather_than_returning_false() -> None:
    # Config validation rejects unknown names, so reaching the evaluator is an orchestrator bug.
    call = PredicateCall.model_construct(name="deploy_succeeded", args=("prod",))
    with pytest.raises(UnknownPredicateError):
        await check_condition(snapshot(), call)


# --- end to end against the canonical sample --------------------------------------------------


def _plan_postconditions() -> Condition:
    process = load_process(SAMPLE_PROCESS)
    step = next(s for s in process.steps if s.name == "plan")
    assert isinstance(step, AgentStep)
    assert step.postconditions is not None
    return step.postconditions


def _delivered(*, with_spec: bool) -> SnapshotState:
    """What the repo looks like once the plan agent has pushed its branch and opened its PR."""
    tree = ("README.md", "specs/12-widget/plan.md")
    if with_spec:
        tree = (*tree, "specs/12-widget/spec.md")
    return snapshot(
        branches=(
            BranchState(name="main", sha="m0"),
            BranchState(name="spec/12-widget", sha="s1"),
        ),
        pull_requests=(pull_request(),),
        files={"spec/12-widget": tree},
    )


async def test_sample_plan_postconditions_hold_after_delivery() -> None:
    result = await check_condition(_delivered(with_spec=True), _plan_postconditions())
    assert result.value is True, result.reason
    assert failures(result) == ()


async def test_sample_plan_postconditions_fail_without_the_spec_file() -> None:
    result = await check_condition(_delivered(with_spec=False), _plan_postconditions())
    assert result.value is False
    assert failures(result) == ('file_exists("specs/12-*/spec.md", "spec/12-*") = false',)
