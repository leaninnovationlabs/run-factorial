"""`CodexCliBackend`: one `codex exec` child per chat turn (tech-stack decision 63).

Mirrors agent `codex_bedrock` / `codex_api` containment: per-turn `CODEX_HOME`, workspace
sandbox, web search off on Bedrock, and a single MCP server (`factorial`) bridged to the
turn's in-process `ChatToolbox`. On Bedrock the child's model requests go through the turn's
relay (`relay.py`), without which Codex runs no call of that server's tools.

A turn keeps no session. Codex writes a thread's rollout under `CODEX_HOME`, which is deleted
when the turn ends, so `codex exec resume` has nothing to continue and exits 1. Every turn is a
new thread that opens with the conversation's transcript (`ChatTurnSpec.history`).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import ClassVar, Final

import httpx

from runtime.adapters._cli.paths import write_private
from runtime.adapters._cli.process import Cli
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_BEDROCK,
    AUTH_MODE_SUBSCRIPTION,
    bedrock_provider_config,
)
from runtime.adapters.codex_cli.cli import (
    AUTH_FILE,
    CODEX_BIN,
    CONFIG_FILE,
    FLAG_CD,
    FLAG_MODEL,
    FLAG_SANDBOX,
    PINNED_VERSION,
    VALUE_SANDBOX_WORKSPACE,
    VERSION_NAME,
)
from runtime.adapters.codex_cli.events import parse_event
from runtime.adapters.codex_cli.tui import auth_document
from runtime.assistant.adapters.codex_cli.config import build_assistant_config
from runtime.assistant.adapters.codex_cli.env import build_child_env
from runtime.assistant.adapters.codex_cli.events import CommandLeaked, EventMapper
from runtime.assistant.adapters.codex_cli.exec_cli import (
    FLAG_IMAGE,
    FLAG_JSON,
    FLAG_SKIP_GIT_REPO_CHECK,
    STDIN_PROMPT,
    SUBCMD_EXEC,
    ExecChild,
    api_key_login,
    launch,
)
from runtime.assistant.adapters.codex_cli.relay import ResponsesRelay
from runtime.assistant.adapters.codex_cli.tool_socket import ToolSocketServer, socket_path_for_turn
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

__all__ = ["PINNED_CODEX_VERSION", "CodexCliBackend"]

log = logging.getLogger(__name__)

PINNED_CODEX_VERSION: Final = PINNED_VERSION


class _Live:
    child: ExecChild | None = None
    cancelled: bool = False


class CodexCliBackend:
    kind: ClassVar[str] = "codex_cli"
    pinned_version: ClassVar[str] = PINNED_CODEX_VERSION
    supports_partial_text: ClassVar[bool] = False
    supports_resume: ClassVar[bool] = False
    supports_cancel: ClassVar[bool] = True
    supports_effort: ClassVar[bool] = False
    supports_images: ClassVar[bool] = True

    def __init__(
        self,
        work_root: Path,
        *,
        mcp_command: tuple[str, ...],
        codex_bin: str = CODEX_BIN,
        bedrock_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._work_root = Path(work_root)
        self._mcp_command = mcp_command
        self._bedrock_transport = bedrock_transport
        self._cli = Cli(agent_bin=codex_bin)
        self._live: dict[str, _Live] = {}

    async def preflight(self) -> None:
        if shutil.which(self._cli.agent_bin) is None:
            raise AdapterUnavailable(f"`{self._cli.agent_bin}` is not on PATH")
        result = await self._cli.run(
            self._cli.agent_bin, "--version", inherit_host_env=False, env={"PATH": _path_only()}
        )
        if not result.ok:
            raise AdapterUnavailable(
                f"`{self._cli.agent_bin} --version` exited {result.returncode}: "
                f"{result.stderr[:200]}"
            )
        version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if PINNED_CODEX_VERSION not in version_line:
            raise AdapterUnavailable(
                f"{VERSION_NAME} {version_line!r} is installed; this adapter is verified against "
                f"{PINNED_CODEX_VERSION}"
            )

    async def turn(
        self, spec: ChatTurnSpec, state: BackendStateStore, tools: ChatToolbox
    ) -> AsyncIterator[ChatEvent]:
        del state
        cwd = self._work_root / spec.conversation_id
        if cwd.resolve().parent != self._work_root.resolve():
            yield TurnFailed(
                reason=TurnFailure.CONFIG,
                message=f"conversation id {spec.conversation_id!r} is not one path component",
            )
            return
        shutil.rmtree(cwd, ignore_errors=True)
        cwd.mkdir(parents=True)
        config_dir = Path(tempfile.mkdtemp(prefix="factorial-assistant-codex-"))
        socket_path = socket_path_for_turn(spec.turn_id)
        tool_host = ToolSocketServer(socket_path, tools)
        relay = (
            ResponsesRelay(
                bedrock_provider_config(spec.bedrock_region)["base_url"],
                transport=self._bedrock_transport,
            )
            if spec.auth_mode == AUTH_MODE_BEDROCK
            else None
        )
        live = self._live[spec.turn_id] = _Live()
        try:
            await tool_host.start()
            if relay is not None:
                await relay.start()
            await _seed_auth(self._cli.agent_bin, config_dir, spec)
            document, _ = build_assistant_config(
                worktree=cwd,
                writable_root=cwd,
                mcp_command=self._mcp_command,
                tool_socket=socket_path,
                auth_mode=spec.auth_mode,
                bedrock_region=spec.bedrock_region,
                bedrock_base_url=None if relay is None else relay.base_url,
            )
            write_private(config_dir / CONFIG_FILE, document)
            prompt = _prompt(spec)
            # Beside the turn's config, which goes when the turn does: not in the worktree, where
            # the bytes would wait for the conversation's next turn to be cleared.
            argv = _argv(spec, cwd, write_images(spec, config_dir))
            env = build_child_env(
                auth_mode=spec.auth_mode,
                credential=spec.credential.get_secret_value(),
                config_dir=config_dir,
                bedrock_region=spec.bedrock_region,
            )
            child = await launch(self._cli.agent_bin, argv, cwd=cwd, env=env, stdin_text=prompt)
            live.child = child
            mapper = EventMapper(max_steps=spec.limits.max_steps)
            ended = False
            async for line in child.stdout_lines():
                if live.cancelled:
                    yield TurnDone(stop=TurnStop.CANCELLED)
                    return
                parsed = parse_event(line)
                if parsed is None:
                    continue
                try:
                    for event in mapper.map(parsed):
                        yield event
                        if isinstance(event, (TurnDone, TurnFailed)):
                            ended = True
                            if live.child is not None:
                                live.child.terminate()
                            return
                except CommandLeaked as exc:
                    yield TurnFailed(
                        reason=TurnFailure.CONFIG,
                        message=f"the CLI ran a shell command outside MCP tools: {exc}",
                    )
                    return
            code = await child.wait()
            if not ended:
                stderr = await child.stderr_text()
                yield TurnFailed(
                    reason=TurnFailure.INFRASTRUCTURE,
                    message=f"codex exited {code}: {stderr.strip()[:400]}",
                )
        finally:
            self._live.pop(spec.turn_id, None)
            if relay is not None:
                await relay.stop()
            await tool_host.stop()
            shutil.rmtree(config_dir, ignore_errors=True)

    async def cancel(self, turn_id: str) -> None:
        live = self._live.get(turn_id)
        if live is None:
            return
        live.cancelled = True
        if live.child is not None:
            live.child.terminate()


def _path_only() -> str:
    import os

    return os.environ.get("PATH", "")


def _prompt(spec: ChatTurnSpec) -> str:
    parts = [spec.system_prompt.strip(), ""]
    if spec.history:
        lines = "\n".join(f"{item.role}: {item.text}" for item in spec.history)
        parts.extend([f"Earlier in this conversation:\n{lines}", ""])
    parts.append(spec.message)
    return "\n".join(parts)


_SUFFIX: Final = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def write_images(spec: ChatTurnSpec, directory: Path) -> list[Path]:
    """Each of the turn's images as a file only this user can read, in the order attached."""
    written: list[Path] = []
    for image in spec.images:
        path = directory / f"{image.name}{_SUFFIX[image.media_type]}"
        path.touch(mode=0o600)
        path.write_bytes(image.content)
        written.append(path)
    return written


def _argv(spec: ChatTurnSpec, cwd: Path, images: Sequence[Path] = ()) -> tuple[str, ...]:
    return (
        SUBCMD_EXEC,
        FLAG_JSON,
        FLAG_MODEL,
        spec.model,
        FLAG_CD,
        str(cwd),
        FLAG_SANDBOX,
        VALUE_SANDBOX_WORKSPACE,
        FLAG_SKIP_GIT_REPO_CHECK,
        *(f"{FLAG_IMAGE}={path}" for path in images),
        STDIN_PROMPT,
    )


async def _seed_auth(codex_bin: str, config_dir: Path, spec: ChatTurnSpec) -> None:
    if spec.auth_mode == AUTH_MODE_SUBSCRIPTION:
        write_private(
            config_dir / AUTH_FILE,
            auth_document(AUTH_MODE_SUBSCRIPTION, spec.credential.get_secret_value()),
        )
    elif spec.auth_mode == AUTH_MODE_API_KEY:
        await api_key_login(codex_bin, config_dir, spec.credential.get_secret_value())
    elif spec.auth_mode == AUTH_MODE_BEDROCK:
        return
    else:
        raise AdapterUnavailable(f"unsupported auth_mode {spec.auth_mode!r}")
