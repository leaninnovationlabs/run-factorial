"""Service logging: one handler per process, JSON lines or text, credentials masked last.

Every entrypoint calls `configure_logging(service)` once, before it logs (tech-stack decision 81);
tests never do, so the test runner keeps its own handlers. `FACTORIAL_LOG_LEVEL` and
`FACTORIAL_LOG_FORMAT` are read with `os.getenv`, not through `Settings`, because a settings file
that fails to load still has to be logged.

`bind()` puts a closed set of ids on the records logged inside it, but for the loggers in
`_UNBOUND`, and `note()` adds more to the block it is called in, for an id learned halfway through
it; a None value removes a field. `revert_on_error()` takes back the notes of a block that raises,
for work a rollback erases, but for what `keep()` noted of rows that existed before it. The
fields live in a `ContextVar`, so a ticket's task never sees another's, and the formatter reads
them on the logging call's own task: nothing here may move formatting to another thread.

`redact()` is a backstop, not a boundary: secret values are never logged (design.md §7.1). It runs
over each value last, so it covers third-party loggers too. `github/media.py` keeps its own filter
for signed media URLs, which no general pattern can tell from an ordinary link.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType, TracebackType
from typing import Final

__all__ = [
    "FIELDS",
    "JsonFormatter",
    "TextFormatter",
    "bind",
    "configure_logging",
    "keep",
    "note",
    "redact",
    "revert_on_error",
]

LEVEL_ENV: Final = "FACTORIAL_LOG_LEVEL"
FORMAT_ENV: Final = "FACTORIAL_LOG_FORMAT"

#: The only names `bind()` accepts. Saved queries filter on them, so a name is never renamed, and
#: none can hold a credential.
FIELDS: Final = frozenset(
    {
        "org_id",
        "project_id",
        "ticket_id",
        "step",
        "attempt",
        "step_run_id",
        "process_version_id",
        "request_id",
        "conversation_id",
        "turn_id",
        "task",
    }
)

_EMPTY: Final[Mapping[str, str | int]] = MappingProxyType({})
_context: ContextVar[Mapping[str, str | int]] = ContextVar("log_context", default=_EMPTY)

#: What `keep()` noted inside the innermost `revert_on_error` block and no later note replaced.
_kept: ContextVar[Mapping[str, str | int]] = ContextVar("log_kept", default=_EMPTY)

#: Loggers that write from the event loop's exception handler, a garbage collection or a warning,
#: on whatever task happens to be running: the bound ids would name an unrelated ticket.
_UNBOUND: Final = ("asyncio", "sqlalchemy.pool", "py.warnings")

_MASK: Final = "[redacted]"
#: One alternation, so a value is scanned once. The `scheme` and `user` groups are kept: they name
#: what leaked without being the secret. A Bedrock bearer key's base64 holds a presigned URL that
#: carries the task role's session token.
_SECRET: Final = re.compile(
    r"\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bsk-ant-[A-Za-z0-9_-]{10,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bbedrock-api-key-[A-Za-z0-9+/=]{20,}"
    r"|\bglpat-[A-Za-z0-9_.-]{20,}"
    r"|\be2b_[A-Za-z0-9]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"
    r"|\b(?P<scheme>(?i:bearer) )[A-Za-z0-9._~+/=-]{16,}"
    r"|(?P<user>://[^/\s:@]+:)[^/\s@]+(?=@)"
)

_UVICORN: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


#: Docker splits a longer line into parts, and awslogs stores each part as an event of its own.
#: `json.dumps` escapes everything outside ASCII, so a character of a line is a byte.
_MAX_LINE: Final = 16_000
_CUT: Final = " [... {} characters cut ...] "


def _width(text: str) -> int:
    return len(json.dumps(text)) - 2


def _cut(text: str, length: int) -> str:
    """`text` in at most `length` characters once escaped, or the marker alone when `length` is
    shorter: its first quarter and its last three quarters around the marker, because the end of a
    traceback names the exception."""
    room = max(length - len(_CUT.format(len(text))), 0)
    head = used = 0
    while head < len(text) and used + (width := _width(text[head])) <= room // 4:
        used, head = used + width, head + 1
    tail, used = len(text), 0
    while tail > head and used + (width := _width(text[tail - 1])) <= room - room // 4:
        used, tail = used + width, tail - 1
    return text[:head] + _CUT.format(tail - head) + text[tail:]


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: (m["scheme"] or m["user"] or "") + _MASK, text)


def _merged(fields: Mapping[str, str | int | None]) -> Mapping[str, str | int]:
    unknown = fields.keys() - FIELDS
    if unknown:
        raise ValueError(f"not a log field: {', '.join(sorted(unknown))}")
    merged = {**_context.get(), **fields}
    return MappingProxyType({k: v for k, v in merged.items() if v is not None})


def _fields(record: logging.LogRecord) -> Mapping[str, str | int]:
    name = record.name
    if any(name == prefix or name.startswith(prefix + ".") for prefix in _UNBOUND):
        return _EMPTY
    return _context.get()


@contextmanager
def bind(**fields: str | int | None) -> Iterator[None]:
    """Add `fields` to the records logged inside the block, but for the loggers in `_UNBOUND`. A
    None value removes the field for the block."""
    token = _context.set(_merged(fields))
    try:
        yield
    finally:
        _context.reset(token)


def note(**fields: str | int | None) -> None:
    """Add `fields` to the enclosing `bind` block, whose exit removes them with its own; a None
    value removes the field now. Outside a block they last as long as the task."""
    _context.set(_merged(fields))
    kept = _kept.get()
    if kept.keys() & fields.keys():
        _kept.set(MappingProxyType({k: v for k, v in kept.items() if k not in fields}))


def keep(**fields: str | int) -> None:
    """`note()` for ids of rows that existed before the enclosing `revert_on_error` block, such as
    a run the tick read: its revert keeps them, unless a later note in the block replaced them."""
    note(**fields)
    _kept.set(MappingProxyType({**_kept.get(), **fields}))


@contextmanager
def revert_on_error() -> Iterator[None]:
    """Keep the notes made inside the block, unless it raises: then the fields go back to what
    they were when it began, plus what `keep()` noted. Blocks do not nest."""
    saved = _context.get()
    token = _kept.set(_EMPTY)
    try:
        yield
    except BaseException:
        _context.set(MappingProxyType({**saved, **_kept.get()}))
        raise
    finally:
        _kept.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per record: a traceback stays inside the record it belongs to, and the
    line stays under `_MAX_LINE`."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        out: dict[str, str | int] = {
            "ts": stamp.replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        fields = {k: redact(v) if isinstance(v, str) else v for k, v in out.items()}
        line = json.dumps(fields)
        # A long message goes down to a quarter of the line first: the worker puts an exception's
        # text in the message too, and the traceback must keep its frames.
        msg, over = fields["msg"], len(line) - _MAX_LINE
        if over > 0 and isinstance(msg, str) and (width := _width(msg)) > _MAX_LINE // 4:
            fields["msg"] = _cut(msg, max(width - over, _MAX_LINE // 4))
            line = json.dumps(fields)
        for key in ("exc", "stack", "msg"):
            over = len(line) - _MAX_LINE
            if over <= 0:
                break
            value = fields.get(key)
            if isinstance(value, str):
                fields[key] = _cut(value, _width(value) - over)
                line = json.dumps(fields)
        return line


class TextFormatter(logging.Formatter):
    """The format the services used before decision 81, with the bound fields after the message."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        fields = _fields(record)
        if fields:
            line += " [" + " ".join(f"{k}={v}" for k, v in fields.items()) + "]"
        return line

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _PathOnly(logging.Filter):
    """uvicorn's access and WebSocket lines pass the request's path as an argument, query string
    included, and a signed page link carries its signature there (decision 58)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.split("?", 1)[0] if isinstance(arg, str) and arg.startswith("/") else arg
                for arg in record.args
            )
        return True


_PATH_ONLY: Final = _PathOnly()


def _log_uncaught(
    kind: type[BaseException], value: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, tb)
        return
    logging.getLogger(__name__).critical(
        "uncaught exception; the service stops", exc_info=(kind, value, tb)
    )


def configure_logging(service: str) -> None:
    """Replace the root logger's handlers with one on stderr, and route uvicorn's loggers to it.
    An exception nothing catches and a Python warning become records on it too.

    An unknown format refuses to start, as an unknown level always has: a service that logs in a
    shape nobody queries for looks healthy and is not.
    """
    style = os.getenv(FORMAT_ENV, "json").lower()
    if style == "json":
        formatter: logging.Formatter = JsonFormatter(service)
    elif style == "text":
        formatter = TextFormatter()
    else:
        raise ValueError(f"{FORMAT_ENV} must be json or text, not {style!r}")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    level = os.getenv(LEVEL_ENV, "INFO").upper()
    logging.basicConfig(level=level, handlers=[handler], force=True)
    if level != "DEBUG":
        # One INFO line per GitHub call outnumbered every other line and buried the warnings.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
    sys.excepthook = _log_uncaught
    logging.captureWarnings(True)

    # uvicorn's CLI gives these handlers of its own and stops them propagating; the terminal's
    # `log_config=None` leaves them bare. Either way they now inherit the root's level and handler.
    for name in _UVICORN:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(_PATH_ONLY)
