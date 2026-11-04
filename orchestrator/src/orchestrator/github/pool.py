"""One `GitHubClient` per set of GitHub credentials, shared by every project that holds them.

Keyed by `GitHubConnection.fingerprint`, so projects on the same App share its installation
tokens, and a rotated key or token reads as a new connection and gets a new client on its next
use. A client no project resolves to any more is dropped then, with its `RepoContents`.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project
from orchestrator.github.client import GitHubClient
from orchestrator.github.connection import connection_for_project
from orchestrator.github.contents import RepoContents

__all__ = ["GitHubPool", "pool"]


class GitHubPool:
    def __init__(self) -> None:
        self._clients: dict[str, GitHubClient] = {}
        self._fingerprints: dict[int, str] = {}
        self._contents: dict[GitHubClient, RepoContents] = {}

    async def for_project(self, session: AsyncSession, project: Project) -> GitHubClient | None:
        connection = await connection_for_project(session, project)
        if connection is None:
            self._forget(project.id)
            return None
        fingerprint = connection.fingerprint
        if self._fingerprints.get(project.id) != fingerprint:
            self._forget(project.id)
            self._fingerprints[project.id] = fingerprint
        client = self._clients.get(fingerprint)
        if client is None:
            client = self._clients[fingerprint] = GitHubClient(connection)
        return client

    def contents(self, client: GitHubClient) -> RepoContents:
        """The one `RepoContents` over `client`: its memo is what stops six file reads per agent
        turning into six round trips on every tick, and a config sync re-fetching."""
        found = self._contents.get(client)
        if found is None:
            found = self._contents[client] = RepoContents(client)
        return found

    def _forget(self, project_id: int) -> None:
        old = self._fingerprints.pop(project_id, None)
        if old is not None and old not in self._fingerprints.values():
            client = self._clients.pop(old, None)
            if client is not None:
                self._contents.pop(client, None)


@lru_cache(maxsize=1)
def pool() -> GitHubPool:
    return GitHubPool()
