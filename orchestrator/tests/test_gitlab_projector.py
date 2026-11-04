"""GitLab projection uses GitLab's project-scoped mutation APIs."""

from __future__ import annotations

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
