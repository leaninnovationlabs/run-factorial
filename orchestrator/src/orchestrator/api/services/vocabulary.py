"""The closed registries process config may reference.

Config may only REFERENCE these; adding to any of them is a code change. The builder's pickers and
the MCP vocabulary tool are both served this, so no client hardcodes a copy that drifts.
"""

from __future__ import annotations

from orchestrator.api.schemas import PredicateOut, VocabularyOut
from orchestrator.config import (
    ACTION_NAMES,
    BARE_INPUT_SOURCES,
    KNOWN_PLACEHOLDERS,
    PREDICATE_ARITY,
)


def config_vocabulary() -> VocabularyOut:
    return VocabularyOut(
        predicates=[
            PredicateOut(name=name, min_args=low, max_args=high)
            for name, (low, high) in sorted(PREDICATE_ARITY.items())
        ],
        actions=sorted(ACTION_NAMES),
        placeholders=sorted(KNOWN_PLACEHOLDERS),
        input_sources=sorted(BARE_INPUT_SOURCES),
        compositions=["all", "any", "not"],
    )
