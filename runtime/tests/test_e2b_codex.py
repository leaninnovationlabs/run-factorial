"""Codex on E2B: what differs from Claude is the login file and codex's own sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from fake_e2b import (
    CODEX_THREAD,
    REWORK_HEAD,
    FakeSandboxClient,
    Script,
    TuiScript,
    codex_turn,
    rollout_path,
)
from pydantic import SecretStr

from runtime.adapters._cli.rework import rework_header
from runtime.adapters.codex_cli.cli import CONSENT_RESTRICTED, HOOK_LABELS
from runtime.adapters.codex_cli.stream import StreamReader
from runtime.adapters.codex_cli.transcript import RolloutTranscript
from runtime.adapters.codex_cli.tui import hook_hash, turn_text
from runtime.adapters.e2b import CodexE2bAdapter, E2bAdapter
from runtime.adapters.e2b.adapter import _RunState, _write_state
from runtime.adapters.e2b.client import NetworkPolicy, SandboxOptions
from runtime.adapters.e2b.codex import AUTH_PATH, CONFIG_PATH, SIGNAL_COMMAND, CodexHarness
from runtime.adapters.e2b.harness import Harness, Launch, attachment_paths
from runtime.adapters.e2b.layout import (
    ARGV,
    ATTACHMENTS_DIR,
    CAPTURE,
    CODEX_NATIVE,
    CONFIG_DIR,
    CONSENT,
    ROLLOUT_FIND,
    SCRUB,
    SESSION,
    SESSION_POLL,
    SIGNAL,
    SIGNALS,
    WORKSPACE,
    read_session_poll,
    turn_file,
)
from runtime.adapters.e2b.template import definition, link_native
from runtime.contract import (
    AgentPrompts,
    McpServerSpec,
    McpTransport,
    ResumeFrom,
    RunAttachment,
    RunSpec,
    RunStatus,
    Workspace,
)

TOKEN = "ghs_codexE2bTokenMustNeverEnterTheSandbox"  # gitleaks:allow
API_KEY = "sk-proj-codexE2bKeyMustNeverEnterTheSandbox"
LOGIN = '{"tokens": {"refresh_token": "rt-codexE2bLoginMustBeScrubbedOnPause"}}'
MCP_TOKEN = "mcp-codexE2bBearerMustNeverEnterTheSandbox"
THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
ORIGIN = "https://github.com/user-attachments/assets/1b92ad15-13e5-4ffe-ac3a-b0c19487acfa"


def attachment(name: str = "image-1", content: bytes = PNG) -> RunAttachment:
    return RunAttachment(
        name=name,
        media_type="image/png",
        content=content,
        source="ticket_body",
        origin=ORIGIN,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "impl-agent",
        "ticket": "GH-9",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="impl/9-{slug}"),
        "prompts": AgentPrompts(system="You are the impl agent."),
        "secrets": {
            "GH_TOKEN": SecretStr(TOKEN),
            "OPENAI_API_KEY": SecretStr(API_KEY),
            "CODEX_AUTH_JSON": SecretStr(LOGIN),
            "DOCS_TOKEN": SecretStr(MCP_TOKEN),
        },
        "model": "gpt-5.1-codex",
    }
    return RunSpec.model_validate(defaults | overrides)


def adapter(
    tmp_path: Path, client: FakeSandboxClient, auth_mode: str = "api_key"
) -> CodexE2bAdapter:
    return CodexE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "state",
        deployment="test",
        auth_mode=auth_mode,
        client=client,
        now=lambda: datetime(2026, 9, 23, tzinfo=UTC),
        poll_interval=0,
    )


def at_its_prompt() -> Script:
    """A codex session's first turn behind its folder consent; then its prompt."""
    return Script(tui=TuiScript(steps=[codex_turn(meta=True)], consent=True))


async def idled(codex: CodexE2bAdapter, run_id: str) -> None:
    for _ in range(10_000):
        if await codex.status(run_id) is RunStatus.IDLE:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"run {run_id} never idled")


async def test_an_api_key_run_holds_only_a_placeholder_login(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[at_its_prompt()])
    codex = adapter(tmp_path, client)
    handle = await codex.spawn(session_spec(attachments=()))
    await idled(codex, handle.run_id)
    await codex.finish(handle.run_id)
    await codex.drain()

    sandbox = client.only()
    assert await codex.status(handle.run_id) is RunStatus.SUCCEEDED
    seen = "\n".join(content.decode() for content, _ in sandbox.files.values())
    for secret in (TOKEN, API_KEY, MCP_TOKEN):
        assert secret not in seen
    assert json.loads(sandbox.files[AUTH_PATH][0])["auth_mode"] == "apikey"
    inject = sandbox.options.network.inject
    assert inject["api.openai.com"] == {"Authorization": f"Bearer {API_KEY}"}
    assert inject["mcp.example.com"] == {"Authorization": f"Bearer {MCP_TOKEN}"}


async def test_a_subscription_login_is_scrubbed_on_pause_and_written_again_on_resume(
    tmp_path: Path,
) -> None:
    """A login file is the one credential a sandbox holds, so it must not ride in a snapshot."""
    client = FakeSandboxClient(scripts=[at_its_prompt(), Script(tui=TuiScript(consent=True))])
    codex = adapter(tmp_path, client, auth_mode="subscription")
    handle = await codex.spawn(session_spec(mcp=()))
    await idled(codex, handle.run_id)
    await codex.interrupt(handle.run_id)

    sandbox = client.only()
    scrub = next(c.command for c in sandbox.commands if c.command.startswith(SCRUB))
    assert AUTH_PATH in scrub

    fresh = '{"tokens": {"refresh_token": "rt-fresh"}}'
    try:
        await codex.resume(handle.run_id, None, {"CODEX_AUTH_JSON": SecretStr(fresh)})
        assert sandbox.files[AUTH_PATH][0].decode() == fresh
    finally:
        await codex.cancel(handle.run_id)
        await codex.drain()


async def test_a_resumed_codex_session_adds_its_turns_tokens(tmp_path: Path) -> None:
    client = FakeSandboxClient(
        scripts=[at_its_prompt(), Script(tui=TuiScript(steps=[codex_turn()], consent=True))]
    )
    codex = adapter(tmp_path, client)
    handle = await codex.spawn(session_spec(mcp=()))
    await idled(codex, handle.run_id)
    await codex.interrupt(handle.run_id)
    await codex.resume(handle.run_id, "go on", {"OPENAI_API_KEY": SecretStr(API_KEY)})
    await idled(codex, handle.run_id)
    await codex.finish(handle.run_id)

    result = await codex.result(handle.run_id)
    assert (result.input_tokens, result.output_tokens) == (200, 14)


# --------------------------------------------------------------------------- the session

NOW = datetime(2026, 9, 23, tzinfo=UTC)
DOCS = McpServerSpec(
    name="docs",
    transport=McpTransport.HTTP,
    url="https://mcp.example.com/docs",
    headers={"Authorization": "Bearer {secrets.DOCS_TOKEN}"},
)


def session_spec(**overrides: object) -> RunSpec:
    defaults = {"mcp": (DOCS,), "attachments": (attachment(),)}
    return spec(**(defaults | overrides))


def launches(auth_mode: str) -> list[Launch]:
    """A spawn, a resume with a turn and an image, and a rework's adoption, each with its seeds."""
    harness = CodexHarness()
    run = session_spec()
    secrets = run.resolved_secrets()
    made = [
        (harness.session_spawn(run, secrets, auth_mode, "unused"), run.prompts.system),
        (
            harness.session_resume(
                harness.session_flags(run),
                THREAD,
                secrets,
                auth_mode,
                run.mcp,
                "go on",
                1,
                run.attachments,
                (),
            ),
            None,
        ),
        (harness.session_adopt(run, secrets, auth_mode, THREAD, 2), run.prompts.system),
    ]
    seeded = []
    for launch, system in made:
        seeds = harness.session_seeds(WORKSPACE, CONFIG_DIR, auth_mode, run.mcp, system)
        seeded.append(Launch(launch.argv, launch.env, {**launch.files, **seeds}, launch.inject))
    return seeded


def test_the_codex_template_links_the_vendored_native_binary_and_checks_its_version(
    tmp_path: Path,
) -> None:
    """A session runs the binary npm's package vendors, since npm's `codex` is a node script whose
    SIGKILL orphans the agent. Where it lies is npm's to change, so a build that finds none, two,
    or another version must fail, not leave a template whose every session fails to start."""
    from e2b import Template

    dockerfile = Template.to_dockerfile(definition("codex", "0.164.0"))
    assert dockerfile.index("npm install -g") < dockerfile.index(link_native("0.164.0"))
    assert "codex-native" not in Template.to_dockerfile(definition("claude", "2.1.310"))

    def build(*versions: str) -> tuple[int, Path, str]:
        case = Path(tempfile.mkdtemp(dir=tmp_path))
        (case / "bin").mkdir()
        npm = case / "bin" / "npm"
        npm.write_text(f"#!/bin/sh\necho {case / 'lib'}\n")
        npm.chmod(0o755)
        vendored = case / "lib/@openai/codex/node_modules/@openai/codex-linux-x64"
        vendored.mkdir(parents=True)
        for n, version in enumerate(versions):
            binary = vendored / f"vendor/target-{n}/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_text(f'#!/bin/sh\necho "codex-cli {version}"\n')
            binary.chmod(0o755)
        link = case / "codex-native"
        done = subprocess.run(
            ["sh", "-c", link_native("0.159.0", str(link))],
            env={"PATH": f"{case / 'bin'}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, link, done.stderr

    code, link, _ = build("0.159.0")
    assert code == 0 and os.readlink(link).endswith("/vendor/target-0/bin/codex")
    for vendored in [(), ("0.159.0", "0.159.0")]:
        code, _, said = build(*vendored)
        assert code != 0 and "expected one vendored codex binary" in said, vendored
    assert build("0.160.0")[0] != 0, "the binary is another version"


@pytest.mark.parametrize("auth_mode", ["api_key", "subscription"])
def test_a_codex_session_launch_holds_placeholders_only(auth_mode: str) -> None:
    """The model key and every MCP bearer ride the egress proxy's headers; the hook command is
    in the config. A subscription's login has nothing to inject, so it alone is written, and
    only to `auth.json`."""
    secrets = session_spec().resolved_secrets()
    for launch in launches(auth_mode):
        assert launch.argv[0] == CODEX_NATIVE
        written = {
            path: content.decode(errors="replace") for path, (content, _, _) in launch.files.items()
        }
        if auth_mode == "subscription":
            assert written.pop(AUTH_PATH) == LOGIN
        seen = "\n".join([*launch.argv, *launch.env.values(), *written.values()])
        for secret in secrets.values():
            assert secret not in seen
        assert launch.inject["mcp.example.com"] == {"Authorization": f"Bearer {MCP_TOKEN}"}
        if auth_mode == "api_key":
            assert launch.inject["api.openai.com"] == {"Authorization": f"Bearer {API_KEY}"}


def test_codex_seeds_trust_nothing_in_the_workspace_and_hook_the_signal_script() -> None:
    """Untrusted, the checkout's `.codex/` neither loads (its MCP servers, its sandbox mode) nor
    puts a hook review in front of the turn, and the hooks run only with a hash keyed on the
    config's real path. The file must parse: a second trust table for the same path makes codex
    refuse it, so the checkout is seeded once."""
    harness: Harness = CodexHarness()
    spawned, resumed = (
        tomllib.loads(seeds[CONFIG_PATH][0].decode())
        for seeds in (
            harness.session_seeds(
                WORKSPACE, CONFIG_DIR, "api_key", (DOCS,), "You are the impl agent."
            ),
            harness.session_seeds(WORKSPACE, CONFIG_DIR, "api_key", (DOCS,), None),
        )
    )
    assert spawned["projects"] == {WORKSPACE: {"trust_level": "untrusted"}}
    assert f"FACTORIAL_SIGNALS={SIGNALS} exec {SIGNAL}" == SIGNAL_COMMAND
    for event, label in HOOK_LABELS.items():
        assert spawned["hooks"][event] == [
            {"hooks": [{"type": "command", "command": SIGNAL_COMMAND}]}
        ]
        state = spawned["hooks"]["state"][f"{CONFIG_DIR}/config.toml:{label}:0:0"]
        assert state == {"trusted_hash": hook_hash(label, SIGNAL_COMMAND)}
    assert (spawned["sandbox_mode"], spawned["approval_policy"]) == ("danger-full-access", "never")
    assert spawned["check_for_update_on_startup"] is False and "notify" not in spawned
    assert set(spawned["mcp_servers"]) == {"docs"}
    assert spawned["developer_instructions"] == "You are the impl agent."
    assert "developer_instructions" not in resumed, "a resume's thread already holds them"
    moved = harness.session_seeds(WORKSPACE, "/elsewhere/.factorial", "api_key", (), None)
    assert "/elsewhere/.factorial/config.toml:stop:0:0" in moved[CONFIG_PATH][0].decode()


def test_the_subscription_login_is_written_at_each_launch_and_scrubbed_before_each_pause() -> None:
    """A login is the one credential a sandbox holds: every launch writes the one it is handed,
    since the pause before it deleted the last."""
    for launch in launches("subscription"):
        assert launch.files[AUTH_PATH] == (LOGIN.encode(), "agent", 0o600)
        assert "api.openai.com" not in launch.inject
    assert AUTH_PATH in CodexHarness().credential_paths


class _CodexSessions(E2bAdapter):
    harness = CodexHarness()
    kind: ClassVar[str] = "codex_e2b"


async def test_session_adopt_resumes_the_thread_with_the_rework_header_and_the_steps_prompt(
    tmp_path: Path,
) -> None:
    """A rework resumes the earlier run's thread in its kept sandbox, the step's prompt as its
    turn behind the header that says where the checkout now stands. The config is rendered
    again, the system prompt with it: the earlier run's agent could have rewritten the file."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(consent=True))])
    options = SandboxOptions("factorial-agent:build-1", 3600, {}, NetworkPolicy(allow=()))
    sandbox = await client.create(options)
    log = rollout_path(THREAD)
    sandbox.logs[log] = b'{"type": "session_meta"}\n'
    await client.pause(sandbox.sandbox_id)
    prev = "GH-9-impl-agent-0000000a"
    (tmp_path / "state" / "runs" / prev).mkdir(parents=True)
    _write_state(
        tmp_path / "state" / "runs" / prev,
        _RunState(
            run_id=prev,
            sandbox_id=sandbox.sandbox_id,
            status=RunStatus.SUCCEEDED,
            agent="impl-agent",
            ticket="GH-9",
            repo="acme/widgets",
            branch="impl/9-{slug}",
            auth_mode="api_key",
            started_at=NOW,
            deadline=NOW,
            session_id=THREAD,
            kept=True,
            workspace_real=WORKSPACE,
            session_log=log,
            segments=1,
        ),
    )
    codex = _CodexSessions(
        template="factorial-agent:build-1",
        state_root=tmp_path / "state",
        deployment="test",
        auth_mode="api_key",
        client=client,
        now=lambda: NOW,
        poll_interval=0,
    )
    run = session_spec(resume_from=ResumeFrom(run_id=prev, session_id=THREAD))
    handle = await codex.spawn(run)

    argv = sandbox.files[ARGV][0].decode().rstrip("\0").split("\0")
    assert argv[:2] == [CODEX_NATIVE, "resume"]
    assert f"--image={ATTACHMENTS_DIR}/image-1.png" in argv
    assert argv[-2:] == [THREAD, f"Read {turn_file(1)} and do what it says."]
    header = rework_header("spec/12-fix", REWORK_HEAD, None, None)
    step = turn_text(run, attachment_paths(run.attachments))
    assert sandbox.files[turn_file(1)][0].decode() == f"{header}\n\n{step}"
    config = tomllib.loads(sandbox.files[CONFIG_PATH][0].decode())
    assert config["developer_instructions"] == "You are the impl agent."
    state = json.loads((tmp_path / "state" / "runs" / handle.run_id / "state.json").read_text())
    assert (state["session_log"], state["log_offset"]) == (log, len(sandbox.logs[log]))
    await codex.cancel(handle.run_id)
    await codex.drain()


async def test_the_fake_codex_tui_holds_its_turn_behind_the_folder_consent() -> None:
    """Codex asks for the folder at every launch, the prompt held behind it (0.159.0). The fake
    holds its turn there too, or a session test passes on a launch that never took it."""
    client = FakeSandboxClient(
        scripts=[Script(tui=TuiScript(steps=[codex_turn(meta=True)], consent=True))]
    )
    options = SandboxOptions("factorial-agent:build-1", 3600, {}, NetworkPolicy(allow=()))
    sandbox = await client.create(options)
    run = session_spec()
    launch = CodexHarness().session_spawn(run, run.resolved_secrets(), "api_key", "unused")
    await sandbox.write_many({ARGV: "\0".join(launch.argv).encode()})
    await sandbox.start(SESSION)
    log = rollout_path(CODEX_THREAD)
    envs = {"LOG": log, "LOG_FROM": "0", "SIG_FROM": "0", "LIMIT": "1000000"}

    held = read_session_poll((await sandbox.run(SESSION_POLL, envs=envs)).stdout)
    assert (held.log_size, held.signals) == (None, b"")
    assert CONSENT_RESTRICTED in (await sandbox.run(CAPTURE)).stdout
    assert (await sandbox.run(ROLLOUT_FIND)).stdout == ""
    assert (await sandbox.run(CONSENT)).exit_code == 0
    released = read_session_poll((await sandbox.run(SESSION_POLL, envs=envs)).stdout)
    assert (await sandbox.run(ROLLOUT_FIND)).stdout == f"{log}\n"
    assert (await sandbox.run(CONSENT)).exit_code != 0, "a second launch's consent, not this one's"

    transcript, reader = RolloutTranscript(CODEX_THREAD, turn=True), StreamReader()
    for line in released.log.decode().splitlines():
        for out in transcript.feed(line):
            reader.feed(out)
    assert (reader.turns, reader.summary, reader.input_tokens) == (1, "Opened PR #9.", 100)
    assert b'"Stop"' in released.signals
