"""The closed action library (design.md §4) — `kind: action`'s executor.

Config may only reference an action; adding one is a code change here plus a name in
`orchestrator.config.models.ACTION_NAMES`. Callers work in terms of
`ACTIONS[name](ctx) -> ActionResult`.
"""

from __future__ import annotations

from orchestrator.actions.registry import (
    ACTIONS,
    Action,
    ActionContext,
    ActionResult,
    ContractGitHub,
    UnknownActionError,
    fix_the_contract,
)

__all__ = [
    "ACTIONS",
    "Action",
    "ActionContext",
    "ActionResult",
    "ContractGitHub",
    "UnknownActionError",
    "fix_the_contract",
]
