"""Artifact discovery: what the run left behind, probed through `git` and `gh`.

Vendor-free — a branch on the remote and a PR in the listing look the same whatever harness
pushed them. Parameterised by the `Cli` seam, the run's bot login (`ENV_BOT_LOGIN`), and where
git finds the run's repository (`git_at`).

With nothing declared this is best effort and informational only — the orchestrator accepts a
step on its postconditions, never on this (design.md §2.4) — so every failure is swallowed.
With expectations declared the same probes run against the declared selectors and the
supervisor reads an unmet one as `failed`, so the swallowing cuts the other way: a `gh` that is
not installed reads as "the evidence is absent". The alternative is worse — a run reported as
delivered because the check could not be made.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import quote

from runtime.adapters._cli.process import GH_BIN
from runtime.contract import (
    SELECTOR_MARKER,
    SELECTOR_WHERE,
    WHERE_PR,
    AgentRuntimeError,
    Artifact,
    ArtifactKind,
    ExpectedArtifact,
    RunStatus,
    missing_expectations,
)
from runtime.gitlab_delivery import gitlab_delivery_author
from runtime.hostgit import NO_TOKEN, remote_url

if TYPE_CHECKING:
    from runtime.adapters._cli.process import Cli

__all__ = [
    "DECLARED_PR_FIELDS",
    "DECLARED_PR_LIMIT",
    "ENV_BOT_LOGIN",
    "PROBEABLE_EXPECTATIONS",
    "ProbedRun",
    "discover_artifacts",
    "expectation_verdict",
]

#: The declared path's pull-request listing horizon. Higher than the undeclared path's
#: `--limit 1` head lookup because a delivered pull request the listing did not reach reads as
#: an unmet expectation and fails a run that succeeded.
DECLARED_PR_LIMIT: Final = "200"
#: `state` rides along so `_abandoned` can drop an earlier attempt's closed PR.
DECLARED_PR_FIELDS: Final = "number,url,headRefName,state"

#: The run's forge login, which the orchestrator hands each run with its token: what its commits
#: name and what the comment probe fences on.
ENV_BOT_LOGIN: Final = "FACTORIAL_BOT_LOGIN"

#: The `ExpectedArtifact` kinds `discover_artifacts` can check for: a branch through
#: `git ls-remote`, a pull request and a comment through `gh`. COMMIT and FILE are absent because
#: the agent's own worktree is not evidence of delivery; a spec declaring one is refused at spawn
#: rather than ignored (`environment.refuse_unverifiable_expectations`).
PROBEABLE_EXPECTATIONS: Final = frozenset(
    {ArtifactKind.BRANCH, ArtifactKind.PR, ArtifactKind.COMMENT}
)


class ProbedRun(Protocol):
    """What the probes read of a run. Each adapter keeps its own state model; both carry these."""

    @property
    def ticket(self) -> str: ...

    @property
    def repo(self) -> str: ...

    @property
    def branch(self) -> str: ...

    @property
    def started_at(self) -> datetime: ...

    @property
    def expected(self) -> tuple[ExpectedArtifact, ...]: ...

    @property
    def bot_login(self) -> str:
        """The login the run was last handed (`ENV_BOT_LOGIN`), kept for a probe whose
        environment no longer carries it."""
        ...


def expectation_verdict(
    state: ProbedRun,
    outcome: RunStatus,
    reason: str | None,
    artifacts: Sequence[Artifact],
    env: Mapping[str, str] | None = None,
) -> tuple[RunStatus, str | None]:
    """The declared-delivery verdict: a run the CLI called successful, whose declared
    artifact is not there, is `failed` and says which one is missing.

    Only `SUCCEEDED` is reconsidered: a run that was cancelled, timed out or crashed already
    has a truer reason than "the evidence is absent". Nothing happens when the spec declared
    no expectations.
    """
    if outcome is not RunStatus.SUCCEEDED or not state.expected:
        return outcome, reason
    forge = "gitlab" if env is not None and _gitlab_host(env) else "github"
    fallback = state.branch.replace("{slug}", "*")
    unmet = missing_expectations(state.expected, artifacts, fallback_glob=fallback)
    if not unmet:
        return outcome, reason
    absent = " and ".join(_absent(expectation, fallback, forge) for expectation in unmet)
    found = "was" if len(unmet) == 1 else "were"
    declared = ", ".join(expectation.display_summary(forge) for expectation in unmet)
    return RunStatus.FAILED, (
        f"The agent said it finished, but {absent} {found} found.\n\nMissing: {declared}"
    )


def _branch(glob: str) -> str:
    if "*" not in glob:
        return glob
    stem = glob.rstrip("*")
    if "*" not in stem:
        return f"a branch starting with {stem}"
    return f"a branch matching {glob}"


def _absent(expectation: ExpectedArtifact, fallback_glob: str, forge: str) -> str:
    """The expectation as a person reads it missing, its glob in words."""
    head = _branch(expectation.head_glob(fallback_glob))
    request = "merge request" if forge == "gitlab" else "pull request"
    match expectation.kind:
        case ArtifactKind.BRANCH:
            return f"no branch {head.removeprefix('a branch ')}"
        case ArtifactKind.PR:
            return f"no {request} from {head}"
        case ArtifactKind.COMMENT if expectation.where == WHERE_PR:
            return f'no comment containing "{expectation.marker}" on the {request} from {head}'
        case ArtifactKind.COMMENT:
            return f'no comment containing "{expectation.marker}" on the issue'
        case _:
            return f"no {expectation.kind.value}"


def _abandoned(entry: Mapping[str, Any]) -> bool:
    """Closed without merging: an earlier attempt's history, not this run's delivery."""
    return str(entry.get("state", "")).upper() == "CLOSED"


def _comment_is_fresh(comment: Any, started_at: datetime) -> bool:
    """Was this comment written by the run that is asking?

    Anything undated, unparseable or older than the run's own start is not this run's evidence.
    A naive timestamp is read as UTC, which is what GitHub sends (`...Z`).
    """
    if not isinstance(comment, Mapping):
        return False
    text = str(comment.get("created_at", "") or "").strip()
    if not text:
        return False
    try:
        created = datetime.fromisoformat(text)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created >= started_at


def _comments_endpoint(repo: str, number: str, since: datetime) -> str:
    """The REST path for one issue's — or one pull request's — conversation comments.

    A pull request's conversation IS its issue's conversation, so `where=issue` and `where=pr`
    ask this one endpoint about different numbers. `since` narrows the page to the run's own
    window; it filters on GitHub's UPDATED time, so `_comment_is_fresh` still decides.
    """
    moment = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"repos/{repo}/issues/{number}/comments?per_page=100&since={stamp}"


async def discover_artifacts(
    cli: Cli,
    state: ProbedRun,
    env: Mapping[str, str],
    *,
    git_at: tuple[str, ...],
    git_at_env: Mapping[str, str] | None = None,
) -> tuple[Artifact, ...]:
    """What the run left behind — the declared path when expectations exist, the best-effort
    default otherwise. `git_at` is the host git's option pair for the run's repository
    (`checkout.probe_git_at`), read for HEAD only, with `git_at_env`, the environment that
    goes with it. A substrate with no host checkout passes `()`: branches and pull requests
    are asked of GitHub, and no COMMIT is reported.

    The remote is always its URL, never a remote name: a name resolves through the
    repository's config, which the agent can write, and the token goes wherever it points."""
    remote = remote_url(state.repo, env.get("GIT_HTTP_HOST", ""))
    if state.expected:
        return await _discover_expected(
            cli, state, env, env.get(ENV_BOT_LOGIN) or state.bot_login, remote
        )

    found: list[Artifact] = []
    pattern = state.branch.replace("{slug}", "*")

    with contextlib.suppress(AgentRuntimeError, OSError):
        if git_at:
            pinned = {**NO_TOKEN, **(git_at_env or {})}
            head = await cli.git(*git_at, "rev-parse", "HEAD", env=pinned)
            if head.ok and head.stdout.strip():
                sha = head.stdout.strip()
                found.append(Artifact(kind=ArtifactKind.COMMIT, locator={"sha": sha}))
        # The pushed branch is the one the predicate will look for, so ask the remote rather
        # than the local checkout — a local branch that was never pushed is not delivery.
        heads = await cli.git("ls-remote", "--heads", remote, pattern, env=env)
        for line in heads.stdout.splitlines() if heads.ok else []:
            _, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
                found.append(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": branch}))
                found.extend(await _discover_pr(cli, state, branch, env))
    return tuple(found)


async def _discover_pr(
    cli: Cli, state: ProbedRun, branch: str, env: Mapping[str, str]
) -> list[Artifact]:
    try:
        listed = await cli.run(
            GH_BIN,
            "pr",
            "list",
            "--repo",
            state.repo,
            "--head",
            branch,
            "--json",
            "number,url",
            "--limit",
            "1",
            env=env,
        )
    except AgentRuntimeError:
        # No `gh` on PATH is an ordinary dev-box state, and the PR the orchestrator actually
        # gates on is observed from GitHub, not from here.
        return []
    if not listed.ok:
        return []
    try:
        payload = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        Artifact(
            kind=ArtifactKind.PR,
            locator={"number": str(entry["number"])},
            url=entry.get("url"),
        )
        for entry in payload
        if isinstance(entry, dict) and "number" in entry
    ]


async def _discover_expected(
    cli: Cli,
    state: ProbedRun,
    env: Mapping[str, str],
    bot_login: str,
    remote: str,
) -> tuple[Artifact, ...]:
    """Probe exactly what the spec declared, on its own selectors.

    The head of every pull request found travels in the locator, unlike the undeclared path's
    `{"number": ...}`: `ExpectedArtifact.satisfied_by` matches a PR back to the expectation
    that asked for it through its head ref, and a number alone cannot. Each kind is probed on
    its own, so neither reports a kind nobody declared.
    """
    fallback = state.branch.replace("{slug}", "*")
    found: list[Artifact] = []
    branch_globs = sorted(
        {item.head_glob(fallback) for item in state.expected if item.kind is ArtifactKind.BRANCH}
    )
    for glob in branch_globs:
        found += await _remote_branches(cli, glob, env, remote)
    pr_globs = sorted(
        {item.head_glob(fallback) for item in state.expected if item.kind is ArtifactKind.PR}
    )
    for glob in pr_globs:
        found += [
            Artifact(
                kind=ArtifactKind.PR,
                locator={"number": str(entry["number"]), "head": entry.get("headRefName", "")},
                url=entry.get("url"),
            )
            for entry in await _matching_pulls(cli, state, glob, env)
        ]
    for item in state.expected:
        if item.kind is ArtifactKind.COMMENT:
            found += await _discover_comment(cli, state, item, fallback, env, bot_login)
    return tuple(found)


async def _remote_branches(
    cli: Cli, glob: str, env: Mapping[str, str], remote: str
) -> list[Artifact]:
    """Branches on the remote matching `glob` — the remote, because a local branch that was
    never pushed is not delivery. `ls-remote` takes the glob itself as a pattern."""
    found: list[Artifact] = []
    with contextlib.suppress(AgentRuntimeError, OSError):
        heads = await cli.git("ls-remote", "--heads", remote, glob, env=env)
        for line in heads.stdout.splitlines() if heads.ok else []:
            _, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                found.append(
                    Artifact(
                        kind=ArtifactKind.BRANCH,
                        locator={"branch": ref.removeprefix("refs/heads/")},
                    )
                )
    return found


def _gitlab_host(env: Mapping[str, str]) -> str | None:
    # The key clone and push switch on; only a GitLab run is given it.
    host = env.get("GIT_HTTP_HOST") or ""
    return host if host and host != "github.com" else None


def _curl_tls_args(env: Mapping[str, str]) -> tuple[str, ...]:
    """Honour the same CA file the worker injected. curl does not read SSL_CERT_FILE
    unless --cacert is set; GIT_SSL_CAINFO is git-only."""
    path = (
        env.get("CURL_CA_BUNDLE")
        or env.get("SSL_CERT_FILE")
        or env.get("GIT_SSL_CAINFO")
        or ""
    ).strip()
    return ("--cacert", path) if path else ()


async def _matching_pulls(
    cli: Cli, state: ProbedRun, glob: str, env: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Pull requests whose head matches `glob`, less the abandoned ones. `gh pr list --head`
    takes one exact branch, so the listing is filtered here instead."""
    host = _gitlab_host(env)
    if host is not None:
        return await _matching_gitlab_merge_requests(cli, state, glob, env, host)
    try:
        listed = await cli.run(
            GH_BIN,
            "pr",
            "list",
            "--repo",
            state.repo,
            "--state",
            "all",
            "--json",
            DECLARED_PR_FIELDS,
            "--limit",
            DECLARED_PR_LIMIT,
            env=env,
        )
    except AgentRuntimeError:
        return []  # no `gh` on PATH is an ordinary dev-box state
    if not listed.ok:
        return []
    try:
        payload = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        entry
        for entry in payload
        if isinstance(entry, dict)
        and "number" in entry
        and fnmatch.fnmatch(entry.get("headRefName", ""), glob)
        and not _abandoned(entry)
    ]


async def _matching_gitlab_merge_requests(
    cli: Cli, state: ProbedRun, glob: str, env: Mapping[str, str], host: str
) -> list[dict[str, Any]]:
    """Merge requests on a GitLab remote, shaped like `gh pr list` rows for the same filters."""
    token = env.get("GITLAB_TOKEN") or env.get("GH_TOKEN") or ""
    if not token:
        return []
    project = quote(state.repo, safe="")
    url = (
        f"https://{host}/api/v4/projects/{project}/merge_requests"
        f"?state=all&per_page={DECLARED_PR_LIMIT}"
    )
    try:
        listed = await cli.run(
            "curl",
            "-fsS",
            *_curl_tls_args(env),
            "-H",
            f"PRIVATE-TOKEN: {token}",
            url,
            env=env,
        )
    except AgentRuntimeError:
        return []
    if not listed.ok:
        return []
    try:
        payload = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict) or "iid" not in row:
            continue
        gl_state = str(row.get("state") or "opened")
        if gl_state == "merged":
            gh_state = "MERGED"
        elif gl_state == "opened":
            gh_state = "OPEN"
        else:
            gh_state = "CLOSED"
        normalized.append(
            {
                "number": row["iid"],
                "headRefName": str(row.get("source_branch") or ""),
                "url": row.get("web_url"),
                "state": gh_state,
            }
        )
    return [
        entry
        for entry in normalized
        if fnmatch.fnmatch(entry.get("headRefName", ""), glob)
        and not _abandoned(entry)
    ]


async def _discover_comment(
    cli: Cli,
    state: ProbedRun,
    expectation: ExpectedArtifact,
    fallback: str,
    env: Mapping[str, str],
    bot_login: str,
) -> list[Artifact]:
    """A comment carrying the declared marker, on the run's ticket or on its pull request.

    REST through `gh api`, NOT `gh pr view --json comments`: the two APIs spell the App's own
    identity differently (GraphQL drops the `[bot]` suffix REST carries), while this fence and
    the orchestrator's comment predicates compare against one configured `bot_login` — the REST
    spelling. Two fences, both of which must hold: freshness (`created_at >= started_at`, so an
    earlier attempt's comment cannot close this one) and author-is-the-App's-own-bot (so a third
    party pasting the marker during the run cannot forge delivery). A blank `bot_login` fails
    closed. PR and BRANCH expectations stay unfenced on purpose: the reject → rework flow
    reuses the same pull request as the delivery vehicle.
    """
    host = _gitlab_host(env)
    if not bot_login and host is None:
        return []
    if expectation.where == WHERE_PR:
        numbers = [
            str(entry["number"])
            for entry in await _matching_pulls(cli, state, expectation.head_glob(fallback), env)
        ]
    else:
        numbers = [str(state.ticket)]

    if host is not None:
        return await _discover_gitlab_comment(
            cli, state, expectation, numbers, env, host, bot_login
        )

    found: list[Artifact] = []
    for number in numbers:
        try:
            listed = await cli.run(
                GH_BIN, "api", _comments_endpoint(state.repo, number, state.started_at), env=env
            )
        except AgentRuntimeError:
            continue
        if not listed.ok:
            continue
        try:
            payload = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for comment in payload if isinstance(payload, list) else []:
            body = str(comment.get("body", "")) if isinstance(comment, dict) else ""
            if expectation.marker not in body:
                continue
            if not _comment_is_fresh(comment, state.started_at):
                continue
            user = comment.get("user") if isinstance(comment, dict) else None
            login = user.get("login", "") if isinstance(user, dict) else ""
            if not login or str(login).casefold() != bot_login.casefold():
                continue
            found.append(
                Artifact(
                    kind=ArtifactKind.COMMENT,
                    locator={
                        SELECTOR_MARKER: expectation.marker,
                        SELECTOR_WHERE: expectation.where,
                        expectation.where: number,
                    },
                    url=comment.get("html_url") if isinstance(comment, dict) else None,
                )
            )
            break  # one comment carrying the marker is the evidence; the rest are noise
    return found


async def _discover_gitlab_comment(
    cli: Cli,
    state: ProbedRun,
    expectation: ExpectedArtifact,
    numbers: Sequence[str],
    env: Mapping[str, str],
    host: str,
    bot_login: str,
) -> list[Artifact]:
    """MR/issue notes through GitLab REST — same fences as the GitHub `gh api` path."""
    token = env.get("GITLAB_TOKEN") or env.get("GH_TOKEN") or ""
    if not token:
        return []
    project = quote(state.repo, safe="")
    segment = "merge_requests" if expectation.where == WHERE_PR else "issues"
    found: list[Artifact] = []
    for number in numbers:
        url = (
            f"https://{host}/api/v4/projects/{project}/{segment}/{number}/notes"
            f"?sort=desc&per_page=100"
        )
        try:
            listed = await cli.run(
                "curl",
                "-fsS",
                *_curl_tls_args(env),
                "-H",
                f"PRIVATE-TOKEN: {token}",
                url,
                env=env,
            )
        except AgentRuntimeError:
            continue
        if not listed.ok:
            continue
        try:
            payload = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        for note in payload:
            if not isinstance(note, dict) or note.get("system"):
                continue
            body = str(note.get("body") or "")
            if expectation.marker not in body:
                continue
            if not _comment_is_fresh(note, state.started_at):
                continue
            author = note.get("author")
            login = (
                author.get("username", "")
                if isinstance(author, dict)
                else ""
            )
            if not gitlab_delivery_author(login, bot_login):
                continue
            found.append(
                Artifact(
                    kind=ArtifactKind.COMMENT,
                    locator={
                        SELECTOR_MARKER: expectation.marker,
                        SELECTOR_WHERE: expectation.where,
                        expectation.where: number,
                    },
                    url=note.get("web_url") if isinstance(note.get("web_url"), str) else None,
                )
            )
            break
    return found
