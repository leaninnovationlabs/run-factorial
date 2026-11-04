"""A stand-in `codex` TUI for the host session tests: it runs in a real tmux pane, reads its
terminal raw, and writes what the adapter reads off the real one (fixtures/codex-0.159.0).

As 0.159.0 does, a launch in a folder `$CODEX_HOME/config.toml` does not seed `trusted` puts up
the folder-consent screen first, with "Open restricted" selected, and waits for Enter. A
`trusted` folder whose `.codex/config.toml` declares hooks shows "Hooks need review" instead,
and waits for ever. A hook of the config runs only when its `hooks.state` key, on the config's
real path, holds the hash this fake computes itself. SessionStart fires as the first turn
starts, never at a launch with no turn.

The rollout is `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local time>-<thread>.jsonl`, begun with
its `session_meta` line once the consent is taken; `resume <thread>` appends to it. Ctrl-C on an
empty input line quits, exit 0; on a draft it empties the draft. `/quit` quits. A hangup exits.
`sandbox ...` exits 0 at once, as the real one does where its sandbox starts.

`FAKE_TUI`, comma-separated, scripts the launch: `fail_turn` ends its first turn on a 401,
`slow` never ends it, `turns=N` makes it N turns, `no_rollout` puts up a login screen and never
begins a rollout, `sign_in` puts up the login screen of a refused login in place of the consent
screen, `deaf_quit` ignores Ctrl-C and `/quit`, and `spawn_child` leaves a `sleep` in
a session of its own with none of the TUI's environment, as a command codex runs and leaves
running is, its pid written to `FAKE_TUI_CHILD`. A typed turn holding FAILME or SLOWME does the
same as `fail_turn` or `slow`; one holding ASKME leaves its turn on a `request_user_input` call,
as the picker does, with no Stop. `FAKE_TUI_DUMP` names a file it writes its argv, cwd and
environment to, `FAKE_TUI_KEYS` one it appends its input to.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import termios
import tomllib
import tty
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.adapters.codex_cli.cli import INPUT_MARK

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
FLAGS_WITH_VALUES = ("-m", "-C", "-s", "-a")
LABELS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "Stop": "stop"}
ASK_ARGS = {
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
USAGE = {
    "input_tokens": 1000,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 7,
    "reasoning_output_tokens": 0,
    "total_tokens": 1007,
}


def stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trusted_hash(label: str, command: str) -> str:
    identity = {
        "event_name": label,
        "hooks": [{"type": "command", "command": command, "timeout": 600, "async": False}],
    }
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


class Tui:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.resumed = bool(argv) and argv[0] == "resume"
        words = argv[1:] if self.resumed else argv
        self.images: list[str] = []
        positional: list[str] = []
        flags: dict[str, str] = {}
        it = iter(words)
        for word in it:
            if word in FLAGS_WITH_VALUES:
                flags[word] = next(it)
            elif word.startswith("--image="):
                self.images.append(word.removeprefix("--image="))
            elif not word.startswith("-"):
                positional.append(word)
        self.model = flags.get("-m", "gpt-fake")
        self.home = Path(os.environ["CODEX_HOME"])
        self.config: dict[str, Any] = tomllib.loads((self.home / "config.toml").read_text())
        self.thread = positional.pop(0) if self.resumed else str(uuid.uuid4())
        self.prompt = positional[0] if positional else None
        self.modes: dict[str, str] = {}
        for part in os.environ.get("FAKE_TUI", "").split(","):
            mode, _, value = part.partition("=")
            self.modes[mode] = value
        self.rollout: Path | None = None
        self.buffer = ""
        self.started = False
        self.turns_done = 0
        self.working = False

    # -- the files ------------------------------------------------------------------------

    def find_rollout(self) -> Path:
        found = list((self.home / "sessions").glob(f"*/*/*/rollout-*-{self.thread}.jsonl"))
        if len(found) != 1:
            sys.stderr.write(f"no rollout for thread {self.thread}\n")
            os._exit(1)
        return found[0]

    def begin_rollout(self) -> None:
        now = datetime.now()
        folder = self.home / "sessions" / now.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        self.rollout = folder / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{self.thread}.jsonl"
        meta = {"id": self.thread, "session_id": self.thread, "cwd": os.getcwd()}
        self.write("session_meta", meta)

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        assert self.rollout is not None
        record = {"timestamp": stamp(), "type": kind, "payload": payload}
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def event(self, payload: dict[str, Any]) -> None:
        self.write("event_msg", payload)

    def hook(self, event: str, turn_id: str | None, **fields: Any) -> None:
        state = self.config.get("hooks", {}).get("state", {})
        key_base = f"{os.path.realpath(self.home / 'config.toml')}:{LABELS[event]}"
        for group, entry in enumerate(self.config.get("hooks", {}).get(event, [])):
            for index, handler in enumerate(entry.get("hooks", [])):
                command = handler["command"]
                trusted = state.get(f"{key_base}:{group}:{index}", {}).get("trusted_hash")
                if trusted != trusted_hash(LABELS[event], command):
                    continue
                body = {
                    "session_id": self.thread,
                    **({"turn_id": turn_id} if turn_id else {}),
                    "transcript_path": str(self.rollout),
                    "cwd": os.getcwd(),
                    "hook_event_name": event,
                    "model": self.model,
                    "permission_mode": "bypassPermissions",
                    **fields,
                }
                subprocess.run(
                    ["/bin/sh", "-c", command],
                    input=json.dumps(body).encode(),
                    start_new_session=True,
                    check=False,
                )

    # -- a turn ---------------------------------------------------------------------------

    def start_turn(self, text: str, images: list[str]) -> None:
        turn_id = str(uuid.uuid4())
        if not self.started:
            self.started = True
            self.hook("SessionStart", None, source="resume" if self.resumed else "startup")
        self.hook("UserPromptSubmit", turn_id, prompt=text)
        self.event({"type": "task_started", "turn_id": turn_id})
        content: list[dict[str, Any]] = [{"type": "local_image", "path": p} for p in images]
        content.append({"type": "text", "text": text, "text_elements": []})
        self.item(turn_id, {"type": "UserMessage", "id": str(uuid.uuid4()), "content": content})
        first = self.turns_done == 0
        if (first and "slow" in self.modes) or "SLOWME" in text:
            self.working = True
            return
        if "ASKME" in text:
            call = {
                "type": "function_call",
                "name": "request_user_input",
                "arguments": json.dumps(ASK_ARGS),
                "call_id": f"call_{uuid.uuid4().hex}",
            }
            self.write("response_item", call)
            self.working = True
            return
        if (first and "fail_turn" in self.modes) or "FAILME" in text:
            error = {"message": "unexpected status 401 Unauthorized: Incorrect API key provided"}
            self.event(
                {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": None,
                    "error": error,
                }
            )
            self.turns_done += 1
            return
        reply = f"done: {text[:40]}"
        self.item(
            turn_id,
            {
                "type": "AgentMessage",
                "id": f"msg_{uuid.uuid4().hex}",
                "content": [{"type": "Text", "text": reply}],
            },
        )
        self.write(
            "token_usage_record",
            {
                "thread_id": self.thread,
                "turn_id": turn_id,
                "usage": USAGE,
                "turn_token_usage": USAGE,
                "thread_token_usage": USAGE,
            },
        )
        self.hook("Stop", turn_id, stop_hook_active=False, last_assistant_message=reply)
        self.event({"type": "task_complete", "turn_id": turn_id, "last_agent_message": reply})
        self.turns_done += 1

    def item(self, turn_id: str, item: dict[str, Any]) -> None:
        self.event(
            {"type": "item_completed", "thread_id": self.thread, "turn_id": turn_id, "item": item}
        )

    # -- the terminal ---------------------------------------------------------------------

    def draw_only(self, screen: str) -> None:
        sys.stdout.write(f"\x1b[2J\x1b[H{screen}")
        sys.stdout.flush()

    def draw(self, banner: str = "") -> None:
        sys.stdout.write(f"\x1b[2J\x1b[H{banner}{INPUT_MARK} {self.buffer}")
        sys.stdout.flush()

    def submit(self) -> None:
        text, self.buffer = self.buffer, ""
        if text == "/quit" and "deaf_quit" not in self.modes:
            raise SystemExit(0)
        if text and text != "/quit" and not self.working:
            self.start_turn(text, [])
        self.draw()

    def keys(self, data: bytes, pasting: bool) -> bool:
        """Feed raw input; returns whether a bracketed paste is still open."""
        while data:
            if pasting:
                body, end, data = data.partition(PASTE_END)
                self.buffer += body.decode(errors="replace").replace("\r", "\n")
                pasting = not end
                continue
            if data.startswith(PASTE_START):
                data, pasting = data[len(PASTE_START) :], True
                continue
            char, data = data[:1], data[1:]
            if char in (b"\r", b"\n"):
                self.submit()
            elif char == b"\x03":
                if not self.buffer and not self.working and "deaf_quit" not in self.modes:
                    raise SystemExit(0)
                self.buffer = ""
            elif char == b"\x7f":
                self.buffer = self.buffer[:-1]
            elif char != b"\x1b":
                self.buffer += char.decode(errors="replace")
        self.draw()
        return pasting


def wait_forever(stdin: int) -> None:
    while True:
        os.read(stdin, 4096)


def trust(tui: Tui) -> str | None:
    project = tui.config.get("projects", {}).get(os.path.realpath(os.getcwd()), {})
    return project.get("trust_level")


def repo_hooks() -> bool:
    try:
        repo = tomllib.loads(Path(".codex/config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(repo.get("hooks"))


def take_consent(tui: Tui, stdin: int) -> None:
    level = trust(tui)
    if level == "trusted":
        if repo_hooks():
            tui.draw("Hooks need review\r\n2 hooks are new or changed\r\n")
            wait_forever(stdin)
        return
    if level is None:
        tui.draw("Do you trust the contents of this directory?\r\n")
        wait_forever(stdin)
    sys.stdout.write(
        f"\x1b[2J\x1b[HFolder access\r\n{os.getcwd()}\r\nConfig, hooks, and exec policies from "
        f"untrusted folders stay disabled\r\n{INPUT_MARK} 1. Open restricted\r\n  2. Quit"
    )
    sys.stdout.flush()
    while b"\r" not in os.read(stdin, 4096):
        pass


def main() -> None:
    if sys.argv[1:2] == ["sandbox"]:
        sys.exit(0)
    tui = Tui(sys.argv[1:])
    if dump := os.environ.get("FAKE_TUI_DUMP"):
        Path(dump).write_text(
            json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)})
        )
    signal.signal(signal.SIGHUP, lambda signum, frame: os._exit(128 + signum))
    if "spawn_child" in tui.modes:
        child = subprocess.Popen(
            ["/bin/sleep", "1000"],
            env={"PATH": "/bin:/usr/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        Path(os.environ["FAKE_TUI_CHILD"]).write_text(str(child.pid))
    keys = os.environ.get("FAKE_TUI_KEYS")
    stdin = sys.stdin.fileno()
    tty.setraw(stdin, termios.TCSANOW)
    sys.stdout.write("\x1b[?2004h")
    if "sign_in" in tui.modes:
        tui.draw_only("Welcome to Codex\r\nSign in with ChatGPT\r\n")
        wait_forever(stdin)
    take_consent(tui, stdin)
    if "no_rollout" in tui.modes:
        tui.draw("Sign in with ChatGPT to continue\r\n")
        wait_forever(stdin)
    if tui.resumed:
        tui.rollout = tui.find_rollout()
    else:
        tui.begin_rollout()
    tui.draw()
    if tui.prompt is not None:
        for _ in range(int(tui.modes.get("turns") or 1)):
            tui.start_turn(tui.prompt, tui.images)
        tui.draw()
    pasting = False
    while True:
        select.select([stdin], [], [])
        data = os.read(stdin, 65536)
        if not data:
            os._exit(1)
        if keys:
            with open(keys, "ab") as handle:
                handle.write(data)
        try:
            pasting = tui.keys(data, pasting)
        except SystemExit:
            sys.stdout.write("\r\nTo continue this session, run codex resume " + tui.thread)
            sys.stdout.flush()
            raise


if __name__ == "__main__":
    main()
