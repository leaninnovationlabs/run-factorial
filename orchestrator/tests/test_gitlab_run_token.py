"""A GitLab agent run receives a Developer token for one project, never the stored token."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.run_token import (
    mint_project_run_token,
    revoke_project_run_tokens,
    sweep_project_run_tokens,
)


class _Client:
    def __init__(self, rows: list[object] | None = None, *, fail: int | None = None) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, str, object]] = []

    async def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> dict[str, object]:
        self.calls.append((method, path, json))
        if self.fail is not None and method == "POST":
            raise GitLabError("nope", status_code=self.fail)
        if method == "DELETE":
            return {}
        return {"id": 9, "token": "project-secret"}

    async def paginate(self, path: str, **params: object) -> list[object]:
        self.calls.append(("GET", path, params))
        return self.rows


async def test_mint_is_a_developer_token_for_this_run() -> None:
    client = _Client(
        [
            {"id": 3, "name": "factorial-run-7-1"},
            {"id": 4, "name": "factorial-run-8-1"},
            {"id": 9, "name": "factorial-run-7-2"},
        ]
    )
    now = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)

    token = await mint_project_run_token(client, "group/app", 7, now=now)  # type: ignore[arg-type]

    assert token == "project-secret"
    created = client.calls[0]
    assert created[0] == "POST"
    assert created[1] == "/projects/group%2Fapp/access_tokens"
    assert created[2] == {
        "name": f"factorial-run-7-{int(now.timestamp())}",
        "scopes": ["api"],
        "access_level": 30,
        "expires_at": "2026-10-13",
    }
    deleted = [path for method, path, _ in client.calls if method == "DELETE"]
    assert deleted == ["/projects/group%2Fapp/access_tokens/3"]


async def test_a_token_that_cannot_mint_is_not_handed_to_the_agent() -> None:
    client = _Client(fail=403)

    with pytest.raises(RuntimeError, match="Maintainer"):
        await mint_project_run_token(
            client,  # type: ignore[arg-type]
            "group/app",
            7,
            now=datetime(2026, 10, 12, tzinfo=UTC),
        )


async def test_revoke_deletes_only_this_run() -> None:
    client = _Client(
        [
            {"id": 3, "name": "factorial-run-7-1"},
            {"id": 4, "name": "factorial-run-8-1"},
            "not-a-row",
        ]
    )

    await revoke_project_run_tokens(client, "group/app", 7)  # type: ignore[arg-type]

    deleted = [path for method, path, _ in client.calls if method == "DELETE"]
    assert deleted == ["/projects/group%2Fapp/access_tokens/3"]


async def test_startup_sweep_preserves_only_live_run_tokens() -> None:
    client = _Client(
        [
            {"id": 3, "name": "factorial-run-7-1"},
            {"id": 4, "name": "factorial-run-8-1"},
            {"id": 5, "name": "operator-token"},
        ]
    )

    deleted = await sweep_project_run_tokens(
        client,  # type: ignore[arg-type]
        "group/app",
        active_run_ids=frozenset({8}),
    )

    assert deleted == 1
    paths = [path for method, path, _ in client.calls if method == "DELETE"]
    assert paths == ["/projects/group%2Fapp/access_tokens/3"]
