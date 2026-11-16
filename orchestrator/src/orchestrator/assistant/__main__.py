"""`python -m orchestrator.assistant` / `factorial-assistant` — the assistant's process.

The fourth entrypoint (tech-stack decision 3): a chat turn holds a `claude` child for minutes, so
it runs apart from the API, which a stuck child must not degrade. The local image runs it as its
own OS user, `factorial-assistant`, which cannot read `/app/.env` (design.md §7.18).

It holds no master key: the API hands it each turn's credential, and its chat tools read GitHub
on entries the API hands it too, so every stored secret is opaque to it. In the image a master
key stops it at start (`drop_master_key`). `make dev` runs it as the developer, from the `.env`
the API reads, so there it blanks the key for itself instead; a laptop has no OS-user boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from datetime import UTC, datetime
from functools import cache

from orchestrator.assistant.credential import api_credential
from orchestrator.assistant.github_token import (
    OrganizationTokens,
    ReadTokenCache,
    api_fetch,
    tool_deps,
)
from orchestrator.assistant.loop import serve
from orchestrator.assistant.runner import RunnerDeps
from orchestrator.db.session import dispose_engine, session_scope
from orchestrator.logs import configure_logging
from orchestrator.mcp_server.context import McpDeps
from orchestrator.secrets.crypto import load_keyring
from orchestrator.settings import ENV_PREFIX, Settings, get_settings
from runtime.assistant.adapters import create_backend
from runtime.assistant.contract import ChatBackend

__all__ = ["backend_options", "drop_master_key", "main"]

_MASTER_KEY = f"{ENV_PREFIX}MASTER_KEY"


def drop_master_key() -> None:
    """Before anything reads the settings: the environment outranks `.env` for every later read,
    so no code path in this process can decrypt."""
    settings = get_settings()
    if not settings.master_key.get_secret_value():
        return
    if settings.assistant.own_user:
        sys.exit(
            f"factorial: the assistant holds {_MASTER_KEY}, which would open every stored "
            "secret; init writes it none, so remove it from /run/factorial-assistant/.env and "
            "the assistant's environment"
        )
    os.environ[_MASTER_KEY] = ""
    get_settings.cache_clear()
    load_keyring.cache_clear()


def backend_options(kind: str, settings: Settings) -> dict[str, object]:
    """What a backend is built with. The two that start a CLI get a working directory, and
    Codex the bridge to this process's tools. `bedrock_responses` starts nothing."""
    if kind == "bedrock_responses":
        return {}
    options: dict[str, object] = {"work_root": settings.assistant.work_root}
    if kind == "codex_cli":
        options["mcp_command"] = (sys.executable, "-m", "orchestrator.assistant.mcp_bridge")
    return options


async def _run(settings: Settings) -> None:
    @cache
    def backend_for(kind: str) -> ChatBackend:
        # One per kind: a backend keeps each live turn, which its `cancel` looks up.
        return create_backend(kind, **backend_options(kind, settings))

    @cache
    def tools_for(token: ReadTokenCache) -> McpDeps:
        # One per organization's token: each GitHub client opens a connection pool it never closes.
        return tool_deps(get_settings, session_scope, token)

    deps = RunnerDeps(
        sessions=session_scope,
        settings=get_settings,
        backend_for=backend_for,
        clock=lambda: datetime.now(UTC),
        tools=tools_for,
        github_tokens=OrganizationTokens(lambda org: api_fetch(get_settings, org=org)),
        credential=api_credential(get_settings),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await serve(deps, stop=stop, settings=settings)
    finally:
        await dispose_engine()


def main() -> None:
    configure_logging("assistant")
    drop_master_key()
    asyncio.run(_run(get_settings()))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
