"""Observed GitHub state — the `observe` half of the reconciliation loop (design.md §3.1).

Polling is authoritative (design.md §2 decision 1); webhooks only shorten the interval. What a
tick observes is frozen into these dataclasses so the evaluate step reasons about a *snapshot*,
not a moving API.

Nothing here decides anything. Glob matching (`branch_exists("spec/{ticket}-*")`, design.md §6)
lives in the predicate layer, so every method takes a **concrete** ref or sha and returns a raw
enumeration.

`files(ref)` and `check_runs(sha)` are lazy: a recursive tree listing is the most expensive call
in the loop and most conditions never need it. Implementations memoize per snapshot — one
`StateSource` instance is one reconciliation tick.

Review verdicts are lazy too: `pull_requests()` returns the listing shape only, and
`pr_review_verdict(number)` is the per-PR seam a gate or predicate calls for the one PR it
selected. Cost accounting lives in `observer.py`'s module docstring.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Final, Literal, Protocol, cast, get_args, runtime_checkable

__all__ = [
    "AsyncKeyedMemo",
    "AsyncOnce",
    "BranchState",
    "CheckRunState",
    "CommentState",
    "IssueState",
    "PullRequestState",
    "RepoPermission",
    "ReviewState",
    "ReviewVerdict",
    "SeededIssue",
    "SnapshotState",
    "StateSource",
    "permission_meets",
]


@dataclass(frozen=True, slots=True)
class IssueState:
    number: int
    title: str
    body: str
    state: str  # "open" | "closed"
    labels: frozenset[str]
    assignees: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BranchState:
    name: str
    sha: str


@dataclass(frozen=True, slots=True)
class PullRequestState:
    """One PR as the LISTING reports it. Review verdicts are absent because they cost an extra
    round trip per PR; they live behind `StateSource.pr_review_verdict(number)`. Dropping the
    fields rather than carrying them unresolved makes a consumer that forgets the seam fail
    loudly at the attribute instead of reading a silently-empty verdict."""

    number: int
    head_ref: str
    base_ref: str
    head_sha: str
    state: str  # "open" | "closed"
    merged: bool
    url: str


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    """One PR's per-login LATEST-review verdict — what `pr_review_verdict(number)` resolves.

    Both tuples come from the same last-write-wins pass over the PR's non-COMMENTED reviews
    (`observer.py`'s `_verdicts_from_reviews`), so a login can never appear in both. An unreviewed
    PR is the empty verdict, indistinguishable from "nobody decided" — the predicate library's
    standing rule for unobserved facts."""

    approvals: tuple[str, ...] = ()  # logins whose LATEST review is APPROVED
    changes_requested: tuple[str, ...] = ()  # logins whose LATEST review is CHANGES_REQUESTED
    #: login -> the id of the review behind its verdict. A gate records the change requests a
    #: rejection answered by id, so the same review cannot reject the rework (worker/gates.py).
    review_ids: Mapping[str, int] = field(default_factory=dict)
    #: login -> the commit the review behind its verdict was submitted on. Absent when GitHub
    #: reported none, which `approvals_at` reads as approving nothing.
    commits: Mapping[str, str] = field(default_factory=dict)
    #: The logins in this verdict whose review came from a bot account (`user.type: Bot`).
    bots: frozenset[str] = frozenset()

    def approvals_at(self, sha: str) -> tuple[str, ...]:
        """The logins whose latest review approved `sha` itself.

        A review stays a login's latest after new commits land unless the base branch dismisses
        stale approvals, and the orchestrator only reports that setting. So an approval counts
        for the head it was given on and for nothing pushed after it."""
        return tuple(login for login in self.approvals if self.commits.get(login) == sha)


@dataclass(frozen=True, slots=True)
class CheckRunState:
    name: str
    status: str  # "queued" | "in_progress" | "completed"
    conclusion: str | None  # "success" | "failure" | ...
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CommentState:
    """One issue or PR comment. `created_at` is RFC3339 text, exactly as GitHub sends it — kept as
    a string because no predicate orders by it, and a `datetime` would be a second timezone
    convention to keep in sync for no behaviour gained."""

    author: str
    body: str
    created_at: str
    #: Where an inline (diff) comment was left; None for a conversation comment.
    path: str | None = None
    line: int | None = None
    #: GitHub's id for this item: what fetches its rendered body when its text embeds an image
    #: (`github/media.py`). None where nothing observed one.
    id: int | None = None
    #: True when the forge reported a bot account (GitLab notes carry this; GitHub leaves it false).
    bot: bool = False


@dataclass(frozen=True, slots=True)
class ReviewState:
    """One review on a PR, whatever its verdict — `pr_reviews()` returns every one, oldest
    first. Mirrors `CommentState`'s author/body/created_at for the review's own text; `comments`
    are the inline (diff) comments GitHub attaches to THAT review.

    Not latest-per-login like `ReviewVerdict`: a verdict is superseded by the next one, feedback
    is not. "Add single comment" on a diff is a COMMENTED review of its own, and keeping only the
    latest non-COMMENTED review per login lost those comments (factorial#132).
    """

    author: str
    body: str
    created_at: str
    comments: tuple[CommentState, ...] = ()
    #: GitHub's id for this item: what fetches its rendered body when its text embeds an image
    #: (`github/media.py`). None where nothing observed one.
    id: int | None = None
    #: The author is a bot account (`user.type: Bot`).
    bot: bool = False


#: GitHub's repository roles, weakest first. `none` is no access, including a login GitHub
#: does not know.
RepoPermission = Literal["none", "read", "triage", "write", "maintain", "admin"]

_PERMISSION_ORDER: Final[tuple[RepoPermission, ...]] = get_args(RepoPermission)


def permission_meets(held: RepoPermission, floor: RepoPermission) -> bool:
    """Whether `held` is `floor` or a stronger role."""
    return _PERMISSION_ORDER.index(held) >= _PERMISSION_ORDER.index(floor)


@runtime_checkable
class StateSource(Protocol):
    # Read-only: a mutable `repo: str` member would exclude every frozen implementation, and
    # `SnapshotState` is frozen.
    @property
    def repo(self) -> str: ...

    async def issue(self) -> IssueState: ...
    async def branches(self) -> Sequence[BranchState]: ...
    async def pull_requests(self) -> Sequence[PullRequestState]: ...
    async def files(self, ref: str) -> Sequence[str]: ...  # paths at a CONCRETE ref
    async def check_runs(self, sha: str) -> Sequence[CheckRunState]: ...
    async def issue_comments(self) -> Sequence[CommentState]: ...  # the ticket's own conversation
    async def pr_comments(self, number: int) -> Sequence[CommentState]: ...  # one PR's
    async def pr_reviews(self, number: int) -> Sequence[ReviewState]: ...  # one PR's, every review
    #: one PR's approval/changes-requested verdict — lazy and keyed, like `files`/`check_runs`:
    #: only the PR a gate or predicate selected costs the review round trip.
    async def pr_review_verdict(self, number: int) -> ReviewVerdict: ...
    #: `login`'s role on this repository, as GitHub reports it now — lazy and keyed, like
    #: `files`: only a reviewer a gate must weigh costs the lookup.
    async def repo_permission(self, login: str) -> RepoPermission: ...


# --- memoization primitives -------------------------------------------------------------------
#
# Shared by the real observer so laziness is expressed once and testable without a network.
# The lock matters: a condition tree evaluates its branches concurrently, and two `ci_green`
# checks on the same sha must cost one API call, not two.


class AsyncOnce[T]:
    """Await a factory at most once per instance; every later call returns the same value."""

    __slots__ = ("_factory", "_lock", "_set", "_value")

    def __init__(self, factory: Callable[[], Awaitable[T]]) -> None:
        self._factory = factory
        self._lock = asyncio.Lock()
        self._set = False
        self._value: T | None = None

    async def get(self) -> T:
        if not self._set:
            async with self._lock:
                if not self._set:
                    self._value = await self._factory()
                    self._set = True
        return cast("T", self._value)


class AsyncKeyedMemo[K, V]:
    """`AsyncOnce` per key — for `files(ref)` / `check_runs(sha)`, which are lazy *and* keyed."""

    __slots__ = ("_entries", "_factory", "_lock")

    def __init__(self, factory: Callable[[K], Awaitable[V]]) -> None:
        self._factory = factory
        self._entries: dict[K, AsyncOnce[V]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: K) -> V:
        entry = self._entries.get(key)
        if entry is None:
            async with self._lock:
                entry = self._entries.get(key)
                if entry is None:
                    entry = AsyncOnce(partial(self._factory, key))
                    self._entries[key] = entry
        return await entry.get()


# --- in-memory implementation -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotState:
    """A `StateSource` built from literals, with no GitHub dependency at all — what predicate
    tests construct, so the closed predicate library (design.md §6) can be exercised without a
    network or a fixture repo.

    Refs and shas that were not supplied resolve to empty: an unobserved tree is indistinguishable
    from an empty one to a predicate, and a test that cares supplies the ref.
    """

    repo: str
    issue_state: IssueState
    branch_states: tuple[BranchState, ...] = ()
    pull_request_states: tuple[PullRequestState, ...] = ()
    files_by_ref: Mapping[str, Sequence[str]] = field(default_factory=dict)
    check_runs_by_sha: Mapping[str, Sequence[CheckRunState]] = field(default_factory=dict)
    issue_comment_states: tuple[CommentState, ...] = ()
    pr_comment_states_by_number: Mapping[int, Sequence[CommentState]] = field(default_factory=dict)
    review_states_by_number: Mapping[int, Sequence[ReviewState]] = field(default_factory=dict)
    review_verdicts_by_number: Mapping[int, ReviewVerdict] = field(default_factory=dict)
    permissions_by_login: Mapping[str, RepoPermission] = field(default_factory=dict)

    async def issue(self) -> IssueState:
        return self.issue_state

    async def branches(self) -> Sequence[BranchState]:
        return self.branch_states

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return self.pull_request_states

    async def files(self, ref: str) -> Sequence[str]:
        return self.files_by_ref.get(ref, ())

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        return self.check_runs_by_sha.get(sha, ())

    async def issue_comments(self) -> Sequence[CommentState]:
        return self.issue_comment_states

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        return self.pr_comment_states_by_number.get(number, ())

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        return self.review_states_by_number.get(number, ())

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        # Unsupplied resolves to the empty verdict — an unreviewed PR, same as every other
        # unobserved key here.
        return self.review_verdicts_by_number.get(number, ReviewVerdict())

    async def repo_permission(self, login: str) -> RepoPermission:
        folded = login.casefold()
        for name, level in self.permissions_by_login.items():
            if name.casefold() == folded:
                return level
        return "none"


@dataclass(frozen=True, slots=True)
class SeededIssue:
    """A `StateSource` whose issue was observed by someone else this tick — a parked listing
    (decision 30) — and whose every other read still goes to `inner`, lazily.

    The seed keeps the one-snapshot-per-tick promise because the listing was taken this tick.
    On the parked path nothing but `issue()` is read, so `inner` costs nothing, except a due
    parked re-check (`agent_steps.recheck_parked`): its reads go to `inner`, and a run it accepts
    is projected from `inner`'s own issue, not the seed.
    """

    inner: StateSource
    seed: IssueState

    @property
    def repo(self) -> str:
        return self.inner.repo

    async def issue(self) -> IssueState:
        return self.seed

    async def branches(self) -> Sequence[BranchState]:
        return await self.inner.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return await self.inner.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        return await self.inner.files(ref)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        return await self.inner.check_runs(sha)

    async def issue_comments(self) -> Sequence[CommentState]:
        return await self.inner.issue_comments()

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        return await self.inner.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        return await self.inner.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        return await self.inner.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        return await self.inner.repo_permission(login)
