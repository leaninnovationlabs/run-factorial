"""L2: the whole `claude_e2b` and `codex_e2b` adapters on the real claude and codex
TUIs, each sandbox a container of the local image in place of E2B's (`DockerSandboxClient`,
below), against `mock_anthropic.py` or `mock_openai.py` running inside it (decision 56). The
adapter's own root scripts drive tmux, the hooks and the CLI.

Opt-in, and never in CI: it needs Docker and an image with tmux, setpriv, git and both pinned
CLIs, codex's native binary at `/opt/codex/bin/codex`, which every container links where a codex
template links npm's (`CODEX_NATIVE`). It takes about four minutes, most of it the two smokes'
75 s holds and codex's WebSocket retries, about 6 s at a launch's first turn against a mock that
refuses the upgrade.

    make image && FACTORIAL_TUI_DOCKER_IMAGE=factorial:local \\
      uv run pytest -c pyproject.toml runtime/tests/test_e2b_tui_docker.py -q -p no:cacheprovider

No real key exists here: the run's key is a made-up one the egress rules would carry and the
sandbox never holds, the CLI is handed the usual placeholder, and the container has no network.
`octocat/Hello-World` is a bare repo in the container, which a system `insteadOf` names, so even
rework.sh's privilege-dropped git finds it. For claude it ships a `.claude/` hook that leaves a
mark when it runs, and for codex a `.codex/` that would leave a mark if codex took any of it up.

`DockerSandboxClient` keeps of E2B: commands run as the user asked, with the envs asked, and
their exit code is returned, never raised; `start` returns a pid that `pids` reports until the
process ends; a write lands as its user; a pause keeps the filesystem and nothing else (the
container is stopped, so a connect cold-boots it, its `boot` script run again as a template's
start command would be). It does not keep: envd's login shell (commands run under `bash -c`, so
the image's PATH holds); the network policy, recorded per sandbox and enforced nowhere (the
container has no network at all); E2B's timeouts and its cap; metadata beyond the container's
labels; and what a command still running sees when E2B pauses the sandbox under it (here
`docker stop` kills it, exit 137).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from runtime.adapters._cli.rework import REWORK_HEADER, REWORK_STASHED
from runtime.adapters.claude_cli.transcript import Transcript
from runtime.adapters.e2b import ClaudeE2bAdapter, CodexE2bAdapter, E2bAdapter
from runtime.adapters.e2b import smoke as smoke_module
from runtime.adapters.e2b.adapter import _read_state
from runtime.adapters.e2b.claude import PLACEHOLDER_API_KEY, ClaudeHarness
from runtime.adapters.e2b.client import (
    CommandOutput,
    NetworkPolicy,
    SandboxGone,
    SandboxInfo,
    SandboxOptions,
)
from runtime.adapters.e2b.codex import CONFIG_PATH, PLACEHOLDER, CodexHarness
from runtime.adapters.e2b.layout import (
    ARGV,
    CAPTURE,
    CODEX_NATIVE,
    ENV,
    ROLLOUT_DIR,
    SIGNALS,
    TMUX_SOCKET,
    WORKSPACE,
    session_log,
    turn_file,
)
from runtime.adapters.e2b.smoke import session_smoke_run, smoke_spec
from runtime.contract import (
    AgentPrompts,
    IdleReason,
    InspectPhase,
    ResumeFrom,
    RunSpec,
    RunStatus,
)

IMAGE = os.environ.get("FACTORIAL_TUI_DOCKER_IMAGE", "")

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason="set FACTORIAL_TUI_DOCKER_IMAGE to an image with the pinned CLIs, tmux and git",
)

# --------------------------------------------------------------------------- the Docker sandbox

DOCKER = "docker"
#: Every container the client makes carries it, so a teardown finds them all.
LABEL = "factorial-l2"
#: Where the image installs the native codex binary (Dockerfile).
IMAGE_CODEX = "/opt/codex/bin/codex"
#: Starts `$0` in a session of its own, detached from the exec, and prints its pid.
_START = 'setsid bash -c "$0" </dev/null >/dev/null 2>&1 & echo $!'
#: Unpacks a tar on stdin at `/`, every file its user's.
_UNTAR = ("tar", "-x", "--no-same-owner", "-C", "/")
#: Prints each pid of `$@` that is alive and not a zombie.
_ALIVE = (
    'for p in "$@"; do grep -qs "^State:[[:space:]]*[^Z]" /proc/$p/status && echo $p; done; true'
)


async def docker(
    *args: str, stdin: bytes | None = None, seconds: float | None = None
) -> CommandOutput:
    process = await asyncio.create_subprocess_exec(
        DOCKER,
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    code = process.returncode if process.returncode is not None else -1
    return CommandOutput(code, out.decode(errors="replace"), err.decode(errors="replace"))


def _exec(
    name: str, user: str, envs: Mapping[str, str] | None, *, stdin: bool = False
) -> list[str]:
    pairs = [f"--env={key}={value}" for key, value in (envs or {}).items()]
    return ["exec", *(["-i"] if stdin else []), "-u", user, "-w", "/", *pairs, name]


class DockerSandbox:
    def __init__(self, client: DockerSandboxClient, name: str) -> None:
        self._client = client
        self._name = name

    @property
    def sandbox_id(self) -> str:
        return self._name

    async def run(
        self,
        command: str,
        *,
        user: str = "root",
        envs: Mapping[str, str] | None = None,
        timeout_seconds: float = 120,
    ) -> CommandOutput:
        self._client.commands.append((self._name, command, dict(envs or {})))
        args = [*_exec(self._name, user, envs), "bash", "-c", command]
        return await docker(*args, seconds=timeout_seconds or None)

    async def start(
        self, command: str, *, user: str = "root", envs: Mapping[str, str] | None = None
    ) -> int:
        self._client.commands.append((self._name, command, dict(envs or {})))
        started = await docker(*_exec(self._name, user, envs), "bash", "-c", _START, command)
        if not started.ok:
            raise RuntimeError(f"starting {command} failed: {started.stderr.strip()}")
        pid = int(started.stdout.strip())
        self._client.started.setdefault(self._name, set()).add(pid)
        return pid

    async def pids(self) -> frozenset[int]:
        mine = sorted(self._client.started.get(self._name, ()))
        alive = await docker(
            *_exec(self._name, "root", None), "bash", "-c", _ALIVE, "-", *map(str, mine)
        )
        if not alive.ok:
            raise RuntimeError(f"listing processes failed: {alive.stderr.strip()}")
        return frozenset(int(pid) for pid in alive.stdout.split())

    async def write_many(self, files: Mapping[str, bytes], *, user: str = "root") -> None:
        # One tar of files alone, no dirs: tar makes the missing parents as `user`, and a file
        # that exists is replaced, as E2B's write does.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path, content in files.items():
                entry = tarfile.TarInfo(path.lstrip("/"))
                entry.size, entry.mode = len(content), 0o644
                archive.addfile(entry, io.BytesIO(content))
        args = [*_exec(self._name, user, None, stdin=True), *_UNTAR]
        written = await docker(*args, stdin=buffer.getvalue(), seconds=60)
        if not written.ok:
            raise RuntimeError(f"writing {', '.join(files)} failed: {written.stderr.strip()}")

    async def update_network(self, network: NetworkPolicy) -> None:
        self._client.networks.setdefault(self._name, []).append(network)

    def host(self, port: int) -> str:
        return f"{port}-{self._name}.docker.test"


@dataclass
class DockerSandboxClient:
    """Sandboxes as containers of `image`, the image's codex linked at `CODEX_NATIVE`. `prepare`
    runs as root once, at creation; `boot` runs as root at every start, creation's included, and
    must return once what it starts is up."""

    image: str
    prepare: str = ""
    boot: str = ""
    #: Root's files, written at creation before `prepare`.
    files: Mapping[str, bytes] = field(default_factory=dict)
    #: This client's containers carry `LABEL=<run>`.
    run: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: list[str] = field(default_factory=list)
    connects: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    #: Every policy each sandbox was given, its creation's first: what E2B would enforce.
    networks: dict[str, list[NetworkPolicy]] = field(default_factory=dict)
    #: (sandbox, command, envs) for every `run` and `start` through a sandbox handle.
    commands: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    #: The pids `start` returned, per sandbox, since its last boot.
    started: dict[str, set[int]] = field(default_factory=dict)

    async def create(self, options: SandboxOptions) -> DockerSandbox:
        name = f"factorial-l2-{self.run}-{len(self.created) + 1}"
        labels = {**options.metadata, LABEL: self.run}
        made = await docker(
            "run", "-d", "--init", "--network", "none", "--name", name,
            *[f"--label={key}={value}" for key, value in labels.items()],
            "--entrypoint", "sleep", self.image, "infinity",
        )  # fmt: skip
        if not made.ok:
            raise RuntimeError(f"docker run failed: {made.stderr.strip()}")
        self.created.append(name)
        self.networks[name] = [options.network]
        sandbox = DockerSandbox(self, name)
        await self._root(name, f"ln -s {IMAGE_CODEX} {CODEX_NATIVE}", "linking codex in")
        if self.files:
            await sandbox.write_many(self.files)
        if self.prepare:
            await self._root(name, self.prepare, "preparing")
        await self._boot(name)
        return sandbox

    async def connect(self, sandbox_id: str, *, timeout_seconds: int) -> DockerSandbox:
        info = await self.info(sandbox_id)
        if info is None:
            raise SandboxGone(sandbox_id)
        self.connects.append(sandbox_id)
        if info.state != "running":
            started = await docker("start", sandbox_id)
            if not started.ok:
                raise RuntimeError(f"docker start failed: {started.stderr.strip()}")
            await self._boot(sandbox_id)
        return DockerSandbox(self, sandbox_id)

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        found = await docker("inspect", "--format", "{{json .}}", sandbox_id)
        if not found.ok:
            return None
        container = json.loads(found.stdout)
        labels = dict(container["Config"].get("Labels") or {})
        labels.pop(LABEL, None)
        state = "running" if container["State"]["Running"] else "paused"
        return SandboxInfo(sandbox_id=sandbox_id, state=state, metadata=labels)

    async def pause(self, sandbox_id: str) -> None:
        stopped = await docker("stop", "-t", "1", sandbox_id)
        if not stopped.ok:
            raise RuntimeError(f"docker stop failed: {stopped.stderr.strip()}")
        self.paused.append(sandbox_id)

    async def kill(self, sandbox_id: str) -> None:
        await docker("rm", "-f", sandbox_id)

    async def list(self, metadata: Mapping[str, str]) -> Sequence[SandboxInfo]:
        filters = [f"--filter=label={k}={v}" for k, v in {**metadata, LABEL: self.run}.items()]
        names = await docker("ps", "-a", "--format", "{{.Names}}", *filters)
        found = [await self.info(name) for name in names.stdout.split()]
        return [info for info in found if info is not None]

    async def check(self) -> None:
        return None

    async def session_verified(self, pin: str) -> bool:
        return True

    async def sh(self, sandbox_id: str, command: str, *, user: str = "root") -> CommandOutput:
        """The test's own look inside, counted as nothing the adapter did. A parked sandbox is
        woken for it, as a connect would."""
        info = await self.info(sandbox_id)
        if info is not None and info.state != "running":
            await docker("start", sandbox_id)
            await self._boot(sandbox_id)
        return await docker(*_exec(sandbox_id, user, None), "bash", "-c", command, seconds=60)

    async def close(self) -> None:
        """Remove every container this client made."""
        names = await docker("ps", "-aq", f"--filter=label={LABEL}={self.run}")
        if names.stdout.split():
            await docker("rm", "-f", *names.stdout.split())

    async def _boot(self, name: str) -> None:
        self.started[name] = set()
        if self.boot:
            await self._root(name, self.boot, "booting")

    async def _root(self, name: str, script: str, what: str) -> None:
        done = await docker(*_exec(name, "root", None), "bash", "-c", script, seconds=120)
        if not done.ok:
            raise RuntimeError(f"{what} {name} failed ({done.exit_code}): {done.stderr.strip()}")


# --------------------------------------------------------------------------- the tests

#: The run's model key: what the egress rules would carry, and so nowhere in the sandbox.
KEY = "sk-ant-l2-madeUpKeyThatMustNeverEnterTheSandbox"
CODEX_KEY = "sk-proj-l2-madeUpKeyThatMustNeverEnterTheSandbox"
KEYS = {"claude": KEY, "codex": CODEX_KEY}
MOCKS = {"claude": "mock_anthropic.py", "codex": "mock_openai.py"}
MOCK_DIR = "/opt/l2"
MOCK_LOG = f"{MOCK_DIR}/requests.jsonl"
MOCK_URL = "http://127.0.0.1:18080"
TURN_WAIT = timedelta(seconds=90)
POLL = 0.5
REPO_URL = "https://github.com/octocat/Hello-World.git"
BRANCH = "smoke/l2-rework"
TMUX = f"tmux -u -N -S {TMUX_SOCKET} -f /opt/factorial/tmux.conf"

#: Once per container: the repo every run clones and pushes to, named by the URL the spec's.
PREPARE = f"""set -eu
git init -q --bare -b master /srv/repo.git
seed=$(mktemp -d)
git -C "$seed" init -q -b master
echo hello > "$seed/README"
git -C "$seed" add README
git -C "$seed" -c user.name=l2 -c user.email=l2@example.com commit -q -m init
git -C "$seed" push -q /srv/repo.git master
chmod -R a+rwX /srv/repo.git
git config --system url.file:///srv/repo.git.insteadOf {REPO_URL}
git config --system safe.directory '*'
"""
#: What a target repo may ship in `.codex/`: a hook in each file that holds one, an MCP server and
#: developer instructions. Each leaves a mark if codex takes it up: a file, or `REPO_MARK` in a
#: request.
REPO_MARKS = "/tmp/l2-repo-"
REPO_MARK = f"REPO-{uuid.uuid4().hex[:8]}"
REPO_JSON_HOOK = f"touch {REPO_MARKS}json"
CODEX_REPO = f"""set -eu
seed=$(mktemp -d)
git clone -q /srv/repo.git "$seed"
mkdir "$seed/.codex"
cat > "$seed/.codex/config.toml" <<'TOML'
developer_instructions = "{REPO_MARK}"

[mcp_servers.x]
command = "sh"
args = ["-c", "touch {REPO_MARKS}mcp"]

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "touch {REPO_MARKS}hook"
TOML
cat > "$seed/.codex/hooks.json" <<'JSON'
{json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": REPO_JSON_HOOK}]}]}})}
JSON
git -C "$seed" add .codex
git -C "$seed" -c user.name=l2 -c user.email=l2@example.com commit -q -m codex
git -C "$seed" push -q origin master
chmod -R a+rwX /srv/repo.git
"""
#: What a target repo may ship in `.claude/`: a SessionStart hook that leaves a mark when it runs.
CLAUDE_REPO_MARK = f"{REPO_MARKS}claude-start"
CLAUDE_REPO_HOOK = {"type": "command", "command": f"touch {CLAUDE_REPO_MARK}"}
CLAUDE_REPO = f"""set -eu
seed=$(mktemp -d)
git clone -q /srv/repo.git "$seed"
mkdir "$seed/.claude"
cat > "$seed/.claude/settings.json" <<'JSON'
{json.dumps({"hooks": {"SessionStart": [{"hooks": [CLAUDE_REPO_HOOK]}]}})}
JSON
git -C "$seed" add .claude
git -C "$seed" -c user.name=l2 -c user.email=l2@example.com commit -q -m claude
git -C "$seed" push -q origin master
chmod -R a+rwX /srv/repo.git
"""
#: Each harness's container preparation: the repo, with its `.claude/` or its `.codex/`.
PREPARES = {"claude": PREPARE + CLAUDE_REPO, "codex": PREPARE + CODEX_REPO}


def boot(mock: str) -> str:
    """At every boot: the mock API, up before anything asks it."""
    return f"""set -eu
rm -f {MOCK_DIR}/port
setsid python3 {MOCK_DIR}/{mock} {MOCK_LOG} 18080 \\
  >{MOCK_DIR}/port 2>>{MOCK_DIR}/mock.err </dev/null &
for _ in $(seq 1 100); do [ -s {MOCK_DIR}/port ] && exit 0; sleep 0.05; done
exit 1
"""


class _MockedClaude(ClaudeHarness):
    """The harness, its CLI pointed at the mock."""

    def _tui_env(self, auth_mode: str, *, signals: str = SIGNALS) -> dict[str, str]:
        return {**super()._tui_env(auth_mode, signals=signals), "ANTHROPIC_BASE_URL": MOCK_URL}


class _Adapter(ClaudeE2bAdapter):
    harness = _MockedClaude()


class _MockedCodex(CodexHarness):
    """The harness, its CLI pointed at the mock. codex reads no base URL from its environment,
    and a key must come before the document's first table to be a top-level one."""

    def session_seeds(self, *args: Any, **kwargs: Any) -> dict[str, tuple[bytes, str, int]]:
        seeds = super().session_seeds(*args, **kwargs)
        config, owner, mode = seeds[CONFIG_PATH]
        url = f'openai_base_url = "{MOCK_URL}/v1"\n'.encode()
        return {**seeds, CONFIG_PATH: (url + config, owner, mode)}


class _CodexAdapter(CodexE2bAdapter):
    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.harness = _MockedCodex()


ADAPTERS: dict[str, type[E2bAdapter]] = {"claude": _Adapter, "codex": _CodexAdapter}


@pytest.fixture
def harness() -> str:
    """The CLI a test drives; a test parametrises it to drive another."""
    return "claude"


@pytest.fixture
async def client(harness: str) -> AsyncIterator[DockerSandboxClient]:
    mock = MOCKS[harness]
    docker = DockerSandboxClient(
        IMAGE,
        prepare=PREPARES[harness],
        boot=boot(mock),
        files={f"{MOCK_DIR}/{mock}": (Path(__file__).parent / mock).read_bytes()},
    )
    try:
        yield docker
    finally:
        await docker.close()


def adapter_on(
    root: Path, client: DockerSandboxClient, harness: str = "claude"
) -> Callable[[], E2bAdapter]:
    """A factory of adapters on one state root: each call is a worker (re)started."""

    def fresh() -> E2bAdapter:
        return ADAPTERS[harness](
            client=client,
            template="factorial-l2:local",
            state_root=root,
            deployment="l2",
            poll_interval=POLL,
        )

    return fresh


def spec(instructions: str | None = None, harness: str = "claude", **update: Any) -> RunSpec:
    smoke = smoke_spec(harness, KEYS[harness])
    if instructions is not None:
        prompts = AgentPrompts(system=smoke.prompts.system, instructions=instructions)
        update["prompts"] = prompts
    return smoke.model_copy(update=update)


async def at_prompt(adapter: E2bAdapter, run_id: str, reason: IdleReason) -> None:
    async with asyncio.timeout(TURN_WAIT.total_seconds()):
        while True:
            status = await adapter.status(run_id)
            assert not status.is_terminal, f"run {run_id} ended {status}"
            idle = status is RunStatus.IDLE
            if idle and (await adapter.session_state(run_id)).idle_reason is reason:
                return
            await asyncio.sleep(POLL)


async def in_box(client: DockerSandboxClient, sandbox: str, script: str, user: str = "root") -> str:
    done = await client.sh(sandbox, script, user=user)
    assert done.ok, f"{script!r} failed ({done.exit_code}): {done.stderr}"
    return done.stdout


async def mock_requests(
    client: DockerSandboxClient, sandbox: str, path: str = "/v1/messages"
) -> list[dict[str, Any]]:
    lines = (await in_box(client, sandbox, f"cat {MOCK_LOG}")).splitlines()
    return [r for r in map(json.loads, lines) if r["path"].startswith(path)]


def run_dir(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


def hook_events(root: Path, run_id: str) -> list[str]:
    lines = (run_dir(root, run_id) / "signals.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line)["hook"].get("hook_event_name", "") for line in lines]


def translated(root: Path, run_id: str) -> list[dict[str, Any]]:
    lines = (run_dir(root, run_id) / "stdout.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def reread(root: Path, run_id: str, session_id: str) -> Transcript:
    """The run's raw session log through a translator of its own, end to end."""
    translator = Transcript(session_id, turn=True)
    for line in (run_dir(root, run_id) / "session.jsonl").read_text("utf-8").splitlines():
        translator.feed(line)
    translator.end_segment()
    return translator


async def test_the_session_smoke_passes_on_the_real_tui_in_a_linux_sandbox(
    tmp_path: Path, client: DockerSandboxClient
) -> None:
    """Spawn, the screen, the hold, a worker restart, a rollover and its resume at the prompt,
    and the finish's `/exit`: the smoke the template builder runs, step for step."""
    fresh = adapter_on(tmp_path, client)

    result, problems = await session_smoke_run(
        fresh(), spec(), turn_timeout=TURN_WAIT, poll=POLL, reattach=fresh
    )

    assert problems == []
    assert (result.outcome, result.stream_warnings) == (RunStatus.SUCCEEDED, ())
    [sandbox] = client.created
    assert client.connects == [sandbox, sandbox], "the restart found it, the resume woke it"
    assert client.paused == [sandbox, sandbox], "parked at the rollover and at the finish"
    sid = result.session_id
    assert sid is not None
    log_dir = session_log(sid).rsplit("/", 1)[0]
    logs = await in_box(client, sandbox, f"cd {log_dir} && ls *.jsonl", user="agent")
    assert logs.split() == [f"{sid}.jsonl"], "the resume continued the same session"
    # The key rides only the egress rules E2B would enforce, and leaves them at each park; the
    # CLI holds the placeholder.
    injected = [n.inject.get("api.anthropic.com") for n in client.networks[sandbox]]
    assert injected == [{"x-api-key": KEY}, None, {"x-api-key": KEY}, None]
    keys = {r["x-api-key"] for r in await mock_requests(client, sandbox)}
    assert keys == {PLACEHOLDER_API_KEY}
    leaked = await client.sh(sandbox, f"grep -rlsF -- '{KEY}' /home /opt /root /srv /tmp /etc /var")
    assert leaked.exit_code == 1, f"the key is in the sandbox: {leaked.stdout}"


async def test_a_question_idles_the_session_and_the_finish_after_its_answer_exits_cleanly(
    tmp_path: Path, client: DockerSandboxClient
) -> None:
    """The finish types `/exit` through finish.sh's bracketed paste into the real TUI: the CLI
    leaves by itself, exit 0, and the paste fires no prompt hook that would void the finish."""
    adapter = adapter_on(tmp_path, client)()
    run_id = (await adapter.spawn(spec("ASKME about the scope."))).run_id
    await at_prompt(adapter, run_id, IdleReason.QUESTION)
    asked = await adapter.session_state(run_id)
    assert asked.question == "Include archived rows? [Active only | Active and archived]"
    assert asked.last_input_at is None
    [sandbox] = client.created

    # A human at the terminal takes the first option.
    async with asyncio.timeout(30):
        while True:
            if "Active and archived" in await in_box(client, sandbox, CAPTURE):
                break
            await asyncio.sleep(POLL)
    await in_box(client, sandbox, f"{TMUX} send-keys -t =run: Enter")
    await at_prompt(adapter, run_id, IdleReason.TURN_END)
    assert (await adapter.session_state(run_id)).last_input_at is not None

    await adapter.finish(run_id)
    result = await adapter.result(run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.SUCCEEDED, None)
    assert result.stream_warnings == ()
    assert result.cost_usd is not None and result.cost_usd > 0
    state = _read_state(run_dir(tmp_path, run_id), run_id)
    assert state.exit_code == 0, "the CLI left by the `/exit`, not by the hangup after it"
    events = hook_events(tmp_path, run_id)
    last_stop = len(events) - 1 - events[::-1].index("Stop")
    assert events[last_stop + 1 :] == ["SessionEnd"], events
    answered = [line for line in translated(tmp_path, run_id) if "tool_use_result" in line]
    assert [line["tool_use_result"] for line in answered] == [
        {"answers": {"Include archived rows?": "Active only"}}
    ]
    assert result.session_id is not None
    assert reread(tmp_path, run_id, result.session_id).missing() == ()


@pytest.mark.parametrize("harness", ["claude", "codex"])
async def test_the_agent_cannot_reach_its_own_tmux_socket(
    tmp_path: Path, client: DockerSandboxClient, harness: str
) -> None:
    adapter = adapter_on(tmp_path, client, harness)()
    run_id = (await adapter.spawn(spec(harness=harness))).run_id
    try:
        await at_prompt(adapter, run_id, IdleReason.TURN_END)
        [sandbox] = client.created
        for attempt in (f"{TMUX} ls", f"cat {ARGV}", f"cat {ENV}"):
            done = await client.sh(sandbox, attempt, user="agent")
            assert not done.ok, f"the agent ran {attempt!r}: {done.stdout}"
        assert "run" in await in_box(client, sandbox, f"{TMUX} ls"), "root still can"
    finally:
        await adapter.cancel(run_id)


async def test_a_rework_adopts_the_session_in_docker(
    tmp_path: Path, client: DockerSandboxClient
) -> None:
    """Run A's kept sandbox is woken for run B: the checkout reset to the pull request's head,
    the agent's draft stashed, and the session resumed on B's turn from where A's log ended."""
    adapter = adapter_on(tmp_path, client)()
    first = (await adapter.spawn(spec())).run_id
    await at_prompt(adapter, first, IdleReason.TURN_END)
    [sandbox] = client.created
    # The agent's work: a branch pushed, a draft left uncommitted. Then a reviewer's commit.
    await in_box(
        client,
        sandbox,
        f"cd {WORKSPACE} && git checkout -q -b {BRANCH} && "
        "git -c user.name=agent -c user.email=agent@example.com commit -q --allow-empty -m work "
        f"&& git push -q origin {BRANCH} && echo draft > DRAFT.md",
        user="agent",
    )
    head = await in_box(
        client,
        sandbox,
        f"git clone -q {REPO_URL} /tmp/review && cd /tmp/review && git checkout -q {BRANCH} && "
        "git -c user.name=r -c user.email=r@example.com commit -q --allow-empty -m fix && "
        f"git push -q origin {BRANCH} && git rev-parse HEAD",
    )
    await adapter.finish(first)
    a = await adapter.result(first)
    assert a.outcome is RunStatus.SUCCEEDED and a.session_id is not None
    a_raw = (run_dir(tmp_path, first) / "session.jsonl").read_bytes()
    a_messages = {
        line["message"]["id"] for line in translated(tmp_path, first) if line["type"] == "assistant"
    }
    assert a_messages and client.paused == [sandbox]

    rework = spec(resume_from=ResumeFrom(run_id=first, session_id=a.session_id))
    second = (await adapter.spawn(rework)).run_id

    session = await adapter.session_state(second)
    assert (session.session_id, session.log_offset) == (a.session_id, len(a_raw))
    assert len(a_raw) > 0 and client.created == [sandbox]
    turn = await in_box(client, sandbox, f"cat {turn_file(1)}", user="agent")
    assert turn.startswith(REWORK_HEADER.format(branch=BRANCH, head=head.strip()))
    assert REWORK_STASHED.split("`")[0] in turn
    checkout = await in_box(
        client, sandbox, f"cd {WORKSPACE} && git rev-parse HEAD && git stash list", user="agent"
    )
    assert checkout.startswith(head) and f"before the rework by run {second}" in checkout
    await at_prompt(adapter, second, IdleReason.TURN_END)
    sent = json.dumps(await mock_requests(client, sandbox))
    assert "Changes were requested on your pull request" in sent, "the turn reached the CLI"
    whole = await in_box(client, sandbox, f"cat {session_log(a.session_id)}", user="agent")
    assert whole.encode().startswith(a_raw) and len(whole.encode()) > len(a_raw)

    await adapter.finish(second)
    b = await adapter.result(second)
    assert (b.outcome, b.session_id, b.stream_warnings) == (
        RunStatus.SUCCEEDED,
        a.session_id,
        (),
    )
    b_lines = translated(tmp_path, second)
    assert not [line for line in b_lines if line.get("message", {}).get("id") in a_messages]
    assert [line["type"] for line in b_lines].count("result") == 1


# --------------------------------------------------------------------------- codex


def finding(name: str, value: object) -> None:
    """What a step observed rather than asserted, shown with `-s`."""
    print(f"FINDING {name}: {json.dumps(value, default=str)}", file=sys.stderr)


def rollout_records(root: Path, run_id: str) -> list[dict[str, Any]]:
    lines = (run_dir(root, run_id) / "session.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("harness", ["codex"])
async def test_the_codex_session_smoke_passes_on_the_real_tui_in_a_linux_sandbox(
    tmp_path: Path, client: DockerSandboxClient, harness: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The smoke the template builder runs, on Linux's native codex: its folder consent taken at
    each launch, its thread found from the rollout it begins, its turnless resume idled with no
    hook, and the finish's Ctrl-C."""
    fresh = adapter_on(tmp_path, client, harness)

    with caplog.at_level(logging.INFO, logger=smoke_module.__name__):
        result, problems = await session_smoke_run(
            fresh(), spec(harness=harness), turn_timeout=TURN_WAIT, poll=POLL, reattach=fresh
        )

    assert problems == []
    assert (result.outcome, result.stream_warnings, result.cost_usd) == (
        RunStatus.SUCCEEDED,
        (),
        None,
    )
    assert result.input_tokens and result.output_tokens
    [sandbox] = client.created
    assert client.connects == [sandbox, sandbox], "the restart found it, the resume woke it"
    assert client.paused == [sandbox, sandbox], "parked at the rollover and at the finish"
    state = _read_state(run_dir(tmp_path, result.run_id), result.run_id)
    assert state.exit_code == 0, "codex left by the finish's Ctrl-C, not by the hangup after it"
    found = await in_box(client, sandbox, f"find {ROLLOUT_DIR} -type f", user="agent")
    assert found.split() == [state.session_log], "the resume continued the same rollout"
    # No hook fires on a turnless resume until a human types: the consent idled it.
    events = hook_events(tmp_path, result.run_id)
    assert events == ["SessionStart", "UserPromptSubmit", "Stop"], events
    injected = [n.inject.get("api.openai.com") for n in client.networks[sandbox]]
    assert injected == [{"Authorization": f"Bearer {CODEX_KEY}"}, None] * 2
    sent = await mock_requests(client, sandbox, "/v1/responses")
    assert {r["authorization"] for r in sent} == {f"Bearer {PLACEHOLDER}"}
    leaked = await client.sh(
        sandbox, f"grep -rlsF -- '{CODEX_KEY}' /home /opt /root /srv /tmp /etc /var"
    )
    assert leaked.exit_code == 1, f"the key is in the sandbox: {leaked.stdout}"

    finding("WebSocket attempts", len([r for r in sent if r["upgrade"] == "websocket"]))
    finding("the smoke's first-token note", [r.getMessage() for r in caplog.records])
    errors = [
        r["payload"] for r in rollout_records(tmp_path, result.run_id)
        if r.get("type") == "event_msg" and r["payload"].get("type") == "error"
    ]  # fmt: skip
    finding("event_msg error payloads", errors)


@pytest.mark.parametrize("harness", ["codex"])
async def test_a_repo_shipped_codex_hook_neither_blocks_nor_runs_in_the_sandbox(
    tmp_path: Path, client: DockerSandboxClient, harness: str
) -> None:
    """The checkout ships `.codex/` with two hooks, an MCP server and developer instructions: the
    untrusted seed leaves all of it unloaded, and the consent keeps the trust as seeded. The
    positive control is L1's: `test_codex_tui_local.py::test_headless_exec_with_a_repo_codex_config`
    shows the same fixture loading with no trust seed, and seeding `trusted` here meets "Hooks need
    review"."""
    adapter = adapter_on(tmp_path, client, harness)()
    run_id = (await adapter.spawn(spec(harness=harness))).run_id
    try:
        await at_prompt(adapter, run_id, IdleReason.TURN_END)
        [sandbox] = client.created
        shipped = await in_box(client, sandbox, f"ls {WORKSPACE}/.codex", user="agent")
        assert shipped.split() == ["config.toml", "hooks.json"]
        screen = await in_box(client, sandbox, CAPTURE)
        assert "Hooks need review" not in screen, screen
        marks = await client.sh(sandbox, f"ls {REPO_MARKS}*")
        assert marks.exit_code != 0, f"a repo hook or MCP server ran: {marks.stdout}"
        sent = json.dumps(await mock_requests(client, sandbox, "/v1/responses"))
        assert REPO_MARK not in sent, "the repo's developer instructions reached the model"
        seeded = await in_box(client, sandbox, f"cat {CONFIG_PATH}", user="agent")
        assert 'trust_level = "untrusted"' in seeded and '"trusted"' not in seeded
    finally:
        await adapter.cancel(run_id)


@pytest.mark.parametrize("harness", ["claude"])
async def test_a_repo_shipped_claude_hook_never_runs_in_a_session_view(
    tmp_path: Path, client: DockerSandboxClient, harness: str
) -> None:
    """The live session takes the checkout's `.claude/settings.json` (the positive control: the
    agent's own repo, which its own session may run). The read-only view of that session, which
    holds the model key in a sandbox a preview may make public, must not."""
    adapter = adapter_on(tmp_path, client, harness)()
    run_id = (await adapter.spawn(spec(harness=harness))).run_id
    await at_prompt(adapter, run_id, IdleReason.TURN_END)
    [sandbox] = client.created
    assert (await client.sh(sandbox, f"test -e {CLAUDE_REPO_MARK}")).exit_code == 0, (
        "the positive control failed: the live session did not run the repo's hook"
    )
    await adapter.finish(run_id)
    await adapter.drain()
    await client.sh(sandbox, f"rm -f {CLAUDE_REPO_MARK}")

    await adapter.inspect_start(
        run_id,
        token="v1",
        limit=timedelta(minutes=5),
        credentials={"ANTHROPIC_API_KEY": SecretStr(KEY)},
    )
    try:
        phase = None
        for _ in range(60):
            phase = (await adapter.inspect_status(run_id)).phase
            if phase is InspectPhase.OPEN:
                break
            await asyncio.sleep(0.5)
        assert phase is InspectPhase.OPEN, "the view never opened, so nothing below proves it"
        await asyncio.sleep(3)  # a SessionStart hook runs as the view's CLI comes up
        assert (await adapter.inspect_status(run_id)).phase is InspectPhase.OPEN, (
            "the view's CLI exited"
        )
        marks = await client.sh(sandbox, f"test -e {CLAUDE_REPO_MARK}")
        assert marks.exit_code != 0, "the session view ran a hook the checkout ships"
    finally:
        await adapter.inspect_stop(run_id)


@pytest.mark.parametrize("harness", ["codex"])
async def test_a_codex_rework_adopts_the_session_in_docker(
    tmp_path: Path, client: DockerSandboxClient, harness: str
) -> None:
    """Run A's kept sandbox is woken for run B: the checkout reset to the pull request's head,
    the agent's draft stashed, and codex resumed on its thread with B's turn file, reading the
    rollout from where A's mirror ended."""
    adapter = adapter_on(tmp_path, client, harness)()
    first = (await adapter.spawn(spec(harness=harness))).run_id
    await at_prompt(adapter, first, IdleReason.TURN_END)
    [sandbox] = client.created
    await in_box(
        client,
        sandbox,
        f"cd {WORKSPACE} && git checkout -q -b {BRANCH} && "
        "git -c user.name=agent -c user.email=agent@example.com commit -q --allow-empty -m work "
        f"&& git push -q origin {BRANCH} && echo draft > DRAFT.md",
        user="agent",
    )
    head = await in_box(
        client,
        sandbox,
        f"git clone -q {REPO_URL} /tmp/review && cd /tmp/review && git checkout -q {BRANCH} && "
        "git -c user.name=r -c user.email=r@example.com commit -q --allow-empty -m fix && "
        f"git push -q origin {BRANCH} && git rev-parse HEAD",
    )
    await adapter.finish(first)
    a = await adapter.result(first)
    assert a.outcome is RunStatus.SUCCEEDED and a.session_id is not None
    rollout = _read_state(run_dir(tmp_path, first), first).session_log
    assert rollout is not None
    a_raw = (run_dir(tmp_path, first) / "session.jsonl").read_bytes()
    assert a_raw and client.paused == [sandbox]

    rework = spec(harness=harness, resume_from=ResumeFrom(run_id=first, session_id=a.session_id))
    second = (await adapter.spawn(rework)).run_id

    session = await adapter.session_state(second)
    assert (session.session_id, session.log_offset) == (a.session_id, len(a_raw))
    assert _read_state(run_dir(tmp_path, second), second).session_log == rollout
    assert client.created == [sandbox]
    turn = await in_box(client, sandbox, f"cat {turn_file(1)}", user="agent")
    assert turn.startswith(REWORK_HEADER.format(branch=BRANCH, head=head.strip()))
    assert REWORK_STASHED.split("`")[0] in turn
    checkout = await in_box(
        client, sandbox, f"cd {WORKSPACE} && git rev-parse HEAD && git stash list", user="agent"
    )
    assert checkout.startswith(head) and f"before the rework by run {second}" in checkout
    await at_prompt(adapter, second, IdleReason.TURN_END)
    sent = json.dumps(await mock_requests(client, sandbox, "/v1/responses"))
    assert f"Read {turn_file(1)} and do what it says." in sent, "the turn reached the CLI"
    whole = await in_box(client, sandbox, f"cat {rollout}", user="agent")
    assert whole.encode().startswith(a_raw) and len(whole.encode()) > len(a_raw)

    await adapter.finish(second)
    b = await adapter.result(second)
    assert (b.outcome, b.session_id, b.stream_warnings) == (RunStatus.SUCCEEDED, a.session_id, ())
    assert b.input_tokens and b.output_tokens
    b_lines = translated(tmp_path, second)
    assert [line["type"] for line in b_lines].count("turn.started") == 1, b_lines
