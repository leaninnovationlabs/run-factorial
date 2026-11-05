"""GitLab as a tracker and as a forge. The worker never imports this; the registry does."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.actions import ContractGitHub
from orchestrator.adapters.display import HostDisplay, display_for
from orchestrator.adapters.split import SourceForge, SourceTracker
from orchestrator.adapters.state import IssueState, StateSource
from orchestrator.db.models import Project
from orchestrator.gitlab import GitLabContractOps, GitLabMedia, GitLabProjector, GitLabStateSource
from orchestrator.gitlab.ca_file import CA_ENV_KEYS, write_run_ca_file
from orchestrator.gitlab.client import GitLabClient
from orchestrator.gitlab.contents import GitLabContents
from orchestrator.gitlab.media import gitlab_media_refs
from orchestrator.gitlab.observer import gitlab_views, list_issues
from orchestrator.gitlab.run_token import (
    mint_project_run_token,
    revoke_project_run_tokens,
    sweep_project_run_tokens,
)
from runtime.gitlab_delivery import gitlab_delivery_author

__all__ = ["GitLabForge", "GitLabTracker", "gitlab_client"]


def _key(key: str) -> int:
    return int(key)


@dataclass(frozen=True, slots=True)
class GitLabTracker:
    client: GitLabClient
    kind: str = "gitlab"

    @property
    def bot_login(self) -> str:
        return self.client.settings.bot_login

    def observe(self, repo: str, key: str) -> SourceTracker:
        source = GitLabStateSource(self.client, repo, _key(key))
        tracker, _ = gitlab_views(source, bot_login=self.bot_login)
        return tracker

    def writes(self, repo: str) -> GitLabProjector:
        return GitLabProjector(self.client, repo)

    def media(self, repo: str) -> Any:
        del repo
        return lambda _repo, _c=self.client: GitLabMedia(_c, _repo)

    def media_refs(self, text: str) -> Sequence[Any]:
        return gitlab_media_refs(text)

    def is_delivery_author(self, login: str) -> bool:
        return gitlab_delivery_author(login, self.bot_login)

    async def candidates(self, repo: str) -> Sequence[IssueState]:
        return await list_issues(self.client, repo, state="opened")

    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
        return gitlab_views(source, bot_login=self.bot_login)


@dataclass(frozen=True, slots=True)
class GitLabForge:
    client: GitLabClient
    kind: str = "gitlab"
    withheld_secrets: frozenset[str] = frozenset({"GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET"})

    @property
    def bot_login(self) -> str:
        return self.client.settings.bot_login

    @property
    def display(self) -> HostDisplay:
        return display_for("gitlab", origin=self.client.settings.base_url or None)

    def full_source(self, repo: str, key: str) -> StateSource:
        return GitLabStateSource(self.client, repo, _key(key))

    def observe(self, repo: str, key: str) -> SourceForge:
        _, forge = gitlab_views(self.full_source(repo, key), bot_login=self.bot_login)
        return forge

    def views(self, source: StateSource) -> tuple[SourceTracker, SourceForge]:
        return gitlab_views(source, bot_login=self.bot_login)

    def writes(self, repo: str) -> GitLabProjector:
        return GitLabProjector(self.client, repo)

    def contract(self, repo: str) -> ContractGitHub:
        return GitLabContractOps(self.client, repo)

    def media(self, repo: str) -> Any:
        return lambda _repo, _c=self.client: GitLabMedia(_c, _repo)

    def is_delivery_author(self, login: str) -> bool:
        return gitlab_delivery_author(login, self.bot_login)

    def hold_until(self, repo: str) -> datetime | None:
        del repo
        return None

    async def trees(self, repo: str, sha: str) -> Sequence[Any]:
        return await GitLabContents(self.client).tree(repo, sha)

    async def compare(self, repo: str, base: str, head: str) -> Any:
        return await GitLabContents(self.client).compare(repo, base, head)

    async def listed_issue(
        self, repo: str, key: str, labels: Sequence[str], listings: Any | None
    ) -> IssueState | None:
        del repo, key, labels, listings
        return None

    async def grant_run_secrets(
        self,
        repo: str,
        run_id: int,
        *,
        now: datetime,
        permissions: Mapping[str, str],
    ) -> Mapping[str, SecretStr]:
        del permissions
        minted = SecretStr(await mint_project_run_token(self.client, repo, run_id, now=now))
        granted: dict[str, SecretStr] = {"GH_TOKEN": minted, "GITLAB_TOKEN": minted}
        host = self.client.settings.host
        if host:
            granted["GIT_HTTP_HOST"] = SecretStr(host)
        if self.bot_login:
            granted["FACTORIAL_BOT_LOGIN"] = SecretStr(self.bot_login)
        ca_name = f"factorial-gitlab-ca-{repo.replace('/', '-')}.pem"
        ca_path = write_run_ca_file(self.client.settings, Path(gettempdir()) / ca_name)
        if ca_path is not None:
            located = SecretStr(str(ca_path))
            for env_key in CA_ENV_KEYS:
                granted[env_key] = located
        return granted

    async def release_run_credential(self, repo: str, run_id: int) -> None:
        await revoke_project_run_tokens(self.client, repo, run_id)

    async def sweep_run_credentials(self, repo: str, *, active_run_ids: frozenset[int]) -> int:
        return await sweep_project_run_tokens(self.client, repo, active_run_ids=active_run_ids)


async def gitlab_client(session: AsyncSession, project: Project) -> GitLabClient | None:
    """The project's own GitLab client; None without its credentials or on a refused host."""
    # Looked up per call, not at import, so a stand-in for `client_for_project` always applies.
    from orchestrator.gitlab import project_client

    return await project_client.client_for_project(session, project)
