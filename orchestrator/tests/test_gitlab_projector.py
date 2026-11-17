"""GitLab projection uses GitLab's project-scoped mutation APIs."""

from __future__ import annotations

import json

import pytest

from orchestrator.github import MergeRefused
from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.projector import GitLabProjector


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def paginate(self, path: str, **params: object) -> list[object]:
        username = params["query"]
        return [{"id": 17, "username": username}]

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> object:
        self.calls.append((method, path, json))
        return {}


async def test_assignees_are_resolved_to_gitlab_member_ids() -> None:
    client = _Client()

    await GitLabProjector(client, "group/app").set_assignees(40, ["teammate"])  # type: ignore[arg-type]

    assert client.calls == [
        ("PUT", "/projects/group%2Fapp/issues/40", {"assignee_ids": [17]})
    ]



async def test_merge_rebase_rebases_then_merges() -> None:
    client = _Client()

    await GitLabProjector(client, "group/app").merge_pr(  # type: ignore[arg-type]
        12, merge_method="rebase", sha="a" * 40
    )

    assert client.calls[0] == (
        "PUT",
        "/projects/group%2Fapp/merge_requests/12/rebase",
        None,
    )
    assert client.calls[1][0] == "PUT"
    assert client.calls[1][1] == "/projects/group%2Fapp/merge_requests/12/merge"
    merge = client.calls[1][2]
    assert isinstance(merge, dict)
    assert merge["squash"] is False
    assert merge["sha"] == "a" * 40


class _Refusing(_Client):
    def __init__(self, status: int, body: str) -> None:
        super().__init__()
        self._status = status
        self._body = body

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> object:
        raise GitLabError(
            f"{method} {path} failed: {self._status} {self._body}",
            status_code=self._status,
            body=self._body,
        )


@pytest.mark.parametrize("status", [405, 406, 409, 422])
async def test_a_refused_merge_request_merge_is_a_merge_refusal(status: int) -> None:
    client = _Refusing(status, json.dumps({"message": "Branch cannot be merged"}))

    with pytest.raises(MergeRefused) as raised:
        await GitLabProjector(client, "group/app").merge_pr(7, sha="abc")  # type: ignore[arg-type]

    assert raised.value.status == status
    assert raised.value.message == "Branch cannot be merged"


async def test_another_merge_failure_still_raises_the_gitlab_error() -> None:
    client = _Refusing(500, json.dumps({"message": "boom"}))

    with pytest.raises(GitLabError):
        await GitLabProjector(client, "group/app").merge_pr(7, sha="abc")  # type: ignore[arg-type]
