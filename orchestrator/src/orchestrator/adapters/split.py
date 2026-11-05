"""Tracker and Forge snapshot views, and the composite `StateSource` that pairs them.

`StateSource` mixes issue reads (where tickets live) with repository reads (where code lives).
A Jira project still keeps its code on GitHub or GitLab, so those halves must be chosen
apart. `CompositeStateSource` is the pairing: predicates keep calling `source.issue()` and
`source.branches()` and do not know which adapter answered.

Writes stay on `Projector` until the registries land. These protocols are the observe half
only (tech-stack.md decision 90).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from orchestrator.adapters.state import (
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    PullRequestState,
    RepoPermission,
    ReviewState,
    ReviewVerdict,
    StateSource,
)

__all__ = [
    "FORGE_KINDS",
    "CompositeStateSource",
    "Forge",
    "SourceForge",
    "SourceTracker",
    "Tracker",
    "forge_kind_of",
    "views_from_source",
]


#: Kinds that can host code. A tracker kind in this set is its own forge (decision 64).
FORGE_KINDS: Final[frozenset[str]] = frozenset({"github", "gitlab"})


def _login_matches(login: str, bot_login: str) -> bool:
    """GitHub's fence: the configured App login, case-insensitive. Blank matches nobody."""
    return bool(bot_login) and login.casefold() == bot_login.casefold()


def forge_kind_of(
    tracker_kind: str | None,
    settings: Mapping[str, object] | None = None,
) -> str | None:
    """Which code host this project uses.

    A ticket source that is also a forge decides, and a missing one is GitHub (decision 64).
    `settings["forge"]` names the forge only for a tracker that hosts no code (`jira`); with
    none, that kind has no forge. On a GitHub or GitLab source the key is ignored: the
    dashboard writes `"gitlab"` there as a form hint and never clears it, so a source switched
    back to GitHub still carries it.
    """
    if tracker_kind in FORGE_KINDS:
        return tracker_kind
    if not tracker_kind:
        return "github"
    raw = settings.get("forge") if settings is not None else None
    return raw if isinstance(raw, str) and raw in FORGE_KINDS else None


@runtime_checkable
class Tracker(Protocol):
    """Issue-side of one snapshot: the ticket, its conversation, and who may author delivery.

    Writes (labels, assignees, comment, close) stay on `Projector` for now. `is_delivery_author`
    is the issue-comment fence: a Jira tracker must not treat a GitHub bot login as its own.
    """

    async def issue(self) -> IssueState: ...
    async def issue_comments(self) -> Sequence[CommentState]: ...
    def is_delivery_author(self, login: str) -> bool: ...


@runtime_checkable
class Forge(Protocol):
    """Code-host side of one snapshot: branches, PRs, checks, reviews, permissions.

    `is_delivery_author` is the PR-comment fence. `repo` is where code lives, which can
    differ from the tracker's project key.
    """

    @property
    def repo(self) -> str: ...

    async def branches(self) -> Sequence[BranchState]: ...
    async def pull_requests(self) -> Sequence[PullRequestState]: ...
    async def files(self, ref: str) -> Sequence[str]: ...
    async def check_runs(self, sha: str) -> Sequence[CheckRunState]: ...
    async def pr_comments(self, number: int) -> Sequence[CommentState]: ...
    async def pr_reviews(self, number: int) -> Sequence[ReviewState]: ...
    async def pr_review_verdict(self, number: int) -> ReviewVerdict: ...
    async def repo_permission(self, login: str) -> RepoPermission: ...
    def is_delivery_author(self, login: str) -> bool: ...


#: `(login, bot_login) -> whether login may carry factory delivery evidence`.
AuthorFence = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class SourceTracker:
    """Issue-side of an existing `StateSource`. Pair with a `SourceForge` to compose two hosts."""

    inner: StateSource
    bot_login: str = ""
    author: AuthorFence = _login_matches

    async def issue(self) -> IssueState:
        return await self.inner.issue()

    async def issue_comments(self) -> Sequence[CommentState]:
        return await self.inner.issue_comments()

    def is_delivery_author(self, login: str) -> bool:
        return self.author(login, self.bot_login)


@dataclass(frozen=True, slots=True)
class SourceForge:
    """Code-host side of an existing `StateSource`."""

    inner: StateSource
    bot_login: str = ""
    author: AuthorFence = _login_matches

    @property
    def repo(self) -> str:
        return self.inner.repo

    async def branches(self) -> Sequence[BranchState]:
        return await self.inner.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return await self.inner.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        return await self.inner.files(ref)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        return await self.inner.check_runs(sha)

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        return await self.inner.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        return await self.inner.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        return await self.inner.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        return await self.inner.repo_permission(login)

    def is_delivery_author(self, login: str) -> bool:
        return self.author(login, self.bot_login)


def views_from_source(
    source: StateSource,
    *,
    bot_login: str = "",
    author: AuthorFence = _login_matches,
) -> tuple[SourceTracker, SourceForge]:
    """Split one `StateSource` into the two views `CompositeStateSource` pairs.

    Same-host projects (GitHub issues on GitHub, GitLab issues on GitLab) wrap the same
    source twice. A mixed pairing wraps two different sources.
    """
    return (
        SourceTracker(source, bot_login=bot_login, author=author),
        SourceForge(source, bot_login=bot_login, author=author),
    )


@dataclass(frozen=True, slots=True)
class CompositeStateSource:
    """A `StateSource` whose issue half and code half can come from different adapters."""

    tracker: Tracker
    forge: Forge

    @property
    def repo(self) -> str:
        return self.forge.repo

    async def issue(self) -> IssueState:
        return await self.tracker.issue()

    async def issue_comments(self) -> Sequence[CommentState]:
        return await self.tracker.issue_comments()

    async def branches(self) -> Sequence[BranchState]:
        return await self.forge.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return await self.forge.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        return await self.forge.files(ref)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        return await self.forge.check_runs(sha)

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        return await self.forge.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        return await self.forge.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        return await self.forge.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        return await self.forge.repo_permission(login)
