"""The closed predicate library and its evaluator (design.md §6).

Config may only *reference* predicates; adding one is a code change here, a name in
`orchestrator.config.conditions.PREDICATE_ARITY` and its phrase in `words.py`. Everything above
this package works in terms of `evaluate(condition, ctx) -> Evaluation`, `failures(evaluation)`
(rule code) and `needs(evaluation)` (the same, in words).
"""

from __future__ import annotations

from orchestrator.predicates.evaluator import (
    EvalContext,
    Evaluation,
    ReviewFacts,
    UnknownPredicateError,
    Unmet,
    evaluate,
    failures,
    needs,
)
from orchestrator.predicates.library import PREDICATES, Predicate

__all__ = [
    "PREDICATES",
    "EvalContext",
    "Evaluation",
    "Predicate",
    "ReviewFacts",
    "UnknownPredicateError",
    "Unmet",
    "evaluate",
    "failures",
    "needs",
]
