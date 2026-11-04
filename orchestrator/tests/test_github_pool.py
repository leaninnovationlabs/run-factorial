"""The per-project client pool: one client per set of credentials, rebuilt when they change."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from orchestrator.db.models import Project
from orchestrator.github import pool as mod
from orchestrator.github.connection import GitHubConnection
from orchestrator.github.pool import GitHubPool


def _connection(key: str) -> GitHubConnection:
    return GitHubConnection(
        mode="app",
        app_id="1",
        private_key=SecretStr(key),
        token=None,
        webhook_secret=None,
        bot_login="acme[bot]",
        installations={"acme": 7},
    )


def _connected(monkeypatch: pytest.MonkeyPatch, connections: dict[int, Any]) -> None:
    async def connection_for_project(_session: object, project: Project) -> Any:
        return connections[project.id]

    monkeypatch.setattr(mod, "connection_for_project", connection_for_project)


async def test_the_same_credentials_reuse_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _connected(monkeypatch, {1: _connection("pem"), 2: _connection("pem")})
    pool = GitHubPool()

    first = await pool.for_project(None, Project(id=1))  # type: ignore[arg-type]
    again = await pool.for_project(None, Project(id=1))  # type: ignore[arg-type]
    sibling = await pool.for_project(None, Project(id=2))  # type: ignore[arg-type]

    assert first is not None
    assert first is again is sibling


async def test_a_rotated_key_gets_a_new_client(monkeypatch: pytest.MonkeyPatch) -> None:
    connections: dict[int, Any] = {1: _connection("old")}
    _connected(monkeypatch, connections)
    pool = GitHubPool()

    before = await pool.for_project(None, Project(id=1))  # type: ignore[arg-type]
    connections[1] = _connection("new")
    after = await pool.for_project(None, Project(id=1))  # type: ignore[arg-type]

    assert before is not None and after is not None
    assert after is not before
    assert after.connection == connections[1]


async def test_an_unconnected_project_has_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _connected(monkeypatch, {1: None})
    assert await GitHubPool().for_project(None, Project(id=1)) is None  # type: ignore[arg-type]


def test_the_pool_is_one_per_process() -> None:
    assert mod.pool() is mod.pool()
