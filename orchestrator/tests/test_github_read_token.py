"""The assistant's read-only token on the GitHub side: which repos it may name, and the mint."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from orchestrator.github.client import GitHubClient, ReadToken
from orchestrator.github.observer import PER_PAGE
from orchestrator.github.read_token import NoReachableRepo, installation_repos, read_token_for


class _Listing:
    def __init__(self, body: dict[str, Any]) -> None:
        self.content = json.dumps(body).encode()


def _client_listing(
    monkeypatch: pytest.MonkeyPatch, pages: dict[int, list[str]]
) -> tuple[GitHubClient, list[int]]:
    asked: list[int] = []

    async def listing(*, per_page: int, page: int) -> _Listing:
        assert per_page == PER_PAGE
        asked.append(page)
        repos = [{"full_name": name} for name in pages.get(page, [])]
        return _Listing({"total_count": sum(map(len, pages.values())), "repositories": repos})

    api = SimpleNamespace(
        rest=SimpleNamespace(
            apps=SimpleNamespace(async_list_repos_accessible_to_installation=listing)
        )
    )
    client = GitHubClient(None)

    async def fake_api(_repo: str) -> Any:
        return api

    monkeypatch.setattr(client, "api", fake_api)
    return client, asked


async def test_installation_repos_reads_every_page_lower_cased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = [f"Acme/R{n}" for n in range(PER_PAGE)]
    client, asked = _client_listing(monkeypatch, {1: first, 2: ["acme/last"]})

    repos = await installation_repos(client, "acme/r0")

    assert asked == [1, 2]
    assert len(repos) == PER_PAGE + 1
    assert "acme/r0" in repos and "acme/last" in repos


async def test_a_repo_outside_the_installation_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client_listing(monkeypatch, {1: ["Acme/API", "acme/intake"]})
    minted: list[Sequence[str]] = []

    async def mint(repos: Sequence[str]) -> ReadToken:
        minted.append(repos)
        return ReadToken(SecretStr("ghs_x"), datetime.now(UTC), tuple(repos))

    monkeypatch.setattr(client, "mint_read_token", mint)

    token = await read_token_for(client, {"acme/api", "acme/intake", "acme/gone"})

    assert minted == [["acme/api", "acme/intake"]]
    assert token.repos == ("acme/api", "acme/intake")


async def test_no_reachable_repo_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client_listing(monkeypatch, {1: ["acme/api"]})
    with pytest.raises(NoReachableRepo):
        await read_token_for(client, {"elsewhere/repo"})


async def test_case_variants_of_one_repo_are_minted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client_listing(monkeypatch, {1: ["acme/api"]})
    minted: list[Sequence[str]] = []

    async def mint(repos: Sequence[str]) -> ReadToken:
        minted.append(repos)
        return ReadToken(SecretStr("ghs_x"), datetime.now(UTC), tuple(repos))

    monkeypatch.setattr(client, "mint_read_token", mint)

    await read_token_for(client, {"Acme/API", "acme/api"})

    assert minted == [["Acme/API"]]
