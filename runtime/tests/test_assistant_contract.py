from datetime import timedelta
from decimal import Decimal
from typing import Any

import claude_agent_sdk
import pytest
from fake_toolbox import FakeToolbox
from pydantic import SecretStr, TypeAdapter, ValidationError

from runtime.assistant.adapters import TAKES_IMAGES, create_backend
from runtime.assistant.adapters.bedrock_responses import BedrockResponsesBackend
from runtime.assistant.adapters.claude_sdk import ClaudeSdkBackend
from runtime.assistant.adapters.codex_cli.backend import CodexCliBackend
from runtime.assistant.contract import (
    ChatEvent,
    ChatImage,
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


PNG = b"\x89PNG\r\n\x1a\n" + b"pixels-that-must-not-leak"


def test_an_image_is_dumped_and_printed_as_its_size_never_its_bytes() -> None:
    spec = _spec(images=(ChatImage(name="image-1", media_type="image/png", content=PNG),))
    assert spec.model_dump()["images"] == (
        {"name": "image-1", "media_type": "image/png", "content": len(PNG)},
    )
    assert "pixels-that-must-not-leak" not in repr(spec)
    assert "pixels-that-must-not-leak" not in spec.model_dump_json()


def test_an_image_s_name_is_one_the_adapter_can_write_as_a_file() -> None:
    for name in ("../etc/passwd", "a b.png", "", "Image-1"):
        with pytest.raises(ValidationError):
            ChatImage(name=name, media_type="image/png", content=PNG)
    with pytest.raises(ValidationError):
        ChatImage(name="image-1", media_type="image/svg+xml", content=PNG)  # type: ignore[arg-type]


def test_the_table_of_backends_that_take_images_is_each_class_s_own_word() -> None:
    """The API refuses an image for a backend that takes none, and reads this table to do it
    without loading a vendor module. The classes are the authority."""
    classes = {
        "claude_sdk": ClaudeSdkBackend,
        "codex_cli": CodexCliBackend,
        "bedrock_responses": BedrockResponsesBackend,
    }
    assert {kind for kind, backend in classes.items() if backend.supports_images} == TAKES_IMAGES
    assert sorted(TAKES_IMAGES) == ["claude_sdk", "codex_cli"]
