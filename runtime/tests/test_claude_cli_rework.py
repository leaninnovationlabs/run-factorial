"""A rework on the host continues the previous attempt's session (decision 56): the claude_cli
adapter adopts the kept worktree and config home, moves the checkout to the pull request's head,
and resumes the same session id in the new run's own tmux server.

The sessions run `fake_claude_tui.py` in a real tmux, against a local bare origin whose GitHub
URL `OriginCli` rewrites, with short roots under /tmp (`host_session_support.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from host_session_support import (
    FAKE_TUI,
    REPO,
    OriginCli,
    bare_origin,
    edit_state,
    fake_tui_settings_reach_sessions,  # noqa: F401
    git,
    leave_job,
    reaches,
    run_dir,
    server_of,
    short_root,
    state_of,
    until,
)
from pydantic import SecretStr

from runtime.adapters._cli import host_session as session
from runtime.adapters._cli import tmux
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.launch import ENV_FILE
from runtime.adapters._cli.process import CommandResult
from runtime.adapters._cli.rework import retry_header, rework_header
from runtime.adapters._cli.session_state import STATE_FILE, HostRunState
from runtime.adapters._cli.tmux import TmuxServer
from runtime.adapters.claude_cli import session as claude_session
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.cli import (
    API_KEY_TAIL,
    CLAUDE_JSON_FILE,
    CLAUDE_JSON_KEY_RESPONSES,
    ENV_API_KEY,
    MCP_CONFIG_FILE,
)
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    InspectPhase,
    InspectUnavailable,
    McpServerSpec,
    McpTransport,
    NoBranchHeld,
    ResumeFrom,
    RunSpec,
    RunStatus,
    RunUsage,
    SessionUnavailable,
    UnknownRunError,
    Workspace,
)

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

BRANCH = "spec/12-widget"
API_KEY = "sk-ant-api03-the-first-attempts-key-0123456789"
TOKEN = "ghs_firstattemptstoken"
NEW_KEY = "sk-ant-api03-the-key-minted-for-the-rework-987654"
NEW_TOKEN = "ghs_mintedfortherework"
DOCS_TOKEN = "docs-token-held-only-by-mcp-json"
MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)
CREDENTIALS = (ENV_FILE, CLAUDE_JSON_FILE, MCP_CONFIG_FILE)


@pytest.fixture
def root() -> Iterator[Path]:
    yield from short_root("f243w-")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return bare_origin(tmp_path)


@pytest.fixture
def claude(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True)
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TUI}" "$@"\n')
    script.chmod(0o755)
    return script


@pytest.fixture
def adapter(root: Path, claude: Path, origin: Path) -> ClaudeCliAdapter:
    return ClaudeCliAdapter(local_root=root, cli=OriginCli(claude, origin), poll=0.05)


def spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You plan.", instructions="Write the spec."),
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 10,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


def rework_of(run_id: str, session_id: str, **overrides: object) -> RunSpec:
    secrets = {ENV_API_KEY: SecretStr(NEW_KEY), ENV_GH_TOKEN: SecretStr(NEW_TOKEN)}
    fields: dict[str, object] = {
        "resume_from": ResumeFrom(run_id=run_id, session_id=session_id),
        "secrets": secrets,
    }
    return spec(**(fields | overrides))


def run_ids(root: Path) -> set[str]:
    return {path.name for path in (root / "runs").iterdir()}


def worktree(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "worktree"


def attached(root: Path, run_id: str) -> str | None:
    shown = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=worktree(root, run_id),
        capture_output=True,
        text=True,
    )
    return shown.stdout.strip() if shown.returncode == 0 else None


def credentials_left(root: Path, run_id: str) -> list[str]:
    config = run_dir(root, run_id) / "config"
    return [name for name in CREDENTIALS if (config / name).exists()]


async def live_servers(root: Path) -> list[str]:
    binary = shutil.which("tmux")
    assert binary is not None
    found = []
    for socket in sorted((root / tmux.SOCKETS_DIRNAME).glob("*-s")):
        server = TmuxServer(binary, str(socket), root / tmux.SESSION_CONF_FILE, root, "run")
        if await server.pane() is not None:
            found.append(socket.name)
    return found


async def ended(adapter: ClaudeCliAdapter, root: Path, *run_ids: str) -> None:
    for run_id in run_ids:
        await server_of(root, run_id).kill()
    await adapter.drain()


async def first_attempt(
    adapter: ClaudeCliAdapter, root: Path, *, push: bool = True, **overrides: object
) -> tuple[str, str]:
    """A run that checked out and pushed its branch, then finished: its HEAD detached by the
    park, the branch it held recorded."""
    run_id = (await adapter.spawn(spec(**overrides))).run_id
    await reaches(adapter, run_id, RunStatus.IDLE)
    checkout = worktree(root, run_id)
    git("checkout", "-b", BRANCH, cwd=checkout)
    (checkout / "spec.md").write_text("the spec\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "spec", cwd=checkout)
    if push:
        git("push", "origin", BRANCH, cwd=checkout)
    await adapter.finish(run_id)
    state = state_of(root, run_id)
    assert state.status is RunStatus.SUCCEEDED and state.session_id is not None
    assert state.held_branch == BRANCH and attached(root, run_id) is None
    return run_id, state.session_id


def session_log(root: Path, run_id: str, session_id: str) -> Path:
    return claude_session.session_log(run_dir(root, run_id) / "config", session_id)


@needs_tmux
async def test_a_rework_resumes_the_session_in_the_kept_worktree(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, sid = await first_attempt(adapter, root)
    parked = state_of(root, first)
    log_size = session_log(root, first, sid).stat().st_size
    worktrees = git("worktree", "list", "--porcelain", cwd=worktree(root, first)).count(
        "\nworktree "
    )
    config = run_dir(root, first) / "config"
    targets = tmp_path / "targets"
    targets.mkdir()
    planted = [config / name for name in (*CREDENTIALS, "settings.json")]
    for n, link in enumerate(planted):
        (targets / str(n)).write_text("the agent's\n")
        link.unlink(missing_ok=True)
        link.symlink_to(targets / str(n))
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))
    secrets = {
        ENV_API_KEY: SecretStr(NEW_KEY),
        ENV_GH_TOKEN: SecretStr(NEW_TOKEN),
        "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
    }

    handle = await adapter.spawn(rework_of(first, sid, secrets=secrets, mcp=(MCP_HTTP,)))
    run_id = handle.run_id
    try:
        assert run_id != first and handle.status is RunStatus.RUNNING
        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        assert not (here / "worktree").exists() and not (here / "config").exists()
        listed = git("worktree", "list", "--porcelain", cwd=worktree(root, first))
        assert listed.count("\nworktree ") == worktrees
        assert attached(root, first) == f"refs/heads/{BRANCH}"

        seen = json.loads(dump.read_text())
        argv = seen["argv"][1:]
        assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv
        turn = run_dir(root, first) / "turns" / "1.md"
        assert turn.parent.parent == worktree(root, first).parent, "beside the worktree it reads in"
        assert not (here / "turns").exists(), "the CLI refuses a path outside its workspace"
        assert argv[-2:] == ["--", f"Read @{turn} and do what it says."]
        head = git("rev-parse", "HEAD", cwd=worktree(root, first))
        text = turn.read_text()
        assert text.startswith(f"{rework_header(BRANCH, head, None, None)}\n\n# Workspace\n")
        assert "Write the spec." in text
        assert seen["cwd"] == str(worktree(root, first))
        env = seen["env"]
        assert env["CLAUDE_CONFIG_DIR"] == str(config)
        assert (env[ENV_API_KEY], env[ENV_GH_TOKEN]) == (NEW_KEY, NEW_TOKEN)

        for n, link in enumerate(planted):
            assert (targets / str(n)).read_text() == "the agent's\n", link
            assert not link.is_symlink(), link
        seed = json.loads((config / CLAUDE_JSON_FILE).read_text())
        assert seed[CLAUDE_JSON_KEY_RESPONSES]["approved"] == [NEW_KEY[-API_KEY_TAIL:]]
        assert DOCS_TOKEN in (config / MCP_CONFIG_FILE).read_text()
        assert not (config / ENV_FILE).exists(), "the pane deletes its env file"

        state = state_of(root, run_id)
        assert (state.workspace_of, state.worktree) == (first, str(worktree(root, first)))
        assert state.log_offset == state.segment_log_start == log_size > 0
        assert state.cost_base == parked.cost_seen is not None
        assert (state.segments, state.segment_turn, state.session_id) == (2, True, sid)
        now = await adapter.session_state(run_id)
        assert (now.session_id, now.log_offset) == (sid, log_size)

        async def counted() -> bool:
            return (await adapter.session_state(run_id)).usage is not None

        await until(counted, "the rework's own message counted")
        usage = (await adapter.session_state(run_id)).usage
        assert usage == RunUsage(input_tokens=10, output_tokens=10), "the first attempt's is not"

        assert state_of(root, first).continued_by == run_id
        with pytest.raises(UnknownRunError):
            await adapter.resume(first, "Go on.", secrets)
        with pytest.raises(InspectUnavailable, match="taken over"):
            await adapter.inspect_start(
                first, token="t", limit=timedelta(minutes=5), credentials=spec().secrets
            )

        await adapter.finish(run_id)
        result = await adapter.result(run_id)
        assert (result.outcome, result.session_id) == (RunStatus.SUCCEEDED, sid)
        assert result.cost_usd == Decimal("0.001"), "the first attempt's cost is not counted"
        lines = [json.loads(line) async for line in adapter.logs(run_id)]
        users = [line.get("factorial_origin") for line in lines if line["type"] == "user"]
        assert users == ["orchestrator"], "the first attempt's turn is not in this transcript"
        assert credentials_left(root, first) == []
        assert attached(root, first) is None, "parked again"
        assert state_of(root, run_id).held_branch == BRANCH
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
@pytest.mark.skipif(
    not sys.platform.startswith("linux") and sys.platform != "darwin",
    reason="reads process environments on Linux and macOS only",
)
async def test_ending_what_a_continued_run_left_spares_its_live_rework(
    adapter: ClaudeCliAdapter, root: Path, claude: Path, origin: Path
) -> None:
    """The rework runs in the config home of the run it continues, so sweeping that home for the
    ended run would hang the live rework's session up."""
    first, sid = await first_attempt(adapter, root)
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    job = leave_job(str(run_dir(root, first) / "config"))
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        tmux_bin = shutil.which("tmux")
        assert tmux_bin is not None

        await session.end_stray(adapter._tui, tmux_bin, root, first)
        restarted = ClaudeCliAdapter(local_root=root, cli=OriginCli(claude, origin), poll=0.05)
        await asyncio.to_thread(restarted.sweep_credentials)

        pane = await server_of(root, run_id).pane()
        assert pane is not None and not pane.dead, "the rework's session was hung up"
        assert await adapter.status(run_id) is RunStatus.IDLE
        assert job.poll() is None, "a process holding the live rework's home was ended"
    finally:
        job.kill()
        job.wait()
        await ended(adapter, root, first, run_id)


@needs_tmux
async def test_a_rework_stashes_uncommitted_work_and_resets_the_branch_to_the_pr_head(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path
) -> None:
    first, sid = await first_attempt(adapter, root)
    other = tmp_path / "other"
    git("clone", "-b", BRANCH, f"file://{tmp_path / 'origin.git'}", str(other), cwd=tmp_path)
    (other / "review.md").write_text("addressed elsewhere\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", "from the PR", cwd=other)
    git("push", "origin", BRANCH, cwd=other)
    pr_head = git("rev-parse", "HEAD", cwd=other)
    checkout = worktree(root, first)
    (checkout / "local.md").write_text("never pushed\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "local only", cwd=checkout)
    local = git("rev-parse", "HEAD", cwd=checkout)
    (checkout / "notes.txt").write_text("untracked work\n")
    (checkout / "README.md").write_text("edited\n")

    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        assert attached(root, first) == f"refs/heads/{BRANCH}"
        assert git("rev-parse", "HEAD", cwd=checkout) == pr_head
        assert not (checkout / "notes.txt").exists()
        assert (checkout / "README.md").read_text() == "widgets\n"
        listed = git("stash", "list", cwd=checkout)
        assert f"factorial: before the rework by run {run_id}" in listed
        assert git("show", "stash@{0}^3:notes.txt", cwd=checkout) == "untracked work"
        assert git("show", "stash@{0}:README.md", cwd=checkout) == "edited"
        stash = git("rev-parse", "refs/stash", cwd=checkout)
        turn = (run_dir(root, first) / "turns" / "1.md").read_text()
        assert turn.startswith(f"{rework_header(BRANCH, pr_head, stash, local)}\n\n")
        assert git("cat-file", "-t", local, cwd=checkout) == "commit"
    finally:
        await ended(adapter, root, run_id)


def stashed(checkout: Path) -> bool:
    shown = ["git", "rev-parse", "-q", "--verify", "refs/stash"]
    return subprocess.run(shown, cwd=checkout, capture_output=True).returncode == 0


def retry_of(run_id: str, session_id: str) -> RunSpec:
    return rework_of(
        run_id,
        session_id,
        resume_from=ResumeFrom(run_id=run_id, session_id=session_id, mode="retry"),
    )


@needs_tmux
async def test_a_retry_continues_on_the_held_branch_at_its_local_tip(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path
) -> None:
    first, sid = await first_attempt(adapter, root)
    checkout = worktree(root, first)
    git("checkout", BRANCH, cwd=checkout)
    (checkout / "local.md").write_text("never pushed\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "local only", cwd=checkout)
    local = git("rev-parse", "HEAD", cwd=checkout)
    (checkout / "notes.txt").write_text("untracked work\n")
    (checkout / "README.md").write_text("edited\n")
    git("checkout", "--detach", cwd=checkout)
    other = tmp_path / "other"
    git("clone", "-b", BRANCH, f"file://{tmp_path / 'origin.git'}", str(other), cwd=tmp_path)
    (other / "review.md").write_text("addressed elsewhere\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", "from the PR", cwd=other)
    git("push", "origin", BRANCH, cwd=other)
    clone = root / "repos" / REPO
    tracked_before = git("rev-parse", f"refs/remotes/origin/{BRANCH}", cwd=clone)

    run_id = (await adapter.spawn(retry_of(first, sid))).run_id
    try:
        assert attached(root, first) == f"refs/heads/{BRANCH}"
        assert git("rev-parse", "HEAD", cwd=checkout) == local
        assert (checkout / "notes.txt").exists()
        assert (checkout / "README.md").read_text() == "edited\n"
        assert not stashed(checkout)
        assert git("rev-parse", f"refs/remotes/origin/{BRANCH}", cwd=clone) == tracked_before
        turn = (run_dir(root, first) / "turns" / "1.md").read_text()
        assert turn.startswith(f"{retry_header(BRANCH, local)}\n\n")
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_retry_keeps_a_branch_that_was_never_pushed(
    adapter: ClaudeCliAdapter, root: Path
) -> None:
    """`_drop_stale_branches` (`_cli/checkout.py`) deletes a local branch of the step's with no
    origin counterpart that no worktree holds, and a parked worktree, detached, holds none: a
    retry that pruned or fetched first would lose the only copy of the attempt's work."""
    first, sid = await first_attempt(adapter, root, push=False)
    checkout = worktree(root, first)
    git("checkout", BRANCH, cwd=checkout)
    (checkout / "local.md").write_text("never pushed\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-m", "local only", cwd=checkout)
    local = git("rev-parse", "HEAD", cwd=checkout)
    git("checkout", "--detach", cwd=checkout)

    run_id = (await adapter.spawn(retry_of(first, sid))).run_id
    try:
        assert git("rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}", cwd=checkout) == local
        assert attached(root, first) == f"refs/heads/{BRANCH}"
        assert git("rev-parse", "HEAD", cwd=checkout) == local
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_retry_of_a_run_that_held_no_branch_raises_no_branch_held(
    adapter: ClaudeCliAdapter, root: Path
) -> None:
    first, sid = await first_attempt(adapter, root)
    edit_state(root, first, held_branch=None)
    runs = run_ids(root)

    with pytest.raises(NoBranchHeld):
        await adapter.spawn(retry_of(first, sid))

    assert run_ids(root) == runs


@needs_tmux
async def test_a_hook_or_fsmonitor_the_agent_configured_does_not_run_during_the_rework(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path
) -> None:
    """The worktree's config is the clone's, which the agent writes as the worker's user."""
    first, sid = await first_attempt(adapter, root)
    checkout = worktree(root, first)
    (checkout / "notes.txt").write_text("untracked work\n")
    ran = tmp_path / "ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch '{ran}-{name}'\n")
        (hooks / name).chmod(0o755)
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(f"#!/bin/sh\ntouch '{ran}-fsmonitor'\n")
    monitor.chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=checkout)
    git("config", "core.fsmonitor", str(monitor), cwd=checkout)

    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        assert attached(root, first) == f"refs/heads/{BRANCH}"
        assert "factorial: before the rework" in git("stash", "list", cwd=checkout)
        assert [name for name in os.listdir(tmp_path) if name.startswith("ran-")] == []
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_rework_ends_the_inspect_shell_before_it_writes_a_credential(
    adapter: ClaudeCliAdapter, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {
        ENV_API_KEY: SecretStr(API_KEY),
        ENV_GH_TOKEN: SecretStr(TOKEN),
        "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
    }
    first, sid = await first_attempt(adapter, root, secrets=secrets, mcp=(MCP_HTTP,))
    await adapter.inspect_start(first, token="t", limit=timedelta(minutes=5), credentials=secrets)
    seen: list[tuple[str, list[str], str | None, str | None]] = []
    real = adapter._end_inspect

    async def end_inspect(ended_id: str) -> None:
        taken, head = state_of(root, first).continued_by, attached(root, first)
        # The view's own credential files go with it, before the adoption writes its own.
        await real(ended_id)
        seen.append((ended_id, credentials_left(root, first), taken, head))

    monkeypatch.setattr(adapter, "_end_inspect", end_inspect)
    rework = rework_of(
        first, sid, secrets={**secrets, ENV_API_KEY: SecretStr(NEW_KEY)}, mcp=(MCP_HTTP,)
    )
    run_id = (await adapter.spawn(rework)).run_id
    try:
        [(ended_id, left, taken, head)] = seen
        assert (ended_id, left, taken, head) == (first, [], run_id, None)
        assert (await adapter.inspect_status(first)).phase is InspectPhase.STOPPED
        await reaches(adapter, run_id, RunStatus.IDLE)
        assert credentials_left(root, first) == [CLAUDE_JSON_FILE, MCP_CONFIG_FILE]
    finally:
        await ended(adapter, root, run_id)


CASES = [
    "no state",
    "from before every run was a session",
    "not ended",
    "another session",
    "another repo",
    "another ticket",
    "another agent",
    "worktree gone",
    "config gone",
    "log gone",
    "no held branch",
    "a branch outside the step's",
    "branch gone from origin",
    "branch held by another worktree",
    "a branch name that is a refspec",
    "garbled",
]


@needs_tmux
@pytest.mark.parametrize("case", CASES)
async def test_an_unresumable_session_raises_session_unavailable_and_launches_nothing(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path, case: str
) -> None:
    first, sid = await first_attempt(adapter, root)
    rework = rework_of(first, sid)
    checkout = worktree(root, first)
    if case == "no state":
        rework = rework_of("GH-12-spec-agent-00000000", sid)
    elif case == "from before every run was a session":
        edit_state(root, first, workspace_real=None)
    elif case == "not ended":
        edit_state(root, first, status=RunStatus.IDLE)
    elif case == "another session":
        rework = rework_of(first, "5f0b1f2e-0000-4000-8000-0000000000aa")
    elif case == "another repo":
        edit_state(root, first, repo="acme/gadgets")
    elif case == "another ticket":
        edit_state(root, first, ticket="GH-13")
    elif case == "another agent":
        edit_state(root, first, agent="build-agent")
    elif case == "worktree gone":
        shutil.rmtree(checkout)
    elif case == "config gone":
        shutil.rmtree(run_dir(root, first) / "config")
    elif case == "log gone":
        session_log(root, first, sid).unlink()
    elif case == "no held branch":
        edit_state(root, first, held_branch=None)
    elif case == "a branch outside the step's":
        git("push", "origin", "HEAD:refs/heads/feature/12-widget", cwd=checkout)
        edit_state(root, first, held_branch="feature/12-widget")
    elif case == "branch gone from origin":
        git("push", "origin", "--delete", BRANCH, cwd=checkout)
    elif case == "a branch name that is a refspec":
        # `fnmatch`'s `*` matches the `:`: fetched, it would write the clone's origin/main.
        edit_state(root, first, held_branch=f"{BRANCH}:refs/remotes/origin/main")
    elif case == "branch held by another worktree":
        clone = root / "repos" / REPO
        git("worktree", "add", str(tmp_path / "elsewhere"), BRANCH, cwd=clone)
    else:
        cli = adapter._cli
        assert isinstance(cli, OriginCli)
        cli.garbled = True
    before = state_of(root, first)
    runs = run_ids(root)
    clone = root / "repos" / REPO
    main = git("rev-parse", "refs/remotes/origin/main", cwd=clone)

    legacy = case == "from before every run was a session"
    with pytest.raises(SessionUnavailable, match="has no session" if legacy else None):
        await adapter.spawn(rework)

    assert git("rev-parse", "refs/remotes/origin/main", cwd=clone) == main
    assert run_ids(root) == runs
    assert state_of(root, first) == before
    assert await live_servers(root) == []
    assert credentials_left(root, first) == []
    if checkout.exists():
        assert attached(root, first) is None, "a fresh attempt can check the branch out"


@needs_tmux
@pytest.mark.parametrize("failure", ["fetch", "launch"])
async def test_an_outage_during_adoption_is_adapter_unavailable_and_leaves_the_run_as_it_was(
    adapter: ClaudeCliAdapter,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A retry may find the session, so nothing says it is gone. A launch that fails after the
    checkout moved and the credentials were written takes both back."""
    first, sid = await first_attempt(adapter, root)
    bare, away = tmp_path / "origin.git", tmp_path / "away.git"
    if failure == "fetch":
        bare.rename(away)
    else:

        async def refused(server: TmuxServer, cwd: Path, argv: list[str]) -> CommandResult:
            return CommandResult(("tmux",), 1, "", "no server")

        monkeypatch.setattr(TmuxServer, "start", refused)

    with pytest.raises(AdapterUnavailable) as caught:
        await adapter.spawn(rework_of(first, sid))

    assert not isinstance(caught.value, SessionUnavailable)
    assert state_of(root, first).continued_by is None
    assert credentials_left(root, first) == [] and await live_servers(root) == []
    assert attached(root, first) is None, "a fresh attempt can check the branch out"
    if failure == "fetch":
        away.rename(bare)
    monkeypatch.undo()
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        assert state_of(root, first).continued_by == run_id
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_ghost_continuer_is_cancelled_and_its_session_continued(
    adapter: ClaudeCliAdapter, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost tick launched a rework but never recorded it, so the retry names the first run
    again: the unrecorded one is stopped, never run beside, and its session continued."""
    first, sid = await first_attempt(adapter, root)
    ghost = (await adapter.spawn(rework_of(first, sid))).run_id
    run_id = ""
    try:
        await reaches(adapter, ghost, RunStatus.IDLE)

        async def refused(run_id: str) -> None:
            raise AdapterUnavailable("tmux did not answer")

        with monkeypatch.context() as patched:
            patched.setattr(adapter, "cancel", refused)
            with pytest.raises(AdapterUnavailable):
                await adapter.spawn(rework_of(first, sid))
        assert state_of(root, first).continued_by == ghost, "the retry must find it again"
        assert await adapter.status(ghost) is RunStatus.IDLE

        run_id = (await adapter.spawn(rework_of(first, sid))).run_id

        stopped = state_of(root, ghost)
        assert (stopped.status, stopped.continued_by) == (RunStatus.CANCELLED, run_id)
        assert await server_of(root, ghost).pane() is None
        state = state_of(root, run_id)
        assert (state.workspace_of, state.segments, state.session_id) == (first, 3, sid)
        assert state.cost_base == stopped.cost_seen
        assert state.log_offset == session_log(root, first, sid).stat().st_size
        assert state.log_offset > stopped.log_offset
        await reaches(adapter, run_id, RunStatus.IDLE)
        await adapter.finish(run_id)
        assert (await adapter.result(run_id)).cost_usd == Decimal("0.001")
    finally:
        await ended(adapter, root, ghost, *([run_id] if run_id else []))


@needs_tmux
async def test_an_adoption_a_worker_died_in_is_taken_over_by_the_retry(
    adapter: ClaudeCliAdapter, root: Path
) -> None:
    """It died after it launched its CLI and before it wrote its state: the earlier run names
    a continuer with no state, whose server still runs in the workspace."""
    first, sid = await first_attempt(adapter, root)
    dead = "GH-12-spec-agent-0000dead"
    edit_state(root, first, continued_by=dead)
    stray = server_of(root, dead)
    started = await stray.start(worktree(root, first), ["/bin/sleep", "1000"])
    assert started.ok

    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        assert await stray.pane() is None
        assert state_of(root, first).continued_by == run_id
        await reaches(adapter, run_id, RunStatus.IDLE)
    finally:
        await ended(adapter, root, run_id, dead)


@needs_tmux
async def test_a_second_rework_runs_in_the_first_runs_workspace(
    adapter: ClaudeCliAdapter, root: Path
) -> None:
    first, sid = await first_attempt(adapter, root)
    second = (await adapter.spawn(rework_of(first, sid))).run_id
    third = ""
    try:
        await reaches(adapter, second, RunStatus.IDLE)
        await adapter.finish(second)
        assert state_of(root, second).held_branch == BRANCH

        third = (await adapter.spawn(rework_of(second, sid))).run_id

        state = state_of(root, third)
        assert (state.workspace_of, state.worktree) == (first, str(worktree(root, first)))
        assert (run_dir(root, first) / "turns" / "2.md").exists()
        assert not (run_dir(root, third) / "turns").exists()
        assert state_of(root, second).continued_by == third
        await reaches(adapter, third, RunStatus.IDLE)
        await adapter.finish(third)
        assert (await adapter.result(third)).cost_usd == Decimal("0.001")
    finally:
        await ended(adapter, root, second, *([third] if third else []))


@needs_tmux
async def test_a_rework_is_taken_back_after_a_worker_restart(
    adapter: ClaudeCliAdapter, root: Path, claude: Path, origin: Path
) -> None:
    """Its server runs the launch from its workspace's config home, not its own run dir's."""
    first, sid = await first_attempt(adapter, root)
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        await reaches(adapter, run_id, RunStatus.IDLE)
        tasks = list(adapter._supervisors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        fresh = ClaudeCliAdapter(local_root=root, cli=OriginCli(claude, origin), poll=0.05)

        assert await fresh.status(run_id) is RunStatus.IDLE
        await asyncio.sleep(0.5)
        assert await fresh.status(run_id) is RunStatus.IDLE, state_of(root, run_id).failure_reason
        await fresh.finish(run_id)
        assert (await fresh.result(run_id)).outcome is RunStatus.SUCCEEDED
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_filter_the_agent_configured_refuses_the_rework_before_it_runs(
    adapter: ClaudeCliAdapter, root: Path, tmp_path: Path
) -> None:
    """A filter driver has no command-scope switch, and the stash would run it on the host with
    the new run's token (design.md §7.13): the clone is refused before the fetch, and the
    session is not continued."""
    first, sid = await first_attempt(adapter, root)
    checkout = worktree(root, first)
    seen = tmp_path / "seen"
    driver = tmp_path / "driver.sh"
    driver.write_text(f"#!/bin/sh\ntouch '{seen}'\ncat\n")
    driver.chmod(0o755)
    git("config", "filter.agent.clean", str(driver), cwd=checkout)
    (checkout / ".gitattributes").write_text("* filter=agent\n")
    (checkout / "README.md").write_text("edited\n")

    with pytest.raises(SessionUnavailable, match=r"filter\.agent\.clean"):
        await adapter.spawn(rework_of(first, sid))

    assert not seen.exists()


@needs_tmux
async def test_a_continuer_that_is_not_the_earlier_runs_rework_is_never_stopped(
    adapter: ClaudeCliAdapter, root: Path
) -> None:
    """The earlier run's state is the agent's to write: it can name any run as its continuer."""
    first, sid = await first_attempt(adapter, root)
    other = (await adapter.spawn(spec())).run_id
    try:
        await reaches(adapter, other, RunStatus.IDLE)
        edit_state(root, first, continued_by=other)

        with pytest.raises(SessionUnavailable):
            await adapter.spawn(rework_of(first, sid))

        assert await adapter.status(other) is RunStatus.IDLE
        assert state_of(root, first).continued_by == other
    finally:
        await ended(adapter, root, other)


def _parked(root: Path, run_id: str, **fields: object) -> None:
    folder = run_dir(root, run_id)
    (folder / "config").mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    state = HostRunState(
        run_id=run_id,
        status=RunStatus.SUCCEEDED,
        agent="spec-agent",
        ticket="GH-12",
        repo=REPO,
        branch="spec/12-{slug}",
        worktree=str(folder / "worktree"),
        started_at=now,
    ).model_copy(update=fields)
    (folder / STATE_FILE).write_text(state.model_dump_json())


@pytest.mark.parametrize("continuer", [RunStatus.SUCCEEDED, RunStatus.IDLE])
def test_the_sweep_deletes_an_adopting_runs_credentials_from_the_workspace_config(
    root: Path, continuer: RunStatus
) -> None:
    """Nor does it delete them from under a continuer that still runs."""
    first, second = "GH-12-spec-agent-0000000a", "GH-12-spec-agent-0000000b"
    _parked(root, first, continued_by=second)
    _parked(
        root,
        second,
        status=continuer,
        workspace_of=first,
        worktree=str(worktree(root, first)),
    )
    shutil.rmtree(run_dir(root, second) / "config")
    for name in CREDENTIALS:
        (run_dir(root, first) / "config" / name).write_text(NEW_KEY)

    swept = ClaudeCliAdapter(local_root=root).sweep_credentials()

    left = credentials_left(root, first)
    if continuer.is_terminal:
        assert (swept, left) == (len(CREDENTIALS), [])
    else:
        assert (swept, left) == (0, list(CREDENTIALS))


def test_the_sweep_survives_a_run_name_no_directory_could_have(root: Path) -> None:
    first, second = "gh-12-spec-agent-0000000a", "gh-12-spec-agent-0000000b"
    _parked(root, first, continued_by="a\x00b")
    _parked(root, second, workspace_of="a\x00b")
    for name in CREDENTIALS:
        (run_dir(root, first) / "config" / name).write_text(NEW_KEY)

    ClaudeCliAdapter(local_root=root).sweep_credentials()

    assert credentials_left(root, first) == []


def test_a_reworks_session_lives_in_its_workspace_and_nowhere_the_state_points_instead(
    root: Path, tmp_path: Path
) -> None:
    first, second = "GH-12-spec-agent-0000000a", "GH-12-spec-agent-0000000b"
    for run_id in (first, second):
        run_dir(root, run_id).mkdir(parents=True)
    (run_dir(root, first) / "worktree").mkdir()
    (run_dir(root, first) / "config").mkdir()
    now = datetime.now(UTC)
    state = HostRunState(
        run_id=second,
        status=RunStatus.INTERRUPTED,
        agent="spec-agent",
        ticket="GH-12",
        repo=REPO,
        branch="spec/12-{slug}",
        worktree=str(worktree(root, first)),
        started_at=now,
        workspace_of=first,
    )
    second_dir = run_dir(root, second)

    found = session._kept_worktree(root, second_dir, state)

    assert found == (worktree(root, first), run_dir(root, first) / "config")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    refused = [
        state.model_copy(update={"worktree": str(foreign)}),
        state.model_copy(update={"workspace_of": "../elsewhere"}),
        state.model_copy(update={"workspace_of": None}),
    ]
    for each in refused:
        with pytest.raises(UnknownRunError):
            session._kept_worktree(root, second_dir, each)
    (run_dir(root, first) / "worktree").rmdir()
    (run_dir(root, first) / "worktree").symlink_to(foreign)
    with pytest.raises(UnknownRunError):
        session._kept_worktree(root, second_dir, state)
