"""Which ADLC lane an issue enters, resolved before any predicate runs (design.md §2.10).

`entry.when` answers "is this issue ready to start?". It does not answer "which lane?". Making
one condition answer both would turn mutual exclusivity across lanes into an operator obligation
that fails silently as lanes are added — and the setup wizard emits an identical
`factorial:new` + assignee entry condition for every lane it generates, so any two wizard-built
lanes would be born ambiguous.

So selection is its own step, and it is explicit: a `factorial:process:<key>` label on the issue,
or the project's `default_process` when it carries none. Only then is the selected lane's
`entry.when` evaluated, and a false one means the issue waits for readiness — it never tries
another lane.

An unknown label is refused rather than defaulted, because the two failure modes are not
symmetrical: an operator who typos `factorial:process:hotfxi` and silently gets the default has
their hotfix run the standard lane, and finds out days later, at the gate.

One policy, one implementation. No Protocol and no second implementation — a Protocol with a
single implementor is an abstraction for single-use code.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final, Literal

from orchestrator.github import LABEL_PREFIX, LABEL_PREFIXES

__all__ = [
    "PROCESS_INFIX",
    "NotSelected",
    "Selected",
    "Selection",
    "SelectionRefusal",
    "process_label",
    "select_process",
    "selected_keys",
]

#: `factorial:process:<key>`. Colon-separated, and it collides with nothing: `step_from_label`
#: reads it as the step name `process:<key>`, which matches no step, and `ticket_label()` handles
#: it like any other name.
PROCESS_INFIX: Final = "process:"

#: Why an issue could not be assigned a lane at all. "Selected, but `entry.when` is not satisfied
#: yet" is deliberately NOT one of these: that is the normal waiting state of every issue nobody
#: has labelled yet, and admission reports it separately (`worker.admission`).
SelectionRefusal = Literal["no_default", "unknown_process", "ambiguous_process"]


@dataclass(frozen=True, slots=True)
class Selected:
    """The lane this issue runs, before its `entry.when` has been consulted."""

    key: str
    #: True when the key came from a `factorial:process:<key>` label rather than the default.
    explicit: bool


@dataclass(frozen=True, slots=True)
class NotSelected:
    reason: SelectionRefusal
    detail: str


Selection = Selected | NotSelected


def process_label(key: str) -> str:
    """The label that selects lane `key`. One vocabulary is written; both are read below."""
    return f"{LABEL_PREFIX}{PROCESS_INFIX}{key}"


def selected_keys(labels: Iterable[str]) -> tuple[str, ...]:
    """Every lane key named by a selection label on the issue, sorted and deduplicated.

    Both prefixes are accepted, the rule `github.projector.step_from_label` follows for the same
    reason: a repo onboarded before issue #138 still spells the namespace `adlc:`, and a gesture
    the loop refuses to read is a gesture an operator cannot make.
    """
    keys = {
        label.removeprefix(prefix).removeprefix(PROCESS_INFIX)
        for label in labels
        for prefix in LABEL_PREFIXES
        if label.startswith(f"{prefix}{PROCESS_INFIX}")
    }
    return tuple(sorted(key for key in keys if key))


def select_process(
    labels: Iterable[str], *, available: Collection[str], default: str | None
) -> Selection:
    """Resolve one issue's lane from its labels and the project's default.

    `available` is the set of lane keys the last sync still found in `.factorial/`
    (`process_version.retired_at`), never every key the project has history for — version history
    is not a menu, or deleting a lane's file would leave it offerable for ever.
    """
    named = selected_keys(labels)
    if len(named) > 1:
        return NotSelected(
            reason="ambiguous_process",
            detail=(
                "the issue carries "
                + ", ".join(process_label(key) for key in named)
                + "; one process label picks the issue's process, so remove all but one"
            ),
        )
    if named:
        key = named[0]
        if key not in available:
            return NotSelected(
                reason="unknown_process",
                detail=(
                    f"{process_label(key)} names no process this project offers "
                    f"({_offered(available)}); fix the label, or add "
                    f"processes/process-{key}.yaml to the repo and sync"
                ),
            )
        return Selected(key=key, explicit=True)

    if default is None:
        return NotSelected(
            reason="no_default",
            detail=(
                "this project has no default process, so every issue must carry a process "
                f"label ({_offered(available)}); set one on the project, or label the issue"
            ),
        )
    if default not in available:
        return NotSelected(
            reason="unknown_process",
            detail=(
                f"the project's default process {default!r} is not offered by the repo "
                f"({_offered(available)}); change the default, or restore its file and sync"
            ),
        )
    return Selected(key=default, explicit=False)


def _offered(available: Collection[str]) -> str:
    return (
        "available: " + ", ".join(sorted(available)) if available else "this project offers none"
    )
