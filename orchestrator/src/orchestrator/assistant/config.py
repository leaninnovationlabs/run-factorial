"""The assistant's settings document, stored in `app_setting` under the key `assistant`."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.assistant.vocabulary import BACKENDS
from orchestrator.factorial.bedrock_token import DEFAULT_REGION
from runtime.assistant.contract import check_model_id

__all__ = ["AssistantSettings"]


class AssistantSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    backend: str = "claude_sdk"
    auth_mode: str = "api_key"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high"] = "medium"
    turn_budget_usd: Decimal = Field(default=Decimal("1.00"), gt=0, le=50)
    max_steps: int = Field(default=25, ge=1, le=100)
    daily_budget_usd: Decimal = Field(default=Decimal("20.00"), gt=0, le=1000)
    #: Bedrock Runtime region when `backend` is `codex_cli` and `auth_mode` is `bedrock`.
    bedrock_region: str = DEFAULT_REGION

    @field_validator("model")
    @classmethod
    def _concrete_model(cls, value: str) -> str:
        return check_model_id(value)

    @model_validator(mode="after")
    def _known_backend_and_mode(self) -> Self:
        vocab = BACKENDS.get(self.backend)
        if vocab is None:
            raise ValueError(f"unknown backend {self.backend!r}; one of {sorted(BACKENDS)}")
        if self.auth_mode not in vocab.credentials:
            raise ValueError(
                f"backend {self.backend!r} has no auth mode {self.auth_mode!r}; "
                f"one of {sorted(vocab.credentials)}"
            )
        return self
