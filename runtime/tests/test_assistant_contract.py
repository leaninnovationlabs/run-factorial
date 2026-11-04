from datetime import timedelta
from decimal import Decimal
from typing import Any

import claude_agent_sdk
import pytest
from fake_toolbox import FakeToolbox
from pydantic import SecretStr, TypeAdapter, ValidationError

from runtime.assistant.adapters import create_backend
from runtime.assistant.contract import (
    ChatEvent,
    ChatToolbox,
    ChatTurnSpec,
    TextDelta,
    ToolOutcome,
    TurnLimits,
    TurnStop,
    check_model_id,
)
from runtime.contract import AdapterUnavailable


def _spec(**overrides: Any) -> ChatTurnSpec:
    base: dict[str, Any] = dict(
        turn_id="7",
        conversation_id="3",
        model="claude-opus-5-5",
        effort="medium",
        auth_mode="api_key",
        credential=SecretStr("sk-test"),
        system_prompt="be brief",
        message="hello",
        limits=TurnLimits(
            max_steps=25, max_budget_usd=Decimal("1.00"), wall_clock=timedelta(minutes=5)
        ),
    )
    return ChatTurnSpec(**(base | overrides))


@pytest.mark.parametrize("alias", ["opus", "Sonnet", "medium", "claude-opus-latest", " "])
def test_model_alias_is_refused(alias):
    with pytest.raises(ValueError):
        check_model_id(alias)
    with pytest.raises(ValidationError):
        _spec(model=alias)


def test_concrete_model_id_passes():
    assert check_model_id("claude-opus-5-5") == "claude-opus-5-5"


def test_events_round_trip_through_the_discriminator():
    event = TypeAdapter(ChatEvent).validate_python(
        {"kind": "text_delta", "block": "0:0", "text": "hi"}
    )
    assert event == TextDelta(kind="text_delta", block="0:0", text="hi")


def test_spec_never_prints_its_credentials():
    text = repr(_spec())
    assert "sk-test" not in text


def test_unknown_backend_kind_is_refused():
    with pytest.raises(AdapterUnavailable, match="claude_sdk"):
        create_backend("nope")


async def test_claude_sdk_preflight_refuses_an_sdk_it_was_not_verified_against(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(claude_agent_sdk, "__version__", "0.0.0")
    backend = create_backend("claude_sdk", work_root=tmp_path)
    with pytest.raises(AdapterUnavailable, match=r"claude-agent-sdk 0\.0\.0 is installed"):
        await backend.preflight()


def test_turn_stop_values_are_stable():
    assert [s.value for s in TurnStop] == ["end", "budget", "steps", "cancelled"]


def test_a_toolbox_satisfies_the_protocol_and_its_outcome_is_frozen() -> None:
    assert isinstance(FakeToolbox(), ChatToolbox)
    outcome = ToolOutcome(ok=True, text="x")
    with pytest.raises(ValidationError):
        outcome.ok = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ToolOutcome(ok=True, text="x", structured={})  # type: ignore[call-arg]
