"""A stand-in for `codex exec --json`, behaving around a thread as 0.164.0 was observed to.

A new thread writes its rollout under `$CODEX_HOME/sessions`. `exec resume <thread>` continues
one whose rollout that home holds, and exits 1 with codex's own words when it holds none. Each
call's argv and stdin are appended to the file `FAKE_CODEX_RECORD` names.

With `FAKE_CODEX_ASKS_THE_MODEL` set it also makes one model request as 0.164.0 does, and
records what came back: to the provider `config.toml` names, with the bearer that provider's
`env_key` names, offering an MCP server's tools as one `namespace` tool.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def _ask_the_model() -> dict[str, Any]:
    config = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    provider = config["model_providers"][config["model_provider"]]
    tool = {"type": "function", "name": "factorial_projects_list", "parameters": {}}
    body = {
        "model": "us.xai.grok-4.6",
        "stream": True,
        "input": [],
        "tools": [{"type": "namespace", "name": "mcp__factorial", "tools": [tool]}],
    }
    request = urllib.request.Request(
        f"{provider['base_url']}/responses",
        data=json.dumps(body).encode(),
        headers={
            "authorization": f"Bearer {os.environ[provider['env_key']]}",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as reply:
        lines = reply.read().decode().split("\n")
    events = [json.loads(line[5:]) for line in lines if line.startswith("data: {")]
    return {
        "base_url": provider["base_url"],
        "calls": [
            {"name": event["item"]["name"], "namespace": event["item"].get("namespace")}
            for event in events
            if event["type"] == "response.output_item.done"
            and event["item"]["type"] == "function_call"
        ],
    }


def main() -> int:
    argv = sys.argv[1:]
    call: dict[str, Any] = {"argv": argv, "prompt": sys.stdin.read()}
    if os.environ.get("FAKE_CODEX_ASKS_THE_MODEL"):
        call["model"] = _ask_the_model()
    with open(os.environ["FAKE_CODEX_RECORD"], "a") as record:
        record.write(json.dumps(call) + "\n")
    sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
    if argv[:2] == ["exec", "resume"]:
        thread = argv[2]
        if not list(sessions.rglob(f"rollout-*-{thread}.jsonl")):
            print(
                "Error: thread/resume: thread/resume failed: no rollout found for thread id "
                f"{thread} (code -32600)",
                file=sys.stderr,
            )
            return 1
    else:
        thread = str(uuid.uuid4())
        sessions.mkdir(parents=True)
        (sessions / f"rollout-2026-11-10T00-00-00-{thread}.jsonl").write_text("{}\n")
    for event in (
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "a reply"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
        },
    ):
        print(json.dumps(event), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
