"""Every tool the server registers. Each module's `TOOLS` joins `ALL_TOOLS` as it lands."""

from __future__ import annotations

from orchestrator.mcp_server.registry import ToolSpec
from orchestrator.mcp_server.tools import (
    config,
    controls,
    dashboard,
    metrics,
    projects,
    runs,
    tickets,
)

__all__ = ["ALL_TOOLS"]

ALL_TOOLS: list[ToolSpec] = [
    *projects.TOOLS,
    *tickets.TOOLS,
    *metrics.TOOLS,
    *dashboard.TOOLS,
    *runs.TOOLS,
    *controls.TOOLS,
    *config.TOOLS,
]
