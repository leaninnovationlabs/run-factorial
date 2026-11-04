from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.vocabulary import BACKENDS, credential_slot


def test_credential_names_match_the_adapter_constants() -> None:
    from runtime.adapters._claude.credentials import ENV_API_KEY, ENV_OAUTH_TOKEN
    from runtime.adapters.codex_cli.auth import (
        ENV_API_KEY as CODEX_API_KEY,
    )
    from runtime.adapters.codex_cli.auth import (
        ENV_BEDROCK_BEARER,
        SECRET_AUTH_JSON,
    )

    assert BACKENDS["claude_sdk"].credentials == {
        "api_key": ENV_API_KEY,
        "subscription": ENV_OAUTH_TOKEN,
    }
    assert BACKENDS["codex_cli"].credentials == {
        "api_key": CODEX_API_KEY,
        "subscription": SECRET_AUTH_JSON,
        "bedrock": ENV_BEDROCK_BEARER,
    }


def test_every_vocabulary_kind_is_registered() -> None:
    from runtime.assistant.adapters import _REGISTRY

    assert set(BACKENDS) == set(_REGISTRY)


def test_the_default_model_is_the_settings_default() -> None:
    assert BACKENDS["claude_sdk"].default_model == AssistantSettings().model


def test_credential_slot_names_backend_and_mode() -> None:
    assert credential_slot("claude_sdk", "api_key") == "assistant:claude_sdk:api_key"


def test_settings_refuse_an_alias_and_an_unknown_backend() -> None:
    with pytest.raises(ValidationError, match="alias"):
        AssistantSettings(model="opus")
    with pytest.raises(ValidationError, match="unknown backend"):
        AssistantSettings(backend="gemini")


def test_settings_refuse_an_auth_mode_the_backend_lacks() -> None:
    with pytest.raises(ValidationError, match="no auth mode"):
        AssistantSettings(auth_mode="oauth")


@pytest.mark.parametrize(
    "field, value",
    [
        ("turn_budget_usd", "0"),
        ("turn_budget_usd", "50.01"),
        ("max_steps", 0),
        ("max_steps", 101),
        ("daily_budget_usd", "1000.01"),
        ("effort", "extreme"),
    ],
)
def test_settings_refuse_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AssistantSettings(**{field: value})  # type: ignore[arg-type]


def test_settings_refuse_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        AssistantSettings.model_validate({"enabled": True, "api_key": "sk-x"})


def test_defaults_are_the_spec_defaults() -> None:
    s = AssistantSettings()
    assert (s.enabled, s.backend, s.auth_mode, s.model, s.effort) == (
        False,
        "claude_sdk",
        "api_key",
        "claude-opus-5-5",
        "medium",
    )
    assert (s.turn_budget_usd, s.max_steps, s.daily_budget_usd) == (
        Decimal("1.00"),
        25,
        Decimal("20.00"),
    )
