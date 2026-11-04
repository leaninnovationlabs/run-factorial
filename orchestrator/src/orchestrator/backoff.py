"""A forge connection that waits instead of failing: GitHub's rate limit, GitLab's host lookup."""

from __future__ import annotations

__all__ = ["ConnectionBackoff"]


class ConnectionBackoff(Exception):
    """A connection waits out a limit or a transient fault. Not the ticket's fault: the worker
    holds the connection's projects and counts no failure, and the API answers 503.

    Never a `RuntimeError`: the worker's `_PERMANENT_CONFIG_ERRORS` carries that bare, and a
    backoff mid-tick would fail the attempt as a misconfiguration and escalate it."""
