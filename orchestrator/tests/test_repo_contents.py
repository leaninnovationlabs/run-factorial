"""`RepoContents` — the boundary between githubkit's contents union and text at a pinned sha.

No network. The fakes stand in for `api.rest.repos` the same way `test_admission_candidates.py`
fakes `async_list_for_repo`, and they count calls, because the two things that fail silently if
they regress are both about call counts: a full sha must not cost a round trip, and a repeated
read of the same `(repo, path, sha)` must not cost a second one.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit.exception import RequestFailed

from orchestrator.github.contents import (
    COMPARE_FILES_MAX,
    ContentsNotFound,
    DirEntry,
    MergeProvenance,
    RepoContents,
    RepoNotVisible,
    TreeEntry,
)

SHA = "a" * 40


# --- githubkit stand-ins ------------------------------------------------------------------------
#
# Duck types, not the real models: `contents.py` narrows on the `type` discriminant and on
# list-ness, never with `isinstance` against a githubkit class, so these are enough.


@dataclass
class _File:
    content: str
    encoding: str = "base64"
    type: str = "file"


@dataclass
class _DirItem:
    name: str
    path: str
    type: str


@dataclass
class _Symlink:
    type: str = "symlink"


@dataclass
class _Repo:
    default_branch: str


@dataclass
class _Commit:
    sha: str


@dataclass
class _Response:
    parsed_data: Any


def _b64(text: str) -> str:
    # GitHub wraps its base64 in newlines; keep that in the fake so decoding is really exercised.
    raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\n".join(raw[i : i + 60] for i in range(0, len(raw), 60)) + "\n"


def _not_found() -> RequestFailed:
    return request_failed(404)


def request_failed(status: int) -> RequestFailed:
    request = httpx.Request("GET", "https://api.github.com/repos/acme/widgets/contents/x")
    response = httpx.Response(status, request=request, json={"message": "Not Found"})
    return RequestFailed(_RawResponse(request, response))  # type: ignore[arg-type]


@dataclass
class _RawResponse:
    """The two attributes `RequestFailed.__init__` reads, plus the one `contents.py` reads."""

    raw_request: httpx.Request
    raw_response: httpx.Response

    @property
    def status_code(self) -> int:
        return self.raw_response.status_code


class _FakeReposApi:
    def __init__(
        self,
        contents: dict[str, Any] | None = None,
        default_branch: str = "main",
        head_sha: str = "b" * 40,
        commit_failure: int | None = None,
        repo_failure: int | None = None,
    ) -> None:
        self._contents = contents or {}
        self._commit_failure = commit_failure
        self._repo_failure = repo_failure
        self._default_branch = default_branch
        self._head_sha = head_sha
        self.content_calls: list[tuple[str, str, str, str]] = []
        self.commit_calls: list[str] = []
        self.get_calls: list[str] = []

    async def async_get_content(self, owner: str, repo: str, path: str, *, ref: str) -> _Response:
        self.content_calls.append((owner, repo, path, ref))
        if path not in self._contents:
            raise _not_found()
        return _Response(self._contents[path])

    async def async_get_commit(self, owner: str, repo: str, ref: str) -> _Response:
        self.commit_calls.append(ref)
        if self._commit_failure is not None:
            raise request_failed(self._commit_failure)
        return _Response(_Commit(sha=self._head_sha))

    async def async_get(self, owner: str, repo: str) -> _Response:
        self.get_calls.append(f"{owner}/{repo}")
        if self._repo_failure is not None:
            raise request_failed(self._repo_failure)
        return _Response(_Repo(default_branch=self._default_branch))


class _FakePullsApi:
    """Closed PRs into a base, served raw in pages, and each PR's issue events. Doubles as the
    `issues` API, which is where the `merged` event lives."""

    def __init__(self, pages: list[list[dict[str, Any]]], events: dict[int, Any]) -> None:
        self._pages = pages
        self._events = events
        self.list_calls: list[dict[str, Any]] = []

    async def async_list(self, owner: str, repo: str, **kwargs: Any) -> Any:
        self.list_calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(self._pages[kwargs["page"] - 1]).encode())

    async def async_list_events(self, owner: str, repo: str, number: int, **_: Any) -> Any:
        return SimpleNamespace(content=json.dumps(self._events.get(number, [])).encode())


@dataclass
class _TreeItem:
    path: str
    sha: str
    type: str = "blob"
    size: int | None = None


@dataclass
class _Tree:
    tree: list[_TreeItem]
    truncated: bool = False


@dataclass
class _Blob:
    content: str
    encoding: str = "base64"


class _FakeGitApi:
    def __init__(
        self,
        trees: dict[str, _Tree] | None = None,
        blobs: dict[str, str] | None = None,
        blob_failure: int | None = None,
    ) -> None:
        self._trees = trees or {}
        self._blobs = blobs or {}
        self._blob_failure = blob_failure
        self.tree_calls: list[str] = []
        self.blob_calls: list[str] = []

    async def async_get_tree(self, owner: str, repo: str, sha: str, *, recursive: str) -> _Response:
        self.tree_calls.append(sha)
        return _Response(self._trees[sha])

    async def async_get_blob(self, owner: str, repo: str, sha: str) -> _Response:
        self.blob_calls.append(sha)
        if self._blob_failure is not None:
            raise request_failed(self._blob_failure)
        if sha not in self._blobs:
            raise _not_found()
        return _Response(_Blob(content=_b64(self._blobs[sha])))


@dataclass
class _FakeClient:
    """Stands in for `GitHubClient`; only `api()` is reached."""

    repos: _FakeReposApi = field(default_factory=_FakeReposApi)
    pulls: _FakePullsApi | None = None
    git: _FakeGitApi | None = None

    async def api(self, _repo: str) -> Any:
        rest = type(
            "_Rest",
            (),
            {"repos": self.repos, "pulls": self.pulls, "issues": self.pulls, "git": self.git},
        )()
        return type("_Api", (), {"rest": rest})()


def _contents(**kwargs: Any) -> tuple[RepoContents, _FakeReposApi]:
    repos = _FakeReposApi(**kwargs)
    client = _FakeClient(repos)
    return RepoContents(client), repos  # type: ignore[arg-type]


# --- refs ---------------------------------------------------------------------------------------


async def test_a_full_sha_resolves_without_an_api_call() -> None:
    """The common case: config already pinned. Paying a round trip to learn nothing would be
    one wasted call per agent, per tick."""
    contents, repos = _contents()
    assert await contents.resolve_ref("acme/widgets", SHA) == SHA
    assert repos.commit_calls == []


async def test_a_branch_name_resolves_through_get_commit() -> None:
    contents, repos = _contents(head_sha="c" * 40)
    assert await contents.resolve_ref("acme/widgets", "main") == "c" * 40
    assert repos.commit_calls == ["main"]


async def test_a_short_sha_is_not_mistaken_for_a_concrete_one() -> None:
    contents, repos = _contents(head_sha="d" * 40)
    assert await contents.resolve_ref("acme/widgets", "a1b2c3d") == "d" * 40
    assert repos.commit_calls == ["a1b2c3d"]


@pytest.mark.parametrize("status", [404, 422])
async def test_a_ref_github_does_not_know_is_contents_not_found(status: int) -> None:
    """A deleted branch, which a sync form can still offer. The routes answer `ContentsNotFound`
    with a 422 that names the ref; a raw GitHub failure reached the client as a 500."""
    contents, _ = _contents(commit_failure=status)
    with pytest.raises(ContentsNotFound, match="claude-subscription-canary"):
        await contents.resolve_ref("acme/widgets", "claude-subscription-canary")


async def test_a_github_outage_is_not_mistaken_for_a_missing_ref() -> None:
    contents, _ = _contents(commit_failure=502)
    with pytest.raises(RequestFailed):
        await contents.resolve_ref("acme/widgets", "main")


async def test_default_branch_is_read_from_the_repo() -> None:
    contents, repos = _contents(default_branch="trunk")
    assert await contents.default_branch("acme/widgets") == "trunk"
    assert repos.get_calls == ["acme/widgets"]


async def test_a_repo_github_will_not_show_is_repo_not_visible() -> None:
    """A private repo outside the App's installation answers 404. It reached the dashboard as a
    500 with no cause; the error has to name the repo and the fix, and retrying cannot help."""
    contents, _ = _contents(repo_failure=404)
    with pytest.raises(RepoNotVisible, match="cannot see acme/widgets"):
        await contents.default_branch("acme/widgets")


async def test_a_github_outage_is_not_mistaken_for_an_invisible_repo() -> None:
    contents, _ = _contents(repo_failure=502)
    with pytest.raises(RequestFailed):
        await contents.default_branch("acme/widgets")


_MERGE = "m" * 40


@pytest.mark.parametrize(
    ("merged_at", "actor", "expected"),
    [
        ("2026-09-27T00:00:00Z", {"login": "operator", "type": "User"}, (9, "operator", True)),
        ("2026-09-27T00:00:00Z", {"login": "app[bot]", "type": "Bot"}, (9, "app[bot]", False)),
        (None, {"login": "operator", "type": "User"}, (None, None, False)),
    ],
    ids=["human_merged", "bot_merged", "closed_unmerged"],
)
async def test_merge_provenance_finds_the_pr_whose_merge_event_names_the_sha_on_any_page(
    merged_at: str | None, actor: dict[str, str], expected: tuple[Any, ...]
) -> None:
    filler = [{"number": n, "merged_at": "x"} for n in range(100, 200)]
    events = {
        n: [{"event": "merged", "commit_id": "f" * 40, "actor": actor}] for n in range(100, 200)
    }
    events[9] = [
        {"event": "closed", "commit_id": None, "actor": actor},
        {"event": "merged", "commit_id": _MERGE, "actor": actor},
    ]
    pulls = _FakePullsApi([filler, [{"number": 9, "merged_at": merged_at}]], events)
    contents = RepoContents(_FakeClient(pulls=pulls))  # type: ignore[arg-type]

    got = await contents.merge_provenance("acme/widgets", _MERGE, "factorial")
    assert got == MergeProvenance(*expected)
    assert [(c["base"], c["state"], c["page"]) for c in pulls.list_calls] == [
        ("factorial", "closed", 1),
        ("factorial", "closed", 2),
    ]


# --- reading files ------------------------------------------------------------------------------


async def test_read_text_decodes_base64() -> None:
    yaml = {".factorial/process.yaml": _File(_b64("kind: Process\n"))}
    contents, repos = _contents(contents=yaml)
    text = await contents.read_text("acme/widgets", ".factorial/process.yaml", SHA)
    assert text == "kind: Process\n"
    assert repos.content_calls == [("acme", "widgets", ".factorial/process.yaml", SHA)]


async def test_read_text_on_a_missing_file_raises_contents_not_found() -> None:
    contents, _ = _contents(contents={})
    with pytest.raises(ContentsNotFound) as excinfo:
        await contents.read_text("acme/widgets", ".factorial/nope.md", SHA)
    # The message carries the `owner/name@<sha7>:<path>` origin the config loader also prints.
    assert f"acme/widgets@{SHA[:7]}:.factorial/nope.md" in str(excinfo.value)


async def test_read_text_on_a_directory_is_rejected() -> None:
    listing = {".factorial": [_DirItem("process.yaml", ".factorial/x", "file")]}
    contents, _ = _contents(contents=listing)
    with pytest.raises(IsADirectoryError):
        await contents.read_text("acme/widgets", ".factorial", SHA)


async def test_read_text_on_a_symlink_is_rejected() -> None:
    """A dangling symlink has no content — returning its (absent) body would be silent nonsense."""
    contents, _ = _contents(contents={".factorial/link.md": _Symlink()})
    with pytest.raises(ContentsNotFound) as excinfo:
        await contents.read_text("acme/widgets", ".factorial/link.md", SHA)
    assert "is a symlink, not a file" in str(excinfo.value)


async def test_a_file_over_the_contents_api_limit_fails_loudly() -> None:
    """GitHub answers `encoding: "none"` with empty content above 1 MB. An empty prompt that
    looks legitimate is worse than a crash."""
    contents, _ = _contents(contents={".factorial/huge.md": _File(content="", encoding="none")})
    with pytest.raises(ValueError) as excinfo:
        await contents.read_text("acme/widgets", ".factorial/huge.md", SHA)
    message = str(excinfo.value)
    assert ".factorial/huge.md" in message
    assert "1048576" in message


async def test_an_unexpected_encoding_is_rejected() -> None:
    contents, _ = _contents(contents={".factorial/x.md": _File(content="hi", encoding="utf-8")})
    with pytest.raises(ValueError) as excinfo:
        await contents.read_text("acme/widgets", ".factorial/x.md", SHA)
    assert "unexpected content encoding 'utf-8'" in str(excinfo.value)


# --- listing directories --------------------------------------------------------------------


async def test_list_dir_returns_immediate_children() -> None:
    contents, _ = _contents(
        contents={
            ".factorial/agents": [
                _DirItem("plan-agent", ".factorial/agents/plan-agent", "dir"),
                _DirItem("README.md", ".factorial/agents/README.md", "file"),
            ]
        }
    )
    entries = await contents.list_dir("acme/widgets", ".factorial/agents", SHA)
    assert list(entries) == [
        DirEntry(name="plan-agent", path=".factorial/agents/plan-agent", type="dir"),
        DirEntry(name="README.md", path=".factorial/agents/README.md", type="file"),
    ]


async def test_list_dir_on_a_missing_directory_is_empty_not_an_error() -> None:
    """`agents/<n>/skills/` is optional — absent means "no skills", not a broken manifest."""
    contents, _ = _contents(contents={})
    assert await contents.list_dir("acme/widgets", ".factorial/agents/x/skills", SHA) == ()


async def test_list_dir_on_a_file_is_rejected() -> None:
    contents, _ = _contents(contents={".factorial/process.yaml": _File(_b64("kind: Process\n"))})
    with pytest.raises(NotADirectoryError):
        await contents.list_dir("acme/widgets", ".factorial/process.yaml", SHA)


# --- memoization ------------------------------------------------------------------------------


async def test_the_same_path_at_the_same_sha_costs_one_call() -> None:
    """A commit is immutable, so a hit can never be stale — this is why the cache spans ticks."""
    contents, repos = _contents(contents={".factorial/p.md": _File(_b64("hello"))})

    first = await contents.read_text("acme/widgets", ".factorial/p.md", SHA)
    second = await contents.read_text("acme/widgets", ".factorial/p.md", SHA)

    assert first == second == "hello"
    assert len(repos.content_calls) == 1


async def test_a_different_sha_is_a_different_cache_entry() -> None:
    other = "e" * 40
    contents, repos = _contents(contents={".factorial/p.md": _File(_b64("hello"))})

    await contents.read_text("acme/widgets", ".factorial/p.md", SHA)
    await contents.read_text("acme/widgets", ".factorial/p.md", other)

    assert [call[3] for call in repos.content_calls] == [SHA, other]


async def test_list_dir_is_memoized_separately_from_read_text() -> None:
    """Same key shape, different value type: one memo would collide file reads with listings."""
    listing = {".factorial/agents": [_DirItem("a", ".factorial/agents/a", "dir")]}
    contents, repos = _contents(contents=listing)
    await contents.list_dir("acme/widgets", ".factorial/agents", SHA)
    await contents.list_dir("acme/widgets", ".factorial/agents", SHA)
    assert len(repos.content_calls) == 1


async def test_a_missing_directory_result_is_cached_too() -> None:
    contents, repos = _contents(contents={})
    assert await contents.list_dir("acme/widgets", ".factorial/skills", SHA) == ()
    assert await contents.list_dir("acme/widgets", ".factorial/skills", SHA) == ()
    assert len(repos.content_calls) == 1


# --- trees and blobs (tech-stack decision 58) --------------------------------------------------

BLOB = "b" * 40


def _git_contents(git: _FakeGitApi) -> RepoContents:
    return RepoContents(_FakeClient(git=git))  # type: ignore[arg-type]


async def test_a_tree_lists_every_blob_with_its_size_once_per_commit() -> None:
    git = _FakeGitApi(
        trees={
            SHA: _Tree(
                [
                    _TreeItem("docs", "c" * 40, type="tree"),
                    _TreeItem("docs/summary.html", BLOB, size=12),
                ]
            )
        }
    )
    contents = _git_contents(git)

    first = await contents.tree("acme/widgets", SHA)
    again = await contents.tree("acme/widgets", SHA)

    assert first == again == (TreeEntry(path="docs/summary.html", sha=BLOB, size=12),)
    assert git.tree_calls == [SHA]


async def test_a_blob_is_read_as_bytes_by_its_sha() -> None:
    git = _FakeGitApi(blobs={BLOB: "<h1>héllo</h1>"})

    assert await _git_contents(git).read_blob("acme/widgets", BLOB) == "<h1>héllo</h1>".encode()


async def test_a_blob_github_does_not_have_is_contents_not_found() -> None:
    with pytest.raises(ContentsNotFound):
        await _git_contents(_FakeGitApi()).read_blob("acme/widgets", BLOB)


async def test_a_github_outage_reading_a_blob_is_not_mistaken_for_a_missing_one() -> None:
    with pytest.raises(RequestFailed):
        await _git_contents(_FakeGitApi(blob_failure=502)).read_blob("acme/widgets", BLOB)


# --- comparisons --------------------------------------------------------------------------------


class _FakeCompareApi(_FakeReposApi):
    def __init__(self, files: list[dict[str, Any]]) -> None:
        super().__init__()
        self._files = files
        self.compare_calls: list[str] = []

    async def async_compare_commits(self, owner: str, repo: str, basehead: str) -> Any:
        self.compare_calls.append(basehead)
        payload = {"status": "ahead", "total_commits": 1, "commits": [], "files": self._files}
        return SimpleNamespace(content=json.dumps(payload).encode())


def _compared(files: list[dict[str, Any]]) -> RepoContents:
    return RepoContents(_FakeClient(repos=_FakeCompareApi(files)))  # type: ignore[arg-type]


def _changed(path: str, blob: str = BLOB) -> dict[str, Any]:
    return {"filename": path, "status": "added", "additions": 1, "deletions": 0, "sha": blob}


async def test_a_comparison_carries_each_file_s_blob() -> None:
    contents = _compared([_changed("specs/12-x/spec.md", blob="d" * 40)])

    comparison = await contents.compare("acme/widgets", SHA, "c" * 40)

    assert [(f.path, f.blob) for f in comparison.files] == [("specs/12-x/spec.md", "d" * 40)]
    assert comparison.cut_off is False


async def test_a_comparison_at_github_s_file_limit_is_cut_off() -> None:
    at_limit = [_changed(f"f{n}.txt") for n in range(COMPARE_FILES_MAX)]

    assert (await _compared(at_limit).compare("acme/widgets", SHA, "c" * 40)).cut_off is True
    below = await _compared(at_limit[:-1]).compare("acme/widgets", SHA, "c" * 40)
    assert below.cut_off is False


async def test_a_fresh_tree_is_read_from_github_each_time() -> None:
    """The worker reads a whole commit's tree once per run, for every run: a memo would keep each
    one for the life of the process."""
    git = _FakeGitApi(trees={SHA: _Tree([_TreeItem("specs/spec.md", BLOB, size=12)])})
    contents = _git_contents(git)

    first = await contents.fresh_tree("acme/widgets", SHA)
    again = await contents.fresh_tree("acme/widgets", SHA)

    assert first == again == (TreeEntry(path="specs/spec.md", sha=BLOB, size=12),)
    assert git.tree_calls == [SHA, SHA]
