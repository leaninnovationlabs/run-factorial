"""`GitLabContractOps` — the GitLab twin of `github.contract.GitHubContractOps`.

Same `actions.registry.ContractGitHub` surface: gate 1's one-time ref surgery against a GitLab
project token. Tags are lightweight (a ref at the commit). Merge requests stand in for PRs.
"""

from __future__ import annotations

from urllib.parse import quote

from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = ["GitLabContractOps"]


class GitLabContractOps:
    """Gate 1's mutations against one GitLab project."""

    def __init__(self, client: GitLabClient, repo: str) -> None:
        self.repo = repo
        self._client = client
        self._pid = quote_project(repo)

    async def tag_sha(self, tag: str) -> str | None:
        try:
            payload = await self._client.get(
                f"/projects/{self._pid}/repository/tags/{quote(tag, safe='')}"
            )
        except GitLabError as exc:
            if exc.status_code == 404:
                return None
            raise
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("id") if isinstance(commit, dict) else None
        return sha if isinstance(sha, str) else None

    async def create_or_move_tag(self, tag: str, sha: str) -> None:
        path = f"/projects/{self._pid}/repository/tags"
        try:
            await self._client.request(
                "POST", path, json={"tag_name": tag, "ref": sha}
            )
            return
        except GitLabError as exc:
            if exc.status_code not in {400, 409}:
                raise
        await self._client.request(
            "DELETE", f"{path}/{quote(tag, safe='')}"
        )
        await self._client.request("POST", path, json={"tag_name": tag, "ref": sha})

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        await self._client.request(
            "POST",
            f"/projects/{self._pid}/merge_requests/{number}/notes",
            json={"body": comment},
        )
        await self._client.request(
            "PUT",
            f"/projects/{self._pid}/merge_requests/{number}",
            json={"state_event": "close"},
        )

    async def delete_branch(self, name: str) -> None:
        await self._client.request(
            "DELETE",
            f"/projects/{self._pid}/repository/branches/{quote(name, safe='')}",
        )

    async def remove_label(self, pr_number: int, label: str) -> None:
        await self._client.request(
            "PUT",
            f"/projects/{self._pid}/merge_requests/{pr_number}",
            json={"remove_labels": label},
        )
