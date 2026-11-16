"""The config branch's two writes: seeding it, and committing a builder change to it (tech-stack
decision 33, issue #248).

`commit_config_files` writes exactly the paths it is handed, and deletes those handed with no
body. Containment is the caller's job, and nothing here checks the paths again: the config-commits
route admits only paths that `assemble()` proves in the same request (design.md §7.7), and a
restore takes its paths from the two listings the server reads (tech-stack decision 93).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from githubkit.exception import RequestFailed

from orchestrator.github.client import GitHubClient, split_repo

__all__ = ["ConfigBranchExists", "ConfigBranchMoved", "commit_config_files", "seed_config_branch"]

logger = logging.getLogger(__name__)


class ConfigBranchExists(Exception):
    """The branch a seed would create is already on the repo."""


class ConfigBranchMoved(Exception):
    """The branch no longer points at the commit a config commit was built on."""


async def commit_config_files(
    client: GitHubClient,
    repo: str,
    branch: str,
    base_sha: str,
    files: Mapping[str, str | None],
    message: str,
    record: Callable[[str], Awaitable[None]],
) -> str:
    """One commit writing `files` on `base_sha`, fast-forwarded onto `branch`. Returns its sha.
    A `None` body deletes the path.

    `record` gets the sha before the ref moves, so the branch never points at a commit the record
    does not name. Raises `ConfigBranchMoved` when `branch` is no longer at `base_sha`.
    """
    api = await client.api(repo)
    owner, name = split_repo(repo)
    base_commit = (await api.rest.git.async_get_commit(owner, name, base_sha)).parsed_data
    tree = (
        await api.rest.git.async_create_tree(
            owner,
            name,
            base_tree=base_commit.tree.sha,
            tree=[
                # A null sha removes the path from the tree.
                {"path": path, "mode": "100644", "type": "blob", "sha": None}
                if content is None
                else {"path": path, "mode": "100644", "type": "blob", "content": content}
                for path, content in sorted(files.items())
            ],
        )
    ).parsed_data
    commit = (
        await api.rest.git.async_create_commit(
            owner, name, message=message, tree=tree.sha, parents=[base_sha]
        )
    ).parsed_data
    await record(commit.sha)
    try:
        await api.rest.git.async_update_ref(
            owner, name, f"heads/{branch}", sha=commit.sha, force=False
        )
    except RequestFailed as exc:
        # A ruleset refusal is a 422 too; only this message means the branch moved.
        if exc.response.status_code != 422 or "fast forward" not in exc.response.text.lower():
            raise
        raise ConfigBranchMoved(
            f"`{branch}` on {repo} moved since {base_sha[:7]} was read"
        ) from exc
    return commit.sha


_EMPTY_README = (
    "This branch holds the Factorial config. Edit processes from the Factorial dashboard, which\n"
    "commits them here.\n"
)


async def seed_config_branch(
    client: GitHubClient,
    repo: str,
    branch: str,
    config_dirs: Sequence[str],
    record: Callable[[str], Awaitable[None]],
    *,
    empty: bool = False,
) -> str:
    """Create `branch` as an orphan commit holding only the first of `config_dirs` the default
    branch's head has, byte for byte (its tree sha is reused), or only a README when `empty` or
    the default branch has none. Returns the new commit's sha.

    Orphan so the branch carries config and nothing else. `empty` is for an operator starting from
    a template: its files reach the branch only through the builder's commit, which recomputes
    every path it writes (design.md §7.7). `record` gets the sha before the ref exists, so no
    branch is ever left that the record does not name. Raises `ConfigBranchExists`.
    """
    api = await client.api(repo)
    owner, name = split_repo(repo)
    try:
        await api.rest.repos.async_get_branch(owner, name, branch)
    except RequestFailed as exc:
        if exc.response.status_code != 404:
            raise
    else:
        raise ConfigBranchExists(f"{repo} already has a `{branch}` branch")

    folder: str | None = None
    subtrees: dict[str, str] = {}
    if empty:
        message = f"Seed the {branch} config branch empty, to start from a template"
    else:
        default = (await api.rest.repos.async_get(owner, name)).parsed_data.default_branch
        head = (await api.rest.repos.async_get_commit(owner, name, default)).parsed_data.sha
        top = (await api.rest.git.async_get_tree(owner, name, head)).parsed_data.tree
        subtrees = {item.path: item.sha for item in top if item.type == "tree" and item.path}
        folder = next((d for d in config_dirs if subtrees.get(d)), None)
        message = f"Seed the {branch} config branch from {default}@{head[:7]}"
    # A repo with no config yet still gets a branch, so the builder has a base to commit onto;
    # git keeps no empty folder, hence the README.
    tree = (
        await api.rest.git.async_create_tree(
            owner,
            name,
            tree=[
                {"path": folder, "mode": "040000", "type": "tree", "sha": subtrees[folder]}
                if folder is not None
                else {
                    "path": f"{config_dirs[0]}/README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": _EMPTY_README,
                }
            ],
        )
    ).parsed_data
    commit = (
        await api.rest.git.async_create_commit(
            owner,
            name,
            message=message,
            tree=tree.sha,
            parents=[],
        )
    ).parsed_data
    await record(commit.sha)
    try:
        await api.rest.git.async_create_ref(owner, name, ref=f"refs/heads/{branch}", sha=commit.sha)
    except RequestFailed as exc:
        if exc.response.status_code != 422:
            raise
        # 422 is also git refusing `factorial` beside a `factorial/...` branch.
        reason = exc.response.text[:200]
        logger.warning("GitHub refused to create %s on %s: %s", branch, repo, reason)
        hint = (
            f"The `{branch}` branch exists."
            if "already exists" in reason
            else f"A branch under `{branch}/` blocks it. Delete those branches first."
        )
        raise ConfigBranchExists(f"GitHub refused to create `{branch}` on {repo}. {hint}") from exc
    return commit.sha
