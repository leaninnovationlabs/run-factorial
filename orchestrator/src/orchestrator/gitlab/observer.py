"""Observe a GitLab project into the same snapshot types the GitHub observer fills.

Predicates and the worker stay forge-agnostic at the snapshot layer. Merge requests are
reported as pull requests; pipeline statuses as check runs; approvals as review verdicts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from orchestrator.adapters.split import SourceForge, SourceTracker, views_from_source
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
from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project
from runtime.gitlab_delivery import gitlab_delivery_author

__all__ = ["GitLabStateSource", "gitlab_views", "list_issues", "repo_issue"]

#: What `merge_status` says, where GitLab before 15.6 sends no `detailed_merge_status`. Every
#: other value, `checking`, `unchecked` and `cannot_be_merged_recheck` among them, is UNKNOWN.
_LEGACY_MERGE_STATUS: Final[dict[object, Mergeability]] = {
    "can_be_merged": Mergeability.CLEAN,
    "cannot_be_merged": Mergeability.CONFLICTING,
}


def _labels(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return frozenset(names)


def _issue_from(payload: dict[str, Any]) -> IssueState:
    assignees = tuple(
        a["username"]
        for a in payload.get("assignees") or ()
        if isinstance(a, dict) and isinstance(a.get("username"), str)
    )
    state = payload.get("state") or "opened"
    return IssueState(
        number=int(payload["iid"]),
        title=str(payload.get("title") or ""),
        body=str(payload.get("description") or ""),
        state="open" if state == "opened" else "closed",
        labels=_labels(payload.get("labels")),
        assignees=assignees,
    )


async def list_issues(
    client: GitLabClient,
    repo: str,
    *,
    state: str = "opened",
    labels: str | None = None,
) -> Sequence[IssueState]:
    # GitLab defaults `scope` to `created_by_me`. The factory token usually did not create the
    # human's issue, so omitting this makes a healthy project look as if it has no open issues.
    params: dict[str, Any] = {"state": state, "scope": "all"}
    if labels:
        params["labels"] = labels
    rows = await client.paginate(f"/projects/{quote_project(repo)}/issues", **params)
    return tuple(_issue_from(row) for row in rows if isinstance(row, dict) and "iid" in row)


async def repo_issue(client: GitLabClient, repo: str, issue_number: int) -> IssueState:
    payload = await client.get(f"/projects/{quote_project(repo)}/issues/{issue_number}")
    if not isinstance(payload, dict):
        raise GitLabError(f"issue {issue_number} in {repo} was not an object")
    return _issue_from(payload)


def gitlab_views(source: StateSource, *, bot_login: str = "") -> tuple[SourceTracker, SourceForge]:
    """Split a GitLab snapshot so a later composite can pair it with a different tracker."""
    return views_from_source(source, bot_login=bot_login, author=gitlab_delivery_author)


class GitLabStateSource(StateSource):
    @property
    def approvals_bound_to_head(self) -> bool:
        return False

    def __init__(self, client: GitLabClient, repo: str, issue_number: int) -> None:
        self._client = client
        self._repo = repo
        self._issue_number = issue_number
        self._pid = quote_project(repo)
        self._issue = AsyncOnce(self._fetch_issue)
        self._branches = AsyncOnce(self._fetch_branches)
        self._mrs = AsyncOnce(self._fetch_mrs)
        self._mr_reviews = AsyncKeyedMemo(self._fetch_mr_reviews)
        self._file_texts = AsyncKeyedMemo(self._fetch_file_text)

    @property
    def repo(self) -> str:
        return self._repo

    async def issue(self) -> IssueState:
        return await self._issue.get()

    async def branches(self) -> Sequence[BranchState]:
        return await self._branches.get()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        return await self._mrs.get()

    async def files(self, ref: str) -> Sequence[str]:
        rows = await self._client.paginate(
            f"/projects/{self._pid}/repository/tree", ref=ref, recursive=True
        )
        return tuple(
            row["path"]
            for row in rows
            if isinstance(row, dict)
            and row.get("type") == "blob"
            and isinstance(row.get("path"), str)
        )

    async def file_text(self, ref: str, path: str) -> str | None:
        return await self._file_texts.get((ref, path))

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        try:
            rows = await self._client.paginate(
                f"/projects/{self._pid}/repository/commits/{sha}/statuses"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return ()
            raise
        out: list[CheckRunState] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            gl_status = str(row.get("status") or "")
            status, conclusion = _pipeline_status(gl_status)
            out.append(
                CheckRunState(
                    name=str(row.get("name") or row.get("stage") or "pipeline"),
                    status=status,
                    conclusion=conclusion,
                )
            )
        return tuple(out)

    async def issue_comments(self) -> Sequence[CommentState]:
        rows = await self._client.paginate(
            f"/projects/{self._pid}/issues/{self._issue_number}/notes"
        )
        return tuple(_human_notes(rows))

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        rows = await self._client.paginate(f"/projects/{self._pid}/merge_requests/{number}/notes")
        return tuple(_human_notes(rows))

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        reviews = await self._mr_reviews.get(number)
        if reviews:
            return tuple(
                ReviewState(
                    author=r.login or "",
                    body=r.body,
                    created_at=r.submitted_at,
                    id=r.id or None,
                    bot=r.bot,
                )
                for r in reviews
                if r.login is not None
            )
        comments = await self.pr_comments(number)
        return tuple(
            ReviewState(
                author=c.author,
                body=c.body,
                created_at=c.created_at,
                id=c.id,
                bot=c.bot,
            )
            for c in comments
        )

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        reviews = await self._mr_reviews.get(number)
        verdict = _verdict_from_gitlab_reviews(reviews) if reviews else ReviewVerdict()
        head_sha = await self._mr_head_sha(number)
        native = await self._fetch_native_approvals(number)
        return _gitlab_gate_verdict(verdict, head_sha, native)

    async def pr_mergeability(self, number: int) -> Mergeability:
        payload = await self._client.get(f"/projects/{self._pid}/merge_requests/{number}")
        if not isinstance(payload, dict):
            return Mergeability.UNKNOWN
        detailed = payload.get("detailed_merge_status")
        if payload.get("has_conflicts") is True or detailed == "conflict":
            return Mergeability.CONFLICTING
        if detailed is None:
            # GitLab before 15.6 sends only `merge_status`, which cannot tell "behind".
            return _LEGACY_MERGE_STATUS.get(payload.get("merge_status"), Mergeability.UNKNOWN)
        if detailed == "need_rebase":
            return Mergeability.BEHIND
        if detailed in ("checking", "unchecked", "preparing"):
            return Mergeability.UNKNOWN
        return Mergeability.CLEAN

    async def _mr_head_sha(self, number: int) -> str | None:
        for pull in await self._mrs.get():
            if pull.number == number and pull.head_sha:
                return pull.head_sha
        return None

    async def _fetch_native_approvals(self, number: int) -> tuple[str, ...]:
        try:
            payload = await self._client.get(
                f"/projects/{self._pid}/merge_requests/{number}/approvals"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return ()
            raise
        if not isinstance(payload, dict):
            return ()
        return tuple(
            a["user"]["username"]
            for a in payload.get("approved_by") or ()
            if isinstance(a, dict)
            and isinstance(a.get("user"), dict)
            and isinstance(a["user"].get("username"), str)
        )

    async def repo_permission(self, login: str) -> RepoPermission:
        rows = await self._client.paginate(f"/projects/{self._pid}/members/all", query=login)
        level = max(
            (
                int(row.get("access_level") or 0)
                for row in rows
                if isinstance(row, dict)
                and str(row.get("username") or "").casefold() == login.casefold()
            ),
            default=0,
        )
        if level >= 50:
            return "admin"
        if level >= 40:
            return "maintain"
        if level >= 30:
            return "write"
        if level >= 20:
            return "triage"
        if level >= 10:
            return "read"
        return "none"

    async def _fetch_issue(self) -> IssueState:
        payload = await self._client.get(f"/projects/{self._pid}/issues/{self._issue_number}")
        if not isinstance(payload, dict):
            raise GitLabError(f"issue {self._issue_number} in {self._repo} was not an object")
        return _issue_from(payload)

    async def _fetch_file_text(self, key: tuple[str, str]) -> str | None:
        """The raw endpoint, streamed so a file past the cap is never read whole. A 404 is an
        absent file; every other failure propagates."""
        ref, path = key
        if walks_a_dot_segment(path):
            return None
        try:
            async with self._client.stream(
                f"/projects/{self._pid}/repository/files/{quote(path, safe='')}/raw", ref=ref
            ) as response:
                return await capped_text(response.aiter_bytes())
        except GitLabError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def _fetch_branches(self) -> tuple[BranchState, ...]:
        rows = await self._client.paginate(f"/projects/{self._pid}/repository/branches")
        out: list[BranchState] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
            sha = commit.get("id") if isinstance(commit, dict) else None
            if isinstance(sha, str):
                out.append(BranchState(name=row["name"], sha=sha))
        return tuple(out)

    async def _fetch_mr_reviews(self, number: int) -> tuple[_GitLabReview, ...]:
        try:
            rows = await self._client.paginate(
                f"/projects/{self._pid}/merge_requests/{number}/reviews"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return ()
            raise
        out: list[_GitLabReview] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            user = row.get("author") if isinstance(row.get("author"), dict) else row.get("user")
            if not isinstance(user, dict):
                user = {}
            login = user.get("username")
            login_str = login if isinstance(login, str) else None
            event = row.get("event") or row.get("event_type") or row.get("state") or ""
            state = _gitlab_review_state(str(event))
            review_id = row.get("id")
            commit = row.get("commit_id") or row.get("commit")
            commit_id = commit if isinstance(commit, str) and commit else None
            body = row.get("body") or row.get("note") or ""
            submitted = (
                row.get("reviewed_at") or row.get("updated_at") or row.get("created_at") or ""
            )
            out.append(
                _GitLabReview(
                    login=login_str,
                    state=state,
                    id=int(review_id) if isinstance(review_id, int) else 0,
                    body=str(body),
                    submitted_at=str(submitted),
                    commit_id=commit_id,
                    bot=str(user.get("user_type") or "").casefold() == "project_bot"
                    or str(user.get("type") or "").casefold() == "bot",
                )
            )
        return tuple(out)

    async def _fetch_mrs(self) -> tuple[PullRequestState, ...]:
        rows = await self._client.paginate(f"/projects/{self._pid}/merge_requests", state="all")
        out: list[PullRequestState] = []
        for row in rows:
            if not isinstance(row, dict) or "iid" not in row:
                continue
            gl_state = row.get("state") or "opened"
            merged = gl_state == "merged" or bool(row.get("merged_at"))
            # `locked` is a merge in progress: read as closed, a gate would take it for a close.
            state = "open" if gl_state in ("opened", "locked") else "closed"
            sha = row.get("sha") or ""
            out.append(
                PullRequestState(
                    number=int(row["iid"]),
                    head_ref=str(row.get("source_branch") or ""),
                    base_ref=str(row.get("target_branch") or ""),
                    head_sha=str(sha),
                    state=state,
                    merged=merged,
                    url=str(row.get("web_url") or ""),
                )
            )
        return tuple(out)


@dataclass(frozen=True, slots=True)
class _GitLabReview:
    login: str | None
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED (GitHub-shaped for the gate)
    id: int
    body: str
    submitted_at: str
    commit_id: str | None = None
    bot: bool = False


def _gitlab_review_state(event: str) -> str:
    """Map GitLab MR review events to the gate's GitHub-shaped review states."""
    key = event.casefold().replace("-", "_")
    if key in {"approved", "approve"}:
        return "APPROVED"
    if key in {
        "requested_changes",
        "request_changes",
        "request_change",
        "changes_requested",
    }:
        return "CHANGES_REQUESTED"
    return "COMMENTED"


def _gitlab_user_bot(user: dict[str, Any]) -> bool:
    return (
        str(user.get("user_type") or "").casefold() == "project_bot"
        or str(user.get("type") or "").casefold() == "bot"
    )


def _verdict_from_gitlab_reviews(reviews: tuple[_GitLabReview, ...]) -> ReviewVerdict:
    """Per-login latest review wins, same rule as `github.observer._verdicts_from_reviews`."""
    latest: dict[str, _GitLabReview] = {}
    for review in reviews:
        if review.login is None or review.state == "COMMENTED":
            continue
        latest[review.login] = review
    return ReviewVerdict(
        approvals=tuple(login for login, review in latest.items() if review.state == "APPROVED"),
        changes_requested=tuple(
            login for login, review in latest.items() if review.state == "CHANGES_REQUESTED"
        ),
        review_ids={login: review.id for login, review in latest.items() if review.id},
        commits={
            login: review.commit_id
            for login, review in latest.items()
            if review.commit_id is not None
        },
        bots=frozenset(login for login, review in latest.items() if review.bot),
    )


def _gitlab_gate_verdict(
    verdict: ReviewVerdict,
    head_sha: str | None,
    native_approvals: tuple[str, ...],
) -> ReviewVerdict:
    """Fold GitLab's MR Approve button and missing review SHAs into a gate-ready verdict.

    The gate only counts `approvals_at(head_sha)`. GitHub reviews carry `commit_id`; GitLab's
    `/reviews` often does not, and `/approvals` never does — without head binding every approval
    is invisible to `_approvals`. Native approvals also override an older `requested_changes`
    review left on `/reviews` when the reviewer used Approve instead of re-submitting a review.
    """
    if not head_sha and not native_approvals:
        return verdict

    approvals = list(verdict.approvals)
    changes = list(verdict.changes_requested)
    commits = dict(verdict.commits)

    if head_sha:
        for login in verdict.approvals:
            if commits.get(login) is None:
                commits[login] = head_sha

    for login in native_approvals:
        if login in changes:
            changes = [name for name in changes if name != login]
        if login not in approvals:
            approvals.append(login)
        if head_sha:
            commits[login] = head_sha

    return ReviewVerdict(
        approvals=tuple(dict.fromkeys(approvals)),
        changes_requested=tuple(dict.fromkeys(changes)),
        review_ids=dict(verdict.review_ids),
        commits=commits,
        bots=verdict.bots,
    )


def _human_notes(rows: Sequence[object]) -> tuple[CommentState, ...]:
    return tuple(
        _note(row) for row in rows if isinstance(row, dict) and row.get("system") is not True
    )


def _note(row: dict[str, Any]) -> CommentState:
    author = ""
    user = row.get("author")
    if isinstance(user, dict) and isinstance(user.get("username"), str):
        author = user["username"]
    bot = isinstance(user, dict) and _gitlab_user_bot(user)
    note_id = row.get("id")
    return CommentState(
        author=author,
        body=str(row.get("body") or ""),
        created_at=str(row.get("created_at") or ""),
        id=int(note_id) if isinstance(note_id, int) else None,
        bot=bot,
    )


def _pipeline_status(status: str) -> tuple[str, str | None]:
    if status in {"pending", "created", "waiting_for_resource", "preparing", "scheduled"}:
        return "queued", None
    if status in {"running", "manual"}:
        return "in_progress", None
    if status == "success":
        return "completed", "success"
    if status in {"failed", "canceled", "cancelled", "skipped"}:
        return "completed", "failure"
    return "completed", status or None
