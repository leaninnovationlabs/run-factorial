"""A run's activity, derived from `RuntimeAdapter.logs` — the transcript, turn by turn.

The adapter streams the agent's session log as each CLI adapter's `transcript.py` translates
it: claude's into `stream-json` events, codex's into its `--json` events. The
dialect is told line by line — a codex line has one of a closed set of `type`s and no `message`
object — and both arms produce the same turns, tool calls and milestones, so the view has one
shape whichever CLI wrote the log.
Codex's vocabulary is not repeated here: `runtime.adapters.codex_cli.events` is its one
reader, and this module consumes its typed events.

## What is read, and what that costs

Tool calls are built field by field. A tool input can be anything the agent passed, so this
module reads a closed set of keys (`_TARGET_KEYS`) and constructs a new object from them — a
credential sitting in an unread field is not scrubbed by a filter that might be wrong, it is
never looked at. `event.details` gets that guarantee structurally (`events.writer.
_reject_credentials`, design.md §7.1); a run's log cannot, because the run holds a minted
`GH_TOKEN` and an `ANTHROPIC_API_KEY` and anything echoed lands in the stream verbatim.

One tool has a closed extractor of its own: `AskUserQuestion` shows each question's `question`
and, from the user line that answers it, `tool_use_result.answers` for those questions — no
target, no result text (the CLI renders it from the input), and no other key of either.

**`text` and `thinking` are the exception, and it is not a small one.** They are free-form model
output with no fields to restrict, so including them is a passthrough of whatever the model
wrote — including, if it read a secret and reasoned about it, that secret. They are here because
a transcript without the agent's own words is not a transcript. Both are truncated, and
`thinking` is marked so a viewer can be told what it is looking at, but neither can be made safe
the way a tool input can. That is a deliberate trade, not an oversight.

## Turn grouping

A turn is one assistant message. `tool_result` blocks arrive in the FOLLOWING user message, so
results are matched back to their call by `tool_use_id` rather than by position — the stream
interleaves them and position is not reliable.

For codex a turn is `turn.started` .. `turn.completed` and its items are the turn's text and
commands. A command arrives twice under one item id, started then completed, so the completion
fills the slot the start opened — the same id-not-position rule.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

from runtime.adapters._cli.session_signals import QUESTION_LIMIT, QUESTIONS_SHOWN
from runtime.adapters.claude_cli.cli import (
    ASK_QUESTION,
    ASK_QUESTIONS,
    EVENT_TOOL_USE_RESULT,
    LOG_ANSWERS,
    TOOL_ASK_USER_QUESTION,
)
from runtime.adapters.claude_cli.transcript import ANSWER_LIMIT
from runtime.adapters.codex_cli.cli import (
    EVENT_ERROR,
    EVENT_ITEM_COMPLETED,
    EVENT_ITEM_STARTED,
    EVENT_THREAD_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
)
from runtime.adapters.codex_cli.events import (
    AgentMessage,
    CommandExecution,
    ErrorEvent,
    ThreadStarted,
    TurnCompleted,
    TurnStarted,
    parse_payload,
)

__all__ = ["ActivityTurn", "RunActivity", "ToolCall", "parse_activity"]

#: The codex dialect's whole top-level vocabulary. Claude's stream never uses these names, and
#: a Claude line always carries a `message` OBJECT where codex's `error` carries a string.
_CODEX_TYPES: Final = frozenset(
    {
        EVENT_THREAD_STARTED,
        EVENT_TURN_STARTED,
        EVENT_TURN_COMPLETED,
        EVENT_TURN_FAILED,
        EVENT_ITEM_STARTED,
        EVENT_ITEM_COMPLETED,
        EVENT_ERROR,
    }
)

#: Longest tool target kept. A tool input can be a whole file; the point is WHICH file.
_TARGET_LIMIT: Final = 160

#: Longest model prose kept per block. Enough to read, short enough that a dashboard row is not
#: a document — and short enough that an accidental paste of something large is bounded.
_PROSE_LIMIT: Final = 2000

#: Longest tool result kept. Results are often entire command outputs.
_RESULT_LIMIT: Final = 400

#: Input keys worth showing, most specific first. Anything not named here yields no target rather
#: than a guess — an unrecognised tool renders as its name alone, which is honest.
_TARGET_KEYS: Final = ("command", "file_path", "path", "pattern", "url", "query", "name")

#: `system` subtypes worth surfacing as milestones. `thinking_tokens` is excluded on purpose: it
#: is a per-delta counter and was 206 of ~330 events in a single real run.
_MILESTONES: Final = {
    "init": "session started",
    "task_started": "task started",
    "vcs_state_changed": "working tree changed",
    "code_change_published": "changes published",
    "task_notification": "notification",
}


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool use, with its result matched back by id."""

    tool: str
    target: str | None
    #: `None` while the call is still outstanding — a live run's last call has no result yet.
    ok: bool | None = None
    result: str | None = None
    #: `AskUserQuestion` only: its questions, and the human's answers to them.
    question: str | None = None
    answer: str | None = None


@dataclass(slots=True)
class ActivityTurn:
    """One assistant message: what it said, what it thought, what it did."""

    index: int
    text: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class RunActivity:
    turns: list[ActivityTurn] = field(default_factory=list)
    #: `system` milestones, in stream order, as human-readable labels.
    milestones: list[str] = field(default_factory=list)
    #: Set from the terminal `result` event when the run reached one.
    outcome: str | None = None
    cost_usd: float | None = None
    truncated: bool = False


def _target(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _TARGET_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, _TARGET_LIMIT)
    return None


_ASK_SEPARATOR: Final = " · "


def _questions(payload: Any) -> tuple[str, ...]:
    """Each question's own text, clipped as the TUI translator clips its `answers` keys."""
    items = payload.get(ASK_QUESTIONS) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ()
    return tuple(
        _clip(item[ASK_QUESTION], QUESTION_LIMIT)
        for item in items[:QUESTIONS_SHOWN]
        if isinstance(item, dict)
        and isinstance(item.get(ASK_QUESTION), str)
        and item[ASK_QUESTION].strip()
    )


def _answer(event: dict[str, Any], questions: tuple[str, ...]) -> str | None:
    result = event.get(EVENT_TOOL_USE_RESULT)
    answers = result.get(LOG_ANSWERS) if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        return None
    wanted = set(questions)
    given = {
        key: _clip(answer, ANSWER_LIMIT)
        for question, answer in answers.items()
        if isinstance(question, str)
        and isinstance(answer, str)
        and (key := _clip(question, QUESTION_LIMIT)) in wanted
    }
    picked = [given.get(question) for question in questions]
    if not any(picked):
        return None
    return _ASK_SEPARATOR.join(answer or "-" for answer in picked)


def _result_text(block: dict[str, Any]) -> str | None:
    content = block.get("content")
    if isinstance(content, str):
        return _clip(content, _RESULT_LIMIT)
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")]
        if parts:
            return _clip(" ".join(parts), _RESULT_LIMIT)
    return None


def _relativise(text: str, run_id: str) -> str:
    """Strip the run's own workspace prefix off a path inside it.

    Every entry in a run shares this prefix, which carries nothing, and it is also the half a
    right-truncating view discards first: the filename. Keyed on `run_id`, so a path the agent
    read from somewhere ELSE on the machine stays absolute — that is worth seeing, not tidying.
    """
    if not run_id or run_id not in text:
        return text
    for candidate in (f"/{run_id}/worktree/", f"/{run_id}/workspace/"):
        if candidate in text:
            return text.split(candidate, 1)[1] or text
    return text


def _is_codex(event: dict[str, Any]) -> bool:
    return event.get("type") in _CODEX_TYPES and not isinstance(event.get("message"), dict)


class _CodexTurns:
    """`parse_activity`'s codex arm, producing the same shapes the Claude arm does.

    The outcome is codex's own terminal signal, as Claude's is `result.subtype`: a completed
    turn is `success` when the agent said something and `finished` when it did not (the
    supervisor's rule — silence is not success), and a failed turn is `error`. An error that
    is not `turn.failed` is a milestone only — the turn goes on after it, so it must not
    colour a live run. A run whose agent finished its turn but failed its job still reads
    `success` here; the step's verdict is what says otherwise.
    """

    def __init__(self, activity: RunActivity, *, limit: int, run_id: str) -> None:
        self._activity = activity
        self._limit = limit
        # A path in a COMMAND keeps the words around it, so the prefix is cut in place rather
        # than split off as `_relativise` does for a lone file argument.
        self._workspace = (
            re.compile(r"\S*/" + re.escape(run_id) + r"/(?:worktree|workspace)/")
            if run_id
            else None
        )
        self._turn: ActivityTurn | None = None
        self._pending: dict[str, int] = {}
        self._spoke = False

    def feed(self, event: dict[str, Any]) -> bool:
        """Consume one line; `True` once the turn limit is hit."""
        match parse_payload(event):
            case ThreadStarted():
                self._milestone(_MILESTONES["init"])
            case TurnStarted():
                if len(self._activity.turns) >= self._limit:
                    self._activity.truncated = True
                    return True
                self._turn = None
                self._pending.clear()
            case AgentMessage(text=text):
                if text.strip():
                    self._current().text.append(_clip(text, _PROSE_LIMIT))
                    self._spoke = True
            case CommandExecution() as command:
                self._command(command)
            case ErrorEvent(message=message, terminal=terminal):
                self._milestone(f"error: {_clip(message, _TARGET_LIMIT)}")
                if terminal:
                    self._activity.outcome = "error"
            case TurnCompleted():
                self._activity.outcome = "success" if self._spoke else "finished"
                self._turn = None
            case _:
                pass
        return False

    def _milestone(self, label: str) -> None:
        if label not in self._activity.milestones:
            self._activity.milestones.append(label)

    def _current(self) -> ActivityTurn:
        # Opened on first content rather than on `turn.started`, so an empty turn adds no row —
        # and an item outside any turn still has somewhere to go.
        if self._turn is None:
            self._turn = ActivityTurn(index=len(self._activity.turns) + 1)
            self._activity.turns.append(self._turn)
        return self._turn

    def _command(self, command: CommandExecution) -> None:
        turn = self._current()
        target = None
        if command.command.strip():
            target = _clip(command.command, _TARGET_LIMIT)
            if self._workspace is not None:
                target = self._workspace.sub("", target)
        if not command.completed:
            turn.tools.append(ToolCall(tool="command", target=target))
            if command.item_id:
                self._pending[command.item_id] = len(turn.tools) - 1
            return
        call = ToolCall(
            tool="command",
            target=target,
            ok=command.exit_code == 0,
            result=(
                _clip(command.aggregated_output, _RESULT_LIMIT)
                if command.aggregated_output.strip()
                else None
            ),
        )
        position = self._pending.pop(command.item_id, None)
        if position is None:
            turn.tools.append(call)
        else:
            turn.tools[position] = call


async def parse_activity(
    lines: AsyncIterator[str], *, limit: int = 400, run_id: str = ""
) -> RunActivity:
    """Read a log stream into turns, stopping at `limit` turns.

    A malformed line is skipped rather than fatal: the stream is a live capture of a process that
    may have been killed mid-write — a cancelled run, or one the adapter terminated at its turn
    cap — so a truncated final line is normal and is not a reason to fail the whole view.
    """
    activity = RunActivity()
    pending: dict[str, tuple[ActivityTurn, int]] = {}
    asked: dict[str, tuple[str, ...]] = {}
    codex = _CodexTurns(activity, limit=limit, run_id=run_id)

    async for line in lines:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            event: Any = json.loads(text)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        if _is_codex(event):
            if codex.feed(event):
                break
            continue

        kind = event.get("type")

        if kind == "system":
            label = _MILESTONES.get(str(event.get("subtype")))
            if label and label not in activity.milestones:
                activity.milestones.append(label)
            continue

        if kind == "result":
            activity.outcome = str(event.get("subtype") or "finished")
            cost = event.get("total_cost_usd") or event.get("cost_usd")
            if isinstance(cost, int | float):
                activity.cost_usd = float(cost)
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        if kind == "user":
            # Results for the PREVIOUS assistant turn's calls (module docstring, "Turn grouping").
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                use_id = block.get("tool_use_id")
                if not isinstance(use_id, str):
                    continue
                slot = pending.pop(use_id, None)
                if slot is None:
                    continue
                turn, position = slot
                call = turn.tools[position]
                questions = asked.pop(use_id, ())
                turn.tools[position] = ToolCall(
                    tool=call.tool,
                    target=call.target,
                    ok=not bool(block.get("is_error")),
                    result=None if call.tool == TOOL_ASK_USER_QUESTION else _result_text(block),
                    question=call.question,
                    answer=_answer(event, questions) if questions else None,
                )
            continue

        if kind != "assistant":
            continue

        if len(activity.turns) >= limit:
            activity.truncated = True
            break

        turn = ActivityTurn(index=len(activity.turns) + 1)
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    turn.text.append(_clip(block["text"], _PROSE_LIMIT))
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                if block["thinking"].strip():
                    turn.thinking.append(_clip(block["thinking"], _PROSE_LIMIT))
            elif btype == "tool_use":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                use_id = block.get("id")
                if name == TOOL_ASK_USER_QUESTION:
                    questions = _questions(block.get("input"))
                    call = ToolCall(
                        tool=name, target=None, question=_ASK_SEPARATOR.join(questions) or None
                    )
                    if isinstance(use_id, str):
                        asked[use_id] = questions
                else:
                    raw_target = _target(block.get("input"))
                    call = ToolCall(
                        tool=name,
                        target=_relativise(raw_target, run_id) if raw_target else None,
                    )
                turn.tools.append(call)
                if isinstance(use_id, str):
                    pending[use_id] = (turn, len(turn.tools) - 1)

        # An assistant message that carried only a stop reason adds nothing to read.
        if turn.text or turn.thinking or turn.tools:
            activity.turns.append(turn)

    return activity
