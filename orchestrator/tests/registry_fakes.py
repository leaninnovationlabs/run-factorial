"""The agent-registry test fakes: a repo serving the sample at two shas, and a runtime row."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import Runtime, RuntimeType
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import ContentsNotFound, DirEntry, MergeProvenance, RepoContents
from tests.paths import SAMPLE
from tests.repo_source_fakes import sample_files

REPO = "acme/widgets"
HEAD_SHA = "b" * 40  # what the default branch resolves to
PINNED_SHA = "c" * 40  # an already-established pin
SAMPLE_KEY = "simple"
SAMPLE_TEXT = (SAMPLE / "processes" / f"process-{SAMPLE_KEY}.yaml").read_text(encoding="utf-8")


class FakeContents(RepoContents):
    """The sample config folder, served at both shas, with the ref calls counted.

    Counting matters here: an agent that already has a pin must not consult the default branch,
    or the pin would silently follow `main`.
    """

    #: What the sync guard sees behind every sha: a human-merged pull request unless a test
    #: says otherwise.
    provenance = MergeProvenance(merged_pr=1, merged_by="operator", merged_by_human=True)

    def __init__(self, config_dir: str = ".factorial") -> None:
        self.config_dir = config_dir
        self.files: dict[tuple[str, str, str], str] = {
            **sample_files(repo=REPO, sha=HEAD_SHA, config_dir=config_dir),
            **sample_files(repo=REPO, sha=PINNED_SHA, config_dir=config_dir),
        }
        self.default_branch_calls: list[str] = []
        self.resolve_ref_calls: list[tuple[str, str]] = []
        #: Per-sha overrides of `provenance`, and each commit's parents (none unless named).
        self.provenances: dict[str, MergeProvenance] = {}
        self.parents: dict[str, tuple[str, ...]] = {}

    async def default_branch(self, repo: str) -> str:
        self.default_branch_calls.append(repo)
        return "main"

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        return self.provenances.get(sha, self.provenance)

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        return self.parents.get(sha, ())

    async def resolve_ref(self, repo: str, ref: str) -> str:
        self.resolve_ref_calls.append((repo, ref))
        return ref if len(ref) == 40 else HEAD_SHA

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        try:
            return self.files[(repo, path, sha)]
        except KeyError as exc:  # the real one 404s; `load_processes` probes for the legacy file
            raise ContentsNotFound(f"{repo}:{path}@{sha}") from exc

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        """Absent-is-empty, like the real one — `resolve_dir` probes with it."""
        prefix = f"{path.rstrip('/')}/"
        entries: dict[str, DirEntry] = {}
        for r, p, s in self.files:
            if r != repo or s != sha or not p.startswith(prefix):
                continue
            rest = p[len(prefix) :]
            head = rest.split("/", 1)[0]
            entries[head] = DirEntry(
                name=head,
                path=f"{prefix}{head}",
                type="file" if head == rest else "dir",
            )
        return tuple(entries.values())


def make_source(config_dir: str = ".factorial") -> tuple[RepoConfigSource, FakeContents]:
    contents = FakeContents(config_dir)
    return RepoConfigSource(contents), contents


#: What the sample's `plan-agent` declares. An operator writes this into `project.settings`;
#: matching is exact, which is the whole point (design.md §7.5).
PRODUCT_DOCS_ALLOWED: Final[dict[str, Any]] = {
    "name": "product-docs",
    "transport": "http",
    "url": "https://mcp.internal.example.com/product-docs/mcp",
    "tools": ["search_docs", "get_doc"],
}


async def sample_runtime(session: AsyncSession) -> None:
    """The row the sample's `runtime: claude-api` selects: sync refuses the slug without it."""
    organization_id = await default_organization_id(session)
    existing = await session.execute(
        select(Runtime).where(
            Runtime.organization_id == organization_id, Runtime.slug == "claude-api"
        )
    )
    if existing.scalar_one_or_none():
        return
    session.add(
        Runtime(
            organization_id=organization_id,
            slug="claude-api",
            name="Claude (API key)",
            type=RuntimeType.CLAUDE_API,
        )
    )
    await session.flush()
