"""Readiness probes for a GitLab ticket source.

The checklist's profile stays the one GitHub branch protection is measured against. Each part
has a GitLab reading: a protected branch, approval rules (or `approvals_before_merge`), code
owner approval, `reset_approvals_on_push`, and `only_allow_merge_if_pipeline_succeeds` in place
of a required check named `verify`. Approval-rule endpoints are Premium; a 403 or 404 there is
the project not having the feature, so it counts as absent rather than as an unreadable probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = [
    "GitLabBranchProtection",
    "create_repo_label",
    "project_visible",
    "repo_branch_protection",
    "repo_ci_files",
    "repo_labels",
]


@dataclass(frozen=True, slots=True)
class GitLabBranchProtection:
    """GitLab's default-branch profile in the shape readiness expects (`branch`, `shortfalls`)."""

    branch: str
    protected: bool
    required_approvals: int = 0
    requires_codeowners: bool = False
    dismisses_stale: bool = False
    pipeline_required: bool = False

    def shortfalls(self) -> tuple[str, ...]:
        if not self.protected:
            return ("no protection rule",)
        gaps: list[str] = []
        if self.required_approvals < 1:
            gaps.append("no required approval")
        if not self.requires_codeowners:
            gaps.append("no code owner approval")
        if not self.dismisses_stale:
            gaps.append("stale approvals not dismissed")
        if not self.pipeline_required:
            gaps.append("pipelines are not required to succeed")
        return tuple(gaps)


def _pid(repo: str) -> str:
    return quote_project(repo)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


async def project_visible(client: GitLabClient, repo: str) -> str:
    """The project's `path_with_namespace` when the token can read it. A 404 propagates."""
    payload = await client.get(f"/projects/{_pid(repo)}")
    if not isinstance(payload, dict):
        raise GitLabError(f"{repo} was not a project")
    path = payload.get("path_with_namespace")
    return path if isinstance(path, str) and path else repo


async def repo_labels(client: GitLabClient, repo: str) -> frozenset[str]:
    """Label names defined on the project. The palette, not one issue's labels."""
    rows = await client.paginate(f"/projects/{_pid(repo)}/labels")
    return frozenset(
        row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)
    )


async def create_repo_label(
    client: GitLabClient, repo: str, name: str, colour: str, description: str
) -> None:
    """Define one label. A label that already exists is success, as on the GitHub writer."""
    color = colour if colour.startswith("#") else f"#{colour}"
    try:
        await client.request(
            "POST",
            f"/projects/{_pid(repo)}/labels",
            json={"name": name, "color": color, "description": description},
        )
    except GitLabError as exc:
        if exc.status_code == 409 or (exc.status_code == 400 and "already" in str(exc).lower()):
            return
        raise


async def repo_branch_protection(client: GitLabClient, repo: str) -> GitLabBranchProtection:
    """Protection on the project's default branch."""
    pid = _pid(repo)
    project = await client.get(f"/projects/{pid}")
    if not isinstance(project, dict) or not isinstance(project.get("default_branch"), str):
        raise GitLabError(f"{repo} has no default branch")
    branch = project["default_branch"]
    pipeline_required = project.get("only_allow_merge_if_pipeline_succeeds") is True
    try:
        rule = await client.get(f"/projects/{pid}/protected_branches/{quote(branch, safe='')}")
    except GitLabError as exc:
        if exc.status_code == 404:
            return GitLabBranchProtection(
                branch=branch,
                protected=False,
                pipeline_required=pipeline_required,
            )
        raise
    if not isinstance(rule, dict):
        raise GitLabError(f"{repo} protected branch {branch} was not an object")

    approvals_required = 0
    dismisses_stale = False
    try:
        approvals = await client.get(f"/projects/{pid}/approvals")
    except GitLabError as exc:
        if exc.status_code not in (403, 404):
            raise
        approvals = None
    if isinstance(approvals, dict):
        approvals_required = _as_int(approvals.get("approvals_before_merge"))
        dismisses_stale = approvals.get("reset_approvals_on_push") is True

    codeowners = rule.get("code_owner_approval_required") is True
    try:
        rules = await client.paginate(f"/projects/{pid}/approval_rules")
    except GitLabError as exc:
        if exc.status_code not in (403, 404):
            raise
        rules = []
    for row in rules:
        if not isinstance(row, dict):
            continue
        needed = _as_int(row.get("approvals_required"))
        approvals_required = max(approvals_required, needed)
        if row.get("rule_type") == "code_owner" and needed >= 1:
            codeowners = True

    return GitLabBranchProtection(
        branch=branch,
        protected=True,
        required_approvals=approvals_required,
        requires_codeowners=codeowners,
        dismisses_stale=dismisses_stale,
        pipeline_required=pipeline_required,
    )


async def repo_ci_files(client: GitLabClient, repo: str) -> tuple[str, ...]:
    """The project's CI config file, if the default branch has it.

    `ci_config_path` overrides `.gitlab-ci.yml`. A path that points at another project is
    already a configuration, so it counts without a second fetch.
    """
    pid = _pid(repo)
    project = await client.get(f"/projects/{pid}")
    if not isinstance(project, dict) or not isinstance(project.get("default_branch"), str):
        raise GitLabError(f"{repo} has no default branch")
    raw = project.get("ci_config_path")
    path = raw.strip() if isinstance(raw, str) and raw.strip() else ".gitlab-ci.yml"
    if ":" in path:
        return (path,)
    try:
        await client.get(
            f"/projects/{pid}/repository/files/{quote(path, safe='')}",
            ref=project["default_branch"],
        )
    except GitLabError as exc:
        if exc.status_code == 404:
            return ()
        raise
    return (path,)
