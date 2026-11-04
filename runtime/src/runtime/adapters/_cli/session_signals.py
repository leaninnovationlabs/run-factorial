"""Whether a session sits at its prompt, and why, from its CLI's hook signals and
its session log (decision 56). The same model for every CLI: claude and codex fire the same hook
names with the same payload keys, and what each CLI's log says about a turn's end reaches the
model through its neutral `note_*` methods, called by that CLI's own log rules.

Pure: lines in, facts out. Both inputs are agent-writable, so a hook gives only the fields the
facts need, and a claimed time never runs past when the line was read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from runtime.contract import IdleReason, RunUsage

#: A signals line, as `signal.sh` writes it: `{"at": <UTC, seconds>, "hook": <stdin JSON>}`.
SIGNAL_AT: Final = "at"
SIGNAL_HOOK: Final = "hook"

# The hook vocabulary this model reads. Each CLI's pinned surface keeps its own copies, and a
# test holds them equal to these.
HOOK_EVENT_NAME: Final = "hook_event_name"
HOOK_SESSION_ID: Final = "session_id"
HOOK_SESSION_START: Final = "SessionStart"
HOOK_USER_PROMPT_SUBMIT: Final = "UserPromptSubmit"
HOOK_PRE_TOOL_USE: Final = "PreToolUse"
HOOK_POST_TOOL_USE: Final = "PostToolUse"
HOOK_STOP: Final = "Stop"
HOOK_STOP_FAILURE: Final = "StopFailure"
HOOK_SESSION_END: Final = "SessionEnd"
HOOK_TOOL_NAME: Final = "tool_name"
HOOK_TOOL_INPUT: Final = "tool_input"
HOOK_TOOL_USE_ID: Final = "tool_use_id"
#: On `SessionStart`. A compaction or a `/clear` starts a session too, under another source.
HOOK_SOURCE: Final = "source"
LAUNCH_SOURCES: Final = ("startup", "resume")
HOOK_LAST_MESSAGE: Final = "last_assistant_message"
HOOK_ERROR: Final = "error"
#: The question tool whose `PreToolUse` idles a session on a question: claude's. A turn of any
#: CLI whose closing words end in a question idles on that question too.
TOOL_ASK_USER_QUESTION: Final = "AskUserQuestion"
ASK_QUESTIONS: Final = "questions"
ASK_QUESTION: Final = "question"
ASK_OPTIONS: Final = "options"
ASK_LABEL: Final = "label"

STOP_HOOK_MISSING: Final = "the Stop hook did not fire"

QUESTIONS_SHOWN: Final = 4
QUESTION_LIMIT: Final = 300
#: Markdown that can close a question: `**Which one?**`.
_TRAILING_MARKUP: Final = "*_`~ \t\r\n"
#: The `question_id` of a question asked in words; no tool result answers it.
QUESTION_AT_PROMPT: Final = "at-prompt"
OPTIONS_SHOWN: Final = 6
LABEL_LIMIT: Final = 80
QUESTION_TOTAL: Final = 1500
SUMMARY_LIMIT: Final = 2000
ERROR_LIMIT: Final = 400


class CostState(BaseModel):
    """The session's running totals from its last `cost-state` line, every model summed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usd: Decimal
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


#: The most a count may carry: what `step_run`'s bigint columns hold.
COUNT_CAP: Final = 2**63 - 1


def usage(
    input_tokens: int, cached_input_tokens: int, cache_write_input_tokens: int, output_tokens: int
) -> RunUsage:
    """Counts read off an agent-written log, held to [0, COUNT_CAP]: a forged line moves only
    an estimate, and never one a column refuses."""
    return RunUsage(
        input_tokens=_bounded(input_tokens),
        cached_input_tokens=_bounded(cached_input_tokens),
        cache_write_input_tokens=_bounded(cache_write_input_tokens),
        output_tokens=_bounded(output_tokens),
    )


def plus(base: RunUsage | None, segment: RunUsage | None) -> RunUsage | None:
    """A segment's usage on top of the earlier segments'; None only when both are."""
    if base is None or segment is None:
        return segment or base
    return usage(*(getattr(base, name) + getattr(segment, name) for name in RunUsage.model_fields))


def _bounded(count: int) -> int:
    return min(max(count, 0), COUNT_CAP)


class SessionSignals(BaseModel):
    """Whether the session sits at its prompt, and why, from its hook signals (decision 56).

    The facts are the whole state, so the caller persists this model and a restarted worker
    carries on without replaying a line. Feed the signals and the log in the order they were
    read; within one read, the signals first, then `settle_turn_ends` if the read took the
    signals file to its end.
    """

    #: Both None while the agent works; both set while it waits for a human.
    idle_reason: IdleReason | None = None
    idle_since: AwareDatetime | None = None
    question: str | None = None
    #: The pending question tool call, so a log line answering an older one is not taken for
    #: the answer to this one.
    question_id: str | None = None
    #: When the CLI answers the pending question itself if nobody does.
    question_expires_at: AwareDatetime | None = None
    #: A question tool call the CLI refused or answered with nothing: the turn's end idles on it.
    unanswered: str | None = None
    #: The closing question of a Stop read before its picker's output, for that output to keep.
    deferred_question: str | None = None
    last_input_at: AwareDatetime | None = None
    #: The last turn's closing words, from `Stop`.
    summary: str | None = None
    #: The last turn's failure; cleared by a turn that ends normally.
    turn_error: str | None = None
    ended: bool = False
    #: Whether this segment's launch carried the orchestrator's turn.
    segment_turn: bool = False
    prompts: int = 0
    #: Turn ends this segment, as the hooks reported them and as the log did.
    hook_turn_ends: int = 0
    log_turn_ends: int = 0
    #: Turn ends the log reported with no hook, already credited, whose hook may still come: a
    #: hook's line lands before the log's, but one read can take the signals before it and the
    #: log after.
    stops_owed: int = 0
    warnings: tuple[str, ...] = ()

    def start_segment(self, *, turn: bool) -> None:
        """A launch: the CLI is starting, so it is not at its prompt until a hook says so."""
        self.settle_turn_ends()
        self.segment_turn = turn
        self.prompts = self.hook_turn_ends = self.log_turn_ends = 0
        self.ended = False
        self.unanswered = self.deferred_question = None
        self._active()

    @property
    def at_plain_prompt(self) -> bool:
        """Whether the CLI sits at its plain prompt, where `/exit` quits it; a question tool's
        dialog would take it for an answer."""
        return (
            self.idle_reason in (IdleReason.TURN_END, IdleReason.TURN_ERROR)
            or self.question_id == QUESTION_AT_PROMPT
        )

    def feed(self, line: str, *, observed_at: datetime, session_id: str | None = None) -> None:
        """One signals line. `observed_at` is when it was read, and bounds the `at` it claims.
        With `session_id`, a hook that names another session is not this one's."""
        require_aware(observed_at)
        record = json_object(line)
        hook = record.get(SIGNAL_HOOK) if record else None
        if record is None or not isinstance(hook, Mapping):
            return
        if session_id is not None and hook.get(HOOK_SESSION_ID) != session_id:
            return
        at = claimed_at(record.get(SIGNAL_AT), observed_at)
        event = hook.get(HOOK_EVENT_NAME)
        asking = hook.get(HOOK_TOOL_NAME) == TOOL_ASK_USER_QUESTION
        if event == HOOK_SESSION_START:
            # A resume without a turn sits at its prompt. A compaction mid-turn starts a session
            # too, and must not make a working agent look idle.
            source = hook.get(HOOK_SOURCE)
            launch = source in LAUNCH_SOURCES if isinstance(source, str) else self.prompts == 0
            if launch and not self.segment_turn:
                self.note_at_prompt(at)
        elif event == HOOK_USER_PROMPT_SUBMIT:
            self.settle_turn_ends()  # hooks fire in order: an owed Stop is lost, not late
            self.prompts += 1
            self._active()
            self.summary = None
            self.unanswered = self.deferred_question = None
            if not (self.segment_turn and self.prompts == 1):
                self.last_input_at = at
        elif event == HOOK_PRE_TOOL_USE and asking:
            self._idle(IdleReason.QUESTION, at)
            self.question = _question(hook.get(HOOK_TOOL_INPUT))
            use_id = hook.get(HOOK_TOOL_USE_ID)
            self.question_id = use_id if isinstance(use_id, str) else None
        elif event == HOOK_POST_TOOL_USE and asking:
            self._active()
            self.last_input_at = at
        elif event == HOOK_STOP:
            self._hooked_turn_end()
            message = hook.get(HOOK_LAST_MESSAGE)
            text = message if isinstance(message, str) else ""
            asked, self.unanswered = self.unanswered, None
            if self.question_expires_at is not None:
                # A logged call's picker: its output, in the log before this Stop, decides.
                self.question_expires_at = None
                if _ends_in_question(text):
                    self.deferred_question = _closing_question(text) or None
            elif asked is not None or _ends_in_question(text):
                self._idle(IdleReason.QUESTION, at)
                self.question = asked or _closing_question(text) or None
                self.question_id = QUESTION_AT_PROMPT
            else:
                self._idle(IdleReason.TURN_END, at)
            if isinstance(message, str):
                self.summary = clip(message, SUMMARY_LIMIT)
            self.turn_error = None
        elif event == HOOK_STOP_FAILURE:
            self._hooked_turn_end()
            self._idle(IdleReason.TURN_ERROR, at)
            parts = [hook.get(HOOK_ERROR), hook.get(HOOK_LAST_MESSAGE)]
            self.turn_error = clip(": ".join(p for p in parts if isinstance(p, str)), ERROR_LIMIT)
        elif event == HOOK_SESSION_END:
            self.ended = True

    def note_at_prompt(self, at: datetime) -> None:
        """A launch with no turn reached its prompt: a CLI that fires no hook until a turn starts
        is seen there by its launch instead. Back at the prompt a failed turn left it at, not at
        a fresh turn's end."""
        self._idle(IdleReason.TURN_ERROR if self.turn_error else IdleReason.TURN_END, at)

    # What the log says, for when a hook did not fire: an agent can edit its hooks away, and the
    # next launch restores them only then. Each CLI's log rules call these.

    def note_turn_end(self, at: datetime) -> None:
        """A turn ended normally. With no hook for it, the session idles at once, and
        `settle_turn_ends` says the hook did not fire if none comes by then."""
        if self._unhooked_turn_end() and self._idling():
            self._idle(IdleReason.TURN_END, at)
            self.stops_owed += 1

    def settle_turn_ends(self) -> None:
        """After a read's signals, and before its log: a hook owed since an earlier read did
        not fire. Also at a segment's end, which no later read follows."""
        if self.stops_owed:
            self.stops_owed = 0
            self.warnings = tuple(dict.fromkeys((*self.warnings, STOP_HOOK_MISSING)))

    def note_turn_error(self, at: datetime, error: str) -> None:
        """A turn failed, which a CLI with no failure hook says only in its log."""
        if self._unhooked_turn_end():
            if self._idling() and not self._prompted_after(at):
                self._idle(IdleReason.TURN_ERROR, at)
            self.turn_error = clip(error, ERROR_LIMIT)

    def note_turn_aborted(self, at: datetime) -> None:
        """A human stopped the turn: the session is back at its prompt, no hook was lost, and
        the last turn did not fail. A logged call's picker goes with the turn."""
        if self._unhooked_turn_end():
            if self.question_expires_at is not None or (
                self._idling() and not self._prompted_after(at)
            ):
                self._idle(IdleReason.TURN_END, at)
            self.turn_error = None

    # A CLI with no question-tool hook says a call and its output only in its log, and a read
    # takes the signals first: the turn's Stop, which follows both, can be read before them.
    # The log is then a turn behind the hooks (`_log_lag`), and the line's turn has already
    # ended. Further behind, the line's turn is no longer the latest ended one, and is ignored.

    def note_question(
        self,
        at: datetime,
        tool_input: Mapping[str, object],
        call_id: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """A question tool call the CLI put up for a human, which the CLI answers itself at
        `expires_at` if nobody has."""
        lag = self._log_lag()
        if lag > 1:
            return
        if lag:
            if self.idle_reason is not IdleReason.TURN_END:
                return  # the next turn has begun
            # The picker is gone with the turn; the output, next in the log, says how.
            self._idle(IdleReason.QUESTION, self.idle_since or at)
        else:
            self._idle(IdleReason.QUESTION, at)
            self.question_expires_at = expires_at
        self.question = _question(tool_input)
        self.question_id = call_id

    def note_unanswered(
        self, call_id: str, tool_input: Mapping[str, object] | None = None
    ) -> None:
        """A question tool call nobody answered: refused, given as `tool_input`, or the pending
        one answered with nothing. The turn goes on, and its end idles on the question."""
        if tool_input is not None:
            question = _question(tool_input) or ""
        elif self.idle_reason is IdleReason.QUESTION and self.question_id == call_id:
            question = self.question or ""
        else:
            return
        lag = self._log_lag()
        if lag > 1:
            return
        if not lag:
            self.unanswered = question
            if tool_input is None:
                self._active()
        elif self.idle_since is not None and (
            self.idle_reason is IdleReason.TURN_END or tool_input is None
        ):
            self._idle(IdleReason.QUESTION, self.idle_since)
            self.question, self.question_id = question or None, QUESTION_AT_PROMPT
            self.deferred_question = None

    def note_answer(self, at: datetime, answered: Collection[str]) -> None:
        """A question tool call returned answers; `answered` holds the tool calls it answers."""
        if self.idle_reason is not IdleReason.QUESTION:
            return
        lag = self._log_lag()
        if lag > 1:
            return
        if self.question_id is None or self.question_id in answered:
            self.last_input_at = at
            if lag:
                asked, self.deferred_question = self.deferred_question, None
                since = self.idle_since or at
                if asked is None:
                    self._idle(IdleReason.TURN_END, since)
                else:
                    self._idle(IdleReason.QUESTION, since)
                    self.question, self.question_id = asked, QUESTION_AT_PROMPT
            else:
                self._active()

    def _unhooked_turn_end(self) -> bool:
        """Counts a turn end the log reported; True when no hook reported it, which is then
        credited, so one missing hook does not skew every later turn."""
        self.log_turn_ends += 1
        # Counted rather than timed: a fast turn's Stop and the next prompt's hook can share a
        # second, and the log's line for the earlier turn can be read after both.
        if self.log_turn_ends <= self.hook_turn_ends:
            return False
        self.hook_turn_ends = self.log_turn_ends
        return True

    def _hooked_turn_end(self) -> None:
        if self.stops_owed:
            self.stops_owed -= 1  # late: the log's turn end was credited with it
        else:
            self.hook_turn_ends += 1

    def _log_lag(self) -> int:
        """How many turn ends the hooks have reported that the log has not reached yet."""
        return max(self.hook_turn_ends - self.log_turn_ends, 0)

    def _prompted_after(self, at: datetime) -> bool:
        """Whether a prompt read already came after this turn ended, so the session is in that
        prompt's turn. With no hook for the ended turn, only the times order the two. A hook's
        time is in whole seconds, so a prompt within the same second still idles the session
        until its own turn's hook."""
        return self.last_input_at is not None and self.last_input_at > at

    def _idling(self) -> bool:
        return self.idle_reason is None and not self.ended

    def _idle(self, reason: IdleReason, at: datetime) -> None:
        if self.idle_reason is not reason:
            self.idle_reason, self.idle_since = reason, at
        self.question = self.question_id = self.question_expires_at = None

    def _active(self) -> None:
        self.idle_reason = self.idle_since = self.question_expires_at = None
        self.question = self.question_id = None


def _ends_in_question(message: str) -> bool:
    if message.rstrip().endswith("```"):
        return False
    return message.rstrip(_TRAILING_MARKUP).endswith("?")


def _closing_question(message: str) -> str:
    """The last paragraph, whitespace collapsed; over the limit, its tail."""
    paragraphs = [p for p in re.split(r"\n\s*\n", message) if p.strip(_TRAILING_MARKUP)]
    text = " ".join(paragraphs[-1].split()) if paragraphs else ""
    if len(text) <= QUESTION_LIMIT:
        return truncate(text, QUESTION_LIMIT)
    tail = text[-(QUESTION_LIMIT - 1) :].encode("utf-8", "replace").decode("utf-8")
    return "…" + tail


def _question(tool_input: Any) -> str | None:
    """The question tool's questions as one line each: "Question? [Option | Option]"."""
    questions = tool_input.get(ASK_QUESTIONS) if isinstance(tool_input, Mapping) else None
    if not isinstance(questions, list):
        return None
    lines: list[str] = []
    for item in questions[:QUESTIONS_SHOWN]:
        text = item.get(ASK_QUESTION) if isinstance(item, Mapping) else None
        if not isinstance(text, str):
            continue
        options = item.get(ASK_OPTIONS)
        labels = [
            clip(option[ASK_LABEL], LABEL_LIMIT)
            for option in (options[:OPTIONS_SHOWN] if isinstance(options, list) else [])
            if isinstance(option, Mapping) and isinstance(option.get(ASK_LABEL), str)
        ]
        line = clip(text, QUESTION_LIMIT)
        lines.append(f"{line} [{' | '.join(labels)}]" if labels else line)
    return truncate("\n".join(lines), QUESTION_TOTAL) or None


def json_object(line: str) -> Mapping[str, Any] | None:
    """A line's JSON object, or None. The files are agent-writable, so nesting and integer size
    are hostile too."""
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def log_count(value: Any) -> int:
    """A count read off an agent-written log, or 0. Bounded to what a Postgres bigint holds."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= COUNT_CAP else 0


def claimed_at(value: Any, observed_at: datetime) -> datetime:
    """A claimed time, never later than when it was read: the files are agent-writable."""
    if not isinstance(value, str):
        return observed_at
    try:
        at = datetime.fromisoformat(value)
        return observed_at if at.tzinfo is None else min(at.astimezone(UTC), observed_at)
    except (ValueError, OverflowError):
        return observed_at


def require_aware(observed_at: datetime) -> None:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")


def clip(text: str, limit: int) -> str:
    return truncate(" ".join(text.split()), limit)


def truncate(text: str, limit: int) -> str:
    # A lone surrogate is valid in a JSON string and in a Python one, but not in UTF-8, so it
    # would fail the state file's write.
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text if len(text) <= limit else text[: limit - 1] + "…"
