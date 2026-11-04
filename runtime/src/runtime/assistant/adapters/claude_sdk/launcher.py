"""The `cli_path` the SDK spawns: re-executes the real `claude` with an environment built from an
allowlist (tech-stack.md decision 63).

The Python SDK merges the whole of `os.environ` into the CLI child, under `ClaudeAgentOptions.env`,
and a merge cannot delete — the trap `claude_cli/environment.py` documents. The assistant process
holds the database URL and the API token, so the child is built here instead:
what the CLI needs to run and reach the API, the `CLAUDE_CODE_*` flags the adapter and the SDK set,
by exact name, and the one credential the turn's auth mode names. Everything else stays behind,
every `FACTORIAL_*` value included, and every other `CLAUDE_CODE_*` one: the CLI reads credentials
and session wiring under that prefix too (`CLAUDE_CODE_SESSION_ACCESS_TOKEN`,
`CLAUDE_CODE_GATEWAY_TOKEN`, `CLAUDE_CODE_SSE_PORT`, ...), and a shell inside Claude Code exports
some of them.

Installed as the `factorial-assistant-cli` console script beside the interpreter.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Final, NoReturn

from runtime.adapters._claude.credentials import ENV_API_KEY, ENV_OAUTH_TOKEN
from runtime.adapters._claude.flags import ENV_NONESSENTIAL, ENV_TOOL_SEARCH

__all__ = ["ENV_AUTH_MODE", "ENV_REAL_CLI", "child_environment", "main"]

#: The bundled `claude` to exec. Set by the adapter in `ClaudeAgentOptions.env`, never kept.
ENV_REAL_CLI: Final = "FACTORIAL_ASSISTANT_REAL_CLI"
#: `api_key` or `subscription`: which of the two credentials the child keeps.
ENV_AUTH_MODE: Final = "FACTORIAL_ASSISTANT_AUTH_MODE"
#: An allowlist of exact names, never a denylist or a prefix: either leaks the next secret
#: anyone exports.
_KEEP: Final = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "DISABLE_AUTOUPDATER",
        # Set by `options.build_options`.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        ENV_NONESSENTIAL,
        ENV_TOOL_SEARCH,
        # Set by the pinned SDK when it spawns the CLI (`subprocess_cli.py`).
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    }
)


def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    kept = {k: v for k, v in parent.items() if k in _KEEP}
    mode = parent.get(ENV_AUTH_MODE, "api_key")
    name = ENV_OAUTH_TOKEN if mode == "subscription" else ENV_API_KEY
    if parent.get(name):
        kept[name] = parent[name]
    return kept


def main() -> NoReturn:
    real = os.environ.get(ENV_REAL_CLI)
    if not real:
        # The SDK probes `cli_path -v` with its own environment, which never carries this.
        sys.exit(f"{ENV_REAL_CLI} is not set: this launcher runs only under the claude_sdk adapter")
    os.execve(real, [real, *sys.argv[1:]], child_environment(os.environ))


if __name__ == "__main__":
    main()
