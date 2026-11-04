"""Template expansion for ADLC config strings (design.md §4).

Not `str.format`: config strings carry JSON-ish braces and `{roles.x}` dotted names, which
`format` would choke on or resolve by attribute access. Only `{name}` and `{roles.<name>}` are
recognised; everything else is literal text.

`expand` leaves an unknown placeholder in place, because an unexpanded `{slug}` is meaningful —
it marks the part of a branch name the agent chooses at runtime, which predicates match with a
`*` glob. Call sites needing a fully resolved string use `expand_strict`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

#: Placeholders the orchestrator supplies. `{roles.<name>}` is resolved separately, against the
#: process config's `roles:` block, and only by `expand_principals`.
KNOWN_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "ticket",
        "ticket_owner",  # assignee at admission — stable across reassignment
        "repo",
        "pr_url",
        # agent-chosen; literal (globbed) until observed. Recomputed every tick by
        # `worker/templates.py`'s `_resolve_slug`, reverting to literal if a later artifact makes
        # the observed name ambiguous. Feeds directives only (workspace.branch, predicate
        # arguments) — the orchestrator's own PR/gate lookups (`_glob`) stay `*`-globbed, because
        # a matcher must only ever widen against what actually happened.
        "slug",
        "step",
        "attempt",
        "failure_reason",  # the whole reason, rule code and raw output included
        "failure_summary",  # the one plain sentence a person reads (worker/failures.py)
        "contract_sha",  # kind: action's fix_the_contract onSuccess (actions.registry)
    }
)

ROLE_PREFIX: Final = "roles."

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)?)\}")


class TemplateError(ValueError):
    """A template could not be resolved (unknown placeholder, unknown role, role cycle)."""


def placeholders(template: str) -> tuple[str, ...]:
    """Placeholder names in `template`, in first-appearance order, deduplicated."""
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER.finditer(template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def expand(template: str, values: Mapping[str, str]) -> str:
    """Substitute known placeholders; leave unknown ones exactly as written."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def globbed(template: str) -> str:
    """Every placeholder still standing after expansion becomes `*` — the agent's own choice is
    the only thing that can be left, and a glob is how the whole system matches it."""
    for name in placeholders(template):
        template = template.replace(f"{{{name}}}", "*")
    return template


def expand_strict(template: str, values: Mapping[str, str]) -> str:
    """Substitute placeholders, raising if any is unresolved."""
    missing = [name for name in placeholders(template) if name not in values]
    if missing:
        raise TemplateError(
            f"Unresolved placeholder(s) {', '.join(sorted(missing))} in {template!r}. "
            f"Supplied: {', '.join(sorted(values)) or 'none'}."
        )
    return expand(template, values)


def _role_reference(entry: str) -> str | None:
    """`{roles.spec_reviewers}` -> `spec_reviewers`; anything else -> None."""
    match = _PLACEHOLDER.fullmatch(entry.strip())
    if match is None:
        return None
    name = match.group(1)
    return name[len(ROLE_PREFIX) :] if name.startswith(ROLE_PREFIX) else None


def expand_principals(
    entries: Sequence[str],
    roles: Mapping[str, Sequence[str]],
    values: Mapping[str, str],
) -> tuple[str, ...]:
    """Resolve an `assign:` / `approvers:` list to concrete logins.

    An entry is a literal login, a template (`{ticket_owner}`), or a whole-entry role reference
    (`{roles.spec_reviewers}`) whose members are themselves entries — the sample's roles are
    defined as `["{ticket_owner}"]`, so resolution recurses. Order is preserved and duplicates
    dropped, because these lists *replace* the issue's assignees.
    """
    resolved: dict[str, None] = {}
    for entry in entries:
        for login in _resolve_entry(entry, roles, values, ()):
            resolved.setdefault(login, None)
    return tuple(resolved)


def _resolve_entry(
    entry: str,
    roles: Mapping[str, Sequence[str]],
    values: Mapping[str, str],
    stack: tuple[str, ...],
) -> list[str]:
    role = _role_reference(entry)
    if role is None:
        # A role reference has to be the whole entry: there is nothing sensible to splice a
        # multi-member list into the middle of a string.
        embedded = [p for p in placeholders(entry) if p.startswith(ROLE_PREFIX)]
        if embedded:
            raise TemplateError(
                f"Role reference {embedded[0]!r} must be the entire entry, not embedded in "
                f"{entry!r}."
            )
        return [expand_strict(entry, values)]

    if role in stack:
        raise TemplateError(f"Role reference cycle: {' -> '.join((*stack, role))}.")
    members = roles.get(role)
    if members is None:
        raise TemplateError(
            f"Unknown role {role!r}. Defined roles: {', '.join(sorted(roles)) or 'none'}."
        )
    return [
        login
        for member in members
        for login in _resolve_entry(member, roles, values, (*stack, role))
    ]
