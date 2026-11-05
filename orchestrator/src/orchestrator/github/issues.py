"""Opening an issue as the App: the one GitHub write the MCP server adds (design.md §7.16).

`issues: write` is already in `APP_PERMISSIONS`. Agents act as the same App, so the creator of
an issue never proves the orchestrator opened it. The marker in the body does that
(`api/services/intake.py`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from githubkit.exception import RequestFailed

from orchestrator.adapters.state import IssueState
from orchestrator.github.client import GitHubClient, split_repo
from orchestrator.github.observer import _issue_from_raw, capped_issue_listing, repo_labels

__all__ = ["GitHubIssueWriter", "IssueWriter", "MarkerUnconfirmed", "marker_line"]

#: Recovery looks back a few minutes, so three pages of the App's issues touched since then is
#: far more than the window holds.
_RECOVERY_PAGES: Final = 3


class MarkerUnconfirmed(Exception):
    """The recovery listing hit its page cap before any marked issue, so whether one exists is
    unknown."""


def marker_line(marker: str) -> str:
    """The HTML comment that carries `marker` as an issue body's last line: hidden on the
    rendered issue, and exact to compare."""
    return f"<!-- {marker} -->"


class IssueWriter(Protocol):
    async def assignable(self, repo: str, login: str) -> bool: ...

    async def labels(self, repo: str) -> frozenset[str]: ...

    async def create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
        assignees: Sequence[str],
    ) -> IssueState: ...

    async def find_marked(
        self, repo: str, marker: str, *, creator: str, since: datetime
    ) -> IssueState | None:
        """The earliest issue whose body ends with `marker`'s line, or None. Raises
        `MarkerUnconfirmed` when the search could not read far enough to say."""
        ...


@dataclass(frozen=True, slots=True)
class GitHubIssueWriter:
    client: GitHubClient

    async def assignable(self, repo: str, login: str) -> bool:
        """Checked before creating, because GitHub silently drops an assignee it cannot assign
        and the issue would then wait for ever on `ticket_has_assignee()`."""
        owner, name = split_repo(repo)
        api = await self.client.api(repo)
        try:
            await api.rest.issues.async_check_user_can_be_assigned(owner, name, login)
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return False
            raise
        return True

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
        owner, name = split_repo(repo)
        api = await self.client.api(repo)
        response = await api.rest.issues.async_create(
            owner, name, title=title, body=body, labels=list(labels), assignees=list(assignees)
        )
        issue = _issue_from_raw(json.loads(response.content), repo=repo, listing="issue-create")
        if issue is None:
            raise RuntimeError(f"GitHub answered an issue create in {repo} with no issue number")
        return issue

    async def find_marked(
        self, repo: str, marker: str, *, creator: str, since: datetime
    ) -> IssueState | None:
        """Among the issues `creator` touched since `since`. The earliest marked one is the
        original, because a copy of the marker line can only post-date it. Read oldest first, so
        a match before the page cap is still the earliest; no match before it is unknown."""
        line = marker_line(marker)
        issues, capped = await capped_issue_listing(
            self.client,
            repo,
            state="all",
            listing="create-recovery",
            creator=creator,
            since=since,
            oldest_first=True,
            max_pages=_RECOVERY_PAGES,
        )
        marked = [issue for issue in issues if _last_line(issue.body) == line]
        if marked:
            return min(marked, key=lambda issue: issue.number)
        if capped:
            raise MarkerUnconfirmed(f"{repo}: no marked issue in the first {_RECOVERY_PAGES} pages")
        return None


def _last_line(body: str) -> str | None:
    lines = body.rstrip().splitlines()
    return lines[-1] if lines else None
