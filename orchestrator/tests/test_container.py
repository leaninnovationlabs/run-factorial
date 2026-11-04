"""The local image's startup check (tech-stack decision 52).

Each test starts from a configuration that passes and breaks one thing, so a problem that
appears belongs to that one thing.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator import container
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import OrganizationCredential, RuntimeCredential, Secret
from orchestrator.secrets import store
from orchestrator.secrets.crypto import load_keyring
from orchestrator.settings import (
    E2bSettings,
    GitLabSettings,
    RuntimeSettings,
    Settings,
    get_settings,
)


def _settings(
    *,
    api_token: str = "an-api-token-of-thirty-two-chars",
    master_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    gitlab: GitLabSettings | None = None,
    runtime: RuntimeSettings | None = None,
) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  (see test_settings.py)
        api_token=SecretStr(api_token),
        master_key=SecretStr(master_key),
        gitlab=gitlab if gitlab is not None else GitLabSettings(),
        runtime=runtime
        if runtime is not None
        else RuntimeSettings(e2b=E2bSettings(deployment="factorial-test")),
    )


def test_a_complete_configuration_has_no_problems() -> None:
    assert container.problems(_settings, environ={}) == []


def test_the_startup_check_builds_both_host_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        "orchestrator.container.create_adapter",
        lambda kind, **_options: recorded.append(kind),
    )
    assert container.problems(_settings, environ={}) == []
    assert recorded == ["claude_cli", "codex_cli"]


def test_an_empty_api_token_is_named() -> None:
    assert container.problems(lambda: _settings(api_token=""), environ={}) == [
        "FACTORIAL_API_TOKEN is empty"
    ]


@pytest.mark.parametrize(
    ("token", "problem"),
    [
        ("dev-token-change-me", "a published sample value"),
        ("x" * 31, "shorter than 32 characters"),
    ],
)
def test_a_weak_api_token_is_named(token: str, problem: str) -> None:
    [found] = container.problems(lambda: _settings(api_token=token), environ={})
    assert found.startswith(f"FACTORIAL_API_TOKEN is {problem}")


def test_an_empty_master_key_is_named() -> None:
    [found] = container.problems(lambda: _settings(master_key=""), environ={})
    assert found.startswith("FACTORIAL_MASTER_KEY is empty")


@pytest.mark.parametrize(
    ("key", "problem"),
    [("change-me", "is not valid base64"), ("changeme", "must decode to 32 bytes")],
)
def test_a_master_key_in_the_wrong_format_is_named(key: str, problem: str) -> None:
    """Otherwise the app reports ready, and every secret read fails in the middle of a request."""
    [found] = container.problems(lambda: _settings(master_key=key), environ={})
    assert found.startswith("FACTORIAL_MASTER_KEY") and problem in found
    assert "openssl rand -base64 32" in found


def test_a_deployment_needs_no_forge_of_its_own() -> None:
    """Each project holds its own GitHub connection, so the deployment holds none, and GitLab
    stays optional."""
    assert container.problems(lambda: _settings(gitlab=GitLabSettings()), environ={}) == []


def test_the_retired_github_app_settings_left_in_the_services_environment_are_named() -> None:
    """Settings no longer name them, so the secret check alone would let a deployment's leftover
    App key reach every agent run."""
    environ = {"FACTORIAL_GITHUB__PRIVATE_KEY": "pem", "FACTORIAL_GITHUB__APP_ID": "1"}
    named = [problem.split(" ", 1)[0] for problem in container.problems(_settings, environ)]
    assert named == ["FACTORIAL_GITHUB__APP_ID", "FACTORIAL_GITHUB__PRIVATE_KEY"]


def test_settings_that_do_not_load_are_named() -> None:
    def load() -> Settings:
        raise ValueError("SF_ was retired in issue #138; rename to FACTORIAL_: SF_API_TOKEN")

    [problem] = container.problems(load, environ={})
    assert problem.startswith("settings do not load")
    assert "SF_API_TOKEN" in problem


def test_main_prints_each_problem_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(container, "problems", lambda: ["one", "two"])
    with pytest.raises(SystemExit) as exited:
        container.main()
    assert exited.value.code == 1
    assert capsys.readouterr().err.splitlines() == ["factorial: one", "factorial: two"]


def test_main_exits_zero_when_nothing_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "problems", list)
    with pytest.raises(SystemExit) as exited:
        container.main()
    assert exited.value.code == 0


def test_the_deployment_name_is_needed_only_where_the_cloud_sandbox_is_offered() -> None:
    """The image always sets the state root, so the documented two-value `.env` starts and offers
    the host alone; an explicit @e2b offer without a name to reap by still stops the start."""
    e2b = E2bSettings(state_root=Path("/data/runs/e2b"))
    unset = RuntimeSettings(e2b=e2b)
    assert container.problems(lambda: _settings(runtime=unset), environ={}) == []
    assert not any(offer.endswith("@e2b") for offer in _settings(runtime=unset).offers)
    offered = RuntimeSettings(e2b=e2b, offered=["claude_api@e2b"])
    found = container.problems(lambda: _settings(runtime=offered), environ={})
    assert sum("DEPLOYMENT" in problem for problem in found) == 1


def test_the_retired_e2b_lines_of_an_old_env_file_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host's env file may still carry the deployment's key and pins, and its default kind and
    routes, after this code ships: they must not stop the service starting. The offered list
    names only a host runtime, so E2B stays off and needs no deployment name."""
    monkeypatch.setenv("FACTORIAL_RUNTIME__KIND", "codex_e2b")
    monkeypatch.setenv("FACTORIAL_RUNTIME__ROUTES", '{"codex": "codex_cli"}')
    monkeypatch.setenv("FACTORIAL_RUNTIME__E2B__API_KEY", "e2b_retired")
    monkeypatch.setenv("FACTORIAL_RUNTIME__E2B__CLAUDE_TEMPLATE", "not-even-a-pin")
    for name in ("STATE_ROOT", "DEPLOYMENT"):
        monkeypatch.setenv(f"FACTORIAL_RUNTIME__E2B__{name}", "")
    monkeypatch.setenv("FACTORIAL_RUNTIME__OFFERED", "claude_api@local_cli")
    runtime = Settings().runtime
    assert runtime.e2b.state_root is None
    assert container.problems(lambda: _settings(runtime=runtime), environ={}) == []


def test_a_secret_left_in_the_services_environment_is_named() -> None:
    """Every agent run inherits that environment, so a secret init did not move would reach each
    one, and nothing else would say so. An external database's URL holds a password; the image's
    own loopback URL holds none worth keeping."""
    environ = {
        "FACTORIAL_GITLAB__TOKEN": "s",
        "factorial_master_key": "k",
        "FACTORIAL_LOG_LEVEL": "INFO",
        "FACTORIAL_DATABASE_URL": "postgresql+asyncpg://factory:pw@db.example/factory",
        "FACTORIAL_IMAGE_DATABASE": "external",
    }

    def named(env: dict[str, str]) -> list[str]:
        return [problem.split(" ", 1)[0] for problem in container.problems(_settings, env)]

    assert named(environ) == [
        "FACTORIAL_DATABASE_URL",
        "FACTORIAL_GITLAB__TOKEN",
        "factorial_master_key",
    ]
    assert named({**environ, "FACTORIAL_IMAGE_DATABASE": "internal"}) == [
        "FACTORIAL_GITLAB__TOKEN",
        "factorial_master_key",
    ]


def test_a_secret_setting_is_named_however_it_is_annotated() -> None:
    """The leak check knows a secret only by its annotation; a field it missed would pass the
    check and reach every agent run."""

    class Inner(BaseModel):
        token: SecretStr | None = None

    class Outer(BaseModel):
        key: SecretStr = SecretStr("")
        inner: Inner = Inner()
        maybe: Inner | None = None
        plain: str = ""

    assert sorted(container._secret_names(Outer)) == [
        "FACTORIAL_INNER__TOKEN",
        "FACTORIAL_KEY",
        "FACTORIAL_MAYBE__TOKEN",
    ]


def test_aws_credentials_left_in_the_services_environment_are_named() -> None:
    """An agent run that inherits one can act as the task role, and nothing else would say so."""
    environ = {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/x",
        "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "ECS_AGENT_URI": "http://169.254.170.2/api/x",
    }
    named = [problem.split(" ", 1)[0] for problem in container.problems(_settings, environ)]
    assert named == [
        "AWS_ACCESS_KEY_ID",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "ECS_AGENT_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
    ]
    assert container.problems(_settings, {"AWS_REGION": "us-east-1"}) == []


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("master_key")
async def test_master_key_problem_names_a_volume_made_with_another_key(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _only_our_rows(async_session)
    await store.put_for_organization(
        async_session, await default_organization_id(async_session), "slot", "v", by="t"
    )
    monkeypatch.setenv("FACTORIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    get_settings.cache_clear()
    load_keyring.cache_clear()
    problem = await container.master_key_problem(async_session)
    assert problem is not None and "docker volume rm factorial-public" in problem


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("master_key")
async def test_master_key_problem_passes_an_empty_or_matching_volume(
    async_session: AsyncSession,
) -> None:
    await _only_our_rows(async_session)
    assert await container.master_key_problem(async_session) is None
    await store.put_for_organization(
        async_session, await default_organization_id(async_session), "slot", "v", by="t"
    )
    assert await container.master_key_problem(async_session) is None


async def _only_our_rows(session: AsyncSession) -> None:
    """The database is shared by the whole run and other tests commit secrets under their own
    keys; the session never commits, so this deletion is undone when the test ends."""
    for model in (Secret, RuntimeCredential, OrganizationCredential):
        await session.execute(delete(model))
