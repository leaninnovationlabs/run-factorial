"""A worker stand-in for `codex_cli`, as `host_session_driver.py` is for claude: it spawns one run
in a process of its own, prints the run's id, then asks its status every second and prints each
answer, until it is killed.

    python codex_session_driver.py serve <root> --codex PATH --origin BARE [--record FILE]
        [--finish FILE]

`--codex` is the binary the adapter runs, here a wrapper that points the pinned one at a mock API
(`codex_tui_support.wrapper`). `--origin` sends the repo's GitHub URL to a local bare repo, for
the worker's git. `--record` keeps the run's id: a driver started again, as a supervisor restarts
a worker, takes that run back instead of spawning one. Once the file `--finish` names exists and
the session is idle, the driver finishes it, as the orchestrator does when a step is done.

A refused spawn prints `refused <reason>` and the refusal's message, a line each, and the driver
then waits to be killed, as a worker goes on after it escalates a ticket. The model key is
`DRIVER_API_KEY`'s, else a made-up one, and the token is made up.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from host_session_support import REPO, CodexOriginCli
from pydantic import SecretStr

from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.cli import ENV_API_KEY
from runtime.contract import AgentPrompts, RunSpec, RunStatus, SpawnError, Workspace

API_KEY = os.environ.get("DRIVER_API_KEY", "sk-proj-the-drivers-fake-key-0123456789")
TOKEN = "ghs_thedriversfaketoken"


def _spec() -> RunSpec:
    return RunSpec(
        agent="spec-agent",
        ticket="GH-12",
        workspace=Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        prompts=AgentPrompts(system="You plan.", instructions="Write the spec."),
        model="gpt-5.3-codex",
        max_turns=10,
        secrets={ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    )


def _recorded(record: Path | None) -> str | None:
    if record is None or not record.exists():
        return None
    return record.read_text().strip()


async def serve(
    root: str,
    codex: Path,
    origin: Path,
    record: Path | None,
    finish: Path | None,
) -> None:
    adapter = CodexCliAdapter(local_root=root, cli=CodexOriginCli(codex, origin), poll=0.05)
    run_id = await asyncio.to_thread(_recorded, record)
    if run_id is None:
        try:
            run_id = (await adapter.spawn(_spec())).run_id
        except SpawnError as exc:
            print(f"refused {exc.reason}", exc, sep="\n", flush=True)
            await asyncio.Event().wait()
            return
        if record is not None:
            await asyncio.to_thread(record.write_text, f"{run_id}\n")
    print(run_id, flush=True)
    while True:
        status = await adapter.status(run_id)
        print(status, flush=True)
        if (
            status is RunStatus.IDLE
            and finish is not None
            and await asyncio.to_thread(finish.exists)
        ):
            await adapter.finish(run_id)
        await asyncio.sleep(1)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    serving = commands.add_parser("serve")
    serving.add_argument("root")
    serving.add_argument("--codex", type=Path, required=True)
    serving.add_argument("--origin", type=Path, required=True)
    serving.add_argument("--record", type=Path)
    serving.add_argument("--finish", type=Path)
    args = parser.parse_args(argv)
    asyncio.run(serve(args.root, args.codex, args.origin, args.record, args.finish))


if __name__ == "__main__":
    main(sys.argv[1:])
