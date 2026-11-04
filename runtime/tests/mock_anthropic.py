"""A stand-in for the Anthropic Messages API, so the real claude TUI finishes a turn with no key.

    python3 mock_anthropic.py LOGFILE [PORT]

Prints the port it listens on (PORT, default 0: any free one), then serves until killed. Each
request is appended to LOGFILE as one JSON line: method, path, the two credential headers and the
parsed body. Standard library only, so a sandbox image's `python3` runs it too.

Every response has its own message id and a `request-id` header, as the real API's do: the
session log keeps both, and the translator merges a response's lines by message id and counts
turns by request id, so one constant id would make every turn one.

The last user message steers the reply. With `ASKME` in it (and tools offered) the reply is an
`AskUserQuestion` call; with `BASHME <command> EMHSAB` it is a `Bash` call running that command;
with `TOOLME <name> <json input> EMLOOT` it is a call to the tool `<name>` with that input; with
`SLOWME` it never finishes, so a test can stop the CLI mid-turn. Anything else gets a short
text reply.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ASK: Any = {
    "questions": [
        {
            "question": "Include archived rows?",
            "header": "Scope",
            "options": [
                {"label": "Active only", "description": "Leave archived rows out."},
                {"label": "Active and archived", "description": "Count every row."},
            ],
            "multiSelect": False,
        }
    ]
}
BASH = re.compile(r"BASHME (.+?) EMHSAB")
TOOL = re.compile(r"TOOLME (\S+) (.+?) EMLOOT", re.DOTALL)
USAGE: Any = {
    "input_tokens": 1000,
    "output_tokens": 1,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}

_log_lock = threading.Lock()


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _last_user(body: Any) -> tuple[str, bool]:
    """The last user message's text, and whether it carries a tool's result. Under the Agent SDK
    the CLI sends its environment block as a `system` message after it."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        messages = []
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    content = users[-1].get("content") if users else None
    if isinstance(content, str):
        return content, False
    blocks = [b for b in content or [] if isinstance(b, dict)]
    text = " ".join(str(b.get("text", "")) for b in blocks)
    return text, any(b.get("type") == "tool_result" for b in blocks)


class Handler(BaseHTTPRequestHandler):
    log_path = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _record(self, raw: bytes) -> Any:
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"unparsed": raw[:2000].decode("utf-8", "replace")}
        line = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "x-api-key": self.headers.get("x-api-key"),
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )
        with _log_lock, open(self.log_path, "a", encoding="utf-8") as log:
            log.write(line + "\n")
        return body

    def _json(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", f"req_{uuid.uuid4().hex}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record(b"")
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "mock"}})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        body = self._record(self.rfile.read(int(self.headers.get("content-length") or 0)))
        if "count_tokens" in self.path:
            return self._json(200, {"input_tokens": 10})
        if not self.path.startswith("/v1/messages"):
            return self._json(
                404, {"type": "error", "error": {"type": "not_found_error", "message": "mock"}}
            )
        body = body if isinstance(body, dict) else {}
        message: dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-haiku-4-5-20251001"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": USAGE,
        }
        text, answered = _last_user(body)
        if not body.get("stream"):
            message["content"] = [{"type": "text", "text": "PONG from mock."}]
            message["stop_reason"] = "end_turn"
            return self._json(200, message)
        if "ASKME" in text and not answered and body.get("tools"):
            block: Any = {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex}",
                "name": "AskUserQuestion",
                "input": {},
            }
            delta: Any = {"type": "input_json_delta", "partial_json": json.dumps(ASK)}
            stop = "tool_use"
        elif (bash := BASH.search(text)) and not answered and body.get("tools"):
            block = {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex}",
                "name": "Bash",
                "input": {},
            }
            command = {"command": bash[1], "description": "Run the command the test named"}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(command)}
            stop = "tool_use"
        elif (tool := TOOL.search(text)) and not answered and body.get("tools"):
            block = {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex}",
                "name": tool[1],
                "input": {},
            }
            delta = {"type": "input_json_delta", "partial_json": tool[2]}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "PONG from mock."}
            stop = "end_turn"
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("request-id", f"req_{uuid.uuid4().hex}")
        self.end_headers()
        events = [_sse("message_start", {"type": "message_start", "message": message})]
        if "SLOWME" in text:
            self.wfile.write(events[0])
            self.wfile.flush()
            time.sleep(600)
            return
        events += [
            _sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": block},
            ),
            _sse(
                "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": 20},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        for event in events:
            self.wfile.write(event)
            self.wfile.flush()


def main() -> None:
    Handler.log_path = sys.argv[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 0), Handler
    )
    server.daemon_threads = True
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
