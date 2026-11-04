"""The delivery probes both host CLI adapters share, called directly: `discover_artifacts` asks
a fake `git` and `gh`, and `expectation_verdict` judges what came back. No run is spawned.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.adapters._cli.probes import (
    ENV_BOT_LOGIN,
    ProbedRun,
    discover_artifacts,
    expectation_verdict,
)
from runtime.adapters._cli.process import Cli, CommandResult
from runtime.contract import Artifact, ArtifactKind, ExpectedArtifact, RunStatus

HEAD_SHA = "d34db33f" * 5

#: The comment probe is fenced on the run's own clock, so these tests pin it rather than race
#: it: the run starts at RUN_STARTED, and a comment is fresh or stale by sitting either side of
#: that instant.
RUN_STARTED = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
FRESH_AT = datetime(2026, 8, 21, 12, 30, tzinfo=UTC)
STALE_AT = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)

#: The App's own bot login — same value convention as `orchestrator/tests/test_predicates.py`
#: and `test_reconcile.py`.
BOT_LOGIN = "sentinel[bot]"


def _comment(
    body: str, *, created: datetime | None = FRESH_AT, author: str | None = BOT_LOGIN
) -> dict[str, Any]:
    """A comment as the REST comments endpoint returns one — `created_at` included, because that
    is what decides whether it is THIS run's evidence, and `user.login`, because that
    is what decides whether it is the App's OWN evidence. `created=None`/`author=None` omit the
    field entirely, for the undated/unauthored cases.

    REST's spelling throughout, not GraphQL's (`created_at`/`user`/`html_url`, not
    `createdAt`/`author`/`url`): a fixture in the other shape is what let the probe read a login
    without the `[bot]` suffix for a whole release while every test passed."""
    comment: dict[str, Any] = {"body": body, "html_url": "u/9"}
    if created is not None:
        comment["created_at"] = created.isoformat().replace("+00:00", "Z")
    if author is not None:
        comment["user"] = {"login": author}
    return comment


@dataclass(frozen=True)
class _Probed:
    expected: tuple[ExpectedArtifact, ...]
    ticket: str = "GH-12"
    repo: str = "acme/widgets"
    branch: str = "spec/12-{slug}"
    started_at: datetime = RUN_STARTED
    bot_login: str = BOT_LOGIN


class ProbeCli(Cli):
    """`git` and `gh` answering the declared-expectation probes: `ls-remote` filtered by the
    pattern it is handed (the real `git` does), pull requests listed with their head refs, and
    the REST comments payload `gh api` returns."""

    def __init__(
        self,
        *,
        branches: Sequence[str] = (),
        pulls: Sequence[dict[str, Any]] = (),
        comments: Sequence[dict[str, Any]] = (),
    ) -> None:
        super().__init__(agent_bin="claude")
        self.branches = list(branches)
        self.pulls = list(pulls)
        self.comments = list(comments)
        self.commands: list[tuple[str, ...]] = []

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        self.commands.append((program, *args))
        argv = (program, *args)

        def ok(stdout: str) -> CommandResult:
            return CommandResult(argv=argv, returncode=0, stdout=stdout, stderr="")

        if "ls-remote" in args:
            pattern = args[-1]
            matched = [name for name in self.branches if fnmatch.fnmatch(name, pattern)]
            return ok("".join(f"{HEAD_SHA}\trefs/heads/{name}\n" for name in matched))
        if program == "gh" and args[:1] == ("api",):
            return ok(json.dumps(self.comments))
        if program == "gh":
            return ok(json.dumps(self.pulls))
        return ok("")


def _expect(kind: ArtifactKind, **selector: str) -> ExpectedArtifact:
    return ExpectedArtifact(kind=kind, selector=selector)


EVIDENCE = (_expect(ArtifactKind.COMMENT, marker="## Evidence Pack"),)


async def probed(
    cli: ProbeCli,
    expected: tuple[ExpectedArtifact, ...],
    *,
    outcome: RunStatus = RunStatus.SUCCEEDED,
    reason: str | None = None,
    bot_login: str = BOT_LOGIN,
    env: Mapping[str, str] | None = None,
) -> tuple[RunStatus, str | None, tuple[Artifact, ...]]:
    """The verdict on a run the CLI ended with `outcome`, and the artifacts it was judged on."""
    state: ProbedRun = _Probed(expected=expected, bot_login=bot_login)
    artifacts = await discover_artifacts(cli, state, env or {}, git_at=())
    verdict, why = expectation_verdict(state, outcome, reason, artifacts)
    return verdict, why, artifacts


async def test_a_met_expectation_is_reported_and_the_run_succeeds() -> None:
    cli = ProbeCli(
        branches=["spec/12-widget-endpoint"],
        pulls=[{"number": 7, "url": "u/7", "headRefName": "spec/12-widget-endpoint"}],
    )

    status, _, artifacts = await probed(
        cli,
        (
            _expect(ArtifactKind.BRANCH, head_glob="spec/12-*"),
            _expect(ArtifactKind.PR, head_glob="spec/12-*"),
        ),
    )

    assert status is RunStatus.SUCCEEDED
    pr = next(a for a in artifacts if a.kind is ArtifactKind.PR)
    # The head travels, unlike the undeclared path's bare number: it is what ties the artifact
    # back to the expectation that asked for it.
    assert pr.locator == {"number": "7", "head": "spec/12-widget-endpoint"}


async def test_an_unmet_expectation_fails_a_run_the_cli_called_successful() -> None:
    """The agent reported success, the declared evidence is absent. The reason names which
    expectation."""
    cli = ProbeCli(branches=["spec/12-widget-endpoint"])

    status, reason, _ = await probed(
        cli,
        (
            _expect(ArtifactKind.BRANCH, head_glob="spec/12-*"),
            _expect(ArtifactKind.PR, head_glob="spec/12-*"),
        ),
    )

    assert status is RunStatus.FAILED
    assert reason is not None
    summary, _, detail = reason.partition("\n\n")
    assert summary == (
        "The agent said it finished, but no pull request from a branch starting with spec/12- "
        "was found."
    )
    assert 'pr(head_glob="spec/12-*")' in detail
    assert 'branch(head_glob="spec/12-*")' not in reason  # that one was there


async def test_a_comment_only_delivery_is_honoured() -> None:
    """A run that pushes nothing and posts one Evidence Pack comment is delivered."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict")])

    status, _, artifacts = await probed(cli, EVIDENCE)

    assert status is RunStatus.SUCCEEDED
    (comment,) = artifacts
    assert comment.kind is ArtifactKind.COMMENT
    assert comment.locator == {"marker": "## Evidence Pack", "where": "issue", "issue": "GH-12"}


async def test_an_absent_comment_fails_the_run() -> None:
    cli = ProbeCli(comments=[_comment("unrelated chatter")])

    status, reason, _ = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED
    assert reason is not None
    assert reason.startswith(
        'The agent said it finished, but no comment containing "## Evidence Pack" on the issue '
        "was found.\n\n"
    )
    assert 'comment(marker="## Evidence Pack")' in reason


async def test_an_expectation_never_overwrites_a_truer_failure() -> None:
    """A run that was cancelled already has a better explanation than 'the evidence is absent',
    and replacing it would hide why the agent never got as far as delivering."""
    cli = ProbeCli()

    verdict = await probed(
        cli,
        (_expect(ArtifactKind.PR, head_glob="spec/12-*"),),
        outcome=RunStatus.CANCELLED,
        reason="cancelled by the operator",
    )

    assert verdict[:2] == (RunStatus.CANCELLED, "cancelled by the operator")


async def test_a_comment_older_than_the_run_does_not_satisfy_it() -> None:
    """Attempt 1's Evidence Pack must not close attempt 2. A comment is never overwritten by the
    next attempt the way a branch is, so an unfenced one would answer forever."""
    cli = ProbeCli(
        comments=[_comment("## Evidence Pack\n\nfrom the previous attempt", created=STALE_AT)]
    )

    status, reason, artifacts = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED
    assert artifacts == ()
    assert reason is not None
    assert 'comment(marker="## Evidence Pack")' in reason


async def test_an_undated_comment_is_not_this_runs_evidence() -> None:
    """Fails toward 'absent': a comment that cannot be dated cannot be dated to this run."""
    cli = ProbeCli(comments=[{"body": "## Evidence Pack", "html_url": "u/9"}])

    status, _, _ = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED


async def test_a_pull_request_older_than_the_run_still_satisfies_it() -> None:
    """The deliberate asymmetry: the reject -> rework flow reuses the same pull request as the
    delivery vehicle, so finding the one attempt 1 opened is the right answer. Only COMMENT is
    fenced on the run clock — the listing is not even asked for a timestamp."""
    cli = ProbeCli(pulls=[{"number": 7, "url": "u/7", "headRefName": "spec/12-widget-endpoint"}])

    status, _, _ = await probed(cli, (_expect(ArtifactKind.PR, head_glob="spec/12-*"),))

    assert status is RunStatus.SUCCEEDED


# -------------------------------------------------------- COMMENT author fence + kind isolation


async def test_a_third_party_comment_carrying_the_marker_does_not_satisfy_it() -> None:
    """The fake-delivery vector the author fence exists to close, as in the comment predicates:
    a third party pasting the marker text must not forge delivery evidence."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nforged", author="some-other-user")])

    status, _, artifacts = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED
    assert artifacts == ()


async def test_a_null_author_comment_does_not_satisfy_it() -> None:
    """`user: null` is the shape the API actually returns for a comment whose account was
    deleted (verified live, 2026-08-21) — not a missing `user` key. The `isinstance` guard
    treats it the same as any other non-`dict` user: fails closed, never crashes."""
    # `_comment(author=None)` OMITS the key; this test's whole value is pinning the explicit
    # `"user": null` wire shape, so set the key by hand.
    null_author = _comment("## Evidence Pack\n\nverdict")
    null_author["user"] = None
    cli = ProbeCli(comments=[null_author])

    status, _, artifacts = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED
    assert artifacts == ()


async def test_a_bot_authored_comment_matches_case_insensitively() -> None:
    """GitHub logins are not case sensitive; the bot login a connection saved and the App's own
    casing from GitHub's API need not agree letter for letter."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict", author=BOT_LOGIN.upper())])

    status, _, artifacts = await probed(cli, EVIDENCE, bot_login=BOT_LOGIN)

    assert status is RunStatus.SUCCEEDED
    assert artifacts != ()


async def test_the_run_s_env_names_the_bot_login_on_github_too() -> None:
    """The login a run was handed wins over the one it kept, on GitHub as on GitLab."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict")])

    status, _, artifacts = await probed(
        cli, EVIDENCE, bot_login="someone-else[bot]", env={ENV_BOT_LOGIN: BOT_LOGIN}
    )

    assert status is RunStatus.SUCCEEDED
    assert [a.kind for a in artifacts] == [ArtifactKind.COMMENT]


async def test_an_unset_bot_login_fails_closed_even_for_the_bots_own_comment() -> None:
    """UNSET/empty `bot_login` must fail CLOSED — same direction as `library.py`'s fence: no
    GitHub login is ever the empty string, so an unconfigured adapter trusts nothing, even a
    comment that genuinely is the bot's own."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict")])  # author=BOT_LOGIN

    status, _, artifacts = await probed(cli, EVIDENCE, bot_login="")

    assert status is RunStatus.FAILED
    assert artifacts == ()
    # Fails closed before even asking `gh` — an unconfigured adapter should not probe at all.
    assert not any(cmd[0] == "gh" for cmd in cli.commands)


async def test_a_bot_authored_but_stale_comment_does_not_satisfy_it() -> None:
    """Composition: the author fence and the freshness fence both must hold. A
    bot-authored comment from a previous attempt is exactly the case the freshness fence exists
    to catch, and the author fence passing it must not override that."""
    cli = ProbeCli(
        comments=[_comment("## Evidence Pack\n\nfrom the previous attempt", created=STALE_AT)]
    )

    status, _, artifacts = await probed(cli, EVIDENCE)

    assert status is RunStatus.FAILED
    assert artifacts == ()


async def test_a_branch_only_expectation_makes_zero_pr_list_calls() -> None:
    """A BRANCH-only expectation never asks `gh pr list`."""
    cli = ProbeCli(branches=["spec/12-widget-endpoint"])

    status, _, _ = await probed(cli, (_expect(ArtifactKind.BRANCH, head_glob="spec/12-*"),))

    assert status is RunStatus.SUCCEEDED
    assert not any(cmd[0] == "gh" for cmd in cli.commands)


async def test_a_pr_only_expectation_makes_zero_ls_remote_calls() -> None:
    """A PR-only expectation never asks `git ls-remote` for branches."""
    cli = ProbeCli(pulls=[{"number": 7, "url": "u/7", "headRefName": "spec/12-widget-endpoint"}])

    status, _, _ = await probed(cli, (_expect(ArtifactKind.PR, head_glob="spec/12-*"),))

    assert status is RunStatus.SUCCEEDED
    assert not any("ls-remote" in cmd for cmd in cli.commands)


async def test_the_comment_probe_asks_the_rest_comments_endpoint() -> None:
    """REST through `gh api`, never `gh pr view --json comments`.

    The two APIs disagree about the App's own name — GraphQL answers `sentinel`, REST answers
    `sentinel[bot]` — while this fence and `orchestrator.predicates.library`'s comment predicates
    both compare against the one configured `bot_login`, which the predicates see in REST's
    spelling. On GraphQL no value of that setting satisfied both, and a delivered comment read as
    an undelivered artifact. Pinning the endpoint is what stops the probe drifting back.
    """
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict")])

    status, _, _ = await probed(cli, EVIDENCE)

    assert status is RunStatus.SUCCEEDED
    asked = [cmd for cmd in cli.commands if cmd[0] == "gh" and cmd[1] == "api"]
    assert asked == [
        (
            "gh",
            "api",
            "repos/acme/widgets/issues/GH-12/comments"
            "?per_page=100&since=2026-08-21T12:00:00Z",  # RUN_STARTED, the run's own clock
        )
    ]
    assert not any("--json" in cmd and "comments" in cmd for cmd in cli.commands)


async def test_a_comment_only_expectation_makes_zero_branch_or_pr_calls() -> None:
    """A COMMENT-only expectation never asks `git ls-remote` or `gh pr list` — the other two
    probes have nothing to declare for."""
    cli = ProbeCli(comments=[_comment("## Evidence Pack\n\nverdict")])

    status, _, _ = await probed(cli, EVIDENCE)

    assert status is RunStatus.SUCCEEDED
    assert not any("ls-remote" in cmd for cmd in cli.commands)
    assert not any(cmd[0] == "gh" and "list" in cmd for cmd in cli.commands)
