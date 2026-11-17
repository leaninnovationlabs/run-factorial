"""An image in a turn reaches the Claude SDK as a content block beside the person's words."""

from __future__ import annotations

import base64
from datetime import timedelta
from decimal import Decimal

from pydantic import SecretStr

from runtime.assistant.adapters.claude_sdk.backend import _asked
from runtime.assistant.contract import ChatImage, ChatTurnSpec, TurnLimits

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def _spec(**overrides: object) -> ChatTurnSpec:
    base: dict[str, object] = dict(
        turn_id="7",
        conversation_id="3",
        model="claude-opus-5-5",
        effort="medium",
        auth_mode="api_key",
        credential=SecretStr("sk-test"),
        system_prompt="be brief",
        message="What is wrong here?",
        limits=TurnLimits(
            max_steps=25, max_budget_usd=Decimal("1.00"), wall_clock=timedelta(minutes=5)
        ),
    )
    return ChatTurnSpec(**(base | overrides))  # type: ignore[arg-type]


def test_words_alone_are_asked_as_the_string_they_always_were() -> None:
    assert _asked(_spec()) == "What is wrong here?"


async def test_an_image_is_one_structured_message_the_words_first() -> None:
    spec = _spec(images=(ChatImage(name="image-1", media_type="image/png", content=PNG),))
    asked = _asked(spec)
    assert not isinstance(asked, str)
    messages = [message async for message in asked]
    assert messages == [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is wrong here?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(PNG).decode(),
                        },
                    },
                ],
            },
            "parent_tool_use_id": None,
        }
    ]
