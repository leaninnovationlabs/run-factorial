"""Each turn's model credential, from the API (design.md §7.18).

This process holds no master key, so it cannot open the credential an organization stored. It
asks the API for it on loopback with the API token, as it asks for its GitHub access
(`github_token.api_fetch`), naming the backend and auth mode the turn will run: the API hands
out only the credential stored for those, and answers 409 when the assistant is off, holds no
credential, or its settings changed since the turn read them. The last raises `SettingsChanged`,
so the turn does not tell the person to set the assistant up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

import httpx
from pydantic import SecretStr

from orchestrator.assistant.config import AssistantSettings
from orchestrator.settings import APP_PREFIX, Settings

__all__ = ["CREDENTIAL_PATH", "CredentialFetch", "SettingsChanged", "api_credential"]

#: Formatted with the organization's slug.
CREDENTIAL_PATH: Final = f"{APP_PREFIX}/api/orgs/{{org}}/assistant/credential"
FETCH_TIMEOUT: Final = 10.0

class SettingsChanged(Exception):
    """The organization's assistant settings changed after the turn read them."""


#: `(organization slug, its settings) -> the credential, or None when there is none to run on`.
CredentialFetch = Callable[[str, AssistantSettings], Awaitable[SecretStr | None]]


def api_credential(
    settings: Callable[[], Settings], *, transport: httpx.AsyncBaseTransport | None = None
) -> CredentialFetch:
    async def fetch(org: str, assistant: AssistantSettings) -> SecretStr | None:
        current = settings()
        async with httpx.AsyncClient(
            base_url=current.assistant.api_url.rstrip("/"),
            timeout=FETCH_TIMEOUT,
            trust_env=False,
            transport=transport,
        ) as client:
            response = await client.post(
                CREDENTIAL_PATH.format(org=org),
                json={"backend": assistant.backend, "auth_mode": assistant.auth_mode},
                headers={"Authorization": f"Bearer {current.api_token.get_secret_value()}"},
            )
        if response.status_code == httpx.codes.CONFLICT:
            if response.json().get("code") == "settings_changed":
                raise SettingsChanged
            return None
        response.raise_for_status()
        return SecretStr(response.json()["credential"])

    return fetch
