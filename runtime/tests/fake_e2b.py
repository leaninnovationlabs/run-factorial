"""A `SandboxClient` with no network behind it: the test double for the e2b adapter's seam.

A `FakeSandbox` plays the agent's session, from a `Script` with a `TuiScript`: once `SESSION`
starts, each `SESSION_POLL` releases the next step's signal and session-log lines, then answers
as the script does. The log is the one `ARGV` names: claude's by its session id, codex's by its
thread, a rollout `ROLLOUT_FIND` reports once it is written; a spawn's first look for it releases
the first step, as codex begins its rollout with its first turn. The signals file and every log
are kept on the sandbox, so a pause keeps them as a disk would. A codex session can first hold
everything behind its folder-consent screen until `CONSENT` takes it, or be gone before it
looks. The session lives until `FINISH` (or `CODEX_FINISH`), a stop or a scrub, unless it
`ends_by_itself` once its steps are out.

It plays a session view too: `INSPECT` opens it unless the client says it fails to, its CLI the
client's `view` script on the session `VIEW_ARGV` names, and `INSPECT_STATUS` reports it with the
client's `view_clients` and `view_activity`. It ends at `INSPECT_STOP`, which then cuts the log
back as `LOG` and `SIZE` say, at a stop, a scrub or a finish script, which end every process of
the agent's, and at a pause. As it ends, its CLI appends the script's exit lines to the log.

A preview runs from `PREVIEW` until `PREVIEW_STOP`, a pause, or its script ending by itself
(`end_preview`), after which `PREVIEW_ENDED` prints the client's `preview_left`: its services are
another uid's, so no stop of the agent's ends it. Its setup is the agent's, though: while the
client's next preview report says `setup running`, any stop of the agent's cuts it, and the
preview then reports its setup failed, as `PREVIEW_SH` does. It plays `FIREWALL` by recording its
mode, which a pause clears, and whether a credential rule stood when it lifted the containment.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from runtime.adapters.claude_cli.cli import FLAG_RESUME, FLAG_SESSION_ID
from runtime.adapters.codex_cli import cli as codex
from runtime.adapters.codex_cli.tui import thread_of
from runtime.adapters.e2b.client import (
    CommandOutput,
    NetworkPolicy,
    SandboxGone,
    SandboxInfo,
    SandboxOptions,
)
from runtime.adapters.e2b.layout import (
    ARGV,
    CAPTURE,
    CODEX_FINISH,
    CODEX_NATIVE,
    CONFIG_DIR,
    CONSENT,
    CONSENT_NOT_ASKED,
    END_SHELL,
    EXIT_CODE,
    FINISH,
    FIREWALL,
    HANGUP_EXIT_CODE,
    INSPECT,
    INSPECT_SESSION,
    INSPECT_STATUS,
    INSPECT_STOP,
    PREVIEW,
    PREVIEW_ENDED,
    PREVIEW_STATUS,
    PREVIEW_STOP,
    REALPATHS,
    REWORK,
    ROLLOUT_DIR,
    ROLLOUT_FIND,
    SCRUB,
    SESSION,
    SESSION_POLL,
    SETUP,
    STDERR,
    STOP,
    TMUX_SOCKET,
    VIEW_ARGV,
    WORKSPACE,
    session_log,
)

#: The pull request head `REWORK` reports by default.
REWORK_HEAD = "0123abc" + "0" * 33
#: The thread a codex spawn names its rollout after, by default.
CODEX_THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
#: What codex draws first at every launch in a checkout seeded untrusted.
CONSENT_SCREEN = f"""  Folder access
  {WORKSPACE}

  Config, hooks, and exec policies from untrusted folders stay
  disabled. Opening will not change saved trust.

{codex.INPUT_MARK} 1. {codex.CONSENT_RESTRICTED}
  2. Quit
"""
#: A `request_user_input` call's arguments, as codex 0.159.0 passes them.
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
_THREAD = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def rollout_path(thread: str) -> str:
    return f"{ROLLOUT_DIR}/2026/09/23/rollout-2026-09-23T12-00-00-{thread}.jsonl"


def codex_hook(event: str, thread: str, **fields: object) -> str:
    """A codex hook's line, as `SIGNAL` appends it."""
    hook = {"session_id": thread, "hook_event_name": event, **fields}
    return json.dumps({"at": "2026-09-23T12:00:00Z", "hook": hook})


def codex_turn(
    thread: str = CODEX_THREAD,
    *,
    summary: str = "Opened PR #9.",
    tokens: tuple[int, int] = (100, 7),
    failed: str | None = None,
    meta: bool = False,
    asked: bool = False,
) -> tuple[list[str], list[str]]:
    """One codex turn as a `TuiScript` step: its hook lines and its rollout lines, shaped as
    `fixtures/codex-0.159.0` records them. `meta` opens the rollout and the launch, as a spawn's
    first turn does. `failed` ends the turn as a 401 does: no Stop hook, no usage, and the error
    on its `task_complete`. `asked` leaves the turn on a `request_user_input` call, with no
    Stop."""

    def record(kind: str, payload: Mapping[str, object]) -> str:
        at = "2026-09-23T12:00:00.000Z"
        return json.dumps({"timestamp": at, "type": kind, "payload": payload})

    def event(kind: str, **payload: object) -> str:
        return record("event_msg", {"type": kind, **payload})

    def item(kind: str, content: str, text: str) -> str:
        part = {"type": content, "text": text}
        return event("item_completed", item={"type": kind, "id": kind, "content": [part]})

    hooks = [codex_hook("SessionStart", thread, source="startup")] if meta else []
    log = [record("session_meta", {"id": thread})] if meta else []
    hooks.append(codex_hook("UserPromptSubmit", thread, prompt="the turn"))
    log += [event("task_started", turn_id="turn"), item("UserMessage", "text", "the turn")]
    if asked:
        call = {
            "type": "function_call",
            "name": "request_user_input",
            "arguments": json.dumps(ASK_ARGS),
            "call_id": f"call_{uuid.uuid4().hex}",
        }
        log.append(record("response_item", call))
        return hooks, log
    if failed is not None:
        log.append(event("task_complete", last_agent_message=None, error={"message": failed}))
        return hooks, log
    usage = {"input_tokens": tokens[0], "cached_input_tokens": 0, "output_tokens": tokens[1]}
    log += [
        item("AgentMessage", "Text", summary),
        record("token_usage_record", {"turn_token_usage": usage}),
        event("task_complete", last_agent_message=summary),
    ]
    hooks.append(codex_hook("Stop", thread, last_assistant_message=summary))
    return hooks, log


@dataclass
class TuiScript:
    """What an interactive session does once `SESSION` starts."""

    #: Per `SESSION_POLL`: the signal lines and the session-log lines it releases first.
    steps: Sequence[tuple[Sequence[str], Sequence[str]]] = ()
    #: The signal and log lines written as the CLI leaves, however it leaves: the `SessionEnd`
    #: and the cost-state.
    on_exit: tuple[Sequence[str], Sequence[str]] = ((), ())
    #: Leaves once its steps are out, as a CLI ending on its own does. Otherwise it waits at its
    #: prompt until `FINISH`, a stop or a scrub.
    ends_by_itself: bool = False
    #: The CLI's exit code when it leaves by itself or by its quit command; a hangup leaves 255.
    exit_code: int = 0
    #: Whether the quit command typed at its prompt ends it. When it does not, the finish script
    #: hangs it up.
    exits: bool = True
    #: What `CAPTURE` prints.
    pane: str = ""
    #: Codex's folder-consent screen, drawn at every launch: no step is released until `CONSENT`
    #: takes it.
    consent: bool = False
    #: Gone before `CONSENT` looks, as a launch that dies at its startup is, its tmux server with
    #: it: `CONSENT` exits 1.
    gone_at_consent: bool = False
    #: The thread a codex spawn names its rollout after.
    thread: str = CODEX_THREAD
    #: Other threads whose rollouts the launch begins too, which `ROLLOUT_FIND` lists beside its
    #: own: a sub-agent's, say.
    other_threads: Sequence[str] = ()


@dataclass
class Script:
    """What the agent does once started: its session, a `TuiScript()` when None."""

    tui: TuiScript | None = None


@dataclass
class Command:
    command: str
    user: str
    envs: Mapping[str, str]


class FakeSandbox:
    def __init__(self, client: FakeSandboxClient, sandbox_id: str, options: SandboxOptions):
        self._client = client
        self._id = sandbox_id
        self.options = options
        self.network = options.network
        self.networks: list[NetworkPolicy] = [options.network]
        self.files: dict[str, tuple[bytes, str]] = {}
        self.commands: list[Command] = []
        self.state = "running"
        self._pid = 100
        self._running: int | None = None
        self._exit_code: int | None = None
        self._preview: int | None = None
        self._reports = 0
        #: A stop of the agent's cut the preview's setup, which `PREVIEW` clears.
        self._setup_cut = False
        #: For each `update_network`: was the preview's script alive when the rules changed?
        self.serving_at_network: list[bool] = []
        #: The mode `FIREWALL` last loaded since the sandbox woke, and for each `update_network`
        #: whether it was `contain`.
        self.firewall: str | None = None
        self.contained_at_network: list[bool] = []
        #: For each `FIREWALL` load that lifts the containment: did a credential rule stand?
        self.injected_at_open: list[bool] = []
        #: The session view's start script while it runs, and when its session was created; the
        #: log its CLI writes, and whether its consent screen is up.
        self._view: int | None = None
        self._view_created = 0
        self._view_log: str | None = None
        self._view_consenting = False
        #: For each `update_network`: was a session view open when the rules changed?
        self.view_at_network: list[bool] = []
        #: The interactive session, while `SESSION` runs: its script and unreleased steps.
        self._tui: TuiScript | None = None
        self._steps: list[tuple[Sequence[str], Sequence[str]]] = []
        self._log: str | None = None
        self._consenting = False
        #: The logs written since the session started, which `ROLLOUT_FIND` reports.
        self._written: set[str] = set()
        #: The files a session writes, kept across a pause like the disk they stand for.
        self.signals_file = b""
        self.logs: dict[str, bytes] = {}

    @property
    def sandbox_id(self) -> str:
        return self._id

    @property
    def view_open(self) -> bool:
        return self._view is not None

    async def run(
        self,
        command: str,
        *,
        user: str = "root",
        envs: Mapping[str, str] | None = None,
        timeout_seconds: float = 120,
    ) -> CommandOutput:
        self._check()
        self.commands.append(Command(command, user, dict(envs or {})))
        for marker, output in self._client.command_results.items():
            if marker in command:
                return output
        if command == SETUP:
            return self._client.setup_result
        if command == "env -0":
            printed = "PATH=/usr/local/bin:/usr/bin:/bin\0NODE_EXTRA_CA_CERTS=/ca\0"
            return CommandOutput(0, printed, "")
        if command == FIREWALL:
            if self._client.firewall_exit:
                return CommandOutput(self._client.firewall_exit, "", "nft: no such table\n")
            self.firewall = (envs or {})["MODE"]
            if self.firewall == "open":
                self.injected_at_open.append(bool(self.network.inject))
            return CommandOutput(0, "", "")
        if command == PREVIEW_STOP:
            self._preview = None
            return CommandOutput(0, "", "")
        if command in (STOP, END_SHELL) or command.startswith(SCRUB):
            # stop.sh's SIGTERM ends the CLI; a scrub's kill-server hangs it up first.
            self._end(HANGUP_EXIT_CODE if command != STOP else None)
            self._end_view()
            self._cut_setup()
            return CommandOutput(0, "", "")
        if command.startswith(INSPECT_STOP):
            self._end_view()
            self._cut_setup()
            values = envs or {}
            log, size = values.get("LOG", ""), int(values["SIZE"])
            if log in self.logs and len(self.logs[log]) > size:
                self.logs[log] = self.logs[log][:size]
            return CommandOutput(0, "", "")
        if command == INSPECT_STATUS:
            if self._view is None:
                return CommandOutput(0, "closed\n", "")
            client = self._client
            shown = f"{client.view_clients} {client.view_activity} {self._view_created}"
            return CommandOutput(0, f"open {shown}\n", "")
        if command == SESSION_POLL:
            return self._poll(envs or {})
        if command in (FINISH, CODEX_FINISH):
            mode = (envs or {}).get("MODE")
            assert mode in ("exit", "hangup"), mode
            self._cut_setup()
            if self._tui is not None:
                typed = mode == "exit" and self._tui.exits
                self._end(self._tui.exit_code if typed else HANGUP_EXIT_CODE)
            self._end_view()  # the server's kill
            return CommandOutput(0, "", "")
        if command == REWORK:
            return self._client.rework_result
        if command == CAPTURE:
            return CommandOutput(0, self._screen(), "")
        if command == CONSENT and (envs or {}).get("TMUX_TARGET") == INSPECT_SESSION:
            return self._view_consent()
        if command == CONSENT:
            if self._tui is not None and self._tui.gone_at_consent:
                self._end(1)
                return CommandOutput(1, "", f"no server running on {TMUX_SOCKET}\n")
            if not self._consenting:
                return CommandOutput(CONSENT_NOT_ASKED, self._screen() + "\n", "")
            self._consenting = False
            return CommandOutput(0, "", "")
        if command == ROLLOUT_FIND:
            if self._live() and self._steps and self._log not in self._written:
                self._write(*self._steps.pop(0))
            found = sorted(log for log in self._written if log.startswith(f"{ROLLOUT_DIR}/"))
            return CommandOutput(0, "".join(f"{log}\n" for log in found), "")
        if command == REALPATHS:
            real = f"{self._client.workspace_real}\n{self._client.config_real}\n"
            return CommandOutput(0, real, "")
        if command == PREVIEW_STATUS:
            if self._setup_cut:
                return CommandOutput(0, "setup failed\nnpm ERR! signal SIGTERM\n", "")
            reports = self._client.preview_reports
            report = reports[min(self._reports, len(reports) - 1)]
            self._reports += 1
            return CommandOutput(0, report + "\n", "")
        if command == PREVIEW_ENDED:
            return CommandOutput(0, self._client.preview_left, "")
        if command == f"cat {EXIT_CODE}":
            if self._exit_code is None:
                return CommandOutput(1, "", "No such file")
            return CommandOutput(0, f"{self._exit_code}\n", "")
        if command.startswith("tail -c") and STDERR in command:
            return CommandOutput(0, self._client.stderr, "")
        return CommandOutput(0, "", "")

    async def start(
        self, command: str, *, user: str = "root", envs: Mapping[str, str] | None = None
    ) -> int:
        self._check()
        assert command in (PREVIEW, SESSION, INSPECT) or self._client.command_results
        self.commands.append(Command(command, user, dict(envs or {})))
        self._pid += 1
        if command == PREVIEW:
            self._preview, self._setup_cut = self._pid, False
            return self._pid
        if command == INSPECT:
            if self._client.view_opens:
                self._view, self._view_created = self._pid, self._client.view_activity
                self._view_log = self._session_log(self._client.view, VIEW_ARGV)
                self._view_consenting = self._client.view.consent
            return self._pid
        if command != SESSION:
            return self._pid  # a script `command_results` answers, gone at once
        script = self._client.scripts.pop(0) if self._client.scripts else Script()
        self._exit_code = None
        self._running = self._pid
        self._tui = script.tui or TuiScript()
        self._steps = list(self._tui.steps)
        self._log = self._session_log(self._tui, ARGV)
        self._consenting = self._tui.consent
        self._written = set()
        for thread in self._tui.other_threads:
            meta = json.dumps({"type": "session_meta", "payload": {"id": thread}})
            self.logs[rollout_path(thread)] = f"{meta}\n".encode()
            self._written.add(rollout_path(thread))
        return self._pid

    async def pids(self) -> frozenset[int]:
        self._check()
        tui = self._tui
        if tui is not None and self._running is not None and not self._steps and tui.ends_by_itself:
            self._end(tui.exit_code)
        return frozenset(
            pid for pid in (self._running, self._preview, self._view) if pid is not None
        )

    async def write_many(self, files: Mapping[str, bytes], *, user: str = "root") -> None:
        self._check()
        self.files.update({path: (content, user) for path, content in files.items()})

    async def update_network(self, network: NetworkPolicy) -> None:
        self._check()
        self.network = network
        self.networks.append(network)
        self.serving_at_network.append(self._preview is not None)
        self.view_at_network.append(self._view is not None)
        self.contained_at_network.append(self.firewall == "contain")

    def host(self, port: int) -> str:
        return f"{port}-{self._id}.e2b.test"

    def _end(self, exit_code: int | None = None) -> None:
        if self._running is None:
            return
        assert self._tui is not None
        self._write(*self._tui.on_exit)
        self._steps = []
        self._running = None
        self._exit_code = 143 if exit_code is None else exit_code

    def play(self, signals: Sequence[str] = (), log: Sequence[str] = ()) -> None:
        """One more step for the running session, released by the next `SESSION_POLL`: how a
        test holds a session at a point until it has looked."""
        assert self._tui is not None and self._running is not None, "no session is running"
        self._steps.append((signals, log))

    def end_preview(self) -> None:
        """The preview's script exits by itself, as one that fails before its setup does: nothing
        marks the setup, so the preview's reports stay as they were."""
        self._preview = None

    def close_view(self) -> None:
        """The view's CLI leaves by itself, and its tmux session with it."""
        self._end_view()

    def cold_boot(self) -> None:
        """A resume from a filesystem-only pause: no process survives, nor writes on its way, and
        no firewall rule."""
        self._preview = None
        self.firewall = None
        self._view = None
        if self._tui is not None and self._running is not None:
            self._running = None
            self._steps = []

    def _cut_setup(self) -> None:
        """A stop of the agent's uid: a setup still running is the agent's, so it dies, and with
        it the preview's start script, which records the setup failed."""
        reports = self._client.preview_reports
        report = reports[min(self._reports, len(reports) - 1)]
        if self._preview is not None and report.startswith("setup running"):
            self._preview, self._setup_cut = None, True

    def _end_view(self) -> None:
        if self._view is not None and self._view_log is not None:
            lines = "".join(f"{line}\n" for line in self._client.view.on_exit[1]).encode()
            self.logs[self._view_log] = self.logs.get(self._view_log, b"") + lines
        self._view = None

    def _view_consent(self) -> CommandOutput:
        view = self._client.view
        if self._view is not None and view.gone_at_consent:
            self._end_view()
            return CommandOutput(1, "", f"no server running on {TMUX_SOCKET}\n")
        if not self._view_consenting:
            screen = view.pane if self._view is not None else ""
            return CommandOutput(CONSENT_NOT_ASKED, screen + "\n", "")
        self._view_consenting = False
        return CommandOutput(0, "", "")

    def _session_log(self, tui: TuiScript, launch: str) -> str:
        """The log of the session the `launch` argv file names, as the CLI would: claude's by
        `--session-id` or `--resume`; codex's a new rollout for `tui.thread`, or `resume`'s
        thread's own."""
        assert launch in self.files, f"a TUI started with no {launch} written"
        argv = self.files[launch][0].decode().split("\0")
        if argv[0] == CODEX_NATIVE:
            if argv[1] != codex.SUBCMD_RESUME:
                return rollout_path(tui.thread)
            (thread,) = [arg for arg in argv if _THREAD.fullmatch(arg)]
            known = [log for log in self.logs if thread_of(log) == thread]
            return known[0] if known else rollout_path(thread)
        for flag in (FLAG_SESSION_ID, FLAG_RESUME):
            if flag in argv:
                return session_log(argv[argv.index(flag) + 1])
        raise AssertionError(f"SESSION's ARGV names no session: {argv!r}")

    def _screen(self) -> str:
        if self._tui is None:
            return ""
        return CONSENT_SCREEN if self._consenting else self._tui.pane

    def _write(self, signals: Sequence[str], log: Sequence[str]) -> None:
        self.signals_file += "".join(f"{line}\n" for line in signals).encode()
        if log:
            assert self._log is not None
            lines = "".join(f"{line}\n" for line in log).encode()
            self.logs[self._log] = self.logs.get(self._log, b"") + lines
            self._written.add(self._log)

    def _poll(self, envs: Mapping[str, str]) -> CommandOutput:
        """`SESSION_POLL_SH`: release the next step while the session runs, then answer."""
        if self._live() and self._steps:
            self._write(*self._steps.pop(0))
        sig_from, log_from, limit = (int(envs[k]) for k in ("SIG_FROM", "LOG_FROM", "LIMIT"))
        log = self.logs.get(envs["LOG"])
        sizes = f"{-1 if log is None else len(log)} {len(self.signals_file)}"
        signals = base64.b64encode(self.signals_file[sig_from : sig_from + limit]).decode()
        logged = "" if log is None else base64.b64encode(log[log_from : log_from + limit]).decode()
        return CommandOutput(0, f"{sizes}\n{signals}\n{logged}\n", "")

    def _live(self) -> bool:
        """A session runs and is past its consent screen."""
        return self._running is not None and self._tui is not None and not self._consenting

    def _check(self) -> None:
        if self._id in self._client.gone:
            raise SandboxGone(self._id)
        if self.state != "running":
            raise AssertionError(f"sandbox {self._id} used while {self.state}")


@dataclass
class FakeSandboxClient:
    scripts: list[Script] = field(default_factory=list)
    setup_result: CommandOutput = field(default_factory=lambda: CommandOutput(0, "", ""))
    stderr: str = ""
    sandboxes: dict[str, FakeSandbox] = field(default_factory=dict)
    gone: set[str] = field(default_factory=set)
    killed: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    connects: list[tuple[str, int]] = field(default_factory=list)
    create_error: Exception | None = None
    #: Raised by `connect` and `pause`, as a transient E2B failure would be.
    connect_error: Exception | None = None
    pause_error: Exception | None = None
    info_error: Exception | None = None
    #: What a command prints when it contains the key: consulted before every other script.
    command_results: dict[str, CommandOutput] = field(default_factory=dict)
    #: `FIREWALL`'s exit code: anything but 0 loads nothing.
    firewall_exit: int = 0
    #: What `preview-status.sh` prints: each call takes the next report, and the last repeats.
    preview_reports: list[str] = field(default_factory=lambda: ["serving\nhead 0123abc\nready web"])
    #: What `PREVIEW_ENDED` prints: what a preview's script left in its logs.
    preview_left: str = ""
    #: Every sandbox's creation time as `info` and `list` report it; old enough to reap.
    started_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    #: What `REWORK` prints: the branch and the head it reset to.
    rework_result: CommandOutput = field(
        default_factory=lambda: CommandOutput(0, f"spec/12-fix {REWORK_HEAD}\n", "")
    )
    #: What `REALPATHS` prints.
    workspace_real: str = WORKSPACE
    config_real: str = CONFIG_DIR
    #: The pins whose build passed the session smoke, and every pin asked about.
    verdicts: set[str] = field(default_factory=set)
    verdict_checks: list[str] = field(default_factory=list)
    #: Whether `INSPECT` opens its view, or the view exits as it starts.
    view_opens: bool = True
    #: The view's CLI: its consent screen, the `pane` it shows when none comes, and the log lines
    #: it appends on its way out.
    view: TuiScript = field(default_factory=TuiScript)
    #: What `INSPECT_STATUS` reports: the clients attached and tmux's last activity, in epoch
    #: seconds; the latter is the session's creation time too.
    view_clients: int = 0
    view_activity: int = 1_790_000_000

    def everything_seen(self) -> str:
        """Every byte written into a sandbox and every command's env — where a leak would be."""
        parts: list[str] = []
        for sandbox in self.sandboxes.values():
            parts += [content.decode(errors="replace") for content, _ in sandbox.files.values()]
            for command in sandbox.commands:
                parts += [command.command, *command.envs.keys(), *command.envs.values()]
        return "\n".join(parts)

    async def create(self, options: SandboxOptions) -> FakeSandbox:
        if self.create_error is not None:
            raise self.create_error
        sandbox = FakeSandbox(self, f"sbx{len(self.sandboxes) + 1}", options)
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox

    async def connect(self, sandbox_id: str, *, timeout_seconds: int) -> FakeSandbox:
        if sandbox_id in self.gone or sandbox_id not in self.sandboxes:
            raise SandboxGone(sandbox_id)
        if self.connect_error is not None:
            raise self.connect_error
        self.connects.append((sandbox_id, timeout_seconds))
        sandbox = self.sandboxes[sandbox_id]
        if sandbox.state == "paused":
            sandbox.state = "running"  # a filesystem-only pause cold-boots: nothing runs
            sandbox.cold_boot()
        return sandbox

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        if self.info_error is not None:
            raise self.info_error
        if sandbox_id in self.gone or sandbox_id not in self.sandboxes:
            return None
        sandbox = self.sandboxes[sandbox_id]
        metadata = dict(sandbox.options.metadata)
        return SandboxInfo(sandbox_id, sandbox.state, metadata, self.started_at)

    async def pause(self, sandbox_id: str) -> None:
        if self.pause_error is not None:
            raise self.pause_error
        self.paused.append(sandbox_id)
        sandbox = self.sandboxes[sandbox_id]
        sandbox.state = "paused"
        sandbox.firewall = None
        sandbox._preview = None

    async def kill(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)
        self.gone.add(sandbox_id)

    async def list(self, metadata: Mapping[str, str]) -> Sequence[SandboxInfo]:
        found = [
            SandboxInfo(s.sandbox_id, s.state, dict(s.options.metadata), self.started_at)
            for s in self.sandboxes.values()
            if s.sandbox_id not in self.gone
        ]
        return [i for i in found if all(i.metadata.get(k) == v for k, v in metadata.items())]

    async def check(self) -> None:
        return None

    async def session_verified(self, pin: str) -> bool:
        self.verdict_checks.append(pin)
        return pin in self.verdicts

    def only(self) -> FakeSandbox:
        (sandbox,) = self.sandboxes.values()
        return sandbox

    def commands_by_name(self, command: str) -> list[Command]:
        """Every run or start of `command`, a script's path, across every sandbox, in order."""
        return [c for s in self.sandboxes.values() for c in s.commands if c.command == command]
