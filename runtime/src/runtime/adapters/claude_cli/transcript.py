"""The claude TUI's session log as the `stream-json` events `StreamReader` and `parse_activity`
already read, and as what it says about turns to `SessionSignals` (decision 56).

Pure: lines in, lines and facts out. The caller owns the files, the offsets and the state file,
so a host adapter and the E2B one read a session the same way.

The log is agent-writable, so each event is built anew from named fields. Message content
blocks pass through as `stream-json` carries them, and a `toolUseResult` gives only its
`AskUserQuestion` answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from runtime.adapters._cli.session_signals import (
    ERROR_LIMIT,
    QUESTION_LIMIT,
    QUESTIONS_SHOWN,
    CostState,
    SessionSignals,
    claimed_at,
    clip,
    log_count,
    require_aware,
)
from runtime.adapters.claude_cli.cli import (
    COST_CACHE_CREATE,
    COST_CACHE_READ,
    COST_INPUT,
    COST_MODEL_USAGE,
    COST_OUTPUT,
    COST_TOTAL_USD,
    EVENT_ASSISTANT,
    EVENT_PARENT_TOOL_USE,
    EVENT_REQUEST_ID,
    EVENT_RESULT,
    EVENT_SESSION_ID,
    EVENT_SYSTEM,
    EVENT_TOOL_USE_RESULT,
    EVENT_TYPE,
    EVENT_USER,
    LOG_ANSWERS,
    LOG_API_ERROR,
    LOG_COMMAND_TAGS,
    LOG_COMPACT_SUMMARY,
    LOG_CONTENT,
    LOG_MESSAGE,
    LOG_MESSAGE_ID,
    LOG_META,
    LOG_MODEL,
    LOG_REQUEST_ID,
    LOG_SIDECHAIN,
    LOG_SKIPPED_TYPES,
    LOG_STOP_REASON,
    LOG_SUBTYPE,
    LOG_SYNTHETIC_MODEL,
    LOG_TIMESTAMP,
    LOG_TOOL_USE_RESULT,
    LOG_TURN_DURATION,
    LOG_TYPE_ATTACHMENT,
    LOG_TYPE_COST_STATE,
    LOG_USAGE,
    RESULT_COST,
    RESULT_ERROR_DURING_EXECUTION,
    RESULT_IS_ERROR,
    RESULT_NUM_TURNS,
    RESULT_SUBTYPE,
    RESULT_SUCCESS,
    RESULT_TEXT,
    RESULT_USAGE,
    SUBTYPE_INIT,
    USAGE_CACHE_CREATE,
    USAGE_CACHE_READ,
    USAGE_INPUT,
    USAGE_OUTPUT,
)
from runtime.adapters.claude_cli.stream import parse_event

#: Bumped whenever the same log would translate to different lines. A worker restart rebuilds
#: the translator by replaying the log and skipping what the translated mirror already holds,
#: which is only sound when the code that wrote the mirror is this code.
TRANSCRIPT_VERSION: Final = 1

#: Ours, on a user text event: who typed it. `parse_activity` ignores user text.
FACTORIAL_ORIGIN: Final = "factorial_origin"
ORIGIN_ORCHESTRATOR: Final = "orchestrator"
ORIGIN_HUMAN: Final = "human"
#: `parent_tool_use_id` on a subagent's events, which `StreamReader` then does not count as
#: turns. The log does not say which tool call started the subagent.
SIDECHAIN: Final = "sidechain"

ANSWER_LIMIT: Final = 300
TYPE_NAME_LIMIT: Final = 80
#: The `unknown_types` key for a line whose `type` is missing or not a string.
_NOT_A_TYPE: Final = "<not a string>"


@dataclass(slots=True)
class _Pending:
    """An assistant response whose lines are still arriving."""

    message_id: str
    request_id: str
    sidechain: bool
    model: Any
    content: list[Any] = field(default_factory=list)
    usage: dict[str, int] | None = None
    stop_reason: Any = None


class Transcript:
    """One segment of a session — one launch of the CLI — from its first log line to its end.

    `turn` says whether the launch carried the orchestrator's turn: the CLI writes that prompt
    exactly as it writes a typed one, so only its position (the segment's first user text) tells
    them apart. `cost_base` is the running total at the segment's start, so the result reports
    this segment's spend alone. `skip` drops that many leading output lines: a restarted worker
    replays the log from the segment's start and keeps only what the mirror does not yet hold.
    """

    def __init__(
        self,
        session_id: str,
        *,
        turn: bool,
        cost_base: CostState | None = None,
        skip: int = 0,
    ) -> None:
        self._session_id = session_id
        self._turn = turn
        self._cost_base = cost_base
        self._skip = skip
        #: Lines produced so far, the skipped ones included.
        self.emitted = 0
        #: The latest running total: the base until this segment writes one.
        self.cost_seen = cost_base
        #: Top-level line types this translator does not know, with how often each came.
        self.unknown_types: dict[str, int] = {}
        #: Lines that were not a JSON object.
        self.unreadable = 0
        self._started = False
        self._pending: _Pending | None = None
        self._user_texts = 0
        self._requests: dict[str, None] = {}
        #: Each message's usage by id: a message reopened after another line counts once.
        self._message_usage: dict[str, dict[str, int]] = {}
        self._last_text: str | None = None
        self._error: str | None = None
        self._cost_written = False
        self._turn_ends = 0
        self._missing: dict[str, None] = {}

    def feed(self, line: str) -> list[str]:
        out: list[str] = []
        self._start(out)
        record = parse_event(line)
        if record is None:
            if line.strip():
                self.unreadable += 1
            return out
        kind = record.get(EVENT_TYPE)
        if kind == EVENT_ASSISTANT:
            self._assistant(record, out)
        elif kind == EVENT_USER:
            self._flush(out)
            self._user(record, out)
        elif kind == EVENT_SYSTEM:
            self._flush(out)
            if record.get(LOG_SUBTYPE) == LOG_TURN_DURATION:
                self._turn_ends += 1
        elif kind == LOG_TYPE_COST_STATE:
            self._cost(record)
        elif not isinstance(kind, str):
            self.unknown_types[_NOT_A_TYPE] = self.unknown_types.get(_NOT_A_TYPE, 0) + 1
        elif kind != LOG_TYPE_ATTACHMENT and kind not in LOG_SKIPPED_TYPES:
            name = clip(kind, TYPE_NAME_LIMIT)
            self.unknown_types[name] = self.unknown_types.get(name, 0) + 1
        return out

    def end_segment(self, *, stop_message: str | None = None) -> list[str]:
        """Flush and close with a `result` event. `stop_message` is the last `Stop` hook's
        `last_assistant_message`: the hook can fire before the log holds the turn's last line,
        and a hangup can then cut the line off for good."""
        out: list[str] = []
        self._start(out)
        self._flush(out)
        text = self._error or self._last_text or stop_message or ""
        delta = self._cost_delta()
        if delta is None:
            usage = {
                key: sum(each[key] for each in self._message_usage.values())
                for key in _USAGE_FIELDS
            }
        else:
            usage = _usage_of(delta)
        result: dict[str, Any] = {
            EVENT_TYPE: EVENT_RESULT,
            RESULT_SUBTYPE: RESULT_ERROR_DURING_EXECUTION if self._error else RESULT_SUCCESS,
            RESULT_IS_ERROR: self._error is not None,
            RESULT_TEXT: text,
            EVENT_SESSION_ID: self._session_id,
            RESULT_NUM_TURNS: len(self._requests),
            RESULT_USAGE: usage,
        }
        if delta is not None:
            # A float because that is what `parse_activity` reads; `StreamReader` goes back to
            # Decimal through its str().
            result[RESULT_COST] = float(delta.usd)
        self._emit(out, result)
        return out

    def missing(self) -> tuple[str, ...]:
        """What this translator reads off the log and the segment never carried. Meaningful once
        the segment ended by the CLI's own exit: a killed CLI writes no cost-state."""
        found = list(self._missing)
        if not self._cost_written:
            found.append(f"no {LOG_TYPE_COST_STATE} line")
        if self._requests and not self._turn_ends:
            found.append(f"no {EVENT_SYSTEM} {LOG_TURN_DURATION} line")
        return tuple(found)

    def _start(self, out: list[str]) -> None:
        if not self._started:
            self._started = True
            self._emit(
                out,
                {
                    EVENT_TYPE: EVENT_SYSTEM,
                    RESULT_SUBTYPE: SUBTYPE_INIT,
                    EVENT_SESSION_ID: self._session_id,
                },
            )

    def _emit(self, out: list[str], event: Mapping[str, Any]) -> None:
        self.emitted += 1
        if self.emitted > self._skip:
            # An escaped lone surrogate decodes back into one, which no state file can hold.
            line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            out.append(line.encode("utf-8", "replace").decode("utf-8"))

    def _note(self, problem: str) -> None:
        self._missing[problem] = None

    def _assistant(self, record: Mapping[str, Any], out: list[str]) -> None:
        message = record.get(LOG_MESSAGE)
        if not isinstance(message, Mapping):
            self._note(f"an {EVENT_ASSISTANT} line without a {LOG_MESSAGE}")
            return
        if message.get(LOG_MODEL) == LOG_SYNTHETIC_MODEL:
            self._flush(out)
            if record.get(LOG_API_ERROR):
                self._error = clip(_texts(message.get(LOG_CONTENT)), ERROR_LIMIT) or "API error"
            return
        message_id = message.get(LOG_MESSAGE_ID)
        if not isinstance(message_id, str) or not message_id:
            self._note(f"an {EVENT_ASSISTANT} line without a {LOG_MESSAGE}.{LOG_MESSAGE_ID}")
            return
        if self._pending is not None and self._pending.message_id != message_id:
            self._flush(out)
        if self._pending is None:
            request_id = record.get(LOG_REQUEST_ID)
            self._pending = _Pending(
                message_id=message_id,
                request_id=request_id if isinstance(request_id, str) and request_id else message_id,
                sidechain=record.get(LOG_SIDECHAIN) is True,
                model=message.get(LOG_MODEL),
            )
        pending = self._pending
        content = message.get(LOG_CONTENT)
        if isinstance(content, list):
            pending.content.extend(block for block in content if isinstance(block, Mapping))
        usage = message.get(LOG_USAGE)
        if isinstance(usage, Mapping):
            pending.usage = {key: log_count(usage.get(key)) for key in _USAGE_FIELDS}
        pending.stop_reason = message.get(LOG_STOP_REASON)

    def _flush(self, out: list[str]) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if not pending.sidechain:
            self._requests[pending.request_id] = None
            text = _last_text(pending.content)
            if text:
                self._last_text = text
            self._error = None  # a later response reached the API, so the last turn did not fail
        if pending.usage is None:
            self._note(f"an {EVENT_ASSISTANT} message without {LOG_USAGE}")
        else:
            self._message_usage[pending.message_id] = pending.usage
        self._emit(
            out,
            {
                EVENT_TYPE: EVENT_ASSISTANT,
                LOG_MESSAGE: {
                    LOG_MESSAGE_ID: pending.message_id,
                    "role": EVENT_ASSISTANT,
                    LOG_MODEL: pending.model,
                    LOG_CONTENT: pending.content,
                    LOG_USAGE: pending.usage or {},
                    LOG_STOP_REASON: pending.stop_reason,
                },
                EVENT_SESSION_ID: self._session_id,
                EVENT_REQUEST_ID: pending.request_id,
                EVENT_PARENT_TOOL_USE: SIDECHAIN if pending.sidechain else None,
            },
        )

    def _user(self, record: Mapping[str, Any], out: list[str]) -> None:
        if record.get(LOG_META) or record.get(LOG_COMPACT_SUMMARY):
            return
        message = record.get(LOG_MESSAGE)
        content = message.get(LOG_CONTENT) if isinstance(message, Mapping) else None
        if isinstance(content, str):
            blocks: Sequence[Mapping[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [block for block in content if isinstance(block, Mapping)]
        else:
            return
        sidechain = record.get(LOG_SIDECHAIN) is True
        results = [block for block in blocks if block.get("type") == "tool_result"]
        if results:
            event: dict[str, Any] = {
                EVENT_TYPE: EVENT_USER,
                LOG_MESSAGE: {"role": EVENT_USER, LOG_CONTENT: results},
                EVENT_SESSION_ID: self._session_id,
                EVENT_PARENT_TOOL_USE: SIDECHAIN if sidechain else None,
            }
            answers = _answers(record.get(LOG_TOOL_USE_RESULT))
            if answers:
                event[EVENT_TOOL_USE_RESULT] = {LOG_ANSWERS: answers}
            self._emit(out, event)
            return
        # A subagent's prompt is the agent's own words; a pasted image stays out of the stream.
        texts = [
            b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if sidechain or not texts or texts[0].lstrip().startswith(LOG_COMMAND_TAGS):
            return
        self._user_texts += 1
        orchestrator = self._turn and self._user_texts == 1
        self._emit(
            out,
            {
                EVENT_TYPE: EVENT_USER,
                LOG_MESSAGE: {
                    "role": EVENT_USER,
                    LOG_CONTENT: [{"type": "text", "text": text} for text in texts],
                },
                EVENT_SESSION_ID: self._session_id,
                FACTORIAL_ORIGIN: ORIGIN_ORCHESTRATOR if orchestrator else ORIGIN_HUMAN,
            },
        )

    def _cost(self, record: Mapping[str, Any]) -> None:
        usd = _decimal(record.get(COST_TOTAL_USD))
        if usd is None:
            self._note(f"a {LOG_TYPE_COST_STATE} line without {COST_TOTAL_USD}")
            return
        totals = dict.fromkeys(_COST_FIELDS, 0)
        models = record.get(COST_MODEL_USAGE)
        if isinstance(models, Mapping):
            for usage in models.values():
                if isinstance(usage, Mapping):
                    for key in totals:
                        totals[key] += log_count(usage.get(key))
        # Written twice on `/exit`; the later one is the truer.
        self.cost_seen = CostState(usd=usd, **{_COST_FIELDS[k]: v for k, v in totals.items()})
        self._cost_written = True

    def _cost_delta(self) -> CostState | None:
        seen = self.cost_seen
        if not self._cost_written or seen is None:
            return None
        base = self._cost_base or CostState(usd=Decimal(0))
        delta = {name: getattr(seen, name) - getattr(base, name) for name in CostState.model_fields}
        if any(value < 0 for value in delta.values()):
            # A running total that went down is not this session's: no cost beats a negative one.
            self._note(f"a {LOG_TYPE_COST_STATE} below the segment's starting total")
            return None
        return CostState(**delta)


#: cost-state's `modelUsage` keys, and the `CostState` field each sums into.
_COST_FIELDS: Final = {
    COST_INPUT: "input_tokens",
    COST_OUTPUT: "output_tokens",
    COST_CACHE_READ: "cache_read_input_tokens",
    COST_CACHE_CREATE: "cache_creation_input_tokens",
}
_USAGE_FIELDS: Final = (USAGE_INPUT, USAGE_OUTPUT, USAGE_CACHE_READ, USAGE_CACHE_CREATE)


def _usage_of(cost: CostState) -> dict[str, int]:
    return {
        USAGE_INPUT: cost.input_tokens,
        USAGE_OUTPUT: cost.output_tokens,
        USAGE_CACHE_READ: cost.cache_read_input_tokens,
        USAGE_CACHE_CREATE: cost.cache_creation_input_tokens,
    }


def claude_log_signals(signals: SessionSignals, line: str, *, observed_at: datetime) -> None:
    """What one session-log line says about a turn: `turn_duration` closes every turn, a failed
    one included, and a `toolUseResult` carrying answers answers a question."""
    require_aware(observed_at)
    record = parse_event(line)
    if record is None:
        return
    at = claimed_at(record.get(LOG_TIMESTAMP), observed_at)
    kind = record.get(EVENT_TYPE)
    if kind == EVENT_SYSTEM and record.get(LOG_SUBTYPE) == LOG_TURN_DURATION:
        signals.note_turn_end(at)
    elif kind == EVENT_USER:
        result = record.get(LOG_TOOL_USE_RESULT)
        if isinstance(result, Mapping) and isinstance(result.get(LOG_ANSWERS), Mapping):
            signals.note_answer(at, _result_ids(record))


def _answers(result: Any) -> dict[str, str]:
    answers = result.get(LOG_ANSWERS) if isinstance(result, Mapping) else None
    if not isinstance(answers, Mapping):
        return {}
    kept = [(q, a) for q, a in answers.items() if isinstance(q, str) and isinstance(a, str)]
    return {
        clip(question, QUESTION_LIMIT): clip(answer, ANSWER_LIMIT)
        for question, answer in kept[:QUESTIONS_SHOWN]
    }


def _result_ids(record: Mapping[str, Any]) -> set[str]:
    message = record.get(LOG_MESSAGE)
    content = message.get(LOG_CONTENT) if isinstance(message, Mapping) else None
    return {
        block["tool_use_id"]
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, Mapping) and isinstance(block.get("tool_use_id"), str)
    }


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    )


def _last_text(content: Sequence[Any]) -> str | None:
    for block in reversed(content):
        text = block.get("text") if block.get("type") == "text" else None
        if isinstance(text, str) and text.strip():
            return text
    return None


def _decimal(value: Any) -> Decimal | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None
