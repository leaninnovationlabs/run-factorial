"""A fixed-window request limit per client (`client_key`), in process memory.

The API is one process, so its memory is the whole count, and a restart forgets it. uvicorn takes
`request.client.host` from `X-Forwarded-For` only when the load balancer sent it
(`FORWARDED_ALLOW_IPS`), so a client cannot name its own address. Behind a proxy that variable
does not name, every client is the proxy's address, and one client spends everyone's tries:
`UntrustedForwardedFor` says so in the log.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import time
from typing import overload

from fastapi import HTTPException, Request, status
from starlette.types import ASGIApp, Receive, Scope, Send

from orchestrator.lru import CAPACITY, BoundedLRU

__all__ = ["RateLimit", "UntrustedForwardedFor", "client_address", "client_key"]

log = logging.getLogger(__name__)


@overload
def client_address(request: Request) -> str: ...
@overload
def client_address(request: Request, unknown: None) -> str | None: ...
def client_address(request: Request, unknown: str | None = "unknown") -> str | None:
    return request.client.host if request.client is not None else unknown


def client_key(request: Request) -> str:
    """The client a limit or a lockout counts: an IPv4 address, or an IPv6 address's /64. A
    subscriber is handed a whole /64, so keyed on the address, a new one from it would reset
    both."""
    host = client_address(request)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RateLimit:
    def __init__(self, limit: int, window_seconds: float, *, capacity: int = CAPACITY):
        self._limit = limit
        self._window = window_seconds
        #: address → (window start, requests in it).
        self._windows: BoundedLRU[str, tuple[float, int]] = BoundedLRU(capacity)

    def retry_after(self, key: str, now: float | None = None) -> float | None:
        """Count one request from `key`. None admits it; otherwise the seconds until it may."""
        now = time.monotonic() if now is None else now
        start, count = self._windows.get(key, (now, 0))
        if now - start >= self._window:
            start, count = now, 0
        self._windows.put(key, (start, count + 1))
        if count < self._limit:
            return None
        return self._window - (now - start)

    def check(self, request: Request) -> None:
        """Refuse a request over the limit with 429 and `Retry-After`."""
        wait = self.retry_after(client_key(request))
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many tries from this address. Try again later.",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )


def _forwarded_host(entry: str) -> str:
    entry = entry.strip()
    if entry.startswith("["):
        return entry[1:].split("]", 1)[0]
    return entry.split(":", 1)[0] if entry.count(":") == 1 else entry


class UntrustedForwardedFor:
    """Warns once per process about a request whose `X-Forwarded-For` uvicorn did not apply: the
    client it left is none of the addresses the header names, so the peer is not in
    `FORWARDED_ALLOW_IPS`. A misconfigured proxy is otherwise invisible until one client locks
    the others out."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._warned and scope["type"] in ("http", "websocket"):
            self._check(scope)
        await self.app(scope, receive, send)

    def _check(self, scope: Scope) -> None:
        client = scope.get("client")
        named = {
            _forwarded_host(entry)
            for name, value in scope.get("headers", ())
            if name == b"x-forwarded-for"
            for entry in value.decode("latin-1").split(",")
        }
        if client is None or not named or client[0] in named:
            return
        self._warned = True
        log.warning(
            "A request from %s sent X-Forwarded-For, which the API ignored: FORWARDED_ALLOW_IPS "
            "does not name that address. Behind a proxy, every client then shares the proxy's "
            "address, and one client can use up the others' password tries and lock them out. "
            "Set FORWARDED_ALLOW_IPS to the proxy's address.",
            client[0],
        )
