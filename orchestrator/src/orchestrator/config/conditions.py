"""Condition AST — `all` / `any` / `not` over the closed predicate library (design.md §6).

Config may only reference predicates; a new one is a code change here plus an implementation in
`orchestrator.predicates`, which imports `PREDICATE_ARITY` so names and arities have one source
of truth. Call strings go through a hand-written scanner, never `eval`: this is configuration,
not code.

Nodes serialize back to the shape they were written in, so a parsed process round-trips into
`process_version.content` — dump with `by_alias=True`, since `all` / `any` / `not` are aliases
of Python-legal attribute names.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Any, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_serializer,
    model_validator,
)

#: The closed predicate library: name -> (min args, max args). design.md §6.
#: `orchestrator.predicates` implements these; this table only declares them.
PREDICATE_ARITY: Final[Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "branch_exists": (1, 1),
        "file_exists": (2, 2),
        "pr_open": (2, 2),
        "pr_merged": (2, 2),
        "pr_approved": (2, 3),  # (head, base, by?) — `by` is an optional role/login
        "ci_green": (1, 1),
        "ticket_label": (1, 1),
        "ticket_has_assignee": (0, 0),
        "pr_changes_requested": (1, 1),  # (head) — mirrors ci_green's single glob argument
        "issue_comment_contains": (1, 1),  # (marker)
        "pr_comment_contains": (2, 2),  # (head, marker)
    }
)

#: The postcondition predicates a spawn turns into a delivery probe. Declared here beside the
#: arity table so lint and the worker read one registry; `worker.templates` maps each name to
#: its artifact kind and asserts at import that the two tables agree — the same shape the
#: action registry uses.
DELIVERY_PREDICATE_NAMES: Final[frozenset[str]] = frozenset(
    {"branch_exists", "pr_open", "issue_comment_contains", "pr_comment_contains"}
)

#: Which argument of a predicate call is a delivery-evidence marker and so must not be a literal
#: empty string — name -> zero-based index into `args`. An empty marker does not merely read
#: false; `runtime.contract.ExpectedArtifact` cannot be built from it at all, so rejecting it
#: here turns a per-spawn `ValueError` into one load-time error. A placeholder marker that
#: resolves to empty is invisible here and handled by `worker.templates._derive_selector`.
_NONEMPTY_MARKER_ARG: Final[Mapping[str, int]] = MappingProxyType(
    {
        "issue_comment_contains": 0,
        "pr_comment_contains": 1,
    }
)

_QUOTES: Final = ("'", '"')


def _allowed_predicates() -> str:
    return ", ".join(
        f"{name}/{lo}" if lo == hi else f"{name}/{lo}..{hi}"
        for name, (lo, hi) in PREDICATE_ARITY.items()
    )


def _malformed(source: str, detail: str) -> ValueError:
    return ValueError(
        f"Malformed predicate call {source!r}: {detail}. "
        'Write name("arg", "arg") with double- or single-quoted string literals only.'
    )


def parse_call(source: str) -> tuple[str, tuple[str, ...]]:
    """Parse `name("a", "b")` into its name and literal arguments. Strict by design."""
    text = source.strip()
    length = len(text)
    pos = 0

    while pos < length and (text[pos].isalnum() or text[pos] == "_"):
        pos += 1
    name = text[:pos]
    if not name or name[0].isdigit():
        raise _malformed(source, "expected a predicate name")

    if pos >= length or text[pos] != "(":
        raise _malformed(source, "expected '(' after the predicate name")
    pos += 1

    args: list[str] = []
    while pos < length and text[pos].isspace():
        pos += 1
    if pos < length and text[pos] == ")":
        pos += 1
    else:
        while True:
            if pos >= length or text[pos] not in _QUOTES:
                raise _malformed(source, "expected a quoted string argument")
            quote = text[pos]
            pos += 1
            end = text.find(quote, pos)
            if end == -1:
                raise _malformed(source, "unterminated string argument")
            args.append(text[pos:end])
            pos = end + 1
            while pos < length and text[pos].isspace():
                pos += 1
            if pos >= length:
                raise _malformed(source, "expected ')'")
            if text[pos] == ",":
                pos += 1
                while pos < length and text[pos].isspace():
                    pos += 1
                continue
            if text[pos] == ")":
                pos += 1
                break
            raise _malformed(source, f"unexpected {text[pos]!r}")

    if text[pos:].strip():
        raise _malformed(source, f"trailing text {text[pos:]!r}")
    return name, tuple(args)


def render_call(name: str, args: Sequence[str]) -> str:
    rendered = ", ".join(f'"{arg}"' for arg in args)
    return f"{name}({rendered})"


class PredicateCall(BaseModel):
    """One call into the predicate library, e.g. `branch_exists("spec/{ticket}-*")`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    args: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _from_source(cls, value: Any) -> Any:
        if isinstance(value, str):
            name, args = parse_call(value)
            return {"name": name, "args": args}
        return value

    @model_validator(mode="after")
    def _check_registry(self) -> Self:
        arity = PREDICATE_ARITY.get(self.name)
        if arity is None:
            raise ValueError(
                f"Unknown predicate {self.name!r}. Allowed predicates: {_allowed_predicates()}."
            )
        low, high = arity
        if not low <= len(self.args) <= high:
            expected = f"{low}" if low == high else f"{low} to {high}"
            raise ValueError(
                f"Predicate {self.name!r} takes {expected} argument(s), but {self.source!r} "
                f"passes {len(self.args)}."
            )
        index = _NONEMPTY_MARKER_ARG.get(self.name)
        if index is not None and not self.args[index]:
            raise ValueError(
                f"Predicate {self.name!r}: argument {index} (the marker) must be a non-empty "
                f"string in {self.source!r}. An empty marker matches no comment, so the "
                "condition can never hold."
            )
        return self

    @property
    def source(self) -> str:
        """The call written the way config writes it — also the serialized form."""
        return render_call(self.name, self.args)

    @model_serializer
    def _to_source(self) -> str:
        return self.source


class AllOf(BaseModel):
    """`all: [...]` — every child must hold."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    all_of: tuple[Condition, ...] = Field(alias="all", min_length=1)


class AnyOf(BaseModel):
    """`any: [...]` — at least one child must hold."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    any_of: tuple[Condition, ...] = Field(alias="any", min_length=1)


class Not(BaseModel):
    """`not: <condition>` — negation of a single child."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    not_: Condition = Field(alias="not")


def _condition_tag(value: Any) -> str | None:
    """Route a raw YAML node (or an already-built node) to its union member."""
    if isinstance(value, str | PredicateCall):
        return "predicate"
    if isinstance(value, AllOf):
        return "all"
    if isinstance(value, AnyOf):
        return "any"
    if isinstance(value, Not):
        return "not"
    if isinstance(value, Mapping):
        keys = set(value)
        # Aliases are what config uses; the attribute names keep a non-aliased dump re-parseable.
        for tag, attr in (("all", "all_of"), ("any", "any_of"), ("not", "not_")):
            if keys == {tag} or keys == {attr}:
                return tag
        if "name" in keys and keys <= {"name", "args"}:
            return "predicate"
    return None


#: A condition node: a predicate call or a boolean composition of them.
Condition = Annotated[
    Annotated[PredicateCall, Tag("predicate")]
    | Annotated[AllOf, Tag("all")]
    | Annotated[AnyOf, Tag("any")]
    | Annotated[Not, Tag("not")],
    Discriminator(_condition_tag),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def iter_calls(condition: Condition | None) -> Iterator[PredicateCall]:
    """Every predicate call in the condition, at any depth, under `any:` and `not:` too."""
    match condition:
        case PredicateCall():
            yield condition
        case AllOf():
            for child in condition.all_of:
                yield from iter_calls(child)
        case AnyOf():
            for child in condition.any_of:
                yield from iter_calls(child)
        case Not():
            yield from iter_calls(condition.not_)
        case _:
            return


def iter_conjunctive(condition: Condition | None) -> Iterator[PredicateCall]:
    """Every predicate call the condition asserts must hold, on its own.

    Only conjunctive positions: the condition itself and the children of `all:`. A call under
    `any:` is one alternative among several and a call under `not:` asserts the opposite, so
    neither states "this must hold" — the worker derives delivery expectations from this walk
    and lint warns when no delivery predicate survives it.
    """
    match condition:
        case PredicateCall():
            yield condition
        case AllOf():
            for child in condition.all_of:
                yield from iter_conjunctive(child)
        case _:
            return
