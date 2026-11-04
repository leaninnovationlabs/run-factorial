"""Write labels, comments and merge-request mutations to GitLab.

Label vocabulary is the same `factorial:` set the GitHub projector uses, so a process.yaml
does not change when the forge does.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = ["GitLabProjector"]


class GitLabProjector:
    def __init__(self, client: GitLabClient, repo: str) -> None:
        self._client = client
        self._pid = quote_project(repo)

    async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None:
        await self._client.request(
            "PUT",
            f"/projects/{self._pid}/issues/{issue_number}",
            json={"labels": ",".join(labels)},
        )

    async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None:
        usernames = [login for login in logins if login]
        ids: list[int] = []
        for username in usernames:
            rows = await self._client.paginate(
                f"/projects/{self._pid}/members/all", query=username
            )
            member_id = next(
                (
                    row.get("id")
                    for row in rows
                    if isinstance(row, dict)
                    and str(row.get("username") or "").casefold() == username.casefold()
                    and isinstance(row.get("id"), int)
                ),
                None,
            )
            if isinstance(member_id, int):
                ids.append(member_id)
            else:
                raise GitLabError(
                    f"cannot assign issue {issue_number}: {username!r} is not a project member"
                )
        await self._client.request(
            "PUT",
            f"/projects/{self._pid}/issues/{issue_number}",
            json={"assignee_ids": ids},
        )

    async def comment(self, issue_number: int, body: str) -> None:
        await self._client.request(
            "POST",
            f"/projects/{self._pid}/issues/{issue_number}/notes",
            json={"body": body},
        )

    async def close_issue(self, issue_number: int) -> None:
        await self._client.request(
            "PUT",
            f"/projects/{self._pid}/issues/{issue_number}",
            json={"state_event": "close"},
        )

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Literal["merge", "squash", "rebase"] = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str:
        if merge_method == "rebase":
            await self._client.request(
                "PUT",
                f"/projects/{self._pid}/merge_requests/{pr_number}/rebase",
            )
        payload: dict[str, object] = {"squash": merge_method == "squash"}
        if commit_title:
            key = "squash_commit_message" if merge_method == "squash" else "merge_commit_message"
            payload[key] = commit_title
        if sha:
            payload["sha"] = sha
        result = await self._client.request(
            "PUT",
            f"/projects/{self._pid}/merge_requests/{pr_number}/merge",
            json=payload,
        )
        if isinstance(result, dict) and isinstance(result.get("merge_commit_sha"), str):
            return result["merge_commit_sha"]
        if isinstance(result, dict) and isinstance(result.get("sha"), str):
            return result["sha"]
        return sha or ""
