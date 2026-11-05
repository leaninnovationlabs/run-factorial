"""GitHub as a tracker and as a forge. The worker never imports this; the registry does."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import SecretStr

from orchestrator.actions import ContractGitHub
from orchestrator.adapters.display import GITHUB_DISPLAY, HostDisplay
from orchestrator.adapters.split import SourceForge, SourceTracker, views_from_source
from orchestrator.adapters.state import IssueState, StateSource
from orchestrator.github import GitHubProjector
from orchestrator.github.client import GitHubClient
from orchestrator.github.media import media_refs
from orchestrator.github.observer import list_issues

__all__ = ["GitHubBundle", "GitHubForge", "GitHubTracker"]


def _key(key: str) -> int:
    return int(key)


class GitHubBundle(Protocol):
    """What the adapters read off a project's GitHub (`worker.context.ProjectGitHub`), declared
    here so this module does not import the worker."""

    @property
    def client(self) -> GitHubClient: ...
    @property
    def state_source(self) -> Callable[[str, int], StateSource]: ...
    @property
    def projector(self) -> Callable[[str], Any]: ...
    @property
    def media(self) -> Any: ...
    @property
    def trees(self) -> Callable[[str, str], Awaitable[Sequence[Any]]]: ...
    @property
    def compare(self) -> Callable[[str, str, str], Awaitable[Any]]: ...
    @property
    def contract_github(self) -> Callable[[str], ContractGitHub] | None: ...
    @property
    def bot_login(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GitHubTracker:
    github: GitHubBundle
    kind: str = "github"

    @property
    def bot_login(self) -> str:
        return self.github.bot_login

    def observe(self, repo: str, key: str) -> SourceTracker:
        source = self.github.state_source(repo, _key(key))
        tracker, _ = views_from_source(source, bot_login=self.bot_login)
        return tracker

    def writes(self, repo: str) -> GitHubProjector:
        return self.github.projector(repo)  # type: ignore[return-value]

    def media(self, repo: str) -> Any:
        del repo
        return self.github.media

    def media_refs(self, text: str) -> Sequence[Any]:
        return media_refs(text)

    def is_delivery_author(self, login: str) -> bool:
        return bool(self.bot_login) and login.casefold() == self.bot_login.casefold()

    async def candidates(self, repo: str) -> Sequence[IssueState]:
        return await list_issues(self.github.client, repo, state="open", listing="admission")

    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
        return views_from_source(source, bot_login=self.bot_login)


@dataclass(frozen=True, slots=True)
class GitHubForge:
    github: GitHubBundle
    kind: str = "github"
    withheld_secrets: frozenset[str] = frozenset()
    display: HostDisplay = GITHUB_DISPLAY

    @property
    def bot_login(self) -> str:
        return self.github.bot_login

    def full_source(self, repo: str, key: str) -> StateSource:
        return self.github.state_source(repo, _key(key))

    def observe(self, repo: str, key: str) -> SourceForge:
        # A mixed pairing's ticket key belongs to the tracker (PROJ-123). The forge view
        # only needs the repo; issue reads go to the tracker.
        try:
            source = self.full_source(repo, key)
        except ValueError:
            source = self.full_source(repo, "0")
        _, forge = views_from_source(source, bot_login=self.bot_login)
        return forge

    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
        return views_from_source(source, bot_login=self.bot_login)

    def writes(self, repo: str) -> GitHubProjector:
        return self.github.projector(repo)  # type: ignore[return-value]

    def contract(self, repo: str) -> ContractGitHub:
        if self.github.contract_github is None:
            raise RuntimeError("no GitHub action surface is configured")
        return self.github.contract_github(repo)

    def media(self, repo: str) -> Any:
        del repo
        return self.github.media

    def is_delivery_author(self, login: str) -> bool:
        return bool(self.bot_login) and login.casefold() == self.bot_login.casefold()

    def hold_until(self, repo: str) -> datetime | None:
        return self.github.client.backoff_until(repo)

    async def trees(self, repo: str, sha: str) -> Sequence[Any]:
        return await self.github.trees(repo, sha)

    async def compare(self, repo: str, base: str, head: str) -> Any:
        return await self.github.compare(repo, base, head)

    async def listed_issue(
        self, repo: str, key: str, labels: Sequence[str], listings: Any | None
    ) -> IssueState | None:
        if listings is None:
            return None
        try:
            number = _key(key)
        except ValueError:
            return None
        return await listings.issue(self.github.client, repo, number, labels)

    async def grant_run_secrets(
        self,
        repo: str,
        run_id: int,
        *,
        now: datetime,
        permissions: Mapping[str, str],
    ) -> Mapping[str, SecretStr]:
        del run_id, now
        minted = await self.github.client.mint_run_token(repo, permissions)
        granted: dict[str, SecretStr] = {"GH_TOKEN": minted.token}
        if self.bot_login:
            granted["FACTORIAL_BOT_LOGIN"] = SecretStr(self.bot_login)
        return granted

    async def release_run_credential(self, repo: str, run_id: int) -> None:
        del repo, run_id

    async def sweep_run_credentials(self, repo: str, *, active_run_ids: frozenset[int]) -> int:
        del repo, active_run_ids
        return 0
