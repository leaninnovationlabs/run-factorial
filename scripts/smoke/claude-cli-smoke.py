"""Local-adapter smoke: REAL runs of `claude_cli` with the real `claude` CLI.

Run from the repo root: `uv run python scripts/smoke/claude-cli-smoke.py [mode ...]`. With no
mode it runs mode 3; 3 first proven 2026-10-23, PASS, on the host and inside the image (claude
2.1.284, uid 10001), and re-run on the final branch 2026-10-23, PASS, on both, with the agent's
private HOME and allowlisted environment.

What it proves:
  3. A session (decision 56), then its rework. The host must pass the adapter's
     `spawn_refusal`: `claude --version` exactly the pin, and tmux 3.2 or later. The TUI
     starts in its own tmux server under the scratch root and reaches its prompt, which only its
     Stop hook reports, with no first-run dialog on screen. A human's turn, typed through the
     terminal relay the gateway uses (`HostTmuxRelay`), is answered and mirrored as the human's.
     A second adapter on the same root takes the session back, as after a worker restart, and
     repeats no mirrored line. `finish`, with only a read-only viewer attached, ends it as a
     success through the pasted `/exit`: the CLI exits 0 and reports a cost, and nothing the
     adapter reads is missing from its stream. Before the finish the script plays the agent and
     the reviewer: it puts the checkout on a branch, pushes it to the scratch remote, adds a
     commit there and leaves a file uncommitted in the checkout. Then a rework spawns with
     `resume_from` and continues the session in the kept worktree: the file stashed, the branch
     checked out again at the reviewer's commit, the session resumed on the same id from where
     its log ended (a `log_offset` above 0), at its prompt, and finished as a success.
     Afterwards neither run has a credential file or a tmux server, and no file in either run's
     directory outside the worktree holds the key.

The target is a scratch file:// repo built fresh each run — nothing on GitHub is
touched. The adapter's ONE test seam is `_SmokeCli`, which sends git's
`https://github.com/SMOKE/scratch.git` to the scratch path by config entries placed after host
git's hardening (`REDIRECT`), for the adapter's git and this script's own; everything else the
adapter does is its production code path. Mode 3's worker restart cancels the first adapter's
supervisors in this process.

Prereqs: the `claude` CLI on PATH, tmux 3.2 or later, and ANTHROPIC_API_KEY in the repo-root
`.env` (read without echoing; the script prints statuses and env KEY NAMES only, never
values). Mode 3 bills the key for three short model turns.
Scratch root: $FACTORIAL_SMOKE_ROOT if set, else a fresh temp dir; mode 3 leaves its runs there.

The harness (scratch layout, the URL seam) lives in `_smoke_common.py`.
"""

import asyncio
import json
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import EllipsisType

from _smoke_common import (
    REDIRECT_ARGS,
    REPO_NAME,
    ROOT,
    SCRATCH,
    ScratchRedirect,
    build_scratch,
    read_env_key,
)
from pydantic import SecretStr

from runtime.adapters._cli import tmux
from runtime.adapters._cli.paths import CONFIG_DIRNAME, WORKTREE_DIRNAME
from runtime.adapters._cli.rework import REWORK_HEADER, REWORK_STASHED
from runtime.adapters._cli.session_state import LOG_FILE
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter, _Cli
from runtime.adapters.claude_cli.cli import INPUT_MARK
from runtime.adapters.claude_cli.session import SESSION_CREDENTIALS
from runtime.adapters.e2b.smoke import DIALOGS
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    ResumeFrom,
    RunSpec,
    RunStatus,
    SessionState,
    Workspace,
)
from runtime.terminal import HostTmuxRelay

AUTHOR = ("-c", "user.name=smoke", "-c", "user.email=smoke@test", "-c", "commit.gpgsign=false")


class _SmokeCli(ScratchRedirect, _Cli):
    pass


def api_key() -> str:
    return read_env_key("ANTHROPIC_API_KEY")


def spec(task: str) -> RunSpec:
    return RunSpec.model_validate(
        {
            "agent": "smoke-agent",
            "ticket": "SMOKE-1",
            "workspace": Workspace(repo=REPO_NAME, base="main", branch="spec/1-{slug}"),
            "prompts": AgentPrompts(
                system=(
                    "You are a smoke-test agent. Do exactly what the instructions say, nothing "
                    "else, then stop."
                ),
                instructions=task,
            ),
            "skills": (),
            "inputs": (),
            "secrets": {"ANTHROPIC_API_KEY": SecretStr(api_key())},
            "model": "claude-haiku-4-5-20251001",
            "max_turns": 8,
        }
    )


def adapter() -> ClaudeCliAdapter:
    return ClaudeCliAdapter(local_root=ROOT / "root", cli=_SmokeCli())


def check(label: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)
    return ok


# ------------------------------------------------------------------ mode 3: a session

#: Seconds a turn of the real model may take to reach the prompt again.
TURN_WAIT = 300
HUMAN_TURN = "Reply with the word ping."


def session_spec(instructions: str, **update: object) -> RunSpec:
    return spec(instructions).model_copy(update={"max_turns": 20, **update})


def run_path(run_id: str) -> Path:
    return ROOT / "root" / "runs" / run_id


def state_of(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "state.json").read_text())


def server(a: ClaudeCliAdapter, run_id: str) -> tmux.TmuxServer:
    root = a._require_root()
    socket = tmux.socket_path(root, run_id, "s", RuntimeError)
    binary = shutil.which("tmux") or "tmux"
    return tmux.TmuxServer(binary, socket, root / tmux.SESSION_CONF_FILE, run_path(run_id), "run")


async def screen(a: ClaudeCliAdapter, run_id: str) -> str:
    return await server(a, run_id).capture() or ""


async def at_prompt(
    a: ClaudeCliAdapter, run_id: str, *, after: datetime | EllipsisType | None = ...
) -> SessionState | None:
    """The session at its prompt after a turn's end, in an idle spell begun after `after`.
    `after` None is a failure: the earlier spell's start was never read, and the guard would
    pass the stale prompt."""
    if after is None:
        raise AssertionError("at_prompt: the earlier idle spell has no start to wait past")
    for _ in range(TURN_WAIT):
        status = await a.status(run_id)
        if status.is_terminal:
            print(f"      run {run_id} ended {status}: {state_of(run_id).get('failure_reason')}")
            return None
        if status is RunStatus.IDLE:
            idle = await a.session_state(run_id)
            since = idle.idle_since
            if idle.idle_reason is IdleReason.TURN_END and (
                after is ... or (since is not None and since > after)
            ):
                return idle
        await asyncio.sleep(1)
    return None


def squeezed(text: str) -> str:
    return "".join(text.split())


async def say(a: ClaudeCliAdapter, run_id: str, text: str) -> None:
    """A human types `text` in a writable terminal, and presses Enter once it is drawn: compared
    without whitespace, since a long line wraps on the pane."""
    tab = await HostTmuxRelay().attach(
        await a.terminal_target(run_id), writable=True, cols=200, rows=50, on_output=lambda _: None
    )
    try:
        # A key sent in the instant the client attaches is lost.
        await asyncio.sleep(0.5)
        await tab.write(text.encode())
        typed = squeezed(f"{INPUT_MARK} {text}")
        for _ in range(100):
            if typed in squeezed(await screen(a, run_id)):
                break
            await asyncio.sleep(0.1)
        await tab.write(b"\r")
    finally:
        await tab.close()


async def mirrored_as_human(run_id: str, text: str) -> bool:
    """The translated mirror trails the hooks, so it is read again for a moment."""
    for _ in range(50):
        for raw in (run_path(run_id) / LOG_FILE).read_text("utf-8").splitlines():
            line = json.loads(raw)
            human = line.get("type") == "user" and line.get("factorial_origin") == "human"
            if human and text in json.dumps(line.get("message")):
                return True
        await asyncio.sleep(0.2)
    return False


async def finish_watched(a: ClaudeCliAdapter, run_id: str) -> None:
    """`finish` with only a read-only viewer attached, beside which tmux refuses `send-keys`."""
    viewer = await HostTmuxRelay().attach(
        await a.terminal_target(run_id),
        writable=False,
        cols=200,
        rows=50,
        on_output=lambda _: None,
    )
    try:
        await a.finish(run_id)
        await a.drain()
    finally:
        await viewer.close()


async def restarted(first: ClaudeCliAdapter, run_id: str) -> ClaudeCliAdapter:
    """A worker restart in this process: the first adapter's supervisors stop where they stand,
    and a second adapter on the same root takes the session back."""
    tasks = list(first._supervisors.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    second = adapter()
    await second.status(run_id)
    for _ in range(100):
        taken = second._attached(run_id)
        if taken is not None and taken._proven:
            break
        await asyncio.sleep(0.1)
    return second


def git(*args: str, cwd: Path) -> str:
    shown = subprocess.run(
        ["git", *AUTHOR, *REDIRECT_ARGS, *args], cwd=cwd, capture_output=True, text=True
    )
    if shown.returncode:
        raise RuntimeError(f"git {args[0]} failed: {shown.stderr.strip()[-300:]}")
    return shown.stdout.strip()


def play_agent_and_reviewer(worktree: Path, branch: str) -> str:
    """The agent's pushed branch, a reviewer's commit on it, and a draft left uncommitted.
    Returns the branch's head on the remote."""
    git("switch", "-q", "-c", branch, cwd=worktree)
    (worktree / "SPEC.md").write_text("the spec\n")
    git("add", "SPEC.md", cwd=worktree)
    git("commit", "-q", "-m", "spec", cwd=worktree)
    git("push", "-q", "origin", branch, cwd=worktree)
    git("checkout", "-q", branch, cwd=SCRATCH)
    git("commit", "-q", "--allow-empty", "-m", "review", cwd=SCRATCH)
    git("checkout", "-q", "main", cwd=SCRATCH)
    (worktree / "DRAFT.md").write_text("draft\n")
    return git("rev-parse", branch, cwd=SCRATCH)


def holds_key(key: str, run_ids: list[str]) -> bool:
    """Whether any file a run's directory holds outside its checkout holds the key."""
    for run_id in run_ids:
        for path in run_path(run_id).rglob("*"):
            if path.relative_to(run_path(run_id)).parts[0] == WORKTREE_DIRNAME:
                continue
            if path.is_file() and not path.is_symlink() and key.encode() in path.read_bytes():
                print(f"      the key is in {path.relative_to(ROOT)}")
                return True
    return False


async def scrubbed(a: ClaudeCliAdapter, home: str, run_ids: list[str]) -> bool:
    config = run_path(home) / CONFIG_DIRNAME
    left = [name for name in SESSION_CREDENTIALS if (config / name).exists()]
    live = [run_id for run_id in run_ids if await server(a, run_id).pane() is not None]
    for name in left:
        print(f"      left behind: {CONFIG_DIRNAME}/{name}")
    for run_id in live:
        print(f"      a tmux server still runs for run {run_id}")
    return not left and not live


async def finished_well(a: ClaudeCliAdapter, run_id: str, session_id: str) -> bool:
    result = await a.result(run_id)
    exit_code = state_of(run_id).get("exit_code")
    ok = (
        result.outcome is RunStatus.SUCCEEDED
        and result.failure_reason is None
        and result.session_id == session_id
        and result.cost_usd is not None
        and result.cost_usd > 0
        and not result.stream_warnings
        and exit_code == 0
    )
    if not ok:
        print(
            f"      {result.outcome} exit={exit_code} cost={result.cost_usd} "
            f"reason={result.failure_reason!r} warnings={result.stream_warnings}"
        )
    return ok


async def interactive() -> bool:
    first = adapter()
    refusal = await first.spawn_refusal()
    if refusal is not None:
        print(f"      {refusal}")
        return check("this host can run a session", False)
    a = (await first.spawn(session_spec("Reply with the word pong."))).run_id
    print(f"session: spawned run_id={a}")
    second = first
    try:
        idle = await at_prompt(first, a)
        shown = await screen(first, a)
        if not check(
            "the TUI reached its prompt with no dialog on screen",
            idle is not None and not DIALOGS.search(shown),
        ):
            return False
        assert idle is not None and idle.session_id is not None
        session_id = idle.session_id
        await say(first, a, HUMAN_TURN)
        answered = await at_prompt(first, a, after=idle.idle_since)
        typed_at = (await first.session_state(a)).last_input_at
        checks = [
            check(
                "a human's turn through the relay was answered and mirrored as the human's",
                answered is not None
                and typed_at is not None
                and await mirrored_as_human(a, HUMAN_TURN),
            )
        ]
        second = await restarted(first, a)
        lines = (run_path(a) / LOG_FILE).read_text("utf-8").splitlines()
        taken = second._attached(a)
        checks.append(
            check(
                "a second adapter took the session back and repeated no line",
                await second.status(a) is RunStatus.IDLE
                and taken is not None
                and taken._proven
                and len(lines) == len(set(lines)),
            )
        )
        branch = f"spec/1-session-{secrets.token_hex(2)}"
        head = play_agent_and_reviewer(run_path(a) / WORKTREE_DIRNAME, branch)
        await finish_watched(second, a)
        checks.append(
            check(
                "finish, with only a watcher attached, ended it by `/exit` with a cost",
                await finished_well(second, a, session_id),
            )
        )
        checks.append(await rework(second, a, session_id, branch, head))
        runs = [a, *filter(None, [state_of(a).get("continued_by")])]
        checks.append(
            check(
                "no credential file, tmux server or model key is left in either run",
                await scrubbed(second, a, runs) and not holds_key(api_key(), runs),
            )
        )
        return all(checks)
    finally:
        for socket in (ROOT / "root" / tmux.SOCKETS_DIRNAME).glob("*"):
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
        await second.drain()


async def rework(a: ClaudeCliAdapter, first: str, session_id: str, branch: str, head: str) -> bool:
    try:
        handle = await a.spawn(
            session_spec(
                "Reply with the word reworked.",
                resume_from=ResumeFrom(run_id=first, session_id=session_id),
            )
        )
    except Exception as exc:
        print(f"      {type(exc).__name__}: {redacted(exc)}")
        return check("the rework adopted the session", False)
    second = handle.run_id
    print(f"rework: spawned run_id={second}")
    adopted = await a.session_state(second)
    turns = sorted((run_path(first) / "turns").iterdir(), key=lambda p: p.stat().st_mtime)
    turn = turns[-1].read_text("utf-8") if turns else ""
    stashes = git("stash", "list", cwd=run_path(first) / WORKTREE_DIRNAME)
    idle = await at_prompt(a, second)
    if idle is not None:
        await finish_watched(a, second)
    else:
        await a.cancel(second)
        await a.drain()
    return all(
        [
            check(
                "the rework continued the same session from where its log ended",
                adopted.session_id == session_id
                and adopted.log_offset > 0
                and state_of(first).get("continued_by") == second,
            ),
            check(
                "its turn says the branch was reset to the reviewer's head and the draft stashed",
                turn.startswith(REWORK_HEADER.format(branch=branch, head=head))
                and REWORK_STASHED.split("`")[0] in turn
                and f"factorial: before the rework by run {second}" in stashes,
            ),
            check("the rework reached its prompt", idle is not None),
            check(
                "the rework finished as a success with a cost and nothing missing",
                await finished_well(a, second, session_id),
            ),
        ]
    )


MODES = {"3": interactive}


def redacted(exc: BaseException) -> str:
    try:
        return str(exc).replace(api_key(), "<key>")
    except (OSError, AssertionError):
        return str(exc)


async def run_mode(mode: str) -> bool:
    try:
        return await MODES[mode]()
    except Exception as exc:
        print(f"      {type(exc).__name__}: {redacted(exc)}")
        return check(f"mode {mode} ran to its end", False)


async def main() -> int:
    chosen = sys.argv[1:] or list(MODES)
    if unknown := [mode for mode in chosen if mode not in MODES]:
        sys.exit(f"unknown mode {', '.join(unknown)}: expected some of {' '.join(MODES)}")
    print(f"scratch root: {ROOT}")
    build_scratch()
    results = [await run_mode(mode) for mode in chosen]
    ok = all(results)
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
