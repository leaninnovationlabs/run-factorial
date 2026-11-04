"""The dashboard's config commit, when the ticket source is GitLab."""

from __future__ import annotations

from orchestrator.github.files_pr import ConfigBranchExists
from orchestrator.gitlab.files_commit import commit_config_files, seed_config_branch


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def get(self, url: str, **params: object) -> dict[str, str]:
        self.calls.append(("GET", url, params))
        return {"id": "a" * 40}

    async def paginate(self, url: str, **params: object) -> list[object]:
        self.calls.append(("GET", url, params))
        return []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: object = None,
        json: object = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, json))
        return {"id": "b" * 40, "parent_ids": ["a" * 40]}


async def test_commit_config_files_writes_the_branch_and_records_the_sha() -> None:
    client = _Client()
    recorded: list[str] = []

    async def record(sha: str) -> None:
        recorded.append(sha)

    sha = await commit_config_files(
        client,  # type: ignore[arg-type]
        "group/app",
        "factorial",
        "a" * 40,
        {".factorial/processes/process-p.yaml": "name: p\n"},
        "Update the p process from the Factorial dashboard",
        record,
    )

    assert sha == "b" * 40
    assert recorded == ["b" * 40]
    commit = client.calls[-1][2]
    assert isinstance(commit, dict)
    assert commit["branch"] == "factorial"
    assert commit["actions"] == [
        {
            "action": "create",
            "file_path": ".factorial/processes/process-p.yaml",
            "content": "name: p\n",
        }
    ]


class _SeedClient:
    def __init__(self, *, trees: list[object] | None = None, exists: bool = False) -> None:
        self.trees = trees if trees is not None else [{"name": ".factorial", "type": "tree"}]
        self.exists = exists
        self.calls: list[tuple[str, str, object]] = []

    async def get(self, url: str, **params: object) -> object:
        self.calls.append(("GET", url, params))
        if url.endswith("/repository/branches/factorial"):
            if self.exists or any(call[0] == "POST" for call in self.calls[:-1]):
                return {"commit": {"id": "c" * 40}}
            from orchestrator.gitlab.client import GitLabError

            raise GitLabError("missing", status_code=404)
        if url.endswith("/projects/group%2Fapp"):
            return {"default_branch": "main"}
        if url.endswith("/repository/commits/main"):
            return {"id": "a" * 40}
        return {}

    async def paginate(self, url: str, **params: object) -> list[object]:
        self.calls.append(("GET", url, params))
        return list(self.trees)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: object = None,
        json: object = None,
    ) -> object:
        self.calls.append((method, path, params or json))
        return {"id": "b" * 40}


async def test_seed_reuses_the_default_head_when_config_is_already_there() -> None:
    client = _SeedClient()
    recorded: list[str] = []

    async def record(sha: str) -> None:
        recorded.append(sha)

    sha = await seed_config_branch(
        client,  # type: ignore[arg-type]
        "group/app",
        "factorial",
        (".factorial", ".adlc"),
        record,
    )

    assert sha == "a" * 40
    assert recorded == ["a" * 40]
    created = (
        "POST",
        "/projects/group%2Fapp/repository/branches",
        {"branch": "factorial", "ref": "a" * 40},
    )
    assert created in [(method, path, payload) for method, path, payload in client.calls]


async def test_seed_empty_writes_a_readme_on_the_new_branch() -> None:
    client = _SeedClient(trees=[])
    recorded: list[str] = []

    async def record(sha: str) -> None:
        recorded.append(sha)

    sha = await seed_config_branch(
        client,  # type: ignore[arg-type]
        "group/app",
        "factorial",
        (".factorial", ".adlc"),
        record,
        empty=True,
    )

    assert sha == "c" * 40
    assert recorded == ["c" * 40]
    create = next(call for call in client.calls if call[0] == "POST" and "README.md" in call[1])
    assert isinstance(create[2], dict)
    assert create[2]["start_branch"] == "main"
    assert create[2]["branch"] == "factorial"


async def test_seed_refuses_an_existing_branch() -> None:
    client = _SeedClient(exists=True)
    try:
        await seed_config_branch(
            client,  # type: ignore[arg-type]
            "group/app",
            "factorial",
            (".factorial",),
            _noop,
        )
    except ConfigBranchExists:
        return
    raise AssertionError("expected ConfigBranchExists")


async def _noop(sha: str) -> None:
    return None
