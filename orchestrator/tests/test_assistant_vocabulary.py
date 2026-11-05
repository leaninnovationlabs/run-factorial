from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.vocabulary import (
    BACKENDS,
    credential_slot,
    parse_credential_slot,
    stores_credential,
)


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
    assert BACKENDS["claude_sdk"].default_models["api_key"] == AssistantSettings().model


def test_every_auth_mode_has_a_default_model() -> None:
    for vocab in BACKENDS.values():
        assert set(vocab.default_models) == set(vocab.credentials)


def test_codex_defaults_follow_the_sign_in() -> None:
    """A key and a subscription serve different models (`codex_cli/auth.py`)."""
    assert BACKENDS["codex_cli"].default_models == {
        "api_key": "gpt-5.3-codex",
        "subscription": "gpt-5.6-terra",
        "bedrock": "us.xai.grok-4.6",
    }


def test_credential_slot_names_backend_and_mode() -> None:
    assert credential_slot("claude_sdk", "api_key") == "assistant:claude_sdk:api_key"



def test_a_credential_slot_reads_back_as_its_backend_and_mode() -> None:
    assert parse_credential_slot(credential_slot("codex_cli", "subscription")) == (
        "codex_cli",
        "subscription",
    )
    assert parse_credential_slot("runtime:x") is None
    assert parse_credential_slot("assistant:claude_sdk") is None


def test_a_minted_or_unknown_mode_stores_no_credential() -> None:
    assert stores_credential("claude_sdk", "api_key") is True
    assert stores_credential("codex_cli", "subscription") is True
    assert stores_credential("codex_cli", "bedrock") is False
    assert stores_credential("gemini", "api_key") is False

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
        ("organization_daily_budget_usd", "0"),
        ("organization_daily_budget_usd", "10000.01"),
        ("effort", "extreme"),
    ],
)
def test_settings_refuse_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AssistantSettings(**{field: value})  # type: ignore[arg-type]


def test_the_organization_budget_is_off_until_set() -> None:
    assert AssistantSettings().organization_daily_budget_usd is None
    capped = AssistantSettings(organization_daily_budget_usd=Decimal("150"))
    assert capped.organization_daily_budget_usd == Decimal("150")


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
