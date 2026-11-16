"""The HTTP side of `api.services.errors`. A service refusal becomes its status and
`{"detail": message}`, the shape `HTTPException` gives, so a route whose refusal moved into a
service answers exactly as before. A refusal's `code` and `hint` join it when it has them."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse
from githubkit.exception import RateLimitExceeded

from orchestrator.access.accounts import HasherBusy
from orchestrator.api.services.errors import ServiceError, located
from orchestrator.backoff import ConnectionBackoff
from orchestrator.github.client import RateLimited

__all__ = ["backoff_handler", "backoff_wait", "hasher_busy_handler", "service_error_handler"]

#: The wait a client is told when the forge named none, as for a host lookup that did not answer.
BACKOFF_RETRY_SECONDS: Final = 30


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    content: dict[str, str | list[str]] = {"detail": located(exc)}
    if exc.path:
        content["path"] = exc.path
    if exc.code is not None:
        content["code"] = exc.code
    if exc.hint:
        content["hint"] = exc.hint
    if exc.missing_runtimes:
        content["missing_runtimes"] = list(exc.missing_runtimes)
    return JSONResponse(status_code=exc.status, content=content)


async def hasher_busy_handler(request: Request, exc: HasherBusy) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many sign-ins at once. Try again in a moment."},
        headers={"Retry-After": "1"},
    )


def backoff_wait(exc: ConnectionBackoff | RateLimitExceeded) -> tuple[str, int]:
    """What a caller reads while a forge connection waits, and the seconds until it may ask
    again."""
    if isinstance(exc, RateLimited):
        wait = (exc.until - datetime.now(UTC)).total_seconds()
    elif isinstance(exc, RateLimitExceeded):
        wait = exc.retry_after.total_seconds()
    else:
        return (
            "The project's forge can't be reached right now. Try again in a moment.",
            BACKOFF_RETRY_SECONDS,
        )
    seconds = max(1, math.ceil(wait))
    return (
        f"GitHub's rate limit for this project's connection is spent. "
        f"Try again in {seconds} seconds.",
        seconds,
    )


async def backoff_handler(
    request: Request, exc: ConnectionBackoff | RateLimitExceeded
) -> JSONResponse:
    message, seconds = backoff_wait(exc)
    return JSONResponse(
        status_code=503, content={"detail": message}, headers={"Retry-After": str(seconds)}
    )
