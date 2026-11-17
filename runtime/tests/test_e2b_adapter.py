"""The E2B adapter against a fake sandbox: what must hold for a run nobody can watch directly."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from e2b import exceptions as e2b_exceptions
from fake_e2b import FakeSandbox, FakeSandboxClient, Script, TuiScript
from pydantic import SecretStr

from runtime.adapters.e2b import ClaudeE2bAdapter, layout
from runtime.adapters.e2b import adapter as adapter_module
from runtime.adapters.e2b.adapter import SESSION_CAP_REASON, _read_state, _write_state
from runtime.adapters.e2b.client import CommandOutput
from runtime.adapters.e2b.harness import Launch
from runtime.adapters.e2b.layout import (
    ARGV,
    ATTACHMENTS_DIR,
    END_SHELL,
    ENV,
    FIREWALL,
    INSPECT,
    INSPECT_STOP,
    MODES,
    PREVIEW,
    PREVIEW_ENV,
    PREVIEW_SERVICES,
    PREVIEW_SETUP_ENV,
    PREVIEW_STATUS,
    PREVIEW_STOP,
    SCRIPTS,
    SCRUB,
    STOP,
    TMUX_CONF,
    VIEW_ARGV,
    VIEW_ENV,
    session_log,
    turn_file,
)
from runtime.contract import (
    PREVIEW_IDLE,
    PREVIEW_RENEWAL,
    AdapterUnavailable,
    AgentPrompts,
    InspectNoSession,
    InspectPhase,
    InspectUnavailable,
    McpServerSpec,
    McpTransport,
    ParkReason,
    PreviewHost,
    PreviewPhase,
    PreviewServiceSpec,
    PreviewSpec,
    PreviewUnavailable,
    RunAttachment,
    RunSpec,
    RunStatus,
    SecretRoute,
    SpawnError,
    SpawnReason,
    SubstrateUnavailable,
    TerminalTarget,
    UnknownRunError,
    Workspace,
)
from runtime.terminal import TMUX_LOCKDOWN

pytestmark = pytest.mark.asyncio

TOKEN = "ghs_e2bTestTokenMustNeverEnterTheSandbox"  # gitleaks:allow
API_KEY = "sk-ant-e2bTestKeyMustNeverEnterTheSandbox"
DOCS_TOKEN = "docs-e2bTestHeaderValueMustNeverEnter"

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


def make_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "secrets": {
            "GH_TOKEN": SecretStr(TOKEN),
            "ANTHROPIC_API_KEY": SecretStr(API_KEY),
            "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
        },
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 40,
    }
    return RunSpec.model_validate(defaults | overrides)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _nothing_held() -> None:
    """The reaper's hold registry is the worker's, shared by every adapter in the process; the
    fake's sandbox ids repeat from one test to the next."""
    adapter_module._HELD.clear()
    adapter_module._LOCKS.clear()


def adapter_for(
    tmp_path: Path, client: FakeSandboxClient, clock: Clock | None = None
) -> ClaudeE2bAdapter:
    return ClaudeE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "state",
        deployment="test",
        client=client,
        now=clock or Clock(),
        poll_interval=0,
    )


def _hook(event: str, **fields: str) -> str:
    return json.dumps({"at": "2026-09-23T11:59:00Z", "hook": {"hook_event_name": event, **fields}})


def _cost(usd: float) -> str:
    return json.dumps({"type": "cost-state", "totalCostUSD": usd, "modelUsage": {}})


def at_prompt(usd: float = 0.25) -> TuiScript:
    """A session's first turn, to its Stop; then its prompt, until its finish."""
    hooks = [_hook("SessionStart", source="startup"), _hook("UserPromptSubmit"), _hook("Stop")]
    log = [json.dumps({"type": "user", "message": {"role": "user", "content": "the turn"}})]
    return TuiScript(steps=[(hooks, log)], on_exit=([_hook("SessionEnd")], [_cost(usd)]))


async def _finish_session(adapter: ClaudeE2bAdapter, run_id: str) -> None:
    for _ in range(10_000):
        if await adapter.status(run_id) is RunStatus.IDLE:
            break
        await asyncio.sleep(0)
    await adapter.finish(run_id)
    assert await adapter.status(run_id) is RunStatus.SUCCEEDED


async def run_to_end(
    tmp_path: Path, scripts: Sequence[Script], spec: RunSpec | None = None
) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    client = FakeSandboxClient(scripts=list(scripts))
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(spec or make_spec())
    await _finish_session(adapter, handle.run_id)
    await adapter.drain()
    return adapter, client, handle.run_id


# --------------------------------------------------------------------------- the sandbox


async def test_no_secret_ever_enters_the_sandbox(tmp_path: Path) -> None:
    """The model key, the GitHub token and MCP header values ride the egress proxy's rules;
    the sandbox is handed placeholders (decision 25)."""
    server = McpServerSpec(
        name="product-docs",
        transport=McpTransport.HTTP,
        url="https://mcp.example.com/docs",
        headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
    )
    _, client, _ = await run_to_end(tmp_path, [Script(tui=at_prompt())], make_spec(mcp=(server,)))

    seen = client.everything_seen()
    for secret in (TOKEN, API_KEY, DOCS_TOKEN):
        assert secret not in seen
    inject = client.only().options.network.inject
    assert inject["api.anthropic.com"] == {"x-api-key": API_KEY}
    assert inject["mcp.example.com"] == {"Authorization": f"Bearer {DOCS_TOKEN}"}
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert inject["github.com"] == {"Authorization": f"Basic {basic}"}


RAILWAY = "rw-e2bTestProjectTokenMustNeverEnter"
HOME_SEEN = "HOME=/tmp/elsewhere"
DB_PASSWORD = "db-e2bTestPasswordTheAgentMayRead"
RAILWAY_ROUTE = SecretRoute(
    name="RAILWAY_TOKEN_X", host="backboard.railway.com", header="project-access-token"
)


def delivering_spec(**overrides: object) -> RunSpec:
    """A spec with one project secret routed through the proxy and one exposed as a variable."""
    secrets = {
        "GH_TOKEN": SecretStr(TOKEN),
        "ANTHROPIC_API_KEY": SecretStr(API_KEY),
        "RAILWAY_TOKEN_X": SecretStr(RAILWAY),
        "DB_PASSWORD": SecretStr(DB_PASSWORD),
    }
    defaults: dict[str, object] = {
        "secrets": secrets,
        "egress": ("backboard.railway.com",),
        "secret_routes": (RAILWAY_ROUTE,),
        "secret_env": ("DB_PASSWORD",),
    }
    return make_spec(**(defaults | overrides))


def env_file(sandbox: object) -> dict[str, str]:
    content = sandbox.files[ENV][0]  # type: ignore[attr-defined]
    return dict(entry.split("=", 1) for entry in content.decode().split("\0") if entry)


def state_text(tmp_path: Path, run_id: str) -> str:
    return (tmp_path / "state" / "runs" / run_id / "state.json").read_text("utf-8")


async def test_a_routed_secret_rides_the_proxy_and_the_agent_sees_a_placeholder(
    tmp_path: Path,
) -> None:
    _, client, run_id = await run_to_end(tmp_path, [Script(tui=at_prompt())], delivering_spec())

    sandbox = client.only()
    assert sandbox.options.network.inject["backboard.railway.com"] == {
        "project-access-token": RAILWAY
    }
    assert env_file(sandbox)["RAILWAY_TOKEN_X"] == adapter_module.PLACEHOLDER_ROUTED
    assert RAILWAY not in client.everything_seen()
    assert RAILWAY not in state_text(tmp_path, run_id)


GLAB_KEYS = ("GITLAB_TOKEN", "GITLAB_HOST", "GLAB_CHECK_UPDATE", "GLAB_NO_PROMPT")


def _on_host(host: str) -> RunSpec:
    secrets = {"GH_TOKEN": SecretStr(TOKEN), "ANTHROPIC_API_KEY": SecretStr(API_KEY)}
    if host:
        secrets |= {"GITLAB_TOKEN": SecretStr(TOKEN), "GIT_HTTP_HOST": SecretStr(host)}
    return make_spec(secrets=secrets)


async def test_glab_on_a_gitlab_run_gets_a_placeholder_token_and_its_host(
    tmp_path: Path,
) -> None:
    """With no token or host glab refuses before any request; the proxy sets the real one."""
    _, client, run_id = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], _on_host("gitlab.example.com")
    )

    env = env_file(client.only())
    assert {key: env[key] for key in GLAB_KEYS} == {
        "GITLAB_TOKEN": adapter_module.PLACEHOLDER_GITLAB_TOKEN,
        "GITLAB_HOST": "gitlab.example.com",
        "GLAB_CHECK_UPDATE": "false",
        "GLAB_NO_PROMPT": "true",
    }
    assert TOKEN not in client.everything_seen()
    assert TOKEN not in state_text(tmp_path, run_id)


@pytest.mark.parametrize("host", ["", "github.com"])
async def test_a_github_run_gets_no_glab_environment(tmp_path: Path, host: str) -> None:
    _, client, _ = await run_to_end(tmp_path, [Script(tui=at_prompt())], _on_host(host))

    assert not set(GLAB_KEYS) & set(env_file(client.only()))


async def test_the_env_leak_check_passes_the_git_host_and_still_refuses_a_token(
    tmp_path: Path,
) -> None:
    adapter = adapter_for(tmp_path, FakeSandboxClient())
    env = {"GITLAB_HOST": "gitlab.example.com", "LEAKED": f"x{TOKEN}"}
    secrets = {"GH_TOKEN": TOKEN, "GIT_HTTP_HOST": "gitlab.example.com"}
    with pytest.raises(SpawnError, match=r"carry a secret in LEAKED$") as refused:
        await adapter._write_launch(cast(Any, None), Launch(argv=("claude",), env={}), env, secrets)
    assert refused.value.reason is SpawnReason.CONFIG


async def test_a_route_to_a_host_off_the_allowlist_is_refused_before_a_sandbox_exists(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient()
    route = SecretRoute(name="RAILWAY_TOKEN_X", host="evil.example", header="x-token")
    with pytest.raises(SpawnError, match=r"evil\.example") as refused:
        await adapter_for(tmp_path, client).spawn(delivering_spec(secret_routes=(route,)))
    assert refused.value.reason is SpawnReason.CONFIG
    assert client.sandboxes == {}


async def test_a_route_cannot_replace_a_header_factorial_sets(tmp_path: Path) -> None:
    client = FakeSandboxClient()
    route = SecretRoute(name="RAILWAY_TOKEN_X", host="api.github.com", header="authorization")
    with pytest.raises(SpawnError, match=r"api\.github\.com") as refused:
        await adapter_for(tmp_path, client).spawn(delivering_spec(secret_routes=(route,)))
    assert refused.value.reason is SpawnReason.CONFIG
    assert client.sandboxes == {}


async def test_an_env_secret_reaches_the_env_file_and_never_the_run_state(
    tmp_path: Path,
) -> None:
    _, client, run_id = await run_to_end(tmp_path, [Script(tui=at_prompt())], delivering_spec())

    assert env_file(client.only())["DB_PASSWORD"] == DB_PASSWORD
    assert DB_PASSWORD not in state_text(tmp_path, run_id)


async def test_a_resume_rearms_routes_and_rewrites_env_secrets_from_fresh_values(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript()), Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(delivering_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    fresh_railway, fresh_db = "rw-theRotatedProjectToken000", "db-theRotatedPassword0000"
    await adapter.resume(
        handle.run_id,
        "address the review",
        {
            "GH_TOKEN": SecretStr(TOKEN),
            "ANTHROPIC_API_KEY": SecretStr(API_KEY),
            "RAILWAY_TOKEN_X": SecretStr(fresh_railway),
            "DB_PASSWORD": SecretStr(fresh_db),
        },
    )
    await _finish_session(adapter, handle.run_id)

    sandbox = client.only()
    rearmed = sandbox.networks[-2].inject
    assert rearmed["backboard.railway.com"] == {"project-access-token": fresh_railway}
    assert env_file(sandbox)["DB_PASSWORD"] == fresh_db


async def test_a_runs_end_beside_a_serving_preview_scrubs_the_env_file(tmp_path: Path) -> None:
    """The sandbox stays awake, and publicly reachable, for the preview: an exposed value must
    not stay on its disk."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(delivering_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)

    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    assert client.only().state == "running"
    scrubs = [c.command for c in client.only().commands if c.command.startswith(SCRUB)]
    assert scrubs and f"'{ENV}'" in scrubs[-1]


@pytest.mark.parametrize("delivery", ["env", "route"])
async def test_a_project_secret_cannot_replace_a_variable_the_adapter_sets(
    tmp_path: Path, delivery: str
) -> None:
    secrets = {"GH_TOKEN": SecretStr(TOKEN), "ANTHROPIC_API_KEY": SecretStr(API_KEY)}
    route = SecretRoute(name="HOME", host="backboard.railway.com", header="x-home")
    overrides: dict[str, object] = (
        {"secret_env": ("HOME",), "secret_routes": ()}
        if delivery == "env"
        else {"secret_env": (), "secret_routes": (route,)}
    )
    client = FakeSandboxClient()
    with pytest.raises(SpawnError, match="HOME") as refused:
        await adapter_for(tmp_path, client).spawn(
            delivering_spec(secrets=secrets | {"HOME": SecretStr("/tmp/elsewhere")}, **overrides)
        )
    assert refused.value.reason is SpawnReason.CONFIG
    assert HOME_SEEN not in client.everything_seen()


async def test_an_exposed_secret_with_no_value_is_refused_like_a_route(tmp_path: Path) -> None:
    secrets = {"GH_TOKEN": SecretStr(TOKEN), "ANTHROPIC_API_KEY": SecretStr(API_KEY)}
    client = FakeSandboxClient()
    with pytest.raises(SpawnError, match="DB_PASSWORD") as refused:
        await adapter_for(tmp_path, client).spawn(
            delivering_spec(secrets=secrets, secret_routes=(), secret_env=("DB_PASSWORD",))
        )
    assert refused.value.reason is SpawnReason.CONFIG
    assert client.sandboxes == {}


async def test_parking_scrubs_the_env_file_an_env_secret_was_written_to(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(delivering_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    scrubs = [c.command for c in client.only().commands if c.command.startswith(SCRUB)]
    assert scrubs and f"'{ENV}'" in scrubs[-1]


async def test_the_sandbox_takes_no_inbound_traffic_and_denies_unlisted_egress(
    tmp_path: Path,
) -> None:
    _, client, _ = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], make_spec(egress=("registry.npmjs.org",))
    )

    network = client.only().options.network.as_sdk(create=True)
    assert network["allow_public_traffic"] is False
    assert network["deny_out"] == ["0.0.0.0/0"]
    assert "registry.npmjs.org" in network["allow_out"]
    assert "api.anthropic.com" in network["allow_out"]


async def test_only_a_run_kept_for_a_preview_is_created_public(tmp_path: Path) -> None:
    """E2B sets public traffic only at creation, so a run whose preview its sandbox will serve
    must be created public, and every other run stays closed (decision 54)."""
    _, client, run_id = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], make_spec(public=True)
    )

    network = client.only().options.network.as_sdk(create=True)
    assert network["allow_public_traffic"] is True
    state = json.loads((tmp_path / "state" / "runs" / run_id / "state.json").read_text())
    assert state["public"] is True


async def test_the_session_launches_through_the_root_scripts_with_no_shell_interpolation(
    tmp_path: Path,
) -> None:
    """Values reach the sandbox through files and env, never inside a command string."""
    spec = make_spec(workspace=Workspace(repo="acme/widgets", base="feature/x", branch="b/{slug}"))
    _, client, _ = await run_to_end(tmp_path, [Script(tui=at_prompt())], spec)

    sandbox = client.only()
    setup = next(c for c in sandbox.commands if c.envs.get("BASE"))
    assert setup.envs == {"REPO": "acme/widgets", "BASE": "feature/x"}
    assert "feature/x" not in setup.command
    argv = sandbox.files[ARGV][0].split(b"\0")
    assert argv[0] == b"claude" and b"-p" not in argv
    env = dict(e.split(b"=", 1) for e in sandbox.files[ENV][0].split(b"\0") if e)
    assert env[b"NODE_EXTRA_CA_CERTS"] == b"/ca", "the template's CA settings survive `env -i`"
    assert any(c.command == layout.SESSION for c in sandbox.commands)


async def test_a_pinned_config_commit_is_fetched_exported_and_named_in_the_first_turn(
    tmp_path: Path,
) -> None:
    sha = "c" * 40
    workspace = Workspace(repo="acme/widgets", base="main", branch="b/{slug}", config_sha=sha)
    _, client, _ = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], make_spec(workspace=workspace)
    )

    sandbox = client.only()
    setup = next(c for c in sandbox.commands if c.envs.get("BASE"))
    assert setup.envs["CONFIG_SHA"] == sha
    assert sha not in setup.command
    env = dict(e.split(b"=", 1) for e in sandbox.files[ENV][0].split(b"\0") if e)
    assert env[b"FACTORIAL_CONFIG_SHA"] == sha.encode()
    assert f"git show {sha}:.factorial/".encode() in sandbox.files[turn_file(0)][0]


# --------------------------------------------------------------------------- restart survival


async def test_a_vanished_sandbox_before_its_deadline_is_a_failure(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    first = adapter_for(tmp_path, client)
    handle = await first.spawn(make_spec())
    for task in first._supervisors.values():
        task.cancel()
    client.gone.add(client.only().sandbox_id)

    assert await adapter_for(tmp_path, client).status(handle.run_id) is RunStatus.FAILED


async def test_a_vanished_sandbox_past_its_session_cap_is_a_timeout(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    clock = Clock()
    first = adapter_for(tmp_path, client, clock)
    handle = await first.spawn(make_spec())
    for task in first._supervisors.values():
        task.cancel()
    client.gone.add(client.only().sandbox_id)
    clock.now += adapter_module.MAX_SESSION + timedelta(minutes=1)

    assert await adapter_for(tmp_path, client, clock).status(handle.run_id) is RunStatus.TIMEOUT


async def test_a_terminal_run_stays_terminal_once_its_sandbox_is_gone(tmp_path: Path) -> None:
    """Status is about the agent, not the sandbox: a killed sandbox changes nothing."""
    _, client, run_id = await run_to_end(tmp_path, [Script(tui=at_prompt())])
    calls = len(client.connects)

    assert await adapter_for(tmp_path, client).status(run_id) is RunStatus.SUCCEEDED
    assert len(client.connects) == calls, "a terminal run is answered from the mirror"


async def test_the_api_reads_a_transcript_with_no_e2b_key(tmp_path: Path) -> None:
    adapter, _, run_id = await run_to_end(tmp_path, [Script(tui=at_prompt())])
    reader = ClaudeE2bAdapter(state_root=tmp_path / "state")

    transcript = [line async for line in adapter.logs(run_id)]
    assert transcript and [line async for line in reader.logs(run_id)] == transcript
    with pytest.raises(UnknownRunError):
        [line async for line in reader.logs("no-such-run")]


# --------------------------------------------------------------------------- interrupt and resume


async def test_an_interrupt_scrubs_drops_the_credentials_and_pauses_never_kills(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    sandbox = client.only()
    assert await adapter.status(handle.run_id) is RunStatus.INTERRUPTED
    assert client.paused == [sandbox.sandbox_id] and client.killed == []
    assert any(c.command.startswith(SCRUB) for c in sandbox.commands)
    assert sandbox.networks[-1].inject == {}, "no credential stays in E2B's control plane"


async def test_an_interrupt_that_cannot_pause_fails_the_run_and_kills_the_sandbox(
    tmp_path: Path,
) -> None:
    """INTERRUPTED promises a sandbox kept for a resume and holding no credential; a pause that
    failed keeps neither promise, so the run must not claim it."""
    client = FakeSandboxClient(
        scripts=[Script(tui=TuiScript())], pause_error=RuntimeError("E2B 503")
    )
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    assert await adapter.status(handle.run_id) is RunStatus.FAILED
    assert client.killed == [client.only().sandbox_id]


async def _ended(tmp_path: Path, end: str) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    """One run brought to `end`; `cancelled-unsupervised` is a cancel from a restarted worker."""
    tuis = {
        "succeeded": at_prompt(),
        "failed": TuiScript(steps=at_prompt().steps, ends_by_itself=True, exit_code=1),
    }
    client = FakeSandboxClient(scripts=[Script(tui=tuis.get(end, TuiScript()))])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    if end == "succeeded":
        await _finish_session(adapter, handle.run_id)
    elif end == "cancelled":
        await adapter.cancel(handle.run_id)
    elif end == "cancelled-unsupervised":
        for task in adapter._supervisors.values():
            task.cancel()
        adapter = adapter_for(tmp_path, client)
        await adapter.cancel(handle.run_id)
    await adapter.drain()
    return adapter, client, handle.run_id


@pytest.mark.parametrize(
    ("end", "status"),
    [
        ("succeeded", RunStatus.SUCCEEDED),
        ("failed", RunStatus.FAILED),
        ("cancelled", RunStatus.CANCELLED),
        ("cancelled-unsupervised", RunStatus.CANCELLED),
    ],
)
async def test_every_end_parks_the_sandbox_and_never_kills_it(
    tmp_path: Path, end: str, status: RunStatus
) -> None:
    """Decision 54: every run's workspace is kept, scrubbed of its credentials the way an
    interrupt's is, for a rework, a preview or a session view; the reaper deletes it later."""
    adapter, client, run_id = await _ended(tmp_path, end)

    sandbox = client.only()
    assert await adapter.status(run_id) is status
    if end == "failed":
        failure = (await adapter.result(run_id)).failure_reason
        assert failure == "The Claude CLI stopped unexpectedly (exit 1)."
    assert client.paused == [sandbox.sandbox_id]
    assert client.killed == []
    assert any(c.command.startswith(SCRUB) for c in sandbox.commands)
    assert sandbox.network.inject == {}, "no credential stays in E2B's control plane"
    assert _read_state(tmp_path / "state" / "runs" / run_id, run_id).kept is True


async def test_a_failed_park_kills_the_sandbox_and_the_outcome_stands(tmp_path: Path) -> None:
    """Keeping the workspace is not part of the outcome: a run that succeeded stays succeeded,
    and a sandbox that could not be scrubbed and paused is not left holding a credential."""
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], pause_error=RuntimeError("E2B 503")
    )
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await _finish_session(adapter, handle.run_id)
    await adapter.drain()

    assert await adapter.status(handle.run_id) is RunStatus.SUCCEEDED
    assert client.killed == [client.only().sandbox_id]
    state = json.loads((tmp_path / "state" / "runs" / handle.run_id / "state.json").read_text())
    assert state["kept"] is False


async def test_a_quit_whose_sandbox_cannot_be_kept_is_judged_as_the_clean_end_it_was(
    tmp_path: Path,
) -> None:
    """A quit parks only so a person can resume it, and a killed sandbox resumes nothing. Failed
    instead, a run whose work was done would never reach its postconditions."""
    quit_ = TuiScript(steps=at_prompt().steps, ends_by_itself=True)
    client = FakeSandboxClient(scripts=[Script(tui=quit_)], pause_error=RuntimeError("E2B 503"))
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    await adapter.drain()

    result = await adapter.result(handle.run_id)
    assert (result.outcome, result.failure_reason, result.park_reason) == (
        RunStatus.SUCCEEDED,
        None,
        None,
    )
    assert client.killed == [client.only().sandbox_id]
    state = _read_state(tmp_path / "state" / "runs" / handle.run_id, handle.run_id)
    assert (state.park_reason, state.kept) == (None, False)


async def test_a_cancelled_public_run_can_still_be_previewed(tmp_path: Path) -> None:
    """The orchestrator's keep rule serves a cancelled run's preview (decision 54), so the
    adapter must not refuse what the dashboard offers."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True))
    await adapter.cancel(handle.run_id)

    started = await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    assert started.phase is PreviewPhase.STARTING


@pytest.mark.parametrize(
    ("status", "retain", "public", "kept"),
    [("succeeded", True, True, True), ("interrupted", False, False, True)],
)
async def test_a_state_file_written_with_retain_still_reads(
    tmp_path: Path, status: str, retain: bool, public: bool, kept: bool
) -> None:
    """A run kept for a preview or parked by an interrupt before `retain` became `public` and
    `kept` must stay readable after the deploy: the state forbids unknown keys."""
    adapter, _, run_id = await run_to_end(tmp_path, [Script(tui=at_prompt())])
    run_dir = tmp_path / "state" / "runs" / run_id
    written = json.loads((run_dir / "state.json").read_text())
    del written["public"], written["kept"]
    (run_dir / "state.json").write_text(json.dumps(written | {"status": status, "retain": retain}))

    assert await adapter.status(run_id) is RunStatus(status)
    state = _read_state(run_dir, run_id)
    assert (state.public, state.kept) == (public, kept)


async def test_a_state_file_written_before_every_run_was_a_session_still_reads_and_prunes(
    tmp_path: Path,
) -> None:
    """Every live state file holds `interactive` and `timeout_seconds` at the deploy, and the
    state forbids unknown keys, so without the before-validator its status read raises. The prune
    is then checked on that same file."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    clock = Clock()
    adapter = adapter_for(tmp_path, client, clock)
    run_id = (await adapter.spawn(make_spec())).run_id
    await _finish_session(adapter, run_id)
    await adapter.drain()
    run_dir = tmp_path / "state" / "runs" / run_id
    written = json.loads((run_dir / "state.json").read_text())
    written |= {"interactive": True, "timeout_seconds": 1800}
    (run_dir / "state.json").write_text(json.dumps(written))

    assert await adapter.status(run_id) is RunStatus.SUCCEEDED
    clock.now = datetime.now(UTC) + timedelta(hours=2)
    await adapter.reap((), retain=timedelta(hours=1))
    assert not run_dir.exists()


def _outages() -> list[Exception]:
    return [
        e2b_exceptions.SandboxException("E2B answered 503"),
        httpx.ConnectError("E2B answered 503"),
    ]


@pytest.mark.parametrize("call", ["info", "connect"])
@pytest.mark.parametrize("outage", range(len(_outages())), ids=["sandbox-503", "transport"])
async def test_a_resume_that_cannot_reach_e2b_is_a_passing_outage(
    tmp_path: Path, call: str, outage: int
) -> None:
    """Refused rather than unavailable, the orchestrator re-parks the ticket and a human has to
    release it again for a fault that has already cleared; unavailable, the tick retries."""
    adapter, client, run_id = await _interrupted_run(tmp_path)
    setattr(client, f"{call}_error", _outages()[outage])

    with pytest.raises(SubstrateUnavailable, match="503"):
        await adapter.resume(run_id, "go on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})
    assert await adapter.status(run_id) is RunStatus.INTERRUPTED


@pytest.mark.parametrize("call", ["info", "connect"])
async def test_a_resume_with_a_revoked_e2b_key_is_refused_not_retried(
    tmp_path: Path, call: str
) -> None:
    """An outage never counts toward quarantine: a revoked key read as one retries for ever and
    mints a token every tick. Refused, the ticket parks and says why."""
    adapter, client, run_id = await _interrupted_run(tmp_path)
    setattr(client, f"{call}_error", e2b_exceptions.AuthenticationException("invalid API key"))

    with pytest.raises(AdapterUnavailable, match="invalid API key") as raised:
        await adapter.resume(run_id, "go on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})
    assert not isinstance(raised.value, SubstrateUnavailable)


async def _interrupted_run(tmp_path: Path) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    """A session parked by a human's interrupt, its sandbox kept for a resume."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    return adapter, client, handle.run_id


async def test_an_unreachable_interrupt_is_retried_and_never_kills_the_sandbox(
    tmp_path: Path,
) -> None:
    """A worker that restarted cannot reach the sandbox to stop it. Finalising anyway would
    leave no handle to pause with, and the sandbox a resume needs would be killed."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    first = adapter_for(tmp_path, client)
    handle = await first.spawn(make_spec())
    for task in first._supervisors.values():
        task.cancel()
    client.connect_error = RuntimeError("E2B 503")

    with pytest.raises(AdapterUnavailable):
        await adapter_for(tmp_path, client).interrupt(handle.run_id)

    assert client.killed == []
    state = json.loads((tmp_path / "state" / "runs" / handle.run_id / "state.json").read_text())
    assert state["status"] == "running", "not finalised, so the orchestrator retries the stop"


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"workspace": Workspace(repo="acme/w", base="spec/1-x", branch="b", isolation="blind")},
            SpawnReason.CONFIG,
        ),
        (
            {"mcp": (McpServerSpec(name="idx", transport=McpTransport.STDIO, command="npx"),)},
            SpawnReason.CONFIG,
        ),
        ({"secrets": {"GH_TOKEN": SecretStr(TOKEN)}}, SpawnReason.CREDENTIAL),
        (
            {"workspace": Workspace(repo="acme/w", base="--upload-pack=x", branch="b")},
            SpawnReason.WORKSPACE,
        ),
        (
            {"workspace": Workspace(repo="not a repo", base="main", branch="b")},
            SpawnReason.WORKSPACE,
        ),
    ],
)
async def test_a_spec_it_cannot_serve_is_refused_before_a_sandbox_exists(
    tmp_path: Path, overrides: dict[str, object], reason: SpawnReason
) -> None:
    client = FakeSandboxClient()
    with pytest.raises(SpawnError) as refused:
        await adapter_for(tmp_path, client).spawn(make_spec(**overrides))
    assert refused.value.reason is reason
    assert client.sandboxes == {}


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("fatal: couldn't find remote ref nope", SpawnReason.WORKSPACE),
        (
            "fatal: unable to access 'https://github.com/': Could not resolve host",
            SpawnReason.INFRASTRUCTURE,
        ),
    ],
)
async def test_a_failed_checkout_names_its_cause_and_kills_the_sandbox(
    tmp_path: Path, stderr: str, reason: SpawnReason
) -> None:
    client = FakeSandboxClient(setup_result=CommandOutput(128, "", stderr))
    with pytest.raises(SpawnError) as refused:
        await adapter_for(tmp_path, client).spawn(make_spec())
    assert refused.value.reason is reason
    assert client.killed == [client.only().sandbox_id], "a half-built sandbox is not left behind"


async def test_a_pinned_config_commit_the_remote_lacks_is_the_specs_fault(tmp_path: Path) -> None:
    sha = "c" * 40
    workspace = Workspace(repo="acme/widgets", base="main", branch="b/{slug}", config_sha=sha)
    stderr = f"fatal: remote error: upload-pack: not our ref {sha}"
    client = FakeSandboxClient(setup_result=CommandOutput(3, "", stderr))
    with pytest.raises(SpawnError) as refused:
        await adapter_for(tmp_path, client).spawn(make_spec(workspace=workspace))
    assert refused.value.reason is SpawnReason.WORKSPACE
    assert str(refused.value).startswith(
        f"The process config this ticket was pinned to (commit {sha})"
    )


async def test_a_harness_that_puts_a_secret_in_the_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.adapters.e2b import claude

    monkeypatch.setattr(
        claude.ClaudeHarness, "_tui_env", lambda self, auth, **_: {"ANTHROPIC_API_KEY": API_KEY}
    )
    client = FakeSandboxClient()
    with pytest.raises(SpawnError, match="ANTHROPIC_API_KEY"):
        await adapter_for(tmp_path, client).spawn(make_spec())
    assert client.killed == [client.only().sandbox_id]


# --------------------------------------------------------------------------- the reaper


async def test_the_reaper_kills_only_this_deployments_unwanted_sandboxes(tmp_path: Path) -> None:
    """A paused sandbox is kept until deleted. The reaper removes the ones no live run wants,
    and never another deployment's, which share the key."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript()) for _ in range(3)])
    adapter = adapter_for(tmp_path, client)
    wanted = await adapter.spawn(make_spec())
    unwanted = await adapter.spawn(make_spec())
    elsewhere = ClaudeE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "other",
        deployment="other",
        client=client,
        poll_interval=0,
    )
    theirs = await elsewhere.spawn(make_spec())

    reaped = await adapter.reap({wanted.run_id}, retain=timedelta(days=30))

    assert reaped == (unwanted.run_id,)
    live = {info.metadata["factorial_run"] for info in await client.list({})}
    assert live == {wanted.run_id, theirs.run_id}
    for task in [*adapter._supervisors.values(), *elsewhere._supervisors.values()]:
        task.cancel()


async def test_the_reaper_spares_a_sandbox_young_enough_to_be_mid_spawn(tmp_path: Path) -> None:
    """A spawn creates the sandbox before its run id is recorded anywhere the reaper reads."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    client.started_at = clock.now - timedelta(minutes=1)
    adapter = adapter_for(tmp_path, client, clock)
    await adapter.spawn(make_spec())

    assert await adapter.reap(set(), retain=timedelta(days=30)) == ()
    assert client.killed == []
    for task in adapter._supervisors.values():
        task.cancel()


async def test_a_deployment_with_no_id_reaps_nothing(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = ClaudeE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "s",
        client=client,
        poll_interval=0,
    )
    await adapter.spawn(make_spec())

    assert await adapter.reap(set(), retain=timedelta(days=30)) == ()
    assert client.killed == []
    for task in adapter._supervisors.values():
        task.cancel()


async def test_retention_deletes_old_terminal_mirrors_and_keeps_wanted_ones(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt()), Script(tui=at_prompt()), Script(tui=TuiScript())]
    )
    clock = Clock()
    adapter = adapter_for(tmp_path, client, clock)
    old = await adapter.spawn(make_spec())
    young = await adapter.spawn(make_spec())
    for finished in (old, young):
        await _finish_session(adapter, finished.run_id)
    await adapter.drain()
    parked = await adapter.spawn(make_spec())
    await asyncio.sleep(0)
    await adapter.interrupt(parked.run_id)
    await adapter.drain()
    runs = tmp_path / "state" / "runs"
    # Age is read from the state file's mtime, so both ends are pinned to the test's clock.
    for run_id, age in ((old, 40), (young, 1), (parked, 40)):
        written = (clock.now - timedelta(days=age)).timestamp()
        os.utime(runs / run_id.run_id / "state.json", (written, written))

    await adapter.reap({parked.run_id}, retain=timedelta(days=30))

    assert not (runs / old.run_id).exists(), "a finished run's transcript is retained 30 days"
    assert (runs / young.run_id).exists(), "and not a day less"
    assert (runs / parked.run_id).exists(), "a run the orchestrator still wants keeps its record"


async def test_a_deployment_without_a_key_refuses_an_e2b_runtime_as_config(tmp_path: Path) -> None:
    with pytest.raises(SpawnError) as refused:
        await ClaudeE2bAdapter(state_root=tmp_path / "s").spawn(make_spec())
    assert refused.value.reason is SpawnReason.CONFIG


async def test_a_runtime_whose_key_was_removed_spawns_nowhere(tmp_path: Path) -> None:
    """The cached adapter outlives the key: kept, it would go on spawning into a team the
    reaper no longer lists."""
    adapter = ClaudeE2bAdapter(
        api_key="e2b_removed", template="factorial-claude:x", state_root=tmp_path / "s"
    )
    adapter.use_workspace(api_key=None, template="factorial-claude:x")
    with pytest.raises(SpawnError) as refused:
        await adapter.spawn(make_spec())
    assert refused.value.reason is SpawnReason.CONFIG


# --------------------------------------------------------------------------- the scripts


@pytest.mark.parametrize("path", sorted(SCRIPTS))
async def test_every_sandbox_script_is_valid_bash(tmp_path: Path, path: str) -> None:
    """The scripts are f-strings: a stray brace or quote breaks one only inside a sandbox. The
    hooks' script is POSIX `sh`, since a host's TUI runs it too."""
    shebang, _, _ = SCRIPTS[path].partition("\n")
    assert shebang in ("#!/bin/bash", "#!/bin/sh")
    script = tmp_path / "script.sh"
    script.write_text(SCRIPTS[path])
    checked = await asyncio.create_subprocess_exec(
        shebang.rpartition("/")[2], "-n", str(script), stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await checked.communicate()
    assert checked.returncode == 0, stderr.decode()


async def test_the_preview_scripts_are_installed_with_the_others() -> None:
    assert {PREVIEW, PREVIEW_STATUS, PREVIEW_STOP, FIREWALL} <= set(SCRIPTS)


async def test_the_tmux_lockdown_conf_is_installed_as_roots_and_read_only(
    tmp_path: Path,
) -> None:
    """tmux starts even when its `-f` file is missing, and then unlocked: the conf must be on
    disk wherever the scripts are."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    await adapter.spawn(make_spec())
    sandbox = client.only()
    assert sandbox.files[TMUX_CONF] == (TMUX_LOCKDOWN.encode(), "root")
    assert any(f"chmod 644 {TMUX_CONF}" in c.command for c in sandbox.commands)
    for task in adapter._supervisors.values():
        task.cancel()


# --------------------------------------------------------------------------- previews

SPEC = PreviewSpec(
    setup=("npm ci",),
    services=(PreviewServiceSpec(name="web", command="npm run dev", port=3000),),
    stop_after=timedelta(minutes=30),
)
PREVIEW_ENV_VALUES = {"ADMIN_PASSWORD": SecretStr("preview-only-password")}


async def kept(
    tmp_path: Path, client: FakeSandboxClient, clock: Clock | None = None
) -> tuple[ClaudeE2bAdapter, str]:
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec(public=True))
    await _finish_session(adapter, handle.run_id)
    return adapter, handle.run_id


async def test_the_e2b_adapter_is_a_preview_host(tmp_path: Path) -> None:
    assert isinstance(adapter_for(tmp_path, FakeSandboxClient()), PreviewHost)


async def test_a_preview_starts_cold_with_no_credential_and_becomes_ready(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    sandbox = client.only()

    started = await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    ready = await adapter.preview_status(run_id)

    assert started.phase is PreviewPhase.STARTING and started.token == "t1"
    assert sandbox.state == "running", "the start woke the parked sandbox"
    assert sandbox.network.inject == {}, "a preview never runs beside a credential rule"
    assert any(c.command == PREVIEW for c in sandbox.commands)
    env = sandbox.files[PREVIEW_ENV][0]
    assert b"ADMIN_PASSWORD=preview-only-password\0" in env
    assert b"GH_TOKEN" not in env
    assert sandbox.files[PREVIEW_SERVICES][0] == b"web\x003000\x00/\x00npm run dev\x00"
    [opened] = client.commands_by_name(FIREWALL)
    assert opened.envs == {"MODE": "open", "PORTS": "3000"}, "its ports, with no credential rule"
    assert ready.phase is PreviewPhase.READY
    assert [(u.service, u.url) for u in ready.urls] == [("web", "https://3000-sbx1.e2b.test")]
    assert ready.head_sha == "0123abc" and ready.uncommitted is False


async def test_a_start_with_the_same_token_starts_nothing_twice(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)

    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    assert [c.command for c in client.only().commands].count(PREVIEW) == 1


async def test_a_preview_is_refused_where_nothing_can_serve_it(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await _finish_session(adapter, handle.run_id)

    with pytest.raises(PreviewUnavailable, match="not kept for a preview"):
        await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")


async def test_a_preview_starts_beside_a_live_run_without_dropping_its_credentials(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)

    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    sandbox = client.only()
    assert sandbox.network.inject, "the live run's credential rules stand"
    assert sandbox.firewall == "contain"
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.READY
    await adapter.cancel(handle.run_id)


async def test_a_connect_beside_a_live_run_never_shortens_its_sandbox(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)

    await adapter.preview_start(
        handle.run_id,
        SPEC.model_copy(update={"stop_after": timedelta(minutes=5)}),
        PREVIEW_ENV_VALUES,
        token="t1",
    )

    assert client.connects[-1][1] >= 50 * 60, "the connect kept the run's own timeout"
    await adapter.cancel(handle.run_id)


async def test_a_runs_end_keeps_the_sandbox_awake_for_its_preview_without_credentials(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.READY

    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    sandbox = client.only()
    assert sandbox.state == "running", "the preview keeps it awake"
    assert sandbox.network.inject == {}, "with no credential rule left"
    assert sandbox.firewall == "open", "and the preview's egress back"
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.READY


async def test_a_rollover_pauses_and_records_the_preview_stopped_for_renewal(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)

    await adapter.interrupt(handle.run_id, ParkReason.ROLLOVER)
    await adapter.drain()

    assert client.only().state == "paused"
    stopped = await adapter.preview_status(handle.run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL


async def test_a_run_that_ends_at_its_sandboxs_cap_pauses_under_its_preview(
    tmp_path: Path,
) -> None:
    """E2B ends a sandbox an hour after its last wake, whatever is awake in it: a run that ends
    there leaves nothing awake to be destroyed, and the preview is stopped for its renewal."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)

    clock.now += adapter_module.MAX_SESSION - adapter_module.CAP_BACKSTOP
    await adapter.drain()

    result = await adapter.result(handle.run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.INTERRUPTED, SESSION_CAP_REASON)
    assert client.only().state == "paused" and client.killed == []
    stopped = await adapter.preview_status(handle.run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL


async def test_a_preview_stop_beside_a_live_run_leaves_the_run_alone(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    await adapter.preview_stop(handle.run_id)

    assert client.only().state == "running" and client.only().network.inject
    assert await adapter.status(handle.run_id) is RunStatus.RUNNING
    await adapter.cancel(handle.run_id)


async def test_a_preview_read_as_the_run_ends_never_writes_the_run_back_to_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beside a live run the preview and the supervisor keep one record: a status read the run's
    end lands in the middle of must not write back the run it read."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)
    sandbox = client.only()
    run = sandbox.run

    async def ran(command: str, **options: Any) -> CommandOutput:
        if command == PREVIEW_STATUS:
            await adapter.interrupt(handle.run_id)
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", ran)
    await adapter.preview_status(handle.run_id)

    state = _read_state(_run_dir(tmp_path, handle.run_id), handle.run_id)
    assert state.status is RunStatus.INTERRUPTED
    assert state.preview is not None and state.preview.phase is PreviewPhase.READY


async def test_a_preview_stop_that_outlasts_its_runs_end_parks_the_sandbox_and_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop that lands while the run ends waits for that end to decide the sandbox, then parks
    what the end kept awake for the preview. Deciding again after the end had parked it would
    park a paused sandbox, which fails, and the failed park kills the kept workspace."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)
    sandbox = client.only()
    run = sandbox.run
    released = asyncio.Event()
    stopping: list[asyncio.Future[None]] = []

    async def ran(command: str, **options: Any) -> CommandOutput:
        if command.startswith(SCRUB) and not stopping:
            stopping.append(asyncio.ensure_future(adapter.preview_stop(handle.run_id)))
            await asyncio.sleep(0)
        out = await run(command, **options)
        if command == PREVIEW_STOP:
            await released.wait()
        return out

    monkeypatch.setattr(sandbox, "run", ran)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    released.set()
    await stopping[0]

    assert client.killed == [], "the kept workspace was killed"
    state = _read_state(_run_dir(tmp_path, handle.run_id), handle.run_id)
    assert state.kept is True and sandbox.state == "paused"
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.STOPPED


async def test_a_preview_stop_that_ends_inside_its_runs_end_leaves_the_sandbox_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop whose own end would finish while the run's end is still scrubbing waits for that
    end. Settled at once, it would see the run still live and leave the sandbox to the end, which
    had already decided to keep it awake for the preview: an awake, empty sandbox nothing parks."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(handle.run_id)
    sandbox = client.only()
    run = sandbox.run
    stopping: list[asyncio.Future[None]] = []

    async def ran(command: str, **options: Any) -> CommandOutput:
        if command.startswith(SCRUB) and not stopping:
            stopping.append(asyncio.ensure_future(adapter.preview_stop(handle.run_id)))
            for _ in range(50):
                await asyncio.sleep(0)
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", ran)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    await stopping[0]

    assert client.killed == [], "the kept workspace was killed"
    state = _read_state(_run_dir(tmp_path, handle.run_id), handle.run_id)
    assert state.kept is True and sandbox.state == "paused", sandbox.state


async def test_a_preview_start_beside_a_run_that_ends_meanwhile_leaves_the_workspace_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that ends while a Start beside it loads the firewall decides the sandbox once the
    start is on record: it parks it for the preview's renewal. Deciding in between left the start
    to write into a paused sandbox, and the start's failed park killed the kept workspace."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    sandbox = client.only()
    run = sandbox.run
    deciding = asyncio.Event()
    ending: list[asyncio.Future[None]] = []

    async def discovered(*args: Any, **kwargs: Any) -> tuple[()]:
        deciding.set()  # the run's end decides its sandbox next
        return ()

    async def ran(command: str, **options: Any) -> CommandOutput:
        out = await run(command, **options)
        if command == FIREWALL and not ending:
            ending.append(asyncio.ensure_future(adapter.interrupt(handle.run_id)))
            await asyncio.wait_for(deciding.wait(), 10)
        return out

    monkeypatch.setattr(adapter_module, "discover_artifacts", discovered)
    monkeypatch.setattr(sandbox, "run", ran)
    # The workspace is asserted first; whether the start landed, last.
    with contextlib.suppress(PreviewUnavailable):
        await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await ending[0]
    await adapter.drain()

    assert client.killed == [], "the kept workspace was killed"
    state = _read_state(_run_dir(tmp_path, handle.run_id), handle.run_id)
    assert state.kept is True and sandbox.state == "paused"
    renewed = await adapter.preview_status(handle.run_id)
    assert renewed.phase is PreviewPhase.STOPPED and renewed.detail == PREVIEW_RENEWAL


async def test_a_runs_end_restarts_a_preview_still_in_its_setup_rather_than_fail_it(
    tmp_path: Path,
) -> None:
    """Setup runs as the agent's uid, which the run's scrub stops: the preview is parked for its
    renewal, and the worker starts it again, rather than read the cut setup as a failure."""
    client = FakeSandboxClient(
        scripts=[Script(tui=TuiScript())],
        preview_reports=["setup running"],
    )
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.STARTING

    await adapter.interrupt(handle.run_id)
    await adapter.drain()

    stopped = await adapter.preview_status(handle.run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL
    assert stopped.urls == () and client.only().state == "paused"


async def test_a_failed_setup_reports_its_log_and_parks_the_sandbox(tmp_path: Path) -> None:
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], preview_reports=["setup failed\nnpm ERR! boom"]
    )
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    failed = await adapter.preview_status(run_id)

    assert failed.phase is PreviewPhase.FAILED
    assert failed.detail is not None and "npm ERR! boom" in failed.detail
    assert client.only().state == "paused"
    assert client.only().network.inject == {}


PREVIEW_GONE = "the preview's start script ended before setup finished"
START_LEFT = (
    "2026-10-19T14:02:11Z regrouping the checkout\n"
    "find: './.next/cache/x': No such file or directory\n"
    "2026-10-19T14:02:19Z exit 1 during regrouping the checkout\n"
)


@pytest.mark.parametrize(
    ("left", "detail"),
    [
        (START_LEFT, f"{PREVIEW_GONE}:\n{START_LEFT.strip()}"),
        ("", PREVIEW_GONE),
    ],
    ids=["its-logs", "no-logs"],
)
async def test_a_start_script_gone_before_its_setup_finished_fails_with_what_it_left(
    tmp_path: Path, left: str, detail: str
) -> None:
    """Nothing marks a setup that a start never reached or a kill cut, so the start's own logs
    are all that can say which it was."""
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], preview_reports=["setup running"], preview_left=left
    )
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.STARTING

    client.only().end_preview()
    failed = await adapter.preview_status(run_id)

    assert failed.phase is PreviewPhase.FAILED
    assert failed.detail == detail
    assert client.only().state == "paused"


async def test_a_service_that_never_answers_fails_after_its_ready_timeout(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], preview_reports=["serving\nhead 1"]
    )
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.STARTING
    clock.now += timedelta(seconds=121)
    failed = await adapter.preview_status(run_id)

    assert failed.phase is PreviewPhase.FAILED
    assert failed.detail is not None and "did not answer / on port 3000" in failed.detail


async def test_a_preview_past_stop_after_is_parked_not_left_to_e2b(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY

    clock.now += timedelta(minutes=31)
    stopped = await adapter.preview_status(run_id)

    assert stopped.phase is PreviewPhase.STOPPED and stopped.urls == ()
    assert client.only().state == "paused"
    assert client.killed == []


async def test_a_preview_closes_for_idle_at_the_keep_until_the_worker_gives(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    keep = clock.now + timedelta(minutes=40)
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.READY

    clock.now += timedelta(minutes=35)
    kept_on = await adapter.preview_status(run_id)
    assert kept_on.phase is PreviewPhase.READY, "past stopAfter, kept"
    clock.now += timedelta(minutes=6)
    stopped = await adapter.preview_status(run_id)

    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_IDLE
    assert client.only().state == "paused"


async def test_a_read_without_a_keep_until_keeps_the_last_one_given(tmp_path: Path) -> None:
    """A read while setup runs changes nothing else, and must still keep the one it was given."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())], preview_reports=["setup running"])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    keep = clock.now + timedelta(minutes=10)
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.STARTING

    clock.now += timedelta(minutes=11)
    stopped = await adapter.preview_status(run_id)

    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_IDLE


async def test_a_kept_preview_renews_its_sandbox_at_the_session_cap(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    keep = clock.now + timedelta(hours=2)

    clock.now += adapter_module.MAX_SESSION - adapter_module.SESSION_MARGIN
    renewed = await adapter.preview_status(run_id, keep_until=keep)

    assert renewed.phase is PreviewPhase.STOPPED and renewed.detail == PREVIEW_RENEWAL
    assert client.only().state == "paused" and client.killed == []


async def test_a_cap_park_that_fails_says_the_sandbox_was_killed(tmp_path: Path) -> None:
    """The worker starts a preview again after a renewal, which needs the sandbox: one killed for a
    pause that failed must not read as a clean renewal."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    keep = clock.now + timedelta(hours=2)
    clock.now += adapter_module.MAX_SESSION - adapter_module.SESSION_MARGIN
    client.pause_error = RuntimeError("E2B answered 500")

    stopped = await adapter.preview_status(run_id, keep_until=keep)

    assert client.killed == [client.only().sandbox_id]
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail != PREVIEW_RENEWAL
    assert stopped.detail is not None and "killed" in stopped.detail


async def test_keeping_a_preview_extends_its_sandbox_timeout(tmp_path: Path) -> None:
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    connects = len(client.connects)
    keep = timedelta(minutes=50)

    await adapter.preview_status(run_id, keep_until=clock.now + keep)

    assert len(client.connects) == connects + 1, "one connect set the longer timeout"
    asked = keep + adapter_module.SESSION_MARGIN
    assert client.connects[-1][1] == int(asked.total_seconds()), "the keep's, and the margin"


class TickingClock(Clock):
    """A second later at every read, as a real clock is between the adapter's reads of it."""

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


async def test_the_first_read_after_a_start_makes_no_connect(tmp_path: Path) -> None:
    """The start's connect asked for its deadline already, though the clock moved on between the
    two: the first read takes nothing for a later deadline."""
    clock = TickingClock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    connects = len(client.connects)

    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY

    assert len(client.connects) == connects


async def test_a_keep_past_the_session_cap_extends_the_sandbox_timeout_once(
    tmp_path: Path,
) -> None:
    """E2B's timeout never reaches past the cap, so a keep beyond it is met by one connect, not
    one per read, whatever fraction of a second the reads fall on."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    woke = clock.now
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    keep = clock.now + timedelta(hours=2)
    connects = len(client.connects)
    reads: list[datetime] = []

    for _ in range(3):
        clock.now += timedelta(minutes=5, microseconds=500_000)
        reads.append(clock.now)
        await adapter.preview_status(run_id, keep_until=keep)

    assert len(client.connects) == connects + 1
    capped = woke + adapter_module.MAX_SESSION - reads[0]
    assert client.connects[-1][1] == int(capped.total_seconds()), "the cap's, not the keep's"


async def test_a_preview_beside_a_live_run_leaves_the_sandbox_timeout_to_the_run(
    tmp_path: Path,
) -> None:
    """While its run is live the worker's keep moves with every read. The run's own timeout holds
    the sandbox until its end, so no read connects to stretch it."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    connects = len(client.connects)

    for _ in range(3):
        clock.now += timedelta(minutes=1)
        keep = clock.now + SPEC.stop_after
        assert (await adapter.preview_status(handle.run_id, keep_until=keep)).phase is (
            PreviewPhase.READY
        )

    assert len(client.connects) == connects
    await adapter.cancel(handle.run_id)


async def test_a_restarted_worker_never_wakes_a_sandbox_something_else_parked(
    tmp_path: Path,
) -> None:
    """The state file says the preview is starting; the sandbox says it is paused. `connect`
    would resume it, so the adapter looks with `info` and records the preview stopped."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    first, run_id = await kept(tmp_path, client)
    await first.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await client.pause(client.only().sandbox_id)
    connects = len(client.connects)

    second = adapter_for(tmp_path, client)
    state = await second.preview_status(run_id)

    assert state.phase is PreviewPhase.STOPPED
    assert len(client.connects) == connects, "reading a parked preview must not wake it"
    assert client.only().state == "paused"


async def test_a_restarted_worker_reattaches_to_a_serving_preview(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    first, run_id = await kept(tmp_path, client)
    await first.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    second = adapter_for(tmp_path, client)

    assert (await second.preview_status(run_id)).phase is PreviewPhase.READY


async def test_stop_parks_and_is_idempotent(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    await adapter.preview_stop(run_id)
    await adapter.preview_stop(run_id)

    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.STOPPED
    assert client.only().state == "paused"
    assert client.paused.count(client.only().sandbox_id) == 2, "one park at terminal, one at stop"


async def test_a_resume_keeps_the_preview_and_contains_it_before_writing_credentials(
    tmp_path: Path,
) -> None:
    """Agent-written servers are public in a kept sandbox. Once the proxy holds the run's
    credentials again, the preview's uid must already be kept off it (design.md §7.10)."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript()), Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(handle.run_id)).phase is PreviewPhase.READY

    await adapter.resume(handle.run_id, "carry on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})

    sandbox = client.only()
    assert sandbox.network.inject and sandbox.contained_at_network[-1] is True
    assert sandbox.serving_at_network[-1] is True, "the preview served throughout"
    await _finish_session(adapter, handle.run_id)


async def test_a_resume_restarts_a_preview_still_in_its_setup_rather_than_fail_it(
    tmp_path: Path,
) -> None:
    """Setup runs as the agent's uid, which a resume stops before it launches: the preview is
    stopped first and recorded for its renewal, so the worker starts it again."""
    client = FakeSandboxClient(
        scripts=[Script(tui=TuiScript()), Script(tui=TuiScript())],
        preview_reports=["setup running"],
    )
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    sandbox = client.only()
    began = len(sandbox.commands)

    await adapter.resume(handle.run_id, "carry on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})
    try:
        stopped = await adapter.preview_status(handle.run_id)
        assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL
        commands = [c.command for c in sandbox.commands[began:]]
        assert commands.index(PREVIEW_STOP) < commands.index(END_SHELL), "before the cut"
    finally:
        await adapter.cancel(handle.run_id)


async def test_a_wake_from_a_pause_opens_a_new_window_whatever_was_last_recorded(
    tmp_path: Path,
) -> None:
    """A park whose record was never written, the reaper's, leaves an old wake on disk: a wake
    from the pause must not ask E2B for what is left of that one."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client, clock)
    run_dir = _run_dir(tmp_path, run_id)
    state = _read_state(run_dir, run_id)
    state.awake_since = clock.now - timedelta(hours=2)
    _write_state(run_dir, state)

    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    wanted = SPEC.stop_after + adapter_module.SESSION_MARGIN
    assert client.connects[-1][1] == int(wanted.total_seconds())
    assert _read_state(run_dir, run_id).awake_since == clock.now


async def test_a_serving_preview_survives_a_missed_probe(tmp_path: Path) -> None:
    """A dev server busy compiling a page misses one 2s probe; the reviewer's frame must not die
    for it."""
    ready, miss = "serving\nhead 1\nready web", "serving\nhead 1"
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], preview_reports=[ready, miss, ready]
    )
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    phases = [(await adapter.preview_status(run_id)).phase for _ in range(3)]

    assert phases == [PreviewPhase.READY] * 3
    assert client.only().state == "running"


async def test_a_serving_preview_fails_after_three_missed_probes_in_a_row(tmp_path: Path) -> None:
    ready, miss = "serving\nhead 1\nready web", "serving\nhead 1"
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())], preview_reports=[ready, miss])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    phases = [(await adapter.preview_status(run_id)).phase for _ in range(4)]

    assert phases == [*[PreviewPhase.READY] * 3, PreviewPhase.FAILED]
    assert client.only().state == "paused"


async def test_a_park_deletes_the_preview_env_so_no_value_stays_on_a_paused_disk(
    tmp_path: Path,
) -> None:
    """`preview.env` values are secrets the operator marked preview-only; a paused sandbox still
    holds no credential file (decision 25)."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    await adapter.preview_stop(run_id)

    scrubs = [c.command for c in client.only().commands if c.command.startswith(SCRUB)]
    assert PREVIEW_ENV in scrubs[-1] and PREVIEW_SETUP_ENV in scrubs[-1]


async def test_a_new_start_stops_the_preview_it_replaces_first(tmp_path: Path) -> None:
    """Two supervisors in one sandbox would fight over the same ports."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(run_id)

    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t2")

    commands = [c.command for c in client.only().commands]
    starts = [i for i, command in enumerate(commands) if command == PREVIEW]
    assert len(starts) == 2
    stopped = commands[starts[0] : starts[1]]
    assert PREVIEW_STOP in stopped, "the first preview is stopped before the second"


async def test_a_preview_whose_sandbox_was_destroyed_reports_it_instead_of_raising(
    tmp_path: Path,
) -> None:
    """E2B's console or its timeout can kill the sandbox under a cached handle."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
    client.gone.add(client.only().sandbox_id)

    state = await adapter.preview_status(run_id)

    assert state.phase is PreviewPhase.FAILED
    assert state.detail is not None and "gone" in state.detail


async def test_a_public_spawn_loads_the_firewall_with_its_declared_ports(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)

    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await _finish_session(adapter, handle.run_id)

    [load] = client.commands_by_name(FIREWALL)
    assert load.envs == {"MODE": "contain", "PORTS": "3000"}


async def test_a_private_spawn_loads_no_firewall(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)

    handle = await adapter.spawn(make_spec())
    await _finish_session(adapter, handle.run_id)

    assert client.commands_by_name(FIREWALL) == []


async def test_a_public_spawn_contains_the_preview_before_the_agent_starts(
    tmp_path: Path,
) -> None:
    """The create wrote the run's credential rules: the firewall goes in before anything of the
    agent's runs, so nothing it starts as the preview's uid runs a moment uncontained."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)

    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    try:
        commands = [c.command for c in client.only().commands]
        assert commands.index(FIREWALL) < commands.index(layout.SESSION)
    finally:
        await adapter.cancel(handle.run_id)


async def test_a_preview_is_on_record_with_its_ports_before_its_supervisor_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dies as the supervisor starts must leave a preview the next credential write
    contains: a run kept before its ports were recorded has them on disk first (`_arm`)."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    sandbox = client.only()
    start = sandbox.start
    on_disk: list[adapter_module._RunState] = []

    async def started(command: str, **options: Any) -> int:
        if command == PREVIEW:
            on_disk.append(_read_state(_run_dir(tmp_path, run_id), run_id))
        return await start(command, **options)

    monkeypatch.setattr(sandbox, "start", started)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    [state] = on_disk
    assert state.public_ports == (3000,)
    assert state.preview is not None and state.preview.token == "t1"
    assert state.preview.phase is PreviewPhase.STARTING


async def test_a_preview_whose_supervisor_never_starts_fails_and_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    sandbox = client.only()

    async def refused(command: str, **options: Any) -> int:
        raise RuntimeError("E2B 500")

    monkeypatch.setattr(sandbox, "start", refused)
    with pytest.raises(PreviewUnavailable, match="E2B 500"):
        await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    failed = await adapter.preview_status(run_id)
    assert failed.phase is PreviewPhase.FAILED and failed.token == "t1"
    assert sandbox.state == "paused" and sandbox.network.inject == {}


@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside-a-live-run"])
async def test_a_replaced_previews_setup_is_stopped_only_where_nothing_else_of_the_agents_runs(
    tmp_path: Path, beside: bool
) -> None:
    """Setup runs as the agent's uid, which the preview's own stop leaves alone. With nothing else
    of the agent's running, STOP ends it; beside a live run, STOP would end the agent too."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript() if beside else at_prompt())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    if beside:
        await asyncio.sleep(0)
    else:
        await _finish_session(adapter, handle.run_id)
    await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    sandbox = client.only()
    began = len(sandbox.commands)
    try:
        await adapter.preview_start(handle.run_id, SPEC, PREVIEW_ENV_VALUES, token="t2")

        commands = [c.command for c in sandbox.commands[began:]]
        replaced = commands[: commands.index(PREVIEW)]
        stop = replaced.index(PREVIEW_STOP)
        assert any(c.startswith("chmod 755") for c in replaced[:stop]), "today's scripts, first"
        assert (STOP in replaced) is not beside
        expected = RunStatus.RUNNING if beside else RunStatus.SUCCEEDED
        assert await adapter.status(handle.run_id) is expected
    finally:
        await adapter.cancel(handle.run_id)
        await adapter.drain()


async def _parked_public(tmp_path: Path) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript()), Script(tui=at_prompt())])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(public=True, public_ports=(3000,)))
    await asyncio.sleep(0)
    await adapter.interrupt(handle.run_id)
    await adapter.drain()
    return adapter, client, handle.run_id


async def test_every_credential_rule_is_written_with_the_preview_contained(tmp_path: Path) -> None:
    """A resume wakes a paused sandbox whose firewall the pause cleared: the containment goes in
    again before the proxy holds the run's credentials (design.md §7.10)."""
    adapter, client, run_id = await _parked_public(tmp_path)

    await adapter.resume(run_id, "carry on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})

    sandbox = client.only()
    updates = zip(sandbox.networks[1:], sandbox.contained_at_network, strict=True)
    written = [contained for network, contained in updates if network.inject]
    assert written and all(written)
    await _finish_session(adapter, run_id)


async def test_a_firewall_that_does_not_load_stops_the_credential_write(tmp_path: Path) -> None:
    """Fail closed: no credential rule goes in beside a preview uid the firewall did not contain,
    and the woken sandbox is parked again."""
    adapter, client, run_id = await _parked_public(tmp_path)
    client.firewall_exit = 1

    with pytest.raises(AdapterUnavailable, match="loading the firewall"):
        await adapter.resume(run_id, "carry on", {"ANTHROPIC_API_KEY": SecretStr(API_KEY)})

    sandbox = client.only()
    assert [network for network in sandbox.networks[1:] if network.inject] == []
    assert sandbox.state == "paused"


async def test_setup_gets_no_preview_value_and_the_services_get_them_all(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)

    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    files = client.only().files
    assert b"preview-only-password" not in files[PREVIEW_SETUP_ENV][0]
    assert b"HOME=/home/agent\0" in files[PREVIEW_SETUP_ENV][0]
    assert b"ADMIN_PASSWORD=preview-only-password\0" in files[PREVIEW_ENV][0]
    assert b"HOME=/home/preview\0" in files[PREVIEW_ENV][0]


async def test_a_preview_stop_ends_the_preview_user_not_the_agent(tmp_path: Path) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt())])
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")
    await adapter.preview_status(run_id)

    await adapter.preview_stop(run_id)

    names = [c.command for c in client.only().commands]
    assert PREVIEW_STOP in names
    assert STOP not in names[names.index(PREVIEW) :], "the stop never sends the agent's STOP"


async def test_a_port_held_by_another_user_fails_the_preview_instead_of_publishing_it(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], preview_reports=["serving\nhead 0123abc\nheld web"]
    )
    adapter, run_id = await kept(tmp_path, client)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="t1")

    failed = await adapter.preview_status(run_id)

    assert failed.phase is PreviewPhase.FAILED and failed.urls == ()
    assert failed.detail is not None and "port 3000 is held by another user" in failed.detail


# --------------------------------------------------------------------------- session views


LIMIT = timedelta(minutes=55)
#: What the worker hands a view: the model credential, and a GitHub token it must not pass on.
VIEW_CREDENTIALS = {"GH_TOKEN": SecretStr(TOKEN), "ANTHROPIC_API_KEY": SecretStr(API_KEY)}
DOCS = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.example.com/docs",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "state" / "runs" / run_id


async def session_kept(
    tmp_path: Path,
    clock: Clock | None = None,
    *,
    public: bool = False,
    view: TuiScript | None = None,
    view_opens: bool = True,
    **spec: object,
) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    """A session that finished, its sandbox parked holding it. `view` is the view's CLI."""
    client = FakeSandboxClient(
        scripts=[Script(tui=at_prompt())], view=view or TuiScript(), view_opens=view_opens
    )
    adapter = adapter_for(tmp_path, client, clock)
    spawned = make_spec(public=public, mcp=(DOCS,), **spec)
    run_id = (await adapter.spawn(spawned)).run_id
    await _finish_session(adapter, run_id)
    return adapter, client, run_id


def _nul_file(sandbox: FakeSandbox, path: str) -> list[str]:
    return sandbox.files[path][0].decode().split("\0")[:-1]


async def test_a_view_resumes_the_session_with_the_model_credential_alone(tmp_path: Path) -> None:
    """The CLI comes back on its own session at its prompt, its key set at egress as a resume's
    is, but no GitHub header or token and no MCP server: nobody can type into it, and whatever
    it does reaches no repository (decision 57)."""
    adapter, client, run_id = await session_kept(tmp_path)
    sandbox = client.only()
    session = _read_state(_run_dir(tmp_path, run_id), run_id).session_id
    launch = {path: sandbox.files[path] for path in (ARGV, ENV)}
    began = len(sandbox.commands)

    opened = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )

    assert opened.phase is InspectPhase.OPEN and sandbox.view_open
    assert sandbox.network.inject == {"api.anthropic.com": {"x-api-key": API_KEY}}
    assert sandbox.view_at_network[-1] is False, "the rule went in before the view's CLI"
    argv = _nul_file(sandbox, VIEW_ARGV)
    assert argv[:3] == ["claude", "--resume", session] and "--" not in argv
    assert "--strict-mcp-config" in argv and not [a for a in argv if "--mcp-config" in a]
    env = _nul_file(sandbox, VIEW_ENV)
    assert not [line for line in env if line.startswith(("GH_", "GITHUB"))]
    assert "FACTORIAL_SIGNALS=/dev/null" in env, "its hooks append nowhere the run reads"
    assert {path: sandbox.files[path] for path in (ARGV, ENV)} == launch, "the session's own"
    assert [c.user for c in sandbox.commands[began:] if c.command == INSPECT] == ["root"]
    after = "\n".join(
        [c.command for c in sandbox.commands[began:]]
        + [v for c in sandbox.commands[began:] for v in c.envs.values()]
        + [content.decode(errors="replace") for content, _ in sandbox.files.values()]
    )
    for secret in (TOKEN, API_KEY, DOCS_TOKEN):
        assert secret not in after


async def test_a_views_git_runs_no_hook_or_fsmonitor_the_checkout_names(tmp_path: Path) -> None:
    """The view runs beside the model key in a checkout whose git config the agent wrote: its
    env turns hooks and fsmonitor off above that config (`view_git_env`)."""
    adapter, client, run_id = await session_kept(tmp_path)

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    env = dict(line.split("=", 1) for line in _nul_file(client.only(), VIEW_ENV))
    entries = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env.get("GIT_CONFIG_COUNT", "0")))
    ]
    assert entries[-2:] == [("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null")]


async def test_a_views_credential_rule_is_written_with_the_preview_contained(
    tmp_path: Path,
) -> None:
    """A view's model credential sits behind the egress proxy of a sandbox a preview serves
    from: the preview's uid is contained before the rule goes in (design.md §7.10)."""
    adapter, client, run_id = await session_kept(tmp_path, public=True, public_ports=(3000,))

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    sandbox = client.only()
    assert sandbox.network.inject and sandbox.contained_at_network[-1] is True


@pytest.mark.parametrize("end", ["stop", "limit", "exit", "replaced"])
async def test_a_view_leaves_the_session_log_and_signals_as_it_found_them(
    tmp_path: Path, end: str
) -> None:
    """A view takes no turn: what its CLI writes on its way out, claude's cost, must not be read
    as the session's by a later rework or resume, whichever way the view ends."""
    clock = Clock()
    view = TuiScript(on_exit=([_hook("SessionEnd")], [_cost(9.99)]))
    adapter, client, run_id = await session_kept(tmp_path, clock, view=view)
    sandbox = client.only()
    log = session_log(_read_state(_run_dir(tmp_path, run_id), run_id).session_id or "")
    before, signals = sandbox.logs[log], sandbox.signals_file
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    if end == "stop":
        await adapter.inspect_stop(run_id)
    elif end == "limit":
        clock.now += LIMIT + timedelta(minutes=2)
        await adapter.inspect_status(run_id)
    elif end == "exit":
        sandbox.close_view()
        await adapter.inspect_status(run_id)
    else:
        await adapter.inspect_start(run_id, token="r2", limit=LIMIT, credentials=VIEW_CREDENTIALS)
        assert sandbox.logs[log] == before
        await adapter.inspect_stop(run_id)

    assert sandbox.logs[log] == before
    assert sandbox.signals_file == signals
    assert sandbox.state == "paused" and not sandbox.view_open
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.STOPPED


async def test_a_session_that_never_began_has_no_view(tmp_path: Path) -> None:
    adapter, client, run_id = await session_kept(tmp_path)
    state = _read_state(_run_dir(tmp_path, run_id), run_id)
    state.session_id = None
    _write_state(_run_dir(tmp_path, run_id), state)
    with pytest.raises(InspectNoSession):
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    assert INSPECT not in [c.command for c in client.only().commands]


async def test_a_view_without_the_model_credential_is_refused_before_anything_wakes(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await session_kept(tmp_path)
    connects = len(client.connects)

    with pytest.raises(InspectUnavailable, match="No ANTHROPIC_API_KEY"):
        await adapter.inspect_start(
            run_id, token="r1", limit=LIMIT, credentials={"GH_TOKEN": SecretStr(TOKEN)}
        )
    assert len(client.connects) == connects and client.only().state == "paused"


async def test_the_view_is_the_root_tmux_servers_inspect_session(tmp_path: Path) -> None:
    adapter, client, run_id = await session_kept(tmp_path)

    opened = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )

    assert f"-f {TMUX_CONF} new-session -d -s inspect" in SCRIPTS[INSPECT]
    assert opened.target is not None
    assert TerminalTarget.model_validate(opened.target.model_dump()) == opened.target
    assert (opened.target.substrate, opened.target.session) == ("e2b", "inspect")
    assert opened.target.sandbox_id == client.only().sandbox_id


async def test_inspect_start_is_idempotent_on_its_token(tmp_path: Path) -> None:
    clock = Clock()
    adapter, client, run_id = await session_kept(tmp_path, clock)

    first = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )
    again = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )

    assert [c.command for c in client.only().commands].count(INSPECT) == 1
    assert again == first and again.started_at == clock.now
    replaced = await adapter.inspect_start(
        run_id, token="r2", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )
    commands = [c.command for c in client.only().commands]
    assert replaced.token == "r2" and commands.count(INSPECT) == 2
    after = commands[commands.index(INSPECT) :]
    assert any(c.startswith(INSPECT_STOP) for c in after), "the first view ends first"


async def test_inspect_status_reports_clients_and_last_activity(tmp_path: Path) -> None:
    clock = Clock()
    adapter, client, run_id = await session_kept(tmp_path, clock)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    client.view_clients = 2
    clock.now += timedelta(minutes=3)

    state = await adapter.inspect_status(run_id)

    assert (state.phase, state.clients, state.token) == (InspectPhase.OPEN, 2, "r1")
    assert state.started_at == clock.now - timedelta(minutes=3)
    assert state.last_activity == clock.now, "a client attached now is activity now"


@pytest.mark.parametrize("why", ["running", "continued", "not kept"])
async def test_a_view_is_refused_while_the_agent_runs_or_once_another_run_has_the_workspace(
    tmp_path: Path, why: str
) -> None:
    adapter, client, run_id = await session_kept(tmp_path)
    state = _read_state(_run_dir(tmp_path, run_id), run_id)
    if why == "running":
        state.status = RunStatus.RUNNING
    elif why == "continued":
        state.continued_by = "GH-12-plan-agent-later"
    else:
        state.kept = False
    _write_state(_run_dir(tmp_path, run_id), state)

    error = UnknownRunError if why == "not kept" else InspectUnavailable
    with pytest.raises(error):
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    assert INSPECT not in [c.command for c in client.only().commands]


async def test_inspect_status_never_wakes_a_paused_sandbox(tmp_path: Path) -> None:
    first, client, run_id = await session_kept(tmp_path)
    await first.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    await client.pause(client.only().sandbox_id)
    connects = len(client.connects)

    state = await adapter_for(tmp_path, client).inspect_status(run_id)

    assert state.phase is InspectPhase.STOPPED and state.token == "r1"
    assert len(client.connects) == connects, "reading a parked view must not wake it"
    assert client.only().state == "paused"


async def test_inspect_stop_parks_and_is_idempotent(tmp_path: Path) -> None:
    adapter, client, run_id = await session_kept(tmp_path)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    paused = len(client.paused)

    await adapter.inspect_stop(run_id)
    await adapter.inspect_stop(run_id)

    sandbox = client.only()
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.STOPPED
    assert sandbox.state == "paused" and not sandbox.view_open
    assert len(client.paused) == paused + 1
    assert sandbox.network.inject == {}
    (stop,) = [c for c in sandbox.commands if c.command.startswith(INSPECT_STOP)]
    assert f"'{VIEW_ENV}'" in stop.command, "the view's own launch file goes with it"
    scrub = [c.command for c in sandbox.commands if c.command.startswith(SCRUB)][-1]
    assert f"'{VIEW_ENV}'" in scrub, "and a park's scrub deletes it too"


async def test_a_view_whose_cli_exits_is_stopped_and_parks(tmp_path: Path) -> None:
    adapter, client, run_id = await session_kept(tmp_path)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    client.only().close_view()

    state = await adapter.inspect_status(run_id)

    assert state.phase is InspectPhase.STOPPED and state.detail == "the session view exited"
    assert client.only().state == "paused"


async def test_a_view_that_never_opens_fails_and_parks(tmp_path: Path) -> None:
    adapter, client, run_id = await session_kept(tmp_path, view_opens=False)

    state = await adapter.inspect_start(
        run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS
    )

    assert state.phase is InspectPhase.FAILED and state.target is None
    assert client.only().state == "paused"


async def test_a_view_past_its_limit_is_parked_by_its_own_status_read(tmp_path: Path) -> None:
    """E2B kills the sandbox, and the kept workspace with it, at its timeout: the limit's
    margin after the start."""
    clock = Clock()
    adapter, client, run_id = await session_kept(tmp_path, clock)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    (_, timeout) = client.connects[-1]
    assert timeout == int((LIMIT + timedelta(minutes=5)).total_seconds())

    clock.now += LIMIT + timedelta(minutes=2)
    state = await adapter.inspect_status(run_id)

    assert state.phase is InspectPhase.STOPPED
    assert client.only().state == "paused" and client.killed == []


async def test_a_view_and_a_preview_run_together_with_the_preview_contained(
    tmp_path: Path,
) -> None:
    """The view's egress carries the model key; the preview's uid is kept to loopback while it
    does, so both can be awake (design.md §7.10)."""
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    sandbox = client.only()
    assert sandbox.network.inject, "the view wrote its model key rule"
    assert sandbox.contained_at_network[-1] is True, "with the preview contained first"
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.OPEN


async def test_a_preview_an_older_adapter_started_is_contained_before_a_view_writes_its_key(
    tmp_path: Path,
) -> None:
    """A run kept before its ports were recorded may hold a preview an older adapter started:
    its ports are the preview's own, which the firewall contains before the key goes in."""
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    await adapter.preview_status(run_id)
    run_dir = _run_dir(tmp_path, run_id)
    state = _read_state(run_dir, run_id)
    state.public_ports = ()
    _write_state(run_dir, state)

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    sandbox = client.only()
    assert sandbox.network.inject and sandbox.contained_at_network[-1] is True
    assert client.commands_by_name(FIREWALL)[-1].envs == {"MODE": "contain", "PORTS": "3000"}


async def test_a_preview_starts_beside_an_open_view_contained_and_keeps_its_key(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")

    sandbox = client.only()
    assert sandbox.network.inject == {"api.anthropic.com": {"x-api-key": API_KEY}}
    assert sandbox.firewall == "contain"
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.OPEN


async def test_a_views_end_beside_a_preview_drops_the_model_key_and_keeps_serving(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    await adapter.preview_status(run_id)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    await adapter.inspect_stop(run_id)

    sandbox = client.only()
    assert sandbox.state == "running" and sandbox.network.inject == {}
    assert sandbox.firewall == "open"
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY


@pytest.mark.parametrize("end", ["run", "view"])
async def test_the_preview_gets_its_egress_back_only_once_no_credential_rule_stands(
    tmp_path: Path, end: str
) -> None:
    """A run's or a view's end beside a preview drops the header rules first, and only then
    lifts the containment: never a moment of both (design.md §7.10)."""
    if end == "run":
        client = FakeSandboxClient(scripts=[Script(tui=TuiScript())])
        adapter = adapter_for(tmp_path, client)
        run_id = (await adapter.spawn(make_spec(public=True, public_ports=(3000,)))).run_id
        await asyncio.sleep(0)
        await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
        await adapter.preview_status(run_id)
        await adapter.interrupt(run_id)
        await adapter.drain()
    else:
        adapter, client, run_id = await session_kept(tmp_path, public=True)
        await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
        await adapter.preview_status(run_id)
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
        await adapter.inspect_stop(run_id)

    sandbox = client.only()
    assert sandbox.firewall == "open" and sandbox.state == "running"
    assert sandbox.injected_at_open and not any(sandbox.injected_at_open)


async def test_a_failed_view_close_beside_a_preview_is_scrubbed_before_its_egress_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A view's close can fail and leave its CLI and the credential files behind: the sandbox a
    serving preview keeps awake is scrubbed before the preview's egress comes back."""
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    await adapter.preview_status(run_id)
    sandbox = client.only()
    run, start = sandbox.run, sandbox.start

    async def ran(command: str, **options: Any) -> CommandOutput:
        if command.startswith(INSPECT_STOP):
            return CommandOutput(1, "", "tmux would not answer")
        return await run(command, **options)

    async def started(command: str, **options: Any) -> int:
        if command == INSPECT:
            raise RuntimeError("E2B 500")
        return await start(command, **options)

    monkeypatch.setattr(sandbox, "run", ran)
    monkeypatch.setattr(sandbox, "start", started)
    began = len(sandbox.commands)

    with pytest.raises(InspectUnavailable):
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    after = sandbox.commands[began:]
    opens = [i for i, c in enumerate(after) if c.command == FIREWALL and c.envs["MODE"] == "open"]
    opened = opens[-1]
    scrubs = [i for i, c in enumerate(after) if c.command.startswith(SCRUB)]
    assert scrubs and scrubs[0] < opened, "scrubbed before the egress came back"
    for path in adapter.harness.credential_paths:
        assert f"'{path}'" in after[scrubs[0]].command
    assert sandbox.state == "running" and sandbox.network.inject == {}


async def test_a_views_end_restarts_a_preview_still_in_its_setup_rather_than_fail_it(
    tmp_path: Path,
) -> None:
    """Setup runs as the agent's uid, which a view's stop ends with the view's CLI: the preview
    is stopped first and recorded for its renewal, so the worker starts it again."""
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    client.preview_reports = ["setup running"]
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    sandbox = client.only()
    began = len(sandbox.commands)

    await adapter.inspect_stop(run_id)

    stopped = await adapter.preview_status(run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL
    commands = [c.command for c in sandbox.commands[began:]]
    cut = next(i for i, command in enumerate(commands) if command.startswith(INSPECT_STOP))
    assert PREVIEW_STOP in commands[:cut], "stopped before the view's stop cut its setup"
    assert sandbox.state == "paused"


#: How far into its preview's window a view or a session comes with `RENEW_FLOOR` of it left.
AT_THE_FLOOR = (
    adapter_module.MAX_SESSION - adapter_module.SESSION_MARGIN - adapter_module.RENEW_FLOOR
)


async def test_a_view_beside_a_preview_takes_what_is_left_of_the_window_down_to_the_floor(
    tmp_path: Path,
) -> None:
    """A view opened late in the window its preview's wake opened ends with that window, rather
    than stop the preview: the preview's next read renews the sandbox once the view is gone."""
    clock = Clock()
    adapter, client, run_id = await session_kept(tmp_path, clock, public=True)
    woke = clock.now
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    clock.now += AT_THE_FLOOR
    keep = clock.now + SPEC.stop_after
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.READY
    paused = len(client.paused)

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    assert len(client.paused) == paused, "the sandbox was not renewed"
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
    view = _read_state(_run_dir(tmp_path, run_id), run_id).inspect
    window = woke + adapter_module.MAX_SESSION - adapter_module.SESSION_MARGIN
    assert view is not None and view.deadline == window, "it ends with the window"


async def test_a_view_below_the_floor_renews_its_sandbox_for_its_whole_limit(
    tmp_path: Path,
) -> None:
    """A view with less than `RENEW_FLOOR` of its preview's window left would barely open before
    it closed: the sandbox is renewed first, and the preview stopped for the worker to start
    again."""
    clock = Clock()
    adapter, client, run_id = await session_kept(tmp_path, clock, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    clock.now += AT_THE_FLOOR + timedelta(minutes=1)
    keep = clock.now + SPEC.stop_after
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.READY

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    view = _read_state(_run_dir(tmp_path, run_id), run_id).inspect
    assert view is not None and view.deadline == clock.now + LIMIT, "the view gets its whole limit"
    stopped = await adapter.preview_status(run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL
    assert client.only().contained_at_network[-1] is True
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.OPEN


async def test_a_view_renews_a_sandbox_whose_wake_was_never_recorded(tmp_path: Path) -> None:
    """A running sandbox whose wake is not on record may be near E2B's cap: the view renews it
    rather than guess how much of the window is left."""
    adapter, client, run_id = await session_kept(tmp_path, public=True)
    await adapter.preview_start(run_id, SPEC, PREVIEW_ENV_VALUES, token="p1")
    run_dir = _run_dir(tmp_path, run_id)
    state = _read_state(run_dir, run_id)
    state.awake_since = None
    _write_state(run_dir, state)
    paused = len(client.paused)

    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)

    assert len(client.paused) == paused + 1, "the sandbox was renewed"
    stopped = await adapter.preview_status(run_id)
    assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL


async def test_a_start_with_the_same_token_is_refused_once_another_run_has_the_workspace(
    tmp_path: Path,
) -> None:
    """The token's start found again must not reopen a view a rework took the workspace from."""
    adapter, _, run_id = await session_kept(tmp_path)
    await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    state = _read_state(_run_dir(tmp_path, run_id), run_id)
    state.continued_by = "GH-12-plan-agent-later"
    _write_state(_run_dir(tmp_path, run_id), state)

    with pytest.raises(InspectUnavailable, match="continued"):
        await adapter.inspect_start(run_id, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)


async def test_a_sandbox_carries_its_organization(tmp_path: Path) -> None:
    _, client, _ = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], make_spec(organization="acme-" + "x" * 80)
    )
    (sandbox,) = client.sandboxes.values()
    assert sandbox.options.metadata["factorial_org"] == ("acme-" + "x" * 80)[:64]
    assert {"factorial_run", "factorial_deployment"} <= sandbox.options.metadata.keys()


async def test_a_run_without_an_organization_adds_no_key(tmp_path: Path) -> None:
    _, client, _ = await run_to_end(tmp_path, [Script(tui=at_prompt())])
    (sandbox,) = client.sandboxes.values()
    assert "factorial_org" not in sandbox.options.metadata


async def test_reap_reparks_a_running_sandbox_nothing_keeps_awake(tmp_path: Path) -> None:
    """A viewer's connect that raced a park, or a wake whose worker died before recording it,
    leaves a kept sandbox running, which E2B kills, workspace and all, at its timeout. The
    reaper is another adapter instance, as the worker builds it."""
    client = FakeSandboxClient(scripts=[Script(tui=at_prompt()) for _ in range(3)])
    adapter = adapter_for(tmp_path, client)
    stray = (await adapter.spawn(make_spec())).run_id
    viewed = (await adapter.spawn(make_spec())).run_id
    open_elsewhere = (await adapter.spawn(make_spec())).run_id
    for run_id in (stray, viewed, open_elsewhere):
        await _finish_session(adapter, run_id)
    await adapter.drain()
    sandboxes = {s.options.metadata["factorial_run"]: s for s in client.sandboxes.values()}
    for run_id in (stray, open_elsewhere):
        await client.connect(sandboxes[run_id].sandbox_id, timeout_seconds=300)
    await adapter.inspect_start(viewed, token="r1", limit=LIMIT, credentials=VIEW_CREDENTIALS)
    # In flight: woken and held by an adapter that has not recorded why yet.
    await adapter._hold(sandboxes[open_elsewhere].sandbox_id, timeout_seconds=300)

    reaper = adapter_for(tmp_path, client)
    assert await reaper.reap({stray, viewed, open_elsewhere}, retain=None) == ()

    assert sandboxes[stray].state == "paused"
    scrubs = [c for c in sandboxes[stray].commands if c.command.startswith(SCRUB)]
    assert len(scrubs) == 2, "scrubbed at its end, and again before the second pause"
    assert sandboxes[viewed].state == "running" and sandboxes[viewed].view_open
    assert sandboxes[open_elsewhere].state == "running"
    assert client.killed == []


async def test_an_attachment_is_written_for_the_agent_and_named_in_the_first_turn(
    tmp_path: Path,
) -> None:
    _, client, _ = await run_to_end(
        tmp_path, [Script(tui=at_prompt())], make_spec(attachments=(attachment(),))
    )

    sandbox = client.only()
    path = f"{ATTACHMENTS_DIR}/image-1.png"
    assert sandbox.files[path] == (PNG, "agent")
    assert f"600\0{path}".encode() in sandbox.files[MODES][0]
    assert f"- image-1 (ticket_body, {ORIGIN}): {path}" in sandbox.files[turn_file(0)][0].decode()
    assert path in _nul_file(sandbox, ARGV)[-1], "the pointer turn names it"
