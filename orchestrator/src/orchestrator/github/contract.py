"""`GitHubContractOps` — the real `actions.registry.ContractGitHub`, backed by githubkit. Gate 1's
contract-fix and a restart's cleanup (decision 67) use it.

A separate mutation surface from `GitHubProjector` (`projector.py`): gate 1's contract-fix is
one-time, irreversible ref surgery (a lightweight tag, a branch delete), not the per-tick
projection that converges.

`create_or_move_tag` writes a LIGHTWEIGHT tag — a ref pointing straight at the commit, never an
annotated tag object, so `tag_sha` reading the ref back gets the commit sha directly.
"""

from __future__ import annotations

from githubkit.exception import RequestFailed

from orchestrator.github.client import GitHubClient, split_repo

__all__ = ["GitHubContractOps"]


class GitHubContractOps:
    """Gate 1's GitHub mutations against one repository, as the App installation."""

    def __init__(self, client: GitHubClient, repo: str) -> None:
        self.repo = repo
        self._client = client
        self._owner, self._name = split_repo(repo)

    async def tag_sha(self, tag: str) -> str | None:
        api = await self._client.api(self.repo)
        try:
            resp = await api.rest.git.async_get_ref(self._owner, self._name, f"tags/{tag}")
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return resp.parsed_data.object_.sha

    async def create_or_move_tag(self, tag: str, sha: str) -> None:
        api = await self._client.api(self.repo)
        ref = f"tags/{tag}"
        try:
            await api.rest.git.async_create_ref(self._owner, self._name, ref=f"refs/{ref}", sha=sha)
        except RequestFailed as exc:
            # 422: the ref already exists — a contract revision re-fires this on a slug already
            # tagged, so create falls back to PATCH.
            if exc.response.status_code != 422:
                raise
            await api.rest.git.async_update_ref(self._owner, self._name, ref, sha=sha, force=True)

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        api = await self._client.api(self.repo)
        await api.rest.issues.async_create_comment(self._owner, self._name, number, body=comment)
        await api.rest.pulls.async_update(self._owner, self._name, number, state="closed")

    async def delete_branch(self, name: str) -> None:
        api = await self._client.api(self.repo)
        try:
            await api.rest.git.async_delete_ref(self._owner, self._name, f"heads/{name}")
        except RequestFailed as exc:
            status = exc.response.status_code
            # GitHub answers a ref that is already gone with 422, not 404.
            gone = status == 404 or (
                status == 422 and "Reference does not exist" in exc.response.text
            )
            if not gone:
                raise

    async def remove_label(self, pr_number: int, label: str) -> None:
        api = await self._client.api(self.repo)
        await api.rest.issues.async_remove_label(self._owner, self._name, pr_number, label)
