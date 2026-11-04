"""An interactive codex run on E2B against a fake sandbox: a spawn learns its thread from the
rollout codex begins, and the session verbs run end to end (decision 56)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_e2b import (
    CODEX_THREAD,
    CONSENT_SCREEN,
    FakeSandbox,
    FakeSandboxClient,
    Script,
    TuiScript,
    codex_hook,
    codex_turn,
    rollout_path,
)
from pydantic import SecretStr

from runtime.adapters.codex_cli.cli import INPUT_MARK
from runtime.adapters.e2b import CodexE2bAdapter
from runtime.adapters.e2b import adapter as adapter_module
from runtime.adapters.e2b.adapter import _read_state, _RunState
from runtime.adapters.e2b.codex import AUTH_PATH, CONFIG_PATH
from runtime.adapters.e2b.layout import (
    ARGV,
    ATTACHMENTS_DIR,
    CODEX_FINISH,
    CODEX_NATIVE,
    CONFIG_DIR,
    CONSENT,
    FINISH,
    INSPECT_STOP,
    ROLLOUT_FIND,
    SESSION,
    SESSION_POLL,
    SIGNALS,
    VIEW_ARGV,
    VIEW_ENV,
    VIEW_SIGNALS,
    turn_file,
)
from runtime.adapters.e2b.smoke import session_smoke_run, smoke_spec
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    IdleReason,
    InspectLoginRefused,
    InspectPhase,
    McpServerSpec,
    McpTransport,
    ParkReason,
    ResumeFrom,
    RunAttachment,
    RunResult,
    RunSpec,
    RunStatus,
    SpawnError,
    SpawnReason,
    Workspace,
)

TOKEN = "ghs_codexSessionTokenMustNeverEnterTheSandbox"  # gitleaks:allow
API_KEY = "sk-proj-codexSessionKeyMustNeverEnterTheSandbox"
FRESH = {
    "GH_TOKEN": SecretStr("ghs_codexFreshTokenMustNeverEnterTheSandbox"),  # gitleaks:allow
    "OPENAI_API_KEY": SecretStr("sk-proj-codexFreshKeyMustNeverEnterTheSandbox"),
}
OTHER_THREAD = "0199a213-81c0-7800-8aa1-000000000001"
LOG = rollout_path(CODEX_THREAD)
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def make_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "impl-agent",
        "ticket": "GH-9",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="impl/9-{slug}"),
        "prompts": AgentPrompts(system="You are the impl agent.", instructions="Fix it."),
        "secrets": {"GH_TOKEN": SecretStr(TOKEN), "OPENAI_API_KEY": SecretStr(API_KEY)},
        "model": "gpt-5.1-codex",
    }
    return RunSpec.model_validate(defaults | overrides)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def adapter_for(
    tmp_path: Path, client: FakeSandboxClient, clock: Clock | None = None, *, ask_tool: bool = False
) -> CodexE2bAdapter:
    return CodexE2bAdapter(
        template="factorial-agent:build-1",
        state_root=tmp_path / "state",
        deployment="test",
        client=client,
        now=clock or Clock(),
        poll_interval=0,
        ask_tool=ask_tool,
    )


def at_its_prompt() -> TuiScript:
    """A spawn's first turn, to its Stop, behind the folder consent; then the prompt."""
    return TuiScript(steps=[codex_turn(meta=True)], consent=True)


def turnless() -> TuiScript:
    """A resume with no turn: back at the prompt once its consent is taken, firing no hook."""
    return TuiScript(consent=True)


def first_turn_halves() -> tuple[tuple[list[str], list[str]], tuple[list[str], list[str]]]:
    """The spawn's first turn cut after its prompt: its start, and its end with the Stop."""
    hooks, log = codex_turn(meta=True)
    return (hooks[:2], log[:3]), (hooks[2:], log[3:])


async def polled(client: FakeSandboxClient, n: int = 2) -> None:
    start = len(client.commands_by_name(SESSION_POLL))
    for _ in range(10_000):
        if len(client.commands_by_name(SESSION_POLL)) >= start + n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"the supervisor did not poll {n} more times")


async def idle(
    tmp_path: Path, *scripts: TuiScript, clock: Clock | None = None
) -> tuple[CodexE2bAdapter, FakeSandboxClient, str]:
    """Spawned, at its prompt after its first turn; each further script is a later launch's."""
    client = FakeSandboxClient(scripts=[Script(tui=tui) for tui in scripts])
    adapter = adapter_for(tmp_path, client, clock)
    run_id = (await adapter.spawn(make_spec())).run_id
    await polled(client)
    assert await adapter.status(run_id) is RunStatus.IDLE
    return adapter, client, run_id


async def stop(adapter: CodexE2bAdapter, run_id: str) -> None:
    await adapter.cancel(run_id)
    await adapter.drain()


def state_of(tmp_path: Path, run_id: str) -> _RunState:
    return _read_state(tmp_path / "state" / "runs" / run_id, run_id)


def mirror(tmp_path: Path, run_id: str) -> list[str]:
    path = tmp_path / "state" / "runs" / run_id / "stdout.jsonl"
    return path.read_text("utf-8").splitlines() if path.exists() else []


def argv_of(sandbox: FakeSandbox) -> list[str]:
    return sandbox.files[ARGV][0].decode().split("\0")[:-1]


def commands(sandbox: FakeSandbox, *names: str) -> list[str]:
    return [c.command for c in sandbox.commands if c.command in names]


# --------------------------------------------------------------------------- spawn


async def test_a_codex_spawn_returns_once_the_rollout_names_its_thread(tmp_path: Path) -> None:
    """Codex takes no session id, so the spawn reads the thread off the rollout it begins, once
    its consent is taken, and the supervisor reads that file."""
    client = FakeSandboxClient(scripts=[Script(tui=at_its_prompt())])
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec())).run_id
    sandbox = client.only()
    try:
        session = await adapter.session_state(run_id)
        assert (session.session_id, session.log_offset) == (CODEX_THREAD, 0)
        assert state_of(tmp_path, run_id).session_log == LOG
        assert commands(sandbox, SESSION, CONSENT, ROLLOUT_FIND) == [
            SESSION,
            CONSENT,
            ROLLOUT_FIND,
        ]
        await polled(client)
        assert {c.envs["LOG"] for c in client.commands_by_name(SESSION_POLL)} == {LOG}
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        await stop(adapter, run_id)


async def test_a_codex_spawn_whose_rollout_never_appears_fails_with_the_screen_and_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that never begins its turn is held by a dialog the seeds did not answer: the
    spawn names it, hangs the TUI up and kills the sandbox, which no run was recorded for."""
    monkeypatch.setattr(adapter_module, "SESSION_ID_WAIT", 0)
    pane = (
        ">_ OpenAI Codex (v0.159.0)\n\n  Hooks need review\n\n"
        f"{INPUT_MARK} 1. Review hooks\n  2. Trust all and continue\n"
    )
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(consent=True, pane=pane))])
    adapter = adapter_for(tmp_path, client)

    with pytest.raises(SpawnError, match="no session log") as raised:
        await adapter.spawn(make_spec())
    assert raised.value.reason is SpawnReason.INFRASTRUCTURE
    assert "'Hooks need review'" in str(raised.value)
    sandbox = client.only()
    assert [c.envs for c in client.commands_by_name(CODEX_FINISH)] == [{"MODE": "hangup"}]
    assert client.commands_by_name(FINISH) == []
    assert client.killed == [sandbox.sandbox_id]
    assert not list((tmp_path / "state" / "runs").iterdir())


async def test_a_codex_spawn_refuses_a_launch_that_began_two_rollouts(tmp_path: Path) -> None:
    """Which of two is the session's cannot be told, and supervising the other's would hold the
    run to a thread nobody asked for."""
    tui = TuiScript(steps=[codex_turn(meta=True)], consent=True, other_threads=[OTHER_THREAD])
    client = FakeSandboxClient(scripts=[Script(tui=tui)])
    adapter = adapter_for(tmp_path, client)

    with pytest.raises(SpawnError, match="not one") as raised:
        await adapter.spawn(make_spec())
    assert raised.value.reason is SpawnReason.INFRASTRUCTURE
    assert [c.envs for c in client.commands_by_name(CODEX_FINISH)] == [{"MODE": "hangup"}]
    assert client.killed == [client.only().sandbox_id]


async def test_a_codex_spawn_refuses_a_rollout_whose_first_line_names_another_thread(
    tmp_path: Path,
) -> None:
    hooks, log = codex_turn(meta=True)
    log[0] = json.dumps({"type": "session_meta", "payload": {"id": OTHER_THREAD}})
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(steps=[(hooks, log)], consent=True))])
    adapter = adapter_for(tmp_path, client)

    with pytest.raises(SpawnError, match="does not name") as raised:
        await adapter.spawn(make_spec())
    assert raised.value.reason is SpawnReason.INFRASTRUCTURE
    assert client.killed == [client.only().sandbox_id]


# --------------------------------------------------------------------------- signals


async def test_a_codex_session_idles_on_its_stop_hook_and_on_a_failed_turn_from_the_rollout(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    try:
        session = await adapter.session_state(run_id)
        assert (session.idle_reason, session.turns, session.last_input_at) == (
            IdleReason.TURN_END,
            1,
            None,
        )

        # A 401 fires no Stop: only the rollout's task_complete says the turn ended, and how.
        client.only().play(*codex_turn(failed="unexpected status 401 Unauthorized"))
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        session = await adapter.session_state(run_id)
        assert session.idle_reason is IdleReason.TURN_ERROR
        assert session.last_input_at == datetime(2026, 9, 23, 12, tzinfo=UTC)
    finally:
        await stop(adapter, run_id)


async def test_a_hook_line_from_another_thread_does_not_idle_the_session(tmp_path: Path) -> None:
    started, ended = first_turn_halves()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(steps=[started], consent=True))])
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec())).run_id
    sandbox = client.only()
    try:
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.RUNNING

        sandbox.play([codex_hook("Stop", OTHER_THREAD, last_assistant_message="Not this one.")])
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.RUNNING

        sandbox.play(*ended)
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        await stop(adapter, run_id)


# --------------------------------------------------------------------------- the verbs


async def test_codex_finish_quits_and_succeeds_with_this_segments_tokens_and_no_cost(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt())
    await adapter.finish(run_id)

    assert [c.envs for c in client.commands_by_name(CODEX_FINISH)] == [{"MODE": "exit"}]
    assert client.commands_by_name(FINISH) == []
    result = await adapter.result(run_id)
    assert (result.outcome, result.failure_reason) == (RunStatus.SUCCEEDED, None)
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (100, 7, None)
    assert (result.summary, result.session_id) == ("Opened PR #9.", CODEX_THREAD)
    assert result.stream_warnings == ()
    assert state_of(tmp_path, run_id).kept is True


async def test_a_codex_session_whose_only_turn_was_aborted_finishes_with_no_usage_warning(
    tmp_path: Path,
) -> None:
    """Codex logs no usage for an aborted or a failed turn, so a segment of only those owes
    none; usage is never invented for it either."""
    (hooks, log), _ = first_turn_halves()
    aborted = {"type": "turn_aborted", "reason": "interrupted"}
    log = [
        *log,
        json.dumps({"timestamp": "2026-09-23T12:00:01Z", "type": "event_msg", "payload": aborted}),
    ]
    adapter, _, run_id = await idle(tmp_path, TuiScript(steps=[(hooks, log)], consent=True))
    assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_END
    await adapter.finish(run_id)

    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert (result.input_tokens, result.output_tokens) == (None, None)
    assert result.stream_warnings == ()


async def asking(
    tmp_path: Path, *later: TuiScript
) -> tuple[CodexE2bAdapter, FakeSandboxClient, str]:
    """Spawned with the ask tool on, at its prompt, then idle on a `request_user_input` call."""
    scripts = [Script(tui=at_its_prompt()), *(Script(tui=tui) for tui in later)]
    client = FakeSandboxClient(scripts=scripts)
    adapter = adapter_for(tmp_path, client, ask_tool=True)
    run_id = (await adapter.spawn(make_spec())).run_id
    await polled(client)
    client.only().play(*codex_turn(asked=True))
    await polled(client)
    return adapter, client, run_id


async def test_a_codex_question_call_idles_the_sandbox_session_with_the_ask_tool_on(
    tmp_path: Path,
) -> None:
    adapter, client, run_id = await asking(tmp_path)
    try:
        state = await adapter.session_state(run_id)
        assert state.idle_reason is IdleReason.QUESTION
        assert state.question == "Which database? [Postgres | SQLite]"
        assert state.idle_since is not None
        assert state.question_expires_at == state.idle_since + timedelta(seconds=120)
        config = client.only().files[CONFIG_PATH][0].decode()
        assert "default_mode_request_user_input = true" in config
    finally:
        await stop(adapter, run_id)


async def test_finish_on_a_codex_tool_question_hangs_up_the_sandbox(tmp_path: Path) -> None:
    """The picker takes typed text for an answer, so a quit typed there would answer it."""
    adapter, client, run_id = await asking(tmp_path)
    await adapter.finish(run_id)
    assert [c.envs for c in client.commands_by_name(CODEX_FINISH)] == [{"MODE": "hangup"}]


async def test_a_sandbox_resume_clears_a_codex_tool_question(tmp_path: Path) -> None:
    """The park's hang-up took the picker with it: a resumed session still reporting the
    question would be parked again at once for a question nobody can see."""
    adapter, client, run_id = await asking(tmp_path, turnless())
    try:
        await adapter.interrupt(run_id, ParkReason.IDLE)
        await adapter.resume(run_id, None, FRESH)
        await polled(client)
        state = await adapter.session_state(run_id)
        assert state.question_expires_at is None
        assert state.idle_reason is not IdleReason.QUESTION
    finally:
        await stop(adapter, run_id)


async def test_codex_rollover_resumes_the_same_thread_at_its_prompt(tmp_path: Path) -> None:
    """No hook fires on a turnless codex resume, so the consent's view of its prompt idles it.
    Its config is rendered again: the agent may have rewritten it."""
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(), turnless())
    sandbox = client.only()
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    sandbox.files[CONFIG_PATH] = (b'[mcp_servers.x]\ncommand = "sh"\n', "agent")

    await adapter.resume(run_id, None, FRESH)
    argv = argv_of(sandbox)
    assert argv[:2] == [CODEX_NATIVE, "resume"] and argv[-1] == CODEX_THREAD
    assert not any(arg.startswith("Read ") for arg in argv), "no turn: nothing typed"
    assert b"mcp_servers.x" not in sandbox.files[CONFIG_PATH][0]
    assert len(client.commands_by_name(CONSENT)) == 2
    assert len(client.commands_by_name(ROLLOUT_FIND)) == 1, "a resume knows its rollout"
    assert state_of(tmp_path, run_id).session_log == LOG

    await polled(client)
    assert await adapter.status(run_id) is RunStatus.IDLE
    session = await adapter.session_state(run_id)
    assert (session.idle_reason, session.session_id) == (IdleReason.TURN_END, CODEX_THREAD)

    await adapter.finish(run_id)
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert result.stream_warnings == (), "a segment nobody typed into owes no turn"
    assert (result.input_tokens, result.output_tokens) == (100, 7)
    assert result.summary == "Opened PR #9.", "the last Stop's words, from the segment before"
    assert client.commands_by_name(FINISH) == [], "the park and the finish are codex's own"


async def test_a_turnless_codex_resume_idles_where_its_failed_turn_left_it(tmp_path: Path) -> None:
    adapter, client, run_id = await idle(tmp_path, at_its_prompt(), turnless())
    client.only().play(*codex_turn(failed="unexpected status 401 Unauthorized"))
    await polled(client)
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    try:
        await adapter.resume(run_id, None, FRESH)
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
        assert (await adapter.session_state(run_id)).idle_reason is IdleReason.TURN_ERROR
    finally:
        await stop(adapter, run_id)


async def test_a_codex_resume_whose_consent_fails_parks_again_and_names_the_screen(
    tmp_path: Path,
) -> None:
    pane = f"  Sign in with ChatGPT\n\n{INPUT_MARK} 1. Sign in\n  2. Provide your own API key\n"
    adapter, client, run_id = await idle(
        tmp_path, at_its_prompt(), TuiScript(consent=False, pane=pane), turnless()
    )
    sandbox = client.only()
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    paused = len(client.paused)

    with pytest.raises(AdapterUnavailable, match="no consent screen") as raised:
        await adapter.resume(run_id, None, FRESH)
    assert "'Sign in with ChatGPT'" in str(raised.value)
    assert [c.envs for c in client.commands_by_name(CODEX_FINISH)][-1] == {"MODE": "hangup"}
    assert len(client.paused) == paused + 1 and sandbox.state == "paused"
    state = state_of(tmp_path, run_id)
    assert (state.status, state.kept) == (RunStatus.INTERRUPTED, True)

    try:
        await adapter.resume(run_id, None, FRESH)
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        await stop(adapter, run_id)


async def test_a_codex_launch_gone_before_its_consent_names_what_its_own_log_ends_with(
    tmp_path: Path,
) -> None:
    """A TUI that dies at its startup takes its tmux server, and so its screen, with it, and
    codex logs some causes only to its own log: losing that tail costs a live canary."""
    adapter, client, run_id = await idle(
        tmp_path, at_its_prompt(), TuiScript(consent=True, gone_at_consent=True)
    )
    await adapter.interrupt(run_id, ParkReason.ROLLOVER)
    cause = f"ERROR codex_tui: Error finding conversation path: {CODEX_THREAD}"
    log = b"WARN earlier\n" * 1000 + f"{cause}\n".encode()
    client.only().logs[f"{CONFIG_DIR}/log/codex-tui.log"] = log

    with pytest.raises(AdapterUnavailable, match="did not reach its consent screen") as raised:
        await adapter.resume(run_id, None, FRESH)
    assert cause in str(raised.value)


async def test_a_codex_resume_turn_attaches_its_images_with_the_equals_form(
    tmp_path: Path,
) -> None:
    image = RunAttachment(
        name="resume1-image-1",
        media_type="image/png",
        content=PNG,
        source="ticket_comments",
        origin="https://github.com/user-attachments/assets/1b92ad15",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    adapter, client, run_id = await idle(
        tmp_path, at_its_prompt(), TuiScript(steps=[codex_turn()], consent=True)
    )
    sandbox = client.only()
    await adapter.interrupt(run_id)
    try:
        await adapter.resume(run_id, "Match this layout.", FRESH, (image,))

        path = f"{ATTACHMENTS_DIR}/resume1-image-1.png"
        argv = argv_of(sandbox)
        assert f"--image={path}" in argv and path not in argv, "never a bare path to swallow"
        assert argv[-2:] == [CODEX_THREAD, f"Read {turn_file(1)} and do what it says."]
        text = sandbox.files[turn_file(1)][0].decode()
        assert text.startswith("Match this layout.") and path in text
        assert state_of(tmp_path, run_id).segment_turn is True
        await polled(client)
        assert await adapter.status(run_id) is RunStatus.IDLE
    finally:
        await stop(adapter, run_id)


async def test_codex_rework_adopts_the_kept_sandbox_and_reads_from_the_rollouts_end(
    tmp_path: Path,
) -> None:
    rework_turn = codex_turn(summary="Pushed the fix.", tokens=(30, 4))
    adapter, client, first = await idle(
        tmp_path, at_its_prompt(), TuiScript(steps=[rework_turn], consent=True)
    )
    sandbox = client.only()
    await adapter.finish(first)
    log_size = len(sandbox.logs[LOG])

    rework = make_spec(secrets=FRESH, resume_from=ResumeFrom(run_id=first, session_id=CODEX_THREAD))
    run_id = (await adapter.spawn(rework)).run_id
    assert len(client.sandboxes) == 1
    argv = argv_of(sandbox)
    assert argv[:2] == [CODEX_NATIVE, "resume"]
    assert argv[-2:] == [CODEX_THREAD, f"Read {turn_file(1)} and do what it says."]
    state = state_of(tmp_path, run_id)
    assert state.log_offset == state.log_read == log_size > 0
    assert (state.session_id, state.session_log) == (CODEX_THREAD, LOG)
    assert len(client.commands_by_name(ROLLOUT_FIND)) == 1, "only the spawn looks for one"

    await polled(client)
    assert await adapter.status(run_id) is RunStatus.IDLE
    await adapter.finish(run_id)
    result = await adapter.result(run_id)
    assert result.outcome is RunStatus.SUCCEEDED
    assert (result.input_tokens, result.output_tokens) == (30, 4)
    assert result.summary == "Pushed the fix."
    assert not [line for line in mirror(tmp_path, run_id) if "Opened PR #9." in line]
    assert result.stream_warnings == ()


async def test_a_restarted_worker_re_adopts_a_codex_session_from_its_recorded_log_path(
    tmp_path: Path,
) -> None:
    """Codex's log path is known only from the state, and a turn straddling the restart is
    mirrored once."""
    clock = Clock()
    started, ended = first_turn_halves()
    client = FakeSandboxClient(scripts=[Script(tui=TuiScript(steps=[started], consent=True))])
    first = adapter_for(tmp_path, client, clock)
    run_id = (await first.spawn(make_spec())).run_id
    await polled(client)
    for task in first._supervisors.values():
        task.cancel()
    before = mirror(tmp_path, run_id)
    assert before, "the turn's start is mirrored before the restart"

    clock.now += timedelta(minutes=10)
    second = adapter_for(tmp_path, client, clock)
    assert await second.status(run_id) is RunStatus.RUNNING
    polls = len(client.commands_by_name(SESSION_POLL))
    try:
        client.only().play(*ended)
        await polled(client)

        after = mirror(tmp_path, run_id)
        assert after[: len(before)] == before
        assert len(after) == len(set(after)), "no line is mirrored twice"
        assert [json.loads(line)["type"] for line in after].count("thread.started") == 1
        assert {c.envs["LOG"] for c in client.commands_by_name(SESSION_POLL)[polls:]} == {LOG}
        assert await second.status(run_id) is RunStatus.IDLE
        assert (await second.session_state(run_id)).turns == 1
    finally:
        await stop(second, run_id)


# --------------------------------------------------------------------------- session views

VIEW_CREDENTIALS = {"GH_TOKEN": SecretStr(TOKEN), "OPENAI_API_KEY": SecretStr(API_KEY)}
DOCS = McpServerSpec(name="docs", transport=McpTransport.HTTP, url="https://mcp.example.com/d")
SIGN_IN = f"  Sign in with ChatGPT\n\n{INPUT_MARK} 1. Sign in\n  2. Provide your own API key\n"


async def finished(tmp_path: Path, view: TuiScript) -> tuple[CodexE2bAdapter, FakeSandbox, str]:
    """A codex session that finished, its sandbox parked; `view` is its view's CLI."""
    client = FakeSandboxClient(scripts=[Script(tui=at_its_prompt())], view=view)
    adapter = adapter_for(tmp_path, client)
    run_id = (await adapter.spawn(make_spec(mcp=(DOCS,)))).run_id
    await polled(client)
    await adapter.finish(run_id)
    assert state_of(tmp_path, run_id).kept is True
    return adapter, client.only(), run_id


async def test_a_codex_view_takes_its_folder_consent_with_no_mcp_server_and_no_signal(
    tmp_path: Path,
) -> None:
    """Codex asks for the folder at every launch, a view's too, and nobody can type the Enter
    into a read-only view: the adapter takes it in the view's own tmux session. The config it
    renders has no MCP server, and its hooks append nowhere the run reads."""
    adapter, sandbox, run_id = await finished(tmp_path, TuiScript(consent=True))
    assert "mcp_servers" in sandbox.files[CONFIG_PATH][0].decode(), "the session's own has it"

    opened = await adapter.inspect_start(
        run_id, token="r1", limit=timedelta(minutes=55), credentials=VIEW_CREDENTIALS
    )

    assert opened.phase is InspectPhase.OPEN
    consents = [c for c in sandbox.commands if c.command == CONSENT]
    assert consents[-1].envs == {"TMUX_TARGET": "inspect"}
    config = sandbox.files[CONFIG_PATH][0].decode()
    assert "mcp_servers" not in config
    assert SIGNALS not in config and f"FACTORIAL_SIGNALS={VIEW_SIGNALS}" in config
    assert sandbox.network.inject == {"api.openai.com": {"Authorization": f"Bearer {API_KEY}"}}
    argv = sandbox.files[VIEW_ARGV][0].decode().split("\0")[:-1]
    assert argv[:2] == [CODEX_NATIVE, "resume"] and argv[-1] == CODEX_THREAD
    env = sandbox.files[VIEW_ENV][0].decode().split("\0")[:-1]
    assert not [line for line in env if line.startswith(("GH_", "GITHUB"))]

    await adapter.inspect_stop(run_id)
    (stop,) = [c for c in sandbox.commands if c.command.startswith(INSPECT_STOP)]
    assert f"'{AUTH_PATH}'" in stop.command, "the login the view wrote goes with it"


async def test_a_codex_view_whose_login_is_refused_says_to_store_a_fresh_one(
    tmp_path: Path,
) -> None:
    adapter, sandbox, run_id = await finished(tmp_path, TuiScript(pane=SIGN_IN))

    with pytest.raises(InspectLoginRefused, match="store a fresh one"):
        await adapter.inspect_start(
            run_id, token="r1", limit=timedelta(minutes=55), credentials=VIEW_CREDENTIALS
        )

    assert [c.envs for c in sandbox.commands if c.command == CODEX_FINISH][-1] == {"MODE": "hangup"}
    assert sandbox.state == "paused" and not sandbox.view_open
    assert (await adapter.inspect_status(run_id)).phase is InspectPhase.FAILED


# --------------------------------------------------------------------------- the session smoke


async def smoke(tmp_path: Path, *scripts: TuiScript) -> tuple[RunResult, list[str]]:
    """The builder's session smoke of a codex build, whose sessions play `scripts`: the
    spawn's, then the rollover's."""
    client = FakeSandboxClient(scripts=[Script(tui=tui) for tui in scripts])
    run = session_smoke_run(
        adapter_for(tmp_path, client),
        smoke_spec("codex", API_KEY),
        poll=0,
        reattach=lambda: adapter_for(tmp_path, client),
        hold=timedelta(0),
    )
    return await asyncio.wait_for(run, 10)


@pytest.mark.parametrize(
    ("pane", "shown"),
    [
        # The consent screen is every launch's, and taken: never the seeds' fault.
        (CONSENT_SCREEN, None),
        (
            "  Hooks need review\n  1 hook is new or changed\n\n"
            f"{INPUT_MARK} 1. Review hooks\n  2. Trust all and continue\n",
            "Hooks need review",
        ),
        (
            "  Sign in with ChatGPT to use Codex as part of your paid plan\n\n"
            f"{INPUT_MARK} 1. Sign in with ChatGPT\n  2. Provide your own API key\n",
            "Sign in with ChatGPT",
        ),
    ],
)
async def test_the_smoke_reports_a_codex_dialog_on_screen(
    tmp_path: Path, pane: str, shown: str | None
) -> None:
    first = TuiScript(steps=[codex_turn(meta=True)], consent=True, pane=pane)

    _, problems = await smoke(tmp_path, first, turnless())

    if shown is None:
        assert problems == []
    else:
        assert len(problems) == 1 and "first-run dialog" in problems[0]
        assert shown in problems[0]


async def test_a_codex_smoke_requires_tokens_not_cost(tmp_path: Path) -> None:
    """Codex reports no cost (decision 34): a session that reports its tokens passes without one,
    and one whose turns logged no usage fails for that, whatever else it reports."""
    result, problems = await smoke(tmp_path, at_its_prompt(), turnless())

    assert problems == []
    assert (result.cost_usd, result.input_tokens, result.output_tokens) == (None, 100, 7)

    hooks, log = codex_turn(meta=True)
    unmetered = [line for line in log if "token_usage_record" not in line]
    first = TuiScript(steps=[(hooks, unmetered)], consent=True)
    _, problems = await smoke(tmp_path / "unmetered", first, turnless())

    assert "the session reported no tokens" in problems
