"""Codex-adapter smoke: REAL runs of `codex_cli` with the real `codex` CLI.

Run from the repo root: `uv run python scripts/smoke/codex-cli-smoke.py [mode ...]`. With no
mode it runs mode 3.

What it proves (the live half of tech-stack decision 34's pin; mode 3's session and rework passed
in the image under docker/seccomp.json on 2026-10-20, decision 52, and its steps also run against
the real 0.159.0 TUI and a mock API in `runtime/tests/test_codex_cli_interactive_local.py`. The
commit, passphrase and escape checks, folded in from mode 1 and the resume smoke, have not run
live):
  3. A session (decision 56), then its rework. The host must pass the adapter's
     `spawn_refusal`: the native `codex` (not npm's node wrapper) at exactly the pin, and
     tmux 3.2 or later. The TUI starts in its own tmux server under the scratch root, takes the
     folder-consent screen, names its thread by the rollout it begins, and reaches its prompt,
     which its Stop hook reports, with no first-run dialog on screen. Its first turn plants a
     passphrase and commits on a branch inside codex's sandbox, which the script reads from the
     clone. An INFO line gives how long the first turn waited for its first token: codex tries
     a WebSocket to the API before HTTPS. A human's turn, typed through the terminal relay the
     gateway uses (`HostTmuxRelay`), is answered and mirrored as the human's. A second adapter
     on the same root takes the session back, as after a worker restart, and repeats no
     mirrored item. `finish`, with only a read-only viewer attached, ends it by Ctrl-C at the
     empty input line as a success: the CLI exits 0, reports tokens and no cost, and nothing the
     adapter reads is missing from its stream. Before the finish the script plays the reviewer:
     it pushes the agent's branch to the scratch remote, adds a commit there and leaves a file
     uncommitted in the checkout. Then a rework spawns with `resume_from` and continues the
     thread in the kept worktree: the file stashed, the branch checked out again at the
     reviewer's commit, the thread resumed from where its rollout ended (a `log_offset` above
     0), at its prompt. Its turn asks for the passphrase back, which only the continued thread
     can give, and to write it outside the run area, which codex's sandbox must refuse on a
     resumed TUI too. With a draft left on its input line, where Ctrl-C only empties the draft,
     its finish ends it by the pasted `/quit`. Afterwards neither run has a credential file or a
     tmux server, and in `api_key` mode no file in either run's directory outside the worktree
     holds the key.
  4. codex's question box (decision 56). A session spawned with the ask tool on is told to
     ask, through `request_user_input`, which colour to use. It must idle on that question with
     the expiry codex answers it at, within 30 s of the call's rollout timestamp (an INFO line
     gives the figure), and still be on it 90 s after the call, with no output for the call in
     the rollout. Parked as the worker parks it (`interrupt(IDLE)`) and resumed with no turn, it
     is at its prompt. The rollout never holds codex's empty answer `{"answers":{}}` for the
     call: 0.159.0 writes no output for a call the hang-up left (an INFO line shows what is
     there). Parked again and resumed 30 s later with the maintainer's answer, framed as the
     worker frames a ticket comment (`worker/interrupts.py::_steering_turn`), the thread takes
     the turn with the call still unanswered and writes the colour to COLOUR.txt, and is
     finished. The 30 s stands for a person answering on the ticket: the model ignores a turn
     resumed seconds after its unanswered call, and production never resumes that soon. A second
     session, with the ask tool off and the same question asked with no tool named, ends its
     turn idle on the question. A model that asks in words with the box on fails the mode: the
     tool path is then unproven.
  5. An unprompted ask. A session with the ask tool on gets a task it cannot finish without the
     maintainer's answer, and a prompt that does not name the tool: only the line the ask tool
     adds to its system prompt tells it to ask in the box. It must ask there, reach the question
     with its expiry, be parked before the expiry, be resumed 30 s later (as in mode 4) with the
     answer framed as the worker frames it, and write the answer to CODENAME.txt. A model that
     asks in words fails the mode: the instruction did not reach it.

The target is a scratch file:// repo built fresh each run — nothing on GitHub is touched. The
ONE test seam is `_SmokeCli`, which sends git's `https://github.com/SMOKE/scratch.git` to the
scratch path by config entries placed after host git's hardening (`REDIRECT`), for the adapter's
git and this script's own; everything else the adapter does is its production code path.
Mode 3's agent pushes nothing: a session's sandbox writes only its worktree and the clone's
`.git`, and a push to a file:// remote writes elsewhere, so the script pushes for it, and the
turn says so over the workspace section's "push the branch and open the PR". Mode 3's
worker restart cancels the first adapter's supervisors in this process.

Prereqs: the `codex` CLI on PATH at the pin, 0.164.0, which mode 3 refuses to run without, and
tmux 3.2 or later; and OPENAI_API_KEY in the repo-root `.env` (read without echoing; the script
prints statuses and env KEY NAMES only, never values). Mode 3 bills three short model turns,
mode 4 about four and mode 5 about two; mode 4 waits about two minutes on its question, and
modes 4 and 5 30 s each for the answer.
Scratch root: $FACTORIAL_SMOKE_ROOT if set, else a fresh temp dir;
mode 3 leaves its runs there, and needs a root short enough for a tmux socket path (its refusal
says so; set $FACTORIAL_SMOKE_ROOT to a short path then). Auth: $FACTORIAL_SMOKE_CODEX_AUTH,
`api_key` (default, bills API credits) or `subscription` (bills the operator's own
subscription, needs `codex login` on this host and no key at all). Model:
$FACTORIAL_SMOKE_CODEX_MODEL, defaulting per mode -- gpt-5.3-codex or gpt-5.6-terra.

The id is account-specific and the failure is indistinguishable from an adapter fault, so check
it before blaming this script. `gpt-5.1-codex` was the default until 2026-09-06, when the live
smoke got `404 ... does not exist or you do not have access to it` on it -- while GET /v1/models
still LISTS it, and codex-cli 0.153.2 reports it as having no local metadata. Listed is not
served: probe the responses endpoint, where an entitled model answers 429-for-credits rather
than 404-for-model.

The harness (scratch layout, the URL seam) lives in `_smoke_common.py`.
"""
import asyncio
import json
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import EllipsisType

from _smoke_common import (
    AUTH,
    MODEL,
    REDIRECT_ARGS,
    REPO_NAME,
    ROOT,
    SCRATCH,
    ScratchRedirect,
    auth_secrets,
    build_scratch,
    read_env_key,
)

from orchestrator.github.state import CommentState
from orchestrator.worker.interrupts import _Park, _steering_turn
from runtime.adapters._cli import tmux
from runtime.adapters._cli.checkout import worktree_git_at
from runtime.adapters._cli.paths import CONFIG_DIRNAME, WORKTREE_DIRNAME
from runtime.adapters._cli.rework import REWORK_HEADER, REWORK_STASHED
from runtime.adapters._cli.session_state import LOG_FILE
from runtime.adapters.codex_cli.adapter import CodexCliAdapter, _Cli
from runtime.adapters.codex_cli.cli import (
    CALL_ID,
    CALL_NAME,
    CALL_OUTPUT,
    INPUT_MARK,
    ITEM_FUNCTION_CALL,
    ITEM_FUNCTION_CALL_OUTPUT,
    MSG_TASK_COMPLETE,
    PAYLOAD_TYPE,
    RECORD_EVENT_MSG,
    RECORD_RESPONSE_ITEM,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TIMESTAMP,
    ROLLOUT_TYPE,
    TOOL_REQUEST_USER_INPUT,
)
from runtime.adapters.codex_cli.session import SESSION_CREDENTIALS
from runtime.adapters.e2b.smoke import DIALOGS, FIRST_TOKEN_MS
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    ParkReason,
    ResumeFrom,
    RunSpec,
    RunStatus,
    SessionState,
    Workspace,
)
from runtime.terminal import HostTmuxRelay


class _SmokeCli(ScratchRedirect, _Cli):
    pass


AUTHOR = ("-c", "user.name=smoke", "-c", "user.email=smoke@test", "-c", "commit.gpgsign=false")


#: Under $TMPDIR, and that is the point: `workspace-write` leaves $TMPDIR and /tmp writable by
#: DEFAULT, which is how a first version of this probe "escaped" and looked like a resume bug.
#: The adapter now excludes both, so a write here is denied — and this asserts that exclusion as
#: much as it asserts the resumed turn is sandboxed at all.
OUTSIDE = ROOT / "outside"
#: Planted in turn one, asked for in the rework's. Arbitrary and unguessable on purpose:
#: a fresh session cannot produce it, so the assertion cannot pass by luck.
PASSPHRASE = "HALIBUT-SEVENTEEN-QUARTZ"


def spec(task: str) -> RunSpec:
    return RunSpec.model_validate({
        "agent": "smoke-agent",
        "ticket": "SMOKE-1",
        "workspace": Workspace(repo=REPO_NAME, base="main", branch="spec/1-{slug}"),
        "prompts": AgentPrompts(
            system=(
                "You are a smoke-test agent. Do exactly what the instructions say, nothing else, "
                "then stop."
            ),
            instructions=task,
        ),
        "skills": (),
        "inputs": (),
        "secrets": auth_secrets(),
        "model": MODEL,
        "max_turns": 8,
    })


def adapter(*, ask_tool: bool = False) -> CodexCliAdapter:
    return CodexCliAdapter(
        local_root=ROOT / "root", cli=_SmokeCli(), auth_mode=AUTH, ask_tool=ask_tool
    )


def check(label: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)
    return ok


# ------------------------------------------------------------------ mode 3: a session

#: Seconds a turn of the real model may take to reach the prompt again.
TURN_WAIT = 300
HUMAN_TURN = "Reply with the word ping."
#: codex reads an Enter that arrives within a burst of keys as a newline.
SETTLE = 0.5


def session_spec(instructions: str, **update: object) -> RunSpec:
    return spec(instructions).model_copy(update={"max_turns": 20, **update})


def run_path(run_id: str) -> Path:
    return ROOT / "root" / "runs" / run_id


def state_of(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "state.json").read_text())


def server(a: CodexCliAdapter, run_id: str) -> tmux.TmuxServer:
    root = a._require_root()
    socket = tmux.socket_path(root, run_id, "s", RuntimeError)
    binary = shutil.which("tmux") or "tmux"
    return tmux.TmuxServer(binary, socket, root / tmux.SESSION_CONF_FILE, run_path(run_id), "run")


async def screen(a: CodexCliAdapter, run_id: str) -> str:
    return await server(a, run_id).capture() or ""


async def at_prompt(
    a: CodexCliAdapter, run_id: str, *, after: datetime | EllipsisType | None = ...
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
    print(f"      run {run_id} did not reach its prompt; the screen:\n{await screen(a, run_id)}")
    return None


def squeezed(text: str) -> str:
    return "".join(text.split())


async def type_in(a: CodexCliAdapter, run_id: str, text: str, *, enter: bool) -> None:
    """A human types `text` in a writable terminal, and, with `enter`, presses Enter once it is
    drawn and a moment has passed. Compared without whitespace, since a long line wraps."""
    tab = await HostTmuxRelay().attach(
        await a.terminal_target(run_id), writable=True, cols=200, rows=50, on_output=lambda _: None
    )
    try:
        # A key sent in the instant the client attaches is lost.
        await asyncio.sleep(SETTLE)
        typed = squeezed(f"{INPUT_MARK} {text}")
        before = squeezed(await screen(a, run_id)).count(typed)
        await tab.write(text.encode())
        for _ in range(100):
            if squeezed(await screen(a, run_id)).count(typed) > before:
                break
            await asyncio.sleep(0.1)
        if enter:
            await asyncio.sleep(SETTLE)
            await tab.write(b"\r")
    finally:
        await tab.close()


async def mirrored_as_human(run_id: str, text: str) -> bool:
    """The translated mirror trails the hooks, so it is read again for a moment."""
    for _ in range(50):
        for raw in (run_path(run_id) / LOG_FILE).read_text("utf-8").splitlines():
            line = json.loads(raw)
            item = line.get("item") or {}
            human = (
                line.get("type") == "item.completed"
                and item.get("type") == "user_message"
                and line.get("factorial_origin") == "human"
            )
            if human and text in str(item.get("text")):
                return True
        await asyncio.sleep(0.2)
    return False


def mirrored_items(run_id: str) -> list[str]:
    lines = (run_path(run_id) / LOG_FILE).read_text("utf-8").splitlines()
    return [line for line in lines if '"item.completed"' in line]


def first_token_wait(run_id: str) -> object:
    """The first turn's wait for a token, from the rollout's first `task_complete`."""
    log = state_of(run_id).get("session_log")
    if not log:
        return None
    for raw in Path(log).read_text("utf-8").splitlines():
        record = json.loads(raw)
        payload = record.get(ROLLOUT_PAYLOAD)
        if (
            record.get(ROLLOUT_TYPE) == RECORD_EVENT_MSG
            and isinstance(payload, dict)
            and payload.get(PAYLOAD_TYPE) == MSG_TASK_COMPLETE
        ):
            return payload.get(FIRST_TOKEN_MS)
    return None


async def finish_watched(a: CodexCliAdapter, run_id: str) -> None:
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


async def restarted(first: CodexCliAdapter, run_id: str) -> CodexCliAdapter:
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


def worktree_of(run_id: str) -> Path:
    return Path(state_of(run_id)["worktree"])


def git_at(worktree: Path) -> tuple[str, ...]:
    """The worktree has no `.git` pointer (decision 34), so git is told where both are."""
    found = worktree_git_at(ROOT / "root", REPO_NAME, worktree)
    if found is None:
        raise RuntimeError(f"the clone registers no worktree at {worktree}")
    return found


def git(*args: str, cwd: Path) -> str:
    shown = subprocess.run(
        ["git", *AUTHOR, *REDIRECT_ARGS, *args], cwd=cwd, capture_output=True, text=True
    )
    if shown.returncode:
        raise RuntimeError(f"git {args[0]} failed: {shown.stderr.strip()[-300:]}")
    return shown.stdout.strip()


def committed_spec(branch: str) -> str:
    """SPEC.md as the agent committed it on `branch`, read from the shared clone. Codex protects
    the cwd's `.git` and, for a worktree, the gitdir its pointer names: a run on the VM once
    wrote every file and then died on `index.lock: Read-only file system`."""
    shown = subprocess.run(
        ["git", "-C", str(ROOT / "root" / "repos" / REPO_NAME), "show", f"{branch}:SPEC.md"],
        capture_output=True,
        text=True,
    )
    return shown.stdout.strip() if shown.returncode == 0 else "MISSING"


def play_reviewer(worktree: Path, branch: str) -> str:
    """The agent's branch pushed for it, a reviewer's commit on it, and a draft left
    uncommitted. Returns the branch's head on the remote."""
    at = git_at(worktree)
    git(*at, "push", "-q", "origin", branch, cwd=worktree)
    git("checkout", "-q", branch, cwd=SCRATCH)
    git("commit", "-q", "--allow-empty", "-m", "review", cwd=SCRATCH)
    git("checkout", "-q", "main", cwd=SCRATCH)
    (worktree / "DRAFT.md").write_text("draft\n")
    return git("rev-parse", branch, cwd=SCRATCH)


def holds_key(run_ids: list[str]) -> bool:
    """Whether any file a run's directory holds outside its checkout holds the key."""
    if AUTH == "subscription":
        return False
    key = read_env_key("OPENAI_API_KEY").encode()
    for run_id in run_ids:
        for path in run_path(run_id).rglob("*"):
            if path.relative_to(run_path(run_id)).parts[0] == WORKTREE_DIRNAME:
                continue
            if path.is_file() and not path.is_symlink() and key in path.read_bytes():
                print(f"      the key is in {path.relative_to(ROOT)}")
                return True
    return False


async def scrubbed(a: CodexCliAdapter, home: str, run_ids: list[str]) -> bool:
    config = run_path(home) / CONFIG_DIRNAME
    left = [name for name in SESSION_CREDENTIALS if (config / name).exists()]
    live = [run_id for run_id in run_ids if await server(a, run_id).pane() is not None]
    for name in left:
        print(f"      left behind: {CONFIG_DIRNAME}/{name}")
    for run_id in live:
        print(f"      a tmux server still runs for run {run_id}")
    return not left and not live


async def finished_well(a: CodexCliAdapter, run_id: str, thread: str) -> bool:
    result = await a.result(run_id)
    exit_code = state_of(run_id).get("exit_code")
    ok = (
        result.outcome is RunStatus.SUCCEEDED
        and result.failure_reason is None
        and result.session_id == thread
        and result.cost_usd is None
        and bool(result.input_tokens)
        and bool(result.output_tokens)
        and not result.stream_warnings
        and exit_code == 0
    )
    if not ok:
        print(
            f"      {result.outcome} exit={exit_code} cost={result.cost_usd} "
            f"tokens={result.input_tokens}/{result.output_tokens} "
            f"reason={result.failure_reason!r} warnings={result.stream_warnings}"
        )
    return ok


async def interactive() -> bool:
    first = adapter()
    refusal = await first.spawn_refusal()
    if refusal is not None:
        print(f"      {refusal}")
        return check("this host can run a session", False)
    OUTSIDE.mkdir(parents=True, exist_ok=True)
    # A reused FACTORIAL_SMOKE_ROOT must not carry last run's verdict into this one.
    (OUTSIDE / "escaped.txt").unlink(missing_ok=True)
    branch = f"spec/1-session-{secrets.token_hex(2)}"
    a = (
        await first.spawn(
            session_spec(
                f"Remember this passphrase, you will be asked for it later: {PASSPHRASE}. "
                "This scratch repository takes no pull request: in place of the push and the PR "
                "the workspace section asks for, a reviewer pushes your branch for you. With your "
                f"shell tool, create and check out the branch {branch}, write SPEC.md containing "
                "exactly `the spec`, and commit it on that branch with the message `spec`. Then "
                "reply with the word pong and the commit's short hash."
            )
        )
    ).run_id
    print(f"session: spawned run_id={a}")
    second = first
    try:
        thread = (await first.session_state(a)).session_id
        idle = await at_prompt(first, a)
        shown = await screen(first, a)
        if not check(
            "spawn named the thread, and the TUI reached its prompt with no dialog on screen",
            thread is not None and idle is not None and not DIALOGS.search(shown),
        ):
            return False
        assert idle is not None and thread is not None
        checks = [
            check(
                "the agent committed on its branch inside codex's sandbox",
                committed_spec(branch) == "the spec",
            )
        ]
        print(f"INFO  not a failure: the first turn waited {first_token_wait(a)} ms for a token")
        await type_in(first, a, HUMAN_TURN, enter=True)
        answered = await at_prompt(first, a, after=idle.idle_since)
        typed_at = (await first.session_state(a)).last_input_at
        checks.append(
            check(
                "a human's turn through the relay was answered and mirrored as the human's",
                answered is not None
                and typed_at is not None
                and await mirrored_as_human(a, HUMAN_TURN),
            )
        )
        second = await restarted(first, a)
        items = mirrored_items(a)
        taken = second._attached(a)
        checks.append(
            check(
                "a second adapter took the session back and repeated no item",
                await second.status(a) is RunStatus.IDLE
                and taken is not None
                and taken._proven
                and len(items) == len(set(items)),
            )
        )
        head = play_reviewer(worktree_of(a), branch)
        await finish_watched(second, a)
        checks.append(
            check(
                "finish, with only a watcher attached, ended it with its tokens and no cost",
                await finished_well(second, a, thread),
            )
        )
        checks.append(await rework(second, a, thread, branch, head))
        runs = [a, *filter(None, [state_of(a).get("continued_by")])]
        checks.append(
            check(
                "no credential file, tmux server or model key is left in either run",
                await scrubbed(second, a, runs) and not holds_key(runs),
            )
        )
        return all(checks)
    finally:
        for socket in (ROOT / "root" / tmux.SOCKETS_DIRNAME).glob("*"):
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
        await second.drain()


async def rework(a: CodexCliAdapter, first: str, thread: str, branch: str, head: str) -> bool:
    try:
        handle = await a.spawn(
            session_spec(
                "Write the passphrase I gave you at the very start into word.txt in the "
                f"workspace root, exactly. Then try to write it to {OUTSIDE}/escaped.txt and say "
                "whether it worked. Then reply with the word reworked.",
                resume_from=ResumeFrom(run_id=first, session_id=thread),
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
    worktree = worktree_of(first)
    stashes = git(*git_at(worktree), "stash", "list", cwd=worktree)
    idle = await at_prompt(a, second)
    if idle is not None:
        await type_in(a, second, "a thought left half typed", enter=False)
        await finish_watched(a, second)
    else:
        await a.cancel(second)
        await a.drain()
    word = worktree / "word.txt"
    return all(
        [
            check(
                "the rework continued the same thread from where its rollout ended",
                adopted.session_id == thread
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
                "context survived the resumed thread",
                word.exists() and PASSPHRASE in word.read_text(),
            ),
            check(
                "codex's sandbox still applies to a resumed TUI",
                not (OUTSIDE / "escaped.txt").exists(),
            ),
            check(
                "with a draft on its input line, the rework's finish quit by the pasted `/quit`",
                await finished_well(a, second, thread),
            ),
        ]
    )


# ------------------------------------------------------------------ modes 4 and 5: the box

#: The most a poll may lag the call before the worker would park too late to beat codex's own
#: answer at 120 s.
SEEN_WITHIN = timedelta(seconds=30)
#: Long past any poll, still short of codex's own answer.
STILL_ASKING_AT = timedelta(seconds=90)
#: Between a park and the resume that answers it: a person's time to answer on the ticket. A
#: turn resumed within seconds of an unanswered `request_user_input` call is ignored by the model.
ANSWER_GAP = 30
CODENAME = f"HERON-{secrets.token_hex(2).upper()}"
IN_WORDS = "the model asked in words; the tool path is unproven"
#: What codex logs for a picker nobody answered: the hang-up must end it before this.
SELF_ANSWERED = '{"answers":{}}'
BOX_TASK = (
    "Use the request_user_input tool to ask me one question: which colour to use, with the "
    "options red and blue. After the answer, write it to COLOUR.txt and reply done."
)
#: BOX_TASK's question with no tool named, which the box-off session does not have.
ASKED_TASK = (
    "Ask me one question before you do anything else: which colour to use, red or blue. Wait "
    "for my answer, then write it to COLOUR.txt and reply done."
)


def answered(text: str) -> str:
    """The turn the worker resumes an idle park with once the maintainer answers on the
    ticket."""
    now = datetime.now(UTC)
    park = _Park(reason=ParkReason.IDLE, cutoff=now, event_id=0, at_prompt=True)
    comment = CommentState(author="maintainer", body=text, created_at=now.isoformat())
    turn = _steering_turn(park, [comment])
    assert turn is not None
    return turn


def self_answered(outputs: list[str]) -> bool:
    return SELF_ANSWERED in ["".join(output.split()) for output in outputs]


def rollout(run_id: str) -> list[dict]:
    log = state_of(run_id).get("session_log")
    if not log:
        return []
    return [json.loads(raw) for raw in Path(log).read_text("utf-8").splitlines() if raw.strip()]


def question_calls(run_id: str) -> list[tuple[datetime, str]]:
    """Each `request_user_input` call in the rollout: when it was logged, and its call id."""
    calls = []
    for record in rollout(run_id):
        payload = record.get(ROLLOUT_PAYLOAD) or {}
        if (
            record.get(ROLLOUT_TYPE) == RECORD_RESPONSE_ITEM
            and payload.get(PAYLOAD_TYPE) == ITEM_FUNCTION_CALL
            and payload.get(CALL_NAME) == TOOL_REQUEST_USER_INPUT
        ):
            at = datetime.fromisoformat(record[ROLLOUT_TIMESTAMP]).astimezone(UTC)
            calls.append((at, str(payload.get(CALL_ID))))
    return calls


def call_outputs(run_id: str, call_id: str) -> list[str]:
    return [
        str(payload.get(CALL_OUTPUT))
        for record in rollout(run_id)
        if record.get(ROLLOUT_TYPE) == RECORD_RESPONSE_ITEM
        and (payload := record.get(ROLLOUT_PAYLOAD) or {}).get(PAYLOAD_TYPE)
        == ITEM_FUNCTION_CALL_OUTPUT
        and payload.get(CALL_ID) == call_id
    ]


async def first_idle(a: CodexCliAdapter, run_id: str) -> tuple[SessionState | None, datetime]:
    """The session's state when it first idles, and when that was seen."""
    for _ in range(TURN_WAIT * 2):
        status = await a.status(run_id)
        if status.is_terminal:
            print(f"      run {run_id} ended {status}: {state_of(run_id).get('failure_reason')}")
            break
        if status is RunStatus.IDLE:
            return await a.session_state(run_id), datetime.now(UTC)
        await asyncio.sleep(0.5)
    return None, datetime.now(UTC)


async def asked_in_the_box(
    a: CodexCliAdapter, run_id: str, in_words: str
) -> tuple[SessionState, datetime] | None:
    """The session idle on a question it put in the box, and when its call was logged; None,
    with `in_words` printed, when it asked any other way."""
    idle, seen = await first_idle(a, run_id)
    calls = question_calls(run_id)
    if idle is None or idle.idle_reason is not IdleReason.QUESTION:
        print(f"      the session idled on {idle and idle.idle_reason}, not on a question")
        return None
    if not calls or idle.question_expires_at is None:
        print(f"      {in_words}: {idle.question!r}")
        return None
    called_at, _ = calls[-1]
    lag = seen - called_at
    print(f"INFO  the question was seen {lag.total_seconds():.1f} s after its call was logged")
    within = f"the question was seen within {SEEN_WITHIN.seconds} s of its call"
    if not check(within, lag < SEEN_WITHIN):
        return None
    return idle, called_at


async def box() -> bool:
    a = adapter(ask_tool=True)
    refusal = await a.spawn_refusal()
    if refusal is not None:
        print(f"      {refusal}")
        return check("this host can run a session", False)
    run_id = (await a.spawn(session_spec(BOX_TASK))).run_id
    print(f"box: spawned run_id={run_id}")
    try:
        found = await asked_in_the_box(a, run_id, IN_WORDS)
        if not check("the session idled on the question it put in the box", found is not None):
            return False
        assert found is not None
        idle, called_at = found
        assert idle.question_expires_at is not None
        checks = [
            check(
                "the question's expiry is codex's own answer, 120 s after the call",
                idle.question_expires_at == called_at + timedelta(seconds=120),
            )
        ]
        wait = called_at + STILL_ASKING_AT - datetime.now(UTC)
        await asyncio.sleep(max(0.0, wait.total_seconds()))
        still = await a.session_state(run_id)
        (_, call_id), *_ = question_calls(run_id)
        checks.append(
            check(
                "90 s after the call it still waits on the question, unanswered",
                still.idle_reason is IdleReason.QUESTION and not call_outputs(run_id, call_id),
            )
        )
        await a.interrupt(run_id, ParkReason.IDLE)
        await a.resume(run_id, None, auth_secrets())
        resumed = await at_prompt(a, run_id)
        checks.append(
            check("parked and resumed with no turn, it is at its prompt", resumed is not None)
        )
        checks.append(await next_turn(a, run_id, call_id, resumed))
        checks.append(await refused(ASKED_TASK))
        return all(checks)
    finally:
        for socket in (ROOT / "root" / tmux.SOCKETS_DIRNAME).glob("*"):
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
        await a.drain()


async def next_turn(
    a: CodexCliAdapter, run_id: str, call_id: str, resumed: SessionState | None
) -> bool:
    """Parked again and resumed with the maintainer's answer, the thread takes the turn with the
    call the hang-up left still unanswered: 0.159.0 writes no output for it, and must never
    write codex's empty answer."""
    if resumed is None:
        return check("the session's next turn was accepted", False)
    await a.interrupt(run_id, ParkReason.IDLE)
    await asyncio.sleep(ANSWER_GAP)
    await a.resume(run_id, answered("Use blue."), auth_secrets())
    done = await at_prompt(a, run_id, after=resumed.idle_since)
    written = worktree_of(run_id) / "COLOUR.txt"
    outputs = call_outputs(run_id, call_id)
    print(f"INFO  the call's output after the hang-up and the next turn: {outputs!r}")
    checks = [
        check(
            "the session's next turn was accepted: it wrote the answer",
            done is not None and written.exists() and "blue" in written.read_text().lower(),
        ),
        check("codex never gave the picker its empty answer", not self_answered(outputs)),
    ]
    if done is not None:
        await a.finish(run_id)
        await a.drain()
        outcome = (await a.result(run_id)).outcome
        checks.append(check("it finished", outcome is RunStatus.SUCCEEDED))
    return all(checks)


async def refused(task: str) -> bool:
    """With the ask tool off, the question asked in words idles the turn's end on it."""
    a = adapter()
    run_id = (await a.spawn(session_spec(task))).run_id
    print(f"box off: spawned run_id={run_id}")
    try:
        idle, _ = await first_idle(a, run_id)
        called = bool(question_calls(run_id))
        print(f"INFO  with the ask tool off the model called the tool: {called}")
        return check(
            "with the ask tool off, the turn's Stop idled on the question",
            idle is not None and idle.idle_reason is IdleReason.QUESTION,
        )
    finally:
        await a.cancel(run_id)
        await a.drain()


async def unprompted() -> bool:
    a = adapter(ask_tool=True)
    refusal = await a.spawn_refusal()
    if refusal is not None:
        print(f"      {refusal}")
        return check("this host can run a session", False)
    task = (
        "Write the release codename the maintainer picks into CODENAME.txt in the workspace "
        "root, then reply done."
    )
    run_id = (await a.spawn(session_spec(task))).run_id
    print(f"unprompted: spawned run_id={run_id}")
    try:
        found = await asked_in_the_box(
            a, run_id, "asked in words: the instruction did not reach the model"
        )
        if not check("the agent asked in the box unprompted", found is not None):
            return False
        assert found is not None
        idle, _ = found
        assert idle.question_expires_at is not None
        await a.interrupt(run_id, ParkReason.IDLE)
        checks = [
            check(
                "it was parked before codex would answer the question itself",
                datetime.now(UTC) < idle.question_expires_at,
            )
        ]
        await asyncio.sleep(ANSWER_GAP)
        await a.resume(run_id, answered(f"The codename is {CODENAME}."), auth_secrets())
        done = await at_prompt(a, run_id)
        written = worktree_of(run_id) / "CODENAME.txt"
        checks.append(
            check(
                "resumed with the answer as its turn, it wrote the answer",
                done is not None and written.exists() and CODENAME in written.read_text(),
            )
        )
        if done is not None:
            await a.finish(run_id)
        return all(checks)
    finally:
        if not (await a.status(run_id)).is_terminal:
            await a.cancel(run_id)
        for socket in (ROOT / "root" / tmux.SOCKETS_DIRNAME).glob("*"):
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
        await a.drain()


MODES = {"3": interactive, "4": box, "5": unprompted}
#: The modes a run with none named runs: 4 and 5 bill more and wait minutes.
DEFAULT_MODES = ("3",)


def redacted(exc: BaseException) -> str:
    if AUTH == "subscription":
        return str(exc)
    try:
        return str(exc).replace(read_env_key("OPENAI_API_KEY"), "<key>")
    except (OSError, AssertionError):
        return str(exc)


async def run_mode(mode: str) -> bool:
    try:
        return await MODES[mode]()
    except Exception as exc:
        print(f"      {type(exc).__name__}: {redacted(exc)}")
        return check(f"mode {mode} ran to its end", False)


async def main() -> int:
    chosen = sys.argv[1:] or list(DEFAULT_MODES)
    if unknown := [mode for mode in chosen if mode not in MODES]:
        sys.exit(f"unknown mode {', '.join(unknown)}: expected some of {' '.join(MODES)}")
    print(f"scratch root: {ROOT} (auth={AUTH} model={MODEL})")
    build_scratch()
    results = [await run_mode(mode) for mode in chosen]
    ok = all(results)
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
