"""The operator MCP server at `/mcp/` (tech-stack decision 60, design.md §7.16).

No re-exports: the mint route imports `tokens`, and a re-export of the app would pull fastmcp
into every import of this package.
"""

from __future__ import annotations
