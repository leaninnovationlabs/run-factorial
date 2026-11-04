"""How a run authenticates: the two modes, and where each credential comes from.

Every refusal here is raised BEFORE anything is created, like the adapter's other refusals — a
missing credential must cost a clean `SpawnError`, not a half-built run directory.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from runtime.adapters.codex_cli.cli import AUTH_FILE, ENV_API_KEY
from runtime.contract import SpawnError, SpawnReason

#: How a run authenticates. This vocabulary is ours, not codex's: `api_key` stores a key, which
#: each launch writes into an apikey-shaped `auth.json` (`tui.py`); `subscription` seeds the run
#: home from a `codex login` bundle, because that is refreshable state with no flag to pass it to.
#: The two do not serve the same models — `gpt-5.x-codex` answers 404 on a subscription and
#: `gpt-5.6-terra` answers 400 on a key — and the adapter cannot know which, so a mismatch
#: surfaces as the vendor's own refusal on the first turn. A seeded login WINS over an
#: `OPENAI_API_KEY` in the child's environment (observed 0.153.2), so a manifest may keep
#: declaring the key for the other mode without breaking this one.
AUTH_MODE_API_KEY: Final = "api_key"
AUTH_MODE_SUBSCRIPTION: Final = "subscription"
AUTH_MODE_BEDROCK: Final = "bedrock"

#: The env var Codex reads for a custom Bedrock provider (`config.toml` `env_key`).
ENV_BEDROCK_BEARER: Final = "AWS_BEARER_TOKEN_BEDROCK"
BEDROCK_PROVIDER: Final = "bedrock-grok"
BEDROCK_REGION: Final = "us-east-1"

#: Where a browser login leaves it. Only read, never written: seeding copies it. The
#: developer-machine fallback — a worker with no operator login has no such file.
DEFAULT_SUBSCRIPTION_AUTH_FILE: Final = Path("~/.codex") / AUTH_FILE

#: The same document as a project secret, which is what lets a worker with no operator login
#: run `subscription` mode at all: per project, encrypted at rest, and injected through
#: `resolved_secrets()` like every other credential. Takes precedence over the file — the store
#: is the source of record.
SECRET_AUTH_JSON: Final = "CODEX_AUTH_JSON"


def subscription_login(secrets: Mapping[str, str], fallback_path: Path) -> str:
    """The stored login, else the operator's own file, else a refusal naming both fixes.

    Store first because it is the only source a worker with no operator login has: there is no
    operator home on the factory host, and a credential outside the store is outside the audit
    trail.
    """
    stored = secrets.get(SECRET_AUTH_JSON, "")
    if stored:
        return stored
    try:
        return fallback_path.read_text("utf-8")
    except OSError as exc:
        raise SpawnError(
            f"No {SECRET_AUTH_JSON}. This runtime runs on a Codex subscription: sign in with "
            "ChatGPT on the Runtime page, or, on a developer's machine, run `codex login` where "
            "factorial runs.\n\n"
            f"{fallback_path} could not be read either ({exc.strerror}).",
            reason=SpawnReason.CREDENTIAL,
        ) from exc


def require_api_key(secrets: Mapping[str, str]) -> str:
    """The key `api_key` mode writes into each launch's `auth.json` — from the STORE, not the
    environment.

    A run pointed at a fresh CODEX_HOME cannot see any operator's login, so a stored key is
    the only auth that works unattended in this mode (decision 34).
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


def require_bedrock_bearer(secrets: Mapping[str, str]) -> str:
    """The minted bearer `bedrock` mode launches with — from the secrets the worker injected,
    never from the host environment. A missing value is a mint failure that should have
    refused earlier.
    """
    token = secrets.get(ENV_BEDROCK_BEARER, "")
    if not token:
        raise SpawnError(
            f"No {ENV_BEDROCK_BEARER}. Factorial makes this token from the server's AWS role "
            "before each run, and none was made. The Runtime page does not store it.",
            reason=SpawnReason.CREDENTIAL,
        )
    return token


def bedrock_provider_config(region: str | None = None) -> dict[str, str]:
    """The `config.toml` provider block Codex needs for Bedrock Runtime `/openai/v1`."""
    chosen = (region or BEDROCK_REGION).strip() or BEDROCK_REGION
    gov = "-gov-" in chosen
    return {
        "provider": BEDROCK_PROVIDER,
        "base_url": f"https://bedrock-runtime.{chosen}.amazonaws.com/openai/v1",
        "env_key": ENV_BEDROCK_BEARER,
        "name": "Bedrock Grok 4.6 GovCloud" if gov else "Bedrock Grok 4.6",
    }
