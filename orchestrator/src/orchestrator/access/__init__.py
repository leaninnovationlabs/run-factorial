"""Accounts, the permission catalogue and the one access decision (design.md §7.19,
tech-stack.md decision 80). The API, the MCP server and the assistant call this package and
none decides on its own; it imports no FastAPI."""

from __future__ import annotations

from orchestrator.access.permissions import (
    ADMIN,
    BUILT_IN_ROLES,
    GATED_READ_TOOLS,
    MEMBER,
    OWNER,
    WRITE_TOOLS,
    Permission,
)

__all__ = [
    "ADMIN",
    "BUILT_IN_ROLES",
    "GATED_READ_TOOLS",
    "MEMBER",
    "OWNER",
    "WRITE_TOOLS",
    "Permission",
]
