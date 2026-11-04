"""The `claude` CLI's two environment credentials, and the rule for putting exactly one of them in
a child's environment. Part of the pinned CLI surface (`claude_cli/cli.py`), held here because
`runtime.assistant`'s `claude_sdk` adapter launches the same binary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: The api_key-mode credential, an ordinary run secret (tech-stack.md decision 16). Under a
#: per-run CLAUDE_CONFIG_DIR the operator's own login is invisible to a child; subscription mode
#: uses `ENV_OAUTH_TOKEN` below instead.
ENV_API_KEY: Final = "ANTHROPIC_API_KEY"
#: The other credential the CLI honours from its environment: the long-lived OAuth token `claude
#: setup-token` prints once on an operator's own machine (an `sk-ant-oat…`, meant for CI).
#: There is no device-code flow to run server-side — the browser is the only way to mint one.
#: PRECEDENCE: a set `ANTHROPIC_API_KEY` outranks this variable, blank included, so a
#: subscription run must launch with the key ABSENT from its environment, never shadowed to "".
ENV_OAUTH_TOKEN: Final = "CLAUDE_CODE_OAUTH_TOKEN"


def credential_environment(
    base: Mapping[str, str], *, auth_mode: str, credential: str
) -> dict[str, str]:
    """A copy of `base` carrying exactly one Claude credential. The CLI prefers a set
    `ANTHROPIC_API_KEY` over `CLAUDE_CODE_OAUTH_TOKEN`, blank included, so a subscription
    environment must not hold the key at all."""
    env = {k: v for k, v in base.items() if k not in (ENV_API_KEY, ENV_OAUTH_TOKEN)}
    env[ENV_OAUTH_TOKEN if auth_mode == "subscription" else ENV_API_KEY] = credential
    return env
