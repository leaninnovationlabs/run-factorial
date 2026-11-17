"""`GitHubStateSource` — the real `StateSource`, backed by polling (design.md §2 decision 1).

One instance is one reconciliation tick's snapshot: every fetch is memoized, so a condition tree
that mentions the same PR five times still costs one `GET /pulls`. Construct a fresh instance per
tick; holding one across ticks stops the loop observing.

Trees, file texts, check runs and review verdicts stay lazy (`state.py`). GitHub offers no
batch review read, so resolving verdicts alongside the PR listing cost one review GET per PR the
repo had ever had, every tick; `pr_review_verdict(number)` is the seam a gate or predicate calls
for the one PR it selected.

Parsing rule: LIST endpoints that return arbitrary historical payloads are read as raw
JSON, mapping only the fields this module needs — strict whole-model validation there holds a
whole tick's snapshot hostage to the repo's single oddest entry. Single-object GETs keep
`.parsed_data`, so a bad payload fails that one read loudly and in isolation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import quote

from githubkit import GitHub, TokenAuthStrategy
from githubkit.exception import RequestFailed
from githubkit.utils import UNSET

from orchestrator.adapters.state import (
    AsyncKeyedMemo,
    AsyncOnce,
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    Mergeability,
    PullRequestState,
    RepoPermission,
    ReviewState,
    ReviewVerdict,
    StateSource,
    capped_text,
    walks_a_dot_segment,
)
from orchestrator.events.types import aware_time
from orchestrator.github.client import GitHubClient, split_repo

__all__ = [
    "BranchProtection",
    "ConfigBranchRules",
    "GitHubStateSource",
    "RepoListings",
    "capped_issue_listing",
    "config_branch_rules",
    "create_repo_label",
    "list_issues",
    "repo_branch_protection",
    "repo_issue",
    "repo_labels",
]

PER_PAGE = 100

#: The contents API answers with the file's bytes, not base64 in JSON, and up to 100 MB.
_RAW: Final = {"Accept": "application/vnd.github.raw+json"}

logger = logging.getLogger(__name__)


async def create_repo_label(
    client: GitHubClient, repo: str, name: str, colour: str, description: str
) -> None:
    """Define one label on `repo`. Needs `issues: write`, which the App already holds.

    Create-only, never update: an existing label is left alone by the caller, so this cannot
    recolour a label a repo owner set deliberately. A 422 (the label already exists — a race
    against someone creating it between the read and this write) is success, not an error.
    """
    owner, repo_name = split_repo(repo)
    api = await client.api(repo)
    try:
        await api.rest.issues.async_create_label(
            owner, repo_name, name=name, color=colour, description=description
        )
    except RequestFailed as exc:
        if exc.response.status_code != 422:
            raise


@dataclass(frozen=True, slots=True)
class BranchProtection:
    """What the default branch's protection actually is, against what the ADLC needs it to be.

    The required profile is not configurable and not this module's opinion: CLAUDE.md fixes it as
    1 approval, CODEOWNERS, the `verify` check, dismiss-stale, and no App bypass. Protection is
    the control that stops agent-authored code reaching `main` without a human, so a *read* of it
    is a security check, not a convenience.

    Read-only on purpose. Writing protection needs `administration: write`, which would let the
    App remove the very control it is being checked against — the same reasoning that keeps
    Checks at read-only (`docs/setup.md` step 2) and `workflows` unset entirely.
    """

    branch: str
    protected: bool
    #: Whether the rule enforces reviews and status checks at all. GitHub's per-setting endpoints
    #: answer 404 for a setting whose enforcement is off, so the remedy must know which it can use.
    reviews_enabled: bool = False
    checks_enabled: bool = False
    required_approvals: int = 0
    requires_codeowners: bool = False
    dismisses_stale: bool = False
    required_checks: frozenset[str] = frozenset()

    def shortfalls(self) -> tuple[str, ...]:
        """Which parts of the required profile this branch does not meet, in reading order."""
        if not self.protected:
            return ("no protection rule",)
        gaps: list[str] = []
        if self.required_approvals < 1:
            gaps.append("no required approval")
        if not self.requires_codeowners:
            gaps.append("no CODEOWNERS review")
        if not self.dismisses_stale:
            gaps.append("stale approvals not dismissed")
        if "verify" not in self.required_checks:
            gaps.append("`verify` not a required check")
        return tuple(gaps)


async def repo_branch_protection(client: GitHubClient, repo: str) -> BranchProtection:
    """Protection on `repo`'s default branch.

    Two calls, two permissions: the default branch comes from `metadata: read` (always granted),
    the protection rule from `administration: read` (granted separately, and absent by default).
    A 404 is the documented answer for an unprotected branch and is the one error read as
    evidence; every other failure — a 403 from the missing permission included — propagates so
    the caller degrades to "not checked" rather than reporting a repo as unprotected because the
    orchestrator was not allowed to look.
    """
    owner, name = split_repo(repo)
    api = await client.api(repo)
    branch = (await api.rest.repos.async_get(owner, name)).parsed_data.default_branch
    try:
        rule = (await api.rest.repos.async_get_branch_protection(owner, name, branch)).parsed_data
    except RequestFailed as exc:
        if exc.response.status_code == 404:
            return BranchProtection(branch=branch, protected=False)
        raise

    reviews = rule.required_pull_request_reviews
    checks = rule.required_status_checks
    return BranchProtection(
        branch=branch,
        protected=True,
        reviews_enabled=reviews is not None,
        checks_enabled=checks is not None,
        required_approvals=getattr(reviews, "required_approving_review_count", 0) or 0,
        requires_codeowners=bool(getattr(reviews, "require_code_owner_reviews", False)),
        dismisses_stale=bool(getattr(reviews, "dismiss_stale_reviews", False)),
        required_checks=frozenset(getattr(checks, "contexts", None) or ()),
    )


@dataclass(frozen=True, slots=True)
class ConfigBranchRules:
    """The rules GitHub applies to the config branch, against the profile issue #248 needs: no
    deletion, no force-push, and nothing that stops the builder's own commits."""

    branch: str
    head_sha: str
    rule_types: frozenset[str]

    @property
    def blocking(self) -> tuple[str, ...]:
        """Rule types that refuse the App's direct `update_ref`, which has no bypass."""
        return tuple(sorted(self.rule_types & _BLOCKING_RULES))

    @property
    def unprotected(self) -> bool:
        return not {"non_fast_forward", "deletion"} <= self.rule_types

    def shortfalls(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if blocking := self.blocking:
            verb = "stops" if len(blocking) == 1 else "stop"
            gaps.append(f"{', '.join(blocking)} {verb} the builder's commits")
        if "non_fast_forward" not in self.rule_types:
            gaps.append("force-push allowed")
        if "deletion" not in self.rule_types:
            gaps.append("deletion allowed")
        return tuple(gaps)


_BLOCKING_RULES: Final = frozenset(
    {
        "merge_queue",
        "pull_request",
        "required_deployments",
        "required_signatures",
        "required_status_checks",
        "update",
    }
)


async def config_branch_rules(
    client: GitHubClient, repo: str, branch: str
) -> ConfigBranchRules | None:
    """The active rules on `branch` and its head, or None when the branch does not exist.

    The rules-for-branch read needs only `metadata: read`, unlike classic protection. Every
    failure but the branch's 404 propagates, so the caller degrades to "not checked".
    """
    owner, name = split_repo(repo)
    api = await client.api(repo)
    try:
        head = (await api.rest.repos.async_get_branch(owner, name, branch)).parsed_data.commit.sha
    except RequestFailed as exc:
        if exc.response.status_code == 404:
            return None
        raise
    rules = (await api.rest.repos.async_get_branch_rules(owner, name, branch)).parsed_data
    return ConfigBranchRules(
        branch=branch, head_sha=head, rule_types=frozenset(rule.type for rule in rules)
    )


async def repo_labels(client: GitHubClient, repo: str) -> frozenset[str]:
    """Every label name defined on `repo` — the repo's label *palette*, not an issue's labels.

    A module function rather than a `GitHubStateSource` method: the state source is one issue's
    per-tick snapshot, and this read has no issue and no tick. One paginated REST listing per
    invocation, uncached — labels change, and the only caller is the admin API's readiness probe,
    which wants a live answer.
    """
    owner, name = split_repo(repo)
    api = await client.api(repo)
    out: set[str] = set()
    page = 1
    while True:
        labels = (
            await api.rest.issues.async_list_labels_for_repo(
                owner, name, per_page=PER_PAGE, page=page
            )
        ).parsed_data
        out.update(label.name for label in labels)
        if len(labels) < PER_PAGE:
            return frozenset(out)
        page += 1


async def repo_issue(client: GitHubClient, repo: str, number: int) -> IssueState:
    """One issue as GitHub has it right now — title, body, state, labels, assignees.

    A module function for the same reason as `repo_labels`: this read has no tick behind it. It
    borrows a one-shot `GitHubStateSource` (defined below; the name resolves when this is called)
    rather than repeating the label and assignee normalisation that snapshot already does — a
    fresh instance per read is exactly the lifetime that class asks for.

    Read live on every call and never stored: GitHub owns the issue text, the orchestrator
    observes it, and a copy in Postgres could only ever disagree with the thing a reader is about
    to open — most likely just after someone edited the issue to unblock a stuck ticket.
    """
    return await GitHubStateSource(client, repo, number).issue()


class _Listing(Protocol):
    @property
    def content(self) -> bytes: ...


async def raw_pages(
    fetch: Callable[[int], Awaitable[_Listing]], *, key: str | None = None
) -> AsyncIterator[list[dict[str, Any]]]:
    """A listing's pages as raw JSON (the parsing rule above), up to the first short page.

    Ends on the raw page length, so entries a caller then drops still fill a page. `key` names
    the array inside an object body, as check runs wrap theirs.
    """
    page = 1
    while True:
        body = json.loads((await fetch(page)).content)
        batch: list[dict[str, Any]] = (body.get(key) or []) if key is not None else body
        yield batch
        if len(batch) < PER_PAGE:
            return
        page += 1


async def list_issues(
    client: GitHubClient,
    repo: str,
    *,
    state: Literal["open", "closed", "all"],
    listing: str,
    labels: str | None = None,
    creator: str | None = None,
    since: datetime | None = None,
    oldest_first: bool = False,
    max_pages: int | None = None,
) -> tuple[IssueState, ...]:
    """The repo's issues under `state` and `labels`, read the way admission and the parked
    listing (decision 30) both need them.

    * `GET /issues` returns pull requests too: every PR is an issue in GitHub's data model. They
      are dropped, or the entry predicate would run against the agents' own PRs and could admit
      one as a ticket.
    * An issue's labels arrive as bare strings or label objects, normalised as `_fetch_issue`
      does, so `ticket_label(...)` sees one shape whichever path observed the issue.
    * Raw JSON: this is the repo's front door, and strict whole-model validation held admission
      for EVERY ticket hostage to the single oddest open payload.

    `listing` names the caller's query in a skip or cap warning. Past `max_pages` the rest goes
    unread, so a caller must read absence from a capped listing as unknown. `creator` and
    `since` (updated at or after) filter on GitHub's side; the search API would lag a create.
    """
    issues, _ = await capped_issue_listing(
        client,
        repo,
        state=state,
        listing=listing,
        labels=labels,
        creator=creator,
        since=since,
        oldest_first=oldest_first,
        max_pages=max_pages,
    )
    return issues


async def capped_issue_listing(
    client: GitHubClient,
    repo: str,
    *,
    state: Literal["open", "closed", "all"],
    listing: str,
    labels: str | None = None,
    creator: str | None = None,
    since: datetime | None = None,
    oldest_first: bool = False,
    max_pages: int | None = None,
) -> tuple[tuple[IssueState, ...], bool]:
    """`list_issues`, and whether `max_pages` cut the listing short."""
    owner, name = split_repo(repo)
    api = await client.api(repo)
    out: list[IssueState] = []
    pages = 0
    async for batch in raw_pages(
        lambda page: api.rest.issues.async_list_for_repo(
            owner,
            name,
            state=state,
            labels=UNSET if labels is None else labels,
            creator=UNSET if creator is None else creator,
            since=UNSET if since is None else since,
            direction="asc" if oldest_first else UNSET,
            per_page=PER_PAGE,
            page=page,
        )
    ):
        out.extend(
            issue
            for raw in batch
            if (issue := _issue_from_raw(raw, repo=repo, listing=listing)) is not None
        )
        pages += 1
        if pages == max_pages and len(batch) == PER_PAGE:
            logger.warning(
                "%s listing for %s exceeds %d pages; entries past them go unread this tick",
                listing,
                repo,
                max_pages,
            )
            return tuple(out), True
    return tuple(out), False


def _issue_from_raw(raw: Mapping[str, Any], *, repo: str, listing: str) -> IssueState | None:
    """One raw `GET /issues` entry as an `IssueState`, or None when it is not a ticket: a pull
    request, or an entry with no usable number. A skip is logged, never silent."""
    # Truthiness, not `is not None`: a real PR stub is a non-empty dict, so every falsy
    # spelling of absence — missing key, null, whatever comes next — reads as "a plain issue"
    # at zero cost.
    if raw.get("pull_request"):
        return None
    try:
        number = int(cast("Any", raw.get("number")))
    except (TypeError, ValueError):
        # No external key to file it under. Skipping one alien entry beats stalling the whole
        # repo, but never silently: name what was skipped.
        logger.warning(
            "%s listing skipped an entry in %s with no usable issue number (number=%r, title=%r)",
            listing,
            repo,
            raw.get("number"),
            raw.get("title"),
        )
        return None
    labels = frozenset(
        label if isinstance(label, str) else (label.get("name") or "")
        for label in raw.get("labels") or ()
        # Neither a bare string nor a label object: an alien entry proves nothing about the
        # ticket's labels, so drop it rather than fail the listing.
        if isinstance(label, str | dict)
    )
    assignees = tuple(
        login
        for user in raw.get("assignees") or ()
        if isinstance(user, dict) and (login := user.get("login"))
    )
    return IssueState(
        number=number,
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        # Admission's endpoint filter guarantees open; the parked listing asks for all.
        state=raw.get("state") or "open",
        labels=labels - {""},
        assignees=assignees,
    )


class RepoListings:
    """The repo-wide listings, branches and every PR, shared by the sources of one tick.

    Each ticket's source otherwise paged them itself, so a pass over N tickets on one repo read
    the repo's whole PR history N times. A listing observed earlier in the same tick is the
    snapshot an earlier-scheduled ticket would have read, so sharing it changes no decision.
    Keyed by client as well as repo, as `ParkedListings` is.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[tuple[str, GitHubClient, str], AsyncOnce[Any]] = {}

    def once[T](
        self, kind: str, client: GitHubClient, repo: str, fetch: Callable[[], Awaitable[T]]
    ) -> AsyncOnce[T]:
        return self._entries.setdefault((kind, client, repo), AsyncOnce(fetch))


class GitHubStateSource(StateSource):
    """Observes one issue and its repository. `issue_number` is the ticket's `external_key`."""

    @property
    def approvals_bound_to_head(self) -> bool:
        return True

    def __init__(
        self,
        client: GitHubClient,
        repo: str,
        issue_number: int,
        *,
        shared: RepoListings | None = None,
    ) -> None:
        self._repo = repo
        self._client = client
        self._issue_number = issue_number
        self._owner, self._name = split_repo(repo)

        self._issue = AsyncOnce(self._fetch_issue)
        if shared is None:
            self._branches = AsyncOnce(self._fetch_branches)
            self._pulls = AsyncOnce(self._fetch_pull_requests)
        else:
            self._branches = shared.once("branches", client, repo, self._fetch_branches)
            self._pulls = shared.once("pulls", client, repo, self._fetch_pull_requests)
        self._files = AsyncKeyedMemo(self._fetch_files)
        self._file_texts = AsyncKeyedMemo(self._fetch_file_text)
        self._checks = AsyncKeyedMemo(self._fetch_check_runs)
        self._issue_comments = AsyncOnce(self._fetch_issue_comments)
        self._pr_comments = AsyncKeyedMemo(self._fetch_pr_comments)
        # One paginated `pulls.async_list_reviews` per PR, shared by `pr_review_verdict` and
        # `_fetch_pr_reviews` so the two purposes never cost two round trips.
        # `_pr_review_comments` is the separate inline-comments endpoint; `_pr_reviews` composes
        # both into the public `ReviewState` shape.
        self._pull_reviews = AsyncKeyedMemo(self._fetch_reviews)
        self._mergeability = AsyncKeyedMemo(self._fetch_mergeability)
        self._pr_review_comments = AsyncKeyedMemo(self._fetch_review_comments)
        self._pr_reviews = AsyncKeyedMemo(self._fetch_pr_reviews)
        self._permissions = AsyncKeyedMemo(self._fetch_permission)

    # --- StateSource ---------------------------------------------------------------------------

    @property
    def repo(self) -> str:
        return self._repo

    async def issue(self) -> IssueState:
        return await self._issue.get()

    async def branches(self) -> Sequence[BranchState]:
        return await self._branches.get()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return await self._pulls.get()

    async def files(self, ref: str) -> Sequence[str]:
        return await self._files.get(ref)

    async def file_text(self, ref: str, path: str) -> str | None:
        return await self._file_texts.get((ref, path))

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        return await self._checks.get(sha)

    async def issue_comments(self) -> Sequence[CommentState]:
        return await self._issue_comments.get()

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        return await self._pr_comments.get(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        return await self._pr_reviews.get(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        """One PR's approval/changes-requested verdict — the lazy seam (module docstring). Costs
        one review GET the first time a tick asks about this PR; every later ask, from either a
        predicate or the gate, reads the `_pull_reviews` memo."""
        verdict = _verdicts_from_reviews(await self._pull_reviews.get(number))
        return ReviewVerdict(
            approvals=_logins_with_state(verdict, "APPROVED"),
            changes_requested=_logins_with_state(verdict, "CHANGES_REQUESTED"),
            review_ids={login: review.id for login, review in verdict.items()},
            commits={
                login: review.commit_id
                for login, review in verdict.items()
                if review.commit_id is not None
            },
            bots=frozenset(login for login, review in verdict.items() if review.bot),
        )

    async def pr_mergeability(self, number: int) -> Mergeability:
        return await self._mergeability.get(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        return await self._permissions.get(login.casefold())

    # --- fetching ------------------------------------------------------------------------------

    async def _api(self) -> GitHub[TokenAuthStrategy]:
        return await self._client.api(self._repo)

    async def _fetch_issue(self) -> IssueState:
        api = await self._api()
        issue = (
            await api.rest.issues.async_get(self._owner, self._name, self._issue_number)
        ).parsed_data
        # `labels` is a union of bare strings and label objects; normalise at the boundary so
        # nothing above ever branches on it.
        labels = frozenset(
            label if isinstance(label, str) else (label.name or "") for label in issue.labels
        )
        assignees = tuple(user.login for user in (issue.assignees or ()))
        return IssueState(
            number=issue.number,
            title=issue.title,
            body=issue.body or "",
            state=issue.state,
            labels=labels - {""},
            assignees=assignees,
        )

    async def _fetch_branches(self) -> tuple[BranchState, ...]:
        api = await self._api()
        out: list[BranchState] = []
        page = 1
        while True:
            branches = (
                await api.rest.repos.async_list_branches(
                    self._owner, self._name, per_page=PER_PAGE, page=page
                )
            ).parsed_data
            out.extend(BranchState(name=b.name, sha=b.commit.sha) for b in branches)
            if len(branches) < PER_PAGE:
                return tuple(out)
            page += 1

    async def _fetch_pull_requests(self) -> tuple[PullRequestState, ...]:
        """All PRs, open and closed — `pr_merged` is a postcondition, so closed ones matter.

        The listing only: a consumer that wants a review verdict asks `pr_review_verdict(number)`
        for the PR it selected (module docstring).
        """
        api = await self._api()
        # Raw JSON, not `.parsed_data` (module docstring): this listing returns every PR the repo
        # has ever had. One closed PR carrying `stack.base.sha: null` — typed non-nullable by
        # githubkit's `PullRequestStack` — stopped every ticket in the project from reconciling.
        # Only the seven fields mapped below are read.
        simple = [
            pr
            async for batch in raw_pages(
                lambda page: api.rest.pulls.async_list(
                    self._owner, self._name, state="all", per_page=PER_PAGE, page=page
                )
            )
            for pr in batch
        ]

        return tuple(
            PullRequestState(
                number=pr["number"],
                head_ref=pr["head"]["ref"],
                base_ref=pr["base"]["ref"],
                head_sha=pr["head"]["sha"],
                state=pr["state"],
                # The listing has no `merged` flag; `merged_at` is the same fact and
                # saves a per-PR `GET /pulls/{n}`.
                merged=pr.get("merged_at") is not None,
                url=pr["html_url"],
            )
            for pr in simple
        )

    async def _fetch_mergeability(self, pull_number: int) -> Mergeability:
        """The single-PR GET: the listing carries neither `mergeable` nor `mergeable_state`.

        `mergeable` speaks of conflicts alone, so `blocked` (a missing review or check) with
        `mergeable: true` is still clean. `mergeable: null` is GitHub computing, never clean."""
        api = await self._api()
        response = await api.rest.pulls.async_get(self._owner, self._name, pull_number)
        body = json.loads(response.content)
        mergeable = body.get("mergeable")
        state = body.get("mergeable_state")
        if mergeable is False:
            return Mergeability.CONFLICTING
        if mergeable is None or state == "unknown":
            return Mergeability.UNKNOWN
        if state == "dirty":
            return Mergeability.CONFLICTING
        if state == "behind":
            return Mergeability.BEHIND
        return Mergeability.CLEAN

    async def _fetch_reviews(self, pull_number: int) -> tuple[_Review, ...]:
        """Every review on one PR, paginated once, as `_Review` records — the single source both
        `pr_review_verdict` (state only) and `_fetch_pr_reviews` (state + body + id) derive from.

        Raw JSON, not `.parsed_data` (module docstring): the listing carries every review the PR
        ever received, including deleted accounts (`user: null` is live) and whatever rollout
        fields the pinned githubkit models lag behind on. Only the seven `_Review` fields are
        read."""
        api = await self._api()
        out: list[_Review] = []
        async for batch in raw_pages(
            lambda page: api.rest.pulls.async_list_reviews(
                self._owner, self._name, pull_number, per_page=PER_PAGE, page=page
            )
        ):
            for raw in batch:
                user = raw.get("user") or {}
                commit_id = raw.get("commit_id")
                out.append(
                    _Review(
                        login=user.get("login"),
                        state=str(raw.get("state") or ""),
                        id=int(raw.get("id") or 0),
                        body=raw.get("body") or "",
                        submitted_at=raw.get("submitted_at") or "",
                        commit_id=commit_id if isinstance(commit_id, str) and commit_id else None,
                        bot=user.get("type") == "Bot",
                    )
                )
        return tuple(out)

    async def _fetch_review_comments(self, pull_number: int) -> dict[int, tuple[CommentState, ...]]:
        """Every inline (diff) comment on one PR, grouped by `pull_request_review_id`.

        One paginated `pulls.async_list_review_comments` plus client-side grouping is as faithful
        as the per-review endpoint, at one round trip instead of one per distinct reviewer.

        Raw JSON, not `.parsed_data` (module docstring): the same historical-listing class as
        `_fetch_reviews`. Only the fields mapped below are read.
        """
        api = await self._api()
        grouped: dict[int, list[CommentState]] = {}
        async for batch in raw_pages(
            lambda page: api.rest.pulls.async_list_review_comments(
                self._owner, self._name, pull_number, per_page=PER_PAGE, page=page
            )
        ):
            for raw in batch:
                review_id = raw.get("pull_request_review_id")
                if review_id is None:  # orphaned inline comment; nothing to attach it to
                    continue
                user = raw.get("user") or {}
                line = raw.get("line")
                grouped.setdefault(int(review_id), []).append(
                    CommentState(
                        author=user.get("login") or "",
                        body=raw.get("body") or "",
                        created_at=raw.get("created_at") or "",
                        path=raw.get("path"),
                        # Null once the commented line changed; the original stays put.
                        line=line if line is not None else raw.get("original_line"),
                        id=raw.get("id") if isinstance(raw.get("id"), int) else None,
                    )
                )
        return {review_id: tuple(comments) for review_id, comments in grouped.items()}

    async def _fetch_pr_reviews(self, pull_number: int) -> tuple[ReviewState, ...]:
        """Every review on one PR, in GitHub's (chronological) order, over the same raw reviews
        `pr_review_verdict` reads (`_pull_reviews`, no second fetch).

        COMMENTED reviews included: "Add single comment" on a diff is one of those, holding the
        comment, and a reviewer's feedback is text whatever verdict it came with. Inline comments
        attach by the review's own id. No reviews means nothing could attach, so the
        `_pr_review_comments` round trip is skipped.
        """
        reviews = await self._pull_reviews.get(pull_number)
        if not reviews:
            return ()
        comments_by_review = await self._pr_review_comments.get(pull_number)
        return tuple(
            ReviewState(
                author=review.login,
                body=review.body,
                created_at=review.submitted_at,
                comments=comments_by_review.get(review.id, ()),
                id=review.id or None,
                bot=review.bot,
            )
            for review in reviews
            if review.login is not None
        )

    async def _fetch_permission(self, login: str) -> RepoPermission:
        """`login`'s role, one login at a time: the collaborator listing leaves out people who
        hold access through the organisation. Needs only `metadata: read`. A 404 is a login
        GitHub does not know; every other failure propagates, so an outage never reads as "no
        access"."""
        api = await self._api()
        try:
            answer = (
                await api.rest.repos.async_get_collaborator_permission_level(
                    self._owner, self._name, login
                )
            ).parsed_data
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return "none"
            raise
        return _strongest(None if answer.user is None else answer.user.permissions)

    async def _fetch_files(self, ref: str) -> tuple[str, ...]:
        """Blob paths at a concrete ref. `recursive` is a string in the REST API, not a bool.

        A truncated tree comes back as GitHub sent it, with a warning: a file past the cut reads
        as absent to `file_exists`.
        """
        api = await self._api()
        tree = (
            await api.rest.git.async_get_tree(self._owner, self._name, ref, recursive="1")
        ).parsed_data
        if tree.truncated:
            logger.warning(
                "%s@%s: tree truncated by GitHub; later files read as absent", self._repo, ref
            )
        return tuple(item.path for item in tree.tree if item.type == "blob" and item.path)

    async def _fetch_file_text(self, key: tuple[str, str]) -> str | None:
        """Streamed, so a file past the cap is never read whole. A 404 is an absent file;
        every other failure propagates.

        githubkit puts `path` into the URL as given, so a `?` or `#` in a file's name would end
        the path and read another file: it is escaped here, once.

        The body is read inside the client's own context: outside it githubkit has closed the
        per-request client, and with a proxy configured that closes the connection the body
        streams on."""
        ref, path = key
        if walks_a_dot_segment(path):
            return None
        api = await self._api()
        async with api:
            try:
                response = await api.rest.repos.async_get_content(
                    self._owner,
                    self._name,
                    quote(path, safe="/"),
                    ref=ref,
                    headers=_RAW,
                    stream=True,
                )
            except RequestFailed as exc:
                if exc.response.status_code == 404:
                    return None
                raise
            try:
                return await capped_text(response.raw_response.aiter_bytes())
            finally:
                await response.raw_response.aclose()

    async def _fetch_check_runs(self, sha: str) -> tuple[CheckRunState, ...]:
        # Raw JSON, not `.parsed_data` (module docstring): the pinned CheckRun model's
        # `conclusion` Literal has no "stale", a value GitHub sets on superseded runs; `status` is
        # a closed Literal GitHub has extended before; and `pull_requests[].base.sha` is typed
        # non-nullable. Any of those on the gated sha would raise out of `ci_green` and block
        # every CI-gated ticket in the repo. Only name/status/conclusion/completed_at are read.
        api = await self._api()
        out: list[CheckRunState] = []
        async for runs in raw_pages(
            lambda page: api.rest.checks.async_list_for_ref(
                self._owner,
                self._name,
                sha,
                # "latest" — a re-run replaces its predecessor; `ci_green` must not be
                # satisfied or blocked by a superseded attempt.
                filter_="latest",
                per_page=PER_PAGE,
                page=page,
            ),
            key="check_runs",
        ):
            for run in runs:
                conclusion = run.get("conclusion")
                out.append(
                    CheckRunState(
                        name=str(run.get("name") or ""),
                        status=str(run.get("status") or ""),
                        # An unknown conclusion maps through as-is: `_all_green` reads any
                        # non-"success" value as not green, which blocks the gate — the safe
                        # direction.
                        conclusion=conclusion if isinstance(conclusion, str) else None,
                        completed_at=aware_time(run.get("completed_at")),
                    )
                )
        return tuple(out)

    async def _fetch_issue_comments(self) -> tuple[CommentState, ...]:
        return await self._fetch_comments(self._issue_number)

    async def _fetch_pr_comments(self, number: int) -> tuple[CommentState, ...]:
        # A pull request IS an issue in GitHub's data model, so its conversation (as opposed to
        # inline review comments on a diff) comes from the issue-comments endpoint under the PR's
        # own number. This is where an Evidence Pack comment lands.
        return await self._fetch_comments(number)

    async def _fetch_comments(self, number: int) -> tuple[CommentState, ...]:
        # Raw JSON, not `.parsed_data` (module docstring): every conversation comment ever left on
        # the issue or PR, feeding the comment predicates. Only id/author/body/created_at are read.
        api = await self._api()
        out: list[CommentState] = []
        async for batch in raw_pages(
            lambda page: api.rest.issues.async_list_comments(
                self._owner, self._name, number, per_page=PER_PAGE, page=page
            )
        ):
            out.extend(
                CommentState(
                    author=(raw.get("user") or {}).get("login") or "",
                    body=raw.get("body") or "",
                    created_at=raw.get("created_at") or "",
                    id=raw.get("id") if isinstance(raw.get("id"), int) else None,
                )
                for raw in batch
            )
        return tuple(out)


@dataclass(frozen=True, slots=True)
class _Review:
    """The seven review fields observation reads — the closed shape `_fetch_reviews` maps each raw
    listing entry into. `login` is None for a deleted account's review (`user: null` on the wire);
    `submitted_at` stays the RFC3339 text GitHub sent, matching `CommentState.created_at`.
    `commit_id` is the head the review was submitted on, None when the payload carries none.
    `bot` is `user.type == "Bot"`."""

    login: str | None
    state: str
    id: int
    body: str
    submitted_at: str
    commit_id: str | None = None
    bot: bool = False


def _verdicts_from_reviews(reviews: Iterable[_Review]) -> dict[str, _Review]:
    """The per-login latest-review mapping for a PR's reviews, in the order GitHub returned them
    (chronological) — a later entry for the same login overwrites an earlier one. Folds a single
    iterable, so how many API pages produced it is irrelevant.

    Last-write-wins is the gate's safety rule: a later CHANGES_REQUESTED or DISMISSED revokes an
    earlier APPROVED, and a re-approval revokes the reverse. Counting raw APPROVED events would let
    a withdrawn review merge a PR. COMMENTED reviews do not change a reviewer's verdict, so they
    are skipped.

    A pure function of `_Review` records so the last-write-wins rule is testable without a
    network: `test_github_state.py` builds them from literals.
    """
    verdict: dict[str, _Review] = {}
    for review in reviews:
        if review.login is None or review.state.upper() == "COMMENTED":
            continue
        verdict[review.login] = review
    return verdict


def _logins_with_state(verdict: dict[str, _Review], state: str) -> tuple[str, ...]:
    return tuple(login for login, review in verdict.items() if review.state.upper() == state)


#: `user.permissions` flag for each role, strongest first. The top-level `permission` folds
#: maintain into write and triage into read, so it cannot answer a `maintain` floor.
_PERMISSION_FLAGS: Final[tuple[tuple[RepoPermission, str], ...]] = (
    ("admin", "admin"),
    ("maintain", "maintain"),
    ("write", "push"),
    ("triage", "triage"),
    ("read", "pull"),
)


def _strongest(flags: object) -> RepoPermission:
    for level, flag in _PERMISSION_FLAGS:
        if getattr(flags, flag, None) is True:
            return level
    return "none"
