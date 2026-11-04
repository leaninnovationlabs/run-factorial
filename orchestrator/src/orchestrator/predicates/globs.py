"""`*`-glob matching for predicate arguments (design.md §6).

Only branch names, PR head/base refs and file paths glob, because the agent chooses part of a
branch name (a slug) at runtime: `branch_exists("spec/{ticket}-*")`. Label names do not glob —
the orchestrator writes them, so `ticket_label` compares exactly.

Everything goes through the helpers here rather than `fnmatch` at each call site, so what a `*`
means is decided in one place.

Accepted imprecision: `fnmatch`'s `*` crosses `/`, so `specs/12-*/spec.md` also matches
`specs/12-a/nested/spec.md`. The predicate still requires a real file at a real ref, so a
segment-aware matcher is not worth the bespoke implementation.

Preview patterns (`preview.files`, tech-stack decision 71) are the exception, because choosing
the right files among everything a step changed is their whole job: they follow standard glob
rules, `*` inside one folder and `**` across any number of them, as `.gitignore` and CODEOWNERS
do (`matches_file_pattern`).
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from orchestrator.github.state import BranchState

__all__ = [
    "fixed_folder",
    "has_glob",
    "matches_file_pattern",
    "matches_path",
    "matches_ref",
    "resolve_refs",
]


def has_glob(pattern: str) -> bool:
    return "*" in pattern


def _match(pattern: str, value: str) -> bool:
    # A literal (no `*`) must match exactly — going through fnmatch would give `?` and `[...]`
    # meanings config never asked for.
    return fnmatchcase(value, pattern) if has_glob(pattern) else value == pattern


def matches_ref(pattern: str, ref: str) -> bool:
    """Match a branch name or a PR head/base ref against a `*` glob or a literal."""
    return _match(pattern, ref)


def matches_path(pattern: str, path: str) -> bool:
    """Match a repo-relative file path against a `*` glob or a literal."""
    return _match(pattern, path)


def matches_file_pattern(pattern: str, path: str) -> bool:
    """Match a repo-relative file path against a preview pattern."""
    return PurePosixPath(path).full_match(pattern)


def fixed_folder(pattern: str) -> str:
    """The folder every match of `pattern` sits under: its segments before the first wildcard
    one, or "" when that is the first."""
    segments = pattern.split("/")
    fixed = next((at for at, segment in enumerate(segments) if "*" in segment), len(segments))
    return "/".join(segments[:fixed])


def resolve_refs(branches: Iterable[BranchState], ref_glob: str) -> tuple[BranchState, ...]:
    """Concrete branches a ref argument names: many for a glob, at most one for a literal."""
    return tuple(branch for branch in branches if matches_ref(ref_glob, branch.name))
