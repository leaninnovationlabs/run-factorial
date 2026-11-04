"""Assistant smoke: one REAL chat turn, sent to a running local stack.

Run from the repo root: `uv run python scripts/smoke/assistant-smoke.py ["message"]`. With no
message it asks for one project's process lanes, which needs a GitHub read.

What it proves: the API queues a message; the assistant's process (`make assistant`, or the
local image's `assistant` service) claims the turn and runs it through the claude_sdk backend;
the model calls its tools in the assistant's process, and a GitHub read rides the read-only
token the API minted; and the reply, the tool calls, the turn's end and its cost come back over
the conversation's SSE stream, read with a bearer header as the Drawer reads it. PASS needs a
`factorial_config_read` tool item that ended `ok`: a turn with no token still gets a reply, and
that reply proves nothing about the token path.

Prereqs: the API (FACTORIAL_SMOKE_API, default http://localhost:8000/factorial) and the assistant's
process on one database; FACTORIAL_API_TOKEN in the environment or the repo-root `.env` (read
without echoing); and the assistant set up in the Settings of the organization the conversation
opens in (FACTORIAL_SMOKE_ORG, default `default`), enabled, with a credential. It bills that
credential for one short turn. It prints item kinds, texts and the turn's status and cost, never
a credential.

It makes an archived conversation titled "Assistant smoke", so the Drawer's list stays clean,
and leaves it for retention to delete. Opt-in and never run in CI, like the other smokes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

API = os.environ.get("FACTORIAL_SMOKE_API", "http://localhost:8000/factorial").rstrip("/")
#: The organization whose assistant settings the turn runs on (tech-stack decision 68).
ORG = os.environ.get("FACTORIAL_SMOKE_ORG", "default")
QUESTION = "Use factorial_config_read on one project and name its lanes in one line."
#: The turn's own wall clock is five minutes by default; this waits a little longer.
DEADLINE_SECONDS = 360
ENDED = {"done", "failed", "cancelled", "interrupted"}


def api_token() -> str:
    token = os.environ.get("FACTORIAL_API_TOKEN", "")
    env = Path(__file__).resolve().parents[2] / ".env"
    if not token and env.is_file():
        for line in env.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key == "FACTORIAL_API_TOKEN":
                token = value.strip().strip("'\"")
    if not token:
        sys.exit("FACTORIAL_API_TOKEN is not set in the environment or .env")
    return token


def events(client: httpx.Client, conversation_id: int) -> Iterator[dict[str, str]]:
    """Each SSE event as `{"event", "data"}`, and each heartbeat as `{"comment"}`, so the caller
    checks its deadline at least every 15 s even when no turn runs. A stream silent for a
    minute, with no heartbeat either, is a dead API and raises."""
    with client.stream(
        "GET",
        f"/api/assistant/conversations/{conversation_id}/stream",
        timeout=httpx.Timeout(30, read=60),
    ) as response:
        response.raise_for_status()
        block: dict[str, str] = {}
        for line in response.iter_lines():
            if line.startswith(":"):
                yield {"comment": line[1:].strip()}
            elif not line:
                if "event" in block:
                    yield block
                block = {}
            else:
                key, _, value = line.partition(": ")
                block[key] = value


def show(item: dict[str, Any]) -> str:
    payload = item["payload"]
    if item["kind"] == "tool":
        return (
            f"{payload.get('name')} -> {payload.get('status')} ok={payload.get('ok')}: "
            f"{payload.get('summary', '')}"
        )
    return str(payload.get("text", json.dumps(payload)))


def main() -> int:
    question = " ".join(sys.argv[1:]) or QUESTION
    headers = {"Authorization": f"Bearer {api_token()}"}
    with httpx.Client(base_url=API, headers=headers, timeout=30) as client:
        created = client.post(
            f"/api/orgs/{ORG}/assistant/conversations", json={"title": "Assistant smoke"}
        )
        created.raise_for_status()
        conversation_id = created.json()["id"]
        client.patch(f"/api/assistant/conversations/{conversation_id}", json={"archived": True})
        sent = client.post(
            f"/api/assistant/conversations/{conversation_id}/messages",
            json={"text": question, "page": "/overview"},
        )
        if sent.status_code != 202:
            print(f"send refused: {sent.status_code} {sent.text}")
            return 1
        turn_id = sent.json()["turn_id"]
        print(f"conversation {conversation_id}, turn {turn_id}: {question}")

        started = time.monotonic()
        latest: dict[int, dict[str, Any]] = {}
        turn: dict[str, Any] | None = None
        for event in events(client, conversation_id):
            if time.monotonic() - started > DEADLINE_SECONDS:
                print("FAIL: no end within the deadline; is the assistant's process running?")
                return 1
            if "comment" in event:
                continue
            data = json.loads(event["data"])
            if event["event"] == "item":
                latest[data["id"]] = data
            elif data is not None and data["id"] == turn_id:
                turn = data
                print(f"turn {turn_id}: {data['status']}")
                if data["status"] in ENDED:
                    break

    for item in sorted(latest.values(), key=lambda item: item["id"]):
        print(f"  [{item['kind']}] {show(item)}")
    assert turn is not None
    elapsed = time.monotonic() - started
    print(
        f"status={turn['status']} stop={turn['stop_reason']} failure={turn['failure_reason']} "
        f"cost={turn['cost_usd']} ({turn['cost_source']}) in {elapsed:.1f}s"
    )
    replied = any(item["kind"] == "assistant_text" for item in latest.values())
    read_ok = any(
        item["kind"] == "tool"
        and item["payload"].get("name") == "factorial_config_read"
        and item["payload"].get("ok") is True
        for item in latest.values()
    )
    if turn["status"] == "done" and replied and read_ok:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
