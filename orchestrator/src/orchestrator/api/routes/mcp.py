"""What the operator MCP server at `/mcp/` offers, for the dashboard's MCP access page.

The catalogue is read from the mounted server, never written out here, so the page cannot list a
tool the server does not have.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from orchestrator.api.access import SIGNED_IN, needs
from orchestrator.api.schemas import McpToolCatalog, McpToolOut
from orchestrator.mcp_server.registry import catalogue

router = APIRouter(tags=["mcp"])


@router.get("/mcp/tools", response_model=McpToolCatalog)
@needs(SIGNED_IN)
async def list_mcp_tools(request: Request) -> McpToolCatalog:
    server = getattr(request.app.state, "mcp_server", None)
    if server is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The MCP server is not mounted on this API."
        )
    return McpToolCatalog(
        tools=[McpToolOut.model_validate(entry) for entry in await catalogue(server)]
    )
