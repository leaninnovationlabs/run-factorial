"""The closed predicate library in words, for people (#383).

Rule code such as `pr_open("spec/12-*", "main") = false` is what the evaluator records and what
the agent's retry briefing quotes. A person reading the timeline, the Admission table, the ticket
page or a comment on the issue gets this module's phrase instead, so every surface words a rule
the same way.

A phrase names what must become true, lower-case, so each surface frames it ("Waiting for: …",
"Still missing: …"). Globs, and placeholders the agent fills in at runtime, read as "a branch
starting with spec/12-".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, assert_never

from orchestrator.config.conditions import (
    PREDICATE_ARITY,
    AllOf,
    AnyOf,
    Condition,
    Not,
    PredicateCall,
    render_call,
)
from orchestrator.config.templates import expand, globbed

__all__ = ["branch_phrase", "describe", "need", "ref"]


def ref(text: str) -> str:
    """A branch or path as a person reads it: `spec/12-*` and `spec/12-{slug}` are `spec/12-*`."""
    return re.sub(r"\*+", "*", globbed(text))


def _pattern(shown: str) -> str:
    head, star, tail = shown.partition("*")
    return f"starting with {head}" if star and not tail else f"matching {shown}"


def _branch(name: str) -> str:
    shown = ref(name)
    return shown if "*" not in shown else f"a branch {_pattern(shown)}"


def _file(path: str) -> str:
    shown = ref(path)
    return shown if "*" not in shown else f"a file {_pattern(shown)}"


def _no_file(path: str) -> str:
    shown = ref(path)
    return f"no {shown}" if "*" not in shown else f"no file {_pattern(shown)}"


def _the_branch(name: str) -> tuple[str, str]:
    shown = ref(name)
    if "*" not in shown:
        return f"the branch {shown}", f"no branch {shown}"
    return f"a branch {_pattern(shown)}", f"no branch {_pattern(shown)}"


def branch_phrase(name: str) -> str:
    return _the_branch(name)[0]


def _approval(head: str, base: str, by: str = "") -> tuple[str, str]:
    who = f" from {by}" if by else ""
    pr = f"the pull request from {_branch(head)} into {_branch(base)}"
    return f"an approval{who} on {pr}", f"no approval{who} on {pr}"


#: Each predicate's `(need, negated need)`, from its expanded arguments.
_WORDS: Final[Mapping[str, Callable[..., tuple[str, str]]]] = MappingProxyType(
    {
        "branch_exists": _the_branch,
        "file_exists": lambda path, branch: (
            f"{_file(path)} on {_branch(branch)}",
            f"{_no_file(path)} on {_branch(branch)}",
        ),
        "pr_open": lambda head, base: (
            f"an open pull request from {_branch(head)} into {_branch(base)}",
            f"no open pull request from {_branch(head)} into {_branch(base)}",
        ),
        "pr_merged": lambda head, base: (
            f"the pull request from {_branch(head)} merged into {_branch(base)}",
            f"no merged pull request from {_branch(head)} into {_branch(base)}",
        ),
        "pr_approved": _approval,
        "pr_changes_requested": lambda head: (
            f"a change request on the pull request from {_branch(head)}",
            f"no change request on the pull request from {_branch(head)}",
        ),
        "ci_green": lambda branch: (
            f"passing CI on {_branch(branch)}",
            f"no passing CI on {_branch(branch)}",
        ),
        "ticket_label": lambda label: (
            f"the {label} label on the issue",
            f"no {label} label on the issue",
        ),
        "ticket_has_assignee": lambda: ("an assignee on the issue", "no assignee on the issue"),
        "issue_comment_contains": lambda marker: (
            f'a comment on the issue containing "{marker}"',
            f'no comment on the issue containing "{marker}"',
        ),
        "pr_comment_contains": lambda head, marker: (
            f'a comment containing "{marker}" on the pull request from {_branch(head)}',
            f'no comment containing "{marker}" on the pull request from {_branch(head)}',
        ),
    }
)

# A predicate with no words would reach a person as rule code: fail at import, as `library.py`
# does for an implementation the arity table does not declare.
if set(_WORDS) != set(PREDICATE_ARITY):
    raise RuntimeError(
        "predicate words disagree with the arity table: "
        f"unworded: {sorted(set(PREDICATE_ARITY) - set(_WORDS)) or '-'}; "
        f"undeclared: {sorted(set(_WORDS) - set(PREDICATE_ARITY)) or '-'}"
    )


def need(name: str, args: Sequence[str], *, negated: bool = False) -> str:
    """What the call `name(*args)` asks for, or with `negated` what its `not` asks for. `args` are
    already expanded; a name outside the library reads as its call."""
    words = _WORDS.get(name)
    if words is None:
        call = render_call(name, args)
        return f"not {call}" if negated else call
    positive, negative = words(*args)
    return negative if negated else positive


def describe(condition: Condition, values: Mapping[str, str]) -> str:
    """The whole condition in words, its template values expanded as the evaluator expands them."""
    match condition:
        case PredicateCall():
            return need(condition.name, [expand(arg, values) for arg in condition.args])
        case Not(not_=PredicateCall() as call):
            return need(call.name, [expand(arg, values) for arg in call.args], negated=True)
        case Not():
            return f"not ({describe(condition.not_, values)})"
        case AllOf():
            return " and ".join(describe(child, values) for child in condition.all_of)
        case AnyOf():
            return " or ".join(describe(child, values) for child in condition.any_of)
        case _:  # pragma: no cover - the union is closed
            assert_never(condition)
