"""Device-auth logins for codex subscription runtimes, behind `POST /runtimes/{id}/login`.

The API runs `codex login --device-auth` in a private config home, hands the one-time code to the
client, and a watcher stores the resulting `auth.json` as the credential the adapter reads
(`CODEX_AUTH_JSON`). Logins live in this process's memory, so a deployment with more than one API
process would have to pin a client to one of them — a documented limit, not a design.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from fastapi import HTTPException, status

from orchestrator.api.deps import LoginRunner, LoginSessions
from orchestrator.api.schemas import RuntimeLoginOut
from orchestrator.api.services.errors import as_sentence
from orchestrator.secrets import store
from runtime.adapters.codex_cli.cli import AUTH_FILE
from runtime.adapters.codex_cli.login import LoginChild, parse_device_prompt
from runtime.contract import AdapterUnavailable

__all__ = ["Login", "LoginStatus", "begin", "close_logins", "find"]

#: The device-code flow's own lifetime, as the CLI states it; the watcher enforces it.
_LOGIN_TTL: Final = timedelta(minutes=15)
#: How long the CLI gets to print its prompt before the start route gives up on it.
_PROMPT_TIMEOUT_S: Final = 30.0
_PROMPT_POLL_S: Final = 0.1
_TAIL_LINES: Final = 5
#: How often the watcher looks at the child. Coarse on purpose: a login takes a human minutes.
_WATCH_POLL_S: Final = 0.5

logger = logging.getLogger(__name__)

LoginStatus = Literal["pending", "complete", "failed", "expired"]


@dataclass
class Login:
    """One device-auth login in flight. `home` holds the plaintext `auth.json` once the browser
    side completes, which is why it is deleted the moment the outcome is known."""

    runtime_id: int
    #: Where the login lands: `credential_name_for(row.type)`, fixed when the login starts.
    credential_name: str
    child: LoginChild
    home: Path
    expires_at: datetime
    status: LoginStatus = "pending"
    detail: str | None = None
    #: `_watch` and `_abandon` (a newer login, or shutdown) can race, and storing then deleting
    #: the file must happen once, whichever gets there first.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: asyncio.Task[None] | None = None


#: Process-local (module docstring). Keyed by the session id the client polls with.
_LOGINS: dict[str, Login] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _tail(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-_TAIL_LINES:])


def _discard(login: Login) -> None:
    """Forget the login's on-disk state. Idempotent: the outcome paths and expiry all call it."""
    shutil.rmtree(login.home, ignore_errors=True)


def _prune(now: datetime) -> None:
    """Drop settled sessions nobody can still be polling — past expiry, a client has moved on."""
    for session_id, login in list(_LOGINS.items()):
        if login.status != "pending" and now >= login.expires_at:
            del _LOGINS[session_id]


async def _await_prompt(child: LoginChild) -> tuple[str, str]:
    """`(verification_url, user_code)` from the CLI's output, or why there is none."""
    deadline = time.monotonic() + _PROMPT_TIMEOUT_S
    while True:
        if prompt := parse_device_prompt(child.output):
            return prompt
        if child.returncode is not None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="`codex login` exited before it showed a device code "
                f"(exit {child.returncode}): {as_sentence(_tail(child.output) or 'no output')}",
            )
        if time.monotonic() >= deadline:
            child.kill()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="`codex login` printed no device code within "
                f"{_PROMPT_TIMEOUT_S:.0f} seconds: "
                + as_sentence(_tail(child.output) or "no output"),
            )
        await asyncio.sleep(_PROMPT_POLL_S)


async def begin(
    runtime_id: int, credential_name: str, start: LoginRunner, sessions: LoginSessions
) -> RuntimeLoginOut:
    """Start a login in a scratch home, wait for the CLI's prompt, and hand the rest to a watcher.
    A CLI that cannot run at all is a 503."""
    _prune(_now())
    for previous in list(_LOGINS.values()):
        if previous.runtime_id == runtime_id and previous.status == "pending":
            # Navigating away loses the page's session id; Connect again must not leave the
            # first login's CLI and scratch home behind, nor let two race to store.
            await _abandon(previous, "A newer sign-in replaced this one.")
    home = Path(tempfile.mkdtemp(prefix="factorial-codex-login-"))
    try:
        try:
            child = await start(home)
        except AdapterUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        verification_url, user_code = await _await_prompt(child)
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise
    login = Login(
        runtime_id=runtime_id,
        credential_name=credential_name,
        child=child,
        home=home,
        expires_at=_now() + _LOGIN_TTL,
    )
    session_id = uuid4().hex
    _LOGINS[session_id] = login
    login.watcher = asyncio.create_task(_watch(login, sessions), name=f"codex-login-{session_id}")
    return RuntimeLoginOut(
        session_id=session_id,
        verification_url=verification_url,
        user_code=user_code,
        expires_at=login.expires_at,
    )


async def _abandon(login: Login, detail: str) -> None:
    """Stop a pending login without an outcome: kill the CLI, drop the home, say why."""
    if login.watcher is not None and login.watcher is not asyncio.current_task():
        login.watcher.cancel()
    async with login.lock:
        if login.status == "pending":
            login.child.kill()
            _discard(login)
            login.status, login.detail = "failed", detail


async def _watch(login: Login, sessions: LoginSessions) -> None:
    """The one place a login settles, whether or not anyone is polling.

    Order on success is store, COMMIT, then discard: the scratch home is the only copy of the
    login until the transaction holding the credential is durable, so a commit that fails must
    leave the file for the next attempt rather than report a credential that was never written.
    """
    try:
        while login.status == "pending":
            if login.child.returncode is not None or _now() >= login.expires_at:
                break
            await asyncio.sleep(_WATCH_POLL_S)
        async with login.lock:
            if login.status != "pending":
                return
            if login.child.returncode is None:
                login.child.kill()
                _discard(login)
                login.status, login.detail = (
                    "expired",
                    "The device code expired before it was used.",
                )
                return
            code = login.child.returncode
            auth_file = login.home / AUTH_FILE
            if code == 0 and auth_file.is_file():
                # The one sanctioned read of the plaintext login; it goes to the store and nowhere
                # else. The scope's exit is the commit.
                async with sessions() as session:
                    await store.put_for_runtime(
                        session, login.runtime_id, login.credential_name, auth_file.read_text()
                    )
                login.status = "complete"
            elif code == 0:
                login.status = "failed"
                login.detail = f"`codex login` exited 0 but wrote no {AUTH_FILE}."
            else:
                login.status = "failed"
                tail = _tail(login.child.output) or "no output"
                login.detail = f"`codex login` exited {code}: {as_sentence(tail)}"
            _discard(login)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The outcome is what failed, not the secret: nothing from the home is in the message.
        logger.exception("device login watcher for runtime %s failed", login.runtime_id)
        async with login.lock:
            if login.status == "pending":
                login.child.kill()
                _discard(login)
                login.status, login.detail = "failed", "The sign-in could not be saved."


async def close_logins() -> None:
    """Shutdown: a pending login's CLI must not outlive the API, and its home must not either."""
    for login in list(_LOGINS.values()):
        await _abandon(login, "The API shut down.")
    _LOGINS.clear()


def find(runtime_id: int, session_id: str) -> Login | None:
    login = _LOGINS.get(session_id)
    return login if login is not None and login.runtime_id == runtime_id else None
