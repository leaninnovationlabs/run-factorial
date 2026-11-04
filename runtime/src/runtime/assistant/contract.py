"""Vendor-neutral chat-backend contract for the in-app assistant (tech-stack.md decision 63).

The orchestrator imports this module and `runtime.assistant.adapters.create_backend`, never a
vendor module. As in `runtime.contract`, nothing here may assume a CLI, a subprocess or a file
path: an adapter over a vendor HTTP API must fit behind it without anything above it changing.

Scope: one turn of one conversation, as a stream of `ChatEvent`s. Who may send, what a turn may
cost a person, and what the tools may change are the orchestrator's and have no representation
here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from pydantic import Field, SecretStr, field_validator

from runtime.contract import _Frozen

__all__ = [
    "MODEL_ALIASES",
    "BackendStateStore",
    "ChatBackend",
    "ChatEvent",
    "ChatToolbox",
    "ChatTurnSpec",
    "HistoryItem",
    "TextDelta",
    "TextDone",
    "ToolCall",
    "ToolDefinition",
    "ToolOutcome",
    "ToolResult",
    "TurnDone",
    "TurnFailed",
    "TurnFailure",
    "TurnLimits",
    "TurnStop",
    "Usage",
    "check_model_id",
]

#: Names a vendor resolves to a model of its choosing. A conversation must run on the model a
#: person picked in Settings, and the cost a turn reports is only true of a concrete one.
MODEL_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "opus",
        "sonnet",
        "haiku",
        "fable",
        "default",
        "best",
        "small",
        "medium",
        "large",
        "extra-large",
        "xl",
    }
)


def check_model_id(model: str) -> str:
    value = model.strip()
    if not value or value.lower() in MODEL_ALIASES or value.lower().endswith("-latest"):
        raise ValueError(
            f"model {model!r} is an alias; name a concrete model id such as 'claude-opus-5-5'"
        )
    return value


class TurnStop(StrEnum):
    """Why a turn ended without failing. `BUDGET` and `STEPS` are the per-turn limits holding."""

    END = "end"
    BUDGET = "budget"
    STEPS = "steps"
    CANCELLED = "cancelled"


class TurnFailure(StrEnum):
    """Why a turn failed — a closed set, so the caller never sniffs adapter wording.

    - `AUTH` — the stored credential was refused, or its account cannot pay.
    - `RATE_LIMIT` — the vendor throttled the credential.
    - `REFUSAL` — the model declined to answer.
    - `CONFIG` — the backend could not be held to its containment, or the spec asks for
      something it will never do.
    - `TIMEOUT` — the turn outlived `TurnLimits.wall_clock`.
    - `INFRASTRUCTURE` — anything else between here and the vendor.
    """

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    REFUSAL = "refusal"
    CONFIG = "config"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


class TurnLimits(_Frozen):
    #: Model calls in one turn, each tool round trip included.
    max_steps: int = Field(ge=1, le=100)
    max_budget_usd: Decimal = Field(gt=0)
    #: Enforced by the caller, which stops iterating a turn that outlives it and records
    #: `TurnFailure.TIMEOUT`; a backend only has to release its resources when stopped.
    wall_clock: timedelta


class HistoryItem(_Frozen):
    role: Literal["user", "assistant"]
    text: str


class ChatTurnSpec(_Frozen):
    """Everything one turn needs but its tools, which are objects and go to `ChatBackend.turn`
    beside the state store. The credential is `SecretStr`, so a logged spec never prints it."""

    turn_id: str
    conversation_id: str
    model: str
    effort: Literal["low", "medium", "high"] | None
    auth_mode: Literal["api_key", "subscription", "bedrock"]
    credential: SecretStr
    system_prompt: str
    #: The person's text, with any page context already prepended as data.
    message: str
    #: A neutral transcript, for a backend that keeps no session of its own.
    history: tuple[HistoryItem, ...] = ()
    #: The session a backend with `supports_resume` continues, as its last `Usage` reported it.
    resume_session: str | None = None
    limits: TurnLimits
    #: Commercial or GovCloud Bedrock region when `auth_mode` is `bedrock`.
    bedrock_region: str | None = None

    @field_validator("model")
    @classmethod
    def _concrete_model(cls, value: str) -> str:
        return check_model_id(value)


# --------------------------------------------------------------------------- events


class TextDelta(_Frozen):
    """Part of a text block. Every delta and the `TextDone` of one block share `block`."""

    kind: Literal["text_delta"] = "text_delta"
    block: str
    text: str


class TextDone(_Frozen):
    """A text block's whole text, which replaces whatever its deltas built."""

    kind: Literal["text_done"] = "text_done"
    block: str
    text: str


class ToolCall(_Frozen):
    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    #: The tool's own name, without any server prefix the vendor adds.
    name: str
    input: Mapping[str, Any]


class ToolResult(_Frozen):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    ok: bool
    #: The result's text, capped, never the whole payload.
    summary: str
    structured: Mapping[str, Any] | None = None


class Usage(_Frozen):
    kind: Literal["usage"] = "usage"
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    #: False when `cost_usd` is the backend's own estimate rather than the vendor's bill.
    cost_reported: bool
    #: What the next turn passes as `ChatTurnSpec.resume_session`.
    session_id: str | None


class TurnDone(_Frozen):
    kind: Literal["turn_done"] = "turn_done"
    stop: TurnStop


class TurnFailed(_Frozen):
    kind: Literal["turn_failed"] = "turn_failed"
    reason: TurnFailure
    message: str


ChatEvent = Annotated[
    TextDelta | TextDone | ToolCall | ToolResult | Usage | TurnDone | TurnFailed,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- tools


class ToolDefinition(_Frozen):
    """One tool the turn's model may call, as a backend offers it to its vendor."""

    name: str
    title: str
    description: str
    #: JSON Schema of the arguments object.
    input_schema: Mapping[str, Any]
    read_only: bool
    destructive: bool


class ToolOutcome(_Frozen):
    ok: bool
    #: What the model reads back: the result, or why the call was refused.
    text: str


# --------------------------------------------------------------------------- protocols


class BackendStateStore(Protocol):
    """An append/load log a backend keeps its own session in, keyed by a string it chooses.
    The orchestrator stores it durably; entries are opaque JSON it never interprets."""

    async def append(self, key: str, entries: Sequence[Mapping[str, Any]]) -> None: ...

    async def load(self, key: str) -> list[Mapping[str, Any]] | None:
        """Every entry appended under `key`, in order, or None for a key never written."""
        ...


@runtime_checkable
class ChatToolbox(Protocol):
    """A turn's tools, called in the process that runs the backend. `call` answers a tool's own
    failure as `ok=False` and never raises for it."""

    def definitions(self) -> Sequence[ToolDefinition]: ...

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome: ...


@runtime_checkable
class ChatBackend(Protocol):
    """One vendor's way of running a chat turn. Every capability is a ClassVar: it cannot vary
    per turn."""

    kind: ClassVar[str]
    #: The vendor SDK, CLI or API version this backend was verified against.
    pinned_version: ClassVar[str]
    #: Token deltas (`TextDelta`), or whole text blocks only.
    supports_partial_text: ClassVar[bool]
    #: Keeps its own session through `BackendStateStore`; False means it reads `history`.
    supports_resume: ClassVar[bool]
    supports_cancel: ClassVar[bool]
    supports_effort: ClassVar[bool]

    async def preflight(self) -> None:
        """Raise `AdapterUnavailable` with the reason when this backend cannot run a turn."""
        ...

    def turn(
        self, spec: ChatTurnSpec, state: BackendStateStore, tools: ChatToolbox
    ) -> AsyncIterator[ChatEvent]:
        """Run one turn, its tool calls answered by `tools`. The last event is `TurnDone` or
        `TurnFailed`. Declared non-async so an implementation can be an async generator."""
        ...

    async def cancel(self, turn_id: str) -> None:
        """Stop a live turn, which then ends `TurnDone(CANCELLED)`. A no-op for a turn that is
        not live."""
        ...
