"""The claude TUI's launch, as documents: the seed files, the hooks and the script they run, the
environment and the argv (decision 56).

Pure: no I/O, and every path is a parameter, because the E2B adapter passes its sandbox layout
and a host passes its run dir. What the CLI sees as its credential is a parameter too: on E2B a
placeholder the egress proxy replaces, so no secret reaches a seed (decision 25).
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from typing import Final

from runtime.adapters._cli.launch import ENV_SIGNALS
from runtime.adapters.claude_cli.auth import AUTH_MODE_SUBSCRIPTION
from runtime.adapters.claude_cli.cli import (
    API_KEY_TAIL,
    ATTACH_PREFIX,
    CLAUDE_BIN,
    CLAUDE_JSON_FILE,
    CLAUDE_JSON_KEY_RESPONSES,
    CLAUDE_JSON_ONBOARDED,
    CLAUDE_JSON_PROJECTS,
    CLAUDE_JSON_TRUSTED,
    END_OF_OPTIONS,
    ENV_API_KEY,
    ENV_CLASSIC_RENDERER,
    ENV_CONFIG_DIR,
    ENV_DISABLE_AUTOUPDATER,
    ENV_DISABLE_MARKETPLACE,
    ENV_OAUTH_TOKEN,
    ENV_PROJECT_DIR_NAME,
    ENV_TERM,
    FLAG_ALLOWED_TOOLS,
    FLAG_MCP_CONFIG,
    FLAG_MODEL,
    FLAG_PERMISSION_MODE,
    FLAG_RESUME,
    FLAG_SESSION_ID,
    FLAG_SETTING_SOURCES,
    FLAG_STRICT_MCP,
    FLAG_SYSTEM_PROMPT_FILE,
    HOOK_COMMAND,
    HOOK_LIST,
    HOOK_MATCHER,
    HOOK_POST_TOOL_USE,
    HOOK_PRE_TOOL_USE,
    HOOK_SESSION_END,
    HOOK_SESSION_START,
    HOOK_STOP,
    HOOK_STOP_FAILURE,
    HOOK_TYPE,
    HOOK_USER_PROMPT_SUBMIT,
    KEY_RESPONSES_APPROVED,
    KEY_RESPONSES_REJECTED,
    SETTINGS_ASK_TIMEOUT,
    SETTINGS_CLAUDE_MD_EXCLUDES,
    SETTINGS_FILE,
    SETTINGS_HOOKS,
    SETTINGS_SKIP_BYPASS_PROMPT,
    TOOL_ASK_USER_QUESTION,
    VALUE_ASK_TIMEOUT_NEVER,
    VALUE_BYPASS_PERMISSIONS,
    VALUE_HOOK_COMMAND,
    VALUE_PROJECT_DIR,
    VALUE_TERM,
)
from runtime.adapters.claude_cli.spec import mcp_tool_patterns
from runtime.contract import RunSpec

_HOOKED: Final = (
    HOOK_SESSION_START,
    HOOK_USER_PROMPT_SUBMIT,
    HOOK_PRE_TOOL_USE,
    HOOK_POST_TOOL_USE,
    HOOK_STOP,
    HOOK_STOP_FAILURE,
    HOOK_SESSION_END,
)
#: A question is the only tool call the adapter needs to hear of.
_MATCHED: Final = {
    HOOK_PRE_TOOL_USE: TOOL_ASK_USER_QUESTION,
    HOOK_POST_TOOL_USE: TOOL_ASK_USER_QUESTION,
}


def seed_files(
    *,
    config_dir: str,
    workspace_real: str,
    signal_script: str,
    auth_mode: str,
    credential: str,
) -> dict[str, bytes]:
    """`.claude.json` and `settings.json`, by path under `config_dir`. Written at every launch,
    which also undoes an agent's edit of its own hooks.

    `workspace_real` is trusted, and only it: the checkout's fully resolved path, read where
    the CLI runs. No CLAUDE.md outside it is read. `credential` is the key the CLI's environment
    holds (`tui_env`); in `api_key` mode its tail is approved, and a subscription run approves
    no key.
    """
    claude_json: dict[str, object] = {
        CLAUDE_JSON_ONBOARDED: True,
        CLAUDE_JSON_PROJECTS: {workspace_real: {CLAUDE_JSON_TRUSTED: True}},
    }
    if auth_mode != AUTH_MODE_SUBSCRIPTION:
        claude_json[CLAUDE_JSON_KEY_RESPONSES] = {
            KEY_RESPONSES_APPROVED: [credential.strip()[-API_KEY_TAIL:]],
            KEY_RESPONSES_REJECTED: [],
        }
    hook = {HOOK_TYPE: VALUE_HOOK_COMMAND, HOOK_COMMAND: shlex.quote(signal_script)}
    hooks = {
        event: [
            {
                **({HOOK_MATCHER: _MATCHED[event]} if event in _MATCHED else {}),
                HOOK_LIST: [hook],
            }
        ]
        for event in _HOOKED
    }
    settings = {
        SETTINGS_SKIP_BYPASS_PROMPT: True,
        SETTINGS_ASK_TIMEOUT: VALUE_ASK_TIMEOUT_NEVER,
        SETTINGS_HOOKS: hooks,
        SETTINGS_CLAUDE_MD_EXCLUDES: [f"!{workspace_real}/**"],
    }
    return {
        f"{config_dir}/{CLAUDE_JSON_FILE}": json.dumps(claude_json, indent=1).encode(),
        f"{config_dir}/{SETTINGS_FILE}": json.dumps(settings, indent=1).encode(),
    }


def tui_env(
    *, config_dir: str, signals_path: str, auth_mode: str, credential: str
) -> dict[str, str]:
    """The CLI's own variables for a TUI run; the caller adds HOME, PATH, git's and the rest.

    A subscription run carries no `ANTHROPIC_API_KEY` at all, since a set key outranks the token
    (cli.py). The `CLAUDE_AFK_*` timers are never set.
    """
    env = {
        ENV_CONFIG_DIR: config_dir,
        ENV_DISABLE_AUTOUPDATER: "1",
        ENV_DISABLE_MARKETPLACE: "1",
        ENV_CLASSIC_RENDERER: "1",
        ENV_PROJECT_DIR_NAME: VALUE_PROJECT_DIR,
        ENV_TERM: VALUE_TERM,
        ENV_SIGNALS: signals_path,
    }
    env[ENV_OAUTH_TOKEN if auth_mode == AUTH_MODE_SUBSCRIPTION else ENV_API_KEY] = credential
    return env


def tui_flags(spec: RunSpec, *, system_prompt_path: str, mcp_path: str) -> tuple[str, ...]:
    """The options a spawn and every resume repeat: `--resume` restores only the conversation.
    `mcp_path` is named only when the spec declares a server; the caller writes the file."""
    flags = [
        FLAG_MODEL,
        spec.model,
        FLAG_PERMISSION_MODE,
        VALUE_BYPASS_PERMISSIONS,
        FLAG_SYSTEM_PROMPT_FILE,
        system_prompt_path,
        FLAG_STRICT_MCP,
    ]
    if spec.mcp:
        flags += [FLAG_MCP_CONFIG, mcp_path]
        patterns = mcp_tool_patterns(spec)
        if patterns:
            flags += [FLAG_ALLOWED_TOOLS, *patterns]
    return tuple(flags)


def view_flags(flags: Sequence[str]) -> tuple[str, ...]:
    """`flags` less every MCP config in either form, with every value it takes:
    `--strict-mcp-config` then leaves no server; and the user's settings alone, so a hook the
    checkout ships never runs beside the model key."""
    kept: list[str] = []
    dropping = False
    for word in flags:
        if word == FLAG_MCP_CONFIG or word.startswith(f"{FLAG_MCP_CONFIG}="):
            dropping = word == FLAG_MCP_CONFIG
            continue
        if dropping and not word.startswith("-"):
            continue
        dropping = False
        kept.append(word)
    if FLAG_STRICT_MCP not in kept:
        kept.append(FLAG_STRICT_MCP)
    return (*kept, FLAG_SETTING_SOURCES, "user")


def pointer(turn_path: str, attachment_paths: Sequence[str] = ()) -> str:
    """The positional prompt: it names the turn file, which keeps the turn text off argv, and
    attaches each image. The turn file itself is `build_prompt`'s text.

    An `@` path ends at whitespace, so one containing any would arrive as text: refused.
    """
    paths = (turn_path, *attachment_paths)
    if any(char.isspace() for path in paths for char in path):
        raise ValueError(f"an attached path must contain no whitespace: {paths!r}")
    attached = "".join(f" {ATTACH_PREFIX}{path}" for path in attachment_paths)
    return f"Read {ATTACH_PREFIX}{turn_path} and do what it says.{attached}"


def spawn_argv(
    flags: Sequence[str], session_id: str, prompt: str, *, binary: str = CLAUDE_BIN
) -> tuple[str, ...]:
    return (binary, FLAG_SESSION_ID, session_id, *flags, END_OF_OPTIONS, prompt)


def resume_argv(
    flags: Sequence[str], session_id: str, prompt: str | None, *, binary: str = CLAUDE_BIN
) -> tuple[str, ...]:
    """With no prompt the session comes back at its prompt, waiting for a human."""
    tail = () if prompt is None else (END_OF_OPTIONS, prompt)
    return (binary, FLAG_RESUME, session_id, *flags, *tail)
