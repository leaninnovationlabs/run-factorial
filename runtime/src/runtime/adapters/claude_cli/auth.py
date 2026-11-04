"""How a run authenticates: the two modes and where each credential comes from.

Every refusal here is raised BEFORE anything is created, like the adapter's other refusals — a
missing credential must cost a clean `SpawnError`, not a half-built run directory.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from runtime.adapters.claude_cli.cli import ENV_API_KEY, ENV_OAUTH_TOKEN
from runtime.contract import SpawnError, SpawnReason

#: How a run authenticates. The codex adapter's vocabulary, kept identical so the orchestrator
#: forwards one `auth_mode` to both families (`factorial.runtimes.auth_mode_for`). `api_key`
#: bills API credits through `ANTHROPIC_API_KEY`; `subscription` bills the operator's own plan
#: through the token `claude setup-token` prints. Unlike codex there is no login bundle to seed
#: a config home with — the token is an ordinary environment variable, and the only adapter
#: work is keeping the key OUT of the child, because the CLI prefers the key when both are set.
AUTH_MODE_API_KEY: Final = "api_key"
AUTH_MODE_SUBSCRIPTION: Final = "subscription"

#: The token as a runtime credential: what the Runtime page stores under
#: `credential_name_for("claude_subscription")`, and what the child reads — one string on both
#: sides, so there is nothing to translate and nothing to drift.
SECRET_OAUTH_TOKEN: Final = ENV_OAUTH_TOKEN


def require_api_key(secrets: Mapping[str, str]) -> str:
    """The key `api_key` mode launches with — from the STORE, not the environment.

    Accepting an exported key let a project with an empty secret table run anyway, which is how
    the resume path went unnoticed reading os.environ. A run is pointed at a fresh
    CLAUDE_CONFIG_DIR and so cannot see any operator's OAuth session either: it would answer
    `Not logged in` (tech-stack.md decision 16, verified against 2.1.236).
    """
    api_key = secrets.get(ENV_API_KEY, "")
    if not api_key:
        raise SpawnError(
            f"No {ENV_API_KEY}. Store it on the Runtime page, for the runtime this process "
            "names. A process that names no runtime reads it from the project's secrets.",
            # CREDENTIAL: a secret a human must store. The next tick reads the same store.
            reason=SpawnReason.CREDENTIAL,
        )
    return api_key


def require_oauth_token(secrets: Mapping[str, str]) -> str:
    """The token `subscription` mode launches with — from the STORE, for the same reason.

    The fix is named in full because there is no server-side flow to point at: the operator
    runs `claude setup-token` in their own browser session and pastes the result on the Runtime
    page, once.
    """
    token = secrets.get(SECRET_OAUTH_TOKEN, "")
    if not token:
        raise SpawnError(
            f"No {SECRET_OAUTH_TOKEN}. This runtime runs on a Claude subscription: run "
            "`claude setup-token` on your own machine, and paste the token it prints on the "
            "Runtime page. Claude Code has no device login to start here.",
            reason=SpawnReason.CREDENTIAL,
        )
    return token
