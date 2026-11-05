"""What the orchestrator knows about each chat backend, without importing any of them.

The credential names are pinned by test against the adapter constants, not imported, as
`factorial/runtimes.py` pins its own: the orchestrator's settings must load with no vendor
package present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from orchestrator.factorial.bedrock_token import DEFAULT_MODEL as BEDROCK_DEFAULT_MODEL

__all__ = [
    "BACKENDS",
    "SLOT_PREFIX",
    "BackendVocab",
    "credential_slot",
    "parse_credential_slot",
    "stores_credential",
]


SLOT_PREFIX: Final = "assistant:"


@dataclass(frozen=True)
class BackendVocab:
    kind: str
    label: str
    family: str
    #: auth mode -> the environment variable the backend reads it from.
    credentials: Mapping[str, str]
    #: auth mode -> the model a switch to it starts from: a Codex key and a Codex subscription
    #: serve different models (`runtime/adapters/codex_cli/auth.py`).
    default_models: Mapping[str, str]


BACKENDS: Final[Mapping[str, BackendVocab]] = {
    "claude_sdk": BackendVocab(
        kind="claude_sdk",
        label="Claude",
        family="claude",
        credentials={
            "api_key": "ANTHROPIC_API_KEY",
            "subscription": "CLAUDE_CODE_OAUTH_TOKEN",
        },
        default_models={"api_key": "claude-opus-5-5", "subscription": "claude-opus-5-5"},
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
        default_models={
            "api_key": "gpt-5.3-codex",
            "subscription": "gpt-5.6-terra",
            "bedrock": BEDROCK_DEFAULT_MODEL,
        },
    ),
}


def credential_slot(kind: str, auth_mode: str) -> str:
    """The `organization_credential` name for one backend and auth mode. A switch of either reads a
    different slot, so a key is never offered to a backend that cannot use it."""
    return f"{SLOT_PREFIX}{kind}:{auth_mode}"


def parse_credential_slot(name: str) -> tuple[str, str] | None:
    if not name.startswith(SLOT_PREFIX):
        return None
    kind, _, auth_mode = name.removeprefix(SLOT_PREFIX).partition(":")
    if not kind or not auth_mode or ":" in auth_mode:
        return None
    return kind, auth_mode


def stores_credential(kind: str, auth_mode: str) -> bool:
    """Bedrock's bearer is minted from the host's role for each turn, so nothing is stored."""
    vocab = BACKENDS.get(kind)
    return vocab is not None and auth_mode in vocab.credentials and auth_mode != "bedrock"
