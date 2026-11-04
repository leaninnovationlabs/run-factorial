"""`GitHubContractOps.delete_branch` against a stand-in for githubkit's REST surface."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit.exception import RequestFailed
from githubkit.response import Response

from orchestrator.github.contract import GitHubContractOps


def _refused(status: int, message: str) -> RequestFailed:
    request = httpx.Request("DELETE", "https://api.github.com/repos/acme/w/git/refs/heads/x")
    raw = httpx.Response(status, json={"message": message}, request=request)
    return RequestFailed(Response(raw, dict))


class _Client:
    def __init__(self, error: Exception | None) -> None:
        self.deleted: list[str] = []

        async def delete_ref(owner: str, name: str, ref: str) -> None:
            if error is not None:
                raise error
            self.deleted.append(ref)

        git = SimpleNamespace(async_delete_ref=delete_ref)
        self._api: Any = SimpleNamespace(rest=SimpleNamespace(git=git))

    async def api(self, _repo: str) -> Any:
        return self._api


@pytest.mark.parametrize(
    "error", [_refused(404, "Not Found"), _refused(422, "Reference does not exist")]
)
async def test_a_branch_already_gone_is_deleted(error: RequestFailed) -> None:
    """A restart retried after a failed tick deletes again; the branch it removed last time is
    not an error."""
    ops = GitHubContractOps(_Client(error), "acme/w")  # type: ignore[arg-type]
    await ops.delete_branch("spec/12-widget")


async def test_any_other_refusal_still_raises() -> None:
    refused = _Client(_refused(422, "Cannot delete protected branch"))
    ops = GitHubContractOps(refused, "acme/w")  # type: ignore[arg-type]
    with pytest.raises(RequestFailed):
        await ops.delete_branch("main")
