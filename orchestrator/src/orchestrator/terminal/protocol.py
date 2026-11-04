"""The terminal WebSocket's wire format (decision 57), mirrored by the dashboard's
`ui/src/components/terminal/protocol.ts`.

`/factorial/terminal/runs/{step_run_id}?cols=&rows=`. Every connection attaches as a viewer, on a
read-only tmux client. A `mode` in the query is accepted and ignored, so a dashboard from before
the seat still connects, gets a viewer, and reads the `writable` that `hello` no longer carries as
false.

A run has one writer seat. Only its holder's tmux client is writable: a viewer that takes the seat
is re-attached writable once the displaced holder's writable client is closed, and one that loses it
is re-attached read-only, which tmux redraws. A claim is granted when the seat is free or already
the asker's; refused, naming the holder, when another holds it; and refused with the admission's
sentence on an open session or a session its ticket has moved past. A forced claim takes the seat
from its holder. The seat frees on a release, on its holder's detach, after `SEAT_IDLE` seconds
without its holder's typing, and when its holder may no longer type.

Binary frames carry bytes both ways: the holder's keystrokes in, the pty's output out. Anyone
else's keystrokes are dropped. Text frames are JSON, and `seat` in `hello` and `status` is
`{"holder": str | null, "yours": bool}`: `holder` is who holds it (an email, or `api-token`),
and `yours` is true while this viewer's keystrokes reach the session. `names` there is who is
watching: the same name for each attached viewer, one per viewer, sorted. Keys a viewer sends before
a claim's answer says `yours` are dropped, never held back for after it.

In:
- `{"type":"resize","cols":int,"rows":int}`: the holder's sets the size; anyone else's is
  ignored.
- `{"type":"claim"}`, or `{"type":"claim","force":true}` to take the seat from its holder. A
  claim sent while the viewer's last one is still being decided is answered at once with the
  seat as it stands; the earlier claim still gives its own answer.
- `{"type":"release"}`. It also withdraws a claim still being decided.

Out:
- `{"type":"hello","status","target":"session"|"inspect","viewers","writers","seat","names"}`
  once, after attach.
- `{"type":"status","status","viewers","writers","seat","names"}` when the run's status, the
  counts, the names or the seat change. `writers` is 0 or 1: whether the seat is held.
- `{"type":"seat","holder","yours","taken","reason"}`: the answer to a claim or a release, and
  to a holder that lost the seat. `taken` is true when someone took it over; `reason` is why a
  claim was refused, or why the seat freed without its holder releasing it, else null.
- `{"type":"exit","reason"}` before a 1000 close when the session ended.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

__all__ = [
    "AUDIT_MAX",
    "AUDIT_QUIET",
    "MAX_REASON_BYTES",
    "MAX_VIEWERS",
    "RECHECK_SECONDS",
    "SEAT_IDLE",
    "VIEWER_QUEUE",
    "Claim",
    "ClientFrame",
    "CloseCode",
    "Release",
    "Resize",
    "close_reason",
    "exit_frame",
    "hello_frame",
    "parse_frame",
    "parse_size",
    "seat_frame",
    "status_frame",
]

#: Output chunks a viewer may fall behind by before it is closed 4408.
VIEWER_QUEUE: Final = 256
#: Viewers one run may have at once, viewers, the seat's holder included.
MAX_VIEWERS: Final = 16
#: Seconds between re-runs of the admission rule and the cookie's expiry on an open connection.
RECHECK_SECONDS: Final = 5.0
#: An input burst is recorded after this many seconds without a keystroke...
AUDIT_QUIET: Final = 2.0
#: ...or this many after its first, whichever comes first, and always on detach.
AUDIT_MAX: Final = 10.0
#: Seconds without its holder's typing after which the writer seat frees.
SEAT_IDLE: Final = 120.0

#: RFC 6455 leaves 123 bytes for a close frame's reason.
MAX_REASON_BYTES: Final = 123

DEFAULT_COLS: Final = 80
DEFAULT_ROWS: Final = 24
_MAX_SIDE: Final = 0xFFFF
#: Longer than any well-formed frame in: anything past it is not parsed at all.
_MAX_TEXT_FRAME: Final = 256


class CloseCode(IntEnum):
    """After `accept()`. A refusal before it (a foreign or missing Origin, no session cookie) is
    an HTTP 403, which a browser reports only as a failed connection."""

    #: The session or an open session ended, or the client left. No reconnect.
    ENDED = 1000
    #: The query string broke this protocol: a client bug. A malformed frame is dropped.
    MALFORMED = 1008
    #: The gateway failed: it could not record this viewer, its taking the seat, or the
    #: holder's input, so it takes no more of that input, or attaching broke unexpectedly. No
    #: reconnect.
    INTERNAL = 1011
    #: The gateway is restarting, lost its database, or the run's terminal moved. Reconnect.
    RESTARTING = 1012
    #: The session cookie expired or stopped verifying mid-connection. Sign in again.
    SIGNED_OUT = 4401
    #: The terminal switch is off, the ticket is quarantined, or the viewer is no longer a
    #: member allowed terminals in the run's organization. No reconnect.
    FORBIDDEN = 4403
    #: Nothing to attach to: a finished run, no recorded target, or a target the
    #: gateway will not attach to. No reconnect.
    NO_TERMINAL = 4404
    #: This viewer fell behind its queue. Reconnect at once; tmux redraws on attach.
    TOO_SLOW = 4408
    #: Parked with no open session: offer Resume session or Open session. No reconnect.
    PARKED = 4409
    #: The run already has `MAX_VIEWERS` viewers. No reconnect.
    CROWDED = 4429
    #: The substrate is unreachable: the sandbox is not running, or tmux would not attach.
    #: Reconnect with backoff.
    UNREACHABLE = 4502


def close_reason(text: str) -> str:
    """`text` cut to fit a close frame, never mid-character."""
    data = text.encode()
    if len(data) <= MAX_REASON_BYTES:
        return text
    return data[: MAX_REASON_BYTES - 3].decode(errors="ignore") + "…"


def _frame(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields}, separators=(",", ":"))


def _seat(holder: str | None, yours: bool) -> dict[str, object]:
    return {"holder": holder, "yours": yours}


def hello_frame(
    *,
    status: str | None,
    target: str,
    viewers: int,
    writers: int,
    holder: str | None,
    names: Sequence[str],
) -> str:
    return _frame(
        "hello",
        status=status,
        target=target,
        viewers=viewers,
        writers=writers,
        seat=_seat(holder, False),
        names=list(names),
    )


def status_frame(
    *,
    status: str | None,
    viewers: int,
    writers: int,
    holder: str | None,
    yours: bool,
    names: Sequence[str],
) -> str:
    return _frame(
        "status",
        status=status,
        viewers=viewers,
        writers=writers,
        seat=_seat(holder, yours),
        names=list(names),
    )


def seat_frame(
    *, holder: str | None, yours: bool, taken: bool = False, reason: str | None = None
) -> str:
    return _frame("seat", holder=holder, yours=yours, taken=taken, reason=reason)


def exit_frame(reason: str) -> str:
    return _frame("exit", reason=reason)


def _side(value: object) -> int | None:
    # `bool` is an `int`; `True` is no terminal width.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _MAX_SIDE else None


def parse_size(cols: str | None, rows: str | None) -> tuple[int, int] | None:
    """The size the query string asks to attach at, the defaults for an absent side, or None
    when either side is not a whole number in 1..65535."""
    sides: list[int] = []
    for text, default in ((cols, DEFAULT_COLS), (rows, DEFAULT_ROWS)):
        if text is None:
            sides.append(default)
            continue
        if len(text) > 5 or not text.isascii() or not text.isdigit():
            return None
        side = _side(int(text))
        if side is None:
            return None
        sides.append(side)
    return sides[0], sides[1]


@dataclass(frozen=True, slots=True)
class Resize:
    cols: int
    rows: int


@dataclass(frozen=True, slots=True)
class Claim:
    force: bool


@dataclass(frozen=True, slots=True)
class Release:
    pass


ClientFrame = Resize | Claim | Release


def parse_frame(text: str) -> ClientFrame | None:
    """A text frame from the client, or None for anything malformed, which is dropped. The relay
    refuses a size outside 1..65535, so nothing past here sees one."""
    if len(text) > _MAX_TEXT_FRAME:
        return None
    try:
        frame = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(frame, dict):
        return None
    match frame.get("type"):
        case "resize":
            cols, rows = _side(frame.get("cols")), _side(frame.get("rows"))
            return Resize(cols, rows) if cols is not None and rows is not None else None
        case "claim":
            force = frame.get("force", False)
            return Claim(force) if isinstance(force, bool) else None
        case "release":
            return Release()
        case _:
            return None
