"""The mirrors a session's supervisor keeps of the files it reads, on E2B and on a host.

A read takes whole lines only: a line still being written is read again, whole, next time. A
mirror is appended before the state that counts it is written, so after a restart it is cut back
to that state's offsets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = [
    "LOG_SHRANK",
    "MAX_LINE",
    "READ_LIMIT",
    "SIGNALS_SHRANK",
    "TRANSLATOR_CHANGED",
    "append",
    "lines",
    "read_slice",
    "truncate",
    "whole",
]

READ_LIMIT: Final = 1 << 20
#: The longest line a read grows to take whole. The agent can append to every file a supervisor
#: reads, so past it the run is stopped rather than the worker's memory spent.
MAX_LINE: Final = 16 << 20
LOG_SHRANK: Final = "the session log shrank below what was already read"
SIGNALS_SHRANK: Final = "the signals file shrank below what was already read"
TRANSLATOR_CHANGED: Final = (
    "the transcript translator changed mid-run, so the mirror restarts at the log's offset"
)


def whole(chunk: bytes, *, last: bool) -> bytes:
    """The whole lines of `chunk`; all of it once nothing more will be written."""
    if last:
        return chunk
    complete, sep, _ = chunk.rpartition(b"\n")
    return complete + sep


def lines(block: bytes) -> list[str]:
    if not block:
        return []
    return [raw.decode("utf-8", errors="replace") for raw in block.rstrip(b"\n").split(b"\n")]


def append(path: Path, block: bytes) -> None:
    if block:
        with path.open("ab") as mirror:
            mirror.write(block)


def truncate(path: Path, size: int) -> None:
    if path.exists() and path.stat().st_size > size >= 0:
        with path.open("r+b") as mirror:
            mirror.truncate(size)


def read_slice(path: Path, start: int, end: int) -> bytes:
    """`path`'s bytes from `start` to `end`; nothing when it is absent or the slice empty."""
    if not path.exists() or end <= start:
        return b""
    with path.open("rb") as mirror:
        mirror.seek(start)
        return mirror.read(end - start)
