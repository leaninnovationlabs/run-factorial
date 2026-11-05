"""Tracker/Forge split: the composite snapshot, the kind default, and the view wrappers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.adapters import (
    FORGE_KINDS,
    Bindings,
    BranchState,
    CheckRunState,
    CommentState,
    CompositeStateSource,
    Forge,
    IssueState,
    PullRequestState,
    ReviewState,
    ReviewVerdict,
    SeededIssue,
    SnapshotState,
    SourceForge,
    SourceTracker,
    SplitProjector,
    StateSource,
    Tracker,
    forge_kind_of,
    views_from_source,
)
from orchestrator.adapters.registry import display_for_settings
from orchestrator.github.client import GitHubClient
from orchestrator.github.observer import GitHubStateSource
from orchestrator.github.state import SnapshotState as GithubSnapshotState
from orchestrator.gitlab.observer import gitlab_views
from runtime.gitlab_delivery import gitlab_delivery_author

TRACKER_ISSUE = IssueState(
    number=99,
    title="Jira ticket",
    body="from the tracker",
    state="open",
    labels=frozenset({"factorial:refine"}),
    assignees=("alice",),
)

FORGE_ISSUE = IssueState(
    number=12,
    title="unused",
    body="",
    state="open",
    labels=frozenset(),
    assignees=(),
)


def _tracker_snapshot() -> SnapshotState:
    return SnapshotState(
        repo="ignored/tracker",
        issue_state=TRACKER_ISSUE,
        issue_comment_states=(
            CommentState(
                author="factory-bot",
                body="Evidence Pack",
                created_at="2026-10-29T00:00:00Z",
            ),
        ),
    )


def _forge_snapshot() -> SnapshotState:
    return SnapshotState(
        repo="acme/app",
        issue_state=FORGE_ISSUE,
        branch_states=(BranchState(name="spec/99-jira", sha="aaa"),),
        pull_request_states=(
            PullRequestState(
                number=7,
                head_ref="spec/99-jira",
                base_ref="main",
                head_sha="aaa",
                state="open",
                merged=False,
                url="https://github.com/acme/app/pull/7",
            ),
        ),
        files_by_ref={"spec/99-jira": ("docs/spec.md",)},
        check_runs_by_sha={
            "aaa": (CheckRunState(name="ci", status="completed", conclusion="success"),)
        },
        pr_comment_states_by_number={
            7: (CommentState(author="reviewer", body="lgtm", created_at="2026-10-29T01:00:00Z"),)
        },
        review_states_by_number={
            7: (ReviewState(author="reviewer", body="ok", created_at="2026-10-29T01:00:00Z"),)
        },
        review_verdicts_by_number={7: ReviewVerdict(approvals=("reviewer",))},
        permissions_by_login={"reviewer": "write"},
    )


def test_github_state_reexports_the_same_objects() -> None:
    """Existing `orchestrator.github.state` imports keep working and name the moved types."""
    assert GithubSnapshotState is SnapshotState


def test_views_and_composite_satisfy_the_protocols() -> None:
    tracker, forge = views_from_source(_tracker_snapshot(), bot_login="factory-bot")
    source = CompositeStateSource(tracker, forge)
    assert isinstance(tracker, Tracker)
    assert isinstance(forge, Forge)
    assert isinstance(source, StateSource)


def test_github_state_source_still_wraps() -> None:
    """The real observer splits into the same views a later composite will pair."""
    source = GitHubStateSource(GitHubClient(None), "acme/widgets", 42)
    tracker, forge = views_from_source(source, bot_login="factorial[bot]")
    assert isinstance(CompositeStateSource(tracker, forge), StateSource)
    assert forge.repo == "acme/widgets"


@pytest.mark.parametrize(
    ("tracker_kind", "settings", "expected"),
    [
        (None, None, "github"),
        ("", None, "github"),
        ("github", None, "github"),
        ("gitlab", None, "gitlab"),
        ("jira", None, None),
        ("jira", {"forge": "github"}, "github"),
        ("github", {"forge": "gitlab"}, "github"),
        ("gitlab", {"forge": "github"}, "gitlab"),
        (None, {"forge": "gitlab"}, "github"),
        ("github", {"forge": "jira"}, "github"),
        ("jira", {"forge": "jira"}, None),
    ],
)
def test_forge_kind_of_defaults(
    tracker_kind: str | None, settings: dict[str, object] | None, expected: str | None
) -> None:
    assert forge_kind_of(tracker_kind, settings) == expected


def test_a_github_project_does_not_link_to_the_gitlab_host_it_left() -> None:
    """Switching the ticket source back to GitHub keeps `settings.gitlab`, which must not move
    the project's links onto the GitLab host."""
    left = {"forge": "gitlab", "gitlab": {"base_url": "https://gitlab.example.com"}}
    github = display_for_settings("github", left)
    assert github.origin == "https://github.com"
    assert github.issue_href("acme/app", "7") == "https://github.com/acme/app/issues/7"
    assert display_for_settings("gitlab", left).origin == "https://gitlab.example.com"


def test_only_github_and_gitlab_are_forges() -> None:
    assert "github" in FORGE_KINDS
    assert "gitlab" in FORGE_KINDS
    assert "jira" not in FORGE_KINDS
    assert len(FORGE_KINDS) == 2


async def test_composite_reads_issue_from_tracker_and_code_from_forge() -> None:
    tracker = SourceTracker(_tracker_snapshot(), bot_login="factory-bot")
    forge = SourceForge(_forge_snapshot(), bot_login="factorial[bot]")
    source = CompositeStateSource(tracker, forge)

    assert source.repo == "acme/app"
    assert await source.issue() is TRACKER_ISSUE
    comments = await source.issue_comments()
    assert [c.author for c in comments] == ["factory-bot"]
    assert [b.name for b in await source.branches()] == ["spec/99-jira"]
    pulls = await source.pull_requests()
    assert [p.number for p in pulls] == [7]
    assert list(await source.files("spec/99-jira")) == ["docs/spec.md"]
    assert [c.name for c in await source.check_runs("aaa")] == ["ci"]
    assert [c.author for c in await source.pr_comments(7)] == ["reviewer"]
    assert [r.author for r in await source.pr_reviews(7)] == ["reviewer"]
    assert await source.pr_review_verdict(7) == ReviewVerdict(approvals=("reviewer",))
    assert await source.repo_permission("reviewer") == "write"
    assert await source.files("missing") == ()
    assert await source.pr_review_verdict(999) == ReviewVerdict()


async def test_seeded_issue_still_wraps_a_composite() -> None:
    source = CompositeStateSource(
        SourceTracker(_tracker_snapshot()), SourceForge(_forge_snapshot())
    )
    seeded = SeededIssue(inner=source, seed=FORGE_ISSUE)
    assert await seeded.issue() is FORGE_ISSUE
    assert [b.name for b in await seeded.branches()] == ["spec/99-jira"]


def test_delivery_author_is_on_each_view() -> None:
    tracker, forge = views_from_source(_tracker_snapshot(), bot_login="factory-bot")
    assert tracker.is_delivery_author("factory-bot")
    assert tracker.is_delivery_author("Factory-Bot")
    assert not tracker.is_delivery_author("alice")
    assert not SourceTracker(_tracker_snapshot()).is_delivery_author("factory-bot")
    assert forge.is_delivery_author("factory-bot")
    assert not forge.is_delivery_author("reviewer")


def test_gitlab_views_use_the_run_bot_fence() -> None:
    tracker, forge = gitlab_views(_tracker_snapshot(), bot_login="factory-gitlab")
    assert tracker.is_delivery_author("factory-gitlab")
    assert tracker.is_delivery_author("project_12_bot_ab12")
    assert not tracker.is_delivery_author("alice")
    assert forge.is_delivery_author("project_12_bot_ab12")
    assert gitlab_delivery_author("project_12_bot_ab12", "") is True


#: A host-kind check: the enum member, an `==`/`!=` between a host literal and a kind or forge
#: name or a call's result, or an isinstance against a GitHub or GitLab type. Comparing a
#: settings key (`k != "github"`) is not one.
_HOST_KIND_CHECK = re.compile(
    r"TicketSourceKind\.GIT(?:HUB|LAB)"
    r"""|(?:\b\w*(?:kind|forge)\w*|\))\s*[!=]=\s*["'](?:gitlab|github)["']"""
    r"""|["'](?:gitlab|github)["']\s*[!=]="""
    r"|isinstance\([^\n]*Git(?:Lab|Hub)"
)

#: The code issue #420's split has reached. The rest of `api/` is a follow-up.
_SPLIT = (
    "worker",
    "predicates",
    "adapters",
    "mcp_server",
    "api/readiness.py",
    "api/routes/admin.py",
)


def test_no_host_kind_checks_outside_adapter_packages() -> None:
    """Issue #420: kind and isinstance checks belong in github/, gitlab/, and the registry."""
    root = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
    registry = root / "adapters" / "registry.py"
    offenders: list[str] = []
    for part in _SPLIT:
        start = root / part
        for path in [start] if start.is_file() else sorted(start.rglob("*.py")):
            if path == registry:
                continue
            for index, line in enumerate(path.read_text().splitlines(), 1):
                if _HOST_KIND_CHECK.search(line):
                    offenders.append(f"{path.relative_to(root)}:{index}:{line.strip()}")
    assert offenders == []


@pytest.mark.parametrize(
    "line",
    [
        "if source.kind == TicketSourceKind.GITLAB:",
        'if tick.source_kind == "gitlab":',
        'if forge_of(source) != "github":',
        'if "gitlab" == kind:',
        "if isinstance(ctx.source, GitLabStateSource):",
    ],
)
def test_the_host_kind_guard_catches_each_shape(line: str) -> None:
    assert _HOST_KIND_CHECK.search(line)


def test_the_host_kind_guard_lets_a_settings_key_pass() -> None:
    line = 'kept = {k: v for k, v in settings.items() if k != "github"}'
    assert not _HOST_KIND_CHECK.search(line)


async def test_bindings_pair_a_tracker_with_a_different_forge() -> None:
    """A Jira-shaped tracker plus the GitHub forge share one snapshot and split writes."""

    class FakeTracker:
        kind = "jira"
        bot_login = "jira-bot"

        def observe(self, repo: str, key: str) -> SourceTracker:
            del repo, key
            return SourceTracker(_tracker_snapshot(), bot_login=self.bot_login)

        def writes(self, repo: str) -> object:
            del repo
            return "tracker-writes"

        def media(self, repo: str) -> None:
            del repo
            return None

        def media_refs(self, text: str) -> tuple[()]:
            del text
            return ()

        def is_delivery_author(self, login: str) -> bool:
            return login == self.bot_login

        async def candidates(self, repo: str) -> tuple[IssueState, ...]:
            del repo
            return (TRACKER_ISSUE,)

        def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
            return views_from_source(source, bot_login=self.bot_login)

    class FakeForge:
        kind = "github"
        bot_login = "factorial[bot]"
        withheld_secrets: frozenset[str] = frozenset()

        def full_source(self, repo: str, key: str) -> SnapshotState:
            del repo, key
            return _forge_snapshot()

        def observe(self, repo: str, key: str) -> SourceForge:
            del repo, key
            return SourceForge(_forge_snapshot(), bot_login=self.bot_login)

        def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
            return views_from_source(source, bot_login=self.bot_login)

        def writes(self, repo: str) -> object:
            del repo
            return "forge-writes"

        def contract(self, repo: str) -> object:
            del repo
            return "contract"

        def media(self, repo: str) -> None:
            del repo
            return None

        def is_delivery_author(self, login: str) -> bool:
            return login == self.bot_login

        def hold_until(self, repo: str) -> None:
            del repo
            return None

        async def trees(self, repo: str, sha: str) -> tuple[()]:
            del repo, sha
            return ()

        async def compare(self, repo: str, base: str, head: str) -> None:
            del repo, base, head
            return None

        async def listed_issue(self, repo: str, key: str, labels: object, listings: object) -> None:
            del repo, key, labels, listings
            return None

        async def grant_run_secrets(
            self, repo: str, run_id: int, **kwargs: object
        ) -> dict[str, object]:
            del repo, run_id, kwargs
            return {}

        async def release_run_credential(self, repo: str, run_id: int) -> None:
            del repo, run_id

        async def sweep_run_credentials(self, repo: str, *, active_run_ids: frozenset[int]) -> int:
            del repo, active_run_ids
            return 0

    bindings = Bindings(FakeTracker(), FakeForge())  # type: ignore[arg-type]
    source = bindings.snapshot("acme/app", "PROJ-123")
    assert source.repo == "acme/app"
    assert await source.issue() is TRACKER_ISSUE
    assert [branch.name for branch in await source.branches()] == ["spec/99-jira"]
    projector = bindings.projector("acme/app")
    assert isinstance(projector, SplitProjector)
    assert projector.tracker == "tracker-writes"
    assert projector.forge == "forge-writes"


class _NoTicketSource:
    """A session whose ticket-source lookup finds nothing, so the project defaults to GitHub."""

    async def execute(self, _statement: object) -> _NoTicketSource:
        return self

    def scalar_one_or_none(self) -> None:
        return None


async def test_a_same_host_project_resolves_its_connection_once() -> None:
    """The tracker and forge of a GitHub project share one resolution, not one each: each
    resolution decrypts the project's credential."""
    from orchestrator.adapters import host_registries, resolve_bindings
    from orchestrator.db.models import Project

    resolved: list[int] = []

    async def github(_session: object, project: Project) -> object:
        resolved.append(project.id)
        return object()

    trackers, forges = host_registries(github)  # type: ignore[arg-type]
    project = Project(id=7, settings={})
    bindings = await resolve_bindings(
        _NoTicketSource(),  # type: ignore[arg-type]
        project,
        trackers=trackers,
        forges=forges,
    )
    assert bindings is not None
    assert (bindings.tracker.kind, bindings.forge.kind) == ("github", "github")
    assert resolved == [7]


async def test_a_project_without_its_host_s_connection_has_no_binding() -> None:
    """Nothing falls back to another host: no GitHub connection means no binding at all."""
    from orchestrator.adapters import host_registries, resolve_bindings
    from orchestrator.db.models import Project

    async def unconnected(_session: object, _project: Project) -> None:
        return None

    trackers, forges = host_registries(unconnected)
    assert (
        await resolve_bindings(
            _NoTicketSource(),  # type: ignore[arg-type]
            Project(id=7, settings={}),
            trackers=trackers,
            forges=forges,
        )
        is None
    )
