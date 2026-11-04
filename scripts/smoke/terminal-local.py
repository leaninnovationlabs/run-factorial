"""Local terminal fixture: one session for the dashboard's Terminal tab to attach to, with
no E2B, no model key and no GitHub (decision 57). It plays the worker for that run; the API, the
terminal gateway and the dashboard are production code.

    FACTORIAL_TUI_CLAUDE=<a claude PINNED_VERSION binary> \\
    FACTORIAL_DATABASE_URL=postgresql+asyncpg://factory:factory@localhost:<port>/factory \\
    FACTORIAL_API_TOKEN=dev-token \\
      uv run python scripts/smoke/terminal-local.py live [--dashboard http://localhost:5174/factorial]

Modes:
  live  Seeds a project, a ticket and a running `plan` run whose `agent_spawned`
        names a host tmux session, then starts the real claude TUI in that session against
        `runtime/tests/mock_anthropic.py`. The prompt asks the mock for an AskUserQuestion, so the
        TUI waits on a question menu. Runs until Ctrl-C, which kills the tmux server and the mock.
  park  Seeds the same run parked by the idle rule: run interrupted, ticket blocked,
        `agent_idle` with the question and `agent_parked{reason: idle}`. No tmux, no mock. The
        gateway closes it 4409. Exits at once.

Both print the ticket URL with `?terminal=<run id>`, and a line to paste in the dashboard's
console: it sets an `sf_session` cookie that `FACTORIAL_API_TOKEN` signs for `--email`. A
host-only cookie on `localhost` reaches every port, so the API and the gateway both read it.
Without the cookie, a dashboard signed in with the token alone connects by a terminal ticket, and
its typing is recorded as `api-token`.

What it needs:
  - FACTORIAL_DATABASE_URL, exported in the shell and naming a host on loopback: a scratch
    database, migrated (`make migrate`). It writes rows there and never deletes them. A URL that
    only `.env` names, or one on another host, is refused before anything is read.
  - FACTORIAL_API_TOKEN: the token the API and the gateway run with.
  - FACTORIAL_TUI_CLAUDE (live only): a claude binary at the adapter's PINNED_VERSION, and tmux.
  - The gateway (`make terminal`) with FACTORIAL_TERMINAL__ENABLED=true and the dashboard's
    origin in FACTORIAL_CORS_ORIGINS, the same database and token. Do not run the worker: it
    would reconcile this run.

The session's files live under one `mkdtemp(dir="/tmp")`, which keeps the tmux socket path under
sun_path's limit: the CLI's config dir, HOME, the worktree and the socket. The tmux server, the
mock and git start with only PATH and that HOME, and the CLI under `env -i` with a placeholder
key and `ANTHROPIC_BASE_URL` at the mock: none of them inherits this shell's environment, so
none reads `~/.claude`, `~/.codex` or a real key. The tmux server loads `TMUX_LOCKDOWN`, as
every server the orchestrator starts does.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.config.loader import load_process
from orchestrator.db.models import (
    ProcessVersion,
    Project,
    RuntimeKind,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.settings import get_settings
from runtime.adapters._cli.launch import SIGNAL_SH
from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY
from runtime.adapters.claude_cli.cli import PINNED_VERSION
from runtime.adapters.claude_cli.tui import seed_files, spawn_argv, tui_env, tui_flags
from runtime.adapters.e2b.claude import ENV_NONESSENTIAL, PLACEHOLDER_API_KEY
from runtime.contract import AgentPrompts, ParkReason, RunSpec, TerminalTarget, Workspace
from runtime.terminal import TMUX_LOCKDOWN

REPO = Path(__file__).resolve().parents[2]
MOCK = REPO / "runtime" / "tests" / "mock_anthropic.py"
PROCESS = REPO / "docs" / "samples" / "factorial-simple" / "processes" / "process-simple.yaml"
STEP = "plan"
SESSION = "run"
MODEL = "claude-haiku-4-5-20251001"
PROMPT = "ASKME: use the AskUserQuestion tool to ask which rows to count."
QUESTION = "Include archived rows?"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout.split() or [""])[0]


async def _seed(
    maker: async_sessionmaker[AsyncSession], *, target: TerminalTarget | None, parked: bool
) -> tuple[int, int]:
    """The rows the worker would have written for this run: (ticket id, step run id)."""
    tag = uuid.uuid4().hex[:8]
    config = load_process(PROCESS)
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        project = Project(
            name=f"terminal-local-{tag}",
            github_repo=f"local/terminal-{tag}",
            organization_id=await default_organization_id(session),
        )
        session.add(project)
        await session.flush()
        source = TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref=project.github_repo
        )
        version = ProcessVersion(
            project_id=project.id,
            process_key=config.name,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha="0" * 40,
        )
        session.add_all([source, version])
        await session.flush()
        ticket = Ticket(
            project_id=project.id,
            ticket_source_id=source.id,
            external_key="1",
            process_version_id=version.id,
            owner_at_admission="local",
            title="Terminal local check",
            current_step=STEP,
            status=TicketStatus.BLOCKED if parked else TicketStatus.RUNNING,
        )
        session.add(ticket)
        await session.flush()
        run = StepRun(
            ticket_id=ticket.id,
            step_name=STEP,
            kind=StepKind.AGENT,
            attempt=1,
            status=StepRunStatus.INTERRUPTED if parked else StepRunStatus.RUNNING,
            runtime_run_id=f"local-{tag}",
            runtime_kind=RuntimeKind.CLAUDE,
            model=MODEL,
            started_at=now,
        )
        session.add(run)
        await session.flush()

        async def record(type: EventType, **details: object) -> None:
            await append(
                session,
                type=type,
                project_id=project.id,
                ticket_id=ticket.id,
                step_run_id=run.id,
                step=STEP,
                attempt=1,
                **details,
            )

        await record(
            EventType.AGENT_SPAWNED,
            agent="plan-agent",
            run_id=run.runtime_run_id,
            model=MODEL,
            minted_at=now.isoformat(),
            runtime="claude_cli",
            **({"terminal": target.model_dump()} if target is not None else {}),
        )
        if parked:
            await record(
                EventType.AGENT_IDLE,
                reason="question",
                idle_since=now.isoformat(),
                question=QUESTION,
            )
            await record(EventType.AGENT_PARKED, run_id=run.runtime_run_id, reason=ParkReason.IDLE)
        return ticket.id, run.id


class Session:
    """The scratch files, the mock and the tmux server of one live run."""

    def __init__(self, claude: str, tmux: str) -> None:
        self.claude = claude
        self.tmux = tmux
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="f243-term-", dir="/tmp")))
        self.socket = str(self.root / "s")
        self.session_id = str(uuid.uuid4())
        self.mock: subprocess.Popen[str] | None = None

    def path(self, name: str) -> str:
        return str(self.root / name)

    @property
    def env(self) -> dict[str, str]:
        """All a child of this script gets: nothing from the shell that started it."""
        return {"PATH": PATH, "HOME": self.path("home")}

    def start(self) -> None:
        for name in ("cfg", "home", "wt", "bin"):
            os.mkdir(self.path(name))
        self.mock = subprocess.Popen(
            [sys.executable, str(MOCK), self.path("requests.jsonl")],
            stdout=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        assert self.mock.stdout is not None
        port = int(self.mock.stdout.readline())

        git_env = {**self.env, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", self.path("wt")], check=True, env=git_env)
        Path(self.path("bin/signal.sh")).write_text(SIGNAL_SH, "utf-8")
        os.chmod(self.path("bin/signal.sh"), 0o755)
        Path(self.path("signals.jsonl")).touch(mode=0o600)
        Path(self.path("system-prompt.md")).write_text("You are a test agent.", "utf-8")
        Path(self.path("tmux.conf")).write_text(TMUX_LOCKDOWN, "utf-8")
        files = seed_files(
            config_dir=self.path("cfg"),
            workspace_real=self.path("wt"),
            signal_script=self.path("bin/signal.sh"),
            auth_mode=AUTH_MODE_API_KEY,
            credential=PLACEHOLDER_API_KEY,
        )
        for name, content in files.items():
            Path(name).write_bytes(content)

        spec = RunSpec(
            agent="plan-agent",
            ticket="GH-1",
            workspace=Workspace(repo="local/terminal", base="main", branch="spec/1-{slug}"),
            prompts=AgentPrompts(system="unused: the system prompt is a file", instructions="-"),
            model=MODEL,
        )
        flags = tui_flags(
            spec,
            system_prompt_path=self.path("system-prompt.md"),
            mcp_path=self.path("mcp.json"),
        )
        env = {
            **self.env,
            "USER": "agent",
            "LANG": "C.UTF-8",
            ENV_NONESSENTIAL: "1",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            **tui_env(
                config_dir=self.path("cfg"),
                signals_path=self.path("signals.jsonl"),
                auth_mode=AUTH_MODE_API_KEY,
                credential=PLACEHOLDER_API_KEY,
            ),
        }
        argv = spawn_argv(flags, self.session_id, PROMPT, binary=self.claude)
        subprocess.run(
            [
                self.tmux, "-u", "-S", self.socket, "-f", self.path("tmux.conf"),
                "new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", self.path("wt"),
                "--", "/bin/sh", "-c", 'exec env -i "$@"', "sh",
                *(f"{k}={v}" for k, v in env.items()), *argv,
            ],
            check=True,
            env=self.env,
        )  # fmt: skip

    @property
    def target(self) -> TerminalTarget:
        return TerminalTarget(substrate="host", socket=self.socket, session=SESSION)

    def _cli_running(self) -> bool:
        return subprocess.run(["pgrep", "-f", self.session_id], capture_output=True).returncode == 0

    def stop(self) -> None:
        subprocess.run(
            [self.tmux, "-S", self.socket, "kill-server"], capture_output=True, env=self.env
        )
        # The CLI's exit runs its SessionEnd hook, which writes into the tree: removing it any
        # sooner leaves the tree behind.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self._cli_running():
            time.sleep(0.25)
        subprocess.run(["pkill", "-KILL", "-f", self.session_id], capture_output=True)
        if self.mock is not None:
            self.mock.kill()
            self.mock.wait()
        shutil.rmtree(self.root, ignore_errors=True)


def _print_access(dashboard: str, email: str, api_token: str, ticket_id: int, run_id: int) -> None:
    cookie = mint_session(email, api_token=api_token, now=datetime.now(UTC), max_age=12 * 3600)
    print(f"step run {run_id}, ticket {ticket_id}")
    print(f"open:    {dashboard.rstrip('/')}/tickets/{ticket_id}?terminal={run_id}")
    print(f'console: document.cookie="{SESSION_COOKIE}={cookie}; path=/"', flush=True)


async def _main(args: argparse.Namespace) -> None:
    # Before the settings load: they would fall back to `.env`, which may name a real database.
    url = os.environ.get("FACTORIAL_DATABASE_URL", "")
    if not url or make_url(url).host not in LOOPBACK:
        sys.exit("export FACTORIAL_DATABASE_URL, naming a scratch database on loopback")
    settings = get_settings()
    api_token = settings.api_token.get_secret_value()
    if not api_token:
        sys.exit("FACTORIAL_API_TOKEN is empty: the session cookie is signed with it")
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.mode == "park":
            ticket_id, run_id = await _seed(maker, target=None, parked=True)
            _print_access(args.dashboard, args.email, api_token, ticket_id, run_id)
            return

        claude = os.environ.get("FACTORIAL_TUI_CLAUDE", "")
        tmux = shutil.which("tmux")
        if tmux is None or not claude or _version(claude) != PINNED_VERSION:
            sys.exit(f"live needs tmux and FACTORIAL_TUI_CLAUDE at claude {PINNED_VERSION}")
        # Before anything starts, so a Ctrl-C at any point still reaches the cleanup below.
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        session = Session(claude, tmux)
        try:
            session.start()
            ticket_id, run_id = await _seed(maker, target=session.target, parked=False)
            _print_access(args.dashboard, args.email, api_token, ticket_id, run_id)
            print(f"tmux:    tmux -S {session.socket} attach -r", flush=True)
            await stopped.wait()
        finally:
            session.stop()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local terminal fixture (decision 57).")
    parser.add_argument("mode", choices=("live", "park"))
    parser.add_argument("--dashboard", default="http://localhost:5174/factorial")
    parser.add_argument("--email", default="local@example.com")
    asyncio.run(_main(parser.parse_args()))
