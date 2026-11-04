"""Tests for the `claude_cli` adapter's refusals and pure helpers.

No test needs the real `claude`, the real `git` or a real subprocess, except where a real child
is the point: the CLI seam (`_Cli`) answers only `run`. Every refusal here happens before
anything is created. A session's behaviour lives in `test_claude_cli_interactive.py`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from host_session_support import short_root
from pydantic import SecretStr

from runtime.adapters import create_adapter
from runtime.adapters._cli.environment import (
    ENV_GH_TOKEN,
    build_hidden_globs,
    classify_probe_failure,
)
from runtime.adapters._cli.paths import contained
from runtime.adapters._cli.process import CommandResult, host_environment
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter, build_prompt
from runtime.adapters.claude_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_SUBSCRIPTION,
    ENV_OAUTH_TOKEN,
    SECRET_OAUTH_TOKEN,
)
from runtime.adapters.claude_cli.cli import ENV_API_KEY, ENV_CONFIG_DIR, PINNED_VERSION, _Cli
from runtime.adapters.claude_cli.environment import _credential_free_environment
from runtime.contract import (
    AdapterUnavailable,
    AgentPrompts,
    AgentSkill,
    ArtifactKind,
    ExpectedArtifact,
    McpServerSpec,
    McpTransport,
    RunInput,
    RunSpec,
    RuntimeAdapter,
    ShownFile,
    SkillScope,
    SpawnError,
    SpawnReason,
    UnknownRunError,
    Workspace,
)

TOKEN = "ghs_verysecrettoken"
API_KEY = "sk-ant-alsosecret"
DOCS_TOKEN = "docs-token"

MCP_HTTP = McpServerSpec(
    name="product-docs",
    transport=McpTransport.HTTP,
    url="https://mcp.internal.example.com/product-docs/mcp",
    headers={"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
    tools_allow=("search_docs",),
)


def make_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "GH-12",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        "prompts": AgentPrompts(system="You are the plan agent.", instructions="Write a spec."),
        "skills": (
            AgentSkill(name="spec-format", content="# Spec format\n", scope=SkillScope.SHARED),
        ),
        "inputs": (RunInput(name="ticket_body", value="Add a widget endpoint."),),
        "secrets": {
            ENV_GH_TOKEN: SecretStr(TOKEN),
            ENV_API_KEY: SecretStr(API_KEY),
            # Resolved by the orchestrator because an MCP server referenced it; harmless when
            # no server is declared (the common case).
            "PRODUCT_DOCS_TOKEN": SecretStr(DOCS_TOKEN),
        },
        "model": "claude-haiku-4-5",
        "max_turns": 40,
    }
    return RunSpec.model_validate(defaults | overrides)


def _is_empty(path: Path) -> bool:
    return not list(path.iterdir())


class FakeCli(_Cli):
    """The CLI seam with no subprocess behind it: every command succeeds with no output."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        return CommandResult(argv=(program, *args), returncode=0, stdout="", stderr="")


# --------------------------------------------------------------------------- refusals


async def test_no_local_root_refuses_to_spawn(tmp_path: Path) -> None:
    """The guard that makes the blast radius knowable. No default, ever."""
    adapter = ClaudeCliAdapter(cli=FakeCli())
    with pytest.raises(AdapterUnavailable, match="LOCAL_ROOT"):
        await adapter.spawn(make_spec())
    assert _is_empty(tmp_path)


async def test_direct_escape_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(SpawnError, match="escapes local_root"):
        contained(root, root / ".." / "elsewhere")


async def test_model_alias_is_refused_before_anything_launches(tmp_path: Path) -> None:
    cli = FakeCli()
    adapter = ClaudeCliAdapter(local_root=tmp_path, cli=cli)
    with pytest.raises(SpawnError, match="size alias"):
        await adapter.spawn(make_spec(model="large"))
    assert not (tmp_path / "runs").exists()


async def test_missing_api_key_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subscription session would run but report no cost, which is half of the point."""
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    cli = FakeCli()
    adapter = ClaudeCliAdapter(local_root=tmp_path, cli=cli)
    spec = make_spec(secrets={ENV_GH_TOKEN: SecretStr(TOKEN)})
    with pytest.raises(SpawnError, match=ENV_API_KEY):
        await adapter.spawn(spec)
    assert not (tmp_path / "runs").exists()


async def test_a_host_env_key_does_not_stand_in_for_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is the only source. A key exported into the worker's environment must not
    satisfy the guard, or a project whose secret table is empty still runs — and the store is
    authoritative in name only, which is what let the resume path read os.environ unnoticed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-host")
    adapter = ClaudeCliAdapter(local_root=tmp_path / "root", cli=FakeCli())

    with pytest.raises(SpawnError):
        await adapter.spawn(make_spec(secrets={}))


async def test_an_unstorable_mcp_secret_refuses_the_spawn(tmp_path: Path) -> None:
    """An `Authorization: Bearer {secrets.X}` header going out unrendered is worse than none."""
    cli = FakeCli()
    adapter = ClaudeCliAdapter(local_root=tmp_path, cli=cli)
    spec = make_spec(
        mcp=(MCP_HTTP,),
        secrets={ENV_GH_TOKEN: SecretStr(TOKEN), ENV_API_KEY: SecretStr(API_KEY)},
    )
    with pytest.raises(SpawnError, match="PRODUCT_DOCS_TOKEN"):
        await adapter.spawn(spec)
    assert _is_empty(tmp_path)  # refused before anything was created


def _expect(kind: ArtifactKind, **selector: str) -> ExpectedArtifact:
    return ExpectedArtifact(kind=kind, selector=selector)


@pytest.mark.parametrize("kind", [ArtifactKind.COMMIT, ArtifactKind.FILE])
async def test_an_unverifiable_expectation_kind_is_refused_at_spawn(
    tmp_path: Path, kind: ArtifactKind
) -> None:
    """Honour or refuse, never ignore. This adapter checks branches, pull requests and comments;
    a worktree it can read is not evidence that anything was delivered, so COMMIT and FILE are
    refused by name rather than dropped."""
    cli = FakeCli()
    adapter = ClaudeCliAdapter(local_root=tmp_path, cli=cli)
    with pytest.raises(SpawnError, match=kind.value):
        await adapter.spawn(make_spec(expected_artifacts=(_expect(kind),)))
    assert not (tmp_path / "runs").exists()


async def test_unknown_run_id_raises(tmp_path: Path) -> None:
    adapter = ClaudeCliAdapter(local_root=tmp_path, cli=FakeCli())
    with pytest.raises(UnknownRunError):
        await adapter.status("no-such-run")
    with pytest.raises(UnknownRunError):
        await adapter.result("no-such-run")


# --------------------------------------------------------------------------- subscription auth


def test_the_token_is_one_name_on_both_sides() -> None:
    """What the Runtime page stores under `credential_name_for("claude_subscription")` is the
    variable the CLI reads: one string, so the store key can never drift from the env var."""
    assert SECRET_OAUTH_TOKEN == ENV_OAUTH_TOKEN == "CLAUDE_CODE_OAUTH_TOKEN"
    assert AUTH_MODE_API_KEY == "api_key" and AUTH_MODE_SUBSCRIPTION == "subscription"


async def test_a_subscription_spawn_refuses_without_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STORE, and only the store. A host-exported token and a stored ANTHROPIC_API_KEY are
    both present here and neither counts: the first is claude_cli's own history (a host
    fallback hid an empty secret table), the second is the other mode's credential. The message
    names the Runtime page and `claude setup-token`, because that is the whole fix."""
    monkeypatch.setenv(ENV_OAUTH_TOKEN, "sk-ant-oat01-from-the-host")
    cli = FakeCli()
    adapter = ClaudeCliAdapter(
        local_root=tmp_path / "root", cli=cli, auth_mode=AUTH_MODE_SUBSCRIPTION
    )
    with pytest.raises(SpawnError) as excinfo:
        await adapter.spawn(make_spec())  # carries ANTHROPIC_API_KEY, no token
    assert excinfo.value.reason is SpawnReason.CREDENTIAL
    assert SECRET_OAUTH_TOKEN in str(excinfo.value)
    assert "Runtime page" in str(excinfo.value)
    assert "claude setup-token" in str(excinfo.value)
    assert not (tmp_path / "root" / "runs").exists()


# --------------------------------------------------------------------------- the child environment


async def test_credential_free_environment_is_an_allowlist_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`credential='none'` (contract.Workspace): the child environment is CONSTRUCTED — the
    allowlist from the host, the run's non-GitHub secrets, the config dir — never a filtered
    copy of anything. An unlisted host variable therefore never arrives, however
    credential-shaped or not it happens to be."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/dev")
    monkeypatch.setenv(ENV_GH_TOKEN, "ghs_host_exported")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.https://github.com.helper")
    monkeypatch.setenv("SOME_UNRELATED_HOST_VAR", "x")
    secrets = {
        ENV_GH_TOKEN: TOKEN,
        "GITHUB_TOKEN": TOKEN,
        ENV_API_KEY: API_KEY,
        "PRODUCT_DOCS_TOKEN": DOCS_TOKEN,
    }
    child = _credential_free_environment(secrets, Path("/cfg"))
    assert child[ENV_API_KEY] == API_KEY
    assert child["PRODUCT_DOCS_TOKEN"] == DOCS_TOKEN  # a non-GitHub secret is the run's own
    assert child[ENV_CONFIG_DIR] == "/cfg"
    assert child["PATH"] == "/usr/bin" and child["HOME"] == "/home/dev"
    assert ENV_GH_TOKEN not in child and "GITHUB_TOKEN" not in child
    assert "GIT_CONFIG_KEY_0" not in child
    assert "SOME_UNRELATED_HOST_VAR" not in child  # allowlist, not a strip
    assert TOKEN not in child.values()


async def test_the_host_api_key_is_carried_when_the_spec_declares_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spawn's guard accepts the harness key from either the spec or the host env, so the
    constructed child env must honor the same contract — a guard-passing run must never launch
    a keyless child that dies on its first turn. The spec's own key still wins when both exist,
    and GitHub-shaped keys are never carried."""
    monkeypatch.setenv(ENV_API_KEY, "sk-ant-from-host")
    monkeypatch.setenv(ENV_GH_TOKEN, "ghs_host_token_never_carried")

    carried = _credential_free_environment({}, tmp_path / "config")
    assert carried[ENV_API_KEY] == "sk-ant-from-host"
    assert ENV_GH_TOKEN not in carried

    spec_wins = _credential_free_environment({ENV_API_KEY: "sk-ant-from-spec"}, tmp_path / "config")
    assert spec_wins[ENV_API_KEY] == "sk-ant-from-spec"


def test_an_agent_never_inherits_the_orchestrators_own_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's environment can carry the master key and the E2B key, wherever a shell or a
    supervisor exports `.env`; the adapter's own processes start from `host_environment()`, and a
    session's from `host_session_environment`, which keeps less."""
    monkeypatch.setenv("FACTORIAL_MASTER_KEY", "master-key-must-not-reach-an-agent")
    monkeypatch.setenv("FACTORIAL_RUNTIME__E2B__API_KEY", "e2b-key-must-not-reach-an-agent")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")

    env = host_environment()

    assert "FACTORIAL_MASTER_KEY" not in env
    assert "FACTORIAL_RUNTIME__E2B__API_KEY" not in env
    assert env["SOMETHING_ELSE"] == "kept"


# --------------------------------------------------------------------------- probes and globs


async def test_a_not_found_probe_splits_on_whether_a_token_was_presented() -> None:
    """`Repository not found` is GitHub's answer both for a missing repo and a private one the
    caller cannot see. With a token that is a permanent WORKSPACE fault (the repo the spec names
    is not reachable as configured); without one it is a CREDENTIAL fault. Deleting the branch
    makes both fall through to INFRASTRUCTURE and retry forever — the 404-spins-at-attempt-1
    shape."""
    not_found = CommandResult(
        argv=("git", "ls-remote"),
        returncode=128,
        stdout="",
        stderr="remote: Repository not found.\nfatal: repository not found",
    )
    with_token = classify_probe_failure("acme/widgets", not_found, has_token=True)
    assert with_token.reason is SpawnReason.WORKSPACE
    assert with_token.reason.is_permanent
    without = classify_probe_failure("acme/widgets", not_found, has_token=False)
    assert without.reason is SpawnReason.CREDENTIAL
    assert without.reason.is_permanent


async def test_the_probe_and_clone_env_never_allows_a_terminal_prompt() -> None:
    """`GIT_TERMINAL_PROMPT=0` is the hang-guard — without it a private repo under an inherited
    tty can block `spawn` indefinitely at the probe or the clone. Pinned here on the constructed
    git env rather than end-to-end, because a hang is exactly the failure shape a test cannot
    wait out."""
    env = {"GH_TOKEN": "x"}
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    # And the real constructed site agrees — read the source line, not a copy of it:
    import inspect

    from runtime.adapters._cli import checkout as _checkout_module

    source = inspect.getsource(_checkout_module)
    assert 'GIT_TERMINAL_PROMPT": "0"' in source


async def test_hidden_glob_translation() -> None:
    """Three globs per declared path entry (bare + `/**` + ref globs untouched), and the empty
    tuple falls back to issue #19's own shape."""
    assert build_hidden_globs(("acceptance/{slug}",), "12-x") == (
        "acceptance/12-x",
        "acceptance/12-x/**",
    )
    assert build_hidden_globs(("refs/*/tests/{slug}",), "12-x") == ("refs/*/tests/12-x",)
    assert build_hidden_globs((), "12-x") == (
        "acceptance/12-x",
        "acceptance/12-x/**",
        "refs/*/tests/12-x",
    )


# --------------------------------------------------------------------------- prompt and wiring


async def test_build_prompt_states_the_workspace_and_fences_untrusted_input() -> None:
    prompt = build_prompt(make_spec())
    assert "spec/12-{slug}" in prompt  # the agent cannot satisfy a branch it was never told
    assert "acme/widgets" in prompt
    assert "Write a spec." in prompt
    # An input is data, not instruction: it arrives fenced under its own heading.
    assert "## ticket_body\n```\nAdd a widget endpoint.\n```" in prompt


async def test_build_prompt_tells_the_run_which_files_the_dashboard_shows() -> None:
    shown = (ShownFile(path="specs/12-{slug}/summary.html", label="Summary"),)
    prompt = build_prompt(make_spec(shown_files=shown))
    assert "- `specs/12-{slug}/summary.html` (Summary)" in prompt
    assert "# Files" not in build_prompt(make_spec())


async def test_the_registry_builds_it_by_kind(tmp_path: Path) -> None:
    adapter = create_adapter("claude_cli", local_root=tmp_path)
    assert isinstance(adapter, ClaudeCliAdapter)
    assert adapter.supports_callbacks is False


# ---------------------------------------------------------------- capability ClassVars


async def test_capability_classvars_and_isolation_posture() -> None:
    adapter = ClaudeCliAdapter()
    assert isinstance(adapter, RuntimeAdapter)
    assert adapter.supports_blind_workspace is False
    assert adapter.supports_attachments is True
    # No adapter-level cap: nothing here serializes runs.
    assert adapter.max_concurrent_runs is None
    assert adapter.pinned_version == PINNED_VERSION


def _on_path(folder: Path, monkeypatch: pytest.MonkeyPatch, **versions: str) -> None:
    """Stand-ins that print a version, alone on PATH, as `spawn_refusal` finds the real ones."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, version in versions.items():
        stub = folder / name
        stub.write_text(f"#!/bin/sh\necho '{version}'\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A root short enough for a tmux socket, which `spawn_refusal` checks."""
    yield from short_root("f10c-")


async def test_a_drifted_claudes_spawn_is_refused_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_root: Path
) -> None:
    """The worker asks `spawn_refusal` before every spawn, and a session log is translated only
    as observed at the pin, so drift refuses the spawn and names the install that fixes it."""
    drifted = "9.9.9 (Claude Code)"
    _on_path(tmp_path / "bin", monkeypatch, claude=drifted, tmux="tmux 3.5a")
    adapter = ClaudeCliAdapter(local_root=socket_root, cli=FakeCli())

    refusal = await adapter.spawn_refusal()

    assert refusal is not None
    assert repr(drifted) in refusal
    assert f"install claude {PINNED_VERSION} on the worker's host" in refusal
