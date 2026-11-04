"""A project's GitHub connection: the secret store is faked, so no database is needed."""

from __future__ import annotations

from collections.abc import Collection

import pytest
from pydantic import SecretStr

from orchestrator.db.models import Project
from orchestrator.github import connection as mod
from orchestrator.github.connection import (
    PRIVATE_KEY_SECRET,
    TOKEN_SECRET,
    WEBHOOK_SECRET,
    GitHubConnection,
    connection_for_project,
)


def _project(github: dict[str, object]) -> Project:
    return Project(id=1, settings={"github": github})


def _secrets(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> list[frozenset[str]]:
    """The store faked; each read's names are recorded."""
    reads: list[frozenset[str]] = []

    async def get_many(
        _session: object, _project_id: int, names: Collection[str]
    ) -> dict[str, SecretStr]:
        reads.append(frozenset(names))
        return {name: SecretStr(values[name]) for name in names if name in values}

    monkeypatch.setattr(mod.store, "get_many", get_many)
    return reads


async def test_an_app_connection_reads_settings_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secrets(monkeypatch, {PRIVATE_KEY_SECRET: "pem", WEBHOOK_SECRET: "whsec"})
    project = _project(
        {
            "mode": "app",
            "app_id": "42",
            "bot_login": "acme[bot]",
            "installations": {"acme": 7, "bad": "x"},
        }
    )
    conn = await connection_for_project(None, project, with_webhook_secret=True)  # type: ignore[arg-type]
    assert conn is not None
    assert conn.mode == "app"
    assert conn.app_id == "42"
    assert conn.private_key is not None and conn.private_key.get_secret_value() == "pem"
    assert conn.webhook_secret is not None and conn.webhook_secret.get_secret_value() == "whsec"
    assert conn.token is None
    assert conn.bot_login == "acme[bot]"
    assert dict(conn.installations) == {"acme": 7}


async def test_a_token_connection_needs_only_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _secrets(monkeypatch, {TOKEN_SECRET: "ghp_x"})
    conn = await connection_for_project(None, _project({"mode": "token"}))  # type: ignore[arg-type]
    assert conn is not None
    assert conn.mode == "token"
    assert conn.token is not None and conn.token.get_secret_value() == "ghp_x"
    assert conn.private_key is None


@pytest.mark.parametrize("mode", ["app", "token"])
async def test_a_resolution_reads_its_secrets_at_once_and_the_webhook_secret_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Every tick resolves every project's connection, and only a webhook route verifies a
    signature: the webhook secret is not read, nor decrypted, for the rest."""
    reads = _secrets(
        monkeypatch, {PRIVATE_KEY_SECRET: "pem", TOKEN_SECRET: "ghp_x", WEBHOOK_SECRET: "whsec"}
    )
    project = _project({"mode": mode, "app_id": "42"})
    credential = PRIVATE_KEY_SECRET if mode == "app" else TOKEN_SECRET

    plain = await connection_for_project(None, project)  # type: ignore[arg-type]
    signed = await connection_for_project(None, project, with_webhook_secret=True)  # type: ignore[arg-type]

    assert plain is not None and plain.webhook_secret is None
    assert signed is not None and signed.webhook_secret is not None
    assert reads == [frozenset({credential}), frozenset({credential, WEBHOOK_SECRET})]


@pytest.mark.parametrize(
    ("github", "secrets"),
    [
        ({"mode": "app", "app_id": "42"}, {}),
        ({"mode": "app"}, {PRIVATE_KEY_SECRET: "pem"}),
        ({"mode": "token"}, {}),
        ({}, {TOKEN_SECRET: "ghp_x", PRIVATE_KEY_SECRET: "pem"}),
    ],
)
async def test_a_project_without_the_mode_s_fields_is_not_connected(
    monkeypatch: pytest.MonkeyPatch, github: dict[str, object], secrets: dict[str, str]
) -> None:
    _secrets(monkeypatch, secrets)
    assert await connection_for_project(None, _project(github)) is None  # type: ignore[arg-type]


def test_the_fingerprint_changes_with_the_key() -> None:
    def make(key: str, installs: dict[str, int] | None = None) -> GitHubConnection:
        return GitHubConnection(
            mode="app",
            app_id="1",
            private_key=SecretStr(key),
            token=None,
            webhook_secret=None,
            bot_login="b",
            installations=installs or {"o": 1},
        )

    assert make("a").fingerprint == make("a").fingerprint
    assert make("a").fingerprint != make("b").fingerprint
    assert make("a").fingerprint != make("a", {"o": 2}).fingerprint
