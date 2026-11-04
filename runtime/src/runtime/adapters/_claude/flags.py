"""`claude` CLI environment flags that more than one adapter sets."""

from __future__ import annotations

from typing import Final

#: Set to "1": no telemetry, error reports or update checks, which the CLI otherwise sends to
#: `api.anthropic.com` whatever `ANTHROPIC_BASE_URL` says. In an E2B sandbox they would only
#: hit blocked hosts; the assistant's child must reach nothing but the API.
ENV_NONESSENTIAL: Final = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
#: Set to "false": no deferred tool loading, so a request offers exactly the tools it was given.
ENV_TOOL_SEARCH: Final = "ENABLE_TOOL_SEARCH"
