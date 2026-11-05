"""GitLab readings of the readiness probes that otherwise require the GitHub App."""

from __future__ import annotations

from urllib.parse import unquote

from orchestrator.adapters.display import GITLAB_DISPLAY
from orchestrator.api.readiness import protection_item
from orchestrator.db.models import Project
from orchestrator.gitlab.client import GitLabError, quote_project
from orchestrator.gitlab.readiness import (
    GitLabBranchProtection,
    create_repo_label,
    project_visible,
    repo_branch_protection,
    repo_ci_files,
    repo_labels,
)

REPO = "group/app"
PID = quote_project(REPO)


class _Api:
    def __init__(self) -> None:
        self.project: dict[str, object] = {
            "default_branch": "main",
            "only_allow_merge_if_pipeline_succeeds": False,
            "ci_config_path": None,
        }
        self.protected: dict[str, object] | None = None
        self.approvals: dict[str, object] | Exception | None = {
            "approvals_before_merge": 0,
            "reset_approvals_on_push": False,
        }
        self.rules: list[object] | Exception = []
        self.labels: list[object] = []
        self.files: set[str] = set()
        self.posts: list[tuple[str, object]] = []
        self.label_error: GitLabError | None = None

    async def get(self, url: str, **params: object) -> object:
        if url == f"/projects/{PID}":
            return self.project
        if "/protected_branches/" in url:
            if self.protected is None:
                raise GitLabError("not protected", status_code=404)
            return self.protected
        if url.endswith("/approvals"):
            if isinstance(self.approvals, Exception):
                raise self.approvals
            return self.approvals
        if "/repository/files/" in url:
            path = unquote(url.rsplit("/repository/files/", 1)[1])
            if path not in self.files:
                raise GitLabError("missing file", status_code=404)
            return {"file_name": path}
        raise AssertionError(url)

    async def paginate(self, url: str, **params: object) -> list[object]:
        if url.endswith("/labels"):
            return self.labels
        if url.endswith("/approval_rules"):
            if isinstance(self.rules, Exception):
                raise self.rules
            return self.rules
        raise AssertionError(url)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: object = None,
        json: object = None,
    ) -> object:
        if self.label_error is not None:
            raise self.label_error
        self.posts.append((path, json))
        return {"name": "ok"}


async def test_project_visible_returns_the_path_the_token_can_read() -> None:
    api = _Api()
    api.project["path_with_namespace"] = "group/app"
    assert await project_visible(api, REPO) == "group/app"  # type: ignore[arg-type]


async def test_labels_are_the_project_palette() -> None:
    api = _Api()
    api.labels = [{"name": "factorial:plan"}, {"name": 3}, "nope"]
    assert await repo_labels(api, REPO) == frozenset({"factorial:plan"})  # type: ignore[arg-type]


async def test_create_label_prefixes_the_hash_and_treats_a_duplicate_as_success() -> None:
    api = _Api()
    await create_repo_label(api, REPO, "factorial:plan", "0e8a16", "Plan")  # type: ignore[arg-type]
    assert api.posts == [
        (
            f"/projects/{PID}/labels",
            {"name": "factorial:plan", "color": "#0e8a16", "description": "Plan"},
        )
    ]
    api.label_error = GitLabError('{"message":"Label already exists"}', status_code=400)
    await create_repo_label(api, REPO, "factorial:plan", "0e8a16", "Plan")  # type: ignore[arg-type]


async def test_missing_protected_branch_is_unprotected() -> None:
    rule = await repo_branch_protection(_Api(), REPO)  # type: ignore[arg-type]
    assert rule == GitLabBranchProtection(branch="main", protected=False, pipeline_required=False)
    assert rule.shortfalls() == ("no protection rule",)


async def test_protected_branch_maps_approvals_code_owners_and_pipelines() -> None:
    api = _Api()
    api.project["only_allow_merge_if_pipeline_succeeds"] = True
    api.protected = {"code_owner_approval_required": False}
    api.approvals = {"approvals_before_merge": 0, "reset_approvals_on_push": True}
    api.rules = [{"rule_type": "code_owner", "approvals_required": 1}, {"approvals_required": 2}]
    rule = await repo_branch_protection(api, REPO)  # type: ignore[arg-type]
    assert rule.protected is True
    assert rule.required_approvals == 2
    assert rule.requires_codeowners is True
    assert rule.dismisses_stale is True
    assert rule.pipeline_required is True
    assert rule.shortfalls() == ()


async def test_a_premium_approval_endpoint_that_refuses_counts_as_absent() -> None:
    api = _Api()
    api.protected = {"code_owner_approval_required": False}
    api.approvals = GitLabError("premium", status_code=403)
    api.rules = GitLabError("premium", status_code=404)
    rule = await repo_branch_protection(api, REPO)  # type: ignore[arg-type]
    assert "no required approval" in rule.shortfalls()
    assert "pipelines are not required to succeed" in rule.shortfalls()
    assert all("verify" not in gap for gap in rule.shortfalls())


async def test_ci_file_is_the_configured_path() -> None:
    api = _Api()
    assert await repo_ci_files(api, REPO) == ()  # type: ignore[arg-type]
    api.files.add(".gitlab-ci.yml")
    assert await repo_ci_files(api, REPO) == (".gitlab-ci.yml",)  # type: ignore[arg-type]
    api.project["ci_config_path"] = "other/app:.gitlab-ci.yml@main"
    found = await repo_ci_files(api, REPO)  # type: ignore[arg-type]
    assert found == ("other/app:.gitlab-ci.yml@main",)


async def test_the_protection_row_on_gitlab_keeps_the_glab_command_and_has_no_checks() -> None:
    """The per-setting checks read GitHub's rule. A GitLab profile carries none of those
    settings, so the row has no checks and its remedy is the command that sets the whole
    profile."""

    async def read(repo: str) -> GitLabBranchProtection:
        return GitLabBranchProtection(branch="main", protected=True)

    row = await protection_item(read, Project(github_repo=REPO), {}, host=GITLAB_DISPLAY)

    assert row is not None
    assert row.state == "warn"
    assert row.checks == []
    assert row.remedy is not None
    assert row.remedy.startswith(f"glab api --method PUT projects/{PID}")
