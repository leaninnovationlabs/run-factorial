"""Refusals more than one route module makes, so the rule is stated once. The wake a request
sends, and the wait for its pass, are `api.wakes`. A refusal an MCP tool makes too lives with
its service instead (`api.services.tickets.refuse_while_quarantined`).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from orchestrator.secrets.store import NAME_RE

__all__ = ["NoEchoRoute", "refuse_bad_name"]


def refuse_bad_name(name: str, noun: str) -> None:
    """422 for a secret or credential name that could not be an environment variable."""
    if not NAME_RE.match(name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The {noun} name must match [A-Za-z_][A-Za-z0-9_]{{0,63}}.",
        )


class NoEchoRoute(APIRoute):
    """A 422 that names where and why, never the `input`, so a refused secret is not echoed."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [
                    {
                        key: value.removeprefix("Value error, ") if key == "msg" else value
                        for key, value in error.items()
                        if key != "input"
                    }
                    for error in exc.errors()
                ]
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": jsonable_encoder(errors)},
                )

        return handle
