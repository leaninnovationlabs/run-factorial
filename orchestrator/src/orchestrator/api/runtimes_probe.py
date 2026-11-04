"""Prove a runtime's stored model credential before a run finds out the hard way.

Each type makes the vendor's cheapest authenticated call, a GET of its model list: it spends no
tokens and refreshes nothing. `codex_subscription` makes none. Codex refreshes an expired login
itself, and OpenAI rotates the refresh token when it does, so a probe that ran codex on a copy
of the stored `auth.json` would revoke the stored one: it reads the access token's expiry instead.

A detail is a fixed sentence, never the vendor's body nor an exception's text: either can echo
the credential it was sent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

import httpx
from pydantic import SecretStr

from orchestrator.api.services.errors import as_sentence
from orchestrator.factorial.bedrock_token import (
    BedrockMintError,
    bedrock_base_url,
    mint_bearer,
)
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    bedrock_model_of,
    bedrock_region_of,
    credential_name_for,
    requires_stored_credential,
)

__all__ = ["TIMEOUT_SECONDS", "ProbeResult", "probe", "probe_e2b"]

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS: Final = 10

_ANTHROPIC_MODELS: Final = "https://api.anthropic.com/v1/models"
_OPENAI_MODELS: Final = "https://api.openai.com/v1/models"
_ANTHROPIC_VERSION: Final = "2023-06-01"
#: The same sentences for a key held only in a request: "stored" would claim a row exists.
_Held = Literal["stored", "given"]
#: What the operator does about a refused credential: store a row, or paste into the form again.
_RENEW: Final[dict[_Held, str]] = {"stored": "save", "given": "paste"}


def _unsendable(held: _Held) -> str:
    return f"The {held} credential could not be sent. Check it for stray characters."


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    #: Read from the stored credential alone; the vendor was not asked.
    local: bool = False


@dataclass(frozen=True)
class _Call:
    vendor: str
    noun: str
    url: str
    headers: Callable[[str], dict[str, str]]


_CALLS: Final[Mapping[str, _Call]] = {
    "claude_api": _Call(
        "Anthropic",
        "API key",
        _ANTHROPIC_MODELS,
        lambda key: {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION},
    ),
    "claude_subscription": _Call(
        "Anthropic",
        "OAuth token",
        _ANTHROPIC_MODELS,
        lambda token: {
            "authorization": f"Bearer {token}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": "oauth-2025-04-20",
        },
    ),
    "codex_api": _Call(
        "OpenAI", "API key", _OPENAI_MODELS, lambda key: {"authorization": f"Bearer {key}"}
    ),
}


async def probe(
    runtime_type: str,
    credentials: Mapping[str, SecretStr],
    *,
    client: httpx.AsyncClient,
    held: _Held = "stored",
    details: Mapping[str, object] | None = None,
) -> ProbeResult:
    """Whether the credential `runtime_type` names, as `credentials` holds it, works."""
    if not requires_stored_credential(runtime_type):
        return await _bedrock_host(client, details)
    name = credential_name_for(runtime_type)
    stored = credentials.get(name)
    if stored is None:
        absent = (
            f"No {name} was given."
            if held == "given"
            else f"The runtime holds no {name}. Save it first."
        )
        return ProbeResult(False, absent)
    if runtime_type == "codex_subscription":
        return _codex_login(stored, held)
    return await _vendor_call(_CALLS[runtime_type], stored, client, held)


async def _bedrock_host(
    client: httpx.AsyncClient, details: Mapping[str, object] | None = None
) -> ProbeResult:
    """Mint from the factory host and prove Grok on the runtime's Runtime Responses endpoint."""
    region = bedrock_region_of(details)
    model = bedrock_model_of(details)
    try:
        token = await asyncio.to_thread(mint_bearer, region=region)
    except BedrockMintError as exc:
        return ProbeResult(False, as_sentence(str(exc)), local=True)
    url = f"{bedrock_base_url(region)}/responses"
    try:
        response = await client.post(
            url,
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            json={"model": model, "input": "say ok"},
        )
    except httpx.TimeoutException:
        return ProbeResult(False, f"Bedrock did not answer within {TIMEOUT_SECONDS} seconds.")
    except httpx.HTTPError:
        logger.warning("credential probe to Bedrock failed: HTTPError")
        return ProbeResult(False, "Factorial couldn't reach Bedrock.")
    except Exception:
        logger.warning("credential probe to Bedrock failed: unexpected")
        return ProbeResult(False, "The host-minted bearer could not be sent.")
    code = response.status_code
    if 200 <= code < 300:
        return ProbeResult(True, "Bedrock accepted a host-minted bearer.")
    if code == 401:
        return ProbeResult(
            False, "Bedrock refused the host-minted bearer (401). Check the instance role."
        )
    if code == 403:
        return ProbeResult(
            False, "Bedrock answered 403. The instance role is not allowed this call."
        )
    if code == 429:
        return ProbeResult(False, "Bedrock rate-limited the check (429). Try again shortly.")
    return ProbeResult(False, f"Bedrock answered {code}. Try again.")


async def _vendor_call(
    call: _Call, stored: SecretStr, client: httpx.AsyncClient, held: _Held
) -> ProbeResult:
    try:
        response = await client.get(call.url, headers=call.headers(stored.get_secret_value()))
    except httpx.TimeoutException:
        return ProbeResult(False, f"{call.vendor} did not answer within {TIMEOUT_SECONDS} seconds.")
    except httpx.LocalProtocolError as exc:
        # A value no header can carry: a pasted newline. Its text quotes the value.
        logger.warning("credential probe to %s failed: %s", call.vendor, type(exc).__name__)
        return ProbeResult(False, _unsendable(held))
    except httpx.HTTPError as exc:
        logger.warning("credential probe to %s failed: %s", call.vendor, type(exc).__name__)
        return ProbeResult(False, f"Factorial couldn't reach {call.vendor}.")
    except Exception as exc:
        # A non-ASCII character fails while the request is built, before any transport.
        logger.warning("credential probe to %s failed: %s", call.vendor, type(exc).__name__)
        return ProbeResult(False, _unsendable(held))
    code = response.status_code
    if 200 <= code < 300:
        return ProbeResult(True, f"{call.vendor} accepted the {held} {call.noun}.")
    if code == 401:
        return ProbeResult(
            False,
            f"{call.vendor} refused the {held} {call.noun} (401). "
            f"{_RENEW[held].capitalize()} a fresh one.",
        )
    if code == 403:
        return ProbeResult(
            False, f"{call.vendor} answered 403. The {held} {call.noun} is not allowed this call."
        )
    if code == 429:
        return ProbeResult(False, f"{call.vendor} rate-limited the check (429). Try again shortly.")
    return ProbeResult(False, f"{call.vendor} answered {code}. Try again.")


def _codex_login(stored: SecretStr, held: _Held) -> ProbeResult:
    # The JWT's payload is read, never verified: this is a clock check, not an authentication.
    try:
        token = json.loads(stored.get_secret_value())["tokens"]["access_token"]
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires = datetime.fromtimestamp(claims["exp"], UTC)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, OSError):
        return ProbeResult(False, f"The {held} login does not parse.", local=True)
    when = expires.strftime("%Y-%m-%d %H:%M UTC")
    if expires <= datetime.now(UTC):
        return ProbeResult(
            False,
            f"The {held} login's access token expired at {when}. A run will try to refresh it. "
            f"If runs fail with 401, {_RENEW[held]} a fresh login.",
            local=True,
        )
    return ProbeResult(
        True,
        f"The {held} login is valid until {when}. Factorial did not check it with OpenAI.",
        local=True,
    )


class _Checked(Protocol):
    async def check(self) -> None: ...


async def probe_e2b(client: _Checked, *, held: _Held = "stored") -> ProbeResult:
    """An E2B runtime's team key, through the sandbox client's own read-only check."""
    try:
        await client.check()
    except Exception as exc:
        logger.warning("credential probe to E2B failed: %s", type(exc).__name__)
        return ProbeResult(
            False,
            f"E2B did not answer to the {held} {E2B_CREDENTIAL}. Check the key, or try again.",
        )
    return ProbeResult(True, f"E2B accepted the {held} {E2B_CREDENTIAL}.")
