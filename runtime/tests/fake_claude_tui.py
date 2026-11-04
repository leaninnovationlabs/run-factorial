"""A stand-in `claude` TUI for the host session tests: it runs in a real tmux pane, reads its
terminal raw, and writes what the adapter reads off the real one (fixtures/claude-2.1.284).

As 2.1.284 does, it fires no hook while `.claude.json` does not trust `realpath(cwd)` or approve
the key's tail: it shows its dialog and waits. Otherwise it fires the hooks `settings.json`
names, writes the session log under `$CLAUDE_CONFIG_DIR/projects/run/`, exits 0 on `/exit` and
writes `cost-state` on the way out, a hangup's included.

`FAKE_TUI`, comma-separated, scripts the launch's first turn: `ask` asks a question, `ask_words`
ends it on a question in plain words, `fail_turn` ends it on an API error, `slow` never ends it,
`turns=N` makes it N API calls. A typed turn holding ASKME, FAILME or SLOWME does the same. It
scripts the TUI too: `hide_input` draws no input line, `deaf_exit` ignores `/exit`, `slow_exit`
takes a second to write its cost once hung up, and `job` leaves a job that ignores SIGHUP in its
process group, its pid written to `FAKE_TUI_JOB`.

`/model` opens a picker as the real one's dialogs are: what is typed filters it on an input line,
Enter applies the highlighted choice to `FAKE_TUI_APPLIED`, Esc closes it. `FAKE_TUI_DUMP` names
a file it writes its argv, cwd and environment to, `FAKE_TUI_KEYS` one it appends its input to.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.adapters.claude_cli.cli import INPUT_MARK

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
MODEL_FLAG = "--model"
QUESTION = "Include archived rows?"
WORDS_QUESTION = "Which schema should I migrate first?"
PICKED = "claude-opus-4-1"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Tui:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.resumed = "--resume" in argv
        flag = "--resume" if self.resumed else "--session-id"
        self.session_id = argv[argv.index(flag) + 1]
        self.prompt = argv[argv.index("--") + 1] if "--" in argv else None
        self.model = argv[argv.index(MODEL_FLAG) + 1] if MODEL_FLAG in argv else "claude-fake"
        self.config = Path(os.environ["CLAUDE_CONFIG_DIR"])
        project = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME", "run")
        self.log = self.config / "projects" / project / f"{self.session_id}.jsonl"
        self.modes: dict[str, str] = {}
        for part in os.environ.get("FAKE_TUI", "").split(","):
            mode, _, value = part.partition("=")
            self.modes[mode] = value
        self.cost = 0.0
        self.tokens = 0
        self.buffer = ""
        self.question: str | None = None
        self.turns_done = 0
        #: The open picker's filter, or None while no picker is open.
        self.picker: str | None = None

    # -- the files ------------------------------------------------------------------------

    def write_log(self, record: dict[str, Any]) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        record = {**record, "sessionId": self.session_id, "timestamp": now()}
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def hook(self, event: str, **fields: Any) -> None:
        settings = json.loads((self.config / "settings.json").read_text())
        for entry in settings.get("hooks", {}).get(event, []):
            matcher = entry.get("matcher")
            if matcher and matcher != fields.get("tool_name"):
                continue
            for each in entry.get("hooks", []):
                body = {
                    "session_id": self.session_id,
                    "transcript_path": str(self.log),
                    "cwd": os.getcwd(),
                    "hook_event_name": event,
                    **fields,
                }
                subprocess.run(each["command"], shell=True, input=json.dumps(body).encode())

    def write_cost(self) -> None:
        usage = {"inputTokens": self.tokens, "outputTokens": self.tokens}
        self.write_log(
            {
                "type": "cost-state",
                "totalCostUSD": self.cost,
                "modelUsage": {
                    self.model: {
                        **usage,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                    }
                },
            }
        )

    def restore_cost(self) -> None:
        """A resumed session's total carries on from its log, as the CLI's does."""
        if not self.log.exists():
            return
        for line in self.log.read_text().splitlines():
            record = json.loads(line)
            if record.get("type") == "cost-state":
                self.cost = record["totalCostUSD"]
                self.tokens = record["modelUsage"][self.model]["inputTokens"]

    def trusted(self) -> bool:
        try:
            seed = json.loads((self.config / ".claude.json").read_text())
        except (OSError, ValueError):
            return False
        project = seed.get("projects", {}).get(os.path.realpath(os.getcwd()), {})
        if project.get("hasTrustDialogAccepted") is not True:
            return False
        key = os.environ.get("ANTHROPIC_API_KEY")
        approved = seed.get("customApiKeyResponses", {}).get("approved", [])
        return not key or key.strip()[-20:] in approved

    # -- a turn ---------------------------------------------------------------------------

    def assistant(self, content: list[dict[str, Any]], *, error: bool = False) -> None:
        self.cost += 0.001
        self.tokens += 10
        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": "<synthetic>" if error else self.model,
            "content": content,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        record = {"type": "assistant", "message": message, "requestId": f"req_{uuid.uuid4().hex}"}
        if error:
            record["isApiErrorMessage"] = True
        self.write_log(record)

    def end_turn(self, text: str) -> None:
        # The hook first, here and on a failed turn: a supervisor that reads the log's turn end
        # before the hook's signal, as a slow hook on a loaded machine allows, takes the hook
        # for lost.
        self.hook("Stop", last_assistant_message=text)
        self.write_log({"type": "system", "subtype": "turn_duration", "durationMs": 5})
        self.turns_done += 1
        self.draw()

    def start_turn(self, text: str) -> None:
        self.hook("UserPromptSubmit", prompt=text)
        message = {"role": "user", "content": text}
        self.write_log({"type": "user", "message": message, "uuid": str(uuid.uuid4())})
        first = self.turns_done == 0
        if (first and "slow" in self.modes) or "SLOWME" in text:
            return
        if (first and "ask" in self.modes) or "ASKME" in text:
            self.ask()
            return
        if first and "ask_words" in self.modes:
            self.assistant([{"type": "text", "text": WORDS_QUESTION}])
            self.end_turn(WORDS_QUESTION)
            return
        if (first and "fail_turn" in self.modes) or "FAILME" in text:
            self.assistant([{"type": "text", "text": "API Error: 500 overloaded"}], error=True)
            self.hook("StopFailure", error="server_error", last_assistant_message="API Error")
            self.write_log({"type": "system", "subtype": "turn_duration", "durationMs": 5})
            self.turns_done += 1
            self.draw()
            return
        calls = int(self.modes.get("turns") or 1) if first else 1
        for n in range(calls):
            self.assistant([{"type": "text", "text": f"call {n + 1} for: {text[:40]}"}])
        self.end_turn(f"done: {text[:40]}")

    def ask(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": QUESTION,
                    "header": "Scope",
                    "options": [{"label": "Active only"}, {"label": "Active and archived"}],
                    "multiSelect": False,
                }
            ]
        }
        self.question = f"toolu_{uuid.uuid4().hex}"
        use = {"type": "tool_use", "id": self.question, "name": "AskUserQuestion"}
        self.assistant([{**use, "input": tool_input}])
        self.hook(
            "PreToolUse",
            tool_name="AskUserQuestion",
            tool_input=tool_input,
            tool_use_id=self.question,
        )
        self.draw()

    def answer(self, text: str) -> None:
        use_id, self.question = self.question, None
        self.hook("PostToolUse", tool_name="AskUserQuestion", tool_use_id=use_id)
        self.write_log(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": text, "tool_use_id": use_id}],
                },
                "toolUseResult": {"answers": {QUESTION: text}},
                "uuid": str(uuid.uuid4()),
            }
        )
        self.assistant([{"type": "text", "text": f"answered: {text}"}])
        self.end_turn(f"answered: {text}")

    # -- the terminal ---------------------------------------------------------------------

    def draw(self, banner: str = "") -> None:
        shown = f"{QUESTION}\r\n" if self.question else ""
        line = "" if "hide_input" in self.modes else f"{INPUT_MARK} {self.buffer}"
        if self.picker is not None:
            shown, line = f"Select model\r\n  1. {PICKED}\r\n", f"{INPUT_MARK} {self.picker}"
        sys.stdout.write(f"\x1b[2J\x1b[H{banner}{shown}{line}")
        sys.stdout.flush()

    def pick(self, char: bytes) -> None:
        if char in (b"\r", b"\n"):
            with open(os.environ["FAKE_TUI_APPLIED"], "a") as handle:
                handle.write(f"{PICKED}\n")
            self.picker = None
        elif char == b"\x1b":
            self.picker = None
        elif char == b"\x03":
            self.picker = ""
        else:
            self.picker = (self.picker or "") + char.decode(errors="replace")

    def submit(self) -> None:
        text, self.buffer = self.buffer, ""
        if text == "/model":
            self.picker = ""
        elif text == "/exit" and "deaf_exit" in self.modes:
            pass
        elif text == "/exit":
            self.write_cost()
            self.write_cost()
            self.hook("SessionEnd", reason="prompt_input_exit")
            raise SystemExit(0)
        elif not text:
            self.draw()
        elif self.question is not None:
            self.answer(text)
        else:
            self.start_turn(text)

    def keys(self, data: bytes, pasting: bool) -> bool:
        """Feed raw input; returns whether a bracketed paste is still open."""
        while data:
            if pasting:
                body, end, data = data.partition(PASTE_END)
                pasted = body.decode(errors="replace").replace("\r", "\n")
                if self.picker is not None:
                    self.picker += pasted
                else:
                    self.buffer += pasted
                pasting = not end
                continue
            if data.startswith(PASTE_START):
                data, pasting = data[len(PASTE_START) :], True
                continue
            char, data = data[:1], data[1:]
            if self.picker is not None:
                self.pick(char)
            elif char in (b"\r", b"\n"):
                self.submit()
            elif char == b"\x03":
                self.buffer = ""
            elif char == b"\x7f":
                self.buffer = self.buffer[:-1]
            elif char != b"\x1b":
                self.buffer += char.decode(errors="replace")
        self.draw()
        return pasting


def main() -> None:
    tui = Tui(sys.argv[1:])
    if dump := os.environ.get("FAKE_TUI_DUMP"):
        Path(dump).write_text(
            json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)})
        )

    def hung_up(signum: int, frame: object) -> None:
        trusted = tui.trusted()
        if "slow_exit" in tui.modes:
            time.sleep(1)
        if trusted:
            tui.write_cost()
        os._exit(128 + signum)

    signal.signal(signal.SIGHUP, hung_up)
    if "job" in tui.modes:
        job = subprocess.Popen(
            ["/bin/sh", "-c", "trap '' HUP; exec /bin/sleep 1000"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(os.environ["FAKE_TUI_JOB"]).write_text(str(job.pid))
    keys = os.environ.get("FAKE_TUI_KEYS")
    stdin = sys.stdin.fileno()
    tty.setraw(stdin, termios.TCSANOW)
    sys.stdout.write("\x1b[?2004h")
    if not tui.trusted():
        tui.draw("Do you trust this folder?\r\n")
        while True:
            os.read(stdin, 4096)
    if tui.resumed:
        tui.restore_cost()
    tui.hook("SessionStart", source="resume" if tui.resumed else "startup", model=tui.model)
    tui.draw()
    if tui.prompt is not None:
        tui.start_turn(tui.prompt)
    pasting = False
    while True:
        select.select([stdin], [], [])
        data = os.read(stdin, 65536)
        if not data:
            # A raw terminal reads empty only once it closed; Linux can return it before the
            # SIGHUP handler runs.
            hung_up(signal.SIGHUP, None)
        if keys:
            with open(keys, "ab") as handle:
                handle.write(data)
        pasting = tui.keys(data, pasting)


if __name__ == "__main__":
    main()
