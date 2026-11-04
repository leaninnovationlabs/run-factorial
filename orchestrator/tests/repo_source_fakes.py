"""An in-memory repo for RepoConfigSource: the sample .factorial/ at one sha."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from hashlib import sha1

from orchestrator.github import (
    ContentsNotFound,
    DirEntry,
    MergeProvenance,
    RepoContents,
    TreeEntry,
)
from tests.paths import SAMPLE

REPO = "acme/widgets"
SHA = "a" * 40
HEAD_SHA = "b" * 40
SAMPLE_KEY = "simple"
#: The sample's one lane, under the layout the tooling writes (decision 37).
SAMPLE_PROCESS_PATH = f".factorial/processes/process-{SAMPLE_KEY}.yaml"

FileMap = dict[tuple[str, str, str], str]


def sample_files(*, repo: str = REPO, sha: str = SHA, config_dir: str = ".factorial") -> FileMap:
    """The sample `.factorial/` folder as a repo with one lane, keyed the way `RepoContents` keys
    reads: its agents, skills and MCP servers, and of its lanes only `SAMPLE_KEY`. The repo these
    tests build is a one-lane repo, and a test that wants a second lane adds it; the sample's own
    lanes are held together by the tests that load the folder itself."""
    files: FileMap = {}
    for path in sorted(SAMPLE.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        rel = path.relative_to(SAMPLE).as_posix()
        if rel.startswith("processes/") and rel != f"processes/process-{SAMPLE_KEY}.yaml":
            continue
        files[(repo, f"{config_dir}/{rel}", sha)] = path.read_text(encoding="utf-8")
    return files


class FakeContents(RepoContents):
    """A `RepoContents` with a dict behind it instead of GitHub.

    Subclassed rather than duck-typed so the resolver is exercised through the real type. It
    deliberately keeps the two counts the resolver's cost depends on: `reads` (calls) and
    `fetches` (misses — what a real instance would spend a round trip on).
    """

    #: What the sync guard sees behind every sha: a human-merged pull request unless a test
    #: says otherwise.
    provenance = MergeProvenance(merged_pr=1, merged_by="operator", merged_by_human=True)

    def __init__(self, files: FileMap, *, default_branch: str = "main") -> None:
        self.files = files
        self._branch = default_branch
        self._cache: FileMap = {}
        self._dir_cache: dict[tuple[str, str, str], tuple[DirEntry, ...]] = {}
        self.reads: list[tuple[str, str, str]] = []
        self.fetches: list[tuple[str, str, str]] = []
        self.dir_fetches: list[tuple[str, str, str]] = []
        self.tree_fetches: list[tuple[str, str, str]] = []
        self.default_branch_calls: list[str] = []
        self.resolve_ref_calls: list[tuple[str, str]] = []
        self.inflight = 0
        self.max_inflight = 0

    async def default_branch(self, repo: str) -> str:
        self.default_branch_calls.append(repo)
        return self._branch

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        return self.provenance

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        return ()

    async def resolve_ref(self, repo: str, ref: str) -> str:
        self.resolve_ref_calls.append((repo, ref))
        return ref if len(ref) == 40 else HEAD_SHA

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        key = (repo, path, sha)
        self.reads.append(key)
        if key in self._cache:  # a commit is immutable, so a hit can never be stale
            return self._cache[key]
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(0)  # two yields: a serial caller cannot hold two reads open
        await asyncio.sleep(0)
        self.inflight -= 1
        if key not in self.files:
            raise ContentsNotFound(f"{repo}@{sha[:7]}:{path} not found")
        self.fetches.append(key)
        self._cache[key] = self.files[key]
        return self._cache[key]

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        """Absent-is-empty and memoized, exactly like the real one — which is what makes it
        usable as the "does this folder exist" probe `resolve_dir` needs.

        The resolver still reads the skills a manifest *declares*, never whatever sits in the
        folder, so this is only ever asked about the config root itself.
        """
        key = (repo, path, sha)
        if key in self._dir_cache:
            return self._dir_cache[key]
        self.dir_fetches.append(key)
        prefix = f"{path.rstrip('/')}/"
        entries = tuple(
            DirEntry(name=p.rsplit("/", 1)[-1], path=p, type="file")
            for (r, p, s) in self.files
            if r == repo and s == sha and p.startswith(prefix) and "/" not in p[len(prefix) :]
        )
        self._dir_cache[key] = entries
        return entries

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
        """Recursive, with a content-derived blob sha — the real one's `sha` is git's blob hash,
        so equal bytes must produce equal shas here or a drift comparison proves nothing."""
        self.tree_fetches.append((repo, root, sha))
        prefix = f"{root.strip('/')}/"
        return tuple(
            TreeEntry(path=p, sha=sha1(text.encode()).hexdigest())
            for (r, p, s), text in sorted(self.files.items())
            if r == repo and s == sha and p.startswith(prefix)
        )
