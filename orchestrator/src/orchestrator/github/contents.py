"""Reading a target repo's files at a pinned commit sha (data-model: `agent.pinned_commit_sha`).

`process.yaml`, agent manifests, prompts and skills all live in the project's GitHub repo, not on
the orchestrator's disk. A pinned sha only pins *behaviour* if the prompt and skill bodies are
fetched at that same sha, so everything an agent definition transitively needs is read through
here, at one concrete sha.

Unlike `GitHubStateSource` (one instance == one reconciliation tick), one `RepoContents` is
shareable across ticks and tickets: every cache key carries a concrete commit sha, and a commit is
immutable, so a hit can never be stale. The answers that genuinely move, `default_branch`,
`resolve_ref` of a branch or tag, and `merge_provenance` (a merge can land after the read), are
not cached.

This reads through the orchestrator's own installation token, the same credential the observer
uses (design.md §7); nothing here mints or handles a run token.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from githubkit import GitHub, TokenAuthStrategy
from githubkit.exception import RequestFailed

from orchestrator.adapters.state import AsyncKeyedMemo
from orchestrator.github.client import GitHubClient, split_repo
from orchestrator.github.observer import PER_PAGE, raw_pages

if TYPE_CHECKING:
    from githubkit_schemas.latest.models import (
        ContentDirectoryItems,
        ContentFile,
        ContentSubmodule,
        ContentSymlink,
    )

    #: What `repos.async_get_content` can answer with. Narrowed at this boundary and never
    #: exposed: callers get `str` or `Sequence[DirEntry]`.
    Contents = list[ContentDirectoryItems] | ContentFile | ContentSymlink | ContentSubmodule

__all__ = [
    "ComparedCommit",
    "ComparedFile",
    "Comparison",
    "ContentsNotFound",
    "DirEntry",
    "MergeProvenance",
    "RepoContents",
    "RepoNotVisible",
    "TreeEntry",
    "TreeTruncated",
]

#: `GET /repos/{o}/{r}/contents` degrades above this: `encoding: "none"` and an empty `content`.
#: A silently empty prompt is worse than a loud failure, so the degradation is raised.
MAX_FILE_BYTES = 1024 * 1024

_FULL_SHA = re.compile(r"\A[0-9a-fA-F]{40}\Z")


class ContentsNotFound(LookupError):
    """A path does not exist in the repo at that sha."""


class RepoNotVisible(ContentsNotFound):
    """GitHub answered 404 for the repository itself. For a private repo that is also the answer
    when the App's installation does not include it, so retrying cannot help."""


class TreeTruncated(LookupError):
    """The repo's tree exceeded what one `git/trees` call returns, so the listing is partial.

    Raised rather than returned partial: a caller comparing two trees would read a truncated
    side as "these files are gone", which is the one wrong answer worse than no answer.
    """


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: str
    path: str
    type: str  # "file" | "dir"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One blob under a recursive listing, with the content sha that identifies it.

    `sha` is git's blob hash: two paths carry identical bytes exactly when it matches, which is
    what makes comparing a pinned tree against a branch's tree a comparison of shas rather than
    of file bodies.
    """

    path: str  # repo-relative
    sha: str
    #: Bytes, as the listing reports them; None where GitHub leaves it out.
    size: int | None = None


@dataclass(frozen=True, slots=True)
class MergeProvenance:
    """The merged pull request whose merge result is a commit, if one exists, and who merged it."""

    merged_pr: int | None
    merged_by: str | None
    merged_by_human: bool


@dataclass(frozen=True, slots=True)
class ComparedCommit:
    sha: str
    message: str
    #: Whatever the pusher's git config said: an agent sets it freely, so it is a claim.
    author_name: str | None
    authored_at: str | None


@dataclass(frozen=True, slots=True)
class ComparedFile:
    path: str
    status: str  # added | removed | modified | renamed | copied | changed | unchanged
    additions: int
    deletions: int
    previous_path: str | None = None
    #: The file's blob at the head commit; a removed file's is the blob it had.
    blob: str | None = None


#: The most files GitHub's compare lists. It says nothing when it stops there, so a comparison
#: that reaches this count may be missing files.
COMPARE_FILES_MAX: Final = 300


@dataclass(frozen=True, slots=True)
class Comparison:
    """`base...head`: `status` is "ahead" when head descends from base."""

    status: str
    total_commits: int
    commits: tuple[ComparedCommit, ...]
    files: tuple[ComparedFile, ...]

    @property
    def cut_off(self) -> bool:
        return len(self.files) >= COMPARE_FILES_MAX


class RepoContents:
    """Reads files and directories out of GitHub repos. One instance per process is intended."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        # Separate memos: `read_text` and `list_dir` share a key shape but not a value type.
        self._files = AsyncKeyedMemo[tuple[str, str, str], str](self._fetch_text)
        self._dirs = AsyncKeyedMemo[tuple[str, str, str], tuple[DirEntry, ...]](self._fetch_dir)
        self._trees = AsyncKeyedMemo[tuple[str, str], tuple[TreeEntry, ...]](self._fetch_tree)
        self._parents = AsyncKeyedMemo[tuple[str, str], tuple[str, ...]](self._fetch_parents)
        self._compares = AsyncKeyedMemo[tuple[str, str, str], Comparison](self._fetch_compare)

    # --- refs -----------------------------------------------------------------------------------

    async def default_branch(self, repo: str) -> str:
        """Never memoized — a repo's default branch is administrative state that can change."""
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            return (await api.rest.repos.async_get(owner, name)).parsed_data.default_branch
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                raise RepoNotVisible(
                    f"the GitHub App cannot see {repo}: add it to the App's selected repositories"
                ) from exc
            raise

    async def resolve_ref(self, repo: str, ref: str) -> str:
        """A branch name, tag or sha -> the concrete 40-char commit sha it points at.

        Not memoized: a branch moves, and resolving it is the one call whose whole purpose is to
        observe that movement. A ref that is already a full sha short-circuits with no API call.
        """
        if _FULL_SHA.match(ref):
            return ref
        api = await self._api(repo)
        owner, name = split_repo(repo)
        # `GET /commits/{ref}` accepts a branch, a tag or a sha and always answers with the
        # commit — one route instead of branching on what the caller handed us.
        try:
            return (await api.rest.repos.async_get_commit(owner, name, ref)).parsed_data.sha
        except RequestFailed as exc:
            # 422 for a name that is no commit, 404 for one GitHub cannot see: a deleted branch
            # answers one or the other, and a caller should say so rather than fail with a 500.
            if exc.response.status_code in (404, 422):
                raise ContentsNotFound(f"no branch, tag or commit named {ref!r}") from exc
            raise

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        """The merged pull request into `base` whose merge produced `sha`.

        Every closed PR into `base` is listed, because `/commits/{sha}/pulls` answers only open
        PRs for a commit off the default branch. API version 2026-03-10 drops `merge_commit_sha`
        from pull requests, so each merged one's `merged` issue event is read instead: its
        `commit_id` is the result of a merge, a squash or a rebase alike, and its `actor` is who
        merged. Raw JSON, as the observer reads PR listings: one odd payload must not fail it.
        """
        api = await self._api(repo)
        owner, name = split_repo(repo)
        async for batch in raw_pages(
            lambda page: api.rest.pulls.async_list(
                owner,
                name,
                state="closed",
                base=base,
                sort="updated",
                direction="desc",
                per_page=PER_PAGE,
                page=page,
            )
        ):
            for pr in batch:
                if not pr.get("merged_at"):
                    continue
                events = json.loads(
                    (
                        await api.rest.issues.async_list_events(
                            owner, name, pr["number"], per_page=PER_PAGE
                        )
                    ).content
                )
                merged = next(
                    (e for e in events if e.get("event") == "merged" and e.get("commit_id") == sha),
                    None,
                )
                if merged is not None:
                    actor = merged.get("actor") or {}
                    return MergeProvenance(
                        merged_pr=pr["number"],
                        merged_by=actor.get("login"),
                        merged_by_human=actor.get("type") not in (None, "Bot"),
                    )
        return MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        """The parent shas of commit `sha`, first parent first. Raises `ContentsNotFound`."""
        return await self._parents.get((repo, sha))

    async def compare(self, repo: str, base: str, head: str) -> Comparison:
        """Commits and changed files between two full shas. Memoized: two commits never change."""
        return await self._compares.get((repo, base, head))

    # --- contents -------------------------------------------------------------------------------

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        """File contents at a CONCRETE sha. Raises `ContentsNotFound` if absent."""
        return await self._files.get((repo, path, sha))

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        """Immediate children at a CONCRETE sha. Returns () if the directory does not exist.

        Absent-is-empty because callers enumerate optional folders (`agents/<n>/skills/`): a
        missing one means "no skills", not a failure. `read_text` raises instead, because a
        declared prompt that is not there IS a broken manifest.
        """
        return await self._dirs.get((repo, path, sha))

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
        """Every blob under `root` at `sha`, recursively, in one call. Raises `TreeTruncated`.

        One `git/trees?recursive=1` covers the whole commit, so listing a small folder costs the
        same as listing a large one and the memo is keyed on the commit rather than the folder —
        reading `.factorial/` at a pinned sha and at HEAD is two calls however deep either is.
        The alternative, walking with `list_dir`, is one call per directory at each sha.
        """
        prefix = f"{root.strip('/')}/"
        entries = await self._trees.get((repo, sha))
        return tuple(entry for entry in entries if entry.path.startswith(prefix))

    async def tree(self, repo: str, sha: str) -> Sequence[TreeEntry]:
        """Every blob at a CONCRETE sha, with its size: `list_tree`'s listing, unfiltered and from
        the same memo. Raises `TreeTruncated`."""
        return await self._trees.get((repo, sha))

    async def fresh_tree(self, repo: str, sha: str) -> Sequence[TreeEntry]:
        """`tree`, read from GitHub on every call and never kept. The worker reads one whole
        commit per ended run (`worker/outputs.py`); the memo would hold each for the life of the
        process. Raises `TreeTruncated`."""
        return await self._fetch_tree((repo, sha))

    async def read_blob(self, repo: str, sha: str) -> bytes:
        """One blob's bytes by its own sha. Not memoized: a page is read when a reviewer opens
        it, and its ETag already spares the repeat (tech-stack decision 58). Raises
        `ContentsNotFound`; any other failure flies."""
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            blob = (await api.rest.git.async_get_blob(owner, name, sha)).parsed_data
        except RequestFailed as exc:
            if exc.response.status_code in (404, 422):
                raise ContentsNotFound(f"{repo} has no blob {sha[:7]}") from exc
            raise
        if blob.encoding != "base64":
            raise ValueError(f"{repo} blob {sha[:7]}: unexpected encoding {blob.encoding!r}")
        return base64.b64decode(blob.content)

    # --- fetching -------------------------------------------------------------------------------

    async def _api(self, repo: str) -> GitHub[TokenAuthStrategy]:
        return await self._client.api(repo)

    async def _get_content(self, repo: str, path: str, sha: str) -> Contents:
        """One `GET /contents/{path}`; a 404 becomes `ContentsNotFound`, everything else flies."""
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            resp = await api.rest.repos.async_get_content(owner, name, path, ref=sha)
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                raise ContentsNotFound(f"{_where(repo, path, sha)} not found") from exc
            raise
        return resp.parsed_data

    async def _fetch_text(self, key: tuple[str, str, str]) -> str:
        repo, path, sha = key
        payload = await self._get_content(repo, path, sha)
        where = _where(repo, path, sha)

        # Narrow githubkit's four-way union so nothing above sees it. A symlink to an ordinary
        # file in the same repo already comes back as a file; what reaches the other branches is a
        # dangling symlink or a submodule, neither of which has content.
        if isinstance(payload, list):
            raise IsADirectoryError(f"{where} is a directory, not a file")
        if payload.type != "file":
            raise ContentsNotFound(f"{where} is a {payload.type}, not a file")

        if payload.encoding == "none":
            # GitHub's documented degradation above 1 MB: `content` comes back "". Name the file
            # and the limit rather than hand an agent an empty prompt that looks legitimate.
            raise ValueError(
                f"{where} exceeds the {MAX_FILE_BYTES}-byte contents-API limit "
                f"(GitHub returned encoding='none'); .factorial/ files must stay under it"
            )
        if payload.encoding != "base64":
            raise ValueError(f"{where}: unexpected content encoding {payload.encoding!r}")
        # b64decode ignores the newlines GitHub wraps the payload in (validate=False is default).
        return base64.b64decode(payload.content).decode("utf-8")

    async def _fetch_tree(self, key: tuple[str, str]) -> tuple[TreeEntry, ...]:
        repo, sha = key
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            resp = await api.rest.git.async_get_tree(owner, name, sha, recursive="1")
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                raise ContentsNotFound(f"{repo}@{sha[:7]} has no tree") from exc
            raise
        payload = resp.parsed_data
        if payload.truncated:
            raise TreeTruncated(f"{repo}@{sha[:7]}: tree truncated by GitHub")
        # Blobs only. A `tree` entry is a directory, which has no content to compare, and the
        # nesting is recoverable from the paths anyway.
        return tuple(
            TreeEntry(
                path=item.path,
                sha=item.sha,
                size=item.size if isinstance(item.size, int) else None,
            )
            for item in payload.tree
            if item.type == "blob" and item.path and item.sha
        )

    async def _fetch_parents(self, key: tuple[str, str]) -> tuple[str, ...]:
        repo, sha = key
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            commit = (await api.rest.git.async_get_commit(owner, name, sha)).parsed_data
        except RequestFailed as exc:
            if exc.response.status_code in (404, 422):
                raise ContentsNotFound(f"{repo} has no commit {sha[:7]}") from exc
            raise
        return tuple(parent.sha for parent in commit.parents)

    async def _fetch_compare(self, key: tuple[str, str, str]) -> Comparison:
        repo, base, head = key
        api = await self._api(repo)
        owner, name = split_repo(repo)
        try:
            resp = await api.rest.repos.async_compare_commits(owner, name, f"{base}...{head}")
        except RequestFailed as exc:
            if exc.response.status_code in (404, 422):
                raise ContentsNotFound(f"{repo} cannot compare {base[:7]} with {head[:7]}") from exc
            raise
        payload = json.loads(resp.content)
        commits = tuple(
            ComparedCommit(
                sha=c.get("sha", ""),
                message=(c.get("commit") or {}).get("message", ""),
                author_name=((c.get("commit") or {}).get("author") or {}).get("name"),
                authored_at=((c.get("commit") or {}).get("author") or {}).get("date"),
            )
            for c in payload.get("commits") or []
        )
        files = tuple(
            ComparedFile(
                path=f.get("filename", ""),
                status=f.get("status", "changed"),
                additions=int(f.get("additions") or 0),
                deletions=int(f.get("deletions") or 0),
                previous_path=f.get("previous_filename"),
                blob=f.get("sha"),
            )
            for f in payload.get("files") or []
        )
        return Comparison(
            status=str(payload.get("status", "")),
            total_commits=int(payload.get("total_commits") or len(commits)),
            commits=commits,
            files=files,
        )

    async def _fetch_dir(self, key: tuple[str, str, str]) -> tuple[DirEntry, ...]:
        repo, path, sha = key
        try:
            payload = await self._get_content(repo, path, sha)
        except ContentsNotFound:
            return ()

        if not isinstance(payload, list):
            raise NotADirectoryError(
                f"{_where(repo, path, sha)} is a {payload.type}, not a directory"
            )
        return tuple(DirEntry(name=item.name, path=item.path, type=item.type) for item in payload)


def _where(repo: str, path: str, sha: str) -> str:
    """The `owner/name@<sha7>:<path>` origin string every layer above uses in errors."""
    return f"{repo}@{sha[:7]}:{path}"
