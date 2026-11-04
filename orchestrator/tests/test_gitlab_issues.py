"""GitLab issue writer: the GitHub App writer's twin for a GitLab ticket source."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.gitlab.issues import GitLabIssueWriter


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def paginate(self, url: str, **params: object) -> list[object]:
        self.calls.append(("GET", url, params))
        if url.endswith("/members/all"):
            return [{"id": 9, "username": "operator", "access_level": 30}]
        if url.endswith("/labels"):
            return [{"name": "factorial:new"}]
        return []

    async def get(self, url: str, **params: object) -> object:
        self.calls.append(("GET", url, params))
        if url.endswith("/issues") and params.get("author_username") == "bot":
            return [
                {
                    "iid": 4,
                    "title": "Do the thing",
                    "description": "body\n<!-- factorial-request:abc -->",
                    "state": "opened",
                    "labels": ["factorial:new"],
                    "assignees": [{"username": "operator"}],
                }
            ]
        if url.endswith("/issues/7"):
            return {
                "iid": 7,
                "title": "Do the thing",
                "description": "body",
                "state": "opened",
                "labels": ["factorial:new"],
                "assignees": [{"username": "operator"}],
            }
        return []

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> object:
        self.calls.append((method, path, json))
        return {"iid": 7}


async def test_create_opens_the_issue_with_labels_and_assignee() -> None:
    client = _Client()
    writer = GitLabIssueWriter(client)  # type: ignore[arg-type]

    assert await writer.assignable("group/app", "operator") is True
    assert await writer.labels("group/app") == frozenset({"factorial:new"})
    issue = await writer.create(
        "group/app",
        title="Do the thing",
        body="body",
        labels=["factorial:new"],
        assignees=["operator"],
    )

    assert issue.number == 7
    create = next(call for call in client.calls if call[0] == "POST")
    assert create[1] == "/projects/group%2Fapp/issues"
    assert create[2] == {
        "title": "Do the thing",
        "description": "body",
        "labels": "factorial:new",
        "assignee_ids": [9],
    }


async def test_find_marked_returns_the_earliest_matching_issue() -> None:
    client = _Client()
    writer = GitLabIssueWriter(client)  # type: ignore[arg-type]

    found = await writer.find_marked(
        "group/app",
        "factorial-request:abc",
        creator="bot",
        since=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert found is not None
    assert found.number == 4
