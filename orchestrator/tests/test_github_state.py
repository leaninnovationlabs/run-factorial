"""Unit tests for the observed-state contract and the label projection. No network."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit.exception import RequestFailed
from githubkit.response import Response

from orchestrator.github.client import GitHubClient
from orchestrator.github.observer import (
    GitHubStateSource,
    _Review,
    _verdicts_from_reviews,
)
from orchestrator.github.projector import (
    BLOCKED_LABELS,
    DONE_LABELS,
    EXIT_LABELS,
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_EXIT,
    LABEL_PREFIX,
    LABEL_WAITING,
    PARKING_LABELS,
    desired_status_labels,
    step_from_label,
    step_label,
)
from orchestrator.github.state import (
    AsyncKeyedMemo,
    AsyncOnce,
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    PullRequestState,
    RepoPermission,
    ReviewState,
    ReviewVerdict,
    SeededIssue,
    SnapshotState,
    StateSource,
    permission_meets,
)

ISSUE = IssueState(
    number=42,
    title="Add a widget",
    body="please",
    state="open",
    labels=frozenset({"adlc:refine", "bug"}),
    assignees=("operator",),
)


def _review(login: str | None, state: str) -> _Review:
    """A `_Review` record carrying just the verdict pair — `_verdicts_from_reviews` only ever
    reads `.login` and `.state`; the record's other fields are inert here."""
    return _Review(login=login, state=state, id=0, body="", submitted_at="")


def _full_review(
    login: str | None, state: str, *, id: int, body: str, submitted_at: str
) -> _Review:
    """A `_Review` carrying everything `_fetch_pr_reviews` reads — `.id` / `.body` /
    `.submitted_at` alongside the `.login` / `.state` pair `_review` above already covers."""
    return _Review(login=login, state=state, id=id, body=body, submitted_at=submitted_at)


@pytest.fixture
def snapshot() -> SnapshotState:
    return SnapshotState(
        repo="acme/widgets",
        issue_state=ISSUE,
        branch_states=(BranchState(name="spec/42-widget", sha="aaa"),),
        pull_request_states=(
            PullRequestState(
                number=7,
                head_ref="spec/42-widget",
                base_ref="main",
                head_sha="aaa",
                state="open",
                merged=False,
                url="https://github.com/acme/widgets/pull/7",
            ),
        ),
        review_verdicts_by_number={
            7: ReviewVerdict(approvals=("teammate",), changes_requested=("teammate-2",))
        },
        files_by_ref={"spec/42-widget": ("docs/spec.md",)},
        check_runs_by_sha={
            "aaa": (CheckRunState(name="ci", status="completed", conclusion="success"),)
        },
        issue_comment_states=(
            CommentState(author="operator", body="thanks", created_at="2026-08-20T00:00:00Z"),
        ),
        pr_comment_states_by_number={
            7: (CommentState(author="teammate", body="lgtm", created_at="2026-08-20T01:00:00Z"),)
        },
    )


# --- the contract ------------------------------------------------------------------------------


def test_snapshot_satisfies_the_protocol(snapshot: SnapshotState) -> None:
    assert isinstance(snapshot, StateSource)


def test_github_state_source_satisfies_the_protocol() -> None:
    """The real implementation and the test double must stay interchangeable — that is the whole
    reason the predicate layer codes against `StateSource`. Construction only; no calls."""
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    assert isinstance(source, StateSource)
    assert source.repo == "acme/widgets"


async def test_snapshot_returns_what_it_was_given(snapshot: SnapshotState) -> None:
    assert await snapshot.issue() is ISSUE
    assert [b.name for b in await snapshot.branches()] == ["spec/42-widget"]
    pulls = await snapshot.pull_requests()
    assert [p.number for p in pulls] == [7]
    verdict = await snapshot.pr_review_verdict(7)
    assert verdict == ReviewVerdict(approvals=("teammate",), changes_requested=("teammate-2",))
    assert list(await snapshot.files("spec/42-widget")) == ["docs/spec.md"]
    assert [c.name for c in await snapshot.check_runs("aaa")] == ["ci"]
    assert [c.author for c in await snapshot.issue_comments()] == ["operator"]
    assert [c.author for c in await snapshot.pr_comments(7)] == ["teammate"]


async def test_unobserved_refs_and_shas_are_empty(snapshot: SnapshotState) -> None:
    """Keyed lookups are lazy and per-ref: a ref the test never supplied is simply empty, not an
    error — predicates ask about refs that may not exist yet."""
    assert await snapshot.files("main") == ()
    assert await snapshot.check_runs("bbb") == ()
    assert await snapshot.pr_comments(999) == ()
    # An unsupplied verdict is an unreviewed PR — empty, never an error.
    assert await snapshot.pr_review_verdict(999) == ReviewVerdict()


async def test_snapshot_needs_no_files_or_checks() -> None:
    """Most conditions never look at a tree, so supplying one must not be required."""
    bare = SnapshotState(repo="acme/widgets", issue_state=ISSUE)
    assert await bare.files("main") == ()
    assert await bare.branches() == ()


# --- laziness / memoization --------------------------------------------------------------------


async def test_async_once_calls_the_factory_at_most_once() -> None:
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return 99

    memo = AsyncOnce(factory)
    assert calls == 0  # lazy: nothing happens before the first await
    assert await memo.get() == 99
    assert await memo.get() == 99
    assert calls == 1


async def test_async_once_is_concurrency_safe() -> None:
    """A condition tree evaluates branches concurrently; the same fetch must cost one call."""
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return calls

    memo = AsyncOnce(factory)
    results = await asyncio.gather(*(memo.get() for _ in range(5)))
    assert results == [1, 1, 1, 1, 1]
    assert calls == 1


async def test_keyed_memo_is_per_key() -> None:
    calls: list[str] = []

    async def factory(key: str) -> str:
        calls.append(key)
        return key.upper()

    memo: AsyncKeyedMemo[str, str] = AsyncKeyedMemo(factory)
    assert await memo.get("a") == "A"
    assert await memo.get("a") == "A"
    assert await memo.get("b") == "B"
    assert calls == ["a", "b"]


async def test_a_truncated_tree_is_returned_as_sent_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file past GitHub's cut reads as absent, so the truncation must at least be visible."""
    blob = SimpleNamespace(path="docs/spec.md", type="blob")
    folder = SimpleNamespace(path="docs", type="tree")

    class _Git:
        async def async_get_tree(self, *_: object, **__: object) -> SimpleNamespace:
            return SimpleNamespace(parsed_data=SimpleNamespace(truncated=True, tree=[blob, folder]))

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(git=_Git()))

    monkeypatch.setattr(source, "_api", fake_api)
    with caplog.at_level(logging.WARNING, logger="orchestrator.github.observer"):
        assert await source.files("main") == ("docs/spec.md",)
    assert "truncated" in caplog.text


async def test_issue_comments_are_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """`issue_comments()` goes through `AsyncOnce`, wired in `__init__` exactly like `issue()` and
    the other whole-snapshot fetches — a second call must not refetch."""
    calls = 0

    async def fake(self: GitHubStateSource) -> tuple[CommentState, ...]:
        nonlocal calls
        calls += 1
        return (CommentState(author="operator", body="hi", created_at="2026-08-20T00:00:00Z"),)

    monkeypatch.setattr(GitHubStateSource, "_fetch_issue_comments", fake)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    first = await source.issue_comments()
    second = await source.issue_comments()
    assert first == second
    assert calls == 1


async def test_pr_comments_are_memoized_per_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pr_comments(number)` goes through `AsyncKeyedMemo` — one fetch per distinct PR number,
    exactly like `files(ref)` / `check_runs(sha)`."""
    calls: list[int] = []

    async def fake(self: GitHubStateSource, number: int) -> tuple[CommentState, ...]:
        calls.append(number)
        return ()

    monkeypatch.setattr(GitHubStateSource, "_fetch_pr_comments", fake)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    await source.pr_comments(7)
    await source.pr_comments(7)
    await source.pr_comments(9)
    assert calls == [7, 9]


# --- review verdicts (approvals / changes_requested) --------------------------------------------


def _states(reviews: list[_Review]) -> dict[str, str]:
    return {login: r.state.upper() for login, r in _verdicts_from_reviews(reviews).items()}


def test_verdicts_from_reviews_takes_the_latest_per_login() -> None:
    verdict = _states([_review("operator", "approved"), _review("teammate", "changes_requested")])
    assert verdict == {"operator": "APPROVED", "teammate": "CHANGES_REQUESTED"}


def test_verdicts_from_reviews_last_write_wins() -> None:
    """The scenario `pr_changes_requested`'s predicate test pins from the other side: a login
    that requested changes and later approved reads as APPROVED only — chronological order in,
    last state per login out."""
    verdict = _states([_review("operator", "changes_requested"), _review("operator", "approved")])
    assert verdict == {"operator": "APPROVED"}


def test_verdicts_from_reviews_ignores_commented_reviews() -> None:
    """A COMMENTED review does not change a reviewer's verdict — it must not overwrite an earlier
    APPROVED with a state that means nothing for the gate."""
    verdict = _states([_review("operator", "approved"), _review("operator", "commented")])
    assert verdict == {"operator": "APPROVED"}


def test_verdicts_from_reviews_skips_a_review_with_no_user() -> None:
    """A deleted-account review carries `user: null` on the wire — `_fetch_reviews` maps it to
    `login=None`; it must not crash and must not count."""
    assert _verdicts_from_reviews([_review(None, "approved")]) == {}


# --- review-body observation ---------------------------------------------------------------------


async def test_pr_reviews_are_memoized_per_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pr_reviews(number)` goes through `AsyncKeyedMemo` exactly like `pr_comments(number)` —
    one fetch per distinct PR number."""
    calls: list[int] = []

    async def fake(self: GitHubStateSource, number: int) -> tuple[ReviewState, ...]:
        calls.append(number)
        return ()

    monkeypatch.setattr(GitHubStateSource, "_fetch_pr_reviews", fake)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    await source.pr_reviews(7)
    await source.pr_reviews(7)
    await source.pr_reviews(9)
    assert calls == [7, 9]


async def test_pull_listing_survives_payload_shapes_the_models_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_pull_requests` parses raw JSON, never githubkit models: the listing returns every
    PR the repo ever had, so strict whole-model validation makes the whole snapshot hostage to
    the single oddest payload in the repo's history. The poisoned entry here is the live shape
    that took down every tick of the first shadow run (2026-08-21) — a closed PR carrying
    `stack.base.sha: null` from GitHub's stacked-PR rollout, which githubkit's `PullRequestStack`
    model (schemas 26.8.14) types as non-nullable — plus a field no model has ever heard of."""
    poisoned = {
        "number": 45,
        "state": "closed",
        "html_url": "https://github.com/acme/widgets/pull/45",
        "merged_at": "2026-08-01T00:00:00Z",
        "head": {"ref": "impl/45-x", "sha": "bbb"},
        "base": {"ref": "spec/45-x", "sha": "ccc"},
        "stack": {"id": 178664, "size": 1, "position": 2, "base": {"ref": "main", "sha": None}},
        "field_nobody_modelled_yet": {"shape": ["unknowable"]},
    }
    plain = {
        "number": 94,
        "state": "open",
        "html_url": "https://github.com/acme/widgets/pull/94",
        "merged_at": None,
        "head": {"ref": "spec/93-empty-state", "sha": "aaa"},
        "base": {"ref": "main", "sha": "ddd"},
    }

    import json as _json

    class _Pulls:
        async def async_list(
            self, owner: str, repo: str, *, state: str, per_page: int, page: int
        ) -> SimpleNamespace:
            assert state == "all"
            body = [poisoned, plain] if page == 1 else []
            return SimpleNamespace(content=_json.dumps(body).encode())

    # No review fake: the listing resolves no verdicts, so a regression back to
    # the eager fan-out would reach for an endpoint this fake api does not even define.
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 93)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(pulls=_Pulls()))

    monkeypatch.setattr(source, "_api", fake_api)

    pulls = await source.pull_requests()

    assert [(p.number, p.state, p.merged) for p in pulls] == [
        (45, "closed", True),
        (94, "open", False),
    ]
    assert pulls[1].head_ref == "spec/93-empty-state"


async def test_review_verdicts_and_pr_reviews_share_one_reviews_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pr_review_verdict` (approvals / changes_requested) and `pr_reviews` (review-body
    observation) must cost ONE `pulls.async_list_reviews` round trip for the same PR, not two —
    proven here by patching the raw-review fetch itself and calling both public paths."""
    review_calls: list[int] = []
    comment_calls: list[int] = []

    async def fake_reviews(self: GitHubStateSource, number: int) -> tuple[_Review, ...]:
        review_calls.append(number)
        return (
            _full_review(
                "reviewer-1",
                "changes_requested",
                id=1,
                body="please fix X",
                submitted_at="2026-08-19T12:00:00Z",
            ),
        )

    async def fake_review_comments(
        self: GitHubStateSource, number: int
    ) -> dict[int, tuple[CommentState, ...]]:
        comment_calls.append(number)
        return {}

    monkeypatch.setattr(GitHubStateSource, "_fetch_reviews", fake_reviews)
    monkeypatch.setattr(GitHubStateSource, "_fetch_review_comments", fake_review_comments)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    verdict = await source.pr_review_verdict(7)
    reviews = await source.pr_reviews(7)

    assert verdict == ReviewVerdict(changes_requested=("reviewer-1",), review_ids={"reviewer-1": 1})
    assert [r.author for r in reviews] == ["reviewer-1"]
    assert reviews[0].body == "please fix X"
    assert review_calls == [7]  # ONE fetch, reused by both call paths
    assert comment_calls == [7]


async def test_review_fetch_is_lazy_and_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observing the PR listing costs ZERO review round trips — the eager shape paid one per PR
    the repo ever had, every tick — and a verdict costs exactly one, only for the PR something
    actually asked about, memoized so asking twice stays one fetch."""
    review_calls: list[int] = []

    def _pr(number: int, state: str, merged: bool) -> PullRequestState:
        return PullRequestState(
            number=number,
            head_ref=f"spec/93-x-{number}",
            base_ref="main",
            head_sha="aaa",
            state=state,
            merged=merged,
            url=f"https://github.test/pr/{number}",
        )

    async def fake_pulls(self: GitHubStateSource) -> tuple[PullRequestState, ...]:
        return (_pr(45, "closed", True), _pr(94, "open", False))

    async def fake_reviews(self: GitHubStateSource, number: int) -> tuple[_Review, ...]:
        review_calls.append(number)
        return (_review("teammate", "approved"), _review("teammate-2", "changes_requested"))

    monkeypatch.setattr(GitHubStateSource, "_fetch_pull_requests", fake_pulls)
    monkeypatch.setattr(GitHubStateSource, "_fetch_reviews", fake_reviews)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 93)

    await source.pull_requests()
    assert review_calls == []  # the listing alone pays for no reviews

    first = await source.pr_review_verdict(94)
    second = await source.pr_review_verdict(94)
    assert (
        first
        == second
        == ReviewVerdict(
            approvals=("teammate",),
            changes_requested=("teammate-2",),
            review_ids={"teammate": 0, "teammate-2": 0},
        )
    )
    assert review_calls == [94]  # one fetch for the selected PR; the closed #45 never cost one


async def test_review_listing_survives_payload_shapes_the_models_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_reviews` and `_fetch_review_comments` parse raw JSON: a PR's review
    history is exactly the arbitrary-historical-payload class — deleted accounts (`user: null`
    is live), nullable text, and fields the pinned models have never heard of — and one fossil
    must not take the verdict, or the review-body observation, hostage."""
    reviews_body = [
        {
            "id": 1,
            "user": None,  # a deleted account's review: carries no verdict, must not crash
            "state": "APPROVED",
            "body": "from a ghost",
            "submitted_at": "2026-08-01T00:00:00Z",
            "field_nobody_modelled_yet": {"shape": ["unknowable"]},
        },
        {
            "id": 2,
            "user": {"login": "teammate-2"},
            "state": "CHANGES_REQUESTED",
            "body": None,  # both nullable on the wire
            "submitted_at": None,
        },
    ]
    comments_body = [
        # Orphaned inline comment: nothing to attach it to, dropped exactly as before.
        {"pull_request_review_id": None, "user": {"login": "teammate-2"}, "body": "orphan"},
        {
            "pull_request_review_id": 2,
            "user": None,  # ghost inline commenter maps to the empty author
            "body": "inline note",
            "created_at": "2026-08-01T01:00:00Z",
            "unmodelled": [1, 2, 3],
        },
    ]

    import json as _json

    class _Pulls:
        async def async_list_reviews(
            self, owner: str, repo: str, number: int, *, per_page: int, page: int
        ) -> SimpleNamespace:
            return SimpleNamespace(content=_json.dumps(reviews_body if page == 1 else []).encode())

        async def async_list_review_comments(
            self, owner: str, repo: str, number: int, *, per_page: int, page: int
        ) -> SimpleNamespace:
            return SimpleNamespace(content=_json.dumps(comments_body if page == 1 else []).encode())

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(pulls=_Pulls()))

    monkeypatch.setattr(source, "_api", fake_api)

    verdict = await source.pr_review_verdict(7)
    assert verdict == ReviewVerdict(changes_requested=("teammate-2",), review_ids={"teammate-2": 2})

    reviews = await source.pr_reviews(7)
    assert [(r.author, r.body) for r in reviews] == [("teammate-2", "")]
    assert [(c.author, c.body) for c in reviews[0].comments] == [("", "inline note")]


async def test_comment_listing_survives_payload_shapes_the_models_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_comments` parses raw JSON: the conversation feeds the comment
    predicates — delivery evidence — and every comment ever left is in the listing, so one odd
    historical payload must not blind `issue_comment_contains` to the marker that IS there."""
    body = [
        {"user": None, "body": "from a ghost", "created_at": "2026-08-01T00:00:00Z"},
        {"user": {"login": "operator"}, "body": None, "performed_via_github_app": {"odd": []}},
        {"user": {"login": "teammate"}, "body": "Evidence Pack: done", "created_at": "x"},
    ]

    import json as _json

    class _Issues:
        async def async_list_comments(
            self, owner: str, repo: str, number: int, *, per_page: int, page: int
        ) -> SimpleNamespace:
            return SimpleNamespace(content=_json.dumps(body if page == 1 else []).encode())

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(issues=_Issues()))

    monkeypatch.setattr(source, "_api", fake_api)

    comments = await source.issue_comments()
    assert [(c.author, c.body) for c in comments] == [
        ("", "from a ghost"),
        ("operator", ""),
        ("teammate", "Evidence Pack: done"),
    ]


async def test_check_run_listing_survives_payload_shapes_the_models_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_check_runs` parses raw JSON: the pinned CheckRun model's
    `conclusion` Literal has no "stale" — a value GitHub itself sets on superseded runs — and
    its `pull_requests[].base.sha` is typed non-nullable, the founding casualty's exact error
    class fed by the same stacked-PR rollout. Either one on the gated sha would otherwise raise
    out of `ci_green` and kill every CI-gated ticket in the repo, tick after tick, until the run
    stopped being latest-for-its-name."""
    body = {
        "total_count": 2,
        "check_runs": [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "stale",  # not in the pinned Literal
                "completed_at": "2026-10-22T10:00:00Z",
                "pull_requests": [{"base": {"ref": "main", "sha": None}}],  # typed non-nullable
                "field_nobody_modelled_yet": {"shape": ["unknowable"]},
            },
            {
                "name": "tests",
                "status": "in_progress",
                "conclusion": None,
                "completed_at": "not a time",
            },
        ],
    }

    import json as _json

    class _Checks:
        async def async_list_for_ref(
            self, owner: str, repo: str, sha: str, *, filter_: str, per_page: int, page: int
        ) -> SimpleNamespace:
            assert filter_ == "latest"
            payload = body if page == 1 else {"total_count": 2, "check_runs": []}
            return SimpleNamespace(content=_json.dumps(payload).encode())

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(checks=_Checks()))

    monkeypatch.setattr(source, "_api", fake_api)

    runs = await source.check_runs("aaa")
    # "stale" maps through as-is: `_all_green` reads any non-"success" conclusion as not green,
    # which blocks the gate — the safe direction — instead of blinding the whole snapshot.
    assert [(r.name, r.status, r.conclusion) for r in runs] == [
        ("ci", "completed", "stale"),
        ("tests", "in_progress", None),
    ]
    assert [r.completed_at for r in runs] == [datetime(2026, 10, 22, 10, tzinfo=UTC), None]


async def test_pr_reviews_attaches_inline_comments_by_review_id() -> None:
    """By the review's own id, never by login: a login who reviewed twice must not have one
    review's inline comments attributed to the other."""
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_reviews(number: int) -> tuple[_Review, ...]:
        return (
            _full_review(
                "reviewer-1",
                "changes_requested",
                id=1,
                body="an earlier review's id",
                submitted_at="2026-08-19T12:00:00Z",
            ),
            _full_review(
                "reviewer-1",
                "changes_requested",
                id=2,
                body="the latest review",
                submitted_at="2026-08-19T13:00:00Z",
            ),
        )

    async def fake_review_comments(number: int) -> dict[int, tuple[CommentState, ...]]:
        return {
            1: (CommentState(author="reviewer-1", body="stale inline note", created_at="x"),),
            2: (CommentState(author="reviewer-1", body="current inline note", created_at="y"),),
        }

    source._pull_reviews = AsyncKeyedMemo(fake_reviews)  # type: ignore[method-assign]
    source._pr_review_comments = AsyncKeyedMemo(fake_review_comments)  # type: ignore[method-assign]
    source._pr_reviews = AsyncKeyedMemo(source._fetch_pr_reviews)  # type: ignore[method-assign]

    reviews = await source.pr_reviews(7)
    assert [(r.body, [c.body for c in r.comments]) for r in reviews] == [
        ("an earlier review's id", ["stale inline note"]),
        ("the latest review", ["current inline note"]),
    ]


async def test_pr_reviews_skips_the_inline_comments_fetch_on_an_unreviewed_pr() -> None:
    """Nothing could attach to a review that is not there."""
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    comment_calls: list[int] = []

    async def fake_reviews(number: int) -> tuple[_Review, ...]:
        return ()

    async def fake_review_comments(number: int) -> dict[int, tuple[CommentState, ...]]:
        comment_calls.append(number)
        return {}

    source._pull_reviews = AsyncKeyedMemo(fake_reviews)  # type: ignore[method-assign]
    source._pr_review_comments = AsyncKeyedMemo(fake_review_comments)  # type: ignore[method-assign]
    source._pr_reviews = AsyncKeyedMemo(source._fetch_pr_reviews)  # type: ignore[method-assign]

    reviews = await source.pr_reviews(7)
    assert reviews == ()
    assert comment_calls == []


def _raw_review_source(
    monkeypatch: pytest.MonkeyPatch,
    reviews: list[dict[str, object]],
    comments: list[dict[str, object]],
) -> GitHubStateSource:
    """A source whose review and inline-comment listings answer these raw pages."""

    class _Pulls:
        async def async_list_reviews(
            self, owner: str, repo: str, number: int, *, per_page: int, page: int
        ) -> SimpleNamespace:
            return SimpleNamespace(content=json.dumps(reviews if page == 1 else []).encode())

        async def async_list_review_comments(
            self, owner: str, repo: str, number: int, *, per_page: int, page: int
        ) -> SimpleNamespace:
            return SimpleNamespace(content=json.dumps(comments if page == 1 else []).encode())

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(pulls=_Pulls()))

    monkeypatch.setattr(source, "_api", fake_api)
    return source


async def test_pr_reviews_keeps_a_standalone_inline_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factorial#132. "Add single comment" on a diff is a COMMENTED review of its own, holding
    the comment. Keeping only each login's latest non-COMMENTED review dropped it, and the
    rework never saw it."""
    source = _raw_review_source(
        monkeypatch,
        reviews=[
            {"id": 1, "user": {"login": "teammate-2"}, "state": "COMMENTED", "body": ""},
            {
                "id": 2,
                "user": {"login": "teammate-2"},
                "state": "CHANGES_REQUESTED",
                "body": "rename",
            },
        ],
        comments=[
            {"pull_request_review_id": 1, "user": {"login": "teammate-2"}, "body": "add a field"},
            {"pull_request_review_id": 2, "user": {"login": "teammate-2"}, "body": "add a header"},
        ],
    )

    reviews = await source.pr_reviews(7)

    assert [(r.body, [c.body for c in r.comments]) for r in reviews] == [
        ("", ["add a field"]),
        ("rename", ["add a header"]),
    ]


async def test_an_inline_comment_keeps_the_file_and_line_it_was_left_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without them "this is wrong" cannot be placed. An outdated comment (the line changed
    since) has `line: null` and keeps its place in `original_line`."""
    source = _raw_review_source(
        monkeypatch,
        reviews=[
            {"id": 1, "user": {"login": "teammate-2"}, "state": "CHANGES_REQUESTED", "body": ""}
        ],
        comments=[
            {"pull_request_review_id": 1, "body": "a", "path": "spec.md", "line": 9},
            {
                "pull_request_review_id": 1,
                "body": "b",
                "path": "spec.md",
                "line": None,
                "original_line": 12,
            },
        ],
    )

    reviews = await source.pr_reviews(7)

    assert [(c.path, c.line) for c in reviews[0].comments] == [("spec.md", 9), ("spec.md", 12)]


# --- label projection --------------------------------------------------------------------------


def test_a_humans_labels_survive() -> None:
    result = desired_status_labels({"bug", "priority:high", "adlc:refine"}, step="develop")
    assert {"bug", "priority:high"} <= result


def test_exactly_one_step_label_results() -> None:
    result = desired_status_labels({"adlc:refine", "adlc:spec_gate", "bug"}, step="develop")
    step_labels = {label for label in result if label.startswith(LABEL_PREFIX)} - {
        LABEL_BLOCKED,
        LABEL_DONE,
    }
    assert step_labels == {"factorial:develop"}


def test_blocked_and_done_are_additive_status_labels() -> None:
    blocked = desired_status_labels({"bug"}, step="develop", blocked=True)
    assert blocked == {"bug", "factorial:develop", LABEL_BLOCKED}

    terminal = desired_status_labels({"adlc:code_gate", "bug"}, done=True)
    assert terminal == {"bug", LABEL_DONE}


def test_exit_is_an_additive_status_label_beside_the_step() -> None:
    """`adlc:exit` sits beside `adlc:<step>` the way `blocked` does, so an exited ticket still
    says where it left (decision 29). Any stale status label is dropped like every other."""
    exited = desired_status_labels({"bug", "adlc:plan", "adlc:blocked"}, step="plan", exited=True)
    assert exited == {"bug", "factorial:plan", LABEL_EXIT}


def test_waiting_is_an_additive_status_label_beside_the_step() -> None:
    """Without the flag the only route is the generic prefix branch, which reads "waiting" as a
    step NAME and writes `factorial:waiting` INSTEAD of `factorial:build`: an idle-parked ticket
    would stop saying where it stopped."""
    waiting = desired_status_labels({"bug", "factorial:build"}, step="build", waiting=True)
    assert waiting == {"bug", "factorial:build", LABEL_WAITING}


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("adlc:spec-review", "spec-review"),
        ("factorial:spec-review", "spec-review"),
        ("needs-triage", None),
        ("adlc:", ""),
    ],
)
def test_step_from_label_reads_either_vocabulary(label: str, expected: str | None) -> None:
    """A pinned config still names its steps `adlc:<step>`; a freshly synced one names them
    `factorial:<step>`. Both mean the same step — which prefix the ISSUE gets is `step_label`'s
    decision, so the two vocabularies never appear on one issue."""
    assert step_from_label(label) == expected


def test_a_status_label_is_recognised_in_either_vocabulary() -> None:
    """A parked ticket is recognised by its label being PRESENT, and removal is the release.

    So a set that knows only the new vocabulary reads every issue still carrying `adlc:blocked`
    as *released* and grants it a fresh attempt — the whole fleet at once, on deploy day.
    """
    assert {
        "adlc:blocked",
        "adlc:interrupted",
        "adlc:waiting",
        "factorial:blocked",
        "factorial:interrupted",
        "factorial:waiting",
    } <= PARKING_LABELS
    assert {"adlc:exit", "factorial:exit"} == EXIT_LABELS
    assert {"adlc:done", "factorial:done"} == DONE_LABELS
    assert {"adlc:blocked", "factorial:blocked"} == BLOCKED_LABELS


def test_a_step_may_not_be_named_after_a_status_label_in_either_vocabulary() -> None:
    for name in ("blocked", "done", "exit"):
        with pytest.raises(ValueError, match="reserved"):
            step_label(name)


def test_both_vocabularies_are_swept() -> None:
    """The sweep is the only thing that removes a stale step label, and it decides by prefix.

    A prefix it does not know reads as a human's label and is kept for ever, so an issue
    projected during the `.adlc` -> `.factorial` rename (issue #138) would carry both.
    """
    result = desired_status_labels(
        {"adlc:plan", "factorial:build", "adlc:blocked", "bug"}, step="code-review"
    )
    assert result == {"factorial:code-review", "bug"}


def test_exit_is_a_reserved_step_name() -> None:
    """A step named `exit` would be indistinguishable from the status label."""
    with pytest.raises(ValueError):
        step_label("exit")


def test_projection_is_idempotent() -> None:
    once = desired_status_labels({"bug", "adlc:refine"}, step="develop")
    assert desired_status_labels(once, step="develop") == once


def test_stale_factory_labels_are_dropped() -> None:
    assert LABEL_BLOCKED not in desired_status_labels(
        {LABEL_BLOCKED, "adlc:develop"}, step="develop"
    )


def test_reserved_step_names_are_rejected() -> None:
    """`blocked` / `done` are status, not steps — a step by either name would be
    indistinguishable from the status label, so config must not allow it."""
    assert step_label("develop") == "factorial:develop"
    for reserved in ("blocked", "done"):
        with pytest.raises(ValueError, match="reserved"):
            step_label(reserved)


async def test_a_seeded_issue_answers_from_the_seed_and_delegates_the_rest(
    snapshot: SnapshotState,
) -> None:
    """The listing observed the issue this tick, so the seed keeps the one-snapshot-per-tick
    promise; every other read is still the inner source's, lazily (decision 30)."""
    seed = IssueState(
        number=snapshot.issue_state.number,
        title="seeded",
        body="",
        state="closed",
        labels=frozenset({"adlc:exit"}),
        assignees=(),
    )
    seeded = SeededIssue(inner=snapshot, seed=seed)

    assert await seeded.issue() is seed
    assert seeded.repo == snapshot.repo
    assert await seeded.branches() == await snapshot.branches()
    assert await seeded.pull_requests() == await snapshot.pull_requests()
    assert await seeded.issue_comments() == await snapshot.issue_comments()
    assert await seeded.repo_permission("operator") == await snapshot.repo_permission("operator")


async def test_a_verdict_records_the_commit_each_review_was_given_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate may act only on an approval of the head it merges, so the verdict carries each
    login's review commit. A COMMENTED review on a newer commit leaves the approval where it was
    given, and a review with no commit approves nothing."""
    source = _raw_review_source(
        monkeypatch,
        reviews=[
            {"id": 1, "user": {"login": "teammate"}, "state": "APPROVED", "commit_id": "c1"},
            {"id": 2, "user": {"login": "teammate"}, "state": "COMMENTED", "commit_id": "c2"},
            {
                "id": 3,
                "user": {"login": "teammate-2"},
                "state": "CHANGES_REQUESTED",
                "commit_id": "c2",
            },
            {"id": 4, "user": {"login": "teammate-3"}, "state": "APPROVED", "commit_id": None},
        ],
        comments=[],
    )

    verdict = await source.pr_review_verdict(7)

    assert verdict.commits == {"teammate": "c1", "teammate-2": "c2"}
    assert verdict.approvals == ("teammate", "teammate-3")
    assert verdict.approvals_at("c1") == ("teammate",)
    assert verdict.approvals_at("c2") == ()


def _listing(rows: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(content=json.dumps(rows).encode())


async def test_comment_ids_are_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An image in a comment is fetched from that comment's own rendered body, by id."""

    class _Issues:
        async def async_list_comments(self, *_: object, **__: object) -> SimpleNamespace:
            return _listing(
                [{"id": 7, "user": {"login": "operator"}, "body": "hi", "created_at": "t"}]
            )

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(issues=_Issues()))

    monkeypatch.setattr(source, "_api", fake_api)
    (comment,) = await source.issue_comments()
    assert comment.id == 7


async def test_review_and_inline_comment_ids_are_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Pulls:
        async def async_list_reviews(self, *_: object, **__: object) -> SimpleNamespace:
            return _listing(
                [
                    {
                        "id": 31,
                        "user": {"login": "rev"},
                        "state": "COMMENTED",
                        "body": "b",
                        "submitted_at": "t",
                    }
                ]
            )

        async def async_list_review_comments(self, *_: object, **__: object) -> SimpleNamespace:
            return _listing(
                [
                    {
                        "id": 41,
                        "pull_request_review_id": 31,
                        "user": {"login": "rev"},
                        "body": "c",
                        "created_at": "t",
                        "path": "a.py",
                        "line": 3,
                    }
                ]
            )

    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(pulls=_Pulls()))

    monkeypatch.setattr(source, "_api", fake_api)
    (review,) = await source.pr_reviews(9)
    assert review.id == 31
    assert review.comments[0].id == 41


async def test_a_review_records_whether_its_author_is_a_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate tells bots apart by the review's own `user.type`, so it needs no lookup."""
    source = _raw_review_source(
        monkeypatch,
        reviews=[
            {
                "id": 1,
                "user": {"login": "dependabot[bot]", "type": "Bot"},
                "state": "APPROVED",
                "commit_id": "c1",
            },
            {
                "id": 2,
                "user": {"login": "operator", "type": "User"},
                "state": "APPROVED",
                "commit_id": "c1",
            },
        ],
        comments=[],
    )

    verdict = await source.pr_review_verdict(7)
    reviews = await source.pr_reviews(7)

    assert verdict.bots == frozenset({"dependabot[bot]"})
    assert [(review.author, review.bot) for review in reviews] == [
        ("dependabot[bot]", True),
        ("operator", False),
    ]


# ------------------------------------------------------------------ repository permission


def _failed(status: int) -> RequestFailed:
    request = httpx.Request(
        "GET", "https://api.github.com/repos/acme/widgets/collaborators/x/permission"
    )
    return RequestFailed(Response(httpx.Response(status, json={}, request=request), dict))


class _Repos:
    """`api.rest.repos` answering the permission lookup; `None` is a payload with `user: null`."""

    def __init__(self, answer: dict[str, bool] | Exception | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def async_get_collaborator_permission_level(
        self, owner: str, repo: str, username: str
    ) -> SimpleNamespace:
        self.asked.append(username)
        if isinstance(self.answer, Exception):
            raise self.answer
        user = None
        if self.answer is not None:
            user = SimpleNamespace(permissions=SimpleNamespace(**self.answer))
        return SimpleNamespace(parsed_data=SimpleNamespace(user=user))


def _permission_source(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, bool] | Exception | None
) -> tuple[GitHubStateSource, _Repos]:
    repos = _Repos(answer)
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)

    async def fake_api() -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(repos=repos))

    monkeypatch.setattr(source, "_api", fake_api)
    return source, repos


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"admin": True, "maintain": True, "push": True, "triage": True, "pull": True}, "admin"),
        (
            {"admin": False, "maintain": True, "push": True, "triage": True, "pull": True},
            "maintain",
        ),
        ({"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}, "write"),
        ({"admin": False, "push": False, "triage": True, "pull": True}, "triage"),
        ({"admin": False, "push": False, "pull": True}, "read"),
        ({"admin": False, "push": False, "pull": False}, "none"),
        (None, "none"),
    ],
)
async def test_repo_permission_reads_the_strongest_flag(
    monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool] | None, expected: RepoPermission
) -> None:
    """From `user.permissions`, not the top-level `permission`, which folds maintain into write
    and triage into read."""
    source, _ = _permission_source(monkeypatch, flags)
    assert await source.repo_permission("operator") == expected


async def test_repo_permission_is_none_for_a_login_github_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _permission_source(monkeypatch, _failed(404))
    assert await source.repo_permission("ghost") == "none"


@pytest.mark.parametrize("status", [403, 500, 502])
async def test_repo_permission_propagates_any_other_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """An outage is not an answer: reading it as `none` would reject a writer's review for a
    reason that says nothing about them."""
    source, _ = _permission_source(monkeypatch, _failed(status))
    with pytest.raises(RequestFailed):
        await source.repo_permission("operator")


async def test_repo_permission_costs_one_lookup_per_login_per_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, repos = _permission_source(monkeypatch, {"admin": False, "push": True, "pull": True})
    await source.repo_permission("Operator")
    await source.repo_permission("operator")
    await source.repo_permission("teammate")
    assert repos.asked == ["operator", "teammate"]


async def test_a_snapshot_answers_permissions_whatever_the_case_and_none_when_unsupplied() -> None:
    snapshot = SnapshotState(
        repo="acme/widgets", issue_state=ISSUE, permissions_by_login={"Operator": "write"}
    )
    assert await snapshot.repo_permission("operator") == "write"
    assert await snapshot.repo_permission("teammate") == "none"


def test_a_stronger_role_meets_a_weaker_floor() -> None:
    assert permission_meets("admin", "write")
    assert permission_meets("maintain", "write")
    assert permission_meets("write", "write")
    assert not permission_meets("triage", "write")
    assert not permission_meets("write", "maintain")
    assert not permission_meets("none", "write")


async def test_one_tick_lists_a_repo_s_branches_and_prs_once_for_all_its_tickets() -> None:
    """Each ticket's source paged the whole PR history and every branch itself, so a pass over
    N tickets on one repo read them N times. Outside a tick nothing is shared, so a source
    built later never reads an earlier tick's listing."""
    from orchestrator.worker.context import ProjectGitHub, github_once_per_tick

    calls: Counter[str] = Counter()

    def page_of(name: str, total: int, page: int) -> range:
        calls[name] += 1
        return range((page - 1) * 100, min(total, page * 100))

    class _Pulls:
        async def async_list(self, *_: object, page: int, **__: object) -> SimpleNamespace:
            return _listing(
                [
                    {
                        "number": i,
                        "state": "closed",
                        "html_url": f"https://github.com/acme/widgets/pull/{i}",
                        "merged_at": None,
                        "head": {"ref": f"impl/{i}", "sha": "a"},
                        "base": {"ref": "main", "sha": "b"},
                    }
                    for i in page_of("pulls", 250, page)
                ]
            )

    class _Repos:
        async def async_list_branches(self, *_: object, page: int, **__: object) -> SimpleNamespace:
            return SimpleNamespace(
                parsed_data=[
                    SimpleNamespace(name=f"b{i}", commit=SimpleNamespace(sha="c"))
                    for i in page_of("branches", 150, page)
                ]
            )

    class _Issues:
        async def async_list_comments(self, *_: object, **__: object) -> SimpleNamespace:
            calls["comments"] += 1
            return _listing([])

    class _Client(GitHubClient):
        async def api(self, repo: str) -> Any:
            return SimpleNamespace(
                rest=SimpleNamespace(pulls=_Pulls(), repos=_Repos(), issues=_Issues())
            )

    contents = SimpleNamespace(fresh_tree=None, compare=None)
    github = ProjectGitHub.over(_Client(None), contents, bot_login="bot")  # type: ignore[arg-type]

    async def reconcile(number: int) -> None:
        source = github.state_source("acme/widgets", number)
        await source.issue_comments()
        await source.pull_requests()
        await source.branches()

    with github_once_per_tick():
        await asyncio.gather(*(reconcile(n) for n in range(1, 11)))
    assert calls == {"comments": 10, "pulls": 3, "branches": 2}

    calls.clear()
    await reconcile(1)
    await reconcile(2)
    assert calls == {"comments": 2, "pulls": 6, "branches": 4}
