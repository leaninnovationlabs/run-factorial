"""GitLab observations used by admission and reconciliation."""

from __future__ import annotations

import pytest

from orchestrator.github.state import ReviewVerdict
from orchestrator.gitlab.observer import GitLabStateSource, list_issues


class _Api:
    def __init__(self) -> None:
        self.url = ""
        self.params: dict[str, object] = {}

    async def paginate(self, url: str, **params: object) -> list[object]:
        self.url = url
        self.params = params
        return [
            {
                "iid": 40,
                "title": "Build the demo",
                "state": "opened",
                "labels": ["factorial:new"],
                "assignees": [{"username": "teammate"}],
            }
        ]


@pytest.mark.asyncio
async def test_admission_lists_every_project_issue_not_only_the_token_owners() -> None:
    api = _Api()

    issues = await list_issues(api, "group/project")  # type: ignore[arg-type]

    assert api.url == "/projects/group%2Fproject/issues"
    assert api.params == {"state": "opened", "scope": "all"}
    assert issues[0].number == 40
    assert issues[0].labels == frozenset({"factorial:new"})
    assert issues[0].assignees == ("teammate",)


class _Members:
    async def paginate(self, url: str, **params: object) -> list[object]:
        assert url == "/projects/group%2Fproject/members/all"
        assert params == {"query": "teammate"}
        return [{"username": "teammate", "access_level": 30}]


@pytest.mark.asyncio
async def test_gitlab_state_source_exposes_repo_and_maps_project_membership() -> None:
    source = GitLabStateSource(_Members(), "group/project", 40)  # type: ignore[arg-type]

    assert source.repo == "group/project"
    assert await source.repo_permission("teammate") == "write"


class _ReviewsClient:
    def __init__(
        self,
        rows: list[object],
        *,
        head_sha: str = "abc123",
        approved_by: list[object] | None = None,
    ) -> None:
        self._rows = rows
        self._head_sha = head_sha
        self._approved_by = approved_by if approved_by is not None else []

    async def paginate(self, url: str, **params: object) -> list[object]:
        if url.endswith("/merge_requests"):
            return [
                {
                    "iid": 3,
                    "state": "opened",
                    "source_branch": "spec/42-x",
                    "target_branch": "main",
                    "sha": self._head_sha,
                    "web_url": "https://gitlab.example/mr/3",
                }
            ]
        assert url.endswith("/merge_requests/3/reviews")
        return self._rows

    async def get(self, url: str, **params: object) -> object:
        assert url.endswith("/merge_requests/3/approvals")
        return {"approved_by": self._approved_by}


@pytest.mark.asyncio
async def test_gitlab_pr_review_verdict_maps_requested_changes() -> None:
    client = _ReviewsClient(
        [
            {
                "id": 9,
                "author": {"username": "teammate"},
                "event": "requested_changes",
                "commit_id": "abc123",
                "created_at": "2026-10-13T12:00:00Z",
                "body": "Please fix the fixture list.",
            }
        ]
    )
    source = GitLabStateSource(client, "group/project", 42)  # type: ignore[arg-type]

    verdict = await source.pr_review_verdict(3)

    assert verdict == ReviewVerdict(
        changes_requested=("teammate",),
        review_ids={"teammate": 9},
        commits={"teammate": "abc123"},
    )


@pytest.mark.asyncio
async def test_gitlab_pr_review_verdict_latest_review_wins() -> None:
    client = _ReviewsClient(
        [
            {
                "id": 1,
                "author": {"username": "teammate"},
                "event": "requested_changes",
                "commit_id": "abc123",
                "created_at": "2026-10-13T12:00:00Z",
            },
            {
                "id": 2,
                "author": {"username": "teammate"},
                "event": "approved",
                "commit_id": "abc123",
                "created_at": "2026-10-13T13:00:00Z",
            },
        ]
    )
    source = GitLabStateSource(client, "group/project", 42)  # type: ignore[arg-type]

    verdict = await source.pr_review_verdict(3)

    assert verdict.approvals == ("teammate",)
    assert verdict.changes_requested == ()
    assert verdict.approvals_at("abc123") == ("teammate",)


class _ApprovalsOnlyClient:
    def __init__(self, *, iid: int, head_sha: str, approved_by: list[object]) -> None:
        self._iid = iid
        self._head_sha = head_sha
        self._approved_by = approved_by

    async def paginate(self, url: str, **params: object) -> list[object]:
        if url.endswith("/merge_requests"):
            return [
                {
                    "iid": self._iid,
                    "state": "opened",
                    "source_branch": "spec/42-x",
                    "target_branch": "main",
                    "sha": self._head_sha,
                    "web_url": "https://gitlab.example/mr/8",
                }
            ]
        if url.endswith(f"/merge_requests/{self._iid}/reviews"):
            return []
        raise AssertionError(f"unexpected paginate {url}")

    async def get(self, url: str, **params: object) -> object:
        assert url.endswith(f"/merge_requests/{self._iid}/approvals")
        return {"approved_by": self._approved_by}


@pytest.mark.asyncio
async def test_gitlab_native_approval_binds_to_mr_head_for_the_gate() -> None:
    """GitLab's Approve button fills `/approvals` only — without head binding the
    gate never sees it."""
    client = _ApprovalsOnlyClient(
        iid=8,
        head_sha="deadbeef",
        approved_by=[{"user": {"username": "teammate"}}],
    )
    source = GitLabStateSource(client, "group/project", 42)  # type: ignore[arg-type]

    verdict = await source.pr_review_verdict(8)

    assert verdict.approvals == ("teammate",)
    assert verdict.approvals_at("deadbeef") == ("teammate",)
    assert verdict.approvals_at("other") == ()


@pytest.mark.asyncio
async def test_gitlab_native_approval_overrides_stale_requested_changes_review() -> None:
    client = _ReviewsClient(
        [
            {
                "id": 1,
                "author": {"username": "teammate"},
                "event": "requested_changes",
                "commit_id": "abc123",
                "created_at": "2026-10-13T12:00:00Z",
            }
        ],
        head_sha="abc123",
        approved_by=[{"user": {"username": "teammate"}}],
    )
    source = GitLabStateSource(client, "group/project", 42)  # type: ignore[arg-type]

    verdict = await source.pr_review_verdict(3)

    assert verdict.changes_requested == ()
    assert verdict.approvals_at("abc123") == ("teammate",)


class _Notes:
    async def paginate(self, url: str, **params: object) -> list[object]:
        return [
            {
                "id": 11,
                "author": {"username": "operator"},
                "body": "please look",
                "created_at": "1",
                "system": False,
            },
            {
                "author": {"username": "ghost"},
                "body": "changed the description",
                "created_at": "2",
                "system": True,
            },
        ]


@pytest.mark.asyncio
async def test_issue_comments_skip_system_notes() -> None:
    source = GitLabStateSource(_Notes(), "group/app", 40)  # type: ignore[arg-type]
    comments = await source.issue_comments()
    assert [comment.body for comment in comments] == ["please look"]
    assert comments[0].id == 11


@pytest.mark.asyncio
async def test_gitlab_verdict_ignores_mr_notes_without_a_formal_review() -> None:
    """Same rule as GitHub: `pr_review_verdict` reads `/reviews` only, not MR thread notes."""
    source = GitLabStateSource(_ReviewsClient([]), "group/project", 42)  # type: ignore[arg-type]

    verdict = await source.pr_review_verdict(3)

    assert verdict.changes_requested == ()


class _MergeRequests:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def paginate(self, url: str, **params: object) -> list[object]:
        assert url.endswith("/merge_requests")
        return list(self._rows)


@pytest.mark.asyncio
async def test_a_merge_request_gitlab_is_merging_reads_as_open() -> None:
    """GitLab reports `locked` while a merge is in progress. Read as closed, a gate polled in that
    window would take the merge for a close (decision 79)."""
    api = _MergeRequests(
        [
            {
                "iid": 3,
                "state": "locked",
                "source_branch": "spec/42-widget",
                "target_branch": "main",
                "sha": "abc123",
                "web_url": "https://gitlab.test/mr/3",
            }
        ]
    )
    source = GitLabStateSource(api, "group/project", 42)  # type: ignore[arg-type]

    [merge_request] = await source.pull_requests()

    assert (merge_request.state, merge_request.merged) == ("open", False)
