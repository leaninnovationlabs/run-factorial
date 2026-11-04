"""An assistant read-only token, on the GitHub side (design.md §7.18): which of an App's repos
under one owner it may name, and the mint.

A mint names every repository in `repositories` or fails whole, so the request is cut to the
ones the installation holds first: a repo that left the installation must not cost the
assistant the other repos.
"""

from __future__ import annotations

from collections.abc import Collection

from orchestrator.github.client import GitHubClient, ReadToken, split_repo
from orchestrator.github.observer import PER_PAGE, raw_pages

__all__ = ["NoReachableRepo", "installation_repos", "read_token_for"]


class NoReachableRepo(ValueError):
    """None of the factory's repositories is in the App's installation."""


async def installation_repos(client: GitHubClient, repo: str) -> frozenset[str]:
    """Every repository the installation on `repo`'s owner was given, as lower-cased
    `owner/name`."""
    api = await client.api(repo)
    names: set[str] = set()
    async for batch in raw_pages(
        lambda page: api.rest.apps.async_list_repos_accessible_to_installation(
            per_page=PER_PAGE, page=page
        ),
        key="repositories",
    ):
        names.update(str(repo["full_name"]).lower() for repo in batch)
    return frozenset(names)


async def read_token_for(client: GitHubClient, wanted: Collection[str]) -> ReadToken:
    """`wanted` is one owner's repos, since a mint covers one installation: the caller mints per
    App and owner."""
    if not wanted:
        raise NoReachableRepo("no project repository is in the App's installation")
    owners = {split_repo(repo)[0].lower() for repo in wanted}
    if len(owners) != 1:
        raise ValueError(f"a read token covers one owner's repositories; got {sorted(owners)}")
    held = await installation_repos(client, min(wanted))
    first: dict[str, str] = {}
    for repo in sorted(wanted):
        if repo.lower() in held:
            first.setdefault(repo.lower(), repo)
    repos = list(first.values())
    if not repos:
        raise NoReachableRepo("no project repository is in the App's installation")
    return await client.mint_read_token(repos)
