"""GitLab contract mutations for `kind: action`."""

from __future__ import annotations

from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.contract import GitLabContractOps


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.tags: dict[str, str] = {}

    async def get(self, url: str, **params: object) -> object:
        self.calls.append(("GET", url, params))
        if url.endswith("/repository/tags/freeze"):
            return {"commit": {"id": "abc"}}
        raise GitLabError("missing", status_code=404)

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> object:
        self.calls.append((method, path, json))
        body = json if isinstance(json, dict) else {}
        if method == "POST" and path.endswith("/repository/tags"):
            name = str(body.get("tag_name"))
            if name in self.tags:
                raise GitLabError("exists", status_code=409)
            self.tags[name] = str(body.get("ref"))
            return {}
        if method == "DELETE" and "/repository/tags/" in path:
            self.tags.pop(path.rsplit("/", 1)[-1], None)
            return None
        return {}


async def test_tag_sha_create_or_move_close_and_label() -> None:
    client = _Client()
    ops = GitLabContractOps(client, "group/app")  # type: ignore[arg-type]

    assert await ops.tag_sha("freeze") == "abc"
    assert await ops.tag_sha("missing") is None

    await ops.create_or_move_tag("v1", "sha1")
    client.tags["v1"] = "sha1"
    await ops.create_or_move_tag("v1", "sha2")
    await ops.close_pull_request(3, comment="done")
    await ops.delete_branch("tests/slug")
    await ops.remove_label(3, "factorial:fix")

    methods = [call[0] for call in client.calls]
    assert "POST" in methods and "PUT" in methods and "DELETE" in methods
    note = next(call for call in client.calls if str(call[1]).endswith("/notes"))
    assert note[2] == {"body": "done"}
