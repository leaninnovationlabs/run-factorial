"""GitLab repository reads used by config and preview pages."""

from __future__ import annotations

import base64

from orchestrator.gitlab.contents import GitLabContents


class _Client:
    def __init__(self) -> None:
        self.gets: list[tuple[str, dict[str, object]]] = []

    async def get(self, path: str, **params: object) -> object:
        self.gets.append((path, params))
        return {
            "encoding": "base64",
            "content": base64.b64encode(b"<h1>Preview</h1>").decode(),
        }


async def test_read_blob_decodes_the_git_object() -> None:
    client = _Client()

    body = await GitLabContents(client).read_blob(  # type: ignore[arg-type]
        "group/app", "a" * 40
    )

    assert body == b"<h1>Preview</h1>"
    assert client.gets == [
        (f"/projects/group%2Fapp/repository/blobs/{'a' * 40}", {})
    ]


class _CompareClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, object]] = []

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> object:
        self.calls.append((method, path, params))
        return self.payload


async def test_compare_maps_commits_and_diffs() -> None:
    payload = {
        "commits": [
            {
                "id": "b" * 40,
                "message": "Seed factorial\n",
                "author_name": "bot",
                "authored_date": "2026-10-15T00:00:00Z",
            }
        ],
        "diffs": [
            {
                "new_path": ".factorial/README.md",
                "old_path": ".factorial/README.md",
                "new_file": True,
                "diff": "@@\n+hello\n",
            }
        ],
    }
    client = _CompareClient(payload)

    comparison = await GitLabContents(client).compare(  # type: ignore[arg-type]
        "group/app", "a" * 40, "b" * 40
    )

    assert comparison.status == "ahead"
    assert comparison.total_commits == 1
    assert comparison.commits[0].sha == "b" * 40
    assert comparison.files[0].status == "added"
    assert comparison.files[0].additions == 1
    assert client.calls == [
        (
            "GET",
            "/projects/group%2Fapp/repository/compare",
            {"from": "a" * 40, "to": "b" * 40, "straight": "true"},
        )
    ]
