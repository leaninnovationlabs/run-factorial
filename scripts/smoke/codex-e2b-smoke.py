"""E2B smoke: REAL runs of `codex_e2b` in real E2B sandboxes from the pinned template.

Run from the repo root: `uv run python scripts/smoke/codex-e2b-smoke.py [mode ...]`. With no
mode it runs 1, 2 and 3 in order; `... 2` runs the question box alone.

What it proves (every production codex project runs on E2B):
  1. A session (decision 56): the builder's session smoke's steps
     (`session_smoke_run`) with the codex harness. The TUI takes its folder consent, names
     its thread by the rollout it begins, and reaches its prompt, which its Stop hook reports,
     with no first-run dialog on screen; waits there 75 s; is found whole by a second adapter on
     the same state root, as after a worker restart; comes back on the same thread after a
     rollover; and ends on `finish` as a success, its rollout translated whole.
  2. codex's question box, as `codex-cli-smoke.py` mode 4 runs it on a host, on
     `CodexE2bAdapter(ask_tool=True)`: the session idles on the question it was told to put in
     the box, with codex's own answer 120 s after the call as its expiry, is seen there within
     30 s of the call's rollout timestamp (an INFO line gives the figure), and still waits on it
     unanswered 90 s after the call. Parked as the worker parks it and resumed with no turn, it
     is at its prompt. Parked again and resumed 30 s later with the maintainer's answer, framed
     as the worker frames a ticket comment (`worker/interrupts.py::_steering_turn`), the thread
     takes the turn with the call still unanswered and writes the colour to COLOUR.txt, read
     from the sandbox; it is finished. The rollout never holds the empty answer
     `{"answers":{}}` codex gives a picker nobody answered: 0.159.0 writes no output for a call
     the hang-up left (an INFO line shows what is there). A second session with the ask tool
     off, asked the same question with no tool named, ends its turn idle on the question. A
     model that asks in words with the box on fails the mode: the tool path is then unproven.
  3. An unprompted ask, as `codex-cli-smoke.py` mode 5 runs it: a task that needs the
     maintainer's answer and a prompt that does not name the tool. The agent must ask in the
     box, be parked before codex's own answer, be resumed 30 s later with the answer framed as
     the worker frames it, and write the answer to CODENAME.txt, read from the sandbox. The 30 s
     in modes 2 and 3 is `codex-cli-smoke.py`'s person answering: the sandbox's pause and
     resume alone have brought mode 3's turn back 11-15 s after the call, and what counts is the
     time since the hang-up, not since the call, as the host's mode 4 shows. A model that asks
     in words fails the mode: the ask tool's line in the system prompt did not reach it.
No mode's mirrors may hold the model key, and each mode kills its sandboxes.

Every run's sandbox carries `factorial_deployment=smoke`; the script fails if one is left. A
mode that raises prints FAIL with the exception's type, the next mode runs, and the check for a
sandbox left behind runs last whatever happened, killing any it finds.

Prereqs: E2B_API_KEY (the team to run in), E2B_CODEX_TEMPLATE (a codex build in that team that
passed the session smoke, `make e2b-template` prints one) and OPENAI_API_KEY in the repo-root
`.env` (read without echoing; the script prints statuses only, never values). The orchestrator
reads none of the three: a runtime holds its own. Every mode clones the public
octocat/Hello-World and writes nothing to GitHub. Mode 1 bills two short model turns, mode 2
about four and mode 3 about two; mode 2 waits about two minutes on its question, and modes 2
and 3 30 s each for the answer.
"""

import asyncio
import json
import os
import re
import secrets
import shlex
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import EllipsisType

from pydantic import SecretStr

from orchestrator.github.state import CommentState
from orchestrator.worker.interrupts import _Park, _steering_turn
from runtime.adapters.codex_cli.cli import (
    CALL_ID,
    CALL_NAME,
    CALL_OUTPUT,
    ITEM_FUNCTION_CALL,
    ITEM_FUNCTION_CALL_OUTPUT,
    PAYLOAD_TYPE,
    RECORD_RESPONSE_ITEM,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TIMESTAMP,
    ROLLOUT_TYPE,
    TOOL_REQUEST_USER_INPUT,
)
from runtime.adapters.e2b import CodexE2bAdapter
from runtime.adapters.e2b.adapter import SESSION_MIRROR
from runtime.adapters.e2b.client import E2bSandboxClient
from runtime.adapters.e2b.layout import AGENT_USER, WORKSPACE
from runtime.adapters.e2b.smoke import session_smoke_run, smoke_spec
from runtime.contract import IdleReason, ParkReason, RunSpec, RunStatus, SessionState

ENV = {
    m[1]: m[2].strip().strip('"').strip("'")
    for line in Path(".env").read_text().splitlines()
    if (m := re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip()))
}
_NEEDED = ("E2B_API_KEY", "E2B_CODEX_TEMPLATE", "OPENAI_API_KEY")
if missing := [name for name in _NEEDED if not ENV.get(name)]:
    sys.exit(f"set {', '.join(missing)} in the repo-root .env (see .env.example); nothing ran")
E2B_KEY = ENV["E2B_API_KEY"]
MODEL_KEY = ENV["OPENAI_API_KEY"]
ROOT = Path(os.environ.get("FACTORIAL_SMOKE_ROOT") or tempfile.mkdtemp(prefix="e2b-smoke-"))
#: The secrets a resume is handed, as the worker hands it a freshly read key.
SECRETS = {"OPENAI_API_KEY": SecretStr(MODEL_KEY)}

#: Seconds a turn of the real model may take to reach the prompt again.
TURN_WAIT = 300
#: The most a poll may lag the call before the worker would park too late to beat codex's own
#: answer at 120 s.
SEEN_WITHIN = timedelta(seconds=30)
#: Long past any poll, still short of codex's own answer.
STILL_ASKING_AT = timedelta(seconds=90)
#: What codex logs for a picker nobody answered: the hang-up must end it before this.
SELF_ANSWERED = '{"answers":{}}'
#: Between a park and the resume that answers it: a person's time to answer on the ticket. A
#: turn resumed within seconds of an unanswered `request_user_input` call is ignored by the
#: model.
ANSWER_GAP = 30
CODENAME = f"HERON-{secrets.token_hex(2).upper()}"
IN_WORDS = "the model asked in words; the tool path is unproven"
BOX_TASK = (
    "Use the request_user_input tool to ask me one question: which colour to use, with the "
    "options red and blue. After the answer, write it to COLOUR.txt and reply done."
)
#: BOX_TASK's question with no tool named, which the box-off session does not have.
ASKED_TASK = (
    "Ask me one question before you do anything else: which colour to use, red or blue. Wait "
    "for my answer, then write it to COLOUR.txt and reply done."
)


def adapter(*, ask_tool: bool = False) -> CodexE2bAdapter:
    return CodexE2bAdapter(
        api_key=E2B_KEY,
        template=ENV["E2B_CODEX_TEMPLATE"],
        state_root=ROOT,
        deployment="smoke",
        poll_interval=1,
        ask_tool=ask_tool,
    )


def spec(instructions: str) -> RunSpec:
    base = smoke_spec("codex", MODEL_KEY)
    return base.model_copy(
        update={"prompts": base.prompts.model_copy(update={"instructions": instructions})}
    )


def check(label: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)
    return ok


def redacted(exc: BaseException) -> str:
    return str(exc).replace(MODEL_KEY, "<key>").replace(E2B_KEY, "<key>")


def run_dir(run_id: str) -> Path:
    return ROOT / "runs" / run_id


def sandbox_of(run_id: str) -> str:
    return json.loads((run_dir(run_id) / "state.json").read_text())["sandbox_id"]


def secret_in_mirrors(run_id: str, secret: str) -> bool:
    mirrors = ("stdout.jsonl", SESSION_MIRROR, "signals.jsonl")
    run = run_dir(run_id)
    return any(secret in (run / name).read_text() for name in mirrors if (run / name).exists())


async def kill(*run_ids: str) -> None:
    for run_id in run_ids:
        await E2bSandboxClient(E2B_KEY).kill(sandbox_of(run_id))


def rollout(run_id: str) -> list[dict]:
    """The rollout as the adapter mirrors it beside the run."""
    mirror = run_dir(run_id) / SESSION_MIRROR
    if not mirror.exists():
        return []
    lines = mirror.read_text("utf-8").splitlines()
    return [json.loads(raw) for raw in lines if raw.strip()]


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


async def first_idle(a: CodexE2bAdapter, run_id: str) -> tuple[SessionState | None, datetime]:
    """The session's state when it first idles, and when that was seen."""
    for _ in range(TURN_WAIT * 2):
        status = await a.status(run_id)
        if status.is_terminal:
            print(f"      run {run_id} ended {status}")
            break
        if status is RunStatus.IDLE:
            return await a.session_state(run_id), datetime.now(UTC)
        await asyncio.sleep(0.5)
    return None, datetime.now(UTC)


async def at_prompt(
    a: CodexE2bAdapter, run_id: str, *, after: datetime | EllipsisType | None = ...
) -> SessionState | None:
    """The session at its prompt after a turn's end, or after a resume with no turn, in an idle
    spell begun after `after`. `after` None is a failure: the earlier spell's start was never
    read, and the guard would pass the stale prompt."""
    if after is None:
        raise AssertionError("at_prompt: the earlier idle spell has no start to wait past")
    for _ in range(TURN_WAIT):
        status = await a.status(run_id)
        if status.is_terminal:
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


async def asked_in_the_box(
    a: CodexE2bAdapter, run_id: str, in_words: str
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


async def workspace_file(a: CodexE2bAdapter, run_id: str, name: str) -> str:
    """A file the agent wrote, read from its sandbox."""
    attached = a._attached(run_id)
    if attached is None:
        return ""
    path = shlex.quote(f"{WORKSPACE}/{name}")
    return (await attached[1].run(f"cat {path}", user=AGENT_USER)).stdout


async def next_turn(
    a: CodexE2bAdapter, run_id: str, call_id: str, resumed: SessionState
) -> bool:
    """Parked again and resumed with the maintainer's answer, the thread takes the turn with the
    call the hang-up left still unanswered: 0.159.0 writes no output for it, and must never
    write codex's empty answer."""
    await a.interrupt(run_id, ParkReason.IDLE)
    await asyncio.sleep(ANSWER_GAP)
    await a.resume(run_id, answered("Use blue."), SECRETS)
    done = await at_prompt(a, run_id, after=resumed.idle_since)
    written = await workspace_file(a, run_id, "COLOUR.txt") if done is not None else ""
    outputs = call_outputs(run_id, call_id)
    print(f"INFO  the call's output after the hang-up and the next turn: {outputs!r}")
    checks = [
        check(
            "the session's next turn was accepted: it wrote the answer",
            "blue" in written.lower(),
        ),
        check("codex never gave the picker its empty answer", not self_answered(outputs)),
    ]
    if done is not None:
        await a.finish(run_id)
        await a.drain()
        outcome = (await a.result(run_id)).outcome
        checks.append(check("it finished", outcome is RunStatus.SUCCEEDED))
    return all(checks)


async def session() -> bool:
    first = spec("Run `whoami` with your shell tool, then reply with the word pong.")
    result, problems = await session_smoke_run(adapter(), first, reattach=adapter)
    for problem in problems:
        print(f"      {problem}")
    try:
        return all(
            [
                check("the session smoke passed", not problems),
                check(
                    "the model key never reached the mirrors",
                    not secret_in_mirrors(result.run_id, MODEL_KEY),
                ),
            ]
        )
    finally:
        await kill(result.run_id)


async def box() -> bool:
    a = adapter(ask_tool=True)
    run_id = (await a.spawn(spec(BOX_TASK))).run_id
    print(f"box: spawned run_id={run_id}")
    runs = [run_id]
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
        await a.resume(run_id, None, SECRETS)
        resumed = await at_prompt(a, run_id)
        checks.append(
            check("parked and resumed with no turn, it is at its prompt", resumed is not None)
        )
        if resumed is not None:
            checks.append(await next_turn(a, run_id, call_id, resumed))
        off = adapter()
        refused = (await off.spawn(spec(ASKED_TASK))).run_id
        runs.append(refused)
        print(f"box off: spawned run_id={refused}")
        idle_off, _ = await first_idle(off, refused)
        called = bool(question_calls(refused))
        print(f"INFO  with the ask tool off the model called the tool: {called}")
        checks.append(
            check(
                "with the ask tool off, the turn's Stop idled on the question",
                idle_off is not None and idle_off.idle_reason is IdleReason.QUESTION,
            )
        )
        await off.cancel(refused)
        await off.drain()
        checks.append(
            check(
                "the model key never reached either run's mirrors",
                not any(secret_in_mirrors(run, MODEL_KEY) for run in runs),
            )
        )
        return all(checks)
    finally:
        await kill(*runs)


async def unprompted() -> bool:
    a = adapter(ask_tool=True)
    run_id = (
        await a.spawn(
            spec(
                "Write the release codename the maintainer picks into CODENAME.txt in the "
                "workspace root, then reply done."
            )
        )
    ).run_id
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
        await a.resume(run_id, answered(f"The codename is {CODENAME}."), SECRETS)
        done = await at_prompt(a, run_id)
        written = await workspace_file(a, run_id, "CODENAME.txt") if done is not None else ""
        checks.append(
            check(
                "resumed with the answer as its turn, it wrote the answer",
                CODENAME in written,
            )
        )
        if done is not None:
            await a.finish(run_id)
            await a.drain()
        checks.append(
            check(
                "the model key never reached the mirrors",
                not secret_in_mirrors(run_id, MODEL_KEY),
            )
        )
        return all(checks)
    finally:
        await kill(run_id)


async def none_left() -> bool:
    left = await E2bSandboxClient(E2B_KEY).list({"factorial_deployment": "smoke"})
    for sandbox in left:
        await E2bSandboxClient(E2B_KEY).kill(sandbox.sandbox_id)
    return check("no smoke sandbox left behind", not left)


MODES = {"1": session, "2": box, "3": unprompted}


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
    print(f"state root: {ROOT}")
    results: list[bool] = []
    try:
        for mode in chosen:
            results.append(await run_mode(mode))
    finally:
        results.append(await none_left())
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
