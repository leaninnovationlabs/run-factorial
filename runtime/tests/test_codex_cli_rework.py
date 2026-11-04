"""A rework on the host continues the previous attempt's codex thread (decision 56): the codex_cli
adapter adopts the kept worktree and config home, moves the checkout to the pull request's head,
and resumes the thread in the new run's own tmux server, inside the same sandbox as a spawn.

The sessions run `fake_codex_tui.py` in a real tmux, against a local bare origin whose GitHub
URL `CodexOriginCli` rewrites, with short roots under /tmp (`host_session_support.py`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from host_session_support import (
    REPO,
    CodexOriginCli,
    bare_origin,
    fake_tui_settings_reach_sessions,  # noqa: F401
    git,
    reaches,
    run_dir,
    server_of,
    short_root,
)
from pydantic import SecretStr

from runtime.adapters._cli import tmux
from runtime.adapters._cli.checkout import worktree_git_at
from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.launch import ENV_FILE
from runtime.adapters._cli.rework import rework_header
from runtime.adapters._cli.session_state import HostRunState, read_state, write_state
from runtime.adapters._cli.tmux import TmuxServer
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.cli import AUTH_FILE, CONFIG_FILE, ENV_API_KEY
from runtime.contract import (
    AgentPrompts,
    InspectUnavailable,
    ResumeFrom,
    RunSpec,
    RunStatus,
    SessionUnavailable,
    UnknownRunError,
    Workspace,
)

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
FAKE_TUI = Path(__file__).with_name("fake_codex_tui.py")

BRANCH = "spec/12-widget"
API_KEY = "sk-proj-the-first-attempts-key-0123456789abcdef"
TOKEN = "ghs_firstattemptstoken"
NEW_KEY = "sk-proj-the-key-minted-for-the-rework-9876543210"
NEW_TOKEN = "ghs_mintedfortherework"
CREDENTIALS = (AUTH_FILE, CONFIG_FILE, ENV_FILE)


@pytest.fixture
def root() -> Iterator[Path]:
    yield from short_root("f243v-")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return bare_origin(tmp_path)


@pytest.fixture
def codex(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "codex"
    script.parent.mkdir(parents=True)
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TUI}" "$@"\n')
    script.chmod(0o755)
    return script


@pytest.fixture
def adapter(root: Path, codex: Path, origin: Path) -> CodexCliAdapter:
    return CodexCliAdapter(local_root=root, cli=CodexOriginCli(codex, origin), poll=0.05)


def spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "agent": "spec-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You plan.", instructions="Write the spec."),
        "model": "gpt-5.3-codex",
        "max_turns": 10,
        "secrets": {ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    }
    return RunSpec.model_validate(fields | overrides)


def rework_of(run_id: str, thread: str, **overrides: object) -> RunSpec:
    secrets = {ENV_API_KEY: SecretStr(NEW_KEY), ENV_GH_TOKEN: SecretStr(NEW_TOKEN)}
    fields: dict[str, object] = {
        "resume_from": ResumeFrom(run_id=run_id, session_id=thread),
        "secrets": secrets,
    }
    return spec(**(fields | overrides))


def state_of(root: Path, run_id: str) -> HostRunState:
    return read_state(run_dir(root, run_id), run_id)


def edit_state(root: Path, run_id: str, **fields: object) -> None:
    write_state(run_dir(root, run_id), state_of(root, run_id).model_copy(update=fields))


def run_ids(root: Path) -> set[str]:
    return {path.name for path in (root / "runs").iterdir()}


def worktree(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "worktree"


def git_at(root: Path, run_id: str) -> tuple[str, ...]:
    """The run's checkout has no `.git` pointer: its administrative directory, as the clone
    registers it, is named outright."""
    found = worktree_git_at(root, REPO, worktree(root, run_id))
    assert found is not None
    return found


def in_worktree(root: Path, run_id: str, *args: str) -> str:
    return git(*git_at(root, run_id), *args, cwd=root)


def attached(root: Path, run_id: str) -> str | None:
    shown = subprocess.run(
        ["git", *git_at(root, run_id), "symbolic-ref", "-q", "HEAD"],
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


async def ended(adapter: CodexCliAdapter, root: Path, *run_ids: str) -> None:
    for run_id in run_ids:
        await server_of(root, run_id).kill()
    await adapter.drain()


async def first_attempt(adapter: CodexCliAdapter, root: Path) -> tuple[str, str]:
    """A run that checked out and pushed its branch, then finished: its HEAD detached by the
    park, the branch it held recorded."""
    run_id = (await adapter.spawn(spec())).run_id
    await reaches(adapter, run_id, RunStatus.IDLE)
    in_worktree(root, run_id, "checkout", "-b", BRANCH)
    (worktree(root, run_id) / "spec.md").write_text("the spec\n")
    in_worktree(root, run_id, "add", "-A")
    in_worktree(root, run_id, "commit", "-m", "spec")
    in_worktree(root, run_id, "push", "origin", BRANCH)
    await adapter.finish(run_id)
    state = state_of(root, run_id)
    assert state.status is RunStatus.SUCCEEDED and state.session_id is not None
    assert state.held_branch == BRANCH and attached(root, run_id) is None
    return run_id, state.session_id


def rollout(root: Path, run_id: str) -> Path:
    recorded = state_of(root, run_id).session_log
    assert recorded is not None
    return Path(recorded)


@needs_tmux
async def test_a_codex_rework_resumes_the_thread_in_the_kept_worktree(
    adapter: CodexCliAdapter, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What B launches with is the step's spec and what the host finds, never A's state: a
    sandbox flag or an administrative directory planted there reaches no launch."""
    first, thread = await first_attempt(adapter, root)
    log_size = rollout(root, first).stat().st_size
    admin = state_of(root, first).git_dir
    planted = ("-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox")
    edit_state(root, first, git_dir="/", resume_args=planted, model="gpt-planted")
    config = run_dir(root, first) / "config"
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_TUI_DUMP", str(dump))

    handle = await adapter.spawn(rework_of(first, thread))
    run_id = handle.run_id
    try:
        assert run_id != first and handle.status is RunStatus.RUNNING
        await reaches(adapter, run_id, RunStatus.IDLE)
        here = run_dir(root, run_id)
        assert not (here / "worktree").exists() and not (here / "config").exists()
        assert attached(root, first) == f"refs/heads/{BRANCH}"

        seen = json.loads(dump.read_text())
        argv = seen["argv"][1:]
        turn = run_dir(root, first) / "turns" / "1.md"
        assert turn.parent.parent == worktree(root, first).parent, "beside the worktree it reads in"
        assert not (here / "turns").exists(), "the CLI refuses a path outside its workspace"
        assert argv[0] == "resume"
        assert argv[-2:] == [thread, f"Read {turn} and do what it says."]
        assert not set(planted[1:]) & set(argv) and "gpt-planted" not in argv
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("-m") + 1] == "gpt-5.3-codex"
        head = in_worktree(root, first, "rev-parse", "HEAD")
        assert turn.read_text().startswith(f"{rework_header(BRANCH, head, None, None)}\n\n")
        assert seen["cwd"] == str(worktree(root, first))
        env = seen["env"]
        assert env["CODEX_HOME"] == str(config)
        assert env["GIT_DIR"] == admin
        assert env[ENV_GH_TOKEN] == NEW_TOKEN
        assert json.loads((config / AUTH_FILE).read_text())[ENV_API_KEY] == NEW_KEY
        document = tomllib.loads((config / CONFIG_FILE).read_text())
        assert document["sandbox_mode"] == "workspace-write"
        writable = document["sandbox_workspace_write"]["writable_roots"]
        assert writable == [str(root / "repos" / REPO / ".git")]
        assert document["developer_instructions"] == "You plan."

        state = state_of(root, run_id)
        assert (state.workspace_of, state.worktree) == (first, str(worktree(root, first)))
        assert (state.git_dir, state.model) == (admin, "gpt-5.3-codex")
        assert state.session_log == str(rollout(root, first))
        assert state.log_offset == state.segment_log_start == log_size > 0
        assert (state.segments, state.segment_turn, state.session_id) == (2, True, thread)
        now = await adapter.session_state(run_id)
        assert (now.session_id, now.log_offset) == (thread, log_size)

        assert state_of(root, first).continued_by == run_id
        with pytest.raises(UnknownRunError):
            await adapter.resume(first, "Go on.", rework_of(first, thread).secrets)
        with pytest.raises(InspectUnavailable, match="taken over"):
            await adapter.inspect_start(
                first,
                token="t",
                limit=timedelta(minutes=5),
                credentials=rework_of(first, thread).secrets,
            )

        await adapter.finish(run_id)
        result = await adapter.result(run_id)
        assert (result.outcome, result.session_id) == (RunStatus.SUCCEEDED, thread)
        assert result.input_tokens == 1000, "the first attempt's tokens are not counted"
        assert result.stream_warnings == ()
        lines = [json.loads(line) async for line in adapter.logs(run_id)]
        users = [
            line.get("factorial_origin")
            for line in lines
            if line["type"] == "item.completed" and line["item"]["type"] == "user_message"
        ]
        assert users == ["orchestrator"], "the first attempt's turn is not in this transcript"
        assert credentials_left(root, first) == []
        assert attached(root, first) is None, "parked again"
        assert state_of(root, run_id).held_branch == BRANCH
    finally:
        await ended(adapter, root, run_id)


@needs_tmux
async def test_a_codex_rework_stashes_uncommitted_work_and_resets_the_branch_to_the_pr_head(
    adapter: CodexCliAdapter, root: Path, tmp_path: Path
) -> None:
    first, thread = await first_attempt(adapter, root)
    other = tmp_path / "other"
    git("clone", "-b", BRANCH, f"file://{tmp_path / 'origin.git'}", str(other), cwd=tmp_path)
    (other / "review.md").write_text("addressed elsewhere\n")
    git("add", "-A", cwd=other)
    git("commit", "-m", "from the PR", cwd=other)
    git("push", "origin", BRANCH, cwd=other)
    pr_head = git("rev-parse", "HEAD", cwd=other)
    checkout = worktree(root, first)
    (checkout / "local.md").write_text("never pushed\n")
    in_worktree(root, first, "add", "-A")
    in_worktree(root, first, "commit", "-m", "local only")
    local = in_worktree(root, first, "rev-parse", "HEAD")
    (checkout / "notes.txt").write_text("untracked work\n")
    (checkout / "README.md").write_text("edited\n")

    run_id = (await adapter.spawn(rework_of(first, thread))).run_id
    try:
        assert attached(root, first) == f"refs/heads/{BRANCH}"
        assert in_worktree(root, first, "rev-parse", "HEAD") == pr_head
        assert not (checkout / "notes.txt").exists()
        assert (checkout / "README.md").read_text() == "widgets\n"
        listed = in_worktree(root, first, "stash", "list")
        assert f"factorial: before the rework by run {run_id}" in listed
        assert in_worktree(root, first, "show", "stash@{0}^3:notes.txt") == "untracked work"
        assert in_worktree(root, first, "show", "stash@{0}:README.md") == "edited"
        stash = in_worktree(root, first, "rev-parse", "refs/stash")
        turn = (run_dir(root, first) / "turns" / "1.md").read_text()
        assert turn.startswith(f"{rework_header(BRANCH, pr_head, stash, local)}\n\n")
        assert in_worktree(root, first, "cat-file", "-t", local) == "commit"
        await reaches(adapter, run_id, RunStatus.IDLE)
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
    "the rollout is gone",
    "a rollout outside its config home",
    "a rollout reached through a link",
    "no held branch",
    "a branch outside the step's",
    "branch gone from origin",
    "branch held by another worktree",
    "a branch name that is a refspec",
    "a worktrees dir reached through a link",
    "garbled",
]


@needs_tmux
@pytest.mark.parametrize("case", CASES)
async def test_an_unresumable_codex_session_raises_session_unavailable_and_launches_nothing(
    adapter: CodexCliAdapter, root: Path, tmp_path: Path, case: str
) -> None:
    first, thread = await first_attempt(adapter, root)
    rework = rework_of(first, thread)
    checkout = worktree(root, first)
    log = rollout(root, first)
    entries = root / "repos" / REPO / ".git" / "worktrees"
    if case == "no state":
        rework = rework_of("GH-12-spec-agent-00000000", thread)
    elif case == "from before every run was a session":
        edit_state(root, first, workspace_real=None)
    elif case == "not ended":
        edit_state(root, first, status=RunStatus.IDLE)
    elif case == "another session":
        rework = rework_of(first, "01a0fd77-0000-7000-8000-0000000000aa")
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
    elif case == "the rollout is gone":
        log.unlink()
    elif case == "a rollout outside its config home":
        elsewhere = tmp_path / "sessions" / "2026" / "10" / "02" / log.name
        elsewhere.parent.mkdir(parents=True)
        shutil.copy(log, elsewhere)
        edit_state(root, first, session_log=str(elsewhere))
    elif case == "a rollout reached through a link":
        elsewhere = tmp_path / log.name
        log.rename(elsewhere)
        log.symlink_to(elsewhere)
    elif case == "no held branch":
        edit_state(root, first, held_branch=None)
    elif case == "a branch outside the step's":
        in_worktree(root, first, "push", "origin", "HEAD:refs/heads/feature/12-widget")
        edit_state(root, first, held_branch="feature/12-widget")
    elif case == "branch gone from origin":
        in_worktree(root, first, "push", "origin", "--delete", BRANCH)
    elif case == "a branch name that is a refspec":
        # `fnmatch`'s `*` matches the `:`: fetched, it would write the clone's origin/main.
        edit_state(root, first, held_branch=f"{BRANCH}:refs/remotes/origin/main")
    elif case == "branch held by another worktree":
        clone = root / "repos" / REPO
        git("worktree", "add", str(tmp_path / "elsewhere"), BRANCH, cwd=clone)
    elif case == "a worktrees dir reached through a link":
        # It names the worktree as the clone's own entries do, from a directory the agent may
        # have made: `.git` is its sandbox's writable root.
        entries.rename(tmp_path / "worktrees")
        entries.symlink_to(tmp_path / "worktrees")
    else:
        cli = adapter._cli
        assert isinstance(cli, CodexOriginCli)
        cli.garbled = True
    before = state_of(root, first)
    runs = run_ids(root)
    clone = root / "repos" / REPO
    main = git("rev-parse", "refs/remotes/origin/main", cwd=clone)

    legacy = case == "from before every run was a session"
    with pytest.raises(SessionUnavailable, match="has no session" if legacy else None):
        await adapter.spawn(rework)

    if entries.is_symlink():
        entries.unlink()
        (tmp_path / "worktrees").rename(entries)
    assert git("rev-parse", "refs/remotes/origin/main", cwd=clone) == main
    assert run_ids(root) == runs
    assert state_of(root, first) == before
    assert await live_servers(root) == []
    assert credentials_left(root, first) == []
    if checkout.exists():
        assert attached(root, first) is None, "a fresh attempt can check the branch out"


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
    write_state(folder, state)


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

    swept = CodexCliAdapter(local_root=root).sweep_credentials()

    left = credentials_left(root, first)
    if continuer.is_terminal:
        assert (swept, left) == (len(CREDENTIALS), [])
    else:
        assert (swept, left) == (0, list(CREDENTIALS))
