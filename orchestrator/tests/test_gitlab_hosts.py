"""Which GitLab host a project may name. Any organization owner writes `settings.gitlab`, and the
API sends the project's reads there, so a host inside the deployment's network would turn the
assistant's read proxy into a window onto it (task metadata at 169.254.170.2, any VPC host)."""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.factorial.runtime_policy import SettingsInvalid, validate_settings
from orchestrator.github.client import is_rate_limited
from orchestrator.gitlab import hosts
from orchestrator.gitlab.hosts import (
    HostRefused,
    check_base_url,
    check_project_gitlab,
    checked_base_url,
)
from tests.api_harness import authed_client_for, build_app, make_settings

OPERATOR = "http://10.0.0.5:8080/gitlab"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.170.2/v2/metadata?",
        "https://169.254.170.2/v2/metadata?",
        "https://169.254.169.254",
        "https://[::1]",
        "https://[::ffff:169.254.169.254]",
        "https://127.1",
        "https://2852039166",
        "https://0.0.0.0",
        "https://user:pass@gitlab.com",
        "https://g\u212aitlab.com",
        "https://gitlab.com:0",
        "https://gitlab.com/x?",
        "https://gitlab.com/#frag",
        "http://169.254.169.254",
        "ftp://gitlab.com",
    ],
)
def test_a_project_may_not_name_a_gitlab_host_inside_the_deployment(url: str) -> None:
    with pytest.raises(HostRefused):
        checked_base_url(url, operator_base="", multi_org=False)


@pytest.mark.parametrize(
    "url", ["https://10.0.0.9", "https://100.64.0.1", "https://[fd00::1]", "https://192.168.1.1"]
)
def test_under_multi_org_a_private_address_is_refused_too(url: str) -> None:
    assert checked_base_url(url, operator_base="", multi_org=False) == url
    with pytest.raises(HostRefused):
        checked_base_url(url, operator_base="", multi_org=True)


def test_the_operator_s_own_gitlab_origin_is_always_allowed() -> None:
    for url in (OPERATOR, "http://10.0.0.5:8080/gitlab/", "HTTP://10.0.0.5:8080/other"):
        assert checked_base_url(url, operator_base=OPERATOR, multi_org=True)


async def test_a_name_resolving_to_loopback_is_refused() -> None:
    with pytest.raises(HostRefused):
        await check_base_url("https://localhost", operator_base="", multi_org=False)


async def test_a_resolver_that_does_not_answer_holds_the_project_instead_of_refusing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal made every GitLab ticket's reconcile raise, and the worker quarantined them all
    during a DNS blip. A backoff is held and never counted, and is not kept, so the next pass
    asks again."""
    asked: list[str] = []

    async def hangs(host: str, *_: Any, **__: Any) -> list[Any]:
        asked.append(host)
        await asyncio.sleep(60)
        return []

    async def fails(host: str, *_: Any, **__: Any) -> list[Any]:
        asked.append(host)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(hosts, "_LOOKUP_TIMEOUT", 0.01)
    loop = asyncio.get_running_loop()
    for lookup, host in ((hangs, "slow.example.com"), (fails, "flaky.example.com")):
        monkeypatch.setattr(loop, "getaddrinfo", lookup)
        for _ in range(2):
            with pytest.raises(Exception) as exc:
                await check_base_url(f"https://{host}", operator_base="", multi_org=True)
            assert not isinstance(exc.value, HostRefused)
            assert is_rate_limited(exc.value)
    assert asked == ["slow.example.com"] * 2 + ["flaky.example.com"] * 2


def test_a_settings_write_refuses_a_bad_gitlab_host() -> None:
    validate_settings({"gitlab": {"base_url": "https://gitlab.com", "ca_bundle": ""}})
    with pytest.raises(SettingsInvalid):
        validate_settings({"gitlab": {"base_url": "http://169.254.170.2/v2/metadata?"}})


def test_plain_http_and_a_ca_file_path_are_refused_only_under_multi_org() -> None:
    """A path makes the server read a file its writer chooses, and plain http sends the token in
    the clear: a stranger's risk under open signup. A single-organization deployment's projects
    saved both before, and an on-premises GitLab behind a private CA needs them."""
    bags = (
        {"base_url": "http://gitlab.corp.example"},
        {"base_url": "https://gitlab.com", "ca_bundle": "/etc/ssl/certs/org-ca.pem"},
    )
    for bag in bags:
        check_project_gitlab({"gitlab": bag}, operator_base="", multi_org=False)
        with pytest.raises(HostRefused):
            check_project_gitlab({"gitlab": bag}, operator_base="", multi_org=True)


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_creating_a_project_refuses_a_gitlab_host_inside_the_deployment(
    async_session: AsyncSession,
) -> None:
    statuses = []
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        for base_url in ("https://169.254.169.254", "https://localhost"):
            response = await client.post(
                "/api/orgs/default/projects",
                json={
                    "name": f"gl-{uuid4().hex[:8]}",
                    "github_repo": "group/app",
                    "settings": {"forge": "gitlab", "gitlab": {"base_url": base_url}},
                },
            )
            statuses.append(response.status_code)
    assert statuses == [422, 422]
