"""The App issue writer's recovery search (`github/issues.py`), against a fake issues API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from orchestrator.github.issues import GitHubIssueWriter, MarkerUnconfirmed

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class _Response:
    content: bytes


class _FullPages:
    """Every page is full of unmarked issues, as a busy App's listing past the cap would be."""

    async def async_list_for_repo(self, owner: str, repo: str, **kw: Any) -> _Response:
        start = (kw["page"] - 1) * kw["per_page"]
        body = [
            {"number": start + n, "title": "t", "body": "x", "state": "open", "labels": []}
            for n in range(1, kw["per_page"] + 1)
        ]
        return _Response(content=json.dumps(body).encode())


class _Client:
    async def api(self, _repo: str) -> Any:
        rest = type("_Rest", (), {"issues": _FullPages()})()
        return type("_Api", (), {"rest": rest})()


async def test_a_capped_search_with_no_marked_issue_is_unknown_not_absent() -> None:
    """None tells the caller to open the issue. Past the cap the original may sit on a page
    nobody read, and opening another would be a second ticket for one request."""
    writer = GitHubIssueWriter(client=_Client())  # type: ignore[arg-type]

    with pytest.raises(MarkerUnconfirmed):
        await writer.find_marked(
            "acme/app", "factorial-request:1:00", creator="bot", since=datetime.now(UTC)
        )
