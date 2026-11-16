"""Process builder delivery: the App commits the operator's config to `factorial` and syncs that
commit in the same request (design.md §7.7).

The GitHub API and the repo both sit behind fakes; `commit_config_files` itself runs. What these
tests prove is our half of the contract: the App refuses to act unconfigured, refuses a selection
its own freshly computed proposal does not contain, refuses a bundle the loader would reject or a
runtime the registry cannot honour before anything is written, rewrites a file the repo already
has only as the operator's edit of the branch's own copy and never from the catalog, records the
commit before the branch moves, and syncs exactly that commit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha1
from itertools import pairwise
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from githubkit.exception import RequestFailed
from githubkit.response import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    ConfigCommitWriter,
    config_branch_seeder_dependency,
    config_commit_writer_dependency,
    config_source_dependency,
)
from orchestrator.api.services import adoption
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.config import (
    ConfigDraft,
    PlannedWrite,
    check_config,
    sync_config,
)
from orchestrator.api.services.errors import Conflict, Forbidden
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.config import parse_process
from orchestrator.config.process_templates import PROCESS_TEMPLATES
from orchestrator.config.scaffold.assemble import FileEdit
from orchestrator.config.scaffold.propose import Proposal, propose
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    Runtime,
    RuntimeType,
    TicketSource,
    TicketSourceKind,
)
from orchestrator.events import EventType, append
from orchestrator.events.types import RequestedVia
from orchestrator.factorial import CONFIG_BRANCH, CONFIG_DIRS, RepoConfigSource
from orchestrator.factorial.registry import WALK_LIMIT
from orchestrator.github import (
    ComparedCommit,
    ComparedFile,
    Comparison,
    ContentsNotFound,
    DirEntry,
    MergeProvenance,
    RepoContents,
    TreeEntry,
    commit_config_files,
    seed_config_branch,
)
from orchestrator.settings import AuthSettings, Settings
from tests.api_harness import (
    TEST_RUNTIME,
    TOKEN,
    agent_files,
    agent_manifest,
    authed_client_for,
    build_app,
    client_for,
    ensure_runtime,
    make_settings,
)
from tests.members import ensure_member
from tests.paths import SAMPLE
from tests.scaffold_lanes import STACKED, UNSTACKED

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

BASE_SHA = "a" * 40
NEW_SHA = "c" * 40


def blob(text: str) -> str:
    """Git's blob hash, which is what `list_tree` returns and what the no-op check compares."""
    data = text.encode()
    return sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeContents(RepoContents):
    """`factorial` at `head`, one file map per sha. No commit was merged by a human, so only a
    recorded event can vouch for one."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files: dict[str, dict[str, str]] = {BASE_SHA: dict(files)}
        self.head = BASE_SHA
        self.parents: dict[str, tuple[str, ...]] = {}
        #: Listed paths whose blob is not UTF-8, which `GitHubContents.read_text` cannot decode.
        self.binary: set[str] = set()
        self.compares: list[tuple[str, str]] = []

    async def default_branch(self, repo: str) -> str:
        return "main"

    async def resolve_ref(self, repo: str, ref: str) -> str:
        return self.head if ref == f"heads/{CONFIG_BRANCH}" else ref

    async def merge_provenance(self, repo: str, sha: str, base: str) -> MergeProvenance:
        return MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)

    async def commit_parents(self, repo: str, sha: str) -> tuple[str, ...]:
        return self.parents.get(sha, ())

    async def compare(self, repo: str, base: str, head: str) -> Comparison:
        self.compares.append((base, head))
        return Comparison(
            status="ahead",
            total_commits=1,
            commits=(
                ComparedCommit(
                    sha=head,
                    message="Tighten the spec",
                    author_name="operator",
                    authored_at="2026-10-13T10:00:00Z",
                ),
            ),
            files=(
                ComparedFile(
                    path=".factorial/processes/process-p.yaml",
                    status="modified",
                    additions=3,
                    deletions=1,
                ),
            ),
        )

    async def read_text(self, repo: str, path: str, sha: str) -> str:
        if path in self.binary:
            raise UnicodeDecodeError("utf-8", b"\x89PNG", 0, 1, "invalid start byte")
        try:
            return self.files[sha][path]
        except KeyError as exc:
            raise ContentsNotFound(f"{repo}:{path}@{sha}") from exc

    async def list_dir(self, repo: str, path: str, sha: str) -> Sequence[DirEntry]:
        prefix = f"{path.rstrip('/')}/"
        entries: dict[str, DirEntry] = {}
        for p in self.files.get(sha, {}):
            if p.startswith(prefix):
                rest = p[len(prefix) :]
                head = rest.split("/", 1)[0]
                kind = "file" if head == rest else "dir"
                entries[head] = DirEntry(name=head, path=f"{prefix}{head}", type=kind)
        return tuple(entries.values())

    async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
        prefix = f"{root.strip('/')}/"
        return tuple(
            TreeEntry(path=p, sha=blob(text))
            for p, text in sorted(self.files.get(sha, {}).items())
            if p.startswith(prefix)
        )


def _refused(status: int, message: str = "") -> RequestFailed:
    request = httpx.Request("PATCH", "https://api.github.com/repos/o/r/git/refs/heads/factorial")
    raw = httpx.Response(status, json={"message": message}, request=request)
    return RequestFailed(Response(raw, dict))


class FakeGitApi:
    """The git calls `commit_config_files` makes, recorded. Moving the ref lands the commit in
    `contents`, unless `moved` (someone else moved the branch) or `sync_breaks` (the committed
    tree cannot be read back)."""

    def __init__(
        self, contents: FakeContents, *, moved: bool = False, sync_breaks: bool = False
    ) -> None:
        self.contents = contents
        self.moved = moved
        self.sync_breaks = sync_breaks
        self.trees: list[list[dict[str, str | None]]] = []
        self.commits: list[dict[str, Any]] = []
        self.refs: list[tuple[str, str, bool]] = []
        self.order: list[str] = []
        self.git = SimpleNamespace(
            async_get_commit=self._get_commit,
            async_create_tree=self._create_tree,
            async_create_commit=self._create_commit,
            async_update_ref=self._update_ref,
        )

    @property
    def written(self) -> dict[str, str]:
        (tree,) = self.trees
        return {
            str(entry["path"]): str(entry["content"]) for entry in tree if "content" in entry
        }

    @property
    def removed(self) -> list[str]:
        """Paths the tree deletes: an entry with a null sha and no content."""
        (tree,) = self.trees
        return [str(entry["path"]) for entry in tree if entry.get("sha", "") is None]

    async def _get_commit(self, owner: str, name: str, sha: str) -> Any:
        return SimpleNamespace(parsed_data=SimpleNamespace(tree=SimpleNamespace(sha="0" * 40)))

    async def _create_tree(self, owner: str, name: str, **kwargs: Any) -> Any:
        self.trees.append(kwargs["tree"])
        return SimpleNamespace(parsed_data=SimpleNamespace(sha="t" * 40))

    async def _create_commit(self, owner: str, name: str, **kwargs: Any) -> Any:
        self.commits.append(kwargs)
        return SimpleNamespace(parsed_data=SimpleNamespace(sha=NEW_SHA))

    async def _update_ref(self, owner: str, name: str, ref: str, *, sha: str, force: bool) -> Any:
        self.order.append("ref")
        if self.moved:
            raise _refused(422, "Update is not a fast forward")
        self.refs.append((ref, sha, force))
        if not self.sync_breaks:
            kept = {
                path: text
                for path, text in self.contents.files[BASE_SHA].items()
                if path not in self.removed
            }
            self.contents.files[sha] = {**kept, **self.written}
        self.contents.head = sha
        return SimpleNamespace(parsed_data=None)


async def _async(value: Any) -> Any:
    return value


def writer_for(api: FakeGitApi) -> ConfigCommitWriter:
    client = SimpleNamespace(api=lambda _repo: _async(SimpleNamespace(rest=api)))

    async def write(
        repo: str,
        branch: str,
        base_sha: str,
        files: Mapping[str, str | None],
        message: str,
        record: Callable[[str], Awaitable[None]],
    ) -> str:
        async def traced(sha: str) -> None:
            await record(sha)
            api.order.append("record")

        return await commit_config_files(client, repo, branch, base_sha, files, message, traced)  # type: ignore[arg-type]

    return write


#: The wizard's plans are all `name: p`, so this is where their process file lands.
PROCESS_PATH = ".factorial/processes/process-p.yaml"


def lane(name: str = "p", runtime: str | None = TEST_RUNTIME) -> str:
    """The two-agent stacked lane, declaring `name` and naming `runtime` unless it is None."""
    text = STACKED.replace("\nname: sample-lane\n", f"\nname: {name}\n")
    if runtime is None:
        return text
    return text.replace("kind: Process\n", f"kind: Process\nruntime: {runtime}\n")


async def project_in(session: AsyncSession, *, seeded: bool = True) -> Project:
    """`seeded` records the orchestrator seeding `factorial` at `BASE_SHA`, the head the builder
    then accepts."""
    # Unique: the routes commit, so the project outlives the test.
    project = Project(
        organization_id=await default_organization_id(session),
        name=f"p-{uuid4().hex[:8]}",
        github_repo="o/r",
    )
    session.add(project)
    await ensure_runtime(session)
    await session.flush()
    if seeded:
        await append(
            session,
            type=EventType.CONFIG_BRANCH_CREATED,
            project_id=project.id,
            branch=CONFIG_BRANCH,
            sha=BASE_SHA,
        )
    return project


def _proposal_for(
    content: str,
    *,
    present_agents: Sequence[str] = (),
    present_skills: Sequence[str] = (),
) -> Proposal:
    config = parse_process(content, origin="test")
    return propose(
        config, present_agents=present_agents, present_skills=present_skills, present_mcp=()
    )


def _selected_files(
    proposal: Proposal, *, only: Sequence[str] | None = None
) -> list[dict[str, str]]:
    """Every file of every ticked-by-default, not-locked variant — what the wizard opens on.
    `only` narrows it to named components, mirroring an operator unticking everything else."""
    wanted = {
        path
        for component in proposal.components
        for offer in component.variants
        if offer.offered and offer.default_on and not component.present_in_repo
        if only is None or component.name in only
        for path in offer.paths
    }
    return [{"path": path, "body": proposal.files[path].body} for path in sorted(wanted)]


def payload(
    *,
    files: list[dict[str, str]] | None = None,
    present_agents: Sequence[str] = (),
    present_skills: Sequence[str] = (),
    only: Sequence[str] | None = None,
    runtime: str = TEST_RUNTIME,
) -> dict[str, object]:
    """The editor's YAML, plus the files it has ticked by default. The `present_*` args must
    agree with the repo `app_with` was built with, so this computes the proposal the route will."""
    content = lane(runtime=runtime)
    if files is None:
        proposal = _proposal_for(
            content, present_agents=present_agents, present_skills=present_skills
        )
        files = _selected_files(proposal, only=only)
    return {"content": content, "files": files}


def app_with(
    session: AsyncSession,
    *,
    present: Sequence[str] = (),
    present_skills: Sequence[str] = (),
    legacy_layout: bool = False,
    moved: bool = False,
    sync_breaks: bool = False,
    configured: bool = True,
    extra: Mapping[str, str] | None = None,
    settings: Settings | None = None,
) -> tuple[FastAPI, FakeGitApi]:
    base = {f".factorial/agents/{name}/agent.yaml": "kind: Agent\n" for name in present}
    base |= {f".factorial/skills/{name}/SKILL.md": "# mine\n" for name in present_skills}
    if legacy_layout:
        base[".factorial/process.yaml"] = "kind: Process\n"
    base |= dict(extra or {})
    contents = FakeContents(base)
    api = FakeGitApi(contents, moved=moved, sync_breaks=sync_breaks)
    app = build_app(settings or make_settings(), session)
    writer = writer_for(api) if configured else None
    app.dependency_overrides[config_commit_writer_dependency] = lambda: writer
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(contents)
    return app, api


async def _post(app: FastAPI, project: Project, body: dict[str, object]) -> httpx.Response:
    async with authed_client_for(app) as client:
        return await client.post(f"/api/projects/{project.id}/config-commits", json=body)


async def _versions(session: AsyncSession, project: Project) -> list[tuple[str, str | None]]:
    rows = await session.execute(
        select(ProcessVersion.source_commit_sha, ProcessVersion.source_ref).where(
            ProcessVersion.project_id == project.id
        )
    )
    return [(sha, ref) for sha, ref in rows.all()]


async def test_unconfigured_app_answers_503(async_session: AsyncSession) -> None:
    app, _ = app_with(async_session, configured=False)
    project = await project_in(async_session)
    assert (await _post(app, project, payload())).status_code == 503


async def test_commits_on_the_head_records_it_then_syncs_that_commit(
    async_session: AsyncSession,
) -> None:
    """The event is what lets the sync guard take a commit no human merged, so it lands before
    the branch can point at the commit."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(app, project, payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert api.commits[0]["parents"] == [BASE_SHA]
    assert api.refs == [("heads/factorial", NEW_SHA, False)]
    assert api.order == ["record", "ref"]
    assert (body["sha"], body["branch"]) == (NEW_SHA, "factorial")
    assert body["url"] == f"https://github.com/o/r/commit/{NEW_SHA}"
    assert body["sync_error"] is None
    assert body["sync"]["sha"] == NEW_SHA
    assert [r["outcome"] for r in body["sync"]["results"]] == ["created"]
    assert await _versions(async_session, project) == [(NEW_SHA, "factorial")]
    recorded = (
        await async_session.execute(
            select(Event.details).where(
                Event.project_id == project.id, Event.type == str(EventType.CONFIG_COMMITTED)
            )
        )
    ).scalar_one()
    assert recorded == {
        "branch": "factorial",
        "sha": NEW_SHA,
        "process": "p",
        "requested_by": "api-token",
        "requested_via": "dashboard",
    }


async def test_a_draft_the_head_already_holds_commits_nothing_and_says_so(
    async_session: AsyncSession,
) -> None:
    """The dashboard reads `committed` to tell a no-op from a commit that dropped its edits."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    first = await _post(app, project, payload())
    assert first.status_code == 201, first.text

    again = await _post(app, project, {"content": lane(), "files": []})

    assert again.status_code == 201, again.text
    body = again.json()
    assert (body["committed"], body["written"], body["sha"]) == (False, [], NEW_SHA)
    assert first.json()["committed"] is True
    assert len(api.commits) == 1


async def test_a_moved_branch_is_a_409_and_nothing_is_synced(async_session: AsyncSession) -> None:
    app, _ = app_with(async_session, moved=True)
    project = await project_in(async_session)
    response = await _post(app, project, payload())

    assert response.status_code == 409
    assert "moved during the commit" in response.json()["detail"]
    assert await _versions(async_session, project) == []


async def test_an_unknown_head_is_a_409_that_writes_nothing(async_session: AsyncSession) -> None:
    """A head no event names and no human merged, such as an agent's push, is never built on:
    the builder's commit would carry it into a sha the recorded event then vouches for."""
    app, api = app_with(async_session)
    agent_sha = "e" * 40
    api.contents.files[agent_sha] = api.contents.files[BASE_SHA]
    api.contents.head = agent_sha
    api.contents.parents[agent_sha] = (BASE_SHA,)
    project = await project_in(async_session)
    response = await _post(app, project, payload())

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "eeeeeee was pushed directly" in detail
    assert f"git push --force-with-lease origin {BASE_SHA}:refs/heads/factorial" in detail
    assert (api.trees, api.commits, api.refs) == ([], [], [])
    committed = await async_session.execute(
        select(Event.id).where(
            Event.project_id == project.id, Event.type == str(EventType.CONFIG_COMMITTED)
        )
    )
    assert committed.all() == []


async def test_a_bad_runtime_binding_commits_nothing(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(app, project, payload(runtime="no-such-runtime"))

    assert response.status_code == 422
    assert "no-such-runtime" in response.json()["detail"]
    assert (api.trees, api.commits, api.refs) == ([], [], [])


async def test_a_sync_failure_after_the_commit_still_answers_201(
    async_session: AsyncSession,
) -> None:
    """The commit landed; failing the request would hide it. Sync can be pressed again."""
    app, api = app_with(async_session, sync_breaks=True)
    project = await project_in(async_session)
    response = await _post(app, project, payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert api.refs and body["sha"] == NEW_SHA
    assert body["sync"] is None and body["sync_error"]
    assert await _versions(async_session, project) == []


async def test_the_simple_template_commits_the_whole_sample_folder(
    async_session: AsyncSession,
) -> None:
    """A repo never synced: its seeded branch holds only the seed's README. One commit of the
    editor's template, every file ticked, leaves the sample folder's tree — its MCP server as an
    inert example — and syncs clean with nothing set up outside the repo."""
    readme = ".factorial/README.md"
    app, api = app_with(async_session, extra={readme: "This branch holds the Factorial config.\n"})
    project = await project_in(async_session)
    # The editor's Runtime select writes the line the template leaves commented out.
    [lane] = [lane for lane in PROCESS_TEMPLATES[0].lanes if lane.key == "simple"]
    content = lane.text.replace("# runtime: claude-api", f"runtime: {TEST_RUNTIME}")
    files = _selected_files(_proposal_for(content))
    response = await _post(app, project, {"content": content, "files": files})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sync_error"] is None
    assert body["sync"]["agent_warnings"] == []
    # The sample's other lanes are files of their own, which their own commits write.
    other_lanes = {
        f"processes/process-{other.key}.yaml"
        for other in PROCESS_TEMPLATES[0].lanes
        if other.key != lane.key
    }
    sample = {
        f".factorial/{path.relative_to(SAMPLE).as_posix()}"
        for path in SAMPLE.rglob("*")
        if path.is_file() and path.relative_to(SAMPLE).as_posix() not in other_lanes
    }
    server = ".factorial/mcp/product-docs.yaml"
    assert set(api.written) | {readme} == sample - {server} | {f"{server}.example"}


async def test_the_bundle_carries_process_and_both_agents(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(app, project, payload())
    assert response.status_code == 201
    files = api.written
    assert PROCESS_PATH in files
    assert ".factorial/agents/plan-agent/agent.yaml" in files
    assert ".factorial/agents/build-agent/system-prompt.md" in files
    assert ".factorial/skills/project-constitution/SKILL.md" in files
    components = {c["name"]: c for c in response.json()["components"]}
    assert components["plan-agent"]["present_in_repo"] is False
    assert sorted(response.json()["written"]) == sorted(files)


async def test_an_agent_already_in_the_repo_is_not_written(async_session: AsyncSession) -> None:
    """No gap, no write — this is what makes re-running safe against hand-edited prompts."""
    app, api = app_with(async_session, present=["plan-agent"])
    project = await project_in(async_session)
    response = await _post(app, project, payload(present_agents=["plan-agent"]))
    assert response.status_code == 201
    files = api.written
    assert not any(path.startswith(".factorial/agents/plan-agent/") for path in files)
    assert ".factorial/agents/build-agent/agent.yaml" in files
    components = {c["name"]: c for c in response.json()["components"]}
    assert components["plan-agent"]["present_in_repo"] is True


async def test_a_skill_already_in_the_repo_is_not_written(async_session: AsyncSession) -> None:
    """It is project policy the operator was told to tune — the same protection prompts get."""
    app, api = app_with(async_session, present_skills=["project-constitution"])
    project = await project_in(async_session)
    response = await _post(app, project, payload(present_skills=["project-constitution"]))
    assert response.status_code == 201
    constitution = ".factorial/skills/project-constitution/"
    assert not any(path.startswith(constitution) for path in api.written)


async def test_every_written_path_is_under_the_config_dir(async_session: AsyncSession) -> None:
    """design.md §7.7: every path is server-derived, so the route cannot be steered to a file
    outside `.factorial/`."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    await _post(app, project, payload())
    assert all(path.startswith(".factorial/") for path in api.written)


async def test_invalid_config_is_refused_before_any_write(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app, project, {"content": "specVersion: adlc/v1\nkind: Process\n", "files": []}
    )
    assert response.status_code == 422
    assert api.trees == []


async def test_the_operators_yaml_is_committed_byte_for_byte(
    async_session: AsyncSession,
) -> None:
    """Hand edits are the whole point of taking content rather than a plan."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    edited = lane() + "\n# an operator's own note\n"
    files = _selected_files(_proposal_for(edited))
    response = await _post(app, project, {"content": edited, "files": files})
    assert response.status_code == 201
    assert api.written[PROCESS_PATH] == edited


async def test_an_agents_files_left_unselected_are_stubbed(async_session: AsyncSession) -> None:
    """Selection is per file: leaving one agent's files unticked stubs that agent even though its
    template exists and would have fitted. The sync names the stub, since it does not parse."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(app, project, payload(only=("build-agent", "project-constitution")))
    assert response.status_code == 201
    assert ".factorial/agents/plan-agent/system-prompt.md" not in api.written
    assert ".factorial/agents/build-agent/system-prompt.md" in api.written
    assert any(w.startswith("plan-agent") for w in response.json()["sync"]["agent_warnings"])


async def test_a_repo_still_on_the_root_process_file_is_refused(
    async_session: AsyncSession,
) -> None:
    """Writing `processes/process-p.yaml` beside the root file would create the mixed layout the
    loader refuses at sync, so the operator moves the file first (decision 37)."""
    app, api = app_with(async_session, legacy_layout=True)
    project = await project_in(async_session)
    response = await _post(app, project, payload())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert ".factorial/process.yaml" in detail and "git mv" in detail
    assert api.trees == []


async def test_preview_returns_file_bodies(async_session: AsyncSession) -> None:
    app, _ = app_with(async_session)
    project = await project_in(async_session)
    async with authed_client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/config-preview", json={"content": UNSTACKED}
        )
    assert response.status_code == 200
    files = {f["path"]: f for f in response.json()["files"]}
    body = files[".factorial/agents/plan-agent/instructions.md"]
    assert body["body"].startswith("#")
    assert body["facts"]["__BRANCH_PREFIX__"] == "spec"
    assert body["present_in_repo"] is False


async def test_preview_and_commit_agree_on_gitlab_paths(async_session: AsyncSession) -> None:
    """The commit recomputes the proposal it checks the selection against, so it must take the
    forge from the same place the preview does: the project's ticket source."""
    app, api = app_with(async_session)
    project = await project_in(async_session)
    async_session.add(
        TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITLAB, external_ref=project.github_repo
        )
    )
    await async_session.flush()
    [lane] = [lane for lane in PROCESS_TEMPLATES[0].lanes if lane.key == "simple"]
    content = lane.text.replace("# runtime: claude-api", f"runtime: {TEST_RUNTIME}")
    async with authed_client_for(app) as client:
        preview = await client.post(
            f"/api/projects/{project.id}/config-preview", json={"content": content}
        )
    assert preview.status_code == 200, preview.text
    previewed = preview.json()
    bodies = {f["path"]: f["body"] for f in previewed["files"]}
    picked = sorted(
        path
        for component in previewed["components"]
        for offer in component["variants"]
        if offer["offered"] and offer["default_on"]
        for path in offer["paths"]
    )
    files = [{"path": path, "body": bodies[path]} for path in picked]
    response = await _post(app, project, {"content": content, "files": files})
    assert response.status_code == 201, response.text
    assert set(picked) <= set(api.written)
    assert response.json()["components"] == previewed["components"]


async def test_preview_reports_a_withheld_variant_with_its_reason(
    async_session: AsyncSession,
) -> None:
    app, _ = app_with(async_session)
    project = await project_in(async_session)
    async with authed_client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/config-preview", json={"content": UNSTACKED}
        )
    build = next(c for c in response.json()["components"] if c["name"] == "build-agent")
    stacked = next(v for v in build["variants"] if v["variant"] == "stacked")
    assert stacked["offered"] is False and "targets the base branch" in stacked["reason"]


class _NoBranch(FakeContents):
    async def resolve_ref(self, repo: str, ref: str) -> str:
        if ref == f"heads/{CONFIG_BRANCH}":
            raise ContentsNotFound(ref)
        return ref


async def test_preview_without_the_branch_says_where_to_create_it(
    async_session: AsyncSession,
) -> None:
    """The editor shows this text; it must name the section that creates the branch and say why
    a branch made by hand will not do, because that is the mistake issue #304 made."""
    app, _ = app_with(async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(_NoBranch({}))
    project = await project_in(async_session)
    async with authed_client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/config-preview", json={"content": lane()}
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Process config page" in detail
    assert "refuses a branch created by hand" in detail
    assert "Create the factorial branch first" not in detail


async def test_a_path_the_proposal_does_not_contain_is_refused(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        {
            "content": UNSTACKED,
            "files": [{"path": ".github/workflows/ci.yml", "body": "on: push"}],
        },
    )
    assert response.status_code == 422
    assert "not proposed" in response.json()["detail"]
    assert api.trees == [], "a refused selection must never reach the App"


async def test_a_submitted_process_yaml_is_refused(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app, project, {"content": UNSTACKED, "files": [{"path": PROCESS_PATH, "body": "x: 1"}]}
    )
    assert response.status_code == 422
    assert api.trees == [], "a refused selection must never reach the App"


async def test_the_edited_body_is_committed(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        {
            "content": UNSTACKED.replace(
                "kind: Process\n", f"kind: Process\nruntime: {TEST_RUNTIME}\n"
            ),
            "files": [{"path": ".factorial/agents/plan-agent/instructions.md", "body": "# mine\n"}],
        },
    )
    assert response.status_code == 201
    assert api.written[".factorial/agents/plan-agent/instructions.md"] == "# mine\n"


# --- seeding the config branch ------------------------------------------------------------------

FACTORIAL_TREE = "e" * 40


class _SeedApi:
    """The git calls `seed_config_branch` makes, recorded; `.factorial/` is a subtree at HEAD."""

    def __init__(self, *, branch_exists: bool) -> None:
        self.branch_exists = branch_exists
        self.trees: list[list[dict[str, str]]] = []
        self.commits: list[dict[str, Any]] = []
        self.refs: list[tuple[str, str]] = []
        self.order: list[str] = []
        self.repos = SimpleNamespace(
            async_get_branch=self._get_branch,
            async_get=self._ok(SimpleNamespace(default_branch="main")),
            async_get_commit=self._ok(SimpleNamespace(sha=BASE_SHA)),
        )
        tree = [
            SimpleNamespace(path="src", type="tree", sha="1" * 40),
            SimpleNamespace(path=".factorial", type="tree", sha=FACTORIAL_TREE),
        ]
        self.git = SimpleNamespace(
            async_get_tree=self._ok(SimpleNamespace(tree=tree)),
            async_create_tree=self._create_tree,
            async_create_commit=self._create_commit,
            async_create_ref=self._create_ref,
        )

    @staticmethod
    def _ok(data: Any) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(parsed_data=data)

        return call

    async def _get_branch(self, owner: str, name: str, branch: str) -> Any:
        if self.branch_exists:
            return SimpleNamespace(parsed_data=None)
        raw = httpx.Response(404, json={}, request=httpx.Request("GET", "https://x"))
        raise RequestFailed(Response(raw, dict))

    async def _create_tree(self, owner: str, name: str, *, tree: list[dict[str, str]]) -> Any:
        self.trees.append(tree)
        return SimpleNamespace(parsed_data=SimpleNamespace(sha="t" * 40))

    async def _create_commit(self, owner: str, name: str, **kwargs: Any) -> Any:
        self.commits.append(kwargs)
        return SimpleNamespace(parsed_data=SimpleNamespace(sha="c" * 40))

    async def _create_ref(self, owner: str, name: str, *, ref: str, sha: str) -> Any:
        self.refs.append((ref, sha))
        self.order.append("ref")
        return SimpleNamespace(parsed_data=None)


def seed_app(session: AsyncSession, api: _SeedApi) -> FastAPI:
    client = SimpleNamespace(api=lambda _repo: _async(SimpleNamespace(rest=api)))

    async def seed(repo: str, record: Callable[[str], Awaitable[None]], empty: bool) -> str:
        async def traced(sha: str) -> None:
            await record(sha)
            api.order.append("record")

        return await seed_config_branch(
            client,  # type: ignore[arg-type]
            repo,
            CONFIG_BRANCH,
            CONFIG_DIRS,
            traced,
            empty=empty,
        )

    app = build_app(make_settings(), session)
    app.dependency_overrides[config_branch_seeder_dependency] = lambda: seed
    return app


async def test_seeding_commits_the_config_subtree_as_an_orphan_and_records_it(
    async_session: AsyncSession,
) -> None:
    """The recorded sha is what lets the sync guard accept a commit no pull request produced. It
    is recorded before the ref exists, so no branch is left that no event names."""
    api = _SeedApi(branch_exists=False)
    project = await project_in(async_session, seeded=False)
    async with authed_client_for(seed_app(async_session, api)) as client:
        response = await client.post(f"/api/projects/{project.id}/config-branch")

    assert response.status_code == 201, response.text
    assert response.json() == {"branch": "factorial", "sha": "c" * 40}
    assert api.trees == [
        [{"path": ".factorial", "mode": "040000", "type": "tree", "sha": FACTORIAL_TREE}]
    ]
    assert api.commits[0]["parents"] == []
    assert api.refs == [("refs/heads/factorial", "c" * 40)]
    assert api.order == ["record", "ref"]
    seeded = (
        (
            await async_session.execute(
                select(Event.details).where(
                    Event.project_id == project.id,
                    Event.type == str(EventType.CONFIG_BRANCH_CREATED),
                )
            )
        )
        .scalars()
        .all()
    )
    assert seeded == [{"branch": "factorial", "sha": "c" * 40}]


async def test_seeding_an_existing_branch_is_a_409_that_writes_nothing(
    async_session: AsyncSession,
) -> None:
    api = _SeedApi(branch_exists=True)
    project = await project_in(async_session, seeded=False)
    async with authed_client_for(seed_app(async_session, api)) as client:
        response = await client.post(f"/api/projects/{project.id}/config-branch")

    assert response.status_code == 409
    assert (api.trees, api.commits, api.refs) == ([], [], [])


class _SeedRefused(RuntimeError):
    pass


async def test_a_seeding_failure_other_than_an_existing_branch_is_raised_as_itself(
    async_session: AsyncSession,
) -> None:
    """Only `ConfigBranchExists` (and a GitLab refusal) become a 409: anything else GitHub
    raises must reach the error log as itself, not as a NameError from a later except clause."""

    async def seed(repo: str, record: object, empty: bool) -> str:
        raise _SeedRefused("GitHub refused the tree")

    project = await project_in(async_session, seeded=False)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_branch_seeder_dependency] = lambda: seed
    async with authed_client_for(app) as client:
        with pytest.raises(_SeedRefused, match="GitHub refused the tree"):
            await client.post(f"/api/projects/{project.id}/config-branch")


async def test_seeding_empty_writes_only_the_readme_though_main_has_config(
    async_session: AsyncSession,
) -> None:
    """A template's files reach the branch only through the builder's commit, which recomputes
    every path; an empty seed is what it commits onto, even when main already has a folder."""
    api = _SeedApi(branch_exists=False)
    project = await project_in(async_session, seeded=False)
    async with authed_client_for(seed_app(async_session, api)) as client:
        response = await client.post(
            f"/api/projects/{project.id}/config-branch", json={"from": "empty"}
        )

    assert response.status_code == 201, response.text
    assert [entry["path"] for entry in api.trees[0]] == [".factorial/README.md"]
    assert api.commits[0]["parents"] == []
    assert "to start from a template" in api.commits[0]["message"]
    assert api.order == ["record", "ref"]


async def test_an_unknown_seed_source_is_a_422_that_writes_nothing(
    async_session: AsyncSession,
) -> None:
    api = _SeedApi(branch_exists=False)
    project = await project_in(async_session, seeded=False)
    async with authed_client_for(seed_app(async_session, api)) as client:
        response = await client.post(
            f"/api/projects/{project.id}/config-branch", json={"from": "deadbeef"}
        )

    assert response.status_code == 422
    assert (api.trees, api.commits, api.refs) == ([], [], [])


SKILL = ".factorial/skills/project-constitution/SKILL.md"
TRACKER = ".factorial/mcp/tracker.yaml"
TRACKER_YAML = (
    "specVersion: factorial/v1\nkind: McpServer\nname: tracker\ntransport: http\n"
    "http:\n  url: https://example.invalid/tracker\nrequired: false\n"
)


def with_edits(body: dict[str, object], *edits: dict[str, str]) -> dict[str, object]:
    return {**body, "edits": list(edits)}


async def test_an_edit_to_a_file_on_the_branch_is_committed(async_session: AsyncSession) -> None:
    app, api = app_with(async_session, present_skills=["project-constitution"])
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_skills=["project-constitution"]),
            {"path": SKILL, "body": "# tuned\n", "base_sha": blob("# mine\n")},
        ),
    )
    assert response.status_code == 201
    assert api.written[SKILL] == "# tuned\n"
    assert SKILL in response.json()["written"]


async def test_an_edit_to_a_file_that_moved_is_a_409_that_writes_nothing(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session, present_skills=["project-constitution"])
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_skills=["project-constitution"]),
            {"path": SKILL, "body": "# tuned\n", "base_sha": "0" * 40},
        ),
    )
    assert response.status_code == 409
    assert SKILL in response.json()["detail"]
    assert api.trees == []


async def test_an_edit_to_a_file_the_branch_lacks_is_refused(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(),
            {"path": ".factorial/agents/plan-agent/extra.md", "body": "x", "base_sha": "0" * 40},
        ),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        ".factorial/agents/plan-agent/extra.md is not a file on the config branch and will not "
        "be written"
    )
    assert api.trees == []


async def test_a_created_file_is_committed(async_session: AsyncSession) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    path = ".factorial/notes/runbook.md"
    response = await _post(
        app, project, {**payload(), "created": [{"path": path, "body": "# runbook\n"}]}
    )
    assert response.status_code == 201
    assert api.written[path] == "# runbook\n"
    assert path in response.json()["written"]


async def test_a_created_file_outside_the_config_folder_writes_nothing(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        {**payload(), "created": [{"path": ".github/workflows/ci.yml", "body": "on: push\n"}]},
    )
    assert response.status_code == 422
    assert ".github/workflows/ci.yml cannot be created" in response.json()["detail"]
    assert api.trees == []


def with_deletes(body: dict[str, object], *deletes: dict[str, str]) -> dict[str, object]:
    return {**body, "deleted": list(deletes)}


OTHER_LANE = ".factorial/processes/process-other.yaml"


async def test_a_deleted_file_is_removed_by_the_commit(async_session: AsyncSession) -> None:
    app, api = app_with(async_session, extra={TRACKER: TRACKER_YAML})
    project = await project_in(async_session)
    response = await _post(
        app, project, with_deletes(payload(), {"path": TRACKER, "base_sha": blob(TRACKER_YAML)})
    )
    assert response.status_code == 201, response.text
    assert response.json()["deleted"] == [TRACKER]
    assert api.removed == [TRACKER]
    assert TRACKER not in api.written
    assert TRACKER not in api.contents.files[NEW_SHA]


async def test_deleting_another_process_file_retires_that_process_at_the_sync(
    async_session: AsyncSession,
) -> None:
    other = lane("other")
    app, api = app_with(async_session, extra={OTHER_LANE: other})
    project = await project_in(async_session)
    await sync_config(async_session, RepoConfigSource(api.contents), project)
    response = await _post(
        app, project, with_deletes(payload(), {"path": OTHER_LANE, "base_sha": blob(other)})
    )
    assert response.status_code == 201, response.text
    outcomes = {row["process"]: row["outcome"] for row in response.json()["sync"]["results"]}
    assert outcomes == {"other": "retired", "p": "created"}


async def _on_two_processes(session: AsyncSession) -> tuple[FastAPI, FakeGitApi, Project]:
    """`p` and `other` on the branch and synced, with the agents both name."""
    files = {PROCESS_PATH: lane(), OTHER_LANE: lane("other")}
    for path, proposed in _proposal_for(lane()).files.items():
        files[path] = proposed.body
    app, api = app_with(session, extra=files)
    project = await project_in(session)
    await sync_config(session, RepoConfigSource(api.contents), project)
    return app, api, project


async def test_deleting_the_process_on_screen_commits_its_removal_and_retires_it(
    async_session: AsyncSession,
) -> None:
    app, api, project = await _on_two_processes(async_session)
    body = {"content": lane(), "deleted": [{"path": PROCESS_PATH, "base_sha": blob(lane())}]}
    response = await _post(app, project, body)
    assert response.status_code == 201, response.text
    assert response.json()["written"] == []
    assert response.json()["deleted"] == [PROCESS_PATH]
    assert api.written == {}
    assert api.removed == [PROCESS_PATH]
    assert api.commits[0]["message"] == "Delete the p process from the Factorial dashboard"
    outcomes = {row["process"]: row["outcome"] for row in response.json()["sync"]["results"]}
    assert outcomes == {"other": "created", "p": "retired"}


async def test_a_retried_delete_of_the_process_on_screen_is_a_no_op(
    async_session: AsyncSession,
) -> None:
    """The first response was lost and the person pressed again: the process stays deleted."""
    app, api, project = await _on_two_processes(async_session)
    body = {"content": lane(), "deleted": [{"path": PROCESS_PATH, "base_sha": blob(lane())}]}
    assert (await _post(app, project, body)).status_code == 201
    retry = await _post(app, project, body)
    assert retry.status_code == 201, retry.text
    assert retry.json()["committed"] is False
    assert len(api.trees) == 1
    assert PROCESS_PATH not in api.contents.files[api.contents.head]


async def test_deleting_the_only_process_is_a_422_in_plain_words(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session, extra={PROCESS_PATH: lane()})
    project = await project_in(async_session)
    body = {"content": lane(), "deleted": [{"path": PROCESS_PATH, "base_sha": blob(lane())}]}
    response = await _post(app, project, body)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "A project keeps at least one process. Add another process before you delete this one."
    )
    assert api.trees == []


async def test_a_delete_of_a_file_that_moved_is_a_409_that_writes_nothing(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session, extra={TRACKER: TRACKER_YAML})
    project = await project_in(async_session)
    response = await _post(
        app, project, with_deletes(payload(), {"path": TRACKER, "base_sha": blob("older\n")})
    )
    assert response.status_code == 409
    assert TRACKER in response.json()["detail"]
    assert api.trees == []


async def test_deleting_a_file_a_manifest_still_names_commits_nothing_and_names_the_manifest(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session, present_skills=["project-constitution"])
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_deletes(
            payload(present_skills=["project-constitution"]),
            {"path": SKILL, "base_sha": blob("# mine\n")},
        ),
    )
    assert response.status_code == 422, response.text
    assert response.json()["path"].endswith("/agent.yaml")
    assert "project-constitution" in response.json()["detail"]
    assert api.trees == []


async def test_an_edit_the_loader_rejects_commits_nothing(async_session: AsyncSession) -> None:
    app, api = app_with(async_session, extra={TRACKER: TRACKER_YAML})
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(), {"path": TRACKER, "body": "kind: [\n", "base_sha": blob(TRACKER_YAML)}
        ),
    )
    assert response.status_code == 422
    assert response.json()["path"] == TRACKER
    assert response.json()["detail"].startswith(f"{TRACKER}: The file is not valid YAML at line ")
    assert api.trees == []


async def _runtime(session: AsyncSession, kind: RuntimeType) -> str:
    slug = f"rt-{uuid4().hex[:12]}"
    session.add(
        Runtime(
            organization_id=await default_organization_id(session),
            slug=slug,
            name=slug,
            type=kind,
        )
    )
    await session.flush()
    return slug


async def test_an_edit_naming_an_unknown_runtime_in_another_lane_commits_nothing(
    async_session: AsyncSession,
) -> None:
    """Sync checks every lane's binding at the new commit, so an edited lane naming a Runtime
    that does not exist would fail it for every lane after the commit had landed."""
    other = ".factorial/processes/process-q.yaml"
    before = lane("q")
    app, api = app_with(async_session, extra={other: before})
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(),
            {
                "path": other,
                "body": lane("q", runtime="no-such-runtime"),
                "base_sha": blob(before),
            },
        ),
    )
    assert response.status_code == 422, response.text
    assert "no-such-runtime" in response.json()["detail"]
    assert api.trees == []


async def test_a_runtime_that_fits_an_edited_manifest_is_committed(
    async_session: AsyncSession,
) -> None:
    """The binding is judged against the manifests the commit will produce, not the ones the
    branch had: moving an agent to codex and picking a codex Runtime is one commit."""
    slug = await _runtime(async_session, RuntimeType.CODEX_API)
    manifest = ".factorial/agents/plan-agent/agent.yaml"
    app, api = app_with(
        async_session,
        extra=agent_files("plan-agent", "claude") | agent_files("build-agent", "codex"),
    )
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_agents=["build-agent", "plan-agent"], runtime=slug),
            {
                "path": manifest,
                "body": agent_manifest("plan-agent", "codex"),
                "base_sha": blob(agent_manifest("plan-agent", "claude")),
            },
        ),
    )
    assert response.status_code == 201, response.text
    assert api.written[manifest] == agent_manifest("plan-agent", "codex")
    assert response.json()["sync_error"] is None


async def test_a_stubbed_agent_does_not_judge_the_runtime_of_an_edit_commit(
    async_session: AsyncSession,
) -> None:
    """Validation stands a `runtime.kind: claude` manifest in for a stub, which says nothing
    about the family; sync skips the unparseable stub the same way."""
    slug = await _runtime(async_session, RuntimeType.CODEX_API)
    prompt = ".factorial/agents/build-agent/instructions.md"
    app, api = app_with(async_session, extra=agent_files("build-agent", "codex"))
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_agents=["build-agent"], runtime=slug, only=("project-constitution",)),
            {"path": prompt, "body": "# tuned\n", "base_sha": blob("# instructions\n")},
        ),
    )
    assert response.status_code == 201, response.text
    assert api.written[prompt] == "# tuned\n"


async def test_a_file_the_loader_never_parses_need_not_be_text(
    async_session: AsyncSession,
) -> None:
    """The loader only checks that prompts and skills exist, so a binary beside them is no
    reason to refuse every edit on the repo."""
    diagram = ".factorial/skills/project-constitution/diagram.png"
    app, api = app_with(
        async_session, present_skills=["project-constitution"], extra={diagram: "PNG"}
    )
    api.contents.binary.add(diagram)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_skills=["project-constitution"]),
            {"path": SKILL, "body": "# tuned\n", "base_sha": blob("# mine\n")},
        ),
    )
    assert response.status_code == 201, response.text
    assert api.written[SKILL] == "# tuned\n"


async def test_a_parsed_file_that_is_not_text_is_a_422_naming_it(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(
        async_session,
        present_skills=["project-constitution"],
        extra={TRACKER: TRACKER_YAML},
    )
    api.contents.binary.add(TRACKER)
    project = await project_in(async_session)
    response = await _post(
        app,
        project,
        with_edits(
            payload(present_skills=["project-constitution"]),
            {"path": SKILL, "body": "# tuned\n", "base_sha": blob("# mine\n")},
        ),
    )
    assert response.status_code == 422, response.text
    assert response.json()["path"] == TRACKER
    assert response.json()["detail"].startswith(f"{TRACKER}: ")
    assert api.trees == []


# --- the whole-draft check ------------------------------------------------------------------------


async def _check(app: FastAPI, project: Project, body: dict[str, object]) -> httpx.Response:
    async with authed_client_for(app) as client:
        return await client.post(f"/api/projects/{project.id}/config-checks", json=body)


async def test_a_check_of_a_sound_draft_says_what_a_commit_would_do_and_writes_nothing(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session, extra={TRACKER: TRACKER_YAML})
    project = await project_in(async_session)
    response = await _check(
        app, project, with_deletes(payload(), {"path": TRACKER, "base_sha": blob(TRACKER_YAML)})
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["problems"] == []
    assert PROCESS_PATH in body["would_write"]
    assert body["would_delete"] == [TRACKER]
    assert api.trees == []
    assert await _versions(async_session, project) == []


async def test_a_check_names_the_file_a_folder_problem_is_in(async_session: AsyncSession) -> None:
    app, _ = app_with(async_session, extra={TRACKER: TRACKER_YAML})
    project = await project_in(async_session)
    response = await _check(
        app,
        project,
        with_edits(
            payload(), {"path": TRACKER, "body": "kind: [\n", "base_sha": blob(TRACKER_YAML)}
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    (problem,) = body["problems"]
    assert body["valid"] is False
    assert problem["path"] == TRACKER
    assert problem["message"].startswith("The file is not valid YAML at line ")


async def test_a_check_of_text_that_does_not_parse_reads_nothing_from_the_forge(
    async_session: AsyncSession,
) -> None:
    app, api = app_with(async_session)
    project = await project_in(async_session)

    async def unread(repo: str, ref: str) -> str:
        raise AssertionError("a draft that does not parse must not reach the forge")

    api.contents.resolve_ref = unread  # type: ignore[method-assign]
    response = await _check(app, project, {"content": "steps: [\n"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["problems"][0]["path"] is None
    assert body["problems"][0]["message"].startswith("The file is not valid YAML")


# --- the MCP surface (design.md §7.17) ----------------------------------------------------------


def gated(approvers: str, permission: str | None = "write") -> str:
    """`lane()` with a gate between its two agent steps. `permission=None` leaves
    `approverPermission` out."""
    gate = (
        "  - name: review\n"
        "    kind: human_gate\n"
        "    next: build\n"
        "    gate:\n"
        "      type: pr_approval\n"
        '      pr: { head: "spec/{ticket}-*", base: "main" }\n'
        f"      approvers: [{approvers}]\n"
        + (f"      approverPermission: {permission}\n" if permission is not None else "")
        + "      onApprove:\n"
        "        merge: true\n"
        "  - name: build\n"
    )
    text = lane().replace("    next: build\n", "    next: review\n", 1)
    return text.replace("  - name: build\n", gate, 1)


async def test_an_agents_files_are_dashboard_only(async_session: AsyncSession) -> None:
    """An agent's prompt holds the only control on what it may do, so a model's commit must not
    rewrite it, nor write a new agent's files."""
    prompt = ".factorial/agents/build-agent/instructions.md"
    source = RepoConfigSource(
        FakeContents(agent_files("plan-agent", "claude") | agent_files("build-agent", "claude"))
    )
    project = await project_in(async_session)
    draft = ConfigDraft(
        content=lane(),
        edits=[FileEdit(prompt, "# merge your own PR\n", blob("# instructions\n"))],
    )

    mcp = await check_config(async_session, source, project, draft, surface="mcp")
    dashboard = await check_config(async_session, source, project, draft, surface="dashboard")
    lacking = RepoConfigSource(FakeContents(agent_files("plan-agent", "claude")))
    stubbed = await check_config(
        async_session, lacking, project, ConfigDraft(content=lane()), surface="mcp"
    )

    assert not mcp.valid
    assert prompt in mcp.blockers[0] and "dashboard-only" in mcp.blockers[0]
    assert dashboard.valid, dashboard.blockers
    assert PlannedWrite(prompt, "edit") in dashboard.would_write
    assert not stubbed.valid
    assert ".factorial/agents/build-agent/" in stubbed.blockers[0]
    assert "dashboard-only" in stubbed.blockers[0]


async def test_new_files_are_dashboard_only(async_session: AsyncSession) -> None:
    """A created file is the one path the client names freely, so only a person names it."""
    source = RepoConfigSource(FakeContents(agent_files("plan-agent", "claude")))
    project = await project_in(async_session)
    draft = ConfigDraft(content=lane(), created={".factorial/notes/runbook.md": "# runbook\n"})

    mcp = await check_config(async_session, source, project, draft, surface="mcp")
    dashboard = await check_config(async_session, source, project, draft, surface="dashboard")

    assert not mcp.valid
    assert "runbook.md" in mcp.blockers[0] and "dashboard-only" in mcp.blockers[0]
    assert dashboard.valid, dashboard.blockers


async def test_shared_skills_are_dashboard_only(async_session: AsyncSession) -> None:
    """A shared skill loads into an agent's context like its prompt, so it is behind a person
    on the same terms."""
    skill = ".factorial/skills/review/SKILL.md"
    source = RepoConfigSource(
        FakeContents(
            agent_files("plan-agent", "claude")
            | agent_files("build-agent", "claude")
            | {skill: "# review\n"}
        )
    )
    project = await project_in(async_session)
    draft = ConfigDraft(
        content=lane(), edits=[FileEdit(skill, "# merge your own PR\n", blob("# review\n"))]
    )

    mcp = await check_config(async_session, source, project, draft, surface="mcp")
    dashboard = await check_config(async_session, source, project, draft, surface="dashboard")

    assert not mcp.valid
    assert skill in mcp.blockers[0] and "dashboard-only" in mcp.blockers[0]
    assert dashboard.valid, dashboard.blockers
    assert PlannedWrite(skill, "edit") in dashboard.would_write


@pytest.mark.parametrize(
    "after",
    [gated('"operator", "outsider"'), gated('"operator"', permission="admin")],
    ids=["approvers", "approverPermission"],
)
async def test_an_approver_change_is_dashboard_only(
    async_session: AsyncSession, after: str
) -> None:
    """Who may approve a gate is a person's decision, and a commit through MCP would change it
    without one."""
    before = gated('"operator"')
    source = RepoConfigSource(
        FakeContents(
            {PROCESS_PATH: before}
            | agent_files("plan-agent", "claude")
            | agent_files("build-agent", "claude")
        )
    )
    project = await project_in(async_session)
    draft = ConfigDraft(content=after)

    mcp = await check_config(async_session, source, project, draft, surface="mcp")
    dashboard = await check_config(async_session, source, project, draft, surface="dashboard")
    note = ConfigDraft(content=f"{before}# a note\n")

    assert (await check_config(async_session, source, project, note, surface="mcp")).valid
    assert not mcp.valid
    assert "'review'" in mcp.blockers[0] and "dashboard-only" in mcp.blockers[0]
    assert dashboard.valid, dashboard.blockers


@pytest.mark.parametrize(
    ("before", "after", "allowed"),
    [
        (gated("", permission=None), lane(), False),
        (lane(), gated("", permission=None), False),
        (None, gated('"operator"'), False),
        (None, gated("", permission=None), True),
    ],
    ids=["gate-removed", "gate-added", "new-lane-names-approvers", "new-lane-leaves-them-unset"],
)
async def test_a_gate_added_or_removed_is_an_approver_change(
    async_session: AsyncSession, before: str | None, after: str, allowed: bool
) -> None:
    """A gate with no approvers set still decides that a person approves, so adding or removing
    one changes who approves. A new lane may add gates, but leaves their approvers to a person."""
    agents = agent_files("plan-agent", "claude") | agent_files("build-agent", "claude")
    base = agents if before is None else {PROCESS_PATH: before} | agents
    source = RepoConfigSource(FakeContents(base))
    project = await project_in(async_session)
    draft = ConfigDraft(content=after)

    mcp = await check_config(async_session, source, project, draft, surface="mcp")
    dashboard = await check_config(async_session, source, project, draft, surface="dashboard")

    assert mcp.valid is allowed, mcp.blockers
    if not allowed:
        assert "'review'" in mcp.blockers[0] and "dashboard-only" in mcp.blockers[0]
    assert dashboard.valid, dashboard.blockers


PUSHED_SHA = "e" * 40
PERSON = Actor.dashboard("p@leaninnovationlabs.com")


def pushed_on_base(api: FakeGitApi, files: Mapping[str, str] | None = None) -> None:
    """A direct push on top of the seed: no event names it and no human merged it."""
    api.contents.files[PUSHED_SHA] = dict(
        files if files is not None else {".factorial/processes/process-p.yaml": lane()}
    )
    api.contents.head = PUSHED_SHA
    api.contents.parents[PUSHED_SHA] = (BASE_SHA,)


async def _adoptions(session: AsyncSession, project: Project) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Event.details).where(
            Event.project_id == project.id, Event.type == str(EventType.CONFIG_HEAD_ADOPTED)
        )
    )
    return list(rows.scalars())


async def test_previewing_an_unknown_head_compares_it_with_the_last_known_commit(
    async_session: AsyncSession,
) -> None:
    _, api = app_with(async_session)
    pushed_on_base(api)
    project = await project_in(async_session)
    head = await adoption.preview_unknown_head(
        async_session, RepoConfigSource(api.contents), project
    )
    assert (head.head_sha, head.known_base) == (PUSHED_SHA, BASE_SHA)
    assert api.contents.compares == [(BASE_SHA, PUSHED_SHA)]
    assert head.compare_url == f"https://github.com/o/r/compare/{BASE_SHA}...{PUSHED_SHA}"


async def test_previewing_a_known_head_is_a_409(async_session: AsyncSession) -> None:
    _, api = app_with(async_session)
    project = await project_in(async_session)
    with pytest.raises(Conflict, match="nothing to adopt"):
        await adoption.preview_unknown_head(async_session, RepoConfigSource(api.contents), project)


async def test_adopting_records_the_person_then_syncs_the_head(
    async_session: AsyncSession,
) -> None:
    _, api = app_with(async_session)
    pushed_on_base(api)
    project = await project_in(async_session)
    adopted = await adoption.adopt_head(
        async_session, RepoConfigSource(api.contents), project, sha=PUSHED_SHA, actor=PERSON
    )
    assert adopted.adopted and adopted.sync_error is None
    assert adopted.sync is not None and adopted.sync.result.sha == PUSHED_SHA
    assert await _adoptions(async_session, project) == [
        {
            "branch": "factorial",
            "sha": PUSHED_SHA,
            "known_base": BASE_SHA,
            "commits": 1,
            "requested_by": "p@leaninnovationlabs.com",
            "requested_via": "dashboard",
        }
    ]
    assert await _versions(async_session, project) == [(PUSHED_SHA, "factorial")]


async def test_adopting_a_moved_head_is_a_409_that_records_nothing(
    async_session: AsyncSession,
) -> None:
    """The person vouched for the head they read. A push after that is a different commit."""
    _, api = app_with(async_session)
    pushed_on_base(api)
    project = await project_in(async_session)
    with pytest.raises(Conflict, match="moved"):
        await adoption.adopt_head(
            async_session, RepoConfigSource(api.contents), project, sha="f" * 40, actor=PERSON
        )
    assert await _adoptions(async_session, project) == []


async def test_a_comparison_github_cannot_make_is_a_409_that_records_nothing(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage-collected base 404s the compare; that is a refusal, not a server error."""
    _, api = app_with(async_session)
    pushed_on_base(api)

    async def gone(repo: str, base: str, head: str) -> Comparison:
        raise ContentsNotFound("no such base")

    monkeypatch.setattr(api.contents, "compare", gone)
    project = await project_in(async_session)
    with pytest.raises(Conflict, match="cannot compare"):
        await adoption.adopt_head(
            async_session, RepoConfigSource(api.contents), project, sha=PUSHED_SHA, actor=PERSON
        )
    assert await _adoptions(async_session, project) == []


async def test_adoption_is_refused_over_mcp(async_session: AsyncSession) -> None:
    """A model under prompt injection could otherwise vouch for an agent's push."""
    _, api = app_with(async_session)
    pushed_on_base(api)
    project = await project_in(async_session)
    with pytest.raises(Forbidden, match="Only a person"):
        await adoption.adopt_head(
            async_session,
            RepoConfigSource(api.contents),
            project,
            sha=PUSHED_SHA,
            actor=Actor(by="p@leaninnovationlabs.com", via=RequestedVia.MCP, mcp_token_id=1),
        )
    assert await _adoptions(async_session, project) == []


async def test_a_head_with_no_known_commit_beneath_is_adoptable(
    async_session: AsyncSession,
) -> None:
    """Another install's seed is unknown to this database, so a person vouches for the tip
    instead of re-seeding, which would lock that install out in turn (issue #422)."""
    _, api = app_with(async_session)
    pushed_on_base(api)
    project = await project_in(async_session, seeded=False)
    preview = await adoption.preview_unknown_head(
        async_session, RepoConfigSource(api.contents), project
    )
    assert preview.known_base is None
    assert preview.comparison.total_commits == 0
    adopted = await adoption.adopt_head(
        async_session, RepoConfigSource(api.contents), project, sha=PUSHED_SHA, actor=PERSON
    )
    assert adopted.adopted
    assert await _adoptions(async_session, project) == [
        {
            "branch": "factorial",
            "sha": PUSHED_SHA,
            "known_base": None,
            "commits": 0,
            "requested_by": "p@leaninnovationlabs.com",
            "requested_via": "dashboard",
        }
    ]


async def test_a_chain_longer_than_the_walk_is_unknown_not_baseless(
    async_session: AsyncSession,
) -> None:
    """A walk that stops at its limit never saw the chain's root: a known commit may lie below,
    with an agent's pushes on top of it. Only a chain walked to its root has no base."""
    _, api = app_with(async_session)
    chain = [f"{n:040x}" for n in range(1, WALK_LIMIT + 3)]
    for child, parent in pairwise(chain):
        api.contents.parents[child] = (parent,)
    api.contents.files[chain[0]] = {".factorial/processes/process-p.yaml": lane()}
    api.contents.head = chain[0]
    project = await project_in(async_session, seeded=False)
    source = RepoConfigSource(api.contents)
    with pytest.raises(Conflict, match="more than 50 commits"):
        await adoption.preview_unknown_head(async_session, source, project)
    with pytest.raises(Conflict, match="more than 50 commits"):
        await adoption.adopt_head(async_session, source, project, sha=chain[0], actor=PERSON)
    assert await _adoptions(async_session, project) == []


async def test_an_adopted_head_that_does_not_parse_stays_adopted(
    async_session: AsyncSession,
) -> None:
    """The builder can then commit the fix on top, which it refuses at an unknown head."""
    _, api = app_with(async_session)
    pushed_on_base(api, {".factorial/processes/process-p.yaml": "kind: Process\nname: [\n"})
    project = await project_in(async_session)
    adopted = await adoption.adopt_head(
        async_session, RepoConfigSource(api.contents), project, sha=PUSHED_SHA, actor=PERSON
    )
    assert adopted.sync is None and adopted.sync_error
    assert len(await _adoptions(async_session, project)) == 1
    assert await _versions(async_session, project) == []


async def test_an_adopted_head_whose_sync_raises_anything_stays_adopted(
    async_session: AsyncSession,
) -> None:
    """The event is committed first, so no failure of the sync may surface as a 500."""
    _, api = app_with(async_session)
    pushed_on_base(api)
    api.contents.binary.add(PROCESS_PATH)
    project = await project_in(async_session)
    adopted = await adoption.adopt_head(
        async_session, RepoConfigSource(api.contents), project, sha=PUSHED_SHA, actor=PERSON
    )
    assert adopted.sync is None and adopted.sync_error
    assert len(await _adoptions(async_session, project)) == 1


DASH = "http://api.test"
EMAIL = "p@leaninnovationlabs.com"


async def _adopt(
    session: AsyncSession, *, signed_in: bool, bearer: bool, origin: str | None = DASH
) -> tuple[httpx.Response, Project]:
    settings = make_settings(auth=AuthSettings(external_url=DASH))
    app, api = app_with(session, settings=settings)
    pushed_on_base(api)
    project = await project_in(session)
    headers = {"Origin": origin} if origin else {}
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    async with client_for(app, base_url=DASH) as client:
        if signed_in:
            await ensure_member(session, EMAIL)
            client.cookies.set(
                SESSION_COOKIE,
                mint_session(
                    EMAIL,
                    verified=True, api_token=TOKEN,
                    now=datetime.now(UTC),
                    max_age=settings.auth.session_max_age,
                ),
            )
        response = await client.post(
            f"/api/projects/{project.id}/config-branch/adoptions",
            json={"sha": PUSHED_SHA},
            headers=headers,
        )
    return response, project


async def test_a_session_with_the_bearer_alongside_adopts_as_the_person(
    async_session: AsyncSession,
) -> None:
    """The dashboard sends the bearer whenever one is stored; the person still signs for it."""
    response, project = await _adopt(async_session, signed_in=True, bearer=True)
    assert response.status_code == 201, response.text
    assert response.json()["adopted"] is True
    assert response.json()["sync"]["sha"] == PUSHED_SHA
    [details] = await _adoptions(async_session, project)
    assert details["requested_by"] == EMAIL


async def test_the_shared_bearer_alone_cannot_adopt(async_session: AsyncSession) -> None:
    """It names no one, and an adoption is a person vouching for a commit."""
    response, project = await _adopt(async_session, signed_in=False, bearer=True)
    assert response.status_code == 403
    assert await _adoptions(async_session, project) == []


async def test_an_adoption_from_another_origin_is_refused(async_session: AsyncSession) -> None:
    response, project = await _adopt(
        async_session, signed_in=True, bearer=False, origin="https://evil.example"
    )
    assert response.status_code == 403
    assert await _adoptions(async_session, project) == []
