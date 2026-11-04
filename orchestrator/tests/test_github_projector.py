"""`GitHubProjector.merge_pr`: which of GitHub's answers is a refusal the gate waits on, and which
is an outage the tick fails on."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit.exception import RequestFailed
from githubkit.response import Response

from orchestrator.github import GitHubProjector, MergeRefused


def _failed(status: int, body: Any) -> RequestFailed:
    request = httpx.Request("PUT", "https://api.github.com/repos/acme/widgets/pulls/7/merge")
    raw = httpx.Response(status, json=body, request=request)
    return RequestFailed(Response(raw, dict))


class _Client:
    def __init__(self, answer: Exception | str) -> None:
        self.answer = answer
        self.merges: list[dict[str, Any]] = []

    async def api(self, _repo: str) -> SimpleNamespace:
        return SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(async_merge=self._merge)))

    async def _merge(self, owner: str, repo: str, number: int, **kwargs: Any) -> SimpleNamespace:
        self.merges.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(parsed_data=SimpleNamespace(sha=self.answer))


def _projector(client: _Client) -> GitHubProjector:
    return GitHubProjector(client, "acme/widgets")  # pyright: ignore[reportArgumentType]


async def test_a_merge_is_conditioned_on_the_sha_it_is_given() -> None:
    client = _Client("merge-sha")

    assert await _projector(client).merge_pr(7, sha="approved-sha") == "merge-sha"
    assert client.merges[0]["sha"] == "approved-sha"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (405, "At least 1 approving review is required by reviewers with write access."),
        (409, "Head branch was modified. Review and try the merge again."),
    ],
)
async def test_github_declining_the_merge_is_a_refusal_in_its_own_words(
    status: int, message: str
) -> None:
    client = _Client(_failed(status, {"message": message}))

    with pytest.raises(MergeRefused) as refused:
        await _projector(client).merge_pr(7, sha="approved-sha")

    assert refused.value.status == status
    assert refused.value.message == message


async def test_a_refusal_without_a_message_still_says_something() -> None:
    client = _Client(_failed(405, {}))

    with pytest.raises(MergeRefused) as refused:
        await _projector(client).merge_pr(7, sha="approved-sha")

    assert refused.value.message == "HTTP 405"


async def test_a_long_refusal_is_cut_before_it_reaches_the_issue() -> None:
    client = _Client(_failed(405, {"message": "x" * 5_000}))

    with pytest.raises(MergeRefused) as refused:
        await _projector(client).merge_pr(7, sha="approved-sha")

    assert len(refused.value.message) == 300


@pytest.mark.parametrize("status", [403, 422, 500])
async def test_any_other_failure_still_fails_the_tick(status: int) -> None:
    client = _Client(_failed(status, {"message": "nope"}))

    with pytest.raises(RequestFailed):
        await _projector(client).merge_pr(7, sha="approved-sha")
