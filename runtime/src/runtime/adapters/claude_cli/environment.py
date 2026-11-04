"""The child-process environment for a claude run. Git credentials and probe classification are
shared (`_cli/environment.py`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from runtime.adapters._cli.environment import (
    CHILD_ENV_ALLOWED_PREFIX,
    CHILD_ENV_ALLOWLIST,
    GITHUB_KEY,
    agent_git_identity,
    config_env,
    git_env,
    host_session_environment,
)
from runtime.adapters.claude_cli.auth import (
    AUTH_MODE_SUBSCRIPTION,
    require_api_key,
    require_oauth_token,
)
from runtime.adapters.claude_cli.cli import (
    ENV_AFK_PREFIX,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_CONFIG_DIR,
    ENV_DISABLE_AUTOUPDATER,
)
from runtime.adapters.claude_cli.tui import tui_env


def _credential_free_environment(
    secrets: Mapping[str, str], config_dir: Path
) -> dict[str, str]:
    """The complete child environment for a `credential="none"` run — constructed, never merged.

    The host keeps the token for its own clone and probes (which is what lets a blind clone use a
    token without the workspace retaining one), while the agent process gets neither the GitHub
    token variables nor the `GIT_CONFIG_*` credential helper that reads them. Filtering an overlay
    is not enough: `Cli.run`'s default path merges over `host_environment()`, and a merge
    cannot delete a host-exported `GH_TOKEN`. So this dict is the child's entire environment
    (`inherit_host_env=False`): `CHILD_ENV_ALLOWLIST`, every non-GitHub secret the spec declared
    (`GITHUB_KEY` is what a credential looks like here), and the per-run config dir.
    """
    child = {
        key: value
        for key, value in os.environ.items()
        if key in CHILD_ENV_ALLOWLIST or key.startswith(CHILD_ENV_ALLOWED_PREFIX)
    }
    child.update(
        {key: value for key, value in secrets.items() if not GITHUB_KEY.search(key)}
    )
    if ENV_API_KEY not in child and os.environ.get(ENV_API_KEY):
        # The one host value carried beyond the allowlist: a child launched with no harness key
        # dies on its first turn and reads as a failed run. GitHub-shaped keys are never carried,
        # from any source. A subscription run strips it again (`_tui_environment`).
        child[ENV_API_KEY] = os.environ[ENV_API_KEY]
    child[ENV_CONFIG_DIR] = str(config_dir)
    child[ENV_DISABLE_AUTOUPDATER] = "1"
    return child


def _tui_credential(secrets: Mapping[str, str], auth_mode: str) -> str:
    """The one credential a host run's CLI sees, from the store: the real key, as
    no proxy stands between a host and the API. `tui_env` and the seed's approved tail take this
    same value, or the TUI opens on the "use this API key?" dialog."""
    if auth_mode == AUTH_MODE_SUBSCRIPTION:
        return require_oauth_token(secrets)
    return require_api_key(secrets)


def _tui_environment(
    secrets: Mapping[str, str],
    *,
    config_dir: Path,
    home: Path,
    signals_path: Path,
    auth_mode: str,
    credential: str,
    credential_less: bool,
    config_sha: str | None,
    bot_login: str,
) -> dict[str, str]:
    """The complete environment of a host run's CLI, which `launch.sh` reads from
    its env file: what a session keeps of the worker's (`host_session_environment`), then the
    git credential, the agent's git identity and the run's secrets, or the constructed
    `credential="none"` environment with that identity, with no AFK timer from any source.
    Either way its HOME is `home`, the run's own (`HOME_DIRNAME`). `tui_env` and the config pin
    come last.
    """
    identity = agent_git_identity(bot_login)
    if credential_less:
        env = identity | _credential_free_environment(secrets, config_dir)
    else:
        host = host_session_environment((ENV_BASE_URL,))
        env = {**host, **git_env(secrets), **identity, **secrets}
    env = {key: value for key, value in env.items() if not key.startswith(ENV_AFK_PREFIX)}
    if auth_mode == AUTH_MODE_SUBSCRIPTION:
        env.pop(ENV_API_KEY, None)
    env |= tui_env(
        config_dir=str(config_dir),
        signals_path=str(signals_path),
        auth_mode=auth_mode,
        credential=credential,
    )
    env["HOME"] = str(home)
    return env | config_env(config_sha)
