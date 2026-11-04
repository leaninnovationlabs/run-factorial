"""Signed dashboard sessions — the credential the Google login mints — terminal tickets, the
one-use credential a WebSocket handshake carries (decision 57), and page links.

The signing keys are derived from `FACTORIAL_API_TOKEN` deliberately: the deployment already
treats that token as the credential that gates everything, so rotating it is one lever that also
logs every browser out, voids every ticket and invalidates every page link. A second secret could
linger and keep old sessions alive after a rotation. Each credential has a key of its own, so
neither passes for the other. `derive_key` is public because the MCP token (`mcp_server.tokens`)
and the issue marker draw their keys from the same lever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, NamedTuple

__all__ = [
    "SESSION_COOKIE",
    "TICKET_PROTOCOL",
    "TICKET_SECONDS",
    "SignedSession",
    "TerminalTicket",
    "derive_key",
    "mint_session",
    "mint_terminal_ticket",
    "read_terminal_ticket",
    "sign_page_link",
    "verified_session",
    "verify_page_link",
    "verify_session",
]

SESSION_COOKIE = "sf_session"

#: The subprotocol a ticket travels beside in `Sec-WebSocket-Protocol`, and the one the gateway
#: echoes: a browser can set no other header on a WebSocket, and a URL lands in access logs.
TICKET_PROTOCOL: Final = "factorial.terminal"
TICKET_SECONDS: Final = 30

#: v2 signed everyone out once when membership became the boundary in every deployment: a
#: session from before has no account behind it, and its next SSO sign-in makes one.
_SESSION_KEY: Final = b"dashboard-session-v2"
_TICKET_KEY: Final = b"terminal-ticket-v1"
_PAGE_LINK_KEY: Final = b"page-link-v1"
_TICKET_VERSION: Final = "v1"
#: A minted ticket is about 250 bytes; anything far past that was never one. The cap also keeps a
#: payload's nesting far below the depth at which `json.loads` raises RecursionError.
_MAX_TICKET: Final = 2048
_MAX_BY: Final = 320


def derive_key(api_token: str, purpose: bytes) -> bytes:
    """The 64-byte key for one kind of credential; `purpose` keeps each kind's key its own."""
    # sha256 the token first: blake2b caps its key at 64 bytes, and the token's length is
    # operator-chosen.
    return hashlib.blake2b(purpose, key=hashlib.sha256(api_token.encode()).digest()).digest()


def _sign(payload: bytes, api_token: str, purpose: bytes) -> str:
    return hmac.new(derive_key(api_token, purpose), payload, hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    # Padding stripped: "=" is outside a subprotocol's characters and SimpleCookie's legal set,
    # where a padded value gets double-quoted on the wire, comes back so, and fails the HMAC.
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode())


class SignedSession(NamedTuple):
    email: str
    #: Unix seconds.
    expires: int
    #: Whether the account's email was verified when the session was minted. A session minted
    #: unverified ends when an SSO sign-in verifies the account (`access.accounts.session_ended`).
    verified: bool
    #: `app_user.session_epoch` when the session was minted; it ends when the account's moves on.
    epoch: int


_VERIFIED: Final = {"v": True, "u": False}


def mint_session(
    email: str, *, verified: bool, api_token: str, now: datetime, max_age: int, epoch: int = 0
) -> str:
    expires = int(now.timestamp()) + int(max_age)
    payload = f"{_b64(email.encode())}.{expires}.{'v' if verified else 'u'}.{int(epoch)}"
    return f"{payload}.{_sign(payload.encode(), api_token, _SESSION_KEY)}"


def verified_session(token: str | None, *, api_token: str, now: datetime) -> SignedSession | None:
    """The signed session, or None. An unset api_token verifies nothing: fail closed."""
    # compare_digest raises on a str that is not ASCII.
    if not token or not api_token or not token.isascii():
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[2] not in _VERIFIED:
        return None
    email_b64, expires_text, verified, epoch_text, signature = parts
    payload = f"{email_b64}.{expires_text}.{verified}.{epoch_text}"
    if not hmac.compare_digest(signature, _sign(payload.encode(), api_token, _SESSION_KEY)):
        return None
    try:
        expires = int(expires_text)
        if expires < int(now.timestamp()):
            return None
        return SignedSession(
            _unb64(email_b64).decode(), expires, _VERIFIED[verified], int(epoch_text)
        )
    except (ValueError, UnicodeDecodeError):
        return None


def verify_session(token: str | None, *, api_token: str, now: datetime) -> str | None:
    """The signed-in email, or None."""
    verified = verified_session(token, api_token=api_token, now=now)
    return verified.email if verified is not None else None


@dataclass(frozen=True, slots=True)
class TerminalTicket:
    """What a ticket admits: one viewer of `step_run_id`, charged to `by`, before `expires`,
    once. `until` is when the session that minted it ends, and the connection with it; None for
    the bearer token, which lasts until it is rotated. `epoch` is that session's, so signing out
    everywhere ends the connection too. Times are Unix seconds."""

    step_run_id: int
    by: str
    expires: int
    nonce: str
    until: int | None
    epoch: int | None = None

    def unexpired(self, now: datetime) -> bool:
        return now.timestamp() < self.expires

    def lasts(self, now: datetime) -> bool:
        return self.until is None or now.timestamp() < self.until


def mint_terminal_ticket(
    step_run_id: int,
    by: str,
    *,
    until: int | None,
    api_token: str,
    now: datetime,
    epoch: int | None = None,
) -> tuple[str, datetime]:
    """`v1.<claims>.<hmac>` and when it expires. Every character is one a WebSocket subprotocol
    may hold."""
    claims: dict[str, Any] = {
        "r": step_run_id,
        "b": by,
        "e": int(now.timestamp()) + TICKET_SECONDS,
        "n": secrets.token_urlsafe(12),
    }
    if until is not None:
        claims["u"] = until
    if epoch is not None:
        claims["g"] = epoch
    payload = f"{_TICKET_VERSION}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    ticket = f"{payload}.{_sign(payload.encode(), api_token, _TICKET_KEY)}"
    return ticket, now + timedelta(seconds=TICKET_SECONDS)


def _int(value: object) -> int | None:
    # bool is an int to isinstance.
    return value if type(value) is int else None


def read_terminal_ticket(value: str | None, *, api_token: str) -> TerminalTicket | None:
    """The claims of a ticket this deployment signed, expired or not, or None for anything else.
    The signature is checked before a byte of the payload is decoded, and nothing raises. A mode
    (`m`), which tickets carried before the writer seat, is ignored."""
    if not value or not api_token or len(value) > _MAX_TICKET or not value.isascii():
        return None
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != _TICKET_VERSION:
        return None
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(parts[2], _sign(payload.encode(), api_token, _TICKET_KEY)):
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
    except ValueError:
        return None
    if not isinstance(claims, dict):
        return None
    run, expires, until = _int(claims.get("r")), _int(claims.get("e")), claims.get("u")
    by, nonce, epoch = claims.get("b"), claims.get("n"), claims.get("g")
    if (
        run is None
        or expires is None
        or (until is not None and _int(until) is None)
        or (epoch is not None and _int(epoch) is None)
        or not isinstance(by, str)
        or not 0 < len(by) <= _MAX_BY
        or not isinstance(nonce, str)
        or not nonce
    ):
        return None
    return TerminalTicket(run, by, expires, nonce, until, epoch)


def _page_payload(step_run_id: int, blob: str, expires: int) -> bytes:
    return f"{step_run_id}.{blob}.{expires}".encode()


def sign_page_link(
    step_run_id: int, blob: str, *, api_token: str, now: datetime
) -> tuple[int, str]:
    """A link to one run's file at one blob (tech-stack decision 58). A frame and a new tab cannot
    send the dashboard's bearer token, so the guarded API vouches for the link instead. It expires
    at the start of the hour after next — one to two hours away — so every link minted within an
    hour is the same link and a polled answer does not reload the frame."""
    expires = (int(now.timestamp()) // 3600 + 2) * 3600
    return expires, _sign(_page_payload(step_run_id, blob, expires), api_token, _PAGE_LINK_KEY)


def verify_page_link(
    step_run_id: int, blob: str, expires: int, signature: str, *, api_token: str, now: datetime
) -> bool:
    """Fail closed: an unset token verifies nothing."""
    if not api_token or expires < int(now.timestamp()):
        return False
    expected = _sign(_page_payload(step_run_id, blob, expires), api_token, _PAGE_LINK_KEY)
    # Compare as bytes to handle non-ASCII signatures from untrusted callers gracefully.
    return hmac.compare_digest(signature.encode(), expected.encode())
