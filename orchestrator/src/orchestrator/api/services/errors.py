"""What a service refuses with. Each class carries the HTTP status a route answers with
(`api/errors.py`). The MCP registry renders the same exception, with its hint and candidates, as
a tool error."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

__all__ = [
    "Conflict",
    "Forbidden",
    "Invalid",
    "NotFound",
    "RefusalCode",
    "ServiceError",
    "Unavailable",
    "UpstreamFailed",
    "as_sentence",
    "render",
]

#: What a refusal is, for a client that branches on it rather than on its words, which may
#: change. Closed: a new code is added here.
RefusalCode = Literal[
    "quarantined",
    # Refresh refused: the ticket's recent passes failed, and the loop retries it on its own.
    "failing",
    "ticket_done",
    "ticket_exited",
    "not_exited",
    "not_quarantined",
    "not_blocked",
    # Un-block refused: the ticket is parked, and Resume continues it.
    "parked",
    # Interrupt refused: the run has ended.
    "not_running",
    "not_an_agent_run",
    "not_parked",
    # Resume refused: only a person, from the dashboard, resumes a session at its ceiling.
    "ceiling",
    # Retry now refused: the backoff has run, so the retry is starting anyway.
    "already_due",
    "process_retired",
    # Restart refused: the process has a newer version than the one the caller restarted onto.
    "process_moved",
    # Restart refused: the ticket already runs its process's current version.
    "already_current",
    "change_request_pending",
    # The idempotency key was already used for another run.
    "key_reused",
    # The run never ran on a runtime, so it has no transcript.
    "no_transcript",
    "runtime_unavailable",
    # Activation refused: a core readiness row is blocked.
    "not_ready",
    # Sync or commit refused: the config branch head is one this install did not write.
    "unknown_head",
    # Sync refused: a process names a runtime this organization has not created.
    "runtime_missing",
]


class ServiceError(Exception):
    """A refusal with a message for the caller, an optional next step, the valid values when
    the caller named something that does not exist or is ambiguous, and a code a client may
    branch on. `missing_runtimes` lists the slugs a `runtime_missing` refusal asks to create.
    `tool_hint` is the next step for a tool caller, an MCP client or the assistant,
    when it differs from the person's: the tool to call or the argument to pass. The dashboard
    never shows it."""

    status: ClassVar[int] = 500

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        candidates: Sequence[str] = (),
        code: RefusalCode | None = None,
        tool_hint: str | None = None,
        missing_runtimes: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.candidates = tuple(candidates)
        self.code = code
        self.tool_hint = tool_hint
        self.missing_runtimes = tuple(missing_runtimes)


def as_sentence(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else f"{text}."


def render(exc: ServiceError) -> str:
    """The refusal as an MCP client reads it: the message, then `Next:` and `Valid:` lines."""
    lines = [exc.message]
    if exc.tool_hint or exc.hint:
        lines.append(f"Next: {exc.tool_hint or exc.hint}")
    if exc.candidates:
        lines.append("Valid: " + ", ".join(exc.candidates))
    return "\n".join(lines)


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Forbidden(ServiceError):
    """403, never 401: the dashboard signs the person out on any 401."""

    status = 403


class Invalid(ServiceError):
    status = 422


class Unavailable(ServiceError):
    status = 503


class UpstreamFailed(ServiceError):
    status = 502
