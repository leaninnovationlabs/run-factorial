"""Tests for codex_cli's RunSpec shaping: the prompt document, the per-run `config.toml`,
and the spawn-time refusals of specs this substrate cannot honour."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr

from runtime.adapters.codex_cli.environment import _credential_free_environment
from runtime.adapters.codex_cli.spec import (
    _reject_model_alias,
    _reject_unverifiable_expectations,
    bearer_env_var,
    build_config_toml,
    build_prompt,
)
from runtime.contract import (
    AgentPrompts,
    ArtifactKind,
    ExpectedArtifact,
    McpServerSpec,
    McpTransport,
    RunAttachment,
    RunInput,
    RunSpec,
    ShownFile,
    SpawnError,
    SpawnReason,
    Workspace,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
ORIGIN = "https://github.com/user-attachments/assets/1b92ad15-13e5-4ffe-ac3a-b0c19487acfa"


def attachment(name: str = "image-1", content: bytes = PNG) -> RunAttachment:
    return RunAttachment(
        name=name,
        media_type="image/png",
        content=content,
        source="ticket_body",
        origin=ORIGIN,
        sha256=hashlib.sha256(content).hexdigest(),
    )


TOKEN = "ghs_verysecrettoken"
API_KEY = "sk-openai-alsosecret"
DOCS_TOKEN = "docs-token"
INDEX_TOKEN = "index-token"

MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
)
MCP_STDIO = McpServerSpec(
    name="repo-index",
    transport=McpTransport.STDIO,
    command="npx",
    args=("-y", "@acme/repo-index-mcp@1.4.2"),
    env={"REPO_INDEX_TOKEN": "{secrets.REPO_INDEX_TOKEN}"},
)

ROOT = Path("/tmp/factory-runs")
WORKTREE = ROOT / "runs" / "1-agent-0" / "worktree"
SECRETS = {
    "GH_TOKEN": TOKEN,
    "OPENAI_API_KEY": API_KEY,
    "PRODUCT_DOCS_TOKEN": DOCS_TOKEN,
    "REPO_INDEX_TOKEN": INDEX_TOKEN,
}


def make_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "inputs": (RunInput(name="ticket_body", value="Add a widget endpoint."),),
        "secrets": {key: SecretStr(value) for key, value in SECRETS.items()},
        "model": "gpt-5.1-codex",
        "max_turns": 40,
    }
    return RunSpec.model_validate(defaults | overrides)


# --------------------------------------------------------------------------- prompt


def test_the_prompt_carries_workspace_instructions_and_fenced_inputs() -> None:
    prompt = build_prompt(make_spec())
    assert "- repo: acme/widgets" in prompt
    assert "- branch to push: spec/12-{slug}" in prompt
    assert "Write a spec." in prompt
    assert "## ticket_body" in prompt
    assert "```\nAdd a widget endpoint.\n```" in prompt


def test_the_prompt_names_the_pinned_config_commit_only_when_there_is_one() -> None:
    sha = "c" * 40
    workspace = Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}", config_sha=sha)
    assert f"git show {sha}:.factorial/" in build_prompt(make_spec(workspace=workspace))
    assert "- config:" not in build_prompt(make_spec())


def test_the_prompt_tells_the_run_which_files_the_dashboard_shows() -> None:
    shown = (ShownFile(path="specs/12-{slug}/summary.html", label="Summary"),)
    prompt = build_prompt(make_spec(shown_files=shown))
    assert "- `specs/12-{slug}/summary.html` (Summary)" in prompt
    assert prompt.index("# Workspace") < prompt.index("# Files") < prompt.index("# Instructions")
    assert "# Files" not in build_prompt(make_spec())


# --------------------------------------------------------------------------- config.toml


def test_a_stdio_server_maps_whole_into_config_toml() -> None:
    text, extra_env = build_config_toml(
        (MCP_STDIO,), SECRETS, writable_root=ROOT, worktree=WORKTREE
    )
    parsed = tomllib.loads(text)
    server = parsed["mcp_servers"]["repo-index"]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@acme/repo-index-mcp@1.4.2"]
    assert server["env"] == {"REPO_INDEX_TOKEN": INDEX_TOKEN}
    assert extra_env == {}


def test_a_value_beyond_the_bmp_or_holding_del_still_parses() -> None:
    value = "token 😀 \x7f é \t end"
    text, _ = build_config_toml(
        (MCP_STDIO,),
        {**SECRETS, "REPO_INDEX_TOKEN": value},
        writable_root=ROOT,
        worktree=WORKTREE,
    )
    assert tomllib.loads(text)["mcp_servers"]["repo-index"]["env"] == {"REPO_INDEX_TOKEN": value}


def test_an_http_bearer_header_becomes_env_var_indirection() -> None:
    """codex's streamable-HTTP auth surface is `bearer_token_env_var` (0.146.0) — the token
    travels through the environment, never through the TOML, and the rendered header value
    loses its `Bearer ` prefix on the way."""
    text, extra_env = build_config_toml((MCP_HTTP,), SECRETS, writable_root=ROOT, worktree=WORKTREE)
    parsed = tomllib.loads(text)
    server = parsed["mcp_servers"]["product-docs"]
    assert server["url"] == MCP_HTTP.url
    var = server["bearer_token_env_var"]
    assert var == bearer_env_var("product-docs")
    assert extra_env == {var: DOCS_TOKEN}
    assert DOCS_TOKEN not in text


def test_a_non_bearer_header_is_refused_not_dropped() -> None:
    """Honour or refuse, never ignore: 0.146.0 offers no arbitrary-header surface, and a server
    the agent reaches without the header its config promised fails as the wrong thing."""
    spec = make_spec(
        mcp=(
            McpServerSpec(
                name="product-docs",
                transport=McpTransport.HTTP,
                url="https://mcp.example.test/mcp",
                headers={"X-Api-Key": "{secrets.PRODUCT_DOCS_TOKEN}"},
            ),
        )
    )
    with pytest.raises(SpawnError) as excinfo:
        build_config_toml(spec.mcp, SECRETS, writable_root=ROOT, worktree=WORKTREE)
    assert excinfo.value.reason is SpawnReason.CONFIG
    assert "X-Api-Key" in str(excinfo.value)


def test_an_sse_server_is_refused() -> None:
    spec = make_spec(
        mcp=(
            McpServerSpec(
                name="legacy-feed",
                transport=McpTransport.SSE,
                url="https://mcp.example.test/sse",
            ),
        )
    )
    with pytest.raises(SpawnError) as excinfo:
        build_config_toml(spec.mcp, SECRETS, writable_root=ROOT, worktree=WORKTREE)
    assert excinfo.value.reason is SpawnReason.CONFIG


def test_a_missing_mcp_secret_refuses_before_any_file_exists() -> None:
    with pytest.raises(SpawnError) as excinfo:
        build_config_toml((MCP_HTTP,), {"GH_TOKEN": TOKEN}, writable_root=ROOT, worktree=WORKTREE)
    assert excinfo.value.reason is SpawnReason.CREDENTIAL


# --------------------------------------------------------------------------- refusals


def test_a_model_size_alias_is_refused() -> None:
    with pytest.raises(SpawnError) as excinfo:
        _reject_model_alias("large")
    assert excinfo.value.reason is SpawnReason.CONFIG


def test_an_unverifiable_expectation_is_refused_naming_the_kind() -> None:
    spec = make_spec(expected_artifacts=(ExpectedArtifact(kind=ArtifactKind.COMMIT),))
    with pytest.raises(SpawnError) as excinfo:
        _reject_unverifiable_expectations(spec)
    assert excinfo.value.reason is SpawnReason.CONFIG
    assert "commit" in str(excinfo.value)


# --------------------------------------------------------------------------- child environment


def test_a_credential_free_environment_keeps_codex_auth_and_drops_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The blind lane's shape: the host keeps the token for its own clone and probes; the agent
    process gets `OPENAI_API_KEY` (codex must still run) and no GitHub-shaped variable at all."""
    monkeypatch.setenv("GH_TOKEN", "host-exported-must-not-leak")
    from pathlib import Path

    config_dir = Path(str(tmp_path)) / "config"
    child = _credential_free_environment(SECRETS, config_dir)
    assert child["OPENAI_API_KEY"] == API_KEY
    assert child["CODEX_HOME"] == str(config_dir)
    assert "GH_TOKEN" not in child
    assert "GITHUB_TOKEN" not in child
    assert not any(key.startswith("GIT_CONFIG") for key in child)


def test_the_prompt_names_where_each_image_was_saved() -> None:
    prompt = build_prompt(make_spec(attachments=(attachment(),)), {"image-1": "/r/a/image-1.png"})
    assert f"- image-1 (ticket_body, {ORIGIN}): /r/a/image-1.png" in prompt


def test_bedrock_provider_is_omitted_unless_asked() -> None:
    text, _ = build_config_toml((), {}, writable_root=ROOT, worktree=WORKTREE)
    parsed = tomllib.loads(text)
    assert "model_provider" not in parsed
    assert "model_providers" not in parsed
    assert "web_search" not in parsed


def test_bedrock_provider_block_is_the_govcloud_responses_endpoint() -> None:
    from runtime.adapters.codex_cli.auth import bedrock_provider_config

    text, _ = build_config_toml(
        (),
        {},
        writable_root=ROOT,
        worktree=WORKTREE,
        bedrock=bedrock_provider_config("us-gov-west-1"),
    )
    parsed = tomllib.loads(text)
    assert parsed["model_provider"] == "bedrock-grok"
    provider = parsed["model_providers"]["bedrock-grok"]
    assert provider["base_url"] == (
        "https://bedrock-runtime.us-gov-west-1.amazonaws.com/openai/v1"
    )
    assert provider["env_key"] == "AWS_BEARER_TOKEN_BEDROCK"
    assert provider["wire_api"] == "responses"
    assert provider["supports_websockets"] is False
    assert parsed["web_search"] == "disabled"


def test_without_host_iam_drops_instance_role_variables() -> None:
    from runtime.adapters.codex_cli.environment import without_host_iam

    cleaned = without_host_iam(
        {
            "PATH": "/usr/bin",
            "AWS_ACCESS_KEY_ID": "ASIAEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "token",
            "AWS_PROFILE": "default",
            "AWS_BEARER_TOKEN_BEDROCK": "keep-me",
        }
    )
    assert cleaned == {"PATH": "/usr/bin", "AWS_BEARER_TOKEN_BEDROCK": "keep-me"}


def test_require_bedrock_bearer_refuses_a_missing_mint() -> None:
    from runtime.adapters.codex_cli.auth import require_bedrock_bearer
    from runtime.contract import SpawnError, SpawnReason

    with pytest.raises(SpawnError) as excinfo:
        require_bedrock_bearer({})
    assert excinfo.value.reason is SpawnReason.CREDENTIAL
    assert require_bedrock_bearer({"AWS_BEARER_TOKEN_BEDROCK": "bedrock-api-key-x"}) == (
        "bedrock-api-key-x"
    )


def test_bedrock_provider_block_uses_the_given_region() -> None:
    from runtime.adapters.codex_cli.auth import bedrock_provider_config

    text, _ = build_config_toml(
        (),
        {},
        writable_root=ROOT,
        worktree=WORKTREE,
        bedrock=bedrock_provider_config("us-east-1"),
    )
    parsed = tomllib.loads(text)
    assert parsed["model_providers"]["bedrock-grok"]["base_url"] == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    )
    assert parsed["model_providers"]["bedrock-grok"]["name"] == "Bedrock Grok 4.6"
