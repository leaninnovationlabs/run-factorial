"""A logger's records as the service would write them, parsed back: what a test reads to check
the ids a line carries (tech-stack decision 81). It stands in for the process's root handler, so
nothing configures the test runner's logging."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from orchestrator.logs import JsonFormatter


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter("test"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@contextmanager
def probing(name: str) -> Iterator[_Lines]:
    """Collect `name`'s records at INFO and above, and only here: the logger stops propagating
    for the block, and gets its handlers, level and propagation back after it."""
    logger = logging.getLogger(name)
    handler = _Lines()
    propagate, level = logger.propagate, logger.level
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
