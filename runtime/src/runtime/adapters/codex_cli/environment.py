"""The child-process environment for a codex run. Git credentials and probe classification are
shared (`_cli/environment.py`); what differs here is the config-home variable (`CODEX_HOME`) and
which harness key a credential-free child must still carry."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from runtime.adapters._cli.environment import (
    CHILD_ENV_ALLOWED_PREFIX,
    CHILD_ENV_ALLOWLIST,
    GITHUB_KEY,
)
from runtime.adapters.codex_cli.cli import ENV_CODEX_HOME

#: Instance-role material the Codex child must not inherit. The Bedrock bearer is a different
#: name (`AWS_BEARER_TOKEN_BEDROCK`) and is injected from secrets on purpose.
_AWS_IAM_ENV: Final = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
    }
)


def _credential_free_environment(secrets: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """The complete child environment for a `credential="none"` run — constructed, never merged.

    The host keeps the token for its own clone and probes (which is what lets a blind clone use a
    token without the workspace retaining one), while the agent process gets neither the GitHub
    token variables nor the `GIT_CONFIG_*` credential helper that reads them. Filtering an
    overlay is not enough: `Cli.run`'s default path merges over `host_environment()`, and a
    merge cannot delete a host-exported `GH_TOKEN`. So this dict is the child's entire environment
    (`inherit_host_env=False`): `CHILD_ENV_ALLOWLIST`, every non-GitHub secret the spec declared
    (`GITHUB_KEY` is what a credential looks like here), and the per-run config home.
    """
    child = {
        key: value
        for key, value in os.environ.items()
        if key in CHILD_ENV_ALLOWLIST or key.startswith(CHILD_ENV_ALLOWED_PREFIX)
    }
    child.update({key: value for key, value in secrets.items() if not GITHUB_KEY.search(key)})
    # No host-env fallback for the harness key, unlike claude_cli's copy of this function:
    # spawn refuses a spec whose STORE has no key, so by here `secrets` always carries one and a
    # fallback could only ever launch a run the guard meant to refuse.
    child[ENV_CODEX_HOME] = str(config_dir)
    return child


def without_host_iam(env: Mapping[str, str]) -> dict[str, str]:
    """Drop IAM/profile variables so a Bedrock run cannot use the instance role as itself."""
    return {key: value for key, value in env.items() if key not in _AWS_IAM_ENV}
