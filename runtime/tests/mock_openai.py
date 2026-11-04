"""A stand-in for the OpenAI Responses API, so the real codex TUI finishes a turn with no key.

    python3 mock_openai.py LOGFILE [PORT]

Prints the port it listens on (PORT, default 0: any free one), then serves until killed. Each
request is appended to LOGFILE as one JSON line: method, path, the `upgrade` and
`authorization` headers and the parsed body. Standard library only, so a sandbox image's
`python3` runs it too.

codex's built-in provider opens a WebSocket on `GET /v1/responses` first. This mock answers
that with a 404, so codex retries, then falls back to `POST /v1/responses` with SSE; the log's
`upgrade` field shows each attempt. Every response has its own `resp_`/`msg_` ids and an
`x-request-id` header, as the real API's do.

The last user message steers the reply. With `RUNME <command>` (to the end of its line) and an
`exec_command`, `shell` or `local_shell` tool offered, the reply calls that tool; the call's
output comes back as the request's last input item, and gets a short text reply. With `ASKME`
and `request_user_input` offered, the reply calls that tool with one question, and its output
gets the short reply too. With `FAILME`
the request gets a 401, on that turn only. With `SLOWME` the response never finishes, so a
test can stop the TUI mid-turn. Anything else gets a short text reply.
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

RUN = re.compile(r"RUNME (.+)")
SHELL_TOOLS = ("exec_command", "shell", "local_shell")
ASK_TOOL = "request_user_input"
ASK_ARGS: Any = {
    "questions": [
        {
            "id": "db",
            "header": "DB",
            "question": "Which database?",
            "options": [
                {"label": "Postgres", "description": ""},
                {"label": "SQLite", "description": ""},
            ],
        }
    ]
}
REPLY = "PONG from mock."
USAGE: Any = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 7,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 1007,
}
UNAUTHORIZED: Any = {
    "error": {
        "message": "Incorrect API key provided: sk-fake.",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
}

_log_lock = threading.Lock()


def _sse(event: Any) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def _last_user(body: Any) -> tuple[str, bool]:
    """The last user message's text, and whether the request's last input item is a tool's
    output, which this mock answers with text."""
    items = body.get("input") if isinstance(body, dict) else None
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    answered = bool(items) and str(items[-1].get("type", "")).endswith("_call_output")
    for item in reversed(items):
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                return content, answered
            parts = [p for p in content or [] if isinstance(p, dict)]
            return " ".join(str(p.get("text", "")) for p in parts), answered
    return "", answered


def _tools(body: Any) -> set[Any]:
    tools = body.get("tools") if isinstance(body, dict) else None
    return {t.get("name") or t.get("type") for t in tools or [] if isinstance(t, dict)}


def _shell_tool(body: Any) -> str | None:
    names = _tools(body)
    return next((name for name in SHELL_TOOLS if name in names), None)


def _call(tool: str, command: str | None) -> Any:
    if tool == ASK_TOOL:
        arguments = ASK_ARGS
    elif tool == "exec_command":
        arguments = {"cmd": command, "yield_time_ms": 2000}
    else:
        arguments = {"command": ["/bin/sh", "-c", command]}
    return {
        "type": "function_call",
        "id": f"fc_{uuid.uuid4().hex}",
        "call_id": f"call_{uuid.uuid4().hex}",
        "name": tool,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _message(text: str) -> Any:
    return {
        "type": "message",
        "role": "assistant",
        "id": f"msg_{uuid.uuid4().hex}",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
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
                "at": time.time(),
                "method": self.command,
                "path": self.path,
                "upgrade": self.headers.get("upgrade"),
                "authorization": self.headers.get("authorization"),
                "content-encoding": self.headers.get("content-encoding"),
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
        self.send_header("x-request-id", f"req_{uuid.uuid4().hex}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record(b"")
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [], "models": []})
        self._json(404, {"error": {"message": "mock", "type": "not_found"}})

    def do_POST(self) -> None:
        body = self._record(self.rfile.read(int(self.headers.get("content-length") or 0)))
        if not self.path.split("?")[0].rstrip("/").endswith("/responses"):
            return self._json(404, {"error": {"message": "mock", "type": "not_found"}})
        text, answered = _last_user(body)
        if "FAILME" in text:
            return self._json(401, UNAUTHORIZED)
        response_id = f"resp_{uuid.uuid4().hex}"
        created = {"type": "response.created", "response": {"id": response_id}}
        if "SLOWME" in text and not answered:
            self._stream_head(None)
            self.wfile.write(_sse(created))
            self.wfile.flush()
            time.sleep(600)
            return
        run, tool = RUN.search(text), _shell_tool(body)
        if run and tool and not answered:
            item = _call(tool, run[1].strip())
        elif "ASKME" in text and ASK_TOOL in _tools(body) and not answered:
            item = _call(ASK_TOOL, None)
        else:
            item = _message(REPLY)
        events = [
            created,
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": response_id, "usage": USAGE}},
        ]
        data = b"".join(_sse(event) for event in events)
        self._stream_head(len(data))
        self.wfile.write(data)

    def _stream_head(self, length: int | None) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-request-id", f"req_{uuid.uuid4().hex}")
        if length is None:
            self.close_connection = True
        else:
            self.send_header("content-length", str(length))
        self.end_headers()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that drops a connection (a WebSocket attempt given up, a turn interrupted)
        # is no error of the mock's.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def main() -> None:
    Handler.log_path = sys.argv[1]
    server = Server(("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
