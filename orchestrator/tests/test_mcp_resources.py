"""What the MCP server teaches a model must match what the orchestrator does."""

from __future__ import annotations

import pytest
from fastmcp import Client

from orchestrator.mcp_server.app import build_server
from orchestrator.mcp_server.teaching import (
    INSTRUCTIONS,
    PROCESS_YAML_URI,
    RESOURCES,
    SKILL_URI,
    VOCABULARY_URI,
)
from tests.api_harness import make_settings, mcp_deps_for
from tests.paths import REPO_ROOT


def test_process_yaml_resource_is_the_docs_copy() -> None:
    # `docs/` is not in the image, so the server serves a package-data copy; an edit to the doc
    # alone would teach models a stale format.
    served = (RESOURCES / "process-yaml.md").read_bytes()
    assert served == (REPO_ROOT / "docs" / "process-yaml.md").read_bytes(), (
        "cp docs/process-yaml.md orchestrator/src/orchestrator/mcp_server/resources/"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_instructions_fit_and_every_resource_they_name_resolves() -> None:
    # Claude Code cuts server instructions at 2,048 characters, and the pointers come last.
    assert len(INSTRUCTIONS) <= 2000
    server = build_server(mcp_deps_for(make_settings(), None))
    async with Client(server) as client:
        for uri in (SKILL_URI, PROCESS_YAML_URI, VOCABULARY_URI):
            assert uri in INSTRUCTIONS
            (content,) = await client.read_resource(uri)
            assert getattr(content, "text", ""), uri
