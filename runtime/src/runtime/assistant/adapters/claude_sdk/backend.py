"""`ClaudeSdkBackend`: one `ClaudeSDKClient`, so one bundled `claude` child, per chat turn
(tech-stack.md decision 63).

A turn runs in `<work_root>/<conversation id>`, emptied first: the SDK keys a stored session by
its cwd, so the path must stay the same across a conversation's turns, while nothing a turn
leaves there may reach the next. `CLAUDE_CONFIG_DIR` is a fresh temp dir per turn, holding the
CLI's local transcript. It is deleted when the turn ends, however it ends: the client's
disconnect, which flushes the transcript to the store, can raise or be cancelled, and the
deletion does not wait on it succeeding.

The turn's tools run in this process, through the SDK's in-process server
(`options.tool_server`): a tool call crosses only the child's stdio.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, cast

import claude_agent_sdk
from claude_agent_sdk import ClaudeSDKClient, ClaudeSDKError, Message, ResultMessage

from runtime.assistant.adapters.claude_sdk.events import EventMapper, ToolsLeaked
from runtime.assistant.adapters.claude_sdk.options import SERVER, build_options
from runtime.assistant.adapters.claude_sdk.session_store import StoreBridge
from runtime.assistant.contract import (
    BackendStateStore,
    ChatEvent,
    ChatToolbox,
    ChatTurnSpec,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnStop,
)
from runtime.contract import AdapterUnavailable

__all__ = ["LAUNCHER", "PINNED_SDK_VERSION", "ClaudeSdkBackend", "bundled_cli", "launcher_path"]

log = logging.getLogger(__name__)

#: The SDK release this adapter was verified against. It bundles `claude` 2.1.310, the version
#: the event mapping was observed on (`events.py`).
PINNED_SDK_VERSION: Final = "0.2.174"
#: The console script `launcher.py` installs.
LAUNCHER: Final = "factorial-assistant-cli"


def bundled_cli() -> Path:
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def launcher_path() -> Path:
    return Path(sys.executable).parent / LAUNCHER


@dataclass
class _Live:
    """One turn, across its attempts: `cancel` reaches whichever client is current."""

    client: ClaudeSDKClient | None = None
    connected: bool = False
    cancelled: bool = False


class _StartFailed(Exception):
    """The CLI failed before its init message, so the caller has seen no event of the attempt."""


class ClaudeSdkBackend:
    kind: ClassVar[str] = "claude_sdk"
    pinned_version: ClassVar[str] = PINNED_SDK_VERSION
    supports_partial_text: ClassVar[bool] = True
    supports_resume: ClassVar[bool] = True
    supports_cancel: ClassVar[bool] = True
    supports_effort: ClassVar[bool] = True

    def __init__(self, work_root: Path) -> None:
        self._work_root = Path(work_root)
        self._live: dict[str, _Live] = {}

    async def preflight(self) -> None:
        installed = claude_agent_sdk.__version__
        if installed != PINNED_SDK_VERSION:
            raise AdapterUnavailable(
                f"claude-agent-sdk {installed} is installed; this adapter is verified against "
                f"{PINNED_SDK_VERSION}"
            )
        for what, path in (("the bundled claude CLI", bundled_cli()), (LAUNCHER, launcher_path())):
            if not os.access(path, os.X_OK):
                raise AdapterUnavailable(f"{what} is missing or not executable at {path}")

    async def turn(
        self, spec: ChatTurnSpec, state: BackendStateStore, tools: ChatToolbox
    ) -> AsyncIterator[ChatEvent]:
        """One turn. When the session `spec.resume_session` names cannot be resumed — the store
        does not hold it, or `work_root` moved and so did the SDK's key for it — the CLI fails
        before its init message, and the turn runs once more as a new session, opened with
        `spec.history`. The new session's id reaches the caller in `Usage`, as always."""
        cwd = self._work_root / spec.conversation_id
        if cwd.resolve().parent != self._work_root.resolve():
            yield TurnFailed(
                reason=TurnFailure.CONFIG,
                message=f"conversation id {spec.conversation_id!r} is not one path component",
            )
            return
        shutil.rmtree(cwd, ignore_errors=True)
        cwd.mkdir(parents=True)
        config_dir = Path(tempfile.mkdtemp(prefix="factorial-assistant-"))
        live = self._live[spec.turn_id] = _Live()
        try:
            attempt = spec
            while True:
                try:
                    run = self._attempt(live, attempt, tools, cwd, config_dir, state)
                    async with aclosing(run) as events:
                        async for event in events:
                            yield event
                    return
                except _StartFailed as failed:
                    if attempt.resume_session is None or live.cancelled:
                        yield TurnFailed(reason=TurnFailure.INFRASTRUCTURE, message=str(failed))
                        return
                    log.warning(
                        "assistant turn %s: session %s did not resume (%s); starting a new one",
                        spec.turn_id,
                        attempt.resume_session,
                        failed,
                    )
                    attempt = _fresh_start(attempt)
        finally:
            self._live.pop(spec.turn_id, None)
            shutil.rmtree(config_dir, ignore_errors=True)

    async def _attempt(
        self,
        live: _Live,
        spec: ChatTurnSpec,
        tools: ChatToolbox,
        cwd: Path,
        config_dir: Path,
        state: BackendStateStore,
    ) -> AsyncGenerator[ChatEvent]:
        options = build_options(
            spec,
            tools,
            cwd=cwd,
            config_dir=config_dir,
            store=StoreBridge(state),
            cli_path=launcher_path(),
            real_cli=bundled_cli(),
        )
        client = live.client = ClaudeSDKClient(options)
        mapper = EventMapper([SERVER])
        try:
            await client.connect()
            live.connected = True
            if live.cancelled:
                yield TurnDone(stop=TurnStop.CANCELLED)
                return
            await client.query(spec.message)
            # An async generator at runtime, typed only as an iterator.
            replies = cast(AsyncGenerator[Message], client.receive_response())
            async with aclosing(replies) as messages:
                async for message in messages:
                    try:
                        events = mapper.map(message)
                    except ToolsLeaked as exc:
                        await _interrupt(client)
                        log.error("assistant turn %s: the CLI loaded %s", spec.turn_id, exc)
                        yield TurnFailed(
                            reason=TurnFailure.CONFIG,
                            message=f"the CLI loaded tools outside the turn's own: {exc}",
                        )
                        return
                    for event in events:
                        yield event
                        if isinstance(event, TurnFailed) and not isinstance(message, ResultMessage):
                            await _interrupt(client)
                            return
        except ClaudeSDKError as exc:
            if not mapper.checked:
                raise _StartFailed(str(exc)) from exc
            yield TurnFailed(reason=TurnFailure.INFRASTRUCTURE, message=str(exc))
        finally:
            live.connected = False
            await client.disconnect()

    async def cancel(self, turn_id: str) -> None:
        live = self._live.get(turn_id)
        if live is None:
            return
        live.cancelled = True
        if live.connected and live.client is not None:
            await _interrupt(live.client)


def _fresh_start(spec: ChatTurnSpec) -> ChatTurnSpec:
    """The turn as a new session, its opening message carrying what the lost one held."""
    message = spec.message
    if spec.history:
        lines = "\n".join(f"{item.role}: {item.text}" for item in spec.history)
        message = f"Earlier in this conversation:\n{lines}\n\n{spec.message}"
    return spec.model_copy(update={"resume_session": None, "message": message})


async def _interrupt(client: ClaudeSDKClient) -> None:
    """Best effort: the disconnect that always follows ends the child whatever this does."""
    with suppress(ClaudeSDKError):
        await client.interrupt()
