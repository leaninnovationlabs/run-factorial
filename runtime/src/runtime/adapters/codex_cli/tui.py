"""The codex TUI's launch, as documents: what it adds to `build_config_toml`'s `config.toml`
(the checkout's trust, the hooks and their hashes, the system prompt), `auth.json`, the
environment and the argv (decision 56).

Pure but for `tui_environment`, which reads the worker's environment. Every path is a
parameter, because the E2B adapter passes its sandbox layout and a host passes its run dir. What
the CLI sees as its credential is a parameter too: on E2B a placeholder the egress proxy
replaces, so no secret reaches a seed (decision 25).
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Final

from runtime.adapters._cli.environment import (
    agent_git_identity,
    config_env,
    git_env,
    host_session_environment,
    worktree_env,
)
from runtime.adapters._cli.launch import ENV_SIGNALS
from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_BEDROCK,
    AUTH_MODE_SUBSCRIPTION,
)
from runtime.adapters.codex_cli.cli import (
    ASK_TOOL_INSTRUCTION,
    AUTH_KEY_MODE,
    CODEX_BIN,
    CONFIG_AUTH_STORE,
    CONFIG_CHECK_FOR_UPDATE,
    CONFIG_DEVELOPER_INSTRUCTIONS,
    CONFIG_FEATURES,
    CONFIG_FILE,
    CONFIG_HOOK_COMMAND,
    CONFIG_HOOK_HANDLERS,
    CONFIG_HOOK_STATE,
    CONFIG_HOOK_TYPE,
    CONFIG_HOOKS,
    CONFIG_LOG_DIR,
    CONFIG_SUPPRESS_UNSTABLE_WARNING,
    CONFIG_TRUSTED_HASH,
    ENV_API_KEY,
    ENV_CODEX_HOME,
    ENV_TERM,
    FEATURE_DEFAULT_MODE_ASK,
    FLAG_APPROVAL,
    FLAG_CD,
    FLAG_IMAGE,
    FLAG_MODEL,
    FLAG_NO_ALT_SCREEN,
    FLAG_NO_DAEMON,
    FLAG_SANDBOX,
    HOOK_DEFAULT_TIMEOUT_SEC,
    HOOK_LABELS,
    LOG_DIRNAME,
    ROLLOUT_NAME,
    SUBCMD_RESUME,
    VALUE_APPROVAL_NEVER,
    VALUE_AUTH_APIKEY,
    VALUE_AUTH_STORE_FILE,
    VALUE_HOOK_COMMAND,
    VALUE_TERM,
)
from runtime.adapters.codex_cli.environment import (
    _credential_free_environment,
    without_host_iam,
)
from runtime.adapters.codex_cli.spec import _toml_string, build_config_toml, build_prompt
from runtime.contract import McpServerSpec, RunSpec

#: The untrusted seed stops codex loading AGENTS.md itself, so the turn asks for it.
AGENTS_MD_LINE: Final = (
    "This checkout's AGENTS.md files are not loaded for you. If the repository has any, read "
    "them before you start."
)


def hook_hash(label: str, command: str) -> str:
    """The `trusted_hash` codex computes for a command hook that names no timeout: sha256 over
    its normalised identity, as canonical JSON."""
    identity = {
        "event_name": label,
        "hooks": [
            {
                "type": VALUE_HOOK_COMMAND,
                "command": command,
                "timeout": HOOK_DEFAULT_TIMEOUT_SEC,
                "async": False,
            }
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def signal_command(signals_path: str, signal_script: str) -> str:
    """Every hook's command: the shared signal script, with the file it appends to named in the
    command, since a hook runs with codex's environment snapshot rather than the TUI's."""
    return f"{ENV_SIGNALS}={shlex.quote(signals_path)} exec {shlex.quote(signal_script)}"


def tui_config(
    base: str,
    *,
    codex_home_real: str,
    signal_command: str,
    developer_instructions: str | None,
    ask_tool: bool = False,
) -> str:
    """`build_config_toml`'s sandbox, trust seed and MCP servers as `base`, with the TUI's keys
    ahead of its first table and the hooks after its last. Written at every launch, which also
    undoes an agent's edit.

    `codex_home_real` is `CODEX_HOME`'s fully resolved path, which the hooks' trust is keyed on.
    A resume passes no `developer_instructions`: the thread's history already holds them.
    `ask_tool` turns on codex's question box in Default mode, and tells the agent to use it.
    """
    if ask_tool and developer_instructions is not None:
        developer_instructions = f"{developer_instructions}\n\n{ASK_TOOL_INSTRUCTION}"
    keys = [
        f"{CONFIG_CHECK_FOR_UPDATE} = false",
        f"{CONFIG_AUTH_STORE} = {_toml_string(VALUE_AUTH_STORE_FILE)}",
        f"{CONFIG_LOG_DIR} = {_toml_string(f'{codex_home_real}/{LOG_DIRNAME}')}",
    ]
    if developer_instructions is not None:
        keys.append(f"{CONFIG_DEVELOPER_INSTRUCTIONS} = {_toml_string(developer_instructions)}")
    features: list[str] = []
    if ask_tool:
        keys.append(f"{CONFIG_SUPPRESS_UNSTABLE_WARNING} = true")
        features = [f"[{CONFIG_FEATURES}]", f"{FEATURE_DEFAULT_MODE_ASK} = true", ""]
    hooks: list[str] = []
    for event in HOOK_LABELS:
        hooks += [
            f"[[{CONFIG_HOOKS}.{event}]]",
            f"[[{CONFIG_HOOKS}.{event}.{CONFIG_HOOK_HANDLERS}]]",
            f"{CONFIG_HOOK_TYPE} = {_toml_string(VALUE_HOOK_COMMAND)}",
            f"{CONFIG_HOOK_COMMAND} = {_toml_string(signal_command)}",
            "",
        ]
    for label in HOOK_LABELS.values():
        key = _toml_string(f"{codex_home_real}/{CONFIG_FILE}:{label}:0:0")
        hooks += [
            f"[{CONFIG_HOOKS}.{CONFIG_HOOK_STATE}.{key}]",
            f"{CONFIG_TRUSTED_HASH} = {_toml_string(hook_hash(label, signal_command))}",
            "",
        ]
    return "\n".join([*keys, "", base.strip("\n"), "", *features, *hooks])


def tui_config_toml(
    servers: Sequence[McpServerSpec],
    secrets: Mapping[str, str],
    *,
    writable_root: Path,
    workspace_real: str,
    codex_home_real: str,
    signal_command: str,
    developer_instructions: str | None,
    ask_tool: bool = False,
    bedrock: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """A host run's whole `config.toml`, and the env its MCP servers need: `build_config_toml`'s
    document, which seeds `workspace_real` (the checkout's fully resolved path) untrusted, plus
    the TUI's."""
    base, mcp_env = build_config_toml(
        servers,
        secrets,
        writable_root=writable_root,
        worktree=Path(workspace_real),
        bedrock=bedrock,
    )
    document = tui_config(
        base,
        codex_home_real=codex_home_real,
        signal_command=signal_command,
        developer_instructions=developer_instructions,
        ask_tool=ask_tool,
    )
    return document, mcp_env


def auth_document(auth_mode: str, credential: str) -> str:
    """`auth.json`: in `api_key` mode the key the CLI sees (`credential`), and in `subscription`
    mode the stored login (`credential`) unchanged. `bedrock` writes none: Codex authenticates
    that provider through `env_key`, and an `OPENAI_API_KEY` auth.json would send it to OpenAI.
    """
    if auth_mode == AUTH_MODE_SUBSCRIPTION:
        return credential
    if auth_mode == AUTH_MODE_BEDROCK:
        return ""
    return json.dumps({AUTH_KEY_MODE: VALUE_AUTH_APIKEY, ENV_API_KEY: credential})


def tui_flags(model: str, *, worktree: str, sandbox_mode: str) -> tuple[str, ...]:
    """The options a spawn and every resume repeat, and all a resume stores."""
    return (
        FLAG_NO_DAEMON,
        FLAG_NO_ALT_SCREEN,
        FLAG_MODEL,
        model,
        FLAG_CD,
        worktree,
        FLAG_SANDBOX,
        sandbox_mode,
        FLAG_APPROVAL,
        VALUE_APPROVAL_NEVER,
    )


def pointer(turn_path: str) -> str:
    """The positional prompt. It names the turn file, which keeps the turn text off argv, where
    `ps` shows it and Linux caps one argument at 128 KiB.

    Refused: a path with whitespace, which an agent may read as two, and a relative one.
    """
    if any(char.isspace() for char in turn_path):
        raise ValueError(f"a turn path must contain no whitespace: {turn_path!r}")
    if not turn_path.startswith("/"):
        raise ValueError(f"a turn path must be absolute: {turn_path!r}")
    return f"Read {turn_path} and do what it says."


def _image_flags(images: Sequence[str]) -> list[str]:
    # Each in its own `=` form: a bare `--image` takes every value after it, the prompt too.
    if any("," in path for path in images):
        raise ValueError(f"`{FLAG_IMAGE}` splits on commas, so no path may hold one: {images!r}")
    return [f"{FLAG_IMAGE}={path}" for path in images]


def spawn_argv(
    flags: Sequence[str], images: Sequence[str], prompt: str, *, binary: str = CODEX_BIN
) -> tuple[str, ...]:
    return (binary, *flags, *_image_flags(images), prompt)


def resume_argv(
    flags: Sequence[str],
    thread: str,
    images: Sequence[str],
    prompt: str | None,
    *,
    binary: str = CODEX_BIN,
) -> tuple[str, ...]:
    """With no prompt the session comes back at its prompt, waiting for a human."""
    tail = () if prompt is None else (prompt,)
    return (binary, SUBCMD_RESUME, *flags, *_image_flags(images), thread, *tail)


def turn_text(spec: RunSpec, attachment_paths: Mapping[str, str]) -> str:
    """The first turn's file: `build_prompt`'s document, and the AGENTS.md line."""
    prompt = build_prompt(spec, attachment_paths)
    return f"{prompt}\n\n{AGENTS_MD_LINE}"


def tui_environment(
    secrets: Mapping[str, str],
    *,
    config_dir: Path,
    home: Path,
    worktree: Path,
    git_dir: Path | None,
    credential_less: bool,
    config_sha: str | None,
    mcp_env: Mapping[str, str],
    bot_login: str,
    bedrock: bool = False,
) -> dict[str, str]:
    """The complete environment of a host run's CLI: a credential-free one, or what a session
    keeps of the worker's (`host_session_environment`; codex reads no base URL from it) with the
    run's git env and secrets; both carry the agent's git identity. Either way its HOME is
    `home`, the run's own (`HOME_DIRNAME`). The config pin comes last."""
    identity = agent_git_identity(bot_login)
    if credential_less:
        env = identity | _credential_free_environment(secrets, config_dir)
    else:
        env = {**host_session_environment(), **git_env(secrets), **identity, **secrets}
        if bedrock:
            env = without_host_iam(env)
    env |= mcp_env
    if git_dir is not None:
        env |= worktree_env(worktree, git_dir)
    env |= {ENV_CODEX_HOME: str(config_dir), ENV_TERM: VALUE_TERM, "HOME": str(home)}
    return env | config_env(config_sha)


def thread_of(path: str | PurePath) -> str | None:
    """The thread a rollout file belongs to, from its name alone."""
    match = ROLLOUT_NAME.fullmatch(PurePath(path).name)
    return match["thread"] if match else None
