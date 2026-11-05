"""Per-project tracker and forge ports, and the binding that pairs one of each.

The worker talks to `Bindings`. Registries construct the ports; GitHub's App client and
GitLab's per-project client stay behind the factories (tech-stack.md decision 90).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from orchestrator.actions import ContractGitHub
from orchestrator.adapters.display import HostDisplay
from orchestrator.adapters.split import (
    CompositeStateSource,
    Forge,
    SourceForge,
    SourceTracker,
    Tracker,
)
from orchestrator.adapters.state import IssueState, StateSource

__all__ = [
    "Bindings",
    "ForgePort",
    "MediaFactory",
    "SplitProjector",
    "TrackerPort",
    "Writes",
]


class Writes(Protocol):
    """Labels, assignees, comments, close, and merge — today's `Projector` surface."""

    async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None: ...
    async def close_issue(self, issue_number: int) -> None: ...
    async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None: ...
    async def comment(self, issue_number: int, body: str) -> None: ...
    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Literal["merge", "squash", "rebase"] = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str: ...


MediaFactory = Callable[[str], Any]


class TrackerPort(Protocol):
    """One project's ticket host. Factory: `(session, project) -> TrackerPort | None`."""

    @property
    def kind(self) -> str: ...
    @property
    def bot_login(self) -> str: ...

    def observe(self, repo: str, key: str) -> Tracker: ...
    def writes(self, repo: str) -> Writes: ...
    def media(self, repo: str) -> MediaFactory | None: ...
    def media_refs(self, text: str) -> Sequence[Any]: ...
    def is_delivery_author(self, login: str) -> bool: ...
    async def candidates(self, repo: str) -> Sequence[IssueState]: ...
    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]: ...


class ForgePort(Protocol):
    """One project's code host. Factory: `(session, project) -> ForgePort | None`."""

    @property
    def kind(self) -> str: ...
    @property
    def bot_login(self) -> str: ...
    @property
    def withheld_secrets(self) -> frozenset[str]: ...
    @property
    def display(self) -> HostDisplay: ...

    def full_source(self, repo: str, key: str) -> StateSource: ...
    def observe(self, repo: str, key: str) -> Forge: ...
    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]: ...
    def writes(self, repo: str) -> Writes: ...
    def contract(self, repo: str) -> ContractGitHub: ...
    def media(self, repo: str) -> MediaFactory | None: ...
    def is_delivery_author(self, login: str) -> bool: ...
    def hold_until(self, repo: str) -> datetime | None: ...
    async def trees(self, repo: str, sha: str) -> Sequence[Any]: ...
    async def compare(self, repo: str, base: str, head: str) -> Any: ...
    async def listed_issue(
        self, repo: str, key: str, labels: Sequence[str], listings: Any | None
    ) -> IssueState | None: ...
    async def sweep_run_credentials(
        self, repo: str, *, active_run_ids: frozenset[int]
    ) -> int: ...
    async def grant_run_secrets(
        self,
        repo: str,
        run_id: int,
        *,
        now: datetime,
        permissions: Mapping[str, str],
    ) -> Mapping[str, SecretStr]: ...
    async def release_run_credential(self, repo: str, run_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class SplitProjector:
    """Tracker writes plus the forge's merge. Same surface as `Writes`."""

    tracker: Writes
    forge: Writes

    async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None:
        await self.tracker.set_labels(issue_number, labels)

    async def close_issue(self, issue_number: int) -> None:
        await self.tracker.close_issue(issue_number)

    async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None:
        await self.tracker.set_assignees(issue_number, logins)

    async def comment(self, issue_number: int, body: str) -> None:
        await self.tracker.comment(issue_number, body)

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Literal["merge", "squash", "rebase"] = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str:
        return await self.forge.merge_pr(
            pr_number, merge_method=merge_method, commit_title=commit_title, sha=sha
        )


@dataclass(frozen=True, slots=True)
class Bindings:
    """The tracker and forge one project uses this tick."""

    tracker: TrackerPort
    forge: ForgePort

    @property
    def same_host(self) -> bool:
        return self.tracker.kind == self.forge.kind

    @property
    def bot_login(self) -> str:
        return self.tracker.bot_login or self.forge.bot_login

    def snapshot(self, repo: str, key: str) -> StateSource:
        """One `StateSource` for the predicate library.

        Same-host pairings share one observer so a tick still pays one listing. A mixed
        pairing (Jira plus GitHub) reads each side from its own adapter.
        """
        if self.same_host:
            source = self.forge.full_source(repo, key)
            tracker, _ = self.tracker.views(source)
            _, forge = self.forge.views(source)
            return CompositeStateSource(tracker, forge)
        return CompositeStateSource(
            self.tracker.observe(repo, key),
            self.forge.observe(repo, key),
        )

    def projector(self, repo: str) -> Writes:
        if self.same_host:
            return self.forge.writes(repo)
        return SplitProjector(self.tracker.writes(repo), self.forge.writes(repo))

    def media(self, repo: str) -> MediaFactory | None:
        return self.tracker.media(repo) or self.forge.media(repo)

    def media_refs(self, text: str) -> Sequence[Any]:
        return self.tracker.media_refs(text)
