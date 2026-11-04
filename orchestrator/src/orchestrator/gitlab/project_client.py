"""Per-project GitLab credentials.

The UI stores these. The global FACTORIAL_GITLAB__* token and webhook secret are a fallback only
for a project on the deployment's own GitLab host, and never under multi-org.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import weakref
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project, TicketSource, TicketSourceKind
from orchestrator.factorial.source import RepoConfigSource
from orchestrator.gitlab.client import GitLabClient
from orchestrator.gitlab.contents import GitLabContents
from orchestrator.gitlab.hosts import (
    HostRefused,
    check_base_url,
    uses_deployment_credentials,
)
from orchestrator.secrets import store
from orchestrator.settings import GitLabSettings, Settings, get_settings

__all__ = [
    "TOKEN_SECRET",
    "WEBHOOK_SECRET_NAME",
    "client_for_project",
    "config_source_for",
    "gitlab_bag",
    "settings_for_project",
    "webhook_secret_for",
]

log = logging.getLogger(__name__)

TOKEN_SECRET = "GITLAB_TOKEN"
WEBHOOK_SECRET_NAME = "GITLAB_WEBHOOK_SECRET"
CA_SECRET = "GITLAB_CA_BUNDLE"


def gitlab_bag(project: Project) -> dict[str, Any]:
    settings = project.settings if isinstance(project.settings, dict) else {}
    bag = settings.get("gitlab")
    return bag if isinstance(bag, dict) else {}


def _ca_path(raw: str) -> Path | None:
    """`~` expands from the environment alone; nothing here touches the disk."""
    return Path(raw).expanduser() if raw else None


def _deployment_credentials(base: str, settings: Settings) -> bool:
    return uses_deployment_credentials(
        base, operator_base=settings.gitlab.base_url, multi_org=settings.multi_org.enabled
    )


def _pasted_pem(raw: object) -> str:
    text = str(raw or "").strip()
    return text if text.startswith("-----BEGIN") else ""


async def webhook_secret_for(session: AsyncSession, project: Project) -> str:
    """The project's own GitLab webhook secret, or the deployment's where it may use it."""
    row = await store.get(session, project.id, WEBHOOK_SECRET_NAME)
    if row is not None and row.get_secret_value():
        return row.get_secret_value()
    settings = get_settings()
    base = str(gitlab_bag(project).get("base_url") or settings.gitlab.base_url or "").strip()
    if _deployment_credentials(base, settings):
        return settings.gitlab.webhook_secret.get_secret_value()
    return ""


async def settings_for_project(session: AsyncSession, project: Project) -> GitLabSettings | None:
    """None when the project has no GitLab URL or token, or names a host it may not (`hosts`);
    `HostLookupFailed` when that host's lookup did not answer. The deployment's token serves
    only a project on the deployment's own host, and none at all under multi-org. The webhook
    secret is not filled in: only `webhook_secret_for` reads it."""
    bag = gitlab_bag(project)
    settings = get_settings()
    operator = settings.gitlab
    own_base = str(bag.get("base_url") or "").strip()
    base = own_base or operator.base_url.strip()
    if not base:
        return None
    if own_base:
        try:
            await check_base_url(
                own_base, operator_base=operator.base_url, multi_org=settings.multi_org.enabled
            )
        except HostRefused as exc:
            log.warning("project %s names a GitLab host it may not use: %s", project.id, exc)
            return None
    found = {
        name: value.get_secret_value()
        for name, value in (
            await store.get_many(session, project.id, (TOKEN_SECRET, CA_SECRET))
        ).items()
    }
    token = found.get(TOKEN_SECRET) or (
        operator.token.get_secret_value() if _deployment_credentials(base, settings) else ""
    )
    if not token:
        return None
    pem = (
        found.get(CA_SECRET)
        or str(bag.get("ca_pem") or "")
        or _pasted_pem(bag.get("ca_bundle"))
        or _pasted_pem(operator.ca_bundle)
    )
    ca_path = "" if _pasted_pem(operator.ca_bundle) else str(operator.ca_bundle or "").strip()
    own_ca = str(bag.get("ca_bundle") or "").strip()
    # Under multi-org a project's path is never read (`hosts.check_project_gitlab`).
    if own_ca and not _pasted_pem(own_ca) and not settings.multi_org.enabled:
        ca_path = own_ca
    return GitLabSettings(
        base_url=base,
        token=SecretStr(token),
        ca_bundle=_ca_path(ca_path),
        ca_pem=pem,
        bot_login=str(bag.get("bot_login") or operator.bot_login or ""),
    )


class _GitLabPool:
    """One `GitLabClient` per set of GitLab settings, as `github.pool` keeps one per connection.

    A client per call built an `httpx.AsyncClient`, and with a CA an `SSLContext`, for every
    ticket on every tick and every API read, and none was ever closed: sockets and memory grew
    in the long-running worker, and each request paid a fresh TLS handshake. A changed token,
    host or CA is a new fingerprint and a new client; the old one is closed once no project
    resolves to it, after the requests it has in flight (`GitLabClient.aclose`).
    """

    def __init__(self) -> None:
        self._clients: dict[str, GitLabClient] = {}
        self._fingerprints: dict[int, str] = {}

    async def client(self, project_id: int, settings: GitLabSettings) -> GitLabClient:
        fingerprint = _fingerprint(settings)
        if self._fingerprints.get(project_id) != fingerprint:
            await self.forget(project_id)
            self._fingerprints[project_id] = fingerprint
        client = self._clients.get(fingerprint)
        if client is None:
            client = self._clients[fingerprint] = GitLabClient(settings)
        return client

    async def forget(self, project_id: int) -> None:
        old = self._fingerprints.pop(project_id, None)
        if old is not None and old not in self._fingerprints.values():
            replaced = self._clients.pop(old, None)
            if replaced is not None:
                await replaced.aclose()


def _fingerprint(settings: GitLabSettings) -> str:
    parts = (
        settings.base_url,
        settings.token.get_secret_value(),
        str(settings.ca_bundle or ""),
        settings.ca_pem or "",
        settings.bot_login,
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


#: Per event loop, because an `httpx.AsyncClient`'s pooled connections belong to the loop that
#: opened them.
_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _GitLabPool] = (
    weakref.WeakKeyDictionary()
)


def _pool() -> _GitLabPool:
    loop = asyncio.get_running_loop()
    found = _pools.get(loop)
    if found is None:
        found = _pools[loop] = _GitLabPool()
    return found


async def client_for_project(session: AsyncSession, project: Project) -> GitLabClient | None:
    resolved = await settings_for_project(session, project)
    if resolved is None:
        await _pool().forget(project.id)
        return None
    return await _pool().client(project.id, resolved)


async def config_source_for(
    session: AsyncSession,
    project: Project,
    *,
    github: RepoConfigSource | None,
    gitlab: Callable[[Project], Awaitable[GitLabClient | None]] | None = None,
) -> RepoConfigSource | None:
    """The reader for `.factorial/` on this project.

    A GitLab ticket source never falls back to GitHub: that would make process sync demand a
    GitHub connection for a project whose issues live on GitLab.
    `None` means the chosen forge is not configured: GitLab has no URL or `GITLAB_TOKEN`,
    or this is a GitHub source and `github` itself is None. `gitlab` stands in for
    `client_for_project` where the caller may not open the stored token.
    """
    row = (
        await session.execute(select(TicketSource).where(TicketSource.project_id == project.id))
    ).scalar_one_or_none()
    if row is not None and row.kind == TicketSourceKind.GITLAB:
        resolve = gitlab or partial(client_for_project, session)
        client = await resolve(project)
        if client is None:
            return None
        return RepoConfigSource(GitLabContents(client))
    return github
