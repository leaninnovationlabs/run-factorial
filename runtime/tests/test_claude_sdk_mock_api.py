"""Real turns: the bundled `claude`, launched through `factorial-assistant-cli`, against the mock
Anthropic API (`mock_anthropic.py`), with an in-process toolbox (`fake_toolbox.py`) and no key."""

import asyncio
import dataclasses
import json
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeSDKClient
from claude_tui_support import Mock
from claude_tui_support import mock_api as start_mock_api
from fake_toolbox import FakeToolbox
from pydantic import SecretStr
from test_assistant_contract import _spec

from runtime.assistant.adapters import create_backend
from runtime.assistant.adapters.claude_sdk import backend as backend_module
from runtime.assistant.adapters.claude_sdk.backend import bundled_cli
from runtime.assistant.contract import (
    HistoryItem,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
    Usage,
)

pytestmark = pytest.mark.skipif(not bundled_cli().exists(), reason="no bundled claude CLI")

ECHO = 'TOOLME mcp__factorial__factorial_echo {"text": "hi"} EMLOOT'
CONFIG_PREFIX = "factorial-assistant-"


class MemoryState:
    def __init__(self) -> None:
        self.entries: dict[str, list[Mapping[str, Any]]] = {}

    async def append(self, key: str, entries: Sequence[Mapping[str, Any]]) -> None:
        self.entries.setdefault(key, []).extend(entries)

    async def load(self, key: str) -> list[Mapping[str, Any]] | None:
        return self.entries.get(key)


@pytest.fixture(scope="module")
def mock_api() -> Iterator[Mock]:
    with start_mock_api() as api:
        yield api


@pytest.fixture
def api_env(mock_api: Mock) -> Iterator[None]:
    with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": mock_api.url}):
        yield


class ConnectLog(BaseHTTPRequestHandler):
    """An HTTPS proxy that refuses every tunnel and records where each one was going."""

    targets: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_CONNECT(self) -> None:
        self.targets.append(self.path)
        self.send_response(403)
        self.end_headers()


@pytest.fixture
def connect_log() -> Iterator[list[str]]:
    ConnectLog.targets = []
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ConnectLog)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        with patch.dict(
            os.environ, {"HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "127.0.0.1,localhost"}
        ):
            yield ConnectLog.targets
    finally:
        proxy.shutdown()
        proxy.server_close()


@pytest.mark.usefixtures("api_env")
@pytest.mark.parametrize("conversation", ["../outside", "a/b", ".", ""])
async def test_a_conversation_id_that_is_not_one_path_component_is_refused(tmp_path, conversation):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("x")
    backend = create_backend("claude_sdk", work_root=root)
    spec = _spec(conversation_id=conversation)
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    assert len(events) == 1 and isinstance(events[0], TurnFailed)
    assert events[0].reason is TurnFailure.CONFIG
    assert (outside / "kept").exists()


@pytest.mark.usefixtures("api_env")
async def test_one_turn_calls_its_tool_in_process_and_ends(tmp_path, monkeypatch, mock_api):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    backend = create_backend("claude_sdk", work_root=tmp_path)
    await backend.preflight()
    toolbox = FakeToolbox()
    events = [e async for e in backend.turn(_spec(message=ECHO), MemoryState(), toolbox)]
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds and kinds[-1] == "turn_done"
    assert next(e for e in events if e.kind == "tool_call").name == "factorial_echo"
    assert next(e for e in events if e.kind == "tool_result").structured == {"text": "hi"}
    assert toolbox.calls == [("factorial_echo", {"text": "hi"})]
    assert {r["x-api-key"] for r in mock_api.requests()} == {"sk-test"}
    assert not list(tmp_path.joinpath("3").iterdir()), "the turn left files in its cwd"
    assert not list(tmp_path.glob(f"{CONFIG_PREFIX}*")), "the turn left its config dir"


@pytest.mark.usefixtures("api_env")
@pytest.mark.parametrize("auth_mode", ["api_key", "subscription"])
async def test_a_turn_reaches_no_host_but_the_api(tmp_path, connect_log, auth_mode):
    """The CLI sends telemetry to api.anthropic.com whatever ANTHROPIC_BASE_URL says, through
    a proxy when one is set; the proxy here sees every such attempt."""
    backend = create_backend("claude_sdk", work_root=tmp_path)
    spec = _spec(turn_id="16", message=ECHO, auth_mode=auth_mode)
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    assert events[-1] == TurnDone(stop=TurnStop.END)
    assert connect_log == []


@pytest.mark.usefixtures("api_env")
async def test_the_config_dir_goes_even_when_the_disconnect_fails(tmp_path, monkeypatch):
    """The disconnect flushes the transcript to the store, which can fail or be cancelled; the
    dir holding the CLI's transcript must not outlive the turn either way."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real_disconnect = ClaudeSDKClient.disconnect

    async def failing_disconnect(self: ClaudeSDKClient) -> None:
        await real_disconnect(self)
        raise RuntimeError("the store is down")

    monkeypatch.setattr(ClaudeSDKClient, "disconnect", failing_disconnect)
    backend = create_backend("claude_sdk", work_root=tmp_path / "work")
    with pytest.raises(RuntimeError, match="the store is down"):
        async for _ in backend.turn(_spec(turn_id="12"), MemoryState(), FakeToolbox()):
            pass
    assert not list(tmp_path.glob(f"{CONFIG_PREFIX}*"))


@pytest.mark.usefixtures("api_env")
async def test_a_second_turn_resumes_from_the_store_alone(tmp_path, mock_api):
    backend = create_backend("claude_sdk", work_root=tmp_path)
    state = MemoryState()
    first = [e async for e in backend.turn(_spec(message="REMEMBERME"), state, FakeToolbox())]
    usage = next(e for e in first if isinstance(e, Usage))
    assert state.entries, "the session was not mirrored to the store"
    assert usage.session_id is not None
    resumed = _spec(turn_id="8", message="and again", resume_session=usage.session_id)
    second = [e async for e in backend.turn(resumed, state, FakeToolbox())]
    assert second[-1] == TurnDone(stop=TurnStop.END)
    last = mock_api.messages()[-1]["body"]["messages"]
    assert "REMEMBERME" in json.dumps(last) and "and again" in json.dumps(last)


@pytest.mark.usefixtures("api_env")
async def test_cancel_ends_a_live_turn_cancelled(tmp_path, mock_api):
    backend = create_backend("claude_sdk", work_root=tmp_path)
    spec = _spec(turn_id="9", message="SLOWME cancel me")

    async def stop_once_the_model_is_called() -> None:
        for _ in range(600):
            if any("cancel me" in json.dumps(r["body"]) for r in mock_api.messages()):
                break
            await asyncio.sleep(0.1)
        await backend.cancel("9")

    async with asyncio.timeout(60):
        stopper = asyncio.create_task(stop_once_the_model_is_called())
        events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
        await stopper
    assert events[-1] == TurnDone(stop=TurnStop.CANCELLED)


@pytest.mark.usefixtures("api_env")
async def test_a_cancel_while_connecting_ends_the_turn_before_the_model_runs(
    tmp_path, monkeypatch, mock_api
):
    backend = create_backend("claude_sdk", work_root=tmp_path)
    connecting, release = asyncio.Event(), asyncio.Event()
    real_connect = ClaudeSDKClient.connect

    async def held_connect(self: ClaudeSDKClient, *args: Any, **kwargs: Any) -> None:
        connecting.set()
        await release.wait()
        await real_connect(self, *args, **kwargs)

    async def cancel_while_connecting() -> None:
        await connecting.wait()
        await backend.cancel("11")
        release.set()

    monkeypatch.setattr(ClaudeSDKClient, "connect", held_connect)
    canceller = asyncio.create_task(cancel_while_connecting())
    spec = _spec(turn_id="11", message="never sent")
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    await canceller
    assert events == [TurnDone(stop=TurnStop.CANCELLED)]
    assert not any("never sent" in json.dumps(r["body"]) for r in mock_api.requests())


@pytest.mark.usefixtures("api_env")
async def test_a_session_the_store_lacks_starts_fresh_with_the_history(tmp_path, mock_api):
    lost = "5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a"
    history = (
        HistoryItem(role="user", text="Which tickets are blocked?"),
        HistoryItem(role="assistant", text="Ticket 12 waits on review."),
    )
    spec = _spec(turn_id="13", message="LOSTSESSION go on", resume_session=lost, history=history)
    backend = create_backend("claude_sdk", work_root=tmp_path)
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    assert events[-1] == TurnDone(stop=TurnStop.END)
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.session_id not in (None, lost)
    sent = next(r for r in mock_api.messages() if "LOSTSESSION" in json.dumps(r["body"]))
    text = json.dumps(sent["body"]["messages"])
    assert "Earlier in this conversation:" in text
    assert "user: Which tickets are blocked?" in text
    assert "assistant: Ticket 12 waits on review." in text


@pytest.mark.parametrize(
    ("resume", "spawns"), [(None, 1), ("5b0c3a3e-8f0e-4a8e-9a43-0d1f4f3b6c2a", 2)]
)
async def test_a_startup_failure_is_retried_only_to_drop_a_lost_session(
    tmp_path, monkeypatch, resume, spawns
):
    log = tmp_path / "spawns"
    broken = tmp_path / "claude"
    broken.write_text(f"#!/bin/sh\necho spawned >> {log}\necho 'no good' >&2\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setattr(backend_module, "bundled_cli", lambda: broken)
    backend = create_backend("claude_sdk", work_root=tmp_path / "work")
    spec = _spec(turn_id="14", resume_session=resume)
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    assert len(events) == 1 and isinstance(events[0], TurnFailed)
    assert events[0].reason is TurnFailure.INFRASTRUCTURE
    assert log.read_text().splitlines() == ["spawned"] * spawns


@pytest.mark.usefixtures("api_env")
async def test_a_subscription_turn_sends_the_token_and_never_a_key(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-key")
    spec = _spec(
        turn_id="15",
        message="SUBSCRIBED hello",
        auth_mode="subscription",
        credential=SecretStr("sk-ant-oat01-test"),
    )
    backend = create_backend("claude_sdk", work_root=tmp_path)
    events = [e async for e in backend.turn(spec, MemoryState(), FakeToolbox())]
    # A turn can only end this way past the init check: the CLI loaded the turn's tools alone.
    assert events[-1] == TurnDone(stop=TurnStop.END)
    sent = [r for r in mock_api.requests() if "SUBSCRIBED" in json.dumps(r["body"])]
    assert sent and all(r["authorization"] == "Bearer sk-ant-oat01-test" for r in sent)
    assert all(r["x-api-key"] is None for r in sent)
    offered = [t["name"] for r in sent for t in r["body"].get("tools") or []]
    assert offered == ["mcp__factorial__factorial_echo"]
    assert "sk-ant-host-key" not in json.dumps(mock_api.requests())


@pytest.mark.usefixtures("api_env")
async def test_a_builtin_tool_in_the_child_fails_the_turn_with_config(tmp_path, monkeypatch):
    real = backend_module.build_options
    monkeypatch.setattr(
        backend_module,
        "build_options",
        lambda *a, **k: dataclasses.replace(real(*a, **k), tools=["Read"]),
    )
    backend = create_backend("claude_sdk", work_root=tmp_path)
    events = [e async for e in backend.turn(_spec(turn_id="10"), MemoryState(), FakeToolbox())]
    assert len(events) == 1 and isinstance(events[0], TurnFailed)
    assert events[0].reason is TurnFailure.CONFIG and "Read" in events[0].message
