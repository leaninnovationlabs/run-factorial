"""What the orchestrator knows about each chat backend, without importing any of them.

The credential names are pinned by test against the adapter constants, not imported, as
`factorial/runtimes.py` pins its own: the orchestrator's settings must load with no vendor
package present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = ["BACKENDS", "BackendVocab", "credential_slot"]


@dataclass(frozen=True)
class BackendVocab:
    kind: str
    label: str
    family: str
    #: auth mode -> the environment variable the backend reads it from.
    credentials: Mapping[str, str]
    default_model: str


BACKENDS: Final[Mapping[str, BackendVocab]] = {
    "claude_sdk": BackendVocab(
        kind="claude_sdk",
        label="Claude",
        family="claude",
        credentials={
            "api_key": "ANTHROPIC_API_KEY",
            "subscription": "CLAUDE_CODE_OAUTH_TOKEN",
        },
        default_model="claude-opus-5-5",
    ),
    "codex_cli": BackendVocab(
        kind="codex_cli",
        label="Codex",
        family="codex",
        credentials={
            "api_key": "OPENAI_API_KEY",
            "subscription": "CODEX_AUTH_JSON",
            "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
        },
        default_model="us.xai.grok-4.6",
    ),
}


def credential_slot(kind: str, auth_mode: str) -> str:
    """The `organization_credential` name for one backend and auth mode. A switch of either reads a
    different slot, so a key is never offered to a backend that cannot use it."""
    return f"assistant:{kind}:{auth_mode}"
