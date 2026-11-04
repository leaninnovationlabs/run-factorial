"""A worker stand-in that a test can kill: it spawns one interactive `claude_cli` run in a process
of its own, prints the run's id, then asks its status every second and prints each answer, until
it is killed.

    python host_session_driver.py serve <root> [--claude PATH] [--origin BARE] [--record FILE]

`--origin` points the run's GitHub remote at a local bare repo; without it, git reaches GitHub.
`--record` keeps the run's id: a driver started again, as a supervisor restarts a worker, takes
that run back instead of spawning one.
The model key is `DRIVER_API_KEY`'s, else a made-up one, and the token is made up: the binary
is expected to be the fake TUI, or the real CLI against a mock API.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from host_session_support import via_origin
from pydantic import SecretStr

from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.process import CommandResult
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.cli import CLAUDE_BIN, ENV_API_KEY, _Cli
from runtime.contract import AgentPrompts, RunSpec, Workspace

REPO = "acme/widgets"
API_KEY = os.environ.get("DRIVER_API_KEY", "sk-ant-api03-the-drivers-fake-key-0123456789")
TOKEN = "ghs_thedriversfaketoken"


class _OriginCli(_Cli):
    def __init__(self, claude: str, origin: str | None) -> None:
        super().__init__(claude_bin=claude)
        self._origin = origin

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if program == "gh":
            return CommandResult((program, *args), 0, "[]", "")
        if self._origin is not None and program == self.git_bin:
            env = via_origin(env, Path(self._origin))
        return await super().run(
            program,
            *args,
            env=env,
            cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


def _spec() -> RunSpec:
    return RunSpec(
        agent="spec-agent",
        ticket="GH-12",
        workspace=Workspace(repo=REPO, base="main", branch="spec/12-{slug}"),
        prompts=AgentPrompts(system="You plan.", instructions="Write the spec."),
        model="claude-haiku-4-5-20251001",
        max_turns=10,
        secrets={ENV_API_KEY: SecretStr(API_KEY), ENV_GH_TOKEN: SecretStr(TOKEN)},
    )


def _recorded(record: Path | None) -> str | None:
    if record is None or not record.exists():
        return None
    return record.read_text().strip()


async def serve(root: str, claude: str, origin: str | None, record: Path | None) -> None:
    adapter = ClaudeCliAdapter(local_root=root, cli=_OriginCli(claude, origin), poll=0.05)
    run_id = await asyncio.to_thread(_recorded, record)
    if run_id is None:
        run_id = (await adapter.spawn(_spec())).run_id
        if record is not None:
            await asyncio.to_thread(record.write_text, f"{run_id}\n")
    print(run_id, flush=True)
    while True:
        print(await adapter.status(run_id), flush=True)
        await asyncio.sleep(1)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    serving = commands.add_parser("serve")
    serving.add_argument("root")
    serving.add_argument("--claude", default=CLAUDE_BIN)
    serving.add_argument("--origin")
    serving.add_argument("--record", type=Path)
    args = parser.parse_args(argv)
    asyncio.run(serve(args.root, args.claude, args.origin, args.record))


if __name__ == "__main__":
    main(sys.argv[1:])
