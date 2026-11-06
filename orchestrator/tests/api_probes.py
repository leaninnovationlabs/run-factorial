"""The activation-readiness probe fixtures: an in-memory `.factorial/`, a project pinned to it,
and an app whose repo readers answer from it."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

import yaml
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    AccessReader,
    SessionDep,
    access_reader_dependency,
    config_branch_reader_dependency,
    config_source_dependency,
    config_source_optional_dependency,
    label_reader_dependency,
    label_writer_dependency,
    protection_reader_dependency,
    workflows_reader_dependency,
)
from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    Agent,
    ProcessVersion,
    Project,
    TicketSource,
    TicketSourceKind,
)
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import (
    BranchProtection,
    ConfigBranchRules,
    ContentsNotFound,
    DirEntry,
    InstallationAccess,
    MergeProvenance,
    RepoContents,
    TreeEntry,
)
from tests.api_harness import build_app, make_settings, seed_connected, seed_project

MINI_SHA = "d" * 40  # the pinned sha; the fake serves the same bytes for any sha

MINI_PROCESS_YAML = """
specVersion: factorial/v1
kind: Process
name: mini
runtime: claude-api
version: 1
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: mini-agent
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: spec/{ticket}-{slug}
terminal:
  step: done
"""

#: Same shape, second agent step: `other-agent` deliberately has NO files in the fake, so its
#: manifest read 404s — the partial-failure world of the F1 review finding.
MINI_TWO_AGENT_PROCESS_YAML = """
specVersion: factorial/v1
kind: Process
name: mini-two
runtime: claude-api
version: 1
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: mini-agent
    next: build
    workspace:
      repo: "{repo}"
      base: main
      branch: spec/{ticket}-{slug}
  - name: build
    kind: agent
    agent: other-agent
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: impl/{ticket}-{slug}
terminal:
  step: done
"""


def mini_config_files(secret_keys: list[str]) -> dict[str, str]:
    """A one-agent `.factorial/`, keyed by path, declaring exactly `secret_keys`."""
    declarations = "".join(f"  - key: {key}\n" for key in secret_keys)
    manifest = (
        "specVersion: factorial/v1\n"
        "kind: Agent\n"
        "name: mini-agent\n"
        "prompts:\n"
        "  system: system-prompt.md\n"
        "  instructions: instructions.md\n"
        "model: claude-haiku-4-5-20251001\n"
        "limits:\n"
        "  max_duration: 1h\n" + (f"secrets:\n{declarations}" if secret_keys else "") + "runtime:\n"
        "  kind: claude\n"
    )
    return {
        ".factorial/agents/mini-agent/agent.yaml": manifest,
        ".factorial/agents/mini-agent/system-prompt.md": "You are mini.",
        ".factorial/agents/mini-agent/instructions.md": "Do the thing.",
    }


class ProbeContents(RepoContents):
    """In-memory `.factorial/` for the readiness probes: any repo, any sha, same bytes."""

    #: What the sync guard sees behind every sha: a human-merged pull request unless a test
    #: says otherwise.
    provenance = MergeProvenance(merged_pr=1, merged_by="operator", merged_by_human=True)

    def __init__(self, files: dict[str, str], merged: Sequence[str] | None = None) -> None:
        self.files = files
        self.merged = merged

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        try:
            return self.files[path]
        except KeyError as exc:
            raise ContentsNotFound(path) from exc

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        """Immediate children, absent-is-empty like the real one — `resolve_dir` probes with it."""
        prefix = f"{path.rstrip('/')}/"
        names = {rest.split("/", 1)[0] for p in self.files if (rest := p.removeprefix(prefix)) != p}
        return tuple(
            DirEntry(
                name=name,
                path=f"{prefix}{name}",
                type="file" if f"{prefix}{name}" in self.files else "dir",
            )
            for name in sorted(names)
        )

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
        prefix = f"{root.strip('/')}/"
        return tuple(
            TreeEntry(path=path, sha=hashlib.sha1(text.encode()).hexdigest())
            for path, text in sorted(self.files.items())
            if path.startswith(prefix)
        )

    # The readiness probes hand in a pinned sha, but `sync` resolves one first — without these
    # the fake falls through to the real client and asks GitHub for a default branch.
    async def default_branch(self, repo: str) -> str:
        return "main"

    async def resolve_ref(self, repo: str, ref: str) -> str:
        return MINI_SHA

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        if self.merged is not None and sha not in self.merged:
            return MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)
        return self.provenance

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        return ()


async def seed_probe_project(
    session: AsyncSession,
    *,
    process_yaml: str = MINI_PROCESS_YAML,
    agents: tuple[str, ...] = ("mini-agent",),
    unpinned: tuple[str, ...] = (),
    connected: bool = True,
) -> Project:
    """Source + snapshotted process + pinned agent row(s) + a GitHub connection (the caller needs
    the `master_key` fixture unless `connected` is False): zero blockers except whatever the
    secrets probe finds. Names in `unpinned` get a row with no `pinned_commit_sha` — the state
    a registered-but-never-synced agent is in, and the probe's not-pinned degrade branch."""
    project = await (seed_connected(session) if connected else seed_project(session))
    session.add(
        TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITHUB,
            external_ref=project.github_repo,
        )
    )
    config = ProcessConfig.model_validate(yaml.safe_load(process_yaml))
    version = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=config.name,
        content=config.model_dump(mode="json", by_alias=True),
        source_commit_sha=MINI_SHA,
    )
    session.add(version)
    for name in agents:
        session.add(
            Agent(
                project_id=project.id,
                name=name,
                repo_path=f".factorial/agents/{name}/agent.yaml",
                pinned_commit_sha=None if name in unpinned else MINI_SHA,
            )
        )
    await session.flush()
    return project


def lane_files(*keys: str) -> dict[str, str]:
    """A `.factorial/` offering one lane per key, all naming the same agent."""
    files = mini_config_files([])
    for key in keys:
        files[f".factorial/processes/process-{key}.yaml"] = MINI_PROCESS_YAML.replace(
            "name: mini", f"name: {key}"
        )
    return files


def app_access_reader(
    read: Callable[[str], Awaitable[InstallationAccess]],
) -> Callable[[int, AsyncSession], Awaitable[AccessReader]]:
    """An `access_reader_dependency` stand-in: an App connection whose installation answers
    `read` for the project's repo."""

    async def dependency(project_id: int, session: SessionDep) -> AccessReader:
        project = await session.get_one(Project, project_id)

        async def no_token(repo: str) -> int:
            raise AssertionError("an App connection never reads with a token")

        return AccessReader(
            mode="app", repos=(project.github_repo,), installation=read, repo_status=no_token
        )

    return dependency


def probe_app(
    session: AsyncSession,
    *,
    files: dict[str, str] | None = None,
    labels: frozenset[str] | Exception | None = None,
    protection: BranchProtection | Exception | None = None,
    label_writer: list[tuple[str, str, str]] | None = None,
    access: InstallationAccess | Exception | None = None,
    workflows: tuple[str, ...] | Exception | None = None,
    config_branch: ConfigBranchRules | Exception | Literal["missing"] | None = None,
    merged: Sequence[str] | None = None,
) -> FastAPI:
    """`build_app` plus the probe seams: a manifest source over `files`, a label reader that
    answers `labels` (or raises it); `merged` narrows the human-merged shas to those named. Left
    un-overridden, a seam degrades exactly like an unconfigured App — that default is itself
    under test below."""
    app = build_app(make_settings(), session)
    if files is not None:
        source = RepoConfigSource(ProbeContents(files, merged))
        app.dependency_overrides[config_source_optional_dependency] = lambda: source
        app.dependency_overrides[config_source_dependency] = lambda: source
    if labels is not None:

        async def read_labels(repo: str) -> frozenset[str]:
            if isinstance(labels, Exception):
                raise labels
            return labels

        app.dependency_overrides[label_reader_dependency] = lambda: read_labels
    if protection is not None:

        async def read_protection(repo: str) -> BranchProtection:
            if isinstance(protection, Exception):
                raise protection
            return protection

        app.dependency_overrides[protection_reader_dependency] = lambda: read_protection
    if label_writer is not None:

        async def write_label(repo: str, name: str, colour: str, description: str) -> None:
            label_writer.append((name, colour, description))

        app.dependency_overrides[label_writer_dependency] = lambda: write_label
    if access is not None:

        async def read_access(repo: str) -> InstallationAccess:
            if isinstance(access, Exception):
                raise access
            return access

        app.dependency_overrides[access_reader_dependency] = app_access_reader(read_access)
    if workflows is not None:

        async def read_workflows(repo: str) -> tuple[str, ...]:
            if isinstance(workflows, Exception):
                raise workflows
            return workflows

        app.dependency_overrides[workflows_reader_dependency] = lambda: read_workflows
    if config_branch is not None:

        async def read_config_branch(repo: str) -> ConfigBranchRules | None:
            if isinstance(config_branch, Exception):
                raise config_branch
            return None if config_branch == "missing" else config_branch

        app.dependency_overrides[config_branch_reader_dependency] = lambda: read_config_branch
    return app
