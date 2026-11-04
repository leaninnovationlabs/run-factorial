"""Host workspaces kept past a run's end (decision 54), against real git.

Both host adapters run a fake TUI in a real tmux here, under a short root, with every git call
real, against a `file://` origin: what the next attempt can check out, and what the reaper may
delete, is git's answer, not a fake's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from host_session_support import CodexOriginCli, OriginCli, reaches, server_of, short_root
from pydantic import SecretStr

from runtime.adapters._cli import checkout, host_session
from runtime.adapters._cli.checkout import _admin_dir_of
from runtime.adapters._cli.process import CommandResult
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.contract import AgentPrompts, RunAttachment, RunSpec, RunStatus, Workspace

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

Kind = Literal["claude_cli", "codex_cli"]
KINDS: tuple[Kind, ...] = ("claude_cli", "codex_cli")
#: What the scripted agent does in its worktree before it reports: check its branch out, and
#: then leave a rebase stopped on a conflict or a bisect started, remove its `.git` pointer, or
#: plant a hook.
Action = Literal["branch", "rebasing", "bisecting", "unpointed", "hook"]

_IDENTITY = {
    "GIT_AUTHOR_NAME": "kept-workspaces",
    "GIT_AUTHOR_EMAIL": "kept-workspaces@example.test",
    "GIT_COMMITTER_NAME": "kept-workspaces",
    "GIT_COMMITTER_EMAIL": "kept-workspaces@example.test",
}

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def _git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> CommandResult:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_IDENTITY, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        argv=("git", *args), returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "f").write_text("a\n")
    for args in (("init", "-q", "-b", "main"), ("add", "f"), ("commit", "-q", "-m", "seed")):
        assert _git(path, *args).ok
    return path


def _act(cwd: Path, env: Mapping[str, str], branch: str, action: Action, marker: Path) -> None:
    located = {key: env[key] for key in ("GIT_DIR", "GIT_WORK_TREE") if key in env}

    def git(*args: str) -> CommandResult:
        return _git(cwd, *args, env=located)

    assert git("checkout", "-q", "-b", branch).ok
    if action == "rebasing":
        for text, args in (("b", ()), ("c", ("checkout", "-q", "-b", f"{branch}-base", "HEAD~1"))):
            if args:
                assert git(*args).ok
            (cwd / "f").write_text(f"{text}\n")
            assert git("commit", "-q", "-am", text).ok
        assert git("checkout", "-q", branch).ok
        assert not git("rebase", f"{branch}-base").ok, "the rebase stops on its conflict"
    elif action == "bisecting":
        assert git("bisect", "start").ok
    elif action == "unpointed":
        (cwd / ".git").unlink()
    elif action == "hook":
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
        hook = common / "hooks" / "reference-transaction"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)


def _fake(folder: Path, kind: Kind) -> Path:
    """The fake TUI behind an absolute path, as `shutil.which` resolves the real one."""
    fake = Path(__file__).with_name(f"fake_{kind.removesuffix('_cli')}_tui.py")
    path = folder / "bin" / kind
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    path.chmod(0o755)
    return path


#: How `spawn` ends the run's session once its agent has acted: `live` leaves it at its prompt.
End = Literal["succeeded", "failed", "cancelled", "interrupted", "live"]


class _Host:
    """One host adapter over real git, running a fake TUI in a real tmux under `root`."""

    def __init__(self, kind: Kind, tmp_path: Path, root: Path) -> None:
        self.kind: Kind = kind
        self.root = root.resolve()
        self.clone = self.root / "repos" / "acme" / "widgets"
        origin = _repo(tmp_path / "origin")
        self.marker = origin.parent / "hook-ran"
        fake = _fake(tmp_path, kind)
        self.runs: list[str] = []
        if kind == "claude_cli":
            self.adapter: ClaudeCliAdapter | CodexCliAdapter = ClaudeCliAdapter(
                local_root=self.root, cli=OriginCli(fake, origin), poll=0.05
            )
        else:
            self.adapter = CodexCliAdapter(
                local_root=self.root, cli=CodexOriginCli(fake, origin), poll=0.05
            )

    async def spawn(
        self,
        ticket: int,
        *,
        end: End = "live",
        images: bool = False,
        action: Action = "branch",
    ) -> str:
        spec = RunSpec.model_validate(
            {
                "agent": "plan-agent",
                "ticket": f"GH-{ticket}",
                "workspace": Workspace(
                    repo="acme/widgets", base="main", branch=f"spec/{ticket}-{{slug}}"
                ),
                "prompts": AgentPrompts(system="You are the plan agent."),
                "secrets": {
                    "GH_TOKEN": SecretStr("ghs_keptWorkspaces"),
                    "ANTHROPIC_API_KEY": SecretStr("sk-ant-keptWorkspaces"),
                    "OPENAI_API_KEY": SecretStr("sk-keptWorkspaces"),
                },
                "model": "claude-haiku-4-5" if self.kind == "claude_cli" else "gpt-5.1-codex",
                "max_turns": 1,
                "attachments": (_image(),) if images else (),
            }
        )
        run_id = (await self.adapter.spawn(spec)).run_id
        self.runs.append(run_id)
        await reaches(self.adapter, run_id, RunStatus.IDLE)
        state = json.loads((self.run_dir(run_id) / "state.json").read_text())
        worktree = state["worktree"]
        env = {} if self.kind == "claude_cli" else {
            "GIT_DIR": state["git_dir"], "GIT_WORK_TREE": worktree
        }
        await asyncio.to_thread(
            _act, Path(worktree), env, f"spec/{ticket}-x", action, self.marker
        )
        if end == "succeeded":
            await self.adapter.finish(run_id)
        elif end == "failed":
            assert await server_of(self.root, run_id).type_text("go on\r")
            await reaches(self.adapter, run_id, RunStatus.FAILED)
        elif end == "cancelled":
            await self.adapter.cancel(run_id)
        elif end == "interrupted":
            await self.adapter.interrupt(run_id)
        return run_id

    async def close(self) -> None:
        """Every session this host spawned ended, and its supervisor drained."""
        for run_id in self.runs:
            await server_of(self.root, run_id).kill()
        await self.adapter.drain()

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def frees(self, branch: str, into: Path) -> CommandResult:
        """Whether a second worktree can check `branch` out, as the next attempt's agent must."""
        return _git(self.clone, "worktree", "add", "-q", str(into), branch)

    def registered(self) -> str:
        return _git(self.clone, "worktree", "list", "--porcelain").stdout

    def edit_state(self, run_id: str, **changes: object) -> None:
        """Rewrite a run's state file; a change of None drops the key, as an older writer did."""
        path = self.run_dir(run_id) / "state.json"
        state = json.loads(path.read_text())
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        path.write_text(json.dumps(state))


@pytest.fixture
def root() -> Iterator[Path]:
    yield from short_root("f243k-")


def _image() -> RunAttachment:
    return RunAttachment(
        name="image-1",
        media_type="image/png",
        content=PNG,
        source="ticket_body",
        origin="https://github.com/user-attachments/assets/1b92ad15",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("end", "action", "released"),
    [
        ("succeeded", "branch", True),
        ("failed", "branch", True),
        ("cancelled", "branch", True),
        ("interrupted", "branch", False),
        ("succeeded", "rebasing", True),
        ("succeeded", "bisecting", True),
    ],
)
async def test_a_kept_worktree_releases_its_branch_unless_the_run_was_interrupted(
    tmp_path: Path, root: Path, kind: Kind, end: End, action: Action, released: bool
) -> None:
    """The agent checks its branch out inside its worktree, and git refuses a branch to a second
    worktree while one holds it — through HEAD, or through a rebase or a bisect left open. Kept
    as it was, a finished run's worktree would stop the next attempt — a rework, or a retry
    after a failure — checking out the branch it pushed. An interrupted run keeps it: its session
    resumes on that branch."""
    host = _Host(kind, tmp_path, root)
    try:
        run_id = await host.spawn(12, end=end, action=action)

        assert await host.adapter.status(run_id) is RunStatus(end)
        worktree = host.run_dir(run_id) / "worktree"
        assert worktree.is_dir()
        assert str(worktree) in host.registered()
        added = host.frees("spec/12-x", tmp_path / "next")
        assert added.ok is released, added.stderr
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_the_host_reaper_removes_unwanted_workspaces_and_keeps_transcripts(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """A terminal run that `keep` does not name and that ended before the grace loses its
    worktree, config home and images; its `state.json` and stream stay, so its activity still
    reads. Kept, live and recent runs are untouched, and so is a run directory this adapter
    cannot read: the other host adapter may share the root."""
    host = _Host(kind, tmp_path, root)
    try:
        old = await host.spawn(1, end="interrupted", images=True)
        kept = await host.spawn(2, end="succeeded")
        recent = await host.spawn(3, end="succeeded")
        live = await host.spawn(4)
        foreign = host.root / "runs" / "foreign-run"
        (foreign / "config").mkdir(parents=True)
        (foreign / "state.json").write_text(json.dumps({"run_id": "foreign-run"}))
        # Spawns that failed before writing their state: one past the grace, and two perhaps
        # still checking out, one of them a large clone half an hour in.
        runs = host.root / "runs"
        failed, starting, cloning = runs / "failed-spawn", runs / "starting", runs / "cloning"
        for run_dir in (failed, starting, cloning):
            (run_dir / "config").mkdir(parents=True)
            (run_dir / "config" / "auth.json").write_text("{}")
        hour = timedelta(hours=1).total_seconds()
        for path in (failed / "config", failed):
            _age(path, 2 * hour)
        for path in (cloning / "config", cloning):
            _age(path, hour / 2)
        # Only `recent` is inside the grace, so each of the others is spared for its own reason.
        for run_dir in (host.run_dir(old), host.run_dir(kept), host.run_dir(live), foreign):
            _age(run_dir / "state.json", hour)
        assert (host.run_dir(old) / "attachments").is_dir(), "an interrupt keeps its images"

        reaped = await host.adapter.reap({kept}, retain=timedelta(days=30))

        assert reaped == (old,)
        gone = host.run_dir(old)
        for part in ("worktree", "config", "attachments"):
            assert not (gone / part).exists(), part
        assert (gone / "state.json").is_file() and (gone / "stdout.jsonl").is_file()
        assert await host.adapter.status(old) is RunStatus.INTERRUPTED
        assert [line async for line in host.adapter.logs(old)], "the transcript still reads"
        assert host.frees("spec/1-x", tmp_path / "next").ok, "its branch went with its worktree"
        for run_id in (kept, recent, live):
            run_dir = host.run_dir(run_id)
            assert (run_dir / "worktree").is_dir() and (run_dir / "config").is_dir(), run_id
            git_dir = json.loads((run_dir / "state.json").read_text()).get("git_dir")
            if git_dir is not None:
                # A pointerless worktree looks orphaned to `git worktree prune`; reaping one must
                # never take the others' administrative directories with it.
                assert await asyncio.to_thread(os.path.isdir, git_dir), run_id
        assert (foreign / "config").is_dir()
        assert not (failed / "config").exists()
        assert (starting / "config" / "auth.json").is_file()
        assert (cloning / "config" / "auth.json").is_file()
        assert await host.adapter.reap({kept}, retain=None) == (), "a second pass finds nothing"
    finally:
        await host.close()


RAW = ("signals.jsonl", "signals.mirror.jsonl", "session.jsonl")


async def _rework(host: _Host, ticket: int) -> tuple[str, str]:
    """A finished run and the rework's run that continued its session (decision 56), both past
    the grace. The rework worked in the first run's worktree and config home, so its own
    directory holds only its state, its stream, its turns and its raw session copies."""
    first = await host.spawn(ticket, end="succeeded")
    rework = await host.spawn(ticket + 100, end="succeeded")
    run_dir = host.run_dir(rework)
    await checkout.remove_workspace(host.root, run_dir=run_dir, repo="acme/widgets", blind=False)
    assert not (run_dir / "worktree").exists()
    shutil.rmtree(run_dir / "config")
    (run_dir / "turns" / "1.json").write_text("{}")
    for name in RAW:
        (run_dir / name).write_text("{}\n")
    host.edit_state(first, continued_by=rework)
    host.edit_state(
        rework,
        workspace_of=first,
        worktree=str(host.run_dir(first) / "worktree"),
    )
    for run_id in (first, rework):
        _age(host.run_dir(run_id) / "state.json", timedelta(hours=1).total_seconds())
    return first, rework


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_the_reaper_removes_a_rework_runs_raw_session_files(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """With no worktree, config home or images of its own, a rework's run kept its raw copies
    of the session for ever. Its state, stream and turns stay, as any reaped run's do."""
    host = _Host(kind, tmp_path, root)
    try:
        first, rework = await _rework(host, 1)

        reaped = await host.adapter.reap(set(), retain=None)

        run_dir = host.run_dir(rework)
        assert [name for name in RAW if (run_dir / name).exists()] == []
        assert set(reaped) == {first, rework}
        for part in ("state.json", "stdout.jsonl", "turns/1.json"):
            assert (run_dir / part).is_file(), part
        assert await host.adapter.reap(set(), retain=None) == (), "a second pass finds nothing"
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_a_kept_rework_run_keeps_its_raw_session_files(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """A parked rework is kept, and its resume reads its `signals.jsonl` from where it stopped.
    The keep set names the run and the workspace it works in."""
    host = _Host(kind, tmp_path, root)
    try:
        first, rework = await _rework(host, 1)

        reaped = await host.adapter.reap({rework, first}, retain=None)

        run_dir = host.run_dir(rework)
        assert [name for name in RAW if not (run_dir / name).is_file()] == []
        assert (host.run_dir(first) / "worktree").is_dir()
        assert reaped == ()
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_reaping_a_continued_rework_never_ends_the_session_continuing_it(
    tmp_path: Path, root: Path, kind: Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rework continued by a live one shares its workspace's config home with it. The reaper
    ends what holds a reaped run's config home, so sweeping that home would kill the live
    session."""
    host = _Host(kind, tmp_path, root)
    try:
        first, rework = await _rework(host, 1)
        live = await host.spawn(300)
        host.edit_state(
            live,
            workspace_of=first,
            worktree=str(host.run_dir(first) / "worktree"),
        )
        host.edit_state(rework, continued_by=live)
        _age(host.run_dir(rework) / "state.json", timedelta(hours=1).total_seconds())
        swept: list[str] = []

        async def end_marked(key: str, value: str, **_: object) -> int:
            swept.append(value)
            return 0

        monkeypatch.setattr(host_session, "end_marked", end_marked)

        reaped = await host.adapter.reap({live, first}, retain=None)

        assert str(host.run_dir(first) / "config") not in swept, swept
        assert reaped == (rework,)
        assert (host.run_dir(first) / "worktree").is_dir()
    finally:
        await host.close()


def _admin_names(clone: Path) -> list[str]:
    return sorted(path.name for path in (clone / ".git" / "worktrees").iterdir())


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_reaping_a_run_never_removes_the_workspace_git_gave_its_name_to(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """git reuses a freed administrative name (`worktree`, `worktree1`, ...). A run whose own
    entry went — an older `worktree prune` took every pointerless one — still names it in its
    state, and the name now belongs to a later run: reaping the first must leave the second
    registered."""
    host = _Host(kind, tmp_path, root)
    try:
        first = await host.spawn(1, end="succeeded")
        (name,) = _admin_names(host.clone)
        shutil.rmtree(host.clone / ".git" / "worktrees" / name)
        second = await host.spawn(2, end="succeeded")
        assert _admin_names(host.clone) == [name], "git gave the freed name to the second run"
        _age(host.run_dir(first) / "state.json", timedelta(hours=1).total_seconds())

        assert await host.adapter.reap({second}, retain=None) == (first,)

        assert (host.clone / ".git" / "worktrees" / name).is_dir()
        assert str(host.run_dir(second) / "worktree") in host.registered()
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_a_park_that_raises_still_ends_the_run(
    tmp_path: Path, root: Path, kind: Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The park runs before the terminal write, so one that raised would leave the run reading
    as running for ever; it says so in the run's warnings instead."""

    async def broken(*args: object, **kwargs: object) -> tuple[str, ...]:
        raise OSError("the disk went away")

    monkeypatch.setattr(host_session, "park_workspace", broken)
    host = _Host(kind, tmp_path, root)
    try:
        run_id = await host.spawn(12, end="succeeded")

        assert await host.adapter.status(run_id) is RunStatus.SUCCEEDED
        warnings = (await host.adapter.result(run_id)).stream_warnings
        assert any("could not be parked" in warning for warning in warnings)
    finally:
        await host.close()


@needs_tmux
async def test_a_worktree_without_its_pointer_never_moves_the_repository_around_it(
    tmp_path: Path, root: Path
) -> None:
    """With its `.git` pointer gone, `git -C <worktree>` walks up into whatever repository holds
    the run area — on a developer's machine, the operator's own checkout."""
    outer = _repo(root / "o")
    host = _Host("claude_cli", tmp_path, outer / "r")
    try:
        run_id = await host.spawn(12, end="succeeded", action="unpointed")

        assert await host.adapter.status(run_id) is RunStatus.SUCCEEDED
        assert _git(outer, "symbolic-ref", "-q", "HEAD").stdout.strip() == "refs/heads/main"
        assert host.frees("spec/12-x", tmp_path / "next").ok, "found by the clone's own record"
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_the_park_runs_no_hook_the_agent_planted(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """The clone is writable by the agent, and a hook in it would run as the worker."""
    host = _Host(kind, tmp_path, root)
    try:
        run_id = await host.spawn(12, end="succeeded", action="hook")

        assert await host.adapter.status(run_id) is RunStatus.SUCCEEDED
        assert not host.marker.exists()
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_the_startup_sweep_deletes_what_earlier_ends_left(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """Runs that ended before every end deleted their credentials still hold them, interrupted
    ones included, and so does a run whose park failed. A live run keeps its own."""
    host = _Host(kind, tmp_path, root)
    try:
        ended = await host.spawn(1, end="succeeded")
        parked = await host.spawn(2, end="interrupted")
        live = await host.spawn(4)
        name = "mcp.json" if kind == "claude_cli" else "auth.json"
        for run_id in (ended, parked, live):
            (host.run_dir(run_id) / "config" / name).write_text("{}")

        deleted = host.adapter.sweep_credentials()

        for run_id in (ended, parked):
            assert not (host.run_dir(run_id) / "config" / name).exists(), run_id
        assert (host.run_dir(live) / "config" / name).is_file()
        assert deleted == 2
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("legacy", [True, False], ids=["session", "headless"])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_state_file_from_before_every_run_was_a_session_is_still_swept(
    tmp_path: Path, root: Path, kind: Kind, legacy: bool
) -> None:
    """Every live state file holds `interactive` at the deploy, and the state forbids unknown
    keys: one it cannot read is skipped by the sweep, and keeps its credentials for ever."""
    host = _Host(kind, tmp_path, root)
    try:
        run_id = await host.spawn(1, end="succeeded")
        name = "mcp.json" if kind == "claude_cli" else "auth.json"
        (host.run_dir(run_id) / "config" / name).write_text("{}")
        host.edit_state(run_id, mcp=None, interactive=legacy)

        host.adapter.sweep_credentials()

        assert not (host.run_dir(run_id) / "config" / name).exists()
        assert await host.adapter.status(run_id) is RunStatus.SUCCEEDED
    finally:
        await host.close()


@needs_tmux
@pytest.mark.parametrize("kind", KINDS)
async def test_a_tree_the_agent_locked_is_still_reaped(
    tmp_path: Path, root: Path, kind: Kind
) -> None:
    """A directory the agent left mode 000 cannot be opened to walk, and the reaper used to
    stop there for good, so no later run was ever reaped."""
    host = _Host(kind, tmp_path, root)
    try:
        locked_run = await host.spawn(1, end="succeeded")
        other = await host.spawn(2, end="succeeded")
        locked = host.run_dir(locked_run) / "config" / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner" / "file").write_text("x")
        locked.chmod(0o000)
        for run_id in (locked_run, other):
            _age(host.run_dir(run_id) / "state.json", timedelta(hours=1).total_seconds())
        try:
            reaped = await host.adapter.reap(set(), retain=None)
        finally:
            if locked.exists():
                locked.chmod(0o700)

        assert reaped == (locked_run, other)
        assert not (host.run_dir(locked_run) / "config").exists()
    finally:
        await host.close()


@needs_tmux
async def test_one_run_that_cannot_be_reaped_never_stalls_the_others(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _Host("claude_cli", tmp_path, root)
    try:
        stuck = await host.spawn(1, end="succeeded")
        other = await host.spawn(2, end="succeeded")
        for run_id in (stuck, other):
            _age(host.run_dir(run_id) / "state.json", timedelta(hours=1).total_seconds())
        rmtree = checkout._rmtree

        def failing(path: Path) -> None:
            if stuck in str(path):
                raise RuntimeError("an unexpected tree")
            rmtree(path)

        monkeypatch.setattr(checkout, "_rmtree", failing)

        assert await host.adapter.reap(set(), retain=None) == (other,)
    finally:
        await host.close()


async def test_an_administrative_entry_that_is_a_link_is_never_chosen(tmp_path: Path) -> None:
    """What `_admin_dir_of` returns is rmtree'd, and the clone is writable by the agent: an
    entry that links out of the clone must not be followed there."""
    clone, worktree = tmp_path / "clone", tmp_path / "run" / "worktree"
    worktree.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "gitdir").write_text(f"{worktree / '.git'}\n")
    (clone / ".git" / "worktrees").mkdir(parents=True)
    (clone / ".git" / "worktrees" / "worktree").symlink_to(elsewhere)

    assert _admin_dir_of(clone, worktree) is None
