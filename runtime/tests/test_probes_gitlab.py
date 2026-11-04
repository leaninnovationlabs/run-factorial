"""GitLab merge requests are discovered through the API, not `gh pr list`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from runtime.adapters._cli.probes import _discover_comment, _matching_pulls
from runtime.adapters._cli.process import Cli, CommandResult
from runtime.contract import AgentRuntimeError, ArtifactKind, ExpectedArtifact


class _State:
    repo = "group/app"
    ticket = "42"
    branch = "spec/42-{slug}"
    started_at = datetime(2026, 10, 12, tzinfo=UTC)
    expected = ()
    bot_login = ""


class _Cli(Cli):
    def __init__(self) -> None:
        super().__init__(agent_bin="true")
        self.argv: list[tuple[str, ...]] = []

    async def run(self, program: str, *args: str, **_: object) -> CommandResult:
        argv = (program, *args)
        self.argv.append(argv)
        if argv[0] == "curl":
            body = json.dumps(
                [
                    {
                        "iid": 3,
                        "source_branch": "spec/42-declarations-fixture-set",
                        "web_url": "https://gitlab.com/group/app/-/merge_requests/3",
                        "state": "opened",
                    }
                ]
            )
            return CommandResult(argv=argv, returncode=0, stdout=body, stderr="")
        raise AgentRuntimeError("unexpected")


@pytest.mark.asyncio
async def test_gitlab_lists_merge_requests_when_the_host_is_not_github() -> None:
    cli = _Cli()
    env = {
        "GIT_HTTP_HOST": "gitlab.com",
        "GITLAB_TOKEN": "project-token",
    }
    found = await _matching_pulls(cli, _State(), "spec/42-*", env)  # type: ignore[arg-type]
    assert found[0]["number"] == 3
    assert cli.argv[0][0] == "curl"
    assert "-fsS" in cli.argv[0]
    assert "--fail-with-body" not in cli.argv[0]


@pytest.mark.asyncio
async def test_github_still_uses_gh_pr_list() -> None:
    class GhCli(_Cli):
        async def run(self, program: str, *args: str, **_: object) -> CommandResult:
            argv = (program, *args)
            assert argv[0] == "gh"
            return CommandResult(argv=argv, returncode=0, stdout="[]", stderr="")

    cli = GhCli()
    env = {"GH_TOKEN": "ghs_test", "GITHUB_TOKEN": "ghs_test"}
    await _matching_pulls(cli, _State(), "spec/42-*", env)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_github_run_holding_a_gitlab_host_secret_still_asks_github() -> None:
    """Clone and push follow GIT_HTTP_HOST alone, which only a GitLab run is given. A project
    secret named GITLAB_HOST must not send a GitHub run's delivery check to another forge."""

    class GhCli(_Cli):
        async def run(self, program: str, *args: str, **_: object) -> CommandResult:
            argv = (program, *args)
            assert argv[0] == "gh"
            return CommandResult(argv=argv, returncode=0, stdout="[]", stderr="")

    env = {"GH_TOKEN": "ghs_test", "GITHUB_TOKEN": "ghs_test", "GITLAB_HOST": "gitlab.example"}
    await _matching_pulls(GhCli(), _State(), "spec/42-*", env)  # type: ignore[arg-type]


class _CommentCli(_Cli):
    def __init__(self) -> None:
        super().__init__()
        self.mr_notes = json.dumps(
            [
                {
                    "body": "noise",
                    "created_at": "2026-10-12T10:00:00Z",
                    "author": {"username": "human"},
                    "system": False,
                },
                {
                    "body": "All good\n\n## Test Verdict\n\npass",
                    "created_at": "2026-10-13T12:00:00Z",
                    "author": {"username": "project_87260009_bot_c2c02e59f286f41a8a1798eedd5f2db1"},
                    "system": False,
                    "web_url": "https://gitlab.com/group/app/-/merge_requests/3#note_1",
                },
            ]
        )

    async def run(self, program: str, *args: str, **kwargs: object) -> CommandResult:
        argv = (program, *args)
        url = argv[-1] if argv else ""
        if argv[0] == "curl" and "/notes" in url:
            return CommandResult(argv=argv, returncode=0, stdout=self.mr_notes, stderr="")
        return await super().run(program, *args, **kwargs)


@pytest.mark.asyncio
async def test_gitlab_finds_marker_on_merge_request_notes() -> None:
    cli = _CommentCli()
    env = {
        "GIT_HTTP_HOST": "gitlab.com",
        "GITLAB_TOKEN": "project-token",
    }
    state = _State()
    state.started_at = datetime(2026, 10, 13, 11, 0, tzinfo=UTC)
    expectation = ExpectedArtifact(
        kind=ArtifactKind.COMMENT,
        selector={"marker": "## Test Verdict", "where": "pr", "head_glob": "spec/42-*"},
    )
    found = await _discover_comment(
        cli,
        state,
        expectation,
        "spec/42-*",
        env,
        "wrong-configured-bot",  # type: ignore[arg-type]
    )
    assert len(found) == 1
    assert found[0].locator["marker"] == "## Test Verdict"


@pytest.mark.asyncio
async def test_gitlab_curl_passes_injected_ca_bundle() -> None:
    cli = _Cli()
    env = {
        "GIT_HTTP_HOST": "gitlab.example.com",
        "GITLAB_TOKEN": "project-token",
        "CURL_CA_BUNDLE": "/tmp/factorial-gitlab-ca.pem",
    }
    await _matching_pulls(cli, _State(), "spec/42-*", env)  # type: ignore[arg-type]
    assert "--cacert" in cli.argv[0]
    assert "/tmp/factorial-gitlab-ca.pem" in cli.argv[0]
