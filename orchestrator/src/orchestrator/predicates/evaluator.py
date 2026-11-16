"""Walk a condition AST against observed state (design.md §6).

An evaluation returns the verdict the state machine acts on and two explanations of it: the rule
code `failures()` extracts (`ci_green("impl/12-*") = false`), which events, the agent's retry
briefing and Force complete compare and quote, and the plain words `needs()` extracts ("passing
CI on impl/12-…"), which is what a person is shown. A bare `False` would leave a blocked ticket
with nothing to read, so every node explains itself.

`all` / `any` short-circuit — the loop makes real GitHub calls, and a cheap false must not pay
for an expensive tree listing — but the `Evaluation` still records the children evaluated.
`every_leaf=True` does not short-circuit: a person overriding a verdict must have seen every
false leaf (decision 78).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, assert_never

from orchestrator.config.conditions import AllOf, AnyOf, Condition, Not, PredicateCall, render_call
from orchestrator.config.templates import expand
from orchestrator.github.state import StateSource
from orchestrator.predicates.library import PREDICATES
from orchestrator.predicates.words import describe

__all__ = [
    "EvalContext",
    "Evaluation",
    "ReviewFacts",
    "UnknownPredicateError",
    "Unmet",
    "evaluate",
    "failures",
    "needs",
]


class UnknownPredicateError(LookupError):
    """A call reached the evaluator that the library cannot serve — a programming error."""


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    """What `request_approval()` compares: the step's branch, as a glob, and the commit a person
    approved for the run being accepted, None when nobody has."""

    branch: str
    approved_sha: str | None


@dataclass(frozen=True, slots=True)
class EvalContext:
    """One reconciliation tick's observed state plus the template values for this ticket."""

    source: StateSource
    values: Mapping[str, str]  # ticket, ticket_owner, repo, pr_url, ...
    #: The App's own bot login (settings, e.g. `"<slug>[bot]"`), not a template value: never
    #: config-supplied, never expanded into a comment or label. Read only by
    #: `issue_comment_contains` / `pr_comment_contains`, which must tell the App's own delivery
    #: evidence from a third party posting the same marker text. Empty when unset, which is also
    #: the fail-closed value — no GitHub login is the empty string, so nothing matches.
    bot_login: str = ""
    #: Set only by an agent step's acceptance (`worker.acceptance.acceptance_verdict`). None is
    #: the fail-closed value every other evaluation gets.
    review: ReviewFacts | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A verdict and why. `children` holds the sub-results actually evaluated, in order."""

    value: bool
    reason: str  # one line, e.g. 'ci_green("impl/12-*") = false'
    children: tuple[Evaluation, ...] = ()
    #: What this node asks for, in words (`words.describe`), e.g. "passing CI on impl/12-…".
    need: str = ""
    #: An `any`: its children are alternatives, so one unmet `need` says what is missing.
    alternatives: bool = False


def _verdict(value: bool) -> str:
    return "true" if value else "false"


async def evaluate(
    condition: Condition, ctx: EvalContext, *, every_leaf: bool = False
) -> Evaluation:
    """Evaluate a condition against observed state, returning the verdict and its explanation.
    `every_leaf` makes `all` and `any` evaluate every child, so `failures()` returns every false
    leaf; the verdict and reasons are the same."""
    match condition:
        case PredicateCall():
            return await _evaluate_call(condition, ctx)
        case AllOf():
            result = await _evaluate_all(condition.all_of, ctx, every_leaf)
            return replace(result, need=describe(condition, ctx.values))
        case AnyOf():
            result = await _evaluate_any(condition.any_of, ctx, every_leaf)
            return replace(result, need=describe(condition, ctx.values), alternatives=True)
        case Not():
            child = await evaluate(condition.not_, ctx, every_leaf=every_leaf)
            value = not child.value
            return Evaluation(
                value=value,
                reason=f"not ({child.reason}) = {_verdict(value)}",
                children=(child,),
                need=describe(condition, ctx.values),
            )
        case _:  # pragma: no cover - the union is closed
            assert_never(condition)


async def _evaluate_call(call: PredicateCall, ctx: EvalContext) -> Evaluation:
    # Lenient expansion: an unresolved `{slug}` must survive — it is the part of a branch name
    # the agent picks at runtime, and the `*` in the config's glob is what covers it.
    args = tuple(expand(arg, ctx.values) for arg in call.args)
    predicate = PREDICATES.get(call.name)
    if predicate is None:
        # Config validation already rejects unknown names (`conditions.PREDICATE_ARITY`), so this
        # is an orchestrator bug, not bad input — never a silent false.
        raise UnknownPredicateError(
            f"no implementation for predicate {call.name!r}, although config validation accepted it"
        )
    value = await predicate(ctx, *args)
    return Evaluation(
        value=value,
        reason=f"{render_call(call.name, args)} = {_verdict(value)}",
        need=describe(call, ctx.values),
    )


async def _evaluate_all(
    children: Sequence[Condition], ctx: EvalContext, every_leaf: bool
) -> Evaluation:
    evaluated: list[Evaluation] = []
    for child in children:
        result = await evaluate(child, ctx, every_leaf=every_leaf)
        evaluated.append(result)
        if not result.value and not every_leaf:
            break
    first_false = next((result for result in evaluated if not result.value), None)
    if first_false is not None:
        return Evaluation(False, f"all = false: {first_false.reason}", tuple(evaluated))
    return Evaluation(True, f"all = true ({len(evaluated)} condition(s) held)", tuple(evaluated))


async def _evaluate_any(
    children: Sequence[Condition], ctx: EvalContext, every_leaf: bool
) -> Evaluation:
    evaluated: list[Evaluation] = []
    for child in children:
        result = await evaluate(child, ctx, every_leaf=every_leaf)
        evaluated.append(result)
        if result.value and not every_leaf:
            break
    first_true = next((result for result in evaluated if result.value), None)
    if first_true is not None:
        return Evaluation(True, f"any = true: {first_true.reason}", tuple(evaluated))
    return Evaluation(
        False, f"any = false: none of {len(evaluated)} condition(s) held", tuple(evaluated)
    )


def failures(evaluation: Evaluation) -> tuple[str, ...]:
    """The leaf reasons that made a false result false — what an event or comment should quote."""
    if evaluation.value:
        return ()
    leaves = tuple(
        reason for child in evaluation.children if not child.value for reason in failures(child)
    )
    # A false `not` has no false leaf beneath it (its child held), so its own line is the
    # explanation; likewise a false predicate call, which has no children at all.
    return leaves or (evaluation.reason,)


def needs(evaluation: Evaluation) -> tuple[str, ...]:
    """What a false result still lacks, in words: `failures()`'s leaves as a person reads them.
    An unmet `any` is one need ("x or y"), since meeting any one of its children is enough."""
    if evaluation.value:
        return ()
    if evaluation.alternatives:
        return (evaluation.need,)
    leaves = tuple(
        phrase for child in evaluation.children if not child.value for phrase in needs(child)
    )
    return leaves or (evaluation.need,)


@dataclass(frozen=True, slots=True)
class Unmet:
    """Why a condition does not hold: the rule code that events, the retry briefing and the MCP
    tools keep (`rules`), and the same in words for a person (`words`). A record written before
    the words existed has only `rules`."""

    rules: tuple[str, ...] = ()
    words: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.rules)

    @property
    def said(self) -> tuple[str, ...]:
        """What a person reads: the words, or the rule code an older record kept instead."""
        return self.words or self.rules

    def details(self) -> dict[str, list[str]]:
        """As an event records it: `unmet`, and `needs` beside it."""
        return {"unmet": list(self.rules), "needs": list(self.words)}

    @classmethod
    def of(cls, evaluation: Evaluation) -> Unmet:
        return cls(failures(evaluation), needs(evaluation))

    @classmethod
    def read(cls, details: Mapping[str, Any]) -> Unmet:
        """From an event's details, as `details()` wrote them."""
        return cls(_strings(details.get("unmet")), _strings(details.get("needs")))


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()
