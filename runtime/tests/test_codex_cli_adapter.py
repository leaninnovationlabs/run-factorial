"""Tests for the `codex_cli` adapter's refusals and registry entry.

No test needs the real `codex`, the real `git` or a real subprocess: the CLI seam (`_Cli`) is
subclassed and answers every command at once. A session run, with its credentials, sandbox and
state, is `test_codex_cli_interactive.py`'s.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from host_session_support import short_root
from pydantic import SecretStr

from runtime.adapters import create_adapter
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.process import CommandResult
from runtime.adapters.codex_cli import session as codex_session
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.auth import AUTH_MODE_SUBSCRIPTION, SECRET_AUTH_JSON
from runtime.adapters.codex_cli.cli import ENV_API_KEY, PINNED_VERSION, _Cli
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    RunSpec,
    SpawnError,
    SpawnReason,
    UnknownRunError,
    Workspace,
)

TOKEN = "ghs_verysecrettoken"
API_KEY = "sk-openai-alsosecret"


def make_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "secrets": {ENV_GH_TOKEN: SecretStr(TOKEN), ENV_API_KEY: SecretStr(API_KEY)},
        "model": "gpt-5.1-codex",
        "max_turns": 40,
    }
    return RunSpec.model_validate(defaults | overrides)


class FakeCli(_Cli):
    """The CLI seam with no subprocess behind it: every command succeeds with no output."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        return CommandResult(argv=(program, *args), returncode=0, stdout="", stderr="")


def _no_run_dirs(root: Path) -> bool:
    return not any(p.is_dir() for p in (root / "runs").glob("*"))


# --------------------------------------------------------------------------- refusals


async def test_no_local_root_refuses_to_spawn() -> None:
    """The guard that makes the blast radius knowable. No default, ever (decision 34)."""
    adapter = CodexCliAdapter(cli=FakeCli())
    with pytest.raises(AdapterUnavailable, match="LOCAL_ROOT"):
        await adapter.spawn(make_spec())


async def test_a_spec_without_the_openai_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STORE, not the environment: a run pointed at a fresh CODEX_HOME cannot see any
    operator's own login, so the stored key is the only auth that works (decision 34).

    The host key is EXPORTED here on purpose. Without it the test refuses for want of a key
    anywhere and proves nothing about which source counts -- and a developer box with the
    smoke's own `.env` sourced is exactly where a host fallback would hide. claude_cli's
    guard carries the history: accepting an exported key let a project with an empty secret
    table run anyway.
    """
    monkeypatch.setenv(ENV_API_KEY, "sk-proj-exported-on-the-host")
    adapter = CodexCliAdapter(local_root=tmp_path / "root", cli=FakeCli())
    with pytest.raises(SpawnError) as excinfo:
        await adapter.spawn(make_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)}))
    assert excinfo.value.reason is SpawnReason.CREDENTIAL
    assert _no_run_dirs(tmp_path / "root")


async def test_the_subscription_refuses_when_the_operator_is_not_logged_in(
    tmp_path: Path,
) -> None:
    """The mode's own missing-credential case. It names the file and `codex login`, because the
    fix is a command the operator runs once, not a secret they store."""
    adapter = CodexCliAdapter(
        local_root=tmp_path / "root",
        cli=FakeCli(),
        auth_mode=AUTH_MODE_SUBSCRIPTION,
        subscription_auth_file=tmp_path / "operator" / "auth.json",
    )
    with pytest.raises(SpawnError) as excinfo:
        await adapter.spawn(make_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)}))
    assert excinfo.value.reason is SpawnReason.CREDENTIAL
    assert "codex login" in str(excinfo.value), "the developer-machine fix"
    assert SECRET_AUTH_JSON in str(excinfo.value), "the stored-login fix"
    assert _no_run_dirs(tmp_path / "root")


async def test_status_of_an_unknown_run_raises(tmp_path: Path) -> None:
    adapter = CodexCliAdapter(local_root=tmp_path / "root", cli=FakeCli())
    with pytest.raises(UnknownRunError):
        await adapter.status("no-such-run")


# --------------------------------------------------------------------------- the pin


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A root short enough for a tmux socket, which `spawn_refusal` checks."""
    yield from short_root("f10x-")


async def test_a_drifted_codexs_spawn_is_refused_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_root: Path
) -> None:
    """The worker asks `spawn_refusal` before every spawn, and a rollout is translated only as
    observed at the pin, so drift refuses the spawn and names the install that fixes it."""
    drifted = "codex-cli 0.999.0"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, version in (("codex", drifted), ("tmux", "tmux 3.5a")):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\necho '{version}'\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    # The stand-in counts as the native binary: a test cannot build one.
    monkeypatch.setattr(codex_session, "_is_script", lambda binary: False)
    adapter = CodexCliAdapter(local_root=socket_root, cli=FakeCli())

    refusal = await adapter.spawn_refusal()

    assert refusal is not None
    assert repr(drifted) in refusal
    assert f"install codex {PINNED_VERSION} on the worker's host" in refusal


# --------------------------------------------------------------------------- registry


async def test_the_registry_builds_this_adapter_by_kind(tmp_path: Path) -> None:
    adapter = create_adapter("codex_cli", local_root=tmp_path / "root")
    assert isinstance(adapter, CodexCliAdapter)
