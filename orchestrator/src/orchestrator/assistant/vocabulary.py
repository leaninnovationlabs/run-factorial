"""What the orchestrator knows about each chat backend, without importing any of them.

The credential names are pinned by test against the adapter constants, not imported, as
`factorial/runtimes.py` pins its own: the orchestrator's settings must load with no vendor
package present.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from orchestrator.factorial.bedrock_token import DEFAULT_MODEL as BEDROCK_DEFAULT_MODEL
from runtime.assistant.adapters import TAKES_IMAGES

__all__ = [
    "ALL_ASSISTANT_OFFERS",
    "BACKENDS",
    "HOST_ROLE_MODE",
    "SLOT_PREFIX",
    "BackendVocab",
    "assistant_offer",
    "credential_slot",
    "parse_assistant_offers",
    "parse_credential_slot",
    "stores_credential",
    "takes_images",
]


SLOT_PREFIX: Final = "assistant:"
#: The auth mode with nothing stored: each turn's bearer is minted from the host's AWS role.
HOST_ROLE_MODE: Final = "bedrock"


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
    # Bedrock Runtime's Responses API, called with no CLI between. A model Codex was not built
    # for cannot call a tool through `codex_cli` on Bedrock (tech-stack decision 63).
    "bedrock_responses": BackendVocab(
        kind="bedrock_responses",
        label="Bedrock",
        family="bedrock",
        credentials={HOST_ROLE_MODE: "AWS_BEARER_TOKEN_BEDROCK"},
        default_models={HOST_ROLE_MODE: BEDROCK_DEFAULT_MODEL},
    ),
}


def assistant_offer(kind: str, auth_mode: str) -> str:
    """One backend and auth mode as `FACTORIAL_ASSISTANT__OFFERED` names it."""
    return f"{kind}:{auth_mode}"


#: Every `<backend>:<auth_mode>` a deployment may offer the assistant.
ALL_ASSISTANT_OFFERS: Final[tuple[str, ...]] = tuple(
    assistant_offer(kind, mode) for kind, vocab in BACKENDS.items() for mode in vocab.credentials
)


def parse_assistant_offers(entries: Iterable[str]) -> frozenset[str]:
    """The set a deployment offers, each entry stripped; an unknown one is refused by name."""
    offers: set[str] = set()
    for raw in entries:
        entry = raw.strip()
        if entry not in ALL_ASSISTANT_OFFERS:
            raise ValueError(
                f"unknown assistant offer {entry!r}; expected <backend>:<auth_mode> from "
                f"{', '.join(ALL_ASSISTANT_OFFERS)}"
            )
        offers.add(entry)
    return frozenset(offers)


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
    return vocab is not None and auth_mode in vocab.credentials and auth_mode != HOST_ROLE_MODE


def takes_images(backend: str, auth_mode: str) -> bool:
    """Whether a turn on this backend and sign-in is handed the images attached to its message.

    The backend must be able to pass one on (`runtime.assistant.adapters.TAKES_IMAGES`), and the
    models behind the sign-in must be known to take one. Claude's and OpenAI's are. Nothing on
    Bedrock has been seen to, through Codex or through Bedrock Responses, so a Bedrock turn gets
    a message's words, its text files and what was pointed at, and is told of an image it was
    not given: a turn that failed on a picture would lose everything sent with it."""
    return backend in TAKES_IMAGES and auth_mode != HOST_ROLE_MODE
