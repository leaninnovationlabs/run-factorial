"""An interactive run on E2B against a fake sandbox: the TUI's spawn, its supervisor, and what
`session_state` and `terminal_target` report (decision 56)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from e2b import exceptions as e2b_exceptions
from fake_e2b import (
    REWORK_HEAD,
    FakeSandbox,
    FakeSandboxClient,
    Script,
    TuiScript,
)
from pydantic import SecretStr

from runtime.adapters._cli import rework
from runtime.adapters.claude_cli.auth import AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION
from runtime.adapters.claude_cli.stream import StreamReader
from runtime.adapters.e2b import ClaudeE2bAdapter
from runtime.adapters.e2b import adapter as adapter_module
from runtime.adapters.e2b import smoke as smoke_module
from runtime.adapters.e2b.adapter import (
    MAX_SESSION,
    PLACEHOLDER_ROUTED,
    RENEW_FLOOR,
    SESSION_CAP_REASON,
    SESSION_MARGIN,
    _github_inject,
    _gitlab_inject,
    _read_state,
    _RunState,
    _write_state,
)
from runtime.adapters.e2b.claude import ClaudeHarness
from runtime.adapters.e2b.client import CommandOutput, NetworkPolicy
from runtime.adapters.e2b.layout import (
    ARGV,
    ATTACHMENTS_DIR,
    CAPTURE,
    CONFIG_DIR,
    ENV,
    FINISH,
    FIREWALL,
    MODES,
    PREVIEW_STOP,
    REWORK,
    SCRUB,
    SESSION,
    SESSION_POLL,
    SIGNAL,
    SIGNALS,
    STOP,
    TMUX_SESSION,
    TMUX_SOCKET,
    session_log,
    turn_file,
)
from runtime.adapters.e2b.smoke import session_smoke_run, smoke_spec
from runtime.contract import (
    PREVIEW_CONTINUED,
    PREVIEW_RENEWAL,
    AdapterUnavailable,
    AgentPrompts,
    AgentRuntimeError,
    IdleReason,
    InspectPhase,
    McpServerSpec,
    McpTransport,
    NoBranchHeld,
    ParkReason,
    PreviewPhase,
    PreviewServiceSpec,
    PreviewSpec,
    PreviewUnavailable,
    ResumeFrom,
    RunAttachment,
    RunResult,
    RunSpec,
    RunStatus,
    SecretRoute,
    SessionUnavailable,
    SpawnError,
    SpawnReason,
    SubstrateUnavailable,
    TerminalTarget,
    UnknownRunError,
    Workspace,
)

pytestmark = pytest.mark.asyncio

TOKEN = "ghs_e2bTestTokenMustNeverEnterTheSandbox"  # gitleaks:allow
API_KEY = "sk-ant-e2bTestKeyMustNeverEnterTheSandbox"
OAUTH = "sk-ant-oat01-e2bTestTokenMustNeverEnterTheSandbox"
DOCS_TOKEN = "docs-e2bTestHeaderValueMustNeverEnter"
REAL_WORKSPACE = "/mnt/disk/agent-workspace"
QUESTION = "Include archived rows?"

DOCS = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.example.com/docs",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
    tools_allow=("search",),
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
            "CLAUDE_CODE_OAUTH_TOKEN": SecretStr(OAUTH),
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


# --------------------------------------------------------------------------- lines


def hook(event: str, at: str = "2026-09-23T11:59:00Z", **fields: Any) -> str:
    return json.dumps({"at": at, "hook": {"hook_event_name": event, **fields}})


def started() -> str:
    return hook("SessionStart", source="startup")


def prompted() -> str:
    return hook("UserPromptSubmit", prompt="whatever was typed")


def stopped(message: str = "Opened PR #7.", at: str = "2026-09-23T11:59:00Z") -> str:
    return hook("Stop", at, last_assistant_message=message)


def asked() -> str:
    tool_input = {"questions": [{"question": QUESTION, "options": [{"label": "Active only"}]}]}
    return hook("PreToolUse", tool_name="AskUserQuestion", tool_use_id="t1", tool_input=tool_input)


def user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def assistant(message_id: str, text: str, request: str) -> str:
    usage = {"input_tokens": 10, "output_tokens": 5}
    message = {
        "id": message_id,
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "usage": usage,
        "stop_reason": "end_turn",
    }
    return json.dumps({"type": "assistant", "requestId": request, "message": message})


def turn_ended() -> str:
    return json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 5})


def cost(usd: float) -> str:
    usage = {"inputTokens": 100, "outputTokens": 50}
    return json.dumps({"type": "cost-state", "totalCostUSD": usd, "modelUsage": {"m": usage}})


#: The first turn, to its end: the orchestrator's prompt, one answer, the Stop.
FIRST_TURN: tuple[Sequence[str], Sequence[str]] = (
    [started(), prompted(), stopped()],
    [user("Read the turn"), assistant("msg_1", "Opened PR #7.", "req_1"), turn_ended()],
)


# --------------------------------------------------------------------------- helpers


async def spawned(
    tmp_path: Path,
    tui: TuiScript,
    clock: Clock | None = None,
    **client_fields: Any,
) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    client = FakeSandboxClient(scripts=[Script(tui=tui)], **client_fields)
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec())
    return adapter, client, handle.run_id


async def polled(client: FakeSandboxClient, n: int = 2) -> None:
    """Until the supervisor polled `n` more times: a step released by the first is then read,
    and its state written."""
    start = len(client.commands_by_name(SESSION_POLL))
    for _ in range(10_000):
        if len(client.commands_by_name(SESSION_POLL)) >= start + n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"the supervisor did not poll {n} more times")


def state_of(tmp_path: Path, run_id: str) -> _RunState:
    return _read_state(tmp_path / "state" / "runs" / run_id, run_id)


def cap_of(tmp_path: Path, run_id: str) -> datetime:
    return state_of(tmp_path, run_id).deadline


def mirror(tmp_path: Path, run_id: str, name: str = "stdout.jsonl") -> list[str]:
    path = tmp_path / "state" / "runs" / run_id / name
    return path.read_text("utf-8").splitlines() if path.exists() else []


async def ended(adapter: ClaudeE2bAdapter) -> None:
    """Every supervisor done, or a TimeoutError: a session nothing ends would hold the test."""
    await asyncio.wait_for(adapter.drain(), timeout=10)


async def stop(adapter: ClaudeE2bAdapter, run_id: str) -> None:
    await adapter.cancel(run_id)
    await adapter.drain()


async def terminal(adapter: ClaudeE2bAdapter, run_id: str) -> RunStatus:
    """The run's status once it ended, or where it stood when it did not."""
    for _ in range(10_000):
        if (status := await adapter.status(run_id)).is_terminal:
            return status
        await asyncio.sleep(0)
    return await adapter.status(run_id)


def _modes(sandbox: FakeSandbox) -> dict[str, str]:
    pairs = sandbox.files[MODES][0].decode().split("\0")[:-1]
    return dict(zip(pairs[1::2], pairs[0::2], strict=True))


def _nul(sandbox: FakeSandbox, path: str) -> list[str]:
    return sandbox.files[path][0].decode().split("\0")[:-1]


# --------------------------------------------------------------------------- spawn


async def test_an_interactive_spawn_starts_tmux_as_root_and_the_cli_as_the_agent(
    tmp_path: Path,
) -> None:
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript())], workspace_real=REAL_WORKSPACE)
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec(mcp=(DOCS,)))
    sandbox = client.only()
    try:
        assert [c.command for c in sandbox.commands if c.command == SESSION] == [SESSION]
        modes = _modes(sandbox)
        for path in (ARGV, ENV):
            assert (sandbox.files[path][1], modes[path]) == ("root", "600"), path
        seeds = (f"{CONFIG_DIR}/.claude.json", f"{CONFIG_DIR}/settings.json", turn_file(0))
        for path in seeds:
            assert (sandbox.files[path][1], modes[path]) == ("agent", "600"), path

        env = dict(entry.split("=", 1) for entry in _nul(sandbox, ENV))
        assert env["FACTORIAL_SIGNALS"] == SIGNALS
        assert not [key for key in env if key.startswith("CLAUDE_AFK_")]
        seen = client.everything_seen()
        for secret in (TOKEN, API_KEY, OAUTH, DOCS_TOKEN):
            assert secret not in seen
        inject = sandbox.options.network.inject
        assert inject["api.anthropic.com"] == {"x-api-key": API_KEY}
        assert inject["mcp.example.com"] == {"Authorization": f"Bearer {DOCS_TOKEN}"}

        claude_json = json.loads(sandbox.files[f"{CONFIG_DIR}/.claude.json"][0])
        assert set(claude_json["projects"]) == {REAL_WORKSPACE}
        settings = json.loads(sandbox.files[f"{CONFIG_DIR}/settings.json"][0])
        assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == SIGNAL
        assert settings["claudeMdExcludes"] == [f"!{REAL_WORKSPACE}/**"]

        state = state_of(tmp_path, handle.run_id)
        argv = _nul(sandbox, ARGV)
        assert argv[argv.index("--session-id") + 1] == state.session_id
        assert argv[argv.index("--") + 1].startswith(f"Read @{turn_file(0)} ")
        assert "--session-id" not in state.spawn_argv and "--" not in state.spawn_argv
        # The session's sandbox lives to E2B's cap, whatever the step's timeout.
        assert sandbox.options.timeout_seconds == int(MAX_SESSION.total_seconds())
        assert state.deadline == Clock().now + MAX_SESSION

        session = await adapter.session_state(handle.run_id)
        assert (session.log_offset, session.session_id) == (0, state.session_id)
    finally:
        await stop(adapter, handle.run_id)


@pytest.mark.parametrize("auth_mode", [AUTH_MODE_API_KEY, AUTH_MODE_SUBSCRIPTION])
async def test_the_harness_puts_no_real_secret_in_a_seed_the_env_or_argv(auth_mode: str) -> None:
    """The run's secrets ride the egress proxy's headers; the CLI is handed a placeholder, and
    the seed approves that placeholder's tail (decision 25)."""
    harness = ClaudeHarness()
    spec = make_spec(mcp=(DOCS,))
    secrets = spec.resolved_secrets()
    launch = harness.session_spawn(spec, secrets, auth_mode, "5f0b1f2e-0000-4000-8000-00000000e2b0")
    seeds = harness.session_seeds(
        REAL_WORKSPACE, CONFIG_DIR, auth_mode, spec.mcp, spec.prompts.system
    )

    written = [content for content, _, _ in [*launch.files.values(), *seeds.values()]]
    seen = "\n".join(
        [*launch.argv, *launch.env.keys(), *launch.env.values()]
        + [content.decode(errors="replace") for content in written]
    )
    for secret in secrets.values():
        assert secret not in seen
    assert any(secret in str(launch.inject) for secret in (API_KEY, OAUTH))


async def test_a_cli_that_died_before_its_result_says_so_first_and_its_stderr_below() -> None:
    """The timeline shows a reason's first paragraph: the stderr is the detail under it."""
    outcome = ClaudeHarness().session_outcome(
        StreamReader(), 1, "  Error: invalid x-api-key\n", summary=None
    )
    assert outcome.error == (
        "The Claude CLI stopped before it reported a result (exit 1).\n\n"
        "Error: invalid x-api-key"
    )
    bare = ClaudeHarness().session_outcome(StreamReader(), None, "", summary=None)
    assert bare.error == "The Claude CLI stopped before it reported a result (no exit code)."


# --------------------------------------------------------------------------- the supervisor


async def test_an_idle_session_is_never_timed_out_by_the_adapter(tmp_path: Path) -> None:
    """The worker holds a session to its step's active time; at its prompt it waits on a human."""
    clock = Clock()
    adapter, client, run_id = await spawned(tmp_path, TuiScript(steps=[FIRST_TURN]), clock)
    try:
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        clock.now += timedelta(minutes=40)  # past the spec's 30 minutes
        await polled(client)

        assert await adapter.status(run_id) is RunStatus.IDLE
        assert client.commands_by_name(STOP) == client.commands_by_name(FINISH) == []
    finally:
        await stop(adapter, run_id)


async def test_the_session_cap_parks_rather_than_letting_e2b_kill_the_workspace(
    tmp_path: Path,
) -> None:
    clock = Clock()
    adapter, client, run_id = await spawned(tmp_path, TuiScript(steps=[FIRST_TURN]), clock)
    await polled(client)
    clock.now = cap_of(tmp_path, run_id) - timedelta(seconds=59)
    await adapter.drain()

    sandbox = client.only()
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.INTERRUPTED
    assert result.failure_reason == SESSION_CAP_REASON
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert client.paused == [sandbox.sandbox_id] and client.killed == []
    assert state_of(tmp_path, run_id).kept is True


async def test_the_backstop_hangs_up_after_a_restart_between_its_record_and_its_hangup(
    tmp_path: Path,
) -> None:
    """The backstop records the park before it hangs up; a worker that died in between must
    still hang up, or E2B's cap kills the sandbox and the workspace with it."""
    clock = Clock()
    first, client, run_id = await spawned(tmp_path, TuiScript(steps=[FIRST_TURN]), clock)
    await polled(client)
    for task in first._supervisors.values():
        task.cancel()
    state = state_of(tmp_path, run_id)
    state.stopping, state.park_reason = RunStatus.INTERRUPTED, ParkReason.ROLLOVER
    state.parked_at_cap = True
    _write_state(tmp_path / "state" / "runs" / run_id, state)

    clock.now = cap_of(tmp_path, run_id) - timedelta(seconds=30)
    second = adapter_for(tmp_path, client, clock)
    await second.status(run_id)
    await ended(second)

    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    result = await second.result(run_id)
    assert result.outcome is RunStatus.INTERRUPTED
    assert result.failure_reason == SESSION_CAP_REASON
    assert client.paused == [client.only().sandbox_id] and client.killed == []


async def test_a_backstop_hangup_that_failed_is_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    adapter, client, run_id = await spawned(tmp_path, TuiScript(steps=[FIRST_TURN]), clock)
    await polled(client)
    sandbox = client.only()
    run, refused = sandbox.run, []

    async def once_unreachable(command: str, **options: Any) -> CommandOutput:
        if command == FINISH and not refused:
            refused.append(command)
            return CommandOutput(1, "", "E2B 503")
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", once_unreachable)
    clock.now = cap_of(tmp_path, run_id) - timedelta(seconds=30)
    await ended(adapter)

    assert refused == [FINISH]
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert (await adapter.result(run_id)).outcome is RunStatus.INTERRUPTED
    assert client.paused == [sandbox.sandbox_id] and client.killed == []


async def test_the_session_turn_cap_hangs_up_and_fails_the_run(tmp_path: Path) -> None:
    """Neither CLI takes a turn cap, so the supervisor counts requests in the session log. It
    hangs up rather than stops, so the CLI still writes its cost on the way out."""
    answers = [assistant(f"msg_{n}", "Working.", f"req_{n}") for n in (1, 2, 3)]
    turn = ([started(), prompted(), stopped()], [user("go"), *answers, turn_ended()])
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(steps=[turn], on_exit=leaving(0.25)))])
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec(max_turns=2))).run_id

    assert await terminal(adapter, run_id) is RunStatus.FAILED
    await ended(adapter)
    result = await adapter.result(run_id)
    assert "Stopped after 2 turns" in (result.failure_reason or "")
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert client.commands_by_name(STOP) == []
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25
    assert client.paused == [client.only().sandbox_id] and client.killed == []


async def test_a_restarted_worker_still_counts_the_session_turns_it_did_not_see(
    tmp_path: Path,
) -> None:
    """Two requests before the restart and one after cross a cap of two. A reader rebuilt from
    nothing would count one and let the session escape its cap."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(on_exit=leaving(0.25)))])
    first = adapter_for(tmp_path, client)
    run_id = (await first.spawn(make_spec(max_turns=2))).run_id
    sandbox = client.only()
    answers = [assistant(f"msg_{n}", "Working.", f"req_{n}") for n in (1, 2)]
    sandbox.play([started(), prompted()], [user("go"), *answers])
    await polled(client)
    for task in first._supervisors.values():
        task.cancel()
    sandbox.play([stopped()], [assistant("msg_3", "Done.", "req_3"), turn_ended()])

    second = adapter_for(tmp_path, client)
    assert await terminal(second, run_id) is RunStatus.FAILED
    await ended(second)
    result = await second.result(run_id)
    assert "Stopped after 2 turns" in (result.failure_reason or "")


async def test_a_line_no_read_may_take_whole_stops_the_session(tmp_path: Path) -> None:
    """The agent can write its own log: a line without end would double the read until the
    worker ran out of memory."""
    adapter, client, run_id = await spawned(tmp_path, TuiScript())
    sandbox = client.only()
    sid = state_of(tmp_path, run_id).session_id
    assert sid is not None
    sandbox.play([started(), prompted()], [user("go")])
    await polled(client)
    sandbox.logs[session_log(sid)] += b"x" * (17 << 20)
    await ended(adapter)

    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.FAILED
    assert result.failure_reason == "the session log carried a line over 16 MiB"
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert client.paused == [sandbox.sandbox_id], "the workspace is kept"


async def test_a_poll_whose_state_write_failed_repeats_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirrors are written before the state. A poll that wrote them and lost the state
    ends its supervisor, and the next one truncates them back and reads again."""
    first, client, run_id = await spawned(tmp_path, TuiScript())
    sandbox = client.only()
    sandbox.play([started(), prompted()], [user("go")])
    await polled(client)
    write = adapter_module._write_state

    def disk_full(*args: Any) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(adapter_module, "_write_state", disk_full)
    sandbox.play([stopped()], [assistant("msg_1", "Done.", "req_1"), turn_ended()])
    with pytest.raises(AgentRuntimeError, match="recording a poll"):
        await ended(first)
    monkeypatch.setattr(adapter_module, "_write_state", write)
    assert mirror(tmp_path, run_id, "signals.jsonl")[-1] == stopped(), "the mirrors were written"

    second = adapter_for(tmp_path, client)
    await second.status(run_id)
    try:
        await polled(client)
        for name in ("signals.jsonl", "session.jsonl", "stdout.jsonl"):
            lines = mirror(tmp_path, run_id, name)
            assert len(lines) == len(set(lines)), name
        assert await second.status(run_id) is RunStatus.IDLE
        assert (await second.session_state(run_id)).turns == 1
    finally:
        await stop(second, run_id)


async def test_stop_then_question_then_human_turn_reach_session_state(tmp_path: Path) -> None:
    adapter, client, run_id = await spawned(tmp_path, TuiScript())
    sandbox = client.only()
    try:
        sandbox.play([started(), prompted()], [user("Read the turn")])
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.RUNNING
        assert (await adapter.session_state(run_id)).idle_reason is None

        answer = [assistant("msg_1", "Done.", "req_1"), turn_ended()]
        sandbox.play([stopped(at="2026-09-23T11:58:00Z")], answer)
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        state = await adapter.session_state(run_id)
        assert state.idle_reason is IdleReason.TURN_END
        assert state.idle_since == datetime(2026, 9, 23, 11, 58, tzinfo=UTC)
        assert state.last_input_at is None, "the orchestrator's prompt is not a human's input"
        assert state.turns == 1

        sandbox.play([hook("UserPromptSubmit", "2026-09-23T11:59:00Z"), asked()])
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        state = await adapter.session_state(run_id)
        assert state.idle_reason is IdleReason.QUESTION
        assert state.question == f"{QUESTION} [Active only]"
        assert state.last_input_at == datetime(2026, 9, 23, 11, 59, tzinfo=UTC)

        sandbox.play([hook("PostToolUse", "2026-09-23T11:59:30Z", tool_name="AskUserQuestion")])
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.RUNNING
        state = await adapter.session_state(run_id)
        assert (state.idle_reason, state.idle_since, state.question) == (None, None, None)
        assert state.last_input_at == datetime(2026, 9, 23, 11, 59, 30, tzinfo=UTC)
    finally:
        await stop(adapter, run_id)


async def test_a_failed_turn_idles_and_a_session_ending_on_it_fails(tmp_path: Path) -> None:
    """A StopFailure leaves the session at its prompt for a human to retry; if it then ends by
    itself, the run took the turn's failure with it."""
    failure = hook("StopFailure", error="rate_limit", last_assistant_message="API Error: 429")
    tui = TuiScript()
    adapter, client, run_id = await spawned(tmp_path, tui)
    client.only().play([started(), prompted(), failure], [user("go")])
    await polled(client)

    assert await adapter.status(run_id) is RunStatus.IDLE
    assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_ERROR

    tui.ends_by_itself = True
    await adapter.drain()
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.FAILED
    summary, _, detail = (result.failure_reason or "").partition("\n\n")
    assert summary == "The session ended on a failed turn."
    assert "rate_limit" in detail


async def test_a_session_that_ends_by_itself_reports_this_segments_cost_and_turns(
    tmp_path: Path,
) -> None:
    tui = TuiScript(
        steps=[FIRST_TURN],
        on_exit=([hook("SessionEnd")], [cost(0.25)]),
        ends_by_itself=True,
    )
    adapter, _, run_id = await spawned(tmp_path, tui)
    await adapter.drain()

    result = await adapter.result(run_id)
    assert (result.outcome, result.park_reason) == (RunStatus.INTERRUPTED, ParkReason.EXITED)
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25
    assert result.summary == "Opened PR #7."
    assert result.stream_warnings == ()
    lines = [json.loads(line) for line in mirror(tmp_path, run_id)]
    assert [line["type"] for line in lines] == ["system", "user", "assistant", "result"]
    assert lines[-1]["num_turns"] == 1


async def test_a_session_log_missing_a_line_the_adapter_reads_says_so_without_failing(
    tmp_path: Path,
) -> None:
    """A CLI that stopped writing its cost-state would otherwise read as a run that cost
    nothing."""
    tui = TuiScript(steps=[FIRST_TURN], on_exit=([hook("SessionEnd")], []), ends_by_itself=True)
    adapter, _, run_id = await spawned(tmp_path, tui)
    await adapter.drain()

    result = await adapter.result(run_id)
    assert (result.outcome, result.park_reason) == (RunStatus.INTERRUPTED, ParkReason.EXITED)
    assert result.stream_warnings == (
        "no cost-state line",
        "no total_cost_usd in the result event",
    )


async def test_a_restarted_worker_reattaches_an_idle_session_without_repeating_a_line(
    tmp_path: Path,
) -> None:
    """A message whose lines straddle the restart is emitted once, whole: the translator is
    rebuilt from the raw mirror, and the translated mirror is not written twice."""
    clock = Clock()
    first, client, run_id = await spawned(tmp_path, TuiScript(), clock)
    sandbox = client.only()
    sandbox.play([started(), prompted()], [user("go"), assistant("msg_1", "Half", "req_1")])
    await polled(client)
    for task in first._supervisors.values():
        task.cancel()
    before = mirror(tmp_path, run_id)

    clock.now += timedelta(minutes=10)
    second = adapter_for(tmp_path, client, clock)
    assert await second.status(run_id) is RunStatus.RUNNING
    [(_, timeout)] = client.connects
    assert timeout <= (cap_of(tmp_path, run_id) - clock.now).total_seconds()
    try:
        sandbox.play([stopped()], [assistant("msg_1", "and whole.", "req_1"), turn_ended()])
        await polled(client)

        after = mirror(tmp_path, run_id)
        assert after[: len(before)] == before
        assert len(after) == len(set(after)), "no line is mirrored twice"
        answers = [json.loads(line) for line in after if json.loads(line)["type"] == "assistant"]
        assert [[b["text"] for b in a["message"]["content"]] for a in answers] == [
            ["Half", "and whole."]
        ]
        assert await second.status(run_id) is RunStatus.IDLE
        assert (await second.session_state(run_id)).turns == 1
    finally:
        await stop(second, run_id)


async def test_a_line_is_read_only_once_it_is_whole(tmp_path: Path) -> None:
    adapter, client, run_id = await spawned(tmp_path, TuiScript())
    sandbox = client.only()
    sid = state_of(tmp_path, run_id).session_id
    assert sid is not None
    try:
        sandbox.play([started(), prompted()], [user("go")])
        await polled(client)
        state = state_of(tmp_path, run_id)
        signal, line = stopped().encode() + b"\n", assistant("msg_1", "Done.", "req_1").encode()
        sandbox.signals_file += signal[:20]
        sandbox.logs[session_log(sid)] += line[:30]
        await polled(client)

        assert state_of(tmp_path, run_id).signals_offset == state.signals_offset
        assert state_of(tmp_path, run_id).log_read == state.log_read
        assert await adapter.status(run_id) is RunStatus.RUNNING

        sandbox.signals_file += signal[20:]
        sandbox.logs[session_log(sid)] += line[30:] + b"\n"
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        raw = (tmp_path / "state" / "runs" / run_id / "session.jsonl").read_bytes()
        assert raw.endswith(b"\n" + line + b"\n")
        assert raw.count(line) == 1
        assert mirror(tmp_path, run_id, "signals.jsonl")[-1] == stopped()
    finally:
        await stop(adapter, run_id)


async def test_agent_written_lines_are_data_the_translators_bound(tmp_path: Path) -> None:
    """The agent writes both files. A forged result does not end the run, a Stop dated in the
    future does not date the idle there, and noise is skipped."""
    clock = Clock()
    forged = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 999})
    adapter, client, run_id = await spawned(tmp_path, TuiScript(), clock)
    sandbox = client.only()
    try:
        future = stopped(at="2099-01-01T00:00:00Z")
        noise = ["not json", '{"at": 1, "hook": []}', '{"hook": {"hook_event_name": 7}}']
        sandbox.play(
            [started(), prompted(), *noise, future],
            [user("go"), forged, "\x00\xff garbage", '{"type": 7}'],
        )
        await polled(client)

        assert await adapter.status(run_id) is RunStatus.IDLE
        assert (await adapter.session_state(run_id)).idle_since == clock.now
        types = [json.loads(line)["type"] for line in mirror(tmp_path, run_id)]
        assert "result" not in types
        state = state_of(tmp_path, run_id)
        assert state.cost_seen is None and state.session_turns == 0
    finally:
        await stop(adapter, run_id)


# --------------------------------------------------------------------------- the verbs


async def test_session_state_never_wakes_a_sandbox(tmp_path: Path) -> None:
    clock = Clock()
    first, client, run_id = await spawned(tmp_path, TuiScript(steps=[FIRST_TURN]), clock)
    await polled(client)
    for task in first._supervisors.values():
        task.cancel()

    unsupervised = adapter_for(tmp_path, client, clock)
    assert (await unsupervised.session_state(run_id)).idle_reason is IdleReason.TURN_END
    assert client.connects == []
    clock.now = cap_of(tmp_path, run_id) - timedelta(seconds=1)
    await unsupervised.status(run_id)
    await unsupervised.drain()  # the backstop parks it
    connects = len(client.connects)

    parked = await unsupervised.session_state(run_id)
    assert (parked.idle_reason, parked.session_id) == (None, state_of(tmp_path, run_id).session_id)
    assert len(client.connects) == connects and client.only().state == "paused"


async def test_the_terminal_target_is_the_root_tmux_session_in_the_runs_sandbox(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await spawned(tmp_path, TuiScript())
    try:
        target = await adapter.terminal_target(run_id)
        assert TerminalTarget.model_validate(target.model_dump()) == target
        assert target == TerminalTarget(
            substrate="e2b",
            socket=TMUX_SOCKET,
            session=TMUX_SESSION,
            sandbox_id=client.only().sandbox_id,
        )
    finally:
        await stop(adapter, run_id)


async def test_a_legacy_state_file_reads(tmp_path: Path) -> None:
    """A headless run's state written before sessions existed carries none of their fields, and
    `extra="forbid"` must not turn that into an unreadable run."""
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(steps=[FIRST_TURN]))])
    adapter = adapter_for(tmp_path, client)
    handle = await adapter.spawn(make_spec())
    await polled(client)
    await adapter.finish(handle.run_id)
    path = tmp_path / "state" / "runs" / handle.run_id / "state.json"
    written = json.loads(path.read_text())
    legacy = {key: value for key, value in written.items() if key in _LEGACY_FIELDS}
    assert set(written) - set(legacy), "the state gained session fields to drop"
    path.write_text(json.dumps(legacy))

    state = state_of(tmp_path, handle.run_id)
    assert state.signals.idle_reason is None
    assert await adapter.status(handle.run_id) is RunStatus(written["status"])


#: `_RunState`'s fields before interactive sessions.
_LEGACY_FIELDS = frozenset(
    {
        "run_id", "sandbox_id", "status", "agent", "ticket", "repo", "branch", "auth_mode",
        "started_at", "deadline", "timeout_seconds", "pid", "session_id", "max_turns", "turns",
        "offset", "segment_start", "exit_code", "summary", "failure_reason", "cost_usd",
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
        "artifacts", "stream_warnings", "expected", "spawn_argv", "mcp", "allow", "agent_env",
        "public", "kept", "preview", "stopping",
    }
)  # fmt: skip


# --------------------------------------------------------------------------- ending and resuming

FRESH_TOKEN = "ghs_e2bFreshTokenMustNeverEnterTheSandbox"  # gitleaks:allow
FRESH_KEY = "sk-ant-e2bFreshKeyMustNeverEnterTheSandbox"
FRESH = {"GH_TOKEN": SecretStr(FRESH_TOKEN), "ANTHROPIC_API_KEY": SecretStr(FRESH_KEY)}
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def leaving(usd: float) -> tuple[Sequence[str], Sequence[str]]:
    """What the CLI writes on its way out, however it leaves: the cost so far, and SessionEnd."""
    return [hook("SessionEnd")], [cost(usd)]


def at_its_prompt(usd: float = 0.25) -> TuiScript:
    """A first turn that ends, then the prompt, until something ends the session."""
    return TuiScript(steps=[FIRST_TURN], on_exit=leaving(usd))


def resumed_at_its_prompt(usd: float) -> TuiScript:
    """A `--resume` with no turn: the session comes back at its prompt."""
    return TuiScript(steps=[([hook("SessionStart", source="resume")], [])], on_exit=leaving(usd))


async def idle(
    tmp_path: Path, *scripts: TuiScript, clock: Clock | None = None
) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str]:
    """Spawned, at its prompt after its first turn; each further script is a resume's."""
    client = FakeSandboxClient(scripts=[Script(tui=tui) for tui in scripts])
    adapter = adapter_for(tmp_path, client, clock)
    handle = await adapter.spawn(make_spec())
    await polled(client)
    assert await adapter.status(handle.run_id) is RunStatus.IDLE
    return adapter, client, handle.run_id


def results(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in mirror(tmp_path, run_id)]
    return [line for line in lines if line["type"] == "result"]


async def test_finish_exits_succeeds_and_parks_with_this_segments_cost(tmp_path: Path) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(0.25))
    await adapter.finish(run_id)

    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "exit"}]
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED and result.failure_reason is None
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25
    assert result.stream_warnings == ()
    assert [line["num_turns"] for line in results(tmp_path, run_id)] == [1]
    assert client.paused == [client.only().sandbox_id] and client.killed == []
    assert state_of(tmp_path, run_id).kept is True

    await adapter.finish(run_id)
    assert len(client.commands_by_name(FINISH)) == 1, "a finished session is sent nothing more"


async def test_finish_on_a_question_hangs_up_rather_than_typing_into_the_dialog(
    tmp_path: Path,
) -> None:
    """`/exit` typed into an open question would be taken for its answer."""
    adapter, client, run_id = await spawned(tmp_path, TuiScript(on_exit=leaving(0.1)))
    client.only().play([started(), prompted(), asked()], [user("go")])
    await polled(client)
    assert (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

    await adapter.finish(run_id)
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert (await adapter.result(run_id)).outcome is RunStatus.SUCCEEDED


async def test_finish_on_a_question_asked_in_words_exits(tmp_path: Path) -> None:
    adapter, client, run_id = await spawned(tmp_path, TuiScript(on_exit=leaving(0.1)))
    client.only().play(
        [started(), prompted(), hook("Stop", last_assistant_message="Which schema?")],
        [user("go")],
    )
    await polled(client)
    assert (await adapter.session_state(run_id)).idle_reason is IdleReason.QUESTION

    await adapter.finish(run_id)
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "exit"}]
    assert (await adapter.result(run_id)).outcome is RunStatus.SUCCEEDED


async def test_finish_on_a_running_session_does_nothing(tmp_path: Path) -> None:
    """A RUNNING session is a turn a human just started; the worker asked a moment too late."""
    adapter, client, run_id = await spawned(tmp_path, TuiScript())
    try:
        client.only().play([started(), prompted()], [user("go")])
        await polled(client)
        await adapter.finish(run_id)

        assert client.commands_by_name(FINISH) == []
        assert await adapter.status(run_id) is RunStatus.RUNNING
        assert state_of(tmp_path, run_id).finishing is False
    finally:
        await stop(adapter, run_id)


async def test_a_finished_session_is_succeeded_even_if_its_last_turn_errored(
    tmp_path: Path,
) -> None:
    """The worker finishes only a session whose postconditions hold: the failed turn after
    them is worth a warning, not the step."""
    failure = hook("StopFailure", error="rate_limit", last_assistant_message="API Error: 429")
    adapter, client, run_id = await spawned(tmp_path, TuiScript(on_exit=leaving(0.1)))
    client.only().play([started(), prompted(), failure], [user("go")])
    await polled(client)
    assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_ERROR

    await adapter.finish(run_id)
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "exit"}]
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED and result.failure_reason is None
    assert any("rate_limit" in warning for warning in result.stream_warnings)


async def test_a_finish_the_tui_does_not_take_is_hung_up_and_still_succeeds(
    tmp_path: Path,
) -> None:
    """When `/exit` does not take, finish.sh hangs the session up (exit 255): the run is still
    the success the worker asked for, and the cut-off log is not taken for a missing line."""
    tui = at_its_prompt(0.25)
    tui.exits = False
    adapter, client, run_id = await idle(tmp_path, tui)
    await adapter.finish(run_id)

    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "exit"}]
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED and result.failure_reason is None
    assert result.stream_warnings == ()
    assert state_of(tmp_path, run_id).exit_code == 255
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25


async def test_a_finish_that_has_not_settled_stays_idle_and_is_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a finish is in flight the session still reads IDLE, as the worker expects: read
    RUNNING, it would start a new stretch and never re-send. The re-send lands."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run, swallowed = sandbox.run, []

    async def lost(command: str, **options: Any) -> CommandOutput:
        if command == FINISH and not swallowed:
            swallowed.append(command)
            return CommandOutput(0, "", "")  # sent, and the session did not end
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", lost)
    wait = adapter_module.STOP_WAIT
    monkeypatch.setattr(adapter_module, "STOP_WAIT", 0.05)
    await adapter.finish(run_id)

    assert await adapter.status(run_id) is RunStatus.IDLE
    state = await adapter.session_state(run_id)
    assert state.idle_reason is IdleReason.TURN_END and state.idle_since is not None
    monkeypatch.setattr(adapter_module, "STOP_WAIT", wait)
    await adapter.finish(run_id)
    assert swallowed == [FINISH]
    assert len(client.commands_by_name(FINISH)) == 1, "the re-send is the one that landed"
    assert (await adapter.result(run_id)).outcome is RunStatus.SUCCEEDED


async def test_a_failed_finish_never_hangs_up_a_turn_a_human_starts_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed send rolls the worker's tick back, and a human may then submit a turn: the next
    finish must leave that turn running."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run, refused = sandbox.run, []

    async def once_refused(command: str, **options: Any) -> CommandOutput:
        if command == FINISH and not refused:
            refused.append(command)
            return CommandOutput(1, "", "E2B 503")
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", once_refused)
    monkeypatch.setattr(adapter_module, "STOP_WAIT", 0.05)
    try:
        with pytest.raises(AdapterUnavailable):
            await adapter.finish(run_id)
        sandbox.play([hook("UserPromptSubmit", prompt="one more thing")], [user("one more")])
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.RUNNING

        await adapter.finish(run_id)
        assert client.commands_by_name(FINISH) == []
        assert await adapter.status(run_id) is RunStatus.RUNNING
    finally:
        monkeypatch.undo()
        await stop(adapter, run_id)


async def test_a_turn_after_a_failed_finish_is_judged_by_its_own_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's finish rolled back with the failed send; a session a human then works on
    and that ends by itself takes its own exit's verdict, not that finish's success."""
    tui = at_its_prompt()
    adapter, client, run_id = await idle(tmp_path, tui)
    sandbox = client.only()
    run = sandbox.run

    async def refused(command: str, **options: Any) -> CommandOutput:
        if command == FINISH:
            return CommandOutput(1, "", "E2B 503")
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", refused)
    monkeypatch.setattr(adapter_module, "STOP_WAIT", 0.05)
    with pytest.raises(AdapterUnavailable):
        await adapter.finish(run_id)
    sandbox.play([hook("UserPromptSubmit", prompt="one more thing")], [user("one more")])
    await polled(client)
    tui.ends_by_itself, tui.exit_code = True, 1
    await ended(adapter)

    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.FAILED
    assert result.failure_reason == "The Claude CLI stopped unexpectedly (exit 1)."


async def test_a_finish_that_could_not_be_sent_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run = sandbox.run

    async def refused(command: str, **options: Any) -> CommandOutput:
        if command == FINISH:
            return CommandOutput(1, "", "E2B 503")
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", refused)
    monkeypatch.setattr(adapter_module, "STOP_WAIT", 0.05)
    try:
        with pytest.raises(AdapterUnavailable, match="cannot finish"):
            await adapter.finish(run_id)
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        monkeypatch.undo()
        await stop(adapter, run_id)


async def test_a_finish_cut_short_by_the_park_of_the_session_it_ended_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI leaves on the `/exit` while finish.sh still runs its tail, and the supervisor
    parks the sandbox under it: the command fails, but the finish landed."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run = sandbox.run

    async def parked_under_it(command: str, **options: Any) -> CommandOutput:
        done = await run(command, **options)
        return CommandOutput(137, "", "") if command == FINISH else done

    monkeypatch.setattr(sandbox, "run", parked_under_it)
    await adapter.finish(run_id)

    result = await adapter.result(run_id)
    assert (result.outcome, result.stream_warnings) == (RunStatus.SUCCEEDED, ())


@pytest.mark.parametrize(
    ("reason", "why"),
    [
        (ParkReason.USER, "Interrupted by a person. Its sandbox is kept for a resume."),
        (ParkReason.IDLE, "Paused while it waits for a person. Its sandbox is kept for a resume."),
        (ParkReason.ROLLOVER, "Paused to renew its token. Its sandbox is kept for a resume."),
    ],
)
async def test_an_interrupt_parks_the_session_and_says_why(
    tmp_path: Path, reason: ParkReason, why: str
) -> None:
    """A worker's rollover is not the cap's backstop, though both park with `rollover`."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(0.25))
    await adapter.interrupt(run_id, reason)

    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    assert client.commands_by_name(STOP) == [], "a hangup, so the CLI writes its cost"
    result = await adapter.result(run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.INTERRUPTED, why)
    assert why != SESSION_CAP_REASON
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25
    assert client.paused == [client.only().sandbox_id] and client.killed == []
    state = state_of(tmp_path, run_id)
    assert (state.kept, state.park_reason) == (True, reason)


async def test_an_interrupt_cut_short_by_the_park_of_the_session_it_ended_parks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run = sandbox.run

    async def parked_under_it(command: str, **options: Any) -> CommandOutput:
        done = await run(command, **options)
        return CommandOutput(137, "", "") if command == FINISH else done

    monkeypatch.setattr(sandbox, "run", parked_under_it)
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)

    state = state_of(tmp_path, run_id)
    assert (state.status, state.kept) == (RunStatus.INTERRUPTED, True)


async def test_a_hangup_refused_while_the_session_lives_fails_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session is still there, so the hangup did not land: the worker hears so now, not
    after the wait a park would take."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run = sandbox.run

    async def refused(command: str, **options: Any) -> CommandOutput:
        if command == FINISH:
            return CommandOutput(1, "", "E2B 503")
        return await run(command, **options)

    monkeypatch.setattr(sandbox, "run", refused)
    try:
        with pytest.raises(AdapterUnavailable, match="cannot stop"):
            await asyncio.wait_for(adapter.interrupt(run_id, ParkReason.ROLLOVER), 5)
        assert state_of(tmp_path, run_id).status is not RunStatus.INTERRUPTED
    finally:
        monkeypatch.setattr(sandbox, "run", run)
        await stop(adapter, run_id)


async def test_a_hangup_whose_supervisor_died_is_not_taken_for_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor that died never finalised the run: the stop is retried, not reported done
    on a session nobody has ended."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    sandbox = client.only()
    run = sandbox.run

    async def unanswered() -> frozenset[int]:
        raise ConnectionError("E2B did not answer")

    async def lost(command: str, **options: Any) -> CommandOutput:
        if command != FINISH:
            return await run(command, **options)
        for task in adapter._supervisors.values():
            task.cancel()
        monkeypatch.setattr(sandbox, "pids", unanswered)
        return CommandOutput(1, "", "connection reset")

    monkeypatch.setattr(sandbox, "run", lost)
    with pytest.raises(AdapterUnavailable, match="cannot stop"):
        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    assert not state_of(tmp_path, run_id).status.is_terminal


async def test_an_idle_rollover_parks_and_resumes_at_the_prompt_on_the_same_session(
    tmp_path: Path,
) -> None:
    clock = Clock()
    adapter, client, run_id = await idle(
        tmp_path, at_its_prompt(0.25), resumed_at_its_prompt(0.40), clock=clock
    )
    sandbox = client.only()
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    parked = state_of(tmp_path, run_id)
    sid = parked.session_id
    assert sid is not None

    clock.now += timedelta(minutes=50)
    handle = await adapter.resume(run_id, None, FRESH)
    assert (handle.run_id, handle.status) == (run_id, RunStatus.RUNNING)

    assert len(client.commands_by_name(SESSION)) == 2
    argv = _nul(sandbox, ARGV)
    assert argv[argv.index("--resume") + 1] == sid
    assert "--session-id" not in argv and "--" not in argv, "no turn: nothing typed"
    inject = sandbox.network.inject
    assert inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
    assert TOKEN not in str(inject) and API_KEY not in str(inject)
    seen = client.everything_seen()
    assert FRESH_KEY not in seen and FRESH_TOKEN not in seen
    assert client.connects[-1] == (sandbox.sandbox_id, int(MAX_SESSION.total_seconds()))

    state = state_of(tmp_path, run_id)
    assert state.segment_log_start == parked.log_read > 0
    assert (state.segments, state.segment_turn) == (2, False)
    assert state.deadline == clock.now + MAX_SESSION
    assert (state.stopping, state.park_reason, state.kept) == (None, None, False)

    await polled(client)
    assert await adapter.status(run_id) is RunStatus.IDLE
    session = await adapter.session_state(run_id)
    assert (session.idle_reason, session.session_id) == (IdleReason.TURN_END, sid)

    await adapter.finish(run_id)
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert result.stream_warnings == (), "a segment nobody typed into owes no turn"
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.40)
    segments = results(tmp_path, run_id)
    assert [line["total_cost_usd"] for line in segments] == pytest.approx([0.25, 0.15])
    inits = [line for line in map(json.loads, mirror(tmp_path, run_id)) if line["type"] == "system"]
    assert len(inits) == 2, "each segment opens its own"
    assert len(mirror(tmp_path, run_id, "session.jsonl")) == len(FIRST_TURN[1]) + 2


async def test_a_resume_turn_with_images_points_at_them(tmp_path: Path) -> None:
    image = RunAttachment(
        name="resume1-image-1",
        media_type="image/png",
        content=PNG,
        source="ticket_comments",
        origin="https://github.com/user-attachments/assets/1b92ad15",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(), TuiScript())
    await adapter.interrupt(run_id)
    try:
        await adapter.resume(run_id, "Match this layout.", FRESH, (image,))

        sandbox = client.only()
        image_path = f"{ATTACHMENTS_DIR}/resume1-image-1.png"
        argv = _nul(sandbox, ARGV)
        assert argv[-2:] == [
            "--",
            f"Read @{turn_file(1)} and do what it says. @{image_path}",
        ]
        modes = _modes(sandbox)
        for path in (turn_file(1), image_path):
            assert (sandbox.files[path][1], modes[path]) == ("agent", "600"), path
        text = sandbox.files[turn_file(1)][0].decode()
        assert text.startswith("Match this layout.") and image_path in text
        assert state_of(tmp_path, run_id).segment_turn is True
    finally:
        await stop(adapter, run_id)


async def test_an_unsupervised_interactive_stop_reattaches_before_stopping(
    tmp_path: Path,
) -> None:
    """After a worker restart the stop reattaches a supervisor first: only it ends the segment
    with the result line, and its cost, that the transcript owes."""
    clock = Clock()
    first, client, run_id = await idle(tmp_path, at_its_prompt(0.25), clock=clock)
    for task in first._supervisors.values():
        task.cancel()

    second = adapter_for(tmp_path, client, clock)
    await second.interrupt(run_id, ParkReason.IDLE)

    assert len(client.connects) == 1, "reattached"
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}]
    [segment] = results(tmp_path, run_id)
    assert segment["total_cost_usd"] == 0.25
    result = await second.result(run_id)
    assert result.outcome is RunStatus.INTERRUPTED
    assert result.cost_usd is not None and float(result.cost_usd) == 0.25
    assert client.paused == [client.only().sandbox_id]


async def test_a_resumed_sandbox_session_keeps_the_run_s_bot_login(tmp_path: Path) -> None:
    client = FakeSandboxClient(
        scripts=[Script(tui=at_its_prompt(0.25)), Script(tui=resumed_at_its_prompt(0.40))]
    )
    adapter = adapter_for(tmp_path, client)
    secrets = {**make_spec().secrets, "FACTORIAL_BOT_LOGIN": SecretStr("acme-factorial[bot]")}
    run_id = (await adapter.spawn(make_spec(secrets=secrets))).run_id
    await polled(client)
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)

    await adapter.resume(run_id, None, FRESH)

    env = dict(entry.split("=", 1) for entry in _nul(client.only(), ENV) if entry)
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "acme-factorial[bot]"
    assert state_of(tmp_path, run_id).bot_login == "acme-factorial[bot]"
    await stop(adapter, run_id)


async def test_a_session_resumes_after_a_worker_restart(tmp_path: Path) -> None:
    """A resume reads only the state on disk: a worker that never saw the run resumes it."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(0.25), resumed_at_its_prompt(0.40))
    await adapter.interrupt(run_id, ParkReason.IDLE)

    restarted = adapter_for(tmp_path, client)
    await restarted.resume(run_id, None, FRESH)
    await polled(client)
    assert await restarted.status(run_id) is RunStatus.IDLE
    await restarted.finish(run_id)
    result = await restarted.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.40)


async def test_a_session_resume_keeps_the_preview_and_contains_it_before_writing_credentials(
    tmp_path: Path,
) -> None:
    """Agent-written servers are public in a kept sandbox: once the proxy holds the run's
    credentials again, the preview's uid must already be kept off it (design.md §7.10)."""
    client = FakeSandboxClient(scripts=[Script(tui=at_its_prompt()), Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec(public=True))).run_id
    await polled(client)
    await adapter.interrupt(run_id)
    spec = PreviewSpec(
        setup=("npm ci",),
        services=(PreviewServiceSpec(name="web", command="npm run dev", port=3000),),
        stop_after=timedelta(minutes=30),
    )
    await adapter.preview_start(run_id, spec, {}, token="t1")
    assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
    try:
        await adapter.resume(run_id, None, FRESH)

        sandbox = client.only()
        assert sandbox.network.inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
        assert sandbox.contained_at_network[-1] is True, "its uid was contained when it was written"
        assert sandbox.serving_at_network[-1] is True, "and the preview served throughout"
        state = state_of(tmp_path, run_id)
        assert state.preview is not None and state.preview.phase is PreviewPhase.READY
    finally:
        await stop(adapter, run_id)


async def test_a_session_resume_beside_a_preview_keeps_its_window_down_to_the_floor(
    tmp_path: Path,
) -> None:
    """E2B counts its hour from the sandbox's last wake, which a resume late in the window its
    preview's wake opened does not move, rather than stop the preview: the session's cap stays
    that window's, and the worker's rollover renews it there."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_its_prompt()), Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client, clock)
    run_id = (await adapter.spawn(make_spec(public=True))).run_id
    await polled(client)
    await adapter.interrupt(run_id)
    woke = clock.now
    await adapter.preview_start(run_id, PREVIEW, {}, token="t1")
    clock.now += MAX_SESSION - SESSION_MARGIN - RENEW_FLOOR
    keep = clock.now + PREVIEW.stop_after
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.READY
    paused = len(client.paused)
    try:
        await adapter.resume(run_id, None, FRESH)

        assert len(client.paused) == paused, "the sandbox was not renewed"
        assert (await adapter.preview_status(run_id)).phase is PreviewPhase.READY
        state = state_of(tmp_path, run_id)
        assert state.deadline == woke + MAX_SESSION
    finally:
        await stop(adapter, run_id)


async def test_a_session_resume_below_the_floor_renews_its_sandbox_for_a_whole_cap(
    tmp_path: Path,
) -> None:
    """A session resumed with less than `RENEW_FLOOR` of its preview's window left would be
    parked almost at once, or meet E2B's kill before its rollover: the sandbox is renewed first,
    its cap counts from its own wake, and the preview is stopped for the worker to start
    again."""
    clock = Clock()
    client = FakeSandboxClient(scripts=[Script(tui=at_its_prompt()), Script(tui=TuiScript())])
    adapter = adapter_for(tmp_path, client, clock)
    run_id = (await adapter.spawn(make_spec(public=True))).run_id
    await polled(client)
    await adapter.interrupt(run_id)
    await adapter.preview_start(run_id, PREVIEW, {}, token="t1")
    clock.now += MAX_SESSION - SESSION_MARGIN - RENEW_FLOOR + timedelta(minutes=1)
    keep = clock.now + PREVIEW.stop_after
    assert (await adapter.preview_status(run_id, keep_until=keep)).phase is PreviewPhase.READY
    try:
        await adapter.resume(run_id, None, FRESH)

        state = state_of(tmp_path, run_id)
        assert state.deadline == clock.now + MAX_SESSION, "a whole cap from its wake"
        stopped = await adapter.preview_status(run_id)
        assert stopped.phase is PreviewPhase.STOPPED and stopped.detail == PREVIEW_RENEWAL
    finally:
        await stop(adapter, run_id)


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "unrecorded"])
async def test_a_session_resume_ends_the_view_before_it_writes_credentials(
    tmp_path: Path, recorded: bool
) -> None:
    """A parked session's view holds the model credential and appends its CLI's exit to the
    log: it ends, and the log is cut back to where the view found it, before the resume writes
    its credentials and reads its new segment from the log's end. The unrecorded end is a
    sandbox an older adapter woke for a shell it never recorded."""
    view = TuiScript(on_exit=([hook("SessionEnd")], [cost(9.99)]))
    client = FakeSandboxClient(
        scripts=[Script(tui=at_its_prompt()), Script(tui=resumed_at_its_prompt(0.4))], view=view
    )
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec())).run_id
    await polled(client)
    await adapter.interrupt(run_id)
    sandbox = client.only()
    log = session_log(state_of(tmp_path, run_id).session_id or "")
    before = sandbox.logs[log]
    await adapter.inspect_start(run_id, token="r1", limit=timedelta(minutes=55), credentials=FRESH)
    assert sandbox.view_open
    if not recorded:
        _edit(tmp_path, run_id, inspect=None)
    try:
        await adapter.resume(run_id, None, FRESH)

        assert sandbox.network.inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
        assert sandbox.view_at_network[-1] is False, "no view was open when it was written"
        if recorded:
            assert sandbox.logs[log].startswith(before)
            assert cost(9.99) not in sandbox.logs[log].decode(), "the view's exit was cut"
            state = state_of(tmp_path, run_id)
            assert state.inspect is not None and state.inspect.phase is InspectPhase.STOPPED
    finally:
        await stop(adapter, run_id)


async def test_a_resume_that_fails_to_launch_parks_the_sandbox_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake let fresh credentials in; a launch that failed must not leave them awake."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    await adapter.interrupt(run_id)
    sandbox = client.only()

    async def refused(command: str, **options: Any) -> int:
        raise RuntimeError("E2B 500")

    monkeypatch.setattr(sandbox, "start", refused)
    with pytest.raises(AdapterUnavailable, match="resuming run"):
        await adapter.resume(run_id, None, FRESH)
    assert sandbox.state == "paused" and client.paused == [sandbox.sandbox_id] * 2
    assert sandbox.network.inject == {}
    state = state_of(tmp_path, run_id)
    assert (state.status, state.kept) == (RunStatus.INTERRUPTED, True)


async def test_a_session_a_person_quit_resumes_in_its_kept_sandbox(tmp_path: Path) -> None:
    """A TUI never exits on its own, so a clean exit nobody asked for is a person's quit: the
    sandbox is paused and kept, and a resume brings the session back in it."""
    quit_ = TuiScript(steps=[FIRST_TURN], on_exit=leaving(0.25), ends_by_itself=True)
    client = FakeSandboxClient(scripts=[Script(tui=quit_), Script(tui=resumed_at_its_prompt(0.4))])
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec())).run_id
    await ended(adapter)
    sandbox = client.only()

    state = state_of(tmp_path, run_id)
    assert (state.status, state.park_reason, state.kept) == (
        RunStatus.INTERRUPTED,
        ParkReason.EXITED,
        True,
    )
    assert sandbox.state == "paused" and client.paused == [sandbox.sandbox_id]
    result = await adapter.result(run_id)
    assert result.park_reason is ParkReason.EXITED
    assert result.failure_reason == adapter_module._PARKED[ParkReason.EXITED]

    handle = await adapter.resume(run_id, None, FRESH)
    try:
        assert (handle.run_id, handle.status) == (run_id, RunStatus.RUNNING)
        assert len(client.commands_by_name(SESSION)) == 2
        assert client.only() is sandbox
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        await stop(adapter, run_id)


async def test_a_session_resumes_only_from_its_own_park(tmp_path: Path) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    try:
        with pytest.raises(UnknownRunError, match="isn't paused"):
            await adapter.resume(run_id, None, FRESH)
    finally:
        await adapter.interrupt(run_id)
    state = state_of(tmp_path, run_id)
    state.continued_by = "GH-12-plan-agent-0000beef"
    _write_state(tmp_path / "state" / "runs" / run_id, state)
    with pytest.raises(UnknownRunError, match="already continued by a later run"):
        await adapter.resume(run_id, None, FRESH)
    _edit(tmp_path, run_id, continued_by=None, workspace_real=None)
    with pytest.raises(UnknownRunError, match="no terminal session to resume"):
        await adapter.resume(run_id, None, FRESH)
    _edit(tmp_path, run_id, workspace_real=state.workspace_real)
    client.gone.add(client.only().sandbox_id)
    with pytest.raises(UnknownRunError, match="nothing to resume in"):
        await adapter.resume(run_id, None, FRESH)
    assert len(client.commands_by_name(SESSION)) == 1


# --------------------------------------------------------------------------- rework adoption

STASH = "5" * 40
LOST = "6" * 40
PREVIEW = PreviewSpec(
    setup=("npm ci",),
    services=(PreviewServiceSpec(name="web", command="npm run dev", port=3000),),
    stop_after=timedelta(minutes=30),
)


def reworked(usd: float) -> TuiScript:
    """A rework's segment: the CLI resumed on the session, the rework turn, one answer, Stop."""
    return TuiScript(
        steps=[
            (
                [hook("SessionStart", source="resume"), prompted(), stopped("Pushed the fix.")],
                [
                    user("Read the rework turn"),
                    assistant("msg_2", "Pushed the fix.", "req_2"),
                    turn_ended(),
                ],
            )
        ],
        on_exit=leaving(usd),
    )


def rework_of(run_id: str, session_id: str, **overrides: object) -> RunSpec:
    """The rework's spec: the next attempt's own, freshly minted secrets."""
    fresh = {"GH_TOKEN": SecretStr(FRESH_TOKEN), "ANTHROPIC_API_KEY": SecretStr(FRESH_KEY)}
    resume_from = ResumeFrom(run_id=run_id, session_id=session_id)
    return make_spec(secrets=fresh, resume_from=resume_from, **overrides)


async def finished(
    tmp_path: Path,
    *scripts: TuiScript,
    clock: Clock | None = None,
    public: bool = False,
    **client_fields: Any,
) -> tuple[ClaudeE2bAdapter, FakeSandboxClient, str, str]:
    """A first attempt that finished, its session kept in its parked sandbox for a rework."""
    client = FakeSandboxClient(scripts=[Script(tui=tui) for tui in scripts], **client_fields)
    adapter = adapter_for(tmp_path, client, clock)
    run_id = (await adapter.spawn(make_spec(public=public))).run_id
    await polled(client)
    await adapter.finish(run_id)
    state = state_of(tmp_path, run_id)
    assert (state.status, state.kept) == (RunStatus.SUCCEEDED, True)
    assert state.session_id is not None
    return adapter, client, run_id, state.session_id


def run_ids(tmp_path: Path) -> set[str]:
    return {path.name for path in (tmp_path / "state" / "runs").iterdir()}


async def test_a_rework_resumes_the_session_in_the_kept_sandbox(tmp_path: Path) -> None:
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(0.25), reworked(0.40), clock=clock
    )
    sandbox = client.only()
    parked = state_of(tmp_path, first)
    log_size, signals_size = len(sandbox.logs[session_log(sid)]), len(sandbox.signals_file)
    first_turn = sandbox.files[turn_file(0)][0]
    settings = f"{CONFIG_DIR}/settings.json"
    sandbox.files[settings] = (b"{}", "agent")  # the agent dropped its hooks

    clock.now += timedelta(minutes=20)
    handle = await adapter.spawn(rework_of(first, sid))
    run_id = handle.run_id
    assert run_id != first and handle.status is RunStatus.RUNNING

    assert len(client.sandboxes) == 1, "no sandbox is created"
    assert client.connects == [(sandbox.sandbox_id, int(MAX_SESSION.total_seconds()))]
    assert [c.envs for c in client.commands_by_name(REWORK)] == [
        {"BRANCH_GLOB": "spec/12-*", "RUN": run_id}
    ]
    argv = _nul(sandbox, ARGV)
    assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv
    assert argv[-2:] == ["--", f"Read @{turn_file(1)} and do what it says."]
    turn = sandbox.files[turn_file(1)][0].decode()
    header = rework.REWORK_HEADER.format(branch="spec/12-fix", head=REWORK_HEAD)
    assert turn.startswith(f"{header}\n\n# Workspace\n") and "Write a spec." in turn
    assert sandbox.files[turn_file(0)][0] == first_turn, "the first attempt's turn is kept"
    hooks = json.loads(sandbox.files[settings][0])["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["command"] == SIGNAL, "the seeds are written again"

    inject = sandbox.network.inject
    assert inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
    assert inject["api.github.com"] == {"Authorization": f"token {FRESH_TOKEN}"}
    assert API_KEY not in str(inject) and TOKEN not in str(inject)
    seen = client.everything_seen()
    for secret in (TOKEN, API_KEY, FRESH_TOKEN, FRESH_KEY):
        assert secret not in seen

    state = state_of(tmp_path, run_id)
    assert (state.sandbox_id, state.workspace_of) == (sandbox.sandbox_id, first)
    assert state.log_offset == state.log_read == state.segment_log_start == log_size > 0
    assert state.signals_start == state.signals_offset == signals_size > 0
    assert state.cost_base == parked.cost_seen is not None
    assert (state.segments, state.segment_turn, state.offset) == (2, True, 0)
    assert state.deadline == clock.now + MAX_SESSION
    session = await adapter.session_state(run_id)
    assert (session.log_offset, session.session_id, session.turns) == (log_size, sid, 0)

    taken = state_of(tmp_path, first)
    assert (taken.continued_by, taken.kept) == (run_id, False)
    with pytest.raises(PreviewUnavailable, match="taken over"):
        await adapter.preview_start(first, PREVIEW, {}, token="t1")

    await polled(client)
    assert await adapter.status(run_id) is RunStatus.IDLE
    await adapter.finish(run_id)
    result = await adapter.result(run_id)
    assert (result.outcome, result.session_id) == (RunStatus.SUCCEEDED, sid)
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.15)
    [segment] = results(tmp_path, run_id)
    assert (segment["num_turns"], segment["total_cost_usd"]) == (1, pytest.approx(0.15))
    assert not [line for line in mirror(tmp_path, run_id) if "msg_1" in line]
    assert len(mirror(tmp_path, run_id, "session.jsonl")) == 3 + 1, "its turn and its cost"
    assert len(mirror(tmp_path, run_id, "signals.jsonl")) == 3 + 1, "its hooks and SessionEnd"
    assert state_of(tmp_path, run_id).session_turns == 1


async def test_a_retry_tells_rework_sh_its_mode_and_opens_with_the_failure_header(
    tmp_path: Path,
) -> None:
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(0.25), reworked(0.40))
    sandbox = client.only()
    fresh = {"GH_TOKEN": SecretStr(FRESH_TOKEN), "ANTHROPIC_API_KEY": SecretStr(FRESH_KEY)}

    retry = ResumeFrom(run_id=first, session_id=sid, mode="retry")
    run_id = (await adapter.spawn(make_spec(secrets=fresh, resume_from=retry))).run_id

    assert [c.envs for c in client.commands_by_name(REWORK)] == [
        {"BRANCH_GLOB": "spec/12-*", "RUN": run_id, "MODE": "retry"}
    ]
    turn = sandbox.files[turn_file(1)][0].decode()
    assert turn.startswith(f"{rework.retry_header('spec/12-fix', REWORK_HEAD)}\n\n")
    inject = sandbox.network.inject
    assert inject["api.github.com"] == {"Authorization": f"token {FRESH_TOKEN}"}
    assert inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
    assert TOKEN not in str(inject) and API_KEY not in str(inject)

    await polled(client)
    await adapter.finish(run_id)
    client.rework_result = CommandOutput(5, "", "the checkout is on no branch")
    again = ResumeFrom(run_id=run_id, session_id=sid, mode="retry")
    with pytest.raises(NoBranchHeld):
        await adapter.spawn(make_spec(secrets=fresh, resume_from=again))


async def test_an_adopted_gitlab_session_sends_its_token_only_to_its_gitlab_host(
    tmp_path: Path,
) -> None:
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(0.25), reworked(0.40))
    sandbox = client.only()
    fresh = FRESH | {"GIT_HTTP_HOST": SecretStr("gitlab.example.com")}

    spec = make_spec(secrets=fresh, resume_from=ResumeFrom(run_id=first, session_id=sid))
    run_id = (await adapter.spawn(spec)).run_id

    inject = sandbox.network.inject
    host = "gitlab.example.com"
    assert {host: inject[host]} == _gitlab_inject(host, FRESH_TOKEN)
    assert "github.com" not in inject and "api.github.com" not in inject
    assert "gitlab.example.com" in sandbox.network.allow
    assert "gitlab.example.com" in state_of(tmp_path, run_id).allow, "a resume reuses it"


async def test_an_adopted_github_session_keeps_githubs_rules(tmp_path: Path) -> None:
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(0.25), reworked(0.40))
    sandbox = client.only()
    spawned = sandbox.network.allow

    await adapter.spawn(rework_of(first, sid))

    inject = sandbox.network.inject
    assert {host: inject[host] for host in ("github.com", "api.github.com")} == _github_inject(
        FRESH_TOKEN
    )
    assert sandbox.network.allow == spawned


async def test_a_rework_routes_and_exposes_its_own_project_secrets(tmp_path: Path) -> None:
    """The rework's choices and fresh values, not the earlier run's: its proxy rule carries the
    new header value and its env file the new exposed one."""
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(0.25), reworked(0.40))
    route = SecretRoute(
        name="RAILWAY_TOKEN_X", host="backboard.railway.com", header="project-access-token"
    )
    spec = make_spec(
        secrets={
            **FRESH,
            "RAILWAY_TOKEN_X": SecretStr("rw-theReworksOwnToken000"),
            "DB_PASSWORD": SecretStr("db-theReworksOwnPassword"),
        },
        resume_from=ResumeFrom(run_id=first, session_id=sid),
        egress=("backboard.railway.com",),
        secret_routes=(route,),
        secret_env=("DB_PASSWORD",),
    )

    run_id = (await adapter.spawn(spec)).run_id

    sandbox = client.only()
    rule = sandbox.network.inject["backboard.railway.com"]
    assert rule == {"project-access-token": "rw-theReworksOwnToken000"}
    env = dict(entry.split("=", 1) for entry in _nul(sandbox, ENV) if entry)
    assert env["DB_PASSWORD"] == "db-theReworksOwnPassword"
    assert env["RAILWAY_TOKEN_X"] == PLACEHOLDER_ROUTED
    await polled(client)
    await adapter.finish(run_id)


async def test_a_second_rework_continues_the_same_sandbox(tmp_path: Path) -> None:
    """The second rework names the first rework's run; the sandbox is still the first run's,
    which its metadata names and the keep rule keeps."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(0.25), reworked(0.40), reworked(0.70)
    )
    second = (await adapter.spawn(rework_of(first, sid))).run_id
    await polled(client)
    await adapter.finish(second)

    third = (await adapter.spawn(rework_of(second, sid))).run_id
    sandbox = client.only()
    assert turn_file(2) in sandbox.files and _nul(sandbox, ARGV)[-1].startswith(
        f"Read @{turn_file(2)} "
    )
    state = state_of(tmp_path, third)
    assert (state.sandbox_id, state.workspace_of) == (sandbox.sandbox_id, first)
    assert state_of(tmp_path, second).continued_by == third
    await polled(client)
    await adapter.finish(third)
    result = await adapter.result(third)
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.30)


async def test_a_rework_says_what_it_stashed_and_where_lost_commits_are(tmp_path: Path) -> None:
    """Uncommitted work is stashed, not discarded (U5), and commits the PR head lacks stay
    reachable: the turn says where both went."""
    reported = CommandOutput(0, f"spec/12-fix {REWORK_HEAD}\nstash {STASH}\nlost {LOST}\n", "")
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), rework_result=reported
    )
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        turn = client.only().files[turn_file(1)][0].decode()
        header = rework.REWORK_HEADER.format(branch="spec/12-fix", head=REWORK_HEAD)
        stashed = rework.REWORK_STASHED.format(stash=STASH)
        lost = rework.REWORK_LOST.format(lost=LOST)
        assert turn.startswith(f"{header} {stashed} {lost}\n\n")
    finally:
        await stop(adapter, run_id)


async def test_a_rework_quiets_the_sandbox_before_it_writes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inspect shell is a human's: it may not run once the proxy holds the rework's
    credentials. Agent-written servers are public in a kept sandbox, so the preview's uid is
    contained before they go in, and the preview serves on through the adoption, contained, and
    moves to the rework's run (design.md §7.10). The checkout moves after the credentials, which
    its fetch needs, and the CLI starts last."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY
    sandbox = client.only()
    order: list[str] = []
    run, start, update_network = sandbox.run, sandbox.start, sandbox.update_network

    async def ran(command: str, **options: Any) -> CommandOutput:
        order.append(command)
        return await run(command, **options)

    async def started_(command: str, **options: Any) -> int:
        order.append(command)
        return await start(command, **options)

    async def network(policy: NetworkPolicy) -> None:
        order.append("network")
        await update_network(policy)

    async def end_inspect(*_: Any) -> None:
        order.append("end inspect")

    monkeypatch.setattr(sandbox, "run", ran)
    monkeypatch.setattr(sandbox, "start", started_)
    monkeypatch.setattr(sandbox, "update_network", network)
    monkeypatch.setattr(adapter, "_end_inspect", end_inspect)
    run_id = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        watched = (PREVIEW_STOP, "end inspect", SCRUB, FIREWALL, "network", REWORK, SESSION)
        steps = [o for o in order if o in watched]
        assert steps == ["end inspect", SCRUB, FIREWALL, "network", REWORK, SESSION]
        assert sandbox.contained_at_network[-1] is True, "its uid contained when they went in"
        assert sandbox.serving_at_network[-1] is True, "and the preview served through them"
        given_up = state_of(tmp_path, first).preview
        assert given_up is not None and given_up.phase is PreviewPhase.STOPPED
        assert given_up.detail == PREVIEW_CONTINUED
        rework_ = state_of(tmp_path, run_id)
        carried = rework_.preview
        assert carried is not None and carried.phase is PreviewPhase.READY, "it serves on"
        assert carried.token == "t1" and rework_.public is True
    finally:
        await stop(adapter, run_id)


async def test_a_rework_carries_a_ready_preview_over_to_its_run(tmp_path: Path) -> None:
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(0.25), reworked(0.40), public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY

    second = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        assert (await adapter.preview_status(first)).detail == PREVIEW_CONTINUED
        carried = await adapter.preview_status(second)
        assert carried.phase is PreviewPhase.READY and carried.token == "t1"
        sandbox = client.only()
        assert sandbox.network.inject and sandbox.contained_at_network[-1] is True
        assert sandbox.serving_at_network[-1] is True, "the preview served through the adoption"
    finally:
        await adapter.cancel(second)


async def test_a_rework_into_a_sandbox_found_paused_carries_no_preview_from_its_record(
    tmp_path: Path,
) -> None:
    """A worker that died between a park's pause and its record left the preview READY on
    record in a paused sandbox, which wakes with nothing serving: the preview is recorded for its
    renewal, for the worker to start again, rather than carried over as serving."""
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), reworked(0.40), clock=clock, public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY
    await client.pause(client.only().sandbox_id)
    restarted = adapter_for(tmp_path, client, clock)

    second = (await restarted.spawn(rework_of(first, sid, public=True))).run_id
    try:
        given_up = await restarted.preview_status(first)
        assert given_up.phase is PreviewPhase.STOPPED and given_up.detail == PREVIEW_RENEWAL
        assert (await restarted.preview_status(second)).phase is PreviewPhase.STOPPED
    finally:
        await stop(restarted, second)


async def test_a_rework_beside_a_preview_carries_it_and_keeps_its_window_down_to_the_floor(
    tmp_path: Path,
) -> None:
    """A rework is a session: one that takes the sandbox over late in the window its preview's
    wake opened carries the preview rather than stop it, while `RENEW_FLOOR` is left, and its cap
    is that window's, where the worker's rollover renews it."""
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), reworked(0.40), clock=clock, public=True
    )
    woke = clock.now
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    clock.now += MAX_SESSION - SESSION_MARGIN - RENEW_FLOOR
    keep = clock.now + PREVIEW.stop_after
    assert (await adapter.preview_status(first, keep_until=keep)).phase is PreviewPhase.READY
    paused = len(client.paused)

    second = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        assert len(client.paused) == paused, "the sandbox was not renewed"
        carried = await adapter.preview_status(second)
        assert carried.phase is PreviewPhase.READY and carried.token == "t1"
        state = state_of(tmp_path, second)
        assert state.deadline == woke + MAX_SESSION
    finally:
        await stop(adapter, second)


async def test_a_rework_below_the_floor_renews_its_sandbox_and_carries_no_preview(
    tmp_path: Path,
) -> None:
    """With less than `RENEW_FLOOR` of its preview's window left, a rework would meet E2B's cap
    almost at once: the sandbox is renewed before the rework's credentials go in, its cap counts
    from that wake, and the preview stops for the worker to start again rather than move."""
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), reworked(0.40), clock=clock, public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    clock.now += MAX_SESSION - SESSION_MARGIN - RENEW_FLOOR + timedelta(minutes=1)
    keep = clock.now + PREVIEW.stop_after
    assert (await adapter.preview_status(first, keep_until=keep)).phase is PreviewPhase.READY
    sandbox = client.only()
    paused, networks = len(client.paused), len(sandbox.networks)

    second = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        assert len(client.paused) == paused + 1, "paused, then woken into a new window"
        injected = [bool(policy.inject) for policy in sandbox.networks[networks:]]
        assert injected == [False, True], "renewed before the rework's credentials went in"
        state = state_of(tmp_path, second)
        assert state.awake_since == clock.now
        assert state.deadline == clock.now + MAX_SESSION
        given_up = await adapter.preview_status(first)
        assert given_up.phase is PreviewPhase.STOPPED and given_up.detail == PREVIEW_RENEWAL
        assert (await adapter.preview_status(second)).phase is PreviewPhase.STOPPED
    finally:
        await stop(adapter, second)


async def test_a_rework_whose_renewal_killed_its_sandbox_has_no_session_to_continue(
    tmp_path: Path,
) -> None:
    """A renewal whose park fails kills the sandbox, and the session with it: the spawn says
    so, and the earlier run is neither continued nor kept."""
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), clock=clock, public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    clock.now += MAX_SESSION - SESSION_MARGIN - RENEW_FLOOR + timedelta(minutes=1)
    keep = clock.now + PREVIEW.stop_after
    assert (await adapter.preview_status(first, keep_until=keep)).phase is PreviewPhase.READY
    client.pause_error = RuntimeError("E2B 500")

    with pytest.raises(SessionUnavailable, match="renewing"):
        await adapter.spawn(rework_of(first, sid, public=True))

    state = state_of(tmp_path, first)
    assert (state.continued_by, state.kept) == (None, False)
    assert client.killed == [client.only().sandbox_id]
    assert len(client.commands_by_name(SESSION)) == 1, "only the first attempt's launch"


@pytest.mark.parametrize(
    "report", ["setup running", "serving\nhead 0123abc"], ids=["in its setup", "past its setup"]
)
async def test_a_rework_starts_a_preview_still_starting_again_rather_than_carry_it(
    tmp_path: Path, report: str
) -> None:
    """Only a ready preview moves to the rework. One still starting is stopped before the
    rework's credentials go in and recorded for its renewal, as a setup an agent's stop would
    cut is, so it starts again on the rework's run rather than serve unwatched."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), reworked(0.40), public=True, preview_reports=[report]
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.STARTING
    sandbox = client.only()

    second = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        given_up = await adapter.preview_status(first)
        assert given_up.phase is PreviewPhase.STOPPED and given_up.detail == PREVIEW_RENEWAL
        assert (await adapter.preview_status(second)).phase is PreviewPhase.STOPPED
        assert sandbox.serving_at_network[-1] is False, "its services were gone"
    finally:
        await stop(adapter, second)


async def test_a_rework_that_fails_to_launch_leaves_the_preview_to_the_earlier_run_to_renew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready preview moves only with an adoption that holds: one undone after the preview
    served on beside the rework's credentials parks the sandbox, and the earlier run, continuable
    again, keeps the preview stopped for its renewal, not as one a rework took over."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY
    sandbox = client.only()

    async def refused(command: str, **options: Any) -> int:
        raise RuntimeError("E2B 500")

    monkeypatch.setattr(sandbox, "start", refused)
    with pytest.raises(AdapterUnavailable):
        await adapter.spawn(rework_of(first, sid, public=True))

    state = state_of(tmp_path, first)
    assert (state.continued_by, state.kept, sandbox.state) == (None, True, "paused")
    assert state.preview is not None and state.preview.phase is PreviewPhase.STOPPED
    assert state.preview.detail == PREVIEW_RENEWAL


async def test_ending_the_continued_runs_preview_leaves_the_rework_its_sandbox(
    tmp_path: Path,
) -> None:
    """A run a rework continued never decides its sandbox's fate (R23): its preview's end stops
    the preview's services and nothing else, so the rework keeps running on its credentials. Its
    record still holds the preview when the worker died before giving it up to the rework."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY
    held = state_of(tmp_path, first).preview
    run_id = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    _edit(tmp_path, first, preview=held)
    sandbox = client.only()
    try:
        await adapter.preview_stop(first)

        assert (await adapter.preview_status(first)).phase is PreviewPhase.STOPPED
        assert sandbox.state == "running" and sandbox.network.inject, "its rules stand"
        assert await adapter.status(run_id) is RunStatus.RUNNING
    finally:
        if sandbox.state == "running":
            await stop(adapter, run_id)
        else:
            # A supervisor polling a paused sandbox would never let a stop finish.
            for task in adapter._supervisors.values():
                task.cancel()


async def test_a_continued_runs_preview_read_at_the_cap_leaves_the_rework_its_sandbox(
    tmp_path: Path,
) -> None:
    """A run a rework continued never decides its sandbox's fate (R23), at E2B's cap either: the
    rework's live session holds the sandbox, and its own end decides it. Its record still holds
    the preview when the worker died before giving it up to the rework."""
    clock = Clock()
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), clock=clock, public=True
    )
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    assert (await adapter.preview_status(first)).phase is PreviewPhase.READY
    held = state_of(tmp_path, first).preview
    run_id = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    _edit(tmp_path, first, preview=held)
    sandbox = client.only()
    connects = len(client.connects)
    try:
        clock.now += MAX_SESSION - SESSION_MARGIN
        await adapter.preview_status(first, keep_until=clock.now + PREVIEW.stop_after)

        assert sandbox.state == "running" and sandbox.network.inject, "its rules stand"
        assert len(client.connects) == connects, "nor did the read stretch its timeout"
        assert await adapter.status(run_id) is RunStatus.RUNNING
    finally:
        if sandbox.state == "running":
            await stop(adapter, run_id)
        else:
            # A supervisor polling a paused sandbox would never let a stop finish.
            for task in adapter._supervisors.values():
                task.cancel()


async def test_a_rework_counts_its_cap_from_the_wake_its_preview_made(tmp_path: Path) -> None:
    """A rework that takes over a sandbox its preview keeps awake inherits that wake's hour: its
    backstop parks it before E2B's cap ends the sandbox."""
    clock = Clock()
    adapter, _, first, sid = await finished(
        tmp_path, at_its_prompt(), TuiScript(), clock=clock, public=True
    )
    woke = clock.now
    await adapter.preview_start(first, PREVIEW, {}, token="t1")
    clock.now += timedelta(minutes=20)

    run_id = (await adapter.spawn(rework_of(first, sid, public=True))).run_id
    try:
        state = state_of(tmp_path, run_id)
        assert state.awake_since == woke
        assert state.deadline == woke + MAX_SESSION
    finally:
        await stop(adapter, run_id)


def _edit(tmp_path: Path, run_id: str, **fields: object) -> None:
    state = state_of(tmp_path, run_id).model_copy(update=fields)
    _write_state(tmp_path / "state" / "runs" / run_id, state)


@pytest.mark.parametrize(
    ("case", "woken"),
    [
        ("no state", False),
        ("no session launched", False),
        ("not kept", False),
        ("another session", False),
        ("another repo", False),
        ("another ticket", False),
        ("another agent", False),
        ("sandbox gone", False),
        ("preview changed", False),
        ("not the branch", True),
        ("branch gone", True),
        ("log gone", True),
        ("garbled report", True),
    ],
)
async def test_an_unresumable_session_raises_session_unavailable_and_launches_nothing(
    tmp_path: Path, case: str, woken: bool
) -> None:
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(), TuiScript())
    sandbox = client.only()
    spec = rework_of(first, sid)
    if case == "no state":
        spec = rework_of("GH-12-plan-agent-00000000", sid)
    elif case == "no session launched":
        _edit(tmp_path, first, workspace_real=None)
    elif case == "not kept":
        _edit(tmp_path, first, kept=False)
    elif case == "another session":
        spec = rework_of(first, "5f0b1f2e-0000-4000-8000-0000000000aa")
    elif case == "another repo":
        _edit(tmp_path, first, repo="acme/gadgets")
    elif case == "another ticket":
        _edit(tmp_path, first, ticket="GH-13")
    elif case == "another agent":
        _edit(tmp_path, first, agent="build-agent")
    elif case == "sandbox gone":
        client.gone.add(sandbox.sandbox_id)
    elif case == "preview changed":
        spec = rework_of(first, sid, public=True)
    elif case == "not the branch":
        client.rework_result = CommandOutput(3, "", "the checkout's branch does not match")
    elif case == "branch gone":
        client.rework_result = CommandOutput(4, "", "fatal: couldn't find remote ref")
    elif case == "garbled report":
        client.rework_result = CommandOutput(0, "hook output\nspec/12-fix 0123\n", "")
    else:
        sandbox.logs.clear()
    before = state_of(tmp_path, first)
    runs = run_ids(tmp_path)

    with pytest.raises(SessionUnavailable):
        await adapter.spawn(spec)

    assert len(client.commands_by_name(SESSION)) == 1, "only the first attempt's launch"
    assert run_ids(tmp_path) == runs
    after = state_of(tmp_path, first)
    assert (after.continued_by, after.kept) == (before.continued_by, before.kept)
    assert client.killed == []
    if woken:
        assert len(client.connects) == 1
        assert sandbox.state == "paused" and client.paused == [sandbox.sandbox_id] * 2
        assert sandbox.network.inject == {}, "parked again without the rework's credentials"
        assert len(client.commands_by_name(REWORK)) == (case != "log gone")
    else:
        assert client.connects == [] and client.paused == [sandbox.sandbox_id]


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        ("info", AdapterUnavailable),
        ("connect", AdapterUnavailable),
        ("rework", AdapterUnavailable),
        ("launch", AdapterUnavailable),
        ("launch on E2B's side", SubstrateUnavailable),
    ],
)
async def test_an_e2b_outage_during_adoption_is_transient_not_session_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, raised: type[Exception]
) -> None:
    """A retry may find the session, so nothing says it is gone: the earlier run stays as
    continuable as it was, and the next tick's spawn continues it."""
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(), TuiScript())
    sandbox = client.only()
    outage: Exception = RuntimeError("E2B 503")
    if failure == "launch on E2B's side":
        outage = e2b_exceptions.SandboxException("E2B 503")
    start = sandbox.start
    if failure == "info":
        client.info_error = outage
    elif failure == "connect":
        client.connect_error = outage
    elif failure == "rework":
        client.rework_result = CommandOutput(128, "", "fatal: unable to access: 503")
    else:

        async def refused(command: str, **options: Any) -> int:
            raise outage

        monkeypatch.setattr(sandbox, "start", refused)

    with pytest.raises(raised) as caught:
        await adapter.spawn(rework_of(first, sid))
    assert not isinstance(caught.value, SessionUnavailable)
    assert caught.type is raised
    assert len(client.commands_by_name(SESSION)) == 1
    state = state_of(tmp_path, first)
    assert (state.continued_by, state.kept) == (None, True)
    assert sandbox.state == "paused" and sandbox.network.inject == {}

    client.info_error = client.connect_error = None
    client.rework_result = CommandOutput(0, f"spec/12-fix {REWORK_HEAD}\n", "")
    monkeypatch.setattr(sandbox, "start", start)
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    try:
        assert state_of(tmp_path, first).continued_by == run_id
    finally:
        await stop(adapter, run_id)


async def test_an_adoption_a_worker_died_in_is_taken_over_by_the_retry(tmp_path: Path) -> None:
    """The worker died after the wake and the credentials: the earlier run names a continuer
    that never came to be, and the sandbox is awake with that attempt's headers."""
    _, client, first, sid = await finished(tmp_path, at_its_prompt(), TuiScript())
    sandbox = client.only()
    _edit(tmp_path, first, continued_by="GH-12-plan-agent-0000dead")
    await client.connect(sandbox.sandbox_id, timeout_seconds=3600)
    dead = {"api.anthropic.com": {"x-api-key": "sk-ant-the-dead-attempts-key"}}
    await sandbox.update_network(NetworkPolicy(allow=(), inject=dead))
    # It died inside start(SESSION): that attempt's CLI runs on.
    client.scripts.insert(0, Script(tui=TuiScript(on_exit=([hook("SessionEnd", "dead")], []))))
    await sandbox.start(SESSION)
    networks = len(sandbox.networks)

    restarted = adapter_for(tmp_path, client)
    run_id = (await restarted.spawn(rework_of(first, sid))).run_id
    try:
        assert sandbox.network.inject["api.anthropic.com"] == {"x-api-key": FRESH_KEY}
        assert state_of(tmp_path, first).continued_by == run_id
        assert len(client.commands_by_name(SESSION)) == 3
        ended_at = sandbox.signals_file.index(b'"at": "dead"')
        assert ended_at < state_of(tmp_path, run_id).signals_start, "ended before the rework"
        # The dead worker's wake was never recorded, so the retry renews the sandbox first, and
        # its park drops that attempt's headers before the rework's go in.
        assert [bool(policy.inject) for policy in sandbox.networks[networks:]] == [False, True]
    finally:
        await stop(restarted, run_id)


async def test_a_rework_stops_an_unrecorded_continuer_and_continues_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost tick launched run B but never recorded it, so the retry names the first run
    again. B runs on in the workspace with live credentials and no supervisor: the retry stops
    it, never spawns fresh beside it, and continues the session from where B left it."""
    adapter, client, first, sid = await finished(
        tmp_path, at_its_prompt(0.25), reworked(0.40), reworked(0.70)
    )
    ghost = (await adapter.spawn(rework_of(first, sid))).run_id
    await polled(client)
    for task in adapter._supervisors.values():
        task.cancel()  # the worker died
    await asyncio.gather(*adapter._supervisors.values(), return_exceptions=True)
    sandbox = client.only()
    restarted = adapter_for(tmp_path, client)

    client.connect_error = RuntimeError("E2B 503")
    with pytest.raises(AdapterUnavailable):
        await restarted.spawn(rework_of(first, sid))
    assert state_of(tmp_path, first).continued_by == ghost, "the retry must find it again"
    client.connect_error = None

    woken: list[tuple[str, dict[str, Any]]] = []
    connect = client.connect

    async def watched(sandbox_id: str, *, timeout_seconds: int) -> FakeSandbox:
        woken.append((sandbox.state, dict(sandbox.network.inject)))
        return await connect(sandbox_id, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(client, "connect", watched)
    run_id = (await restarted.spawn(rework_of(first, sid))).run_id
    try:
        stopped_ = state_of(tmp_path, ghost)
        assert stopped_.status is RunStatus.CANCELLED
        assert (stopped_.continued_by, stopped_.kept) == (run_id, False)
        assert woken[-1] == ("paused", {}), "parked without its credentials before the wake"
        argv = _nul(sandbox, ARGV)
        assert argv[argv.index("--resume") + 1] == sid
        assert argv[-1].startswith(f"Read @{turn_file(2)} ")
        state = state_of(tmp_path, run_id)
        assert (state.workspace_of, state.segments) == (first, 3)
        assert state.cost_base == stopped_.cost_seen
        assert state.log_offset == len(sandbox.logs[session_log(sid)]) > stopped_.log_offset
        await polled(client)
        await restarted.finish(run_id)
        result = await restarted.result(run_id)
        assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.30)
    finally:
        await stop(restarted, run_id)


async def test_a_rework_that_cannot_be_launched_leaves_the_earlier_run_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before the wake, a spawn leaves no mark that would refuse the next one."""
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(), TuiScript())

    def refused(*_: Any, **__: Any) -> Any:
        raise SpawnError("an attached path has whitespace", reason=SpawnReason.CONFIG)

    monkeypatch.setattr(ClaudeHarness, "session_adopt", refused)
    with pytest.raises(SpawnError, match="whitespace"):
        await adapter.spawn(rework_of(first, sid))
    state = state_of(tmp_path, first)
    assert (state.continued_by, state.kept) == (None, True)
    assert client.connects == []


async def test_an_adopted_run_survives_a_worker_restart(tmp_path: Path) -> None:
    """Its mirrors begin at the log's offset, which a reattach truncates and replays against."""
    adapter, client, first, sid = await finished(tmp_path, at_its_prompt(0.25), reworked(0.40))
    run_id = (await adapter.spawn(rework_of(first, sid))).run_id
    await polled(client)
    for task in adapter._supervisors.values():
        task.cancel()
    await asyncio.gather(*adapter._supervisors.values(), return_exceptions=True)

    restarted = adapter_for(tmp_path, client)
    await restarted.status(run_id)
    await polled(client)
    assert await restarted.status(run_id) is RunStatus.IDLE
    await restarted.finish(run_id)
    result = await restarted.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.15)
    assert not [line for line in mirror(tmp_path, run_id) if "msg_1" in line]
    assert len(results(tmp_path, run_id)) == 1


# --------------------------------------------------------------------------- the session smoke


def smoked(tmp_path: Path, *scripts: TuiScript) -> tuple[ClaudeE2bAdapter, FakeSandboxClient]:
    """An adapter for the smoke, whose sessions play `scripts`: the spawn's, then the resume's."""
    client = FakeSandboxClient(scripts=[Script(tui=tui) for tui in scripts])
    return adapter_for(tmp_path, client), client


async def smoke(
    tmp_path: Path, adapter: ClaudeE2bAdapter, client: FakeSandboxClient, **options: Any
) -> tuple[RunResult, list[str]]:
    spec = smoke_spec("claude", API_KEY)
    run = session_smoke_run(
        adapter,
        spec,
        poll=0,
        reattach=lambda: adapter_for(tmp_path, client),
        **{"hold": timedelta(0), **options},
    )
    return await asyncio.wait_for(run, 10)


async def test_the_session_smoke_passes_a_session_that_idles_rolls_over_and_finishes(
    tmp_path: Path,
) -> None:
    adapter, client = smoked(tmp_path, at_its_prompt(0.25), resumed_at_its_prompt(0.40))

    result, problems = await smoke(tmp_path, adapter, client)

    assert problems == []
    assert result.outcome is RunStatus.SUCCEEDED
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.40)
    sandbox = client.only()
    assert len(client.commands_by_name(CAPTURE)) == 1, "the screen was read for a dialog"
    assert all(task.done() for task in adapter._supervisors.values()), "the first worker died"
    # The restarted worker found the session awake; only the resume woke it.
    assert len(client.connects) == 2 and client.paused == [sandbox.sandbox_id] * 2
    assert [c.envs for c in client.commands_by_name(FINISH)] == [
        {"MODE": "hangup"},
        {"MODE": "exit"},
    ], "the rollover hangs up, the finish types /exit"
    argv = _nul(sandbox, ARGV)
    assert argv[argv.index("--resume") + 1] == result.session_id and "--" not in argv
    assert len(results(tmp_path, result.run_id)) == 2, "two segments, each ended once"


async def test_the_session_smoke_fails_a_tui_that_shows_a_first_run_dialog(
    tmp_path: Path,
) -> None:
    """A dialog the seeds should have answered: the session idles, but a human would meet it."""
    dialog = "Do you trust the files in this folder?\n\u276f\xa01. Yes, I trust this folder"
    first = TuiScript(steps=[FIRST_TURN], on_exit=leaving(0.25), pane=dialog)
    adapter, client = smoked(tmp_path, first, resumed_at_its_prompt(0.40))

    _, problems = await smoke(tmp_path, adapter, client)

    assert len(problems) == 1 and "Yes, I trust this folder" in problems[0]


async def test_the_session_smoke_fails_a_session_that_does_not_outlive_its_hold(
    tmp_path: Path,
) -> None:
    """E2B ends a command started with a time limit, so an idle session must stay up."""
    leaves = TuiScript(steps=[FIRST_TURN, ([], [])], on_exit=leaving(0.25), ends_by_itself=True)
    adapter, client = smoked(tmp_path, leaves)

    _, problems = await smoke(tmp_path, adapter, client, hold=timedelta(seconds=0.2))

    assert problems and "while it idled" in problems[0]
    assert client.commands_by_name(FINISH) == [], "nothing is left to finish"


async def test_the_session_smoke_fails_a_log_the_translator_cannot_read_whole(
    tmp_path: Path,
) -> None:
    """A segment hung up is never checked live, so the smoke reads its log again: here the
    first turn never closed with its `turn_duration`."""
    answered = [user("go"), assistant("msg_1", "Hi", "req_1"), user("and then?")]
    unclosed = ([started(), prompted(), stopped()], answered)
    first = TuiScript(steps=[unclosed], on_exit=leaving(0.25))
    adapter, client = smoked(tmp_path, first, resumed_at_its_prompt(0.40))

    _, problems = await smoke(tmp_path, adapter, client)

    assert problems == ["the session log carried no system turn_duration line"]


async def test_the_session_smoke_reports_an_unknown_log_line_without_failing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    novel = json.dumps({"type": "brand-new-kind", "at": "now"})
    turn = (FIRST_TURN[0], [*FIRST_TURN[1][:1], novel, *FIRST_TURN[1][1:]])
    first = TuiScript(steps=[turn], on_exit=leaving(0.25))
    adapter, client = smoked(tmp_path, first, resumed_at_its_prompt(0.40))

    with caplog.at_level(logging.INFO, logger=smoke_module.__name__):
        _, problems = await smoke(tmp_path, adapter, client)

    assert problems == []
    assert "brand-new-kind" in caplog.text


async def test_the_session_smoke_fails_a_session_that_never_reaches_its_prompt(
    tmp_path: Path,
) -> None:
    """The build the worker would trust is one whose TUI says when a turn ends."""
    working = ([started(), prompted()], [user("Read the turn")])
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript([working], on_exit=leaving(0.1)))])
    adapter = adapter_for(tmp_path, client)

    _, problems = await asyncio.wait_for(
        session_smoke_run(
            adapter,
            smoke_spec("claude", API_KEY),
            turn_timeout=timedelta(seconds=0.2),
            poll=0,
        ),
        10,
    )

    assert problems and "no Stop hook fired" in problems[0]
    assert [c.envs for c in client.commands_by_name(FINISH)] == [{"MODE": "hangup"}], "cancelled"
