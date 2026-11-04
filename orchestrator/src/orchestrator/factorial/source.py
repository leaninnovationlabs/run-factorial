"""A repo's `.factorial/` folder, read at a pinned commit sha (design.md §2.8, data-model.md).

Config lives in `project.github_repo`, never on the orchestrator's disk. The reader is
`github.contents.RepoContents`, or the GitLab equivalent when the ticket source is GitLab.
Everything an agent transitively needs — manifest, both prompts,
every `SKILL.md` — is fetched at the same sha as the manifest; a pin covering only the YAML
would pin nothing.

Reading and parsing only. Choosing a sha and writing the pin back belong to `factorial.registry`.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Sequence
from typing import Final, Protocol

from orchestrator.config import (
    AgentManifest,
    ConfigError,
    ProcessConfig,
    parse_agent,
    parse_mcp_server,
    parse_named_process,
    parse_process,
)
from orchestrator.config.loader import (
    AGENT_FILE,
    AGENTS_DIR,
    MCP_DIR,
    MCP_EXAMPLE_SUFFIX,
    MCP_SUFFIX,
    PROCESS_FILE,
    PROCESS_PREFIX,
    PROCESS_SUFFIX,
    PROCESSES_DIR,
    SKILL_FILE,
    SKILLS_DIR,
    process_file_name,
)
from orchestrator.factorial.types import ResolvedAgent, ResolvedMcpServer, ResolvedSkill
from orchestrator.github import (
    Comparison,
    ContentsNotFound,
    DirEntry,
    MergeProvenance,
    TreeEntry,
)


class ConfigContents(Protocol):
    """What `RepoConfigSource` reads through. `RepoContents` and `GitLabContents` both qualify."""

    async def default_branch(self, repo: str) -> str: ...

    async def resolve_ref(self, repo: str, ref: str) -> str: ...

    async def read_text(self, repo: str, path: str, sha: str) -> str: ...

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]: ...

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]: ...

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance: ...

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]: ...

    async def compare(self, repo: str, base: str, head: str) -> Comparison: ...

__all__ = ["CONFIG_BRANCH", "CONFIG_DIRS", "DEFAULT_CONFIG_DIR", "RepoConfigSource"]

#: The branch a project's config is read from when no ref is named (issue #248). Only the builder's
#: commits and human-merged pull requests reach it, so the default branch never moves config.
CONFIG_BRANCH: Final = "factorial"

#: Tried in order by `resolve_dir`. `.factorial` is the name the factory writes now; `.adlc` is
#: the name every repo onboarded before issue #138 still carries, and a pinned sha carries it
#: for ever. Both stay, because a pin is immutable and there is nothing to migrate it to.
CONFIG_DIRS: tuple[str, ...] = (".factorial", ".adlc")
DEFAULT_CONFIG_DIR = CONFIG_DIRS[0]


class RepoConfigSource:
    """Read-only view of one repo's `.factorial/` at whatever sha the caller hands in.

    Stateless and shareable: it holds no sha of its own, and `RepoContents` memoizes on
    `(repo, path, sha)`, so two agents sharing a skill read that file once per process.
    """

    def __init__(self, contents: ConfigContents, *, config_dir: str = DEFAULT_CONFIG_DIR) -> None:
        self._contents = contents
        self._config_dir = config_dir.strip("/")

    @property
    def config_dir(self) -> str:
        return self._config_dir

    def process_path(self, key: str, *, root: str | None = None) -> str:
        """Where lane `key`'s file lives under the new layout.

        `root` is the folder a given sha actually has — pass `resolve_dir`'s answer when the path
        must resolve at a historical commit, exactly as `agent_manifest_path` does.
        """
        return f"{root or self._config_dir}/{PROCESSES_DIR}/{process_file_name(key)}"

    def agent_manifest_path(self, name: str, *, root: str | None = None) -> str:
        """The conventional `repo_path` for an agent registry row; folder name == agent name.

        `root` is the folder the sha being pinned actually has — pass `resolve_dir`'s answer
        when writing the row, or the stored path names a folder the commit does not contain.
        """
        return f"{root or self._config_dir}/{AGENTS_DIR}/{name}/{AGENT_FILE}"

    async def agent_names(self, repo: str, sha: str) -> tuple[str, ...]:
        """Agent folder names under `agents/` at `sha`. No folder means none, not an error.

        Folder names only: an agent counts as present here even if its manifest is a stub that
        does not parse. Whether a present agent is *usable* is `factorial.probe`'s question, kept
        separate on purpose — this one decides what to write, that one decides what to warn about.

        This and its two siblings below read `self._config_dir` rather than `resolve_dir`, unlike
        every other read on this class. They exist to answer "what would the config commit
        overwrite", and the bundle writes `.factorial/` unconditionally (decision 32 moved the
        writer), so asking about the folder the commit happens to have would let the two
        disagree: a repo still on `.adlc/` would report its agents present and receive a
        `.factorial/` bundle with those agents missing. Reading the folder that is written keeps
        a legacy repo's commit a complete `.factorial/`, which `resolve_dir` then prefers.
        """
        try:
            entries = await self._contents.list_dir(repo, f"{self._config_dir}/{AGENTS_DIR}", sha)
        except ContentsNotFound:
            return ()
        return tuple(sorted(entry.name for entry in entries if entry.type == "dir"))

    async def shared_skill_names(self, repo: str, sha: str) -> tuple[str, ...]:
        """Shared skill folder names under `skills/` at `sha`. No folder means none.

        Folder names only, as `agent_names` does: a name here means the operator owns that skill,
        which is the only question the config builder asks before deciding not to write over it.
        """
        try:
            entries = await self._contents.list_dir(repo, f"{self._config_dir}/{SKILLS_DIR}", sha)
        except ContentsNotFound:
            return ()
        return tuple(sorted(entry.name for entry in entries if entry.type == "dir"))

    async def mcp_server_names(self, repo: str, sha: str) -> tuple[str, ...]:
        """Shared MCP server names under `mcp/` at `sha`, from `<name>.yaml` file stems.

        Files rather than folders, unlike agents and skills: the file stem IS the server name
        and the tool prefix (design.md §7.5). A `<name>.yaml.example` counts too, because this is
        read only to keep the catalog off a file the repo already holds for that name.
        """
        try:
            entries = await self._contents.list_dir(repo, f"{self._config_dir}/{MCP_DIR}", sha)
        except ContentsNotFound:
            return ()
        return tuple(
            sorted(
                {
                    entry.name.removesuffix(MCP_EXAMPLE_SUFFIX).removesuffix(MCP_SUFFIX)
                    for entry in entries
                    if entry.type == "file"
                    and entry.name.endswith((MCP_SUFFIX, MCP_EXAMPLE_SUFFIX))
                }
            )
        )

    # --- listing ----------------------------------------------------------------------------

    async def list_files(self, repo: str, sha: str) -> tuple[TreeEntry, ...]:
        """Every file under the config folder this repo has at `sha`, with its blob sha.

        The folder is whichever of `CONFIG_DIRS` the commit carries, so a repo still on `.adlc/`
        lists its own files rather than an empty `.factorial/`.
        """
        root = await self.resolve_dir(repo, sha)
        return tuple(await self._contents.list_tree(repo, root, sha))

    async def read_file(self, repo: str, path: str, sha: str) -> str:
        """One config file's text at `sha`. The caller proves `path` is a listed member first."""
        return await self._contents.read_text(repo, path, sha)

    # --- refs -------------------------------------------------------------------------------

    async def resolve_dir(self, repo: str, sha: str) -> str:
        """Which config folder this repo has at `sha`.

        `list_dir` answers () for an absent folder instead of raising, so the memo keeps the
        miss too: a repo that has not moved to `.factorial/` yet costs one extra call per
        commit, not one per caller. An explicit `config_dir` is an operator's override and is
        never second-guessed.
        """
        if self._config_dir not in CONFIG_DIRS:
            return self._config_dir
        for candidate in CONFIG_DIRS:
            if await self._contents.list_dir(repo, candidate, sha):
                return candidate
        # Neither exists. Answer with the name a repo should have, so the caller's own read
        # raises a labelled ConfigError naming the file to create — more use than "no folder",
        # and it points an operator at `.factorial/` rather than the name being retired.
        return CONFIG_DIRS[0]

    async def default_head(self, repo: str) -> tuple[str, str]:
        """The default branch's name and its head sha, as the seed would read them."""
        branch = await self._contents.default_branch(repo)
        return branch, await self._contents.resolve_ref(repo, branch)

    async def present_dir(self, repo: str, sha: str) -> str | None:
        """The first of `CONFIG_DIRS` that `sha` has, or None. Unlike `resolve_dir`, absence is an
        answer: the seed copies nothing when neither folder exists."""
        for candidate in CONFIG_DIRS:
            if await self._contents.list_dir(repo, candidate, sha):
                return candidate
        return None

    async def resolve_ref(self, repo: str, ref: str | None) -> str:
        """A branch/tag/sha -> a concrete sha. `None` means the `factorial` config branch, by its
        full ref so a tag of that name cannot stand in for it."""
        if ref is not None:
            return await self._contents.resolve_ref(repo, ref)
        try:
            return await self._contents.resolve_ref(repo, f"heads/{CONFIG_BRANCH}")
        except ContentsNotFound as exc:
            raise ContentsNotFound(
                f"{repo} has no `{CONFIG_BRANCH}` branch, which config is read from. Create it "
                "from the project's Process config page. Factorial refuses a branch created by "
                "hand, because it accepts only a branch it seeded"
            ) from exc

    async def merge_provenance(self, repo: str, sha: str) -> MergeProvenance:
        """The pull request into the config branch that produced `sha`, whatever ref was asked
        for: config reaches the orchestrator only through that branch (the sync guard)."""
        return await self._contents.merge_provenance(repo, sha, CONFIG_BRANCH)

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        return await self._contents.commit_parents(repo, sha)

    async def compare(self, repo: str, base: str, head: str) -> Comparison:
        return await self._contents.compare(repo, base, head)

    # --- process ----------------------------------------------------------------------------

    async def process_names(self, repo: str, sha: str) -> tuple[str, ...]:
        """Lane keys under `processes/` at `sha`, from `process-<key>.yaml` file stems.

        File stems rather than folders, like `mcp_server_names`: the stem minus its prefix IS
        the lane key. Anything else in the folder is refused rather than skipped — a lane an
        operator believes they added must never be silently absent (`loader.load_processes`).

        Empty means the commit is on the legacy layout, or has no config at all; `load_processes`
        is what tells those two apart.
        """
        root = await self.resolve_dir(repo, sha)
        try:
            entries = await self._contents.list_dir(repo, f"{root}/{PROCESSES_DIR}", sha)
        except ContentsNotFound:
            return ()
        keys: list[str] = []
        for entry in entries:
            if (
                entry.type == "file"
                and entry.name.startswith(PROCESS_PREFIX)
                and entry.name.endswith(PROCESS_SUFFIX)
            ):
                keys.append(entry.name.removeprefix(PROCESS_PREFIX).removesuffix(PROCESS_SUFFIX))
                continue
            raise ConfigError(
                _origin(repo, f"{root}/{PROCESSES_DIR}/{entry.name}", sha),
                f"{PROCESSES_DIR}/ holds one process per file, named "
                f"{PROCESS_PREFIX}<name>{PROCESS_SUFFIX}; {entry.name!r} is not one",
            )
        return tuple(sorted(keys))

    async def process_path_at(self, repo: str, key: str, sha: str) -> str:
        """Where lane `key`'s file was at `sha` — the layout that commit had, not today's.

        A version registered under the new layout reads `processes/process-<key>.yaml`; one
        registered before it reads the root `process.yaml`. The row knows which because the row
        knows its own sha, which is why no column records the path.
        """
        root = await self.resolve_dir(repo, sha)
        if key in await self.process_names(repo, sha):
            return self.process_path(key, root=root)
        return f"{root}/{PROCESS_FILE}"

    async def load_processes(self, repo: str, sha: str) -> dict[str, ProcessConfig]:
        """Every lane at `sha`, by key — both layouts, exactly as `loader.load_config_folder`
        reads them on disk, including the refusal of a repo carrying both.

        The legacy root file keeps whatever `name:` it has: `check_process_key` guards a NEW
        identity, and a repo that predates the folder is not asked to rename its lane.
        """
        root = await self.resolve_dir(repo, sha)
        keys = await self.process_names(repo, sha)
        if not keys:
            one = await self.load_process(repo, sha)
            return {one.name: one}

        legacy = f"{root}/{PROCESS_FILE}"
        try:
            await self._contents.read_text(repo, legacy, sha)
        except ContentsNotFound:
            pass
        else:
            raise ConfigError(
                _origin(repo, legacy, sha),
                f"This repository has both layouts: {PROCESSES_DIR}/ holds {', '.join(keys)}, "
                f"and {PROCESS_FILE} is still here. Delete {PROCESS_FILE}. The {PROCESSES_DIR}/ "
                "folder replaces it.",
            )

        texts = await asyncio.gather(
            *(
                self._read(
                    repo,
                    self.process_path(key, root=root),
                    sha,
                    origin=_origin(repo, self.process_path(key, root=root), sha),
                    label="process config",
                )
                for key in keys
            )
        )
        return {
            key: parse_named_process(
                text, key=key, origin=_origin(repo, self.process_path(key, root=root), sha)
            )
            for key, text in zip(keys, texts, strict=True)
        }

    async def load_process(self, repo: str, sha: str) -> ProcessConfig:
        """The legacy root `process.yaml` at `sha`. `load_processes` falls back to it."""
        path = f"{await self.resolve_dir(repo, sha)}/{PROCESS_FILE}"
        origin = _origin(repo, path, sha)
        return parse_process(
            await self._read(repo, path, sha, origin=origin, label="process config"), origin=origin
        )

    async def read_process_text(self, repo: str, path: str, sha: str) -> str:
        """The raw text of one process file at `sha` — for editors that must preserve comments.

        Takes the path rather than deriving it: which file a version came from is a property of
        its own commit, and `process_path_at` is what answers that.
        """
        return await self._read(
            repo, path, sha, origin=_origin(repo, path, sha), label="process config"
        )

    # --- agents -----------------------------------------------------------------------------

    async def load_agent(self, repo: str, manifest_path: str, sha: str) -> ResolvedAgent:
        """Parse the manifest at `manifest_path`, plus its prompts and skills, all at `sha`."""
        path = _manifest_path(manifest_path)
        origin = _origin(repo, path, sha)
        folder = posixpath.dirname(path)

        manifest = parse_agent(
            await self._read(repo, path, sha, origin=origin, label="agent manifest"),
            origin=origin,
        )
        # The folder name is the name `process.yaml` steps reference (`loader.load_config_folder`
        # enforces the same equality on disk); without it `agent: plan-agent` resolves nowhere.
        if manifest.name != posixpath.basename(folder):
            raise ConfigError(
                origin,
                f"manifest name {manifest.name!r} does not match its folder "
                f"{posixpath.basename(folder)!r}",
            )

        # The shared root comes from the manifest's own path, not from this source's configured
        # directory. An agent pinned at an older sha still lives under the folder that commit
        # had, and its shared skills live beside it; resolving the two against different roots
        # looks for a file that pin never contained.
        root = posixpath.dirname(posixpath.dirname(folder))
        skill_paths = self._skill_paths(manifest, folder=folder, root=root)
        mcp_paths = self._mcp_paths(manifest, folder=folder, root=root)
        # 3-6 independent reads, serially 3-6 round trips of pure latency. Each read converts
        # its own 404 into a labelled ConfigError, so a failure still names the file.
        system, instructions, *texts = await asyncio.gather(
            self._read(
                repo,
                f"{folder}/{manifest.prompts.system}",
                sha,
                origin=origin,
                label="prompts.system",
            ),
            self._read(
                repo,
                f"{folder}/{manifest.prompts.instructions}",
                sha,
                origin=origin,
                label="prompts.instructions",
            ),
            *(
                self._read(repo, path_, sha, origin=origin, label=f"{scope} skill {name!r}")
                for name, scope, path_ in skill_paths
            ),
            *(
                self._read(repo, path_, sha, origin=origin, label=f"{scope} mcp server {name!r}")
                for name, scope, path_ in mcp_paths
            ),
        )
        skill_texts = texts[: len(skill_paths)]
        mcp_texts = texts[len(skill_paths) :]

        return ResolvedAgent(
            name=manifest.name,
            repo=repo,
            commit_sha=sha,
            manifest=manifest,
            system_prompt=system,
            instructions=instructions,
            skills=tuple(
                ResolvedSkill(name=name, content=text, scope=scope)
                for (name, scope, _), text in zip(skill_paths, skill_texts, strict=True)
            ),
            mcp_servers=tuple(
                ResolvedMcpServer(
                    name=name,
                    manifest=parse_mcp_server(text, name=name, origin=_origin(repo, path_, sha)),
                    scope=scope,
                )
                for (name, scope, path_), text in zip(mcp_paths, mcp_texts, strict=True)
            ),
        )

    # --- internals --------------------------------------------------------------------------

    def _skill_paths(
        self, manifest: AgentManifest, *, folder: str, root: str
    ) -> tuple[tuple[str, str, str], ...]:
        """`(name, scope, path)` for every declared skill, local first. The manifest model
        already refused a name declared in both scopes."""
        return (
            *(
                (name, "local", f"{folder}/{SKILLS_DIR}/{name}/{SKILL_FILE}")
                for name in manifest.skills.local
            ),
            *(
                (name, "shared", f"{root}/{SKILLS_DIR}/{name}/{SKILL_FILE}")
                for name in manifest.skills.shared
            ),
        )

    def _mcp_paths(
        self, manifest: AgentManifest, *, folder: str, root: str
    ) -> tuple[tuple[str, str, str], ...]:
        """`(name, scope, path)` for every declared MCP server, local first. The manifest model
        already refused a name declared in both scopes."""
        return (
            *(
                (name, "local", f"{folder}/{MCP_DIR}/{name}{MCP_SUFFIX}")
                for name in manifest.mcp.local
            ),
            *(
                (name, "shared", f"{root}/{MCP_DIR}/{name}{MCP_SUFFIX}")
                for name in manifest.mcp.shared
            ),
        )

    async def _read(self, repo: str, path: str, sha: str, *, origin: str, label: str) -> str:
        """Fetch a file that must exist. A missing prompt must never become an empty string —
        an agent running with no system prompt is what pinning exists to prevent."""
        try:
            return await self._contents.read_text(repo, path, sha)
        except ContentsNotFound as exc:
            raise ConfigError(
                origin, f"{label} points at missing file {_origin(repo, path, sha)}"
            ) from exc


def _manifest_path(manifest_path: str) -> str:
    """Normalize a registry row's `repo_path`.

    Operators write the agent folder about as often as the file, so a path not ending in
    `agent.yaml` is treated as the folder rather than 404ing on a path that looks right.
    """
    path = manifest_path.strip().strip("/")
    if posixpath.basename(path) != AGENT_FILE:
        path = f"{path}/{AGENT_FILE}"
    return path


def _origin(repo: str, path: str, sha: str) -> str:
    """`owner/name@<sha7>:<path>` — the convention every layer uses in errors (loader.py)."""
    return f"{repo}@{sha[:7]}:{path}"
