"""One small session on a template build, for a claude or a codex build, whose TUI the session
runs.

The template builder gates a pin on the containment smoke (every build, first) and then on this
session, since a template installs the CLI's latest rather than the version the parsers were
hand-checked against, and records the session's verdict on the build (decision 25).
`scripts/smoke/claude-e2b-smoke.py` runs the session, then a rework, as its mode 5.
It clones the public octocat/Hello-World and writes nothing to GitHub. The Docker end-to-end test
runs it on each real TUI against a mock API
(`runtime/tests/test_e2b_tui_docker.py`). Codex reports no cost (decision 34), so its session
must report tokens instead; and how long its first turn waited for a token is logged, since its
WebSocket to the API, when the egress refuses it, retries before codex falls back to HTTPS.

`containment_smoke` is the other check, for any template build, of the preview firewall (decision
54, design.md §7.10). In one public sandbox it shows that the preview's uid never gets the header
the egress proxy injects while the agent's does, that its servers still answer on a declared
port, that an undeclared port is closed at the edge, and that a connection it opened from a
declared port before the switch to contain is cut. It creates one sandbox and always kills it.
The template builder runs it first on every build and fails the build when it does
not hold; `scripts/smoke/e2b-containment-smoke.py` runs it on any template.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from pydantic import SecretStr

from runtime.adapters._cli.session_signals import json_object
from runtime.adapters.claude_cli.cli import RESULT_SUBTYPE, SUBTYPE_INIT
from runtime.adapters.codex_cli.cli import (
    EVENT_THREAD_STARTED,
    EVENT_TURN_STARTED,
    EVENT_TYPE,
    MSG_TASK_COMPLETE,
    PAYLOAD_TYPE,
    RECORD_EVENT_MSG,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TYPE,
)
from runtime.adapters.e2b.adapter import DIALOGS, SESSION_MIRROR, E2bAdapter
from runtime.adapters.e2b.client import NetworkPolicy, Sandbox, SandboxClient, SandboxOptions
from runtime.adapters.e2b.layout import AGENT_USER, CAPTURE, FIREWALL, PREVIEW_USER, SCRIPTS
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    ParkReason,
    RunResult,
    RunSpec,
    RunStatus,
    SessionState,
    Workspace,
)

log = logging.getLogger(__name__)

#: The model key each harness's run bills, read from the environment by the callers.
VENDOR_KEYS: Final = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
#: The cheapest model each vendor's API key serves.
MODELS: Final = {"claude": "claude-haiku-4-5-20251001", "codex": "gpt-5.3-codex"}
#: How long the session must wait at its prompt: past envd's 60 s default for a command, which
#: would end one started with a time limit.
HOLD: Final = timedelta(seconds=75)
#: Each harness's translated lines that carry no id, as (key, value): they may appear twice. A
#: segment's opening line carries only the session id, and codex's turn start nothing.
CONSTANT_LINES: Final = {
    "claude": frozenset({(RESULT_SUBTYPE, SUBTYPE_INIT)}),
    "codex": frozenset({(EVENT_TYPE, EVENT_THREAD_STARTED), (EVENT_TYPE, EVENT_TURN_STARTED)}),
}
FIRST_TOKEN_MS: Final = "time_to_first_token_ms"


def smoke_spec(
    harness: str,
    model_key: str,
    instructions: str = "Run `whoami` with your shell tool, then reply with the word pong.",
) -> RunSpec:
    return RunSpec(
        agent="smoke-agent",
        ticket="SMOKE-1",
        workspace=Workspace(
            repo="octocat/Hello-World", base="master", branch="smoke/{slug}", credential="none"
        ),
        prompts=AgentPrompts(
            system="You are a smoke-test agent. Do exactly what is asked.",
            instructions=instructions,
        ),
        secrets={VENDOR_KEYS[harness]: SecretStr(model_key)},
        model=MODELS[harness],
        max_turns=12,
    )


class _Stopped(Exception):
    """A step failed in a way no later step can run after."""


@dataclass
class _Smoke:
    adapter: E2bAdapter
    run_id: str
    poll: float
    turn_timeout: timedelta
    problems: list[str] = field(default_factory=list)
    finished: bool = False

    async def at_prompt(self, what: str) -> SessionState:
        """The session's state once it idles, or `_Stopped` saying why it never did."""
        try:
            async with asyncio.timeout(self.turn_timeout.total_seconds()):
                while not (status := await self.adapter.status(self.run_id)).is_terminal:
                    if status is RunStatus.IDLE:
                        return await self.adapter.session_state(self.run_id)
                    await asyncio.sleep(self.poll)
        except TimeoutError:
            raise _Stopped(
                f"{what} did not idle within {self.turn_timeout}: no Stop hook fired, so the "
                "trust seed or the hooks are broken"
            ) from None
        raise _Stopped(f"{what} ended {status} before it reached its prompt")

    async def screen(self) -> None:
        attached = self.adapter._attached(self.run_id)
        if attached is None:
            raise _Stopped("no supervisor holds the session's sandbox, so its screen is unread")
        output = await attached[1].run(CAPTURE)
        if not output.ok:
            self.problems.append(f"reading the TUI's screen failed: {output.stderr.strip()[:200]}")
            return
        screen = output.stdout.replace("\xa0", " ").splitlines()
        shown = [line.strip() for line in screen if DIALOGS.search(line)]
        if shown:
            self.problems.append(
                f"the TUI shows a first-run dialog at its prompt ({shown[0]!r}): a seed that "
                "should answer it is wrong"
            )

    async def hold(self, hold: timedelta) -> None:
        await asyncio.sleep(hold.total_seconds())
        status = await self.adapter.status(self.run_id)
        if status is not RunStatus.IDLE:
            raise _Stopped(
                f"the session was {status} after {hold} at its prompt, where it should wait "
                "for a human: something ended it while it idled"
            )

    async def restart(self, reattach: Callable[[], E2bAdapter], idle: SessionState) -> None:
        """What a worker restart does: this adapter's supervisors die with it, and a new one
        finds the session from the state alone, keeping the mirror as it was."""
        before = await _lines(self.adapter, self.run_id)
        dead = list(self.adapter._supervisors.values())
        for task in dead:
            task.cancel()
        await asyncio.gather(*dead, return_exceptions=True)
        self.adapter = reattach()
        status = await self.adapter.status(self.run_id)
        again = await self.adapter.session_state(self.run_id)
        if status is not RunStatus.IDLE or again.idle_reason is not idle.idle_reason:
            raise _Stopped(f"a restarted worker found the session {status}, {again.idle_reason}")
        if again.session_id != idle.session_id:
            self.problems.append("a restarted worker found another session")
        await asyncio.sleep(self.poll)
        if (await _lines(self.adapter, self.run_id))[: len(before)] != before:
            self.problems.append("a restarted worker rewrote the transcript mirrored before it")

    async def rollover(self, secrets: Mapping[str, SecretStr], idle: SessionState) -> None:
        """The worker's hourly rollover at the prompt: the session parked, then back, the same
        one, at its prompt."""
        await self.adapter.interrupt(self.run_id, ParkReason.ROLLOVER)
        status = await self.adapter.status(self.run_id)
        if status is not RunStatus.INTERRUPTED:
            result = await self.adapter.result(self.run_id)
            raise _Stopped(f"the rollover's hangup ended it {status}: {result.failure_reason}")
        await self.adapter.resume(self.run_id, None, secrets)
        resumed = await self.at_prompt("the resumed session")
        if resumed.session_id != idle.session_id:
            self.problems.append("the resume opened another session")

    async def finish(self) -> RunResult:
        await self.adapter.finish(self.run_id)
        self.finished = True
        return await self.end()

    async def end(self) -> RunResult:
        if not (await self.adapter.status(self.run_id)).is_terminal:
            await self.adapter.cancel(self.run_id)
        await self.adapter.drain()
        return await self.adapter.result(self.run_id)

    def raw(self) -> list[str]:
        """The session log as the worker mirrored it: every segment's lines."""
        path = self.adapter._run_dir(self.run_id) / SESSION_MIRROR
        if not path.exists():
            return []
        return path.read_bytes().decode("utf-8", errors="replace").splitlines()

    def reread(self, session_id: str) -> None:
        """The whole raw log through a translator of its own: the live one checked only the
        segment the CLI left cleanly, and a hangup's segment is still the build's to read."""
        translator = self.adapter.harness.transcript(
            session_id, turn=True, cost_base=None, skip=0
        )
        for line in self.raw():
            translator.feed(line)
        translator.end_segment()
        self.problems += [f"the session log carried {gap}" for gap in translator.missing()]
        if translator.unknown_types or translator.unreadable:
            log.warning(
                "not a failure: the session log carried line types the translator skips "
                "unread, %s, and %d unreadable lines",
                translator.unknown_types,
                translator.unreadable,
            )

    def first_token(self) -> None:
        """Codex's WebSocket to the API, refused or unauthenticated by the egress, retries five
        times before HTTPS: slower, never a failure, so only logged."""
        waited = None
        for line in self.raw():
            record = json_object(line) or {}
            payload = record.get(ROLLOUT_PAYLOAD)
            if (
                record.get(ROLLOUT_TYPE) == RECORD_EVENT_MSG
                and isinstance(payload, Mapping)
                and payload.get(PAYLOAD_TYPE) == MSG_TASK_COMPLETE
            ):
                waited = payload.get(FIRST_TOKEN_MS)
                break
        log.info("not a failure: codex's first turn waited %s ms for its first token", waited)


async def session_smoke_run(
    adapter: E2bAdapter,
    spec: RunSpec,
    *,
    turn_timeout: timedelta = timedelta(minutes=5),
    poll: float = 1.0,
    reattach: Callable[[], E2bAdapter] | None = None,
    hold: timedelta = HOLD,
) -> tuple[RunResult, list[str]]:
    """The session's result, and every reason not to run the build's TUI: empty means it
    passed. The session must reach its prompt at its turn's end, which only its Stop hook
    reports, with no first-run dialog on its screen; still be there after `hold`; be found
    whole by `reattach`'s adapter, a restarted worker on the same state root and team; come
    back at its prompt on the same session after a rollover; and end on `finish` as a
    success, with nothing the adapter reads missing from any segment of its log."""
    handle = await adapter.spawn(spec)
    smoke = _Smoke(adapter, handle.run_id, poll, turn_timeout)
    try:
        idle = await smoke.at_prompt("the session")
        if idle.idle_reason is not IdleReason.TURN_END:
            smoke.problems.append(f"the session idled on {idle.idle_reason}, not at its turn's end")
        if idle.turns < 1:
            smoke.problems.append("the session counted no turn")
        await smoke.screen()
        await smoke.hold(hold)
        if reattach is not None:
            await smoke.restart(reattach, idle)
        await smoke.rollover(spec.secrets, idle)
        result = await smoke.finish()
    except _Stopped as exc:
        smoke.problems.insert(0, str(exc))
        result = await smoke.end()
    problems = smoke.problems
    if smoke.finished and result.outcome is not RunStatus.SUCCEEDED:
        problems.append(f"the finish ended it {result.outcome}: {result.failure_reason}")
    problems += result.stream_warnings
    if not result.session_id:
        problems.append("the result names no session")
    else:
        smoke.reread(result.session_id)
    harness = adapter.harness.name
    if _repeated(await _lines(smoke.adapter, smoke.run_id), CONSTANT_LINES[harness]):
        problems.append("the transcript holds a line twice: a replay mirrored it again")
    if harness == "codex":
        smoke.first_token()
        if not (result.input_tokens and result.output_tokens):
            problems.append("the session reported no tokens")
    elif result.cost_usd is None:
        problems.append("the session reported no cost")
    return result, problems


async def _lines(adapter: E2bAdapter, run_id: str) -> list[str]:
    return [line async for line in adapter.logs(run_id)]


def _repeated(lines: list[str], constant: frozenset[tuple[str, str]]) -> bool:
    """Whether a translated line appears twice, other than one of the `constant` lines."""
    records = [(line, json.loads(line)) for line in lines]
    seen = [line for line, record in records if not any(record.get(k) == v for k, v in constant)]
    return len(seen) != len(set(seen))


# ------------------------------------------------------------------------- containment

INJECTED_HOST: Final = "httpbin.org"
INJECTED_HEADER: Final = "X-Factorial-Smoke"
DECLARED_PORTS: Final = (8080, 8081)
UNDECLARED_PORT: Final = 9090
#: What E2B's edge says, with a 502, of a port nothing is serving on from outside.
EDGE_CLOSED_PORT: Final = "port is not open"
#: Where the connection the cut probe opens goes, on loopback, owned by the agent.
PEER_PORT: Final = 9091
PROBE_DIR: Final = "/tmp/containment"
SWITCHED: Final = f"{PROBE_DIR}/switched"
CLIENT_LOG: Final = f"{PROBE_DIR}/client.log"
#: The longest the whole check may take, sandbox lifetime included.
SMOKE_LIFETIME_SECONDS: Final = 600

#: Opens a connection FROM a declared port while the table is open, then, once root has switched
#: to contain, sends on it: any answer but an error or an EOF means the switch left it alive.
_CLIENT_PY: Final = f"""import os, socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", {DECLARED_PORTS[-1]}))
s.connect(("127.0.0.1", {PEER_PORT}))
print("CONNECTED", flush=True)
while not os.path.exists("{SWITCHED}"):
    time.sleep(0.2)
s.settimeout(10)
try:
    s.sendall(b"ping")
    data = s.recv(16)
except socket.timeout:
    print("TIMEOUT", flush=True)
except OSError as exc:
    print("CUT", type(exc).__name__, flush=True)
else:
    print("CUT eof" if not data else "ALIVE " + repr(data), flush=True)
"""
_ECHO_PY: Final = f"""import socketserver
class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(64):
            self.request.sendall(data)
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("127.0.0.1", {PEER_PORT}), Echo).serve_forever()
"""


@dataclass(frozen=True, slots=True)
class ContainmentVerdict:
    """What the firewall held on a real build; `ok` only when every property did."""

    agent_injected: bool
    preview_blocked: bool
    preview_serves: bool
    undeclared_port_closed: bool
    declared_port_cut: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.agent_injected,
                self.preview_blocked,
                self.preview_serves,
                self.undeclared_port_closed,
                self.declared_port_cut,
            )
        )


Fetch = Callable[[str], Awaitable[tuple[int, str]]]


async def _public_fetch(url: str) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url)
    return response.status_code, response.text


async def _until(check: Callable[[], Awaitable[bool]], patience: float) -> bool:
    """`check` once, then each second until it holds or `patience` seconds are gone."""
    deadline = time.monotonic() + patience
    while True:
        if await check():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1)


async def _probe(sandbox: Sandbox, command: str, user: str = "root") -> str:
    out = await sandbox.run(command, user=user, timeout_seconds=60)
    log.info("[%s] %s -> exit %s: %s %s", user, command, out.exit_code, out.stdout, out.stderr)
    return out.stdout.strip()


def _verdict_line(log_text: str, word: str) -> bool:
    """Whether the client printed a line whose first word is exactly `word`."""
    return any(line.split(" ")[0] == word for line in log_text.splitlines())


def _egress_probe(marker: str) -> str:
    """Prints `<curl's exit> <how many lines carry the injected header>`."""
    return (
        f"out=$(curl -sS --max-time 10 https://{INJECTED_HOST}/headers); rc=$?; "
        f'echo "$rc $(printf %s "$out" | grep -c {INJECTED_HEADER})" # {marker}'
    )


async def _setup(sandbox: Sandbox) -> None:
    files = {path: text.encode() for path, text in SCRIPTS.items() if path == FIREWALL}
    files |= {
        f"{PROBE_DIR}/client.py": _CLIENT_PY.encode(),
        f"{PROBE_DIR}/echo.py": _ECHO_PY.encode(),
        f"{PROBE_DIR}/preview/index.html": b"preview-8080\n",
        f"{PROBE_DIR}/agent/index.html": b"agent-9090\n",
    }
    await sandbox.write_many(files)
    out = await sandbox.run(
        f"chmod 755 {FIREWALL} && chmod -R a+rX {PROBE_DIR} && chmod a+w {PROBE_DIR} && "
        f"(id {AGENT_USER} >/dev/null 2>&1 || useradd --create-home {AGENT_USER})"
    )
    if not out.ok:
        raise RuntimeError(f"setting the probe up failed: {out.stderr}")


async def _firewall(sandbox: Sandbox, mode: str) -> None:
    ports = ",".join(str(port) for port in DECLARED_PORTS)
    out = await sandbox.run(FIREWALL, envs={"MODE": mode, "PORTS": ports}, timeout_seconds=60)
    log.info("firewall %s -> exit %s %s", mode, out.exit_code, out.stderr)
    if not out.ok:
        raise RuntimeError(f"loading the firewall in {mode} mode failed: {out.stderr}")


async def containment_smoke(
    client: SandboxClient,
    template: str,
    *,
    fetch: Fetch = _public_fetch,
    patience: float = 30,
) -> ContainmentVerdict:
    """Whether the preview's uid stays off E2B's credential-injecting egress proxy on a real
    build of `template`, while its servers still answer and an undeclared port stays closed.
    One public sandbox, always killed. `fetch` is the public ingress fetch from this host."""
    network = NetworkPolicy(
        allow=(INJECTED_HOST,), inject={INJECTED_HOST: {INJECTED_HEADER: "1"}}, public=True
    )
    sandbox = await client.create(
        SandboxOptions(
            template=template,
            timeout_seconds=SMOKE_LIFETIME_SECONDS,
            metadata={"factorial_deployment": "smoke", "purpose": "containment"},
            network=network,
        )
    )
    try:
        return await _containment(sandbox, fetch, patience)
    finally:
        await client.kill(sandbox.sandbox_id)


async def _containment(sandbox: Sandbox, fetch: Fetch, patience: float) -> ContainmentVerdict:
    await _setup(sandbox)
    await _probe(sandbox, "sysctl fs.protected_regular # protected")
    # The table open, as before a credential is written: preview may open a connection from a
    # declared port, which the switch to contain must then cut.
    await _firewall(sandbox, "open")
    await sandbox.start(f"python3 {PROBE_DIR}/echo.py > {PROBE_DIR}/echo.log 2>&1", user=AGENT_USER)

    async def peer_listening() -> bool:
        listening = await _probe(sandbox, f'ss -Htln "sport = :{PEER_PORT}" # peer-listening')
        return bool(listening)

    if not await _until(peer_listening, patience):
        echo_log = await _probe(sandbox, f"cat {PROBE_DIR}/echo.log")
        raise RuntimeError(f"the probe's peer never listened: {echo_log}")
    await sandbox.start(f"python3 {PROBE_DIR}/client.py > {CLIENT_LOG} 2>&1", user=PREVIEW_USER)

    async def connected() -> bool:
        return "CONNECTED" in await _probe(sandbox, f"cat {CLIENT_LOG} # client-log")

    if not await _until(connected, patience):
        raise RuntimeError("the probe connection never opened while the table was open")
    await _firewall(sandbox, "contain")
    await sandbox.run(f"touch {SWITCHED}")

    async def settled() -> bool:
        log_text = await _probe(sandbox, f"cat {CLIENT_LOG} # client-log")
        return any(_verdict_line(log_text, word) for word in ("CUT", "ALIVE", "TIMEOUT"))

    await _until(settled, patience)
    cut = _verdict_line(await _probe(sandbox, f"cat {CLIENT_LOG} # client-log"), "CUT")

    agent = await _probe(sandbox, _egress_probe("agent-probe"), AGENT_USER)
    preview = await _probe(sandbox, _egress_probe("preview-probe"), PREVIEW_USER)
    agent_injected = agent == "0 1"
    # Reaching the host without the header would pass a count alone, so curl must fail too.
    preview_rc, _, preview_count = preview.partition(" ")
    preview_blocked = preview_rc not in ("", "0") and preview_count == "0"

    async def local(port: int, directory: str, user: str, marker: str, word: str) -> bool:
        await sandbox.start(
            f"cd {PROBE_DIR}/{directory} && exec python3 -m http.server {port}", user=user
        )

        async def answers() -> bool:
            out = await _probe(sandbox, f"curl -sS -m 5 http://127.0.0.1:{port}/ # {marker}")
            return word in out

        return await _until(answers, patience)

    preview_up = await local(
        DECLARED_PORTS[0], "preview", PREVIEW_USER, "preview-local", "preview-8080"
    )
    status, body = await fetch(f"https://{sandbox.host(DECLARED_PORTS[0])}/")
    log.info("public fetch of the declared port -> %s %r", status, body[:80])
    preview_serves = preview_up and status == 200 and "preview-8080" in body

    agent_up = await local(UNDECLARED_PORT, "agent", AGENT_USER, "agent-local", "agent-9090")
    status, body = await fetch(f"https://{sandbox.host(UNDECLARED_PORT)}/")
    log.info("public fetch of the undeclared port -> %s %r", status, body[:80])
    # A server that never came up would be closed for the wrong reason.
    closed = agent_up and status == 502 and EDGE_CLOSED_PORT in body

    return ContainmentVerdict(
        agent_injected=agent_injected,
        preview_blocked=preview_blocked,
        preview_serves=preview_serves,
        undeclared_port_closed=closed,
        declared_port_cut=cut,
    )
