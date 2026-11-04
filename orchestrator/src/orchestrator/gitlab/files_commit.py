"""Commit config onto the `factorial` branch through the GitLab API.

Same contract as `github.files_pr.commit_config_files` and `seed_config_branch`. GitLab's commit
API creates the commit and moves the branch in one call, so the record is written immediately
after that call rather than before the ref moves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote

from orchestrator.github import ConfigBranchRules
from orchestrator.github.files_pr import ConfigBranchExists, ConfigBranchMoved
from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = ["commit_config_files", "config_branch_rules", "seed_config_branch"]


async def commit_config_files(
    client: GitLabClient,
    repo: str,
    branch: str,
    base_sha: str,
    files: Mapping[str, str],
    message: str,
    record: Callable[[str], Awaitable[None]],
) -> str:
    """One commit adding `files` on `base_sha`, onto `branch`. Returns its sha.

    Raises `ConfigBranchMoved` when `branch` is no longer at `base_sha`.
    """
    pid = quote_project(repo)
    head = await client.get(f"/projects/{pid}/repository/commits/{quote(branch, safe='')}")
    if not isinstance(head, dict) or head.get("id") != base_sha:
        raise ConfigBranchMoved(f"`{branch}` on {repo} moved since {base_sha[:7]} was read")
    existing = await _blobs(client, pid, base_sha)
    created = await client.request(
        "POST",
        f"/projects/{pid}/repository/commits",
        json={
            "branch": branch,
            "commit_message": message,
            "actions": [
                {
                    "action": "update" if path in existing else "create",
                    "file_path": path,
                    "content": content,
                }
                for path, content in sorted(files.items())
            ],
        },
    )
    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
        raise GitLabError(f"{repo} did not return a commit")
    sha = created["id"]
    parents = created.get("parent_ids")
    if not isinstance(parents, list) or base_sha not in parents:
        raise ConfigBranchMoved(f"`{branch}` on {repo} moved since {base_sha[:7]} was read")
    await record(sha)
    return sha


_EMPTY_README = (
    "This branch holds the Factorial config. Edit processes from the Factorial dashboard, which\n"
    "commits them here.\n"
)


async def seed_config_branch(
    client: GitLabClient,
    repo: str,
    branch: str,
    config_dirs: Sequence[str],
    record: Callable[[str], Awaitable[None]],
    *,
    empty: bool = False,
) -> str:
    """Create `branch` holding the default branch's config folder, or only a README.

    GitLab has no orphan-commit API. When the default branch already has one of `config_dirs`
    and `empty` is false, `branch` is pointed at that head (the config bytes are reused) and
    `record` runs before the ref exists. When `empty` or the default branch has no config,
    a README is committed onto a new `branch` started from the default head so the builder
    has a base. Raises `ConfigBranchExists`.
    """
    if not config_dirs:
        raise GitLabError(f"{repo} has no config folder to seed")
    pid = quote_project(repo)
    quoted = quote(branch, safe="")
    try:
        await client.get(f"/projects/{pid}/repository/branches/{quoted}")
    except GitLabError as exc:
        if exc.status_code != 404:
            raise
    else:
        raise ConfigBranchExists(f"{repo} already has a `{branch}` branch")
    project = await client.get(f"/projects/{pid}")
    if not isinstance(project, dict) or not isinstance(project.get("default_branch"), str):
        raise GitLabError(f"{repo} has no default_branch")
    default = project["default_branch"]
    head = await client.get(f"/projects/{pid}/repository/commits/{quote(default, safe='')}")
    if not isinstance(head, dict) or not isinstance(head.get("id"), str):
        raise GitLabError(f"{repo} has no commit on its default branch")
    sha = head["id"]
    folder = None if empty else await _config_dir(client, pid, sha, config_dirs)
    if folder is not None:
        await record(sha)
        await client.request(
            "POST",
            f"/projects/{pid}/repository/branches",
            params={"branch": branch, "ref": sha},
        )
        return sha
    await client.request(
        "POST",
        f"/projects/{pid}/repository/files/{quote(f'{config_dirs[0]}/README.md', safe='')}",
        json={
            "branch": branch,
            "start_branch": default,
            "content": _EMPTY_README,
            "commit_message": f"Seed the {branch} config branch empty, to start from a template",
        },
    )
    created = await client.get(f"/projects/{pid}/repository/branches/{quoted}")
    commit = created.get("commit") if isinstance(created, dict) else None
    new_sha = commit.get("id") if isinstance(commit, dict) else None
    if not isinstance(new_sha, str):
        raise GitLabError(f"{repo} did not return a commit for `{branch}`")
    await record(new_sha)
    return new_sha


async def _config_dir(
    client: GitLabClient, pid: str, sha: str, config_dirs: Sequence[str]
) -> str | None:
    try:
        rows = await client.paginate(f"/projects/{pid}/repository/tree", ref=sha)
    except GitLabError as exc:
        if exc.status_code == 404:
            return None
        raise
    names = {
        row["name"]
        for row in rows
        if isinstance(row, dict) and row.get("type") == "tree" and isinstance(row.get("name"), str)
    }
    return next((folder for folder in config_dirs if folder in names), None)


async def config_branch_rules(
    client: GitLabClient, repo: str, branch: str
) -> ConfigBranchRules | None:
    """The `factorial` branch head, or None when that branch does not exist.

    A protected branch is reported as refusing deletion and force-push, which is the profile the
    readiness row asks for. GitLab has no GitHub ruleset to copy.
    """
    try:
        payload = await client.get(
            f"/projects/{quote_project(repo)}/repository/branches/{quote(branch, safe='')}"
        )
    except GitLabError as exc:
        if exc.status_code == 404:
            return None
        raise
    commit = payload.get("commit") if isinstance(payload, dict) else None
    sha = commit.get("id") if isinstance(commit, dict) else None
    if not isinstance(sha, str):
        raise GitLabError(f"{repo} branch {branch} has no commit")
    protected = isinstance(payload, dict) and payload.get("protected") is True
    return ConfigBranchRules(
        branch=branch,
        head_sha=sha,
        rule_types=frozenset({"non_fast_forward", "deletion"}) if protected else frozenset(),
    )


async def _blobs(client: GitLabClient, pid: str, sha: str) -> set[str]:
    try:
        rows = await client.paginate(
            f"/projects/{pid}/repository/tree",
            ref=sha,
            path=".factorial",
            recursive=True,
        )
    except GitLabError as exc:
        if exc.status_code == 404:
            return set()
        raise
    return {
        row["path"]
        for row in rows
        if isinstance(row, dict) and row.get("type") == "blob" and isinstance(row.get("path"), str)
    }
