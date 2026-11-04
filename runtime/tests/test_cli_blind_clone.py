"""The host CLI adapters' blind arm, `checkout.prepare_blind_clone`, called directly: real git
against a `file://` origin, so the clone shape decides what the workspace holds and the
host-side gates judge a real repository. Nothing is spawned: every spawn refuses the lane, so
this file is its only proof.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from host_session_support import REPO, OriginCli, git

from runtime.adapters._cli.checkout import prepare_blind_clone
from runtime.adapters._cli.environment import blind_slug_from_base, build_hidden_globs, git_env
from runtime.adapters._cli.paths import WORKTREE_DIRNAME
from runtime.adapters._cli.process import CommandResult
from runtime.contract import (
    AgentPrompts,
    IsolationError,
    RunSpec,
    SpawnError,
    SpawnReason,
    Workspace,
)
from runtime.workspace import assert_isolated, assert_no_retained_credential

SLUG = "1-x"
SPEC_BRANCH = f"spec/{SLUG}"
SECRETS = {"GH_TOKEN": "ghs_blind"}


def blind_origin(folder: Path) -> Path:
    """A bare origin holding what a blind clone must not bring along: a `tests/<slug>` branch
    with the acceptance material, and a `contract/<slug>` tag on the spec branch's own history,
    which only `--no-tags` keeps out."""
    bare = folder / "origin.git"
    git("init", "--bare", "-b", "main", str(bare), cwd=folder)
    seed = folder / "seed"
    git("clone", str(bare), str(seed), cwd=folder)
    (seed / "README.md").write_text("widgets\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "start", cwd=seed)
    git("push", "origin", "HEAD:main", cwd=seed)
    git("checkout", "-b", SPEC_BRANCH, cwd=seed)
    (seed / "SPEC.md").write_text("the spec\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "spec", cwd=seed)
    git("tag", f"contract/{SLUG}", cwd=seed)
    git("checkout", "-b", f"tests/{SLUG}", cwd=seed)
    acceptance = seed / "acceptance" / SLUG
    acceptance.mkdir(parents=True)
    (acceptance / "test_widget.py").write_text("def test_widget() -> None: ...\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "acceptance material the builder must never see", cwd=seed)
    git("push", "origin", SPEC_BRANCH, f"tests/{SLUG}", f"contract/{SLUG}", cwd=seed)
    return bare


class _Sabotaging(OriginCli):
    """A clone whose wipe failed: a tag is reachable right after it."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        result = await super().run(
            program, *args, env=env, cwd=cwd,
            inherit_host_env=inherit_host_env,
        )
        if program == self.git_bin and "clone" in args and result.ok:
            git("tag", "contract/stale", cwd=Path(args[-1]))
        return result


class _Authless(OriginCli):
    """The remote refuses the read, as a private repo answers a token-less probe under
    GIT_TERMINAL_PROMPT=0."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if program == self.git_bin and "ls-remote" in args:
            return CommandResult(
                (program, *args),
                128,
                "",
                "fatal: could not read Username for 'https://github.com': terminal prompts "
                "disabled",
            )
        return await super().run(
            program, *args, env=env, cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return blind_origin(tmp_path)


def blind_spec(base: str = SPEC_BRANCH) -> RunSpec:
    return RunSpec(
        agent="impl-agent",
        ticket="GH-1",
        workspace=Workspace(
            repo=REPO,
            base=base,
            branch=f"impl/{SLUG}-{{slug}}",
            isolation="blind",
            credential="none",
        ),
        prompts=AgentPrompts(system="You build.", instructions="Build it."),
        model="claude-haiku-4-5",
        max_turns=1,
    )


def env_of(secrets: Mapping[str, str]) -> dict[str, str]:
    return {**git_env(secrets), **secrets}


async def clone(
    tmp_path: Path, cli: OriginCli, *, base: str = SPEC_BRANCH, secrets: Mapping[str, str] = SECRETS
) -> Path:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return await prepare_blind_clone(cli, run_dir, blind_spec(base), env_of(secrets), secrets)


def _lines(worktree: Path, *args: str) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.split()


async def test_a_blind_clone_holds_one_branch_and_passes_the_isolation_gate(
    tmp_path: Path, origin: Path
) -> None:
    worktree = await clone(tmp_path, OriginCli(Path("/nonexistent"), origin))

    assert_isolated(worktree, build_hidden_globs((), SLUG))
    assert _lines(worktree, "tag") == []
    refs = _lines(worktree, "for-each-ref", "--format=%(refname)")
    assert refs and all(ref.endswith(f"/{SPEC_BRANCH}") for ref in refs), refs
    assert not (worktree / "acceptance").exists()


async def test_a_blind_clone_retains_no_credential(tmp_path: Path, origin: Path) -> None:
    worktree = await clone(tmp_path, OriginCli(Path("/nonexistent"), origin))

    assert_no_retained_credential(worktree, SECRETS)
    assert "ghs_blind" not in (worktree / ".git" / "config").read_text()


async def test_a_missing_spec_branch_refuses_with_workspace(tmp_path: Path, origin: Path) -> None:
    with pytest.raises(SpawnError) as raised:
        await clone(tmp_path, OriginCli(Path("/nonexistent"), origin), base="spec/never-pushed")

    assert raised.value.reason is SpawnReason.WORKSPACE
    assert not (tmp_path / "runs" / "r1" / WORKTREE_DIRNAME).exists()


async def test_a_sabotaged_clone_raises_isolation_error_before_any_agent(
    tmp_path: Path, origin: Path
) -> None:
    with pytest.raises(IsolationError):
        await clone(tmp_path, _Sabotaging(Path("/nonexistent"), origin))


async def test_a_tokenless_read_the_remote_refuses_is_a_credential_failure(
    tmp_path: Path, origin: Path
) -> None:
    with pytest.raises(SpawnError) as raised:
        await clone(tmp_path, _Authless(Path("/nonexistent"), origin), secrets={})

    assert raised.value.reason is SpawnReason.CREDENTIAL


def test_a_blind_base_that_is_not_a_spec_branch_is_refused() -> None:
    with pytest.raises(SpawnError) as raised:
        blind_slug_from_base("main")

    assert raised.value.reason is SpawnReason.WORKSPACE
