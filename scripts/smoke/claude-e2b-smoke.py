"""E2B smoke: REAL runs of `claude_e2b` in real E2B sandboxes from the pinned template.

Run from the repo root: `uv run python scripts/smoke/claude-e2b-smoke.py [mode ...]`. With no
mode it runs 3, 4 and 5 in order; `... 5` runs the session alone.

What it proves (3 not yet run; 4 first proven 2026-09-27 on a headless run, not yet as a session;
5 first proven 2026-10-23, PASS, on the factorial-claude-2-1-289 build, and re-run on the final
branch 2026-10-23, PASS):
  3. With FACTORIAL_SMOKE_REPO set to a scratch repo the App is installed on: a session's agent
     pushes a branch and opens a pull request, on a run token the sandbox never holds, and the
     session reaches its prompt and is finished there. The pull request is found host-side, and
     neither the token nor the model key, both injected at the egress proxy, reaches the run's
     mirrors. The token is minted the way a real run's is (`GitHubClient.mint_run_token`,
     contents and pull requests only), from the GitHub App connection of the project on that
     repo, in the database `.env` names, and is never printed.
     The mode kills its sandbox. Close the PR it opens afterwards.
  4. A preview (decision 54): a session kept for it writes a page and is finished at its prompt,
     which parks it, then wakes cold to serve it on a public URL with no credential rule; Stop
     parks it again, and the mode kills the sandbox so none is left. An INFO line says whether
     the public URL also reaches a service bound to 127.0.0.1 only, as many dev servers bind by
     default (it did on 2026-09-27). The agent is told not to push: its run holds no token, and
     an agent that tries spends its turn cap looking for one. A session that never reaches its
     prompt in modes 3 and 4 is cancelled, and fails the mode.
  5. A session (decision 56), then its rework. First the builder's interactive
     smoke's steps (`session_smoke_run`), on this mode's own prompt and branch: the TUI
     reaches its prompt, which only its Stop hook reports, with no first-run dialog on screen;
     waits there 75 s; is found whole by a second adapter on the same state root, as after a
     worker restart; comes back on the same session after a rollover; and ends on `finish` as a
     success, its session log translated whole. Its agent puts the checkout on the public repo's
     `test` branch and leaves a file uncommitted. Then a rework spawns with `resume_from` and
     adopts the kept sandbox: the draft stashed, the checkout reset to the branch's head as
     `git ls-remote` reads it from the sandbox, the session resumed on the same id from where its
     log ended, at its prompt, and finished as a success. Neither run's mirrors may hold the
     model key, and the mode kills the sandbox. It records no verdict: only the builder tags a
     build (`python -m orchestrator.e2b_template --interactive-smoke <pin>`), and the worker
     refuses a session on an untagged build.

Every run's sandbox carries `factorial_deployment=smoke`; the script fails if one is left. A
mode that raises prints FAIL with the exception's type, the next mode runs, and the check for a
sandbox left behind runs last whatever happened, killing any it finds.

Prereqs: E2B_API_KEY (the team to run in), E2B_CLAUDE_TEMPLATE (a claude build in that team,
`make e2b-template` prints one) and ANTHROPIC_API_KEY in the repo-root `.env` (read without
echoing; the script prints statuses only, never values). The orchestrator reads none of the
three: a runtime holds its own. Modes 4 and 5 clone the public octocat/Hello-World and write
nothing to GitHub.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from pydantic import SecretStr

from runtime.adapters._cli.rework import REWORK_STASHED
from runtime.adapters.e2b import ClaudeE2bAdapter
from runtime.adapters.e2b.client import E2bSandboxClient
from runtime.adapters.e2b.layout import AGENT_USER, turn_file
from runtime.adapters.e2b.smoke import session_smoke_run, smoke_spec
from runtime.contract import (
    IdleReason,
    PreviewPhase,
    PreviewServiceSpec,
    PreviewSpec,
    ResumeFrom,
    RunSpec,
    RunStatus,
    SessionState,
    Workspace,
)

ENV = {
    m[1]: m[2].strip().strip('"').strip("'")
    for line in Path(".env").read_text().splitlines()
    if (m := re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip()))
}
_NEEDED = ("E2B_API_KEY", "E2B_CLAUDE_TEMPLATE", "ANTHROPIC_API_KEY")
if missing := [name for name in _NEEDED if not ENV.get(name)]:
    sys.exit(f"set {', '.join(missing)} in the repo-root .env (see .env.example); nothing ran")
E2B_KEY = ENV["E2B_API_KEY"]
MODEL_KEY = ENV["ANTHROPIC_API_KEY"]
ROOT = Path(os.environ.get("FACTORIAL_SMOKE_ROOT") or tempfile.mkdtemp(prefix="e2b-smoke-"))


def adapter() -> ClaudeE2bAdapter:
    return ClaudeE2bAdapter(
        api_key=E2B_KEY,
        template=ENV["E2B_CLAUDE_TEMPLATE"],
        state_root=ROOT,
        deployment="smoke",
        poll_interval=1,
    )


def spec(instructions: str, *, repo: str = "", base: str = "main", token: str = "") -> RunSpec:
    base_spec = smoke_spec("claude", MODEL_KEY)
    base_spec = base_spec.model_copy(
        update={"prompts": base_spec.prompts.model_copy(update={"instructions": instructions})}
    )
    if not token:
        return base_spec
    return base_spec.model_copy(
        update={
            "workspace": Workspace(
                repo=repo, base=base, branch="smoke/{slug}", credential="minted"
            ),
            "secrets": {**base_spec.secrets, "GH_TOKEN": SecretStr(token)},
        }
    )


def check(label: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)
    return ok


async def push_and_pr(repo: str, token: str) -> bool:
    a = adapter()
    handle = await a.spawn(spec(
        "Create a branch named smoke/e2b-<four random lowercase letters>, add a file "
        "SMOKE.md containing one line, commit it, push the branch with git, and open a pull "
        "request against the default branch with `gh pr create --fill`. Reply with the URL.",
        repo=repo, base="main", token=token,
    ))
    try:
        idle = await finished(a, handle.run_id)
        result = await a.result(handle.run_id)
        return all([
            check("the session reached its prompt", idle is not None),
            check("push and PR succeeded", result.outcome is RunStatus.SUCCEEDED),
            check(
                "the PR was found host-side", any(x.kind.value == "pr" for x in result.artifacts)
            ),
            check(
                "the GitHub token never reached the mirrors",
                not secret_in_mirrors(handle.run_id, token),
            ),
            check(
                "the model key never reached the mirrors",
                not secret_in_mirrors(handle.run_id, MODEL_KEY),
            ),
        ])
    finally:
        await E2bSandboxClient(E2B_KEY).kill(sandbox_of(handle.run_id))


PAGE_SERVER = (
    "node -e \"require('http').createServer((q, s) => "
    "s.end(require('fs').readFileSync('index.html'))).listen(process.env.PORT)\""
)
LOOPBACK_SERVER = (
    "node -e \"require('http').createServer((q, s) => "
    "s.end('loopback only')).listen(process.env.PORT, '127.0.0.1')\""
)


async def preview() -> bool:
    a = adapter()
    handle = await a.spawn(
        spec(
            "Create a file index.html in the current directory containing exactly "
            "<h1>smoke preview</h1>. Do not commit it, push or open a pull request: this run "
            "holds no GitHub token. Then reply done."
        ).model_copy(update={"public": True})
    )
    await finished(a, handle.run_id)
    kept = await a.status(handle.run_id)
    state = await a.preview_start(
        handle.run_id,
        PreviewSpec(
            services=(
                PreviewServiceSpec(name="web", command=PAGE_SERVER, port=3000),
                PreviewServiceSpec(name="loopback", command=LOOPBACK_SERVER, port=3001),
            ),
            stop_after=timedelta(minutes=5),
        ),
        {},
        token="smoke",
    )
    for _ in range(60):
        state = await a.preview_status(handle.run_id)
        if state.phase is not PreviewPhase.STARTING:
            break
        await asyncio.sleep(2)
    body = loopback = ""
    if state.urls:
        async with httpx.AsyncClient(timeout=10) as http:
            body = (await http.get(state.urls[0].url)).text
            try:
                loopback = (await http.get(state.urls[1].url)).text
            except httpx.HTTPError as exc:
                loopback = f"unreachable ({type(exc).__name__})"
    reached = "yes" if loopback == "loopback only" else f"no: {loopback[:120]!r}"
    print(f"INFO  the public URL reaches a 127.0.0.1-only service: {reached}", flush=True)
    await a.preview_stop(handle.run_id)
    stopped = await a.preview_status(handle.run_id)
    # A stopped preview is parked, and kept; this mode leaves nothing behind.
    await E2bSandboxClient(E2B_KEY).kill(sandbox_of(handle.run_id))
    return all([
        check("the kept run succeeded", kept is RunStatus.SUCCEEDED),
        check("the preview became ready", state.phase is PreviewPhase.READY),
        check("the public URL served the page", "smoke preview" in body),
        check("Stop parked it", stopped.phase is PreviewPhase.STOPPED),
    ])


async def mint(repo: str) -> str:
    from sqlalchemy import select

    from orchestrator.db.models import Project
    from orchestrator.db.session import session_scope
    from orchestrator.github.pool import pool

    permissions = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    async with session_scope() as session:
        projects = await session.execute(select(Project).where(Project.github_repo == repo))
        project = projects.scalars().first()
        client = None if project is None else await pool().for_project(session, project)
    if client is None:
        raise RuntimeError(f"no project on {repo} has a GitHub connection")
    token = await client.mint_run_token(repo, permissions)
    return token.token.get_secret_value()


#: A branch the public repo has, so a rework can fetch its head with nothing pushed.
REWORK_BRANCH = "test"
REPO_URL = "https://github.com/octocat/Hello-World.git"


def redacted(exc: BaseException) -> str:
    return str(exc).replace(MODEL_KEY, "<key>").replace(E2B_KEY, "<key>")


def interactive_spec(instructions: str, **update: object) -> RunSpec:
    base = smoke_spec("claude", MODEL_KEY)
    return base.model_copy(
        update={
            "workspace": base.workspace.model_copy(update={"branch": REWORK_BRANCH}),
            "prompts": base.prompts.model_copy(update={"instructions": instructions}),
            **update,
        }
    )


async def at_prompt(a: ClaudeE2bAdapter, run_id: str) -> SessionState | None:
    for _ in range(300):
        status = await a.status(run_id)
        if status is RunStatus.IDLE:
            return await a.session_state(run_id)
        if status.is_terminal:
            return None
        await asyncio.sleep(1)
    return None


async def finished(a: ClaudeE2bAdapter, run_id: str) -> SessionState | None:
    """The session at its prompt, which it is then finished from; None when it never got there,
    and it is cancelled instead."""
    idle = await at_prompt(a, run_id)
    if idle is not None:
        await a.finish(run_id)
    else:
        await a.cancel(run_id)
    await a.drain()
    return idle


def sandbox_of(run_id: str) -> str:
    return json.loads((ROOT / "runs" / run_id / "state.json").read_text())["sandbox_id"]


def secret_in_mirrors(run_id: str, secret: str) -> bool:
    mirrors = ("stdout.jsonl", "session.jsonl", "signals.jsonl")
    run = ROOT / "runs" / run_id
    return any(secret in (run / name).read_text() for name in mirrors if (run / name).exists())


async def interactive() -> bool:
    first = interactive_spec(
        f"Run `git fetch --quiet origin {REWORK_BRANCH} && git switch --quiet -c "
        f"{REWORK_BRANCH} FETCH_HEAD && echo draft > DRAFT.md` with your shell tool, then reply "
        "with the word pong."
    )
    result, problems = await session_smoke_run(adapter(), first, reattach=adapter)
    sandbox_id = sandbox_of(result.run_id)
    for problem in problems:
        print(f"      {problem}")
    checks = [check("the session smoke passed", not problems)]
    try:
        if result.session_id:
            checks.append(await rework(result.run_id, result.session_id))
        else:
            checks.append(check("the rework adopted the session", False))
    finally:
        await E2bSandboxClient(E2B_KEY).kill(sandbox_id)
    return all(checks)


async def rework(first: str, session_id: str) -> bool:
    a = adapter()
    spec = interactive_spec(
        "Reply with the word reworked.",
        resume_from=ResumeFrom(run_id=first, session_id=session_id),
    )
    try:
        handle = await a.spawn(spec)
    except Exception as exc:
        print(f"      {type(exc).__name__}: {redacted(exc)}")
        return check("the rework adopted the session", False)
    second = handle.run_id
    adopted = await a.session_state(second)
    segments = json.loads((ROOT / "runs" / second / "state.json").read_text())["segments"]
    attached = a._attached(second)
    turn = head = ""
    if attached is not None:
        read = await attached[1].run(f"cat {turn_file(segments - 1)}", user=AGENT_USER)
        turn = read.stdout
        remote = await attached[1].run(
            f"git ls-remote {REPO_URL} refs/heads/{REWORK_BRANCH}", user=AGENT_USER
        )
        head = remote.stdout.split("\t", 1)[0].strip()
    idle = await finished(a, second)
    done = await a.result(second)
    return all(
        [
            check(
                "the rework continued the same session from where its log ended",
                adopted.session_id == session_id and adopted.log_offset > 0,
            ),
            check(
                "its turn says the branch was reset to its remote head and the draft stashed",
                bool(head)
                and f"The branch `{REWORK_BRANCH}` was reset" in turn
                and f"`{head}`" in turn
                and REWORK_STASHED.split("`")[0] in turn,
            ),
            check(
                "the rework reached its prompt",
                idle is not None and idle.idle_reason is IdleReason.TURN_END,
            ),
            check(
                "the rework finished as a success with nothing missing",
                done.outcome is RunStatus.SUCCEEDED
                and not done.stream_warnings
                and done.session_id == session_id
                and done.cost_usd is not None,
            ),
            check(
                "the model key never reached either run's mirrors",
                not secret_in_mirrors(first, MODEL_KEY)
                and not secret_in_mirrors(second, MODEL_KEY),
            ),
        ]
    )


async def none_left() -> bool:
    left = await E2bSandboxClient(E2B_KEY).list({"factorial_deployment": "smoke"})
    for sandbox in left:
        await E2bSandboxClient(E2B_KEY).kill(sandbox.sandbox_id)
    return check("no smoke sandbox left behind", not left)


async def push_and_pr_if_set() -> bool:
    repo = os.environ.get("FACTORIAL_SMOKE_REPO")
    if not repo:
        print("SKIP  push and PR (set FACTORIAL_SMOKE_REPO to a scratch repo)")
        return True
    return await push_and_pr(repo, await mint(repo))


MODES = {"3": push_and_pr_if_set, "4": preview, "5": interactive}


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
