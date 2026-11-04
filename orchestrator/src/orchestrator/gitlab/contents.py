"""Read `.factorial/` and other files from GitLab at a pinned commit sha."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from urllib.parse import quote

from orchestrator.github.contents import (
    ComparedCommit,
    ComparedFile,
    Comparison,
    ContentsNotFound,
    DirEntry,
    MergeProvenance,
    TreeEntry,
)
from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = ["GitLabContents"]


class GitLabContents:
    """Duck-types `RepoContents` so `RepoConfigSource` can load a GitLab project's config."""

    def __init__(self, client: GitLabClient) -> None:
        self._client = client

    async def default_branch(self, repo: str) -> str:
        payload = await self._client.get(f"/projects/{quote_project(repo)}")
        if not isinstance(payload, dict) or not isinstance(payload.get("default_branch"), str):
            raise ContentsNotFound(f"{repo} has no default_branch")
        return payload["default_branch"]

    async def resolve_ref(self, repo: str, ref: str) -> str:
        # The config reader asks for `heads/<branch>`, which GitHub accepts and GitLab does not.
        if ref.startswith("heads/"):
            ref = ref.removeprefix("heads/")
        try:
            payload = await self._client.get(
                f"/projects/{quote_project(repo)}/repository/commits/{quote(ref, safe='')}"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                raise ContentsNotFound(f"{repo}@{ref}") from exc
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ContentsNotFound(f"{repo}@{ref}")
        return payload["id"]

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        try:
            payload = await self._client.get(
                f"/projects/{quote_project(repo)}/repository/files/{quote(path, safe='')}",
                ref=sha,
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                raise ContentsNotFound(f"{repo}:{path}@{sha}") from exc
            raise
        if not isinstance(payload, dict):
            raise ContentsNotFound(f"{repo}:{path}@{sha}")
        encoding = payload.get("encoding")
        content = payload.get("content")
        if encoding == "base64" and isinstance(content, str):
            return base64.b64decode(content).decode()
        if isinstance(content, str):
            return content
        raise ContentsNotFound(f"{repo}:{path}@{sha}")

    async def read_blob(self, repo: str, sha: str) -> bytes:
        """One blob's bytes by its git object id."""
        try:
            payload = await self._client.get(
                f"/projects/{quote_project(repo)}/repository/blobs/{quote(sha, safe='')}"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                raise ContentsNotFound(f"{repo}:blob@{sha}") from exc
            raise
        if not isinstance(payload, dict):
            raise ContentsNotFound(f"{repo}:blob@{sha}")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ContentsNotFound(f"{repo}:blob@{sha}")
        try:
            return (
                base64.b64decode(content)
                if payload.get("encoding") == "base64"
                else content.encode()
            )
        except ValueError as exc:
            raise ContentsNotFound(f"{repo}:blob@{sha}") from exc

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        try:
            rows = await self._client.paginate(
                f"/projects/{quote_project(repo)}/repository/tree",
                ref=sha,
                path=path,
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return ()
            raise
        out: list[DirEntry] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            kind = "dir" if row.get("type") == "tree" else "file"
            out.append(
                DirEntry(name=row["name"], path=str(row.get("path") or row["name"]), type=kind)
            )
        return tuple(out)

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
        try:
            rows = await self._client.paginate(
                f"/projects/{quote_project(repo)}/repository/tree",
                ref=sha,
                path=root,
                recursive=True,
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return ()
            raise
        return tuple(
            TreeEntry(path=row["path"], sha=str(row.get("id") or ""))
            for row in rows
            if isinstance(row, dict)
            and row.get("type") == "blob"
            and isinstance(row.get("path"), str)
        )

    async def tree(self, repo: str, sha: str) -> Sequence[TreeEntry]:
        return await self.list_tree(repo, "", sha)

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        """A merged merge request into `base` whose result is `sha`, if one exists."""
        try:
            rows = await self._client.paginate(
                f"/projects/{quote_project(repo)}/merge_requests",
                state="merged",
                target_branch=base,
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)
            raise
        for row in rows:
            if not isinstance(row, dict):
                continue
            produced = {row.get("merge_commit_sha"), row.get("squash_commit_sha"), row.get("sha")}
            if sha not in produced:
                continue
            merged_by = row.get("merged_by")
            username = merged_by.get("username") if isinstance(merged_by, dict) else None
            return MergeProvenance(
                merged_pr=row.get("iid") if isinstance(row.get("iid"), int) else None,
                merged_by=username if isinstance(username, str) else None,
                merged_by_human=isinstance(username, str),
            )
        return MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)

    async def compare(self, repo: str, base: str, head: str) -> Comparison:
        """Commits and changed files on `head` that are not on `base`."""
        try:
            payload = await self._client.request(
                "GET",
                f"/projects/{quote_project(repo)}/repository/compare",
                params={"from": base, "to": head, "straight": "true"},
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                raise ContentsNotFound(f"{repo} cannot compare {base[:7]}...{head[:7]}") from exc
            raise
        if not isinstance(payload, dict):
            raise ContentsNotFound(f"{repo} cannot compare {base[:7]}...{head[:7]}")
        return _comparison(payload)

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        try:
            payload = await self._client.get(
                f"/projects/{quote_project(repo)}/repository/commits/{quote(sha, safe='')}"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                raise ContentsNotFound(f"{repo}@{sha}") from exc
            raise
        parents = payload.get("parent_ids") if isinstance(payload, dict) else None
        if not isinstance(parents, list):
            raise ContentsNotFound(f"{repo}@{sha}")
        return tuple(parent for parent in parents if isinstance(parent, str))


def _comparison(payload: dict[str, object]) -> Comparison:
    listed = payload.get("commits")
    raw_commits = listed if isinstance(listed, list) else []
    commits = tuple(
        ComparedCommit(
            sha=str(row.get("id") or ""),
            message=str(row.get("message") or row.get("title") or ""),
            author_name=row.get("author_name") if isinstance(row.get("author_name"), str) else None,
            authored_at=(
                row.get("authored_date") if isinstance(row.get("authored_date"), str) else None
            ),
        )
        for row in raw_commits
        if isinstance(row, dict)
    )
    diffs = payload.get("diffs")
    raw_diffs = diffs if isinstance(diffs, list) else []
    files = tuple(_compared_file(row) for row in raw_diffs if isinstance(row, dict))
    if payload.get("compare_same_ref") or (not commits and not files):
        status = "identical"
    else:
        status = "ahead"
    return Comparison(
        status=status,
        total_commits=len(commits),
        commits=commits,
        files=files,
    )


def _compared_file(row: dict[str, object]) -> ComparedFile:
    if row.get("new_file"):
        status = "added"
    elif row.get("deleted_file"):
        status = "removed"
    elif row.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    diff = row.get("diff")
    additions, deletions = _diff_counts(diff if isinstance(diff, str) else "")
    path = str(row.get("new_path") or row.get("old_path") or "")
    previous = (
        str(row["old_path"])
        if status == "renamed" and isinstance(row.get("old_path"), str)
        else None
    )
    return ComparedFile(
        path=path,
        status=status,
        additions=additions,
        deletions=deletions,
        previous_path=previous,
    )


def _diff_counts(diff: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("\\"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions
