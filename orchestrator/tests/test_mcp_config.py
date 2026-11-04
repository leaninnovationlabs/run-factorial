"""MCP server config: the manifest format, the loader, and the operator allowlist (design.md §7.5).

No database and no network — YAML text, Pydantic, and pure policy functions. The sample under
`docs/samples/factorial-simple/` is the format reference here too, so "the sample still loads"
is what
keeps the models and the documented format from drifting apart.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from orchestrator.config import ConfigError, load_agent, load_config_folder, parse_mcp_server
from orchestrator.factorial import (
    McpAllowEntry,
    McpAllowlistInvalid,
    check_allowed,
    parse_allowlist,
)
from orchestrator.factorial.mcp_policy import own_endpoints
from orchestrator.factorial.types import ResolvedMcpServer
from orchestrator.settings import AuthSettings, Settings
from tests.paths import SAMPLE

HTTP_SERVER = """
specVersion: adlc/v1
kind: McpServer
name: product-docs
transport: http
http:
  url: https://mcp.internal.example.com/product-docs/mcp
  headers:
    Authorization: "Bearer {secrets.PRODUCT_DOCS_TOKEN}"
secrets:
  - key: PRODUCT_DOCS_TOKEN
tools:
  allow: [search_docs, get_doc]
"""

STDIO_SERVER = """
specVersion: adlc/v1
kind: McpServer
name: repo-index
transport: stdio
stdio:
  command: npx
  args: ["-y", "@acme/repo-index-mcp@1.4.2"]
  env:
    REPO_INDEX_TOKEN: "{secrets.REPO_INDEX_TOKEN}"
secrets:
  - key: REPO_INDEX_TOKEN
"""


def _server(text: str, *, name: str) -> Any:
    return parse_mcp_server(textwrap.dedent(text), name=name, origin=f"test:{name}.yaml")


def _resolved(text: str, *, name: str, scope: str = "shared") -> ResolvedMcpServer:
    return ResolvedMcpServer(name=name, manifest=_server(text, name=name), scope=scope)


# --- the sample ---------------------------------------------------------------------------------


def test_the_sample_folder_loads_its_mcp_catalog() -> None:
    config = load_config_folder(SAMPLE)
    assert set(config.mcp_servers) == {"product-docs"}
    assert config.agents["plan-agent"].mcp.shared == ("product-docs",)
    assert config.agents["build-agent"].mcp.shared == ()  # not everyone gets tools


def test_the_sample_server_declares_its_secret_by_reference_only() -> None:
    server = load_config_folder(SAMPLE).mcp_servers["product-docs"]
    assert server.secret_refs() == ("PRODUCT_DOCS_TOKEN",)
    # The reference, not the value: `.factorial/` never holds a credential.
    assert server.http is not None
    assert "{secrets.PRODUCT_DOCS_TOKEN}" in server.http.headers["Authorization"]


# --- manifest validation ------------------------------------------------------------------------


def test_a_plaintext_url_is_refused() -> None:
    with pytest.raises(ConfigError) as exc:
        _server(HTTP_SERVER.replace("https://", "http://"), name="product-docs")
    assert "https" in str(exc.value)


def test_a_floating_stdio_version_is_refused() -> None:
    """A floating tag changes the code running beside a live GH_TOKEN without changing any pin."""
    with pytest.raises(ConfigError) as exc:
        _server(STDIO_SERVER.replace("@1.4.2", "@latest"), name="repo-index")
    assert "floating version" in str(exc.value)


def test_a_pinned_stdio_version_is_accepted() -> None:
    server = _server(STDIO_SERVER, name="repo-index")
    assert server.stdio is not None
    assert server.stdio.args == ("-y", "@acme/repo-index-mcp@1.4.2")
    assert server.secret_refs() == ("REPO_INDEX_TOKEN",)


def test_an_undeclared_secret_reference_is_refused() -> None:
    """The declaration list is what the orchestrator resolves; an undeclared key would reach the
    substrate as a literal `{secrets.X}`."""
    with pytest.raises(ConfigError) as exc:
        _server(
            HTTP_SERVER.replace("secrets:\n  - key: PRODUCT_DOCS_TOKEN\n", ""),
            name="product-docs",
        )
    assert "PRODUCT_DOCS_TOKEN" in str(exc.value)


def test_the_transport_and_its_block_must_agree() -> None:
    with pytest.raises(ConfigError) as exc:
        _server(HTTP_SERVER.replace("transport: http", "transport: stdio"), name="product-docs")
    assert "stdio" in str(exc.value)


def test_a_name_that_cannot_be_a_tool_prefix_is_refused() -> None:
    with pytest.raises(ConfigError) as exc:
        _server(
            HTTP_SERVER.replace("name: product-docs", "name: Product_Docs"), name="Product_Docs"
        )
    assert "tool prefix" in str(exc.value)


def test_the_file_stem_must_match_the_name() -> None:
    with pytest.raises(ConfigError) as exc:
        _server(HTTP_SERVER, name="docs")
    assert "docs.yaml" in str(exc.value)


# --- the loader ---------------------------------------------------------------------------------


def _copy_sample(tmp_path: Path) -> Path:
    root = tmp_path / ".adlc"
    for path in sorted(SAMPLE.rglob("*")):
        if path.is_file():
            target = root / path.relative_to(SAMPLE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_a_shared_server_with_no_file_is_a_config_error(tmp_path: Path) -> None:
    root = _copy_sample(tmp_path)
    (root / "mcp" / "product-docs.yaml").unlink()

    with pytest.raises(ConfigError) as exc:
        load_config_folder(root)
    assert "product-docs" in str(exc.value)


def test_a_local_server_is_parsed_not_merely_counted(tmp_path: Path) -> None:
    """An unparseable server belongs to whoever is reading the manifest, not to a spawn later."""
    root = _copy_sample(tmp_path)
    agent = root / "agents" / "build-agent"
    (agent / "mcp").mkdir()
    (agent / "mcp" / "repo-index.yaml").write_text(
        textwrap.dedent(STDIO_SERVER).replace("@1.4.2", "@latest"), encoding="utf-8"
    )
    manifest = (agent / "agent.yaml").read_text(encoding="utf-8")
    (agent / "agent.yaml").write_text(manifest + "\nmcp:\n  local:\n    - repo-index\n", "utf-8")

    with pytest.raises(ConfigError) as exc:
        load_agent(agent / "agent.yaml")
    assert "floating version" in str(exc.value)


def test_one_name_cannot_be_claimed_by_both_scopes(tmp_path: Path) -> None:
    root = _copy_sample(tmp_path)
    agent = root / "agents" / "plan-agent"
    (agent / "mcp").mkdir()
    (agent / "mcp" / "product-docs.yaml").write_text(textwrap.dedent(HTTP_SERVER), encoding="utf-8")
    manifest = (agent / "agent.yaml").read_text(encoding="utf-8")
    (agent / "agent.yaml").write_text(
        manifest.replace("mcp:\n  shared:", "mcp:\n  local:\n    - product-docs\n  shared:"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        load_agent(agent / "agent.yaml")
    assert "unique across scopes" in str(exc.value)


# --- the operator allowlist ---------------------------------------------------------------------

ALLOWED_HTTP: dict[str, Any] = {
    "name": "product-docs",
    "transport": "http",
    "url": "https://mcp.internal.example.com/product-docs/mcp",
    "tools": ["search_docs", "get_doc"],
}
ALLOWED_STDIO: dict[str, Any] = {
    "name": "repo-index",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@acme/repo-index-mcp@1.4.2"],
}


def test_an_absent_allowlist_is_empty_not_permissive() -> None:
    assert parse_allowlist({}) == ()
    assert parse_allowlist({"agent_runtime": "claude"}) == ()


def test_a_malformed_allowlist_raises_rather_than_degrading() -> None:
    with pytest.raises(McpAllowlistInvalid):
        parse_allowlist({"mcp_allowlist": {"name": "product-docs"}})
    with pytest.raises(McpAllowlistInvalid) as exc:
        parse_allowlist({"mcp_allowlist": [{"name": "product-docs", "transport": "http"}]})
    assert "[0]" in str(exc.value)  # says which entry


def test_a_matching_entry_permits_the_server() -> None:
    allowlist = parse_allowlist({"mcp_allowlist": [ALLOWED_HTTP]})
    assert check_allowed([_resolved(HTTP_SERVER, name="product-docs")], allowlist) == []


def test_an_unnamed_server_is_refused() -> None:
    assert check_allowed([_resolved(HTTP_SERVER, name="product-docs")], []) == [
        "MCP server 'product-docs' is not in this project's mcp_allowlist; permitted: (none)"
    ]


def test_url_matching_ignores_only_case_and_a_trailing_slash() -> None:
    entry = McpAllowEntry.model_validate(
        {**ALLOWED_HTTP, "url": "https://MCP.Internal.Example.com/product-docs/mcp/"}
    )
    assert check_allowed([_resolved(HTTP_SERVER, name="product-docs")], [entry]) == []

    # The path is data, not decoration: a different team's endpoint is a different server.
    other = McpAllowEntry.model_validate(
        {**ALLOWED_HTTP, "url": "https://mcp.internal.example.com/PRODUCT-DOCS/mcp"}
    )
    assert check_allowed([_resolved(HTTP_SERVER, name="product-docs")], [other])


def test_stdio_command_and_args_must_match_exactly() -> None:
    entry = McpAllowEntry.model_validate(
        {**ALLOWED_STDIO, "args": ["-y", "@acme/repo-index-mcp@1.4.3"]}
    )
    problems = check_allowed([_resolved(STDIO_SERVER, name="repo-index")], [entry])
    assert problems and "1.4.2" in problems[0]


def test_a_transport_swap_is_refused_without_comparing_unrelated_fields() -> None:
    entry = McpAllowEntry.model_validate(ALLOWED_STDIO | {"name": "product-docs"})
    problems = check_allowed([_resolved(HTTP_SERVER, name="product-docs")], [entry])
    assert len(problems) == 1 and "transport" in problems[0]


@pytest.mark.parametrize(
    ("allowed", "requested", "permitted"),
    [
        (["search_*"], ["search_docs"], True),
        (["search_*"], ["search_*"], True),
        (["*"], ["search_docs"], True),
        (["search_*"], ["get_doc"], False),
        # Nothing covers "everything" except an entry that says so: a server asking for every tool
        # (empty `allow`) against a narrowed entry must be refused, not quietly narrowed.
        (["search_*"], [], False),
        (["*"], [], True),
    ],
)
def test_tool_pattern_coverage(allowed: list[str], requested: list[str], permitted: bool) -> None:
    text = HTTP_SERVER.replace(
        "tools:\n  allow: [search_docs, get_doc]",
        f"tools:\n  allow: {requested}" if requested else "",
    )
    entry = McpAllowEntry.model_validate({**ALLOWED_HTTP, "tools": allowed})
    assert (check_allowed([_resolved(text, name="product-docs")], [entry]) == []) is permitted


OWN_URL = "https://app.example.com/mcp/"


def _pointing_at(url: str) -> ResolvedMcpServer:
    text = HTTP_SERVER.replace("https://mcp.internal.example.com/product-docs/mcp", url)
    return _resolved(text, name="product-docs")


def _refused(url: str, own: frozenset[tuple[str, int]]) -> bool:
    entry = McpAllowEntry.model_validate({**ALLOWED_HTTP, "url": url})
    problems = check_allowed([_pointing_at(url)], [entry], own_endpoints=own)
    assert problems == [] or (len(problems) == 1 and "Factorial's own MCP server" in problems[0])
    return bool(problems)


def test_factorial_s_own_mcp_server_is_refused_even_when_allowlisted() -> None:
    own = own_endpoints(OWN_URL, "https://app.example.com")
    entry = McpAllowEntry.model_validate({**ALLOWED_HTTP, "url": OWN_URL})
    assert check_allowed([_pointing_at(OWN_URL)], [entry]) == []  # the allowlist alone permits it

    assert _refused(OWN_URL, own)
    # Host and port, not `_same_url`: a case, explicit-port or path variant is the same server.
    assert _refused("https://APP.Example.com:443/elsewhere/mcp", own)
    assert _refused("https://app.example.com./mcp", own)


@pytest.mark.parametrize(
    "url",
    [
        "https://app%2Eexample.com/mcp",
        "https://app.example.com\\@evil.com/mcp",
        "https://\uff41pp.example.com/mcp",
        "https://app.example.com%2e/mcp",
        "https://2130706433/mcp",
        "https://app.example.com:bad/mcp",
    ],
)
def test_a_url_the_client_may_read_differently_is_refused_as_the_factory(url: str) -> None:
    assert _refused(url, own_endpoints(OWN_URL))


def test_an_agent_server_on_another_port_of_a_local_host_is_not_the_factory() -> None:
    own = own_endpoints("http://localhost:8000/mcp/", "")
    # A manifest URL must be https, so the scheme differs from local dev's http: the port decides.
    assert _refused("https://localhost:8000/mcp", own)
    assert not _refused("https://localhost:9000/mcp", own)


def test_the_mcp_url_is_refused_as_the_factory() -> None:
    """The worker's spawn check reads this deployment's MCP URL from the same setting the API
    serves on."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  (see test_settings.py)
        auth=AuthSettings(external_url="https://app.runfactorial.com"),
    )
    assert settings.mcp_url == "https://app.runfactorial.com/factorial/mcp/"
    assert _refused(settings.mcp_url, settings.mcp_endpoints)
