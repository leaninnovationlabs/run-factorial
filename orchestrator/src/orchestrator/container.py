"""`python -m orchestrator.container` — the image's startup check (tech-stack decision 52).

The image's supervisor runs this once, before Postgres starts, and stops the container when it
prints anything. Without it a misconfigured image keeps running: the API answers 503 to every
request and the supervisor restarts a crashing worker for ever, which reads as a working app.

`python -m orchestrator.container keycheck` is the second mode: migrate runs it after the
migrations, on the image's own database only, and it fails when the master key cannot read the
secrets that database already holds.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Final, get_args

from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.session import dispose_engine, session_scope
from orchestrator.secrets import store
from orchestrator.secrets.crypto import (
    DecryptionError,
    SecretsConfigurationError,
    decode_master_key,
)
from orchestrator.settings import ENV_PREFIX, Settings, mail_problems
from orchestrator.worker.context import adapter_options
from runtime.adapters import create_adapter
from runtime.contract import AdapterUnavailable

__all__ = ["main", "master_key_problem", "problems"]

#: Written by the image's `init`: `external` when the database is not the image's own
#: (docker/rootfs/etc/factorial/database.sh).
_IMAGE_DATABASE: Final = f"{ENV_PREFIX}IMAGE_DATABASE"

#: The task role is the only AWS identity, and no agent run may hold it.
_AWS_CREDENTIALS: Final = frozenset(
    {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
#: Prefixes of the task's ECS endpoints: its metadata, and the agent's scale-in protection.
_ECS_ENDPOINTS: Final = ("ECS_CONTAINER_METADATA_URI", "ECS_AGENT_URI")
#: The retired deployment App's settings, its key among them. No setting names them any more, so
#: the secret check alone would pass one an ECS task still sets.
_RETIRED_GITHUB: Final = f"{ENV_PREFIX}GITHUB__"
#: The API token is every org's operator bearer and the key the session cookies and MCP tokens
#: are derived from, so a guessable one opens every org.
_API_TOKEN_MIN_LENGTH: Final = 32
_SAMPLE_API_TOKENS: Final = frozenset({"dev-token", "dev-token-change-me"})
_MAKE_A_TOKEN: Final = "generate one with `openssl rand -base64 32`"


def _holds_secret(annotation: object) -> bool:
    return annotation is SecretStr or any(_holds_secret(arg) for arg in get_args(annotation))


def _models(annotation: object) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _models(arg)


def _secret_names(model: type[BaseModel], prefix: str = ENV_PREFIX) -> Iterator[str]:
    for field_name, field in model.model_fields.items():
        name = f"{prefix}{field_name.upper()}"
        if _holds_secret(field.annotation):
            yield name
        for nested in _models(field.annotation):
            yield from _secret_names(nested, f"{name}__")


def _leaked(environ: Mapping[str, str]) -> list[str]:
    secret = set(_secret_names(Settings))
    if environ.get(_IMAGE_DATABASE) == "external":
        # On the image's own database env.sh exports only `factory`'s URL, whose password nobody
        # guards, and leaves any other in `.env`. An external one's is a secret.
        secret.add(f"{ENV_PREFIX}DATABASE_URL")
    # The settings read a variable whatever its case.
    return sorted(
        name
        for name in environ
        if name.upper() in secret or name.upper().startswith(_RETIRED_GITHUB)
    )


def _aws_leaked(environ: Mapping[str, str]) -> list[str]:
    return sorted(
        name for name in environ if name in _AWS_CREDENTIALS or name.startswith(_ECS_ENDPOINTS)
    )


def problems(
    load: Callable[[], Settings] = Settings, environ: Mapping[str, str] = os.environ
) -> list[str]:
    found = [
        f"{name} is in the services' environment, which every agent run inherits: init moves "
        "it into /app/.env only when the settings arrive as FACTORIAL_* variables with "
        "FACTORIAL_MASTER_KEY among them"
        for name in _leaked(environ)
    ]
    found += [
        f"{name} is in the services' environment, so every agent run would inherit AWS "
        "credentials: init removes the task role's, and no other AWS credential may be passed"
        for name in _aws_leaked(environ)
    ]
    try:
        settings = load()
    # OSError: an env file that cannot be read. ValueError: a validation failure, or the retired
    # `SF_` prefix refused at load.
    except (OSError, ValueError) as exc:
        return [*found, f"settings do not load: {exc}"]
    api_token = settings.api_token.get_secret_value()
    if not api_token:
        found.append("FACTORIAL_API_TOKEN is empty")
    elif api_token in _SAMPLE_API_TOKENS:
        found.append(f"FACTORIAL_API_TOKEN is a published sample value: {_MAKE_A_TOKEN}")
    elif len(api_token) < _API_TOKEN_MIN_LENGTH:
        found.append(
            f"FACTORIAL_API_TOKEN is shorter than {_API_TOKEN_MIN_LENGTH} characters: "
            f"{_MAKE_A_TOKEN}"
        )
    try:
        decode_master_key(settings.master_key.get_secret_value(), settings.master_key_id)
    except SecretsConfigurationError as exc:
        found.append(str(exc))
    # The host kinds a runtime row may run here; E2B runs build per row, from the row's key.
    for kind in ("claude_cli", "codex_cli"):
        try:
            create_adapter(kind, **adapter_options(settings, kind))
        except AdapterUnavailable as exc:
            found.append(str(exc))
    found += mail_problems(settings)
    # The image always names a state root, so an offer is what turns E2B on.
    if not settings.runtime.e2b.deployment and any(
        offer.endswith("@e2b") for offer in settings.offers
    ):
        found.append(
            "FACTORIAL_RUNTIME__E2B__DEPLOYMENT is empty, but the cloud sandbox is offered, by "
            "FACTORIAL_RUNTIME__OFFERED or by default under multi-org: set it, because a "
            "deployment reaps only the sandboxes it named, or set FACTORIAL_RUNTIME__OFFERED "
            "without an @e2b entry"
        )
    return found


async def master_key_problem(session: AsyncSession) -> str | None:
    """A stored secret the current master key cannot read: a volume set up under another key."""
    try:
        await store.verify_readable(session)
    except (DecryptionError, SecretsConfigurationError):
        return (
            "The database on this volume holds secrets made with a different "
            "FACTORIAL_MASTER_KEY. Start the container with the factorial.env this volume was "
            "set up with, or remove the volume (docker volume rm factorial-public) to start over."
        )
    return None


async def _keycheck() -> str | None:
    try:
        async with session_scope() as session:
            return await master_key_problem(session)
    finally:
        await dispose_engine()


def main() -> None:
    if sys.argv[1:] == ["keycheck"]:
        found = [problem] if (problem := asyncio.run(_keycheck())) else []
    else:
        found = problems()
    for problem in found:
        print(f"factorial: {problem}", file=sys.stderr)
    sys.exit(1 if found else 0)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
