"""`scripts/migrate-vendor-keys.py`'s planning and apply logic against a fake API client: which
runtime, which credential name, which projects lose their copy, which keep it because open
tickets still read it, in what order — and that the value never reaches stdout."""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import pytest

from orchestrator.factorial.runtimes import credential_name_for
from tests.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "migrate-vendor-keys.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("migrate_vendor_keys", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolve the module's postponed annotations through it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeApi:
    """Routes the script reads, from plain dicts; every write is recorded in order."""

    def __init__(
        self,
        runtimes: list[dict[str, Any]],
        projects: dict[int, dict[str, Any]],
    ) -> None:
        self.runtimes = runtimes
        # id -> {name, runtime: slug the LATEST version names or None, reading: open tickets
        # pinned to a snapshot that still reads the project copy, secrets: [...]}
        self.projects = projects
        self.writes: list[tuple[str, str]] = []

    def get(self, path: str) -> Any:
        if path == "/api/orgs/default/runtimes":
            return self.runtimes
        if path == "/api/orgs/default/projects":
            return [{"id": pid, "name": p["name"]} for pid, p in self.projects.items()]
        parts = path.split("/")
        assert len(parts) == 4, f"unexpected GET {path}"
        pid = int(parts[3])
        project = self.projects[pid]
        provided = []
        if project["runtime"]:
            (row,) = [r for r in self.runtimes if r["slug"] == project["runtime"]]
            provided.append(
                {
                    "name": credential_name_for(row["type"]),
                    "runtime_slug": row["slug"],
                    "stored": False,
                    "open_tickets_reading_copy": project.get("reading", 0),
                }
            )
        return {
            "id": pid,
            "name": project["name"],
            "secret_names": sorted(project["secrets"]),
            "runtime_provided_secrets": provided,
        }

    def put(self, path: str, body: dict[str, Any]) -> Any:
        self.writes.append(("PUT", path))
        return {"name": path.rsplit("/", 1)[-1]}

    def delete(self, path: str) -> None:
        self.writes.append(("DELETE", path))
        pid = int(path.split("/")[3])
        self.projects[pid]["secrets"].remove(path.rsplit("/", 1)[-1])


@pytest.fixture
def api() -> FakeApi:
    return FakeApi(
        runtimes=[
            {"id": 4, "slug": "codex-team", "type": "codex_api"},
            {"id": 5, "slug": "claude-vm", "type": "claude_api"},
        ],
        projects={
            1: {  # bound on its latest version, holds a copy: deleted
                "name": "widgets",
                "runtime": "codex-team",
                "secrets": ["OPENAI_API_KEY", "PRODUCT_DOCS_TOKEN"],
            },
            2: {  # bound, but holds no copy: nothing to delete
                "name": "gadgets",
                "runtime": "codex-team",
                "secrets": ["PRODUCT_DOCS_TOKEN"],
            },
            3: {  # the latest version names no runtime: untouched
                "name": "sprockets",
                "runtime": None,
                "secrets": ["OPENAI_API_KEY"],
            },
            4: {  # bound to a different runtime: untouched even though it holds the same name
                "name": "gizmos",
                "runtime": "claude-vm",
                "secrets": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
            },
            5: {  # never synced
                "name": "fresh",
                "runtime": None,
                "secrets": ["OPENAI_API_KEY"],
            },
            6: {  # bound and holding a copy, but two open tickets sit on an older snapshot
                "name": "midflight",
                "runtime": "codex-team",
                "reading": 2,
                "secrets": ["OPENAI_API_KEY"],
            },
        },
    )


def test_the_plan_names_the_credential_from_the_type_and_only_latest_bound_projects(
    api: FakeApi,
) -> None:
    script = _load()
    (move,) = script.plan(api, [("codex-team", "CODEX_KEY")])
    assert move.runtime_id == 4
    assert move.credential_name == "OPENAI_API_KEY"
    assert move.env_name == "CODEX_KEY"
    assert move.bound == [(1, "widgets"), (2, "gadgets"), (6, "midflight")]
    assert move.to_delete == [(1, "widgets")]
    assert move.held == [(6, "midflight", 2)]
    assert api.writes == []  # planning never writes


def test_a_slug_nobody_created_is_refused(api: FakeApi) -> None:
    script = _load()
    with pytest.raises(SystemExit, match="ghost"):
        script.plan(api, [("ghost", "KEY")])


def test_apply_stores_the_runtime_key_before_deleting_any_project_copy(
    api: FakeApi, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load()
    value = "sk-never-printed"
    moves = script.plan(api, [("codex-team", "CODEX_KEY"), ("claude-vm", "CLAUDE_KEY")])

    script.apply(api, moves, {"CODEX_KEY": value, "CLAUDE_KEY": "sk-ant-also-never"})
    print(script.describe(moves, applied=True))

    assert api.writes == [
        ("PUT", "/api/runtimes/4/credentials/OPENAI_API_KEY"),
        ("DELETE", "/api/projects/1/secrets/OPENAI_API_KEY"),
        ("PUT", "/api/runtimes/5/credentials/ANTHROPIC_API_KEY"),
        ("DELETE", "/api/projects/4/secrets/ANTHROPIC_API_KEY"),
    ]
    assert api.projects[1]["secrets"] == ["PRODUCT_DOCS_TOKEN"]
    assert api.projects[3]["secrets"] == ["OPENAI_API_KEY"]  # not bound on its latest version
    assert api.projects[6]["secrets"] == ["OPENAI_API_KEY"]  # open tickets still read it
    out = capsys.readouterr().out
    assert value not in out and "sk-ant" not in out
    assert "codex-team: stored OPENAI_API_KEY from $CODEX_KEY; 3 project(s) bound, 1" in out
    assert "widgets" in out
    assert "midflight (project 6): kept, 2 open ticket(s)" in out


def test_main_is_a_dry_run_unless_told_to_apply(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load()
    monkeypatch.setattr(script, "HttpApi", lambda base_url, token: api)
    monkeypatch.setenv("FACTORIAL_API_TOKEN", "tok")
    monkeypatch.setenv("CODEX_KEY", "sk-dry")

    argv = ["--api-url", "http://x", "--runtime", "codex-team", "--from-env", "CODEX_KEY"]
    assert script.main(argv) == 0
    out = capsys.readouterr().out
    assert api.writes == []
    assert "dry run" in out and "would store OPENAI_API_KEY" in out
    assert "sk-dry" not in out


def test_main_refuses_an_unset_key_variable_before_touching_the_api(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load()
    monkeypatch.setattr(script, "HttpApi", lambda base_url, token: api)
    monkeypatch.setenv("FACTORIAL_API_TOKEN", "tok")
    monkeypatch.delenv("CODEX_KEY", raising=False)

    with pytest.raises(SystemExit):
        script.main(["--api-url", "http://x", "--runtime", "codex-team", "--from-env", "CODEX_KEY"])
    assert api.writes == []
