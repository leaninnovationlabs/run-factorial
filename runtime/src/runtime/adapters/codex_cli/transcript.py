"""The codex TUI's rollout as the `exec --json` events `StreamReader` and `parse_activity`
already read, and as what it says about turns to `SessionSignals` (decision 56).

Pure: lines in, lines and facts out. The caller owns the files, the offsets and the state file,
so a host adapter and the E2B one read a session the same way.

`task_started` opens a turn, each `item_completed` becomes an item, the turn's last
`token_usage_record` is its usage, and `task_complete` or `turn_aborted` closes it; a
`task_complete` error is a failed turn. A `request_user_input` call and its output are
`response_item`s: the translator skips them, and only the signals read them. The rollout is
agent-writable, so each event is built anew from named fields.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from runtime.adapters._cli.session_signals import (
    ERROR_LIMIT,
    CostState,
    SessionSignals,
    claimed_at,
    clip,
    json_object,
    log_count,
    require_aware,
)
from runtime.adapters.claude_cli.transcript import (
    FACTORIAL_ORIGIN,
    ORIGIN_HUMAN,
    ORIGIN_ORCHESTRATOR,
    TYPE_NAME_LIMIT,
)
from runtime.adapters.codex_cli.cli import (
    ASK_ANSWERS,
    ASK_SELF_ANSWER_SEC,
    CALL_ARGUMENTS,
    CALL_ID,
    CALL_NAME,
    CALL_OUTPUT,
    CONTENT_AGENT_TEXT,
    CONTENT_USER_TEXT,
    EVENT_ERROR,
    EVENT_ITEM,
    EVENT_ITEM_COMPLETED,
    EVENT_THREAD_ID,
    EVENT_THREAD_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
    EVENT_TYPE,
    EVENT_USAGE,
    ITEM_AGENT_MESSAGE,
    ITEM_AGGREGATED_OUTPUT,
    ITEM_COMMAND,
    ITEM_COMMAND_EXECUTION,
    ITEM_EXIT_CODE,
    ITEM_FUNCTION_CALL,
    ITEM_FUNCTION_CALL_OUTPUT,
    ITEM_ID,
    ITEM_STATUS,
    ITEM_TEXT,
    ITEM_TYPE,
    ITEM_USER_MESSAGE,
    KEY_MESSAGE,
    MSG_ERROR,
    MSG_ITEM_COMPLETED,
    MSG_TASK_COMPLETE,
    MSG_TASK_STARTED,
    MSG_TURN_ABORTED,
    PAYLOAD_ITEM,
    PAYLOAD_TYPE,
    RECORD_EVENT_MSG,
    RECORD_RESPONSE_ITEM,
    RECORD_SESSION_META,
    RECORD_SKIPPED_TYPES,
    RECORD_TOKEN_USAGE,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TIMESTAMP,
    ROLLOUT_TYPE,
    TASK_ERROR,
    TOOL_REQUEST_USER_INPUT,
    TURN_ITEM_AGENT,
    TURN_ITEM_COMMAND,
    TURN_ITEM_CONTENT,
    TURN_ITEM_USER,
    USAGE_CACHE_WRITE_INPUT,
    USAGE_CACHED_INPUT,
    USAGE_INPUT,
    USAGE_OUTPUT,
    USAGE_TURN,
)

#: Bumped whenever the same rollout would translate to different lines. A worker restart
#: rebuilds the translator by replaying the rollout and skipping what the translated mirror
#: already holds, which is only sound when the code that wrote the mirror is this code.
TRANSCRIPT_VERSION: Final = 1

#: A failed turn whose error carries no message.
TURN_FAILED: Final = "the turn failed"
ID_LIMIT: Final = 200
STATUS_LIMIT: Final = 40
NO_TURN_END: Final = f"a {MSG_TASK_STARTED} with no {MSG_TASK_COMPLETE}"
NO_TURN_USAGE: Final = f"a turn with no {RECORD_TOKEN_USAGE} line"
#: The `unknown_types` key for a line whose `type` is missing or not a string.
_NOT_A_TYPE: Final = "<not a string>"
_USAGE_FIELDS: Final = (USAGE_INPUT, USAGE_CACHED_INPUT, USAGE_CACHE_WRITE_INPUT, USAGE_OUTPUT)


class RolloutTranscript:
    """One segment of a session — one launch of the TUI — from its first rollout line to its end.

    `turn` says whether the launch carried the orchestrator's turn: the TUI logs that prompt
    exactly as it logs a typed one, so only its position (the segment's first user message)
    tells them apart. `skip` drops that many leading output lines: a restarted worker replays
    the rollout from the segment's start and keeps only what the mirror does not yet hold.
    A turn's tokens are its own, so a segment needs no base, and codex reports no cost.
    """

    def __init__(self, thread_id: str, *, turn: bool, skip: int = 0) -> None:
        self._thread_id = thread_id
        self._turn = turn
        self._skip = skip
        #: Lines produced so far, the skipped ones included.
        self.emitted = 0
        #: Top-level line types this translator does not know, with how often each came.
        self.unknown_types: dict[str, int] = {}
        #: Lines that were not a JSON object.
        self.unreadable = 0
        self._started = False
        self._open = False
        self._usage: dict[str, int] | None = None
        #: Turns opened, and those of them that ended normally.
        self._turns = 0
        self._ended = 0
        self._user_messages = 0
        self._missing: dict[str, None] = {}

    @property
    def cost_seen(self) -> CostState | None:
        return None

    @property
    def usage_due(self) -> bool:
        """Whether the segment owes a usage figure: it had no turn, or one that ended normally.
        Codex logs none for a failed or an aborted turn, so a segment of only those owes none."""
        return self._turns == 0 or self._ended > 0

    def feed(self, line: str) -> list[str]:
        out: list[str] = []
        self._start(out)
        record = json_object(line)
        if record is None:
            if line.strip():
                self.unreadable += 1
            return out
        kind = record.get(ROLLOUT_TYPE)
        payload = record.get(ROLLOUT_PAYLOAD)
        payload = payload if isinstance(payload, Mapping) else {}
        if kind == RECORD_EVENT_MSG:
            self._event(payload, out)
        elif kind == RECORD_TOKEN_USAGE:
            usage = _usage(payload.get(USAGE_TURN))
            if self._open and usage is not None:
                self._usage = usage  # the turn's running total: the last one is the turn's
        elif not isinstance(kind, str):
            self.unknown_types[_NOT_A_TYPE] = self.unknown_types.get(_NOT_A_TYPE, 0) + 1
        elif kind != RECORD_SESSION_META and kind not in RECORD_SKIPPED_TYPES:
            name = clip(kind, TYPE_NAME_LIMIT)
            self.unknown_types[name] = self.unknown_types.get(name, 0) + 1
        return out

    def end_segment(self, *, stop_message: str | None = None) -> list[str]:
        """Close a turn the segment cut off, with the tokens it had. The `exec --json` dialect has
        no closing `result` line, and the turn's closing words already came as its agent
        message, so `stop_message` is not needed."""
        out: list[str] = []
        self._start(out)
        if self._open:
            self._missing[NO_TURN_END] = None
            self._close(out)
        return out

    def missing(self) -> tuple[str, ...]:
        """What this translator reads off the rollout and the segment never carried. Meaningful
        once the segment ended by the TUI's own exit. A failed or aborted turn writes no
        `token_usage_record`, so only a turn that ended normally is held to one."""
        found = dict(self._missing)
        if self._open:
            found[NO_TURN_END] = None
        return tuple(found)

    def _start(self, out: list[str]) -> None:
        if not self._started:
            self._started = True
            self._emit(out, {EVENT_TYPE: EVENT_THREAD_STARTED, EVENT_THREAD_ID: self._thread_id})

    def _emit(self, out: list[str], event: Mapping[str, Any]) -> None:
        self.emitted += 1
        if self.emitted > self._skip:
            # An escaped lone surrogate decodes back into one, which no state file can hold.
            line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            out.append(line.encode("utf-8", "replace").decode("utf-8"))

    def _event(self, payload: Mapping[str, Any], out: list[str]) -> None:
        kind = payload.get(PAYLOAD_TYPE)
        if kind == MSG_TASK_STARTED:
            if self._open:
                self._missing[NO_TURN_END] = None
                self._close(out)
            self._open = True
            self._turns += 1
            self._emit(out, {EVENT_TYPE: EVENT_TURN_STARTED})
        elif kind == MSG_ITEM_COMPLETED:
            item = payload.get(PAYLOAD_ITEM)
            if isinstance(item, Mapping):
                self._item(item, out)
        elif kind == MSG_TASK_COMPLETE:
            failure = turn_failure(payload)
            if self._open and failure is None:
                self._ended += 1
                if self._usage is None:
                    self._missing[NO_TURN_USAGE] = None
            self._close(out)
            if failure is not None:
                # After the turn's close: `parse_activity` takes the last terminal event as the
                # outcome.
                self._emit(
                    out, {EVENT_TYPE: EVENT_TURN_FAILED, EVENT_ERROR: {KEY_MESSAGE: failure}}
                )
        elif kind == MSG_TURN_ABORTED:
            self._close(out)
        elif kind == MSG_ERROR:
            message = payload.get(KEY_MESSAGE)
            if isinstance(message, str) and message.strip():
                self._emit(out, {EVENT_TYPE: EVENT_ERROR, KEY_MESSAGE: clip(message, ERROR_LIMIT)})

    def _close(self, out: list[str]) -> None:
        """Every opened turn is closed with what usage it had, so a turn that failed, was aborted
        or was cut off still counts the tokens it spent."""
        if not self._open:
            return
        event: dict[str, Any] = {EVENT_TYPE: EVENT_TURN_COMPLETED}
        if self._usage is not None:
            event[EVENT_USAGE] = self._usage
        self._open, self._usage = False, None
        self._emit(out, event)

    def _item(self, item: Mapping[str, Any], out: list[str]) -> None:
        kind = item.get(ITEM_TYPE)
        item_id = _string(item.get(ITEM_ID), ID_LIMIT)
        if kind == TURN_ITEM_USER:
            self._user_messages += 1
            orchestrator = self._turn and self._user_messages == 1
            self._emit(
                out,
                {
                    EVENT_TYPE: EVENT_ITEM_COMPLETED,
                    EVENT_ITEM: {
                        ITEM_ID: item_id,
                        ITEM_TYPE: ITEM_USER_MESSAGE,
                        ITEM_TEXT: "\n".join(_texts(item, CONTENT_USER_TEXT)),
                    },
                    FACTORIAL_ORIGIN: ORIGIN_ORCHESTRATOR if orchestrator else ORIGIN_HUMAN,
                },
            )
        elif kind == TURN_ITEM_AGENT:
            text = "".join(_texts(item, CONTENT_AGENT_TEXT))
            self._emit(
                out,
                {
                    EVENT_TYPE: EVENT_ITEM_COMPLETED,
                    EVENT_ITEM: {ITEM_ID: item_id, ITEM_TYPE: ITEM_AGENT_MESSAGE, ITEM_TEXT: text},
                },
            )
        elif kind == TURN_ITEM_COMMAND:
            output = item.get(ITEM_AGGREGATED_OUTPUT)
            self._emit(
                out,
                {
                    EVENT_TYPE: EVENT_ITEM_COMPLETED,
                    EVENT_ITEM: {
                        ITEM_ID: item_id,
                        ITEM_TYPE: ITEM_COMMAND_EXECUTION,
                        ITEM_COMMAND: _command(item.get(ITEM_COMMAND)),
                        ITEM_AGGREGATED_OUTPUT: output if isinstance(output, str) else "",
                        ITEM_EXIT_CODE: _exit_code(item.get(ITEM_EXIT_CODE)),
                        ITEM_STATUS: _string(item.get(ITEM_STATUS), STATUS_LIMIT),
                    },
                },
            )


def codex_log_signals(
    signals: SessionSignals, line: str, /, *, observed_at: datetime, ask_tool: bool = False
) -> None:
    """What one rollout line says about a turn. Codex fires no hook for a failed or an aborted
    turn, so `task_complete` (with its error, if any) and `turn_aborted` are the only word on
    those, and the fallback for a `Stop` hook that did not fire. Nor for its question box:
    with `ask_tool` a `request_user_input` call idles the session on its question until its
    output, and one refused or answered with nothing makes the turn's end idle on it."""
    require_aware(observed_at)
    record = json_object(line)
    if record is None:
        return
    payload = record.get(ROLLOUT_PAYLOAD)
    if not isinstance(payload, Mapping):
        return
    at = claimed_at(record.get(ROLLOUT_TIMESTAMP), observed_at)
    if record.get(ROLLOUT_TYPE) == RECORD_RESPONSE_ITEM:
        _question_signals(signals, payload, at, ask_tool=ask_tool)
        return
    if record.get(ROLLOUT_TYPE) != RECORD_EVENT_MSG:
        return
    kind = payload.get(PAYLOAD_TYPE)
    if kind == MSG_TURN_ABORTED:
        signals.note_turn_aborted(at)
    elif kind == MSG_TASK_COMPLETE:
        failure = turn_failure(payload)
        if failure is None:
            signals.note_turn_end(at)
            # Unlike claude's turn_duration, this line closes only a turn that did not fail.
            signals.turn_error = None
        else:
            signals.note_turn_error(at, failure)


def _question_signals(
    signals: SessionSignals, payload: Mapping[str, Any], at: datetime, *, ask_tool: bool
) -> None:
    kind = payload.get(PAYLOAD_TYPE)
    call_id = payload.get(CALL_ID)
    if kind == ITEM_FUNCTION_CALL and payload.get(CALL_NAME) == TOOL_REQUEST_USER_INPUT:
        arguments = payload.get(CALL_ARGUMENTS)
        tool_input = (json_object(arguments) if isinstance(arguments, str) else None) or {}
        if ask_tool and isinstance(call_id, str):
            expires_at = at + timedelta(seconds=ASK_SELF_ANSWER_SEC)
            signals.note_question(at, tool_input, call_id, expires_at=expires_at)
        else:
            signals.note_unanswered(call_id if isinstance(call_id, str) else "", tool_input)
    elif kind == ITEM_FUNCTION_CALL_OUTPUT and ask_tool and isinstance(call_id, str):
        if _answers(payload.get(CALL_OUTPUT)):
            signals.note_answer(at, {call_id})
        else:
            signals.note_unanswered(call_id)


def _answers(output: Any) -> bool:
    """Whether a question call's output carries an answer: it is JSON text, and an empty
    `answers` mapping is codex answering for nobody."""
    document = json_object(output) if isinstance(output, str) else None
    answers = document.get(ASK_ANSWERS) if document is not None else None
    return isinstance(answers, Mapping) and bool(answers)


def turn_failure(payload: Mapping[str, Any]) -> str | None:
    """A `task_complete`'s error message, or None for a turn that ended normally."""
    error = payload.get(TASK_ERROR)
    if error is None:
        return None
    message = error.get(KEY_MESSAGE) if isinstance(error, Mapping) else None
    if isinstance(message, str) and message.strip():
        return clip(message, ERROR_LIMIT)
    return TURN_FAILED


def _texts(item: Mapping[str, Any], part_type: str) -> list[str]:
    content = item.get(TURN_ITEM_CONTENT)
    return [
        part[ITEM_TEXT]
        for part in (content if isinstance(content, list) else [])
        if isinstance(part, Mapping)
        and part.get(ITEM_TYPE) == part_type
        and isinstance(part.get(ITEM_TEXT), str)
    ]


def _command(value: Any) -> str:
    """The argv codex ran, as one line, as `exec --json` shows it."""
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return shlex.join(value)
    return value if isinstance(value, str) else ""


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: log_count(value.get(key)) for key in _USAGE_FIELDS}


def _exit_code(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -(2**31) <= value < 2**31 else None


def _string(value: Any, limit: int) -> str:
    return clip(value, limit) if isinstance(value, str) else ""
