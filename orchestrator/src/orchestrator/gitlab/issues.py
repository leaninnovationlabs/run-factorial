"""Opening an issue on GitLab: the IssueWriter the GitHub App path already has."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from orchestrator.github.issues import MarkerUnconfirmed, marker_line
from orchestrator.github.state import IssueState
from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project
from orchestrator.gitlab.observer import repo_issue
from orchestrator.gitlab.readiness import repo_labels

__all__ = ["GitLabIssueWriter"]

_RECOVERY_PAGES = 3


@dataclass(frozen=True, slots=True)
class GitLabIssueWriter:
    """Same surface as `GitHubIssueWriter`, against one project's token."""

    client: GitLabClient

    async def assignable(self, repo: str, login: str) -> bool:
        rows = await self.client.paginate(
            f"/projects/{quote_project(repo)}/members/all", query=login
        )
        return any(
            isinstance(row, dict)
            and str(row.get("username") or "").casefold() == login.casefold()
            and int(row.get("access_level") or 0) >= 10
            for row in rows
        )

    async def labels(self, repo: str) -> frozenset[str]:
        return await repo_labels(self.client, repo)

    async def create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
        assignees: Sequence[str],
    ) -> IssueState:
        assignee_ids = await _assignee_ids(self.client, repo, assignees)
        payload: dict[str, object] = {
            "title": title,
            "description": body,
            "labels": ",".join(labels),
        }
        if assignee_ids:
            payload["assignee_ids"] = assignee_ids
        created = await self.client.request(
            "POST", f"/projects/{quote_project(repo)}/issues", json=payload
        )
        if not isinstance(created, dict) or "iid" not in created:
            raise GitLabError(f"GitLab answered an issue create in {repo} with no issue number")
        return await repo_issue(self.client, repo, int(created["iid"]))

    async def find_marked(
        self, repo: str, marker: str, *, creator: str, since: datetime
    ) -> IssueState | None:
        """Among issues `creator` opened since `since`. Earliest marked body wins."""
        line = marker_line(marker)
        marked: list[IssueState] = []
        capped = False
        for page in range(1, _RECOVERY_PAGES + 1):
            batch = await self.client.get(
                f"/projects/{quote_project(repo)}/issues",
                scope="all",
                author_username=creator,
                created_after=since.isoformat(),
                order_by="created_at",
                sort="asc",
                page=page,
                per_page=100,
            )
            if not isinstance(batch, list):
                break
            for row in batch:
                if not isinstance(row, dict) or "iid" not in row:
                    continue
                body = str(row.get("description") or "")
                if _last_line(body) == line:
                    marked.append(
                        IssueState(
                            number=int(row["iid"]),
                            title=str(row.get("title") or ""),
                            body=body,
                            state="open" if row.get("state") == "opened" else "closed",
                            labels=_label_names(row.get("labels")),
                            assignees=tuple(
                                a["username"]
                                for a in row.get("assignees") or ()
                                if isinstance(a, dict) and isinstance(a.get("username"), str)
                            ),
                        )
                    )
            if len(batch) < 100:
                break
            if page == _RECOVERY_PAGES:
                capped = True
        if marked:
            return min(marked, key=lambda issue: issue.number)
        if capped:
            raise MarkerUnconfirmed(
                f"{repo}: no marked issue in the first {_RECOVERY_PAGES} pages"
            )
        return None


async def _assignee_ids(client: GitLabClient, repo: str, logins: Sequence[str]) -> list[int]:
    ids: list[int] = []
    for login in logins:
        if not login:
            continue
        rows = await client.paginate(f"/projects/{quote_project(repo)}/members/all", query=login)
        member_id = next(
            (
                row.get("id")
                for row in rows
                if isinstance(row, dict)
                and str(row.get("username") or "").casefold() == login.casefold()
                and isinstance(row.get("id"), int)
            ),
            None,
        )
        if isinstance(member_id, int):
            ids.append(member_id)
    return ids


def _last_line(body: str) -> str | None:
    lines = body.rstrip().splitlines()
    return lines[-1] if lines else None


def _label_names(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return frozenset(names)
