"""The `app_access` readiness row, per connection mode: an App's installation on each owner the
project touches, or what GitHub answers the stored token for each repository. No database, no
network."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.api.deps import AccessReader, access_reader_for
from orchestrator.api.readiness import access_item, behind_access
from orchestrator.api.schemas import ReadinessItem
from orchestrator.db.models import Project
from orchestrator.github.client import AppNotInstalled, GitHubClient, InstallationAccess
from orchestrator.github.connection import GitHubConnection

PROJECT = Project(name="p", github_repo="acme/api")
LABELS = ReadinessItem(key="labels", state="ok", summary="all present")


def _installed(account: str = "acme", **permissions: str) -> InstallationAccess:
    return InstallationAccess(
        account=account,
        account_is_org=True,
        installation_id="12345",
        selection="selected",
        permissions={"contents": "write", **permissions},
        repo_reachable=True,
    )


def _app_reader(found: Mapping[str, InstallationAccess | Exception]) -> AccessReader:
    async def installation(repo: str) -> InstallationAccess:
        answer = found[repo]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def no_token(repo: str) -> int:
        raise AssertionError("an App connection never reads with a token")

    return AccessReader(
        mode="app", repos=tuple(found), installation=installation, repo_status=no_token
    )


async def test_an_owner_without_the_app_blocks_access_by_name() -> None:
    intake_out = await access_item(
        _app_reader({"acme/api": _installed(), "other/intake": AppNotInstalled("7", "other")})
    )
    assert intake_out.state == "blocked"
    assert intake_out.summary == "App not installed on other"
    assert intake_out.detail is not None and "installed on other" in intake_out.detail
    assert [(check.key, check.passed) for check in intake_out.checks] == [
        ("acme/api", True),
        ("other/intake", False),
    ]
    # The rows behind it read the project's own repo, which is in reach.
    assert behind_access(intake_out, [LABELS], PROJECT) == [LABELS]

    own_out = await access_item(_app_reader({"acme/api": AppNotInstalled("7", "acme")}))
    assert (own_out.state, own_out.summary) == ("blocked", "App not installed on acme")
    [unread] = behind_access(own_out, [LABELS], PROJECT)
    assert unread.state == "unknown"


async def test_a_workflows_grant_warns() -> None:
    granted = await access_item(_app_reader({"acme/api": _installed(workflows="write")}))
    assert granted.state == "warn"
    assert granted.detail is not None and "workflows" in granted.detail
    assert behind_access(granted, [LABELS], PROJECT) == [LABELS]

    plain = await access_item(_app_reader({"acme/api": _installed()}))
    assert (plain.state, plain.summary) == ("ok", "acme/api · App installed on acme")


def _token_client(statuses: Mapping[str, int], seen: list[httpx.Request]) -> GitHubClient:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(statuses[request.url.path], json={})

    client = GitHubClient(
        GitHubConnection(
            mode="token",
            app_id="",
            private_key=None,
            token=SecretStr("ghp_stored"),
            webhook_secret=None,
            bot_login="operator",
            installations={},
        )
    )
    client._transport = httpx.MockTransport(answer)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return client


@pytest.mark.parametrize(
    ("intake", "state", "summary"),
    [
        (200, "ok", "acme/api, acme/intake · token can read it. Factorial cannot check the "
         "token's workflows permission."),
        (401, "blocked", "GitHub rejected this project's access token"),
        (404, "blocked", "The access token cannot see acme/intake"),
    ],
)
async def test_a_token_project_s_access_row_checks_each_repository_with_the_token(
    intake: int, state: str, summary: str
) -> None:
    seen: list[httpx.Request] = []
    client = _token_client({"/repos/acme/api": 200, "/repos/acme/intake": intake}, seen)

    row = await access_item(access_reader_for(client, ["acme/api", "acme/intake"]))

    assert (row.state, row.summary) == (state, summary)
    assert [request.url.path for request in seen] == ["/repos/acme/api", "/repos/acme/intake"]
    assert {request.headers["authorization"] for request in seen} == {"token ghp_stored"}
    if intake == 404:
        assert [(check.key, check.passed) for check in row.checks] == [
            ("acme/api", True),
            ("acme/intake", False),
        ]
        assert behind_access(row, [LABELS], PROJECT) == [LABELS]
