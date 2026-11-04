"""A per-run GitLab credential that can act only inside one project.

GitHub mints an installation token per run, scoped to one repository and one permission
set. GitLab has no equivalent mint, so the stored `GITLAB_TOKEN` stays with the
orchestrator and is used only to create a project access token. The agent receives that
token. It can push and open merge requests as a Developer of this project, and of nothing
else. The token is deleted when the run is finished; its expiry is the backstop when a
worker dies first.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project

__all__ = [
    "mint_project_run_token",
    "revoke_project_run_tokens",
    "sweep_project_run_tokens",
]

logger = logging.getLogger(__name__)
_RUN_NAME = re.compile(r"^factorial-run-(\d+)-")

#: Developer. Maintainer is the default GitLab would otherwise grant, and that role can mint
#: further project tokens and change protection.
_DEVELOPER = 30
#: `api` is what `POST /merge_requests` requires. On a project access token it does not extend
#: past that project.
_SCOPES = ["api"]
_MAINTAINER_REQUIRED = (
    "the stored GitLab token cannot mint a project access token. It must be a Maintainer "
    "of this project; each agent run then receives a Developer token for that project alone"
)


def _prefix(run_id: int) -> str:
    return f"factorial-run-{run_id}-"


async def mint_project_run_token(
    client: GitLabClient, repo: str, run_id: int, *, now: datetime
) -> str:
    """Create this run's project token and delete any older token for the same run."""
    project = quote_project(repo)
    try:
        created = await client.request(
            "POST",
            f"/projects/{project}/access_tokens",
            json={
                "name": f"{_prefix(run_id)}{int(now.timestamp())}",
                "scopes": _SCOPES,
                "access_level": _DEVELOPER,
                "expires_at": (now.astimezone(UTC) + timedelta(days=1)).date().isoformat(),
            },
        )
    except GitLabError as exc:
        if exc.status_code in (401, 403):
            raise RuntimeError(_MAINTAINER_REQUIRED) from exc
        raise
    token = created.get("token") if isinstance(created, dict) else None
    token_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(token, str) or not token or not isinstance(token_id, int):
        raise RuntimeError(
            f"GitLab minted a project access token for {repo} without returning its id and secret"
        )
    await _delete_named(client, project, run_id, keep=token_id)
    return token


async def revoke_project_run_tokens(client: GitLabClient, repo: str, run_id: int) -> None:
    """Delete every project token minted for `run_id`. A missing token is already gone."""
    await _delete_named(client, quote_project(repo), run_id, keep=None)


async def sweep_project_run_tokens(
    client: GitLabClient, repo: str, *, active_run_ids: frozenset[int]
) -> int:
    """Revoke run tokens that survived a worker while preserving live and idle sessions."""
    project = quote_project(repo)
    try:
        rows = await client.paginate(f"/projects/{project}/access_tokens")
    except GitLabError:
        logger.exception("could not list project access tokens while sweeping %s", repo)
        return 0
    deleted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, token_id = row.get("name"), row.get("id")
        match = _RUN_NAME.match(name) if isinstance(name, str) else None
        if match is None or not isinstance(token_id, int):
            continue
        if int(match.group(1)) in active_run_ids:
            continue
        await _delete_one(client, project, token_id)
        deleted += 1
    return deleted


async def _delete_named(
    client: GitLabClient, project: str, run_id: int, *, keep: int | None
) -> None:
    prefix = _prefix(run_id)
    try:
        rows = await client.paginate(f"/projects/{project}/access_tokens")
    except GitLabError as exc:
        if exc.status_code == 404:
            return
        if exc.status_code in (401, 403) and keep is not None:
            raise RuntimeError(_MAINTAINER_REQUIRED) from exc
        logger.exception("could not list project access tokens for run %s", run_id)
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("id") == keep:
            continue
        name = row.get("name")
        token_id = row.get("id")
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or not isinstance(token_id, int)
        ):
            continue
        await _delete_one(client, project, token_id)


async def _delete_one(client: GitLabClient, project: str, token_id: int) -> None:
    try:
        await client.request("DELETE", f"/projects/{project}/access_tokens/{token_id}")
    except GitLabError as exc:
        if exc.status_code == 404:
            return
        logger.exception("could not revoke GitLab project access token %s", token_id)
