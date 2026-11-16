"""A run's declared files (tech-stack decision 71) as the log recorded them: the latest
`run_outputs_observed` per run IS that run's footprint, and `select_files` picks from it what the
step's pinned `preview.files` declares, in declaration order.

One pure function serves the API's answer and the page route's check (`api/services/outputs.py`),
so a file the dashboard lists is exactly a file `/pages/{id}` will serve. A recorded file the
config does not declare is selected by neither.

A pattern gives one entry per matching file still on the branch, sorted by path and cut at
`PATTERN_MAX_MATCHES`, each named by its path below the pattern's fixed folder. A file shows
under one entry only: files named exactly claim theirs first, whatever the order, then each
pattern in turn. A pattern whose every file another entry shows adds nothing.

Where a run's branches stood at its spawn (`agent_spawned`'s `branch_heads`) is read here too:
the worker starts a run's footprint there, and whether a run has pushed (`pushed`) is judged
against it for the dashboard's review and the park comment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import PreviewFile, expand
from orchestrator.db.models import Event, StepRun
from orchestrator.events.read_models import latest_run_event
from orchestrator.events.types import EventType
from orchestrator.github.state import BranchState
from orchestrator.predicates.globs import fixed_folder, matches_file_pattern, matches_path
from orchestrator.predicates.words import branch_phrase
from runtime.contract import PATTERN_MAX_MATCHES

__all__ = [
    "FileStatus",
    "SelectedFile",
    "files_on_branch",
    "heads_by_name",
    "outputs_observation",
    "pushed",
    "select_files",
    "spawn_heads",
]

FileStatus = Literal["pending", "published", "missing", "ambiguous", "lookup_failed"]


@dataclass(frozen=True, slots=True)
class SelectedFile:
    #: The declared path template, which names the entry across attempts.
    key: str
    label: str
    status: FileStatus
    path: str | None
    blob: str | None
    commit: str | None
    size: int | None
    #: Whether this attempt changed it; None where the record could not tell.
    changed: bool | None
    matches: tuple[str, ...]
    reason: str | None
    #: The pattern's label, on every entry a pattern gives; None for a file named exactly.
    group: str | None = None
    #: How many files the pattern matched before `PATTERN_MAX_MATCHES` cut it, on its files.
    group_total: int | None = None
    #: The folder every file the pattern matches sits under (`fixed_folder`), "" for none.
    folder: str | None = None


async def outputs_observation(session: AsyncSession, run: StepRun) -> dict[str, Any] | None:
    """The details of the run's latest record, or None before the worker has looked."""
    return (
        await session.execute(
            latest_run_event(run.ticket_id, run.id, [EventType.RUN_OUTPUTS_OBSERVED], Event.details)
        )
    ).scalar_one_or_none()


async def spawn_heads(session: AsyncSession, run: StepRun) -> dict[str, str]:
    """The `branch_heads` this run's spawn recorded, by name; empty for a spawn that recorded
    none, as a step that declares no `preview.files` does."""
    spawned = (
        await session.execute(
            latest_run_event(run.ticket_id, run.id, [EventType.AGENT_SPAWNED], Event.details)
        )
    ).scalar_one_or_none()
    return heads_by_name((spawned or {}).get("branch_heads"))


def heads_by_name(value: object) -> dict[str, str]:
    """`branch_heads` as name -> sha: a list of `{name, sha}`, or the map events before it wrote."""
    if isinstance(value, dict):
        return {str(name): str(sha) for name, sha in value.items()}
    if isinstance(value, list):
        return {
            str(item["name"]): str(item["sha"])
            for item in value
            if isinstance(item, dict) and "name" in item and "sha" in item
        }
    return {}


def pushed(head: BranchState | None, heads: Mapping[str, str]) -> bool:
    """Has the run moved `head`, its step's one branch, from where `heads` (`spawn_heads`) says
    it stood at spawn? A branch with no head recorded counts once it exists. On a lane whose
    steps share a branch, the earlier step's head is not this run's work."""
    return head is not None and heads.get(head.name) != head.sha


def files_on_branch(observed: Mapping[str, Any] | None) -> list[str]:
    """The paths the record holds that are still on the run's branch, sorted: what a missing
    entry lists, so whoever wrote the config can correct a path. Never served."""
    if observed is None or observed.get("status") != "observed":
        return []
    return sorted(f["path"] for f in observed.get("files") or () if f.get("blob") is not None)


def select_files(
    declared: Sequence[PreviewFile], observed: Mapping[str, Any] | None, *, ticket: str
) -> list[SelectedFile]:
    """One entry per declared file, and per file a pattern matched, in declaration order. A
    `{slug}` the run never named is a `*` glob, and a file named exactly that two files match is
    `ambiguous`, never a guess."""
    literals = {
        at: _select(item, observed, ticket)
        for at, item in enumerate(declared)
        if not item.is_pattern
    }
    claimed = {
        path
        for chosen in literals.values()
        for path in ((chosen.path,) if chosen.path else ()) + chosen.matches
    }
    selected: list[SelectedFile] = []
    for at, item in enumerate(declared):
        if at in literals:
            selected.append(literals[at])
        else:
            selected.extend(_select_pattern(item, observed, ticket, claimed))
    return selected


def _select_pattern(
    item: PreviewFile, observed: Mapping[str, Any] | None, ticket: str, claimed: set[str]
) -> list[SelectedFile]:
    """The entries one pattern gives, claiming the files it shows."""
    slug = observed.get("slug") if observed is not None else None
    pattern = expand(item.path, {"ticket": ticket, "slug": slug if isinstance(slug, str) else "*"})
    folder = fixed_folder(pattern)
    whole = {"group": item.label, "folder": folder}
    if observed is None or observed.get("status") != "observed":
        return [replace(_select(item, observed, ticket), **whole)]
    hits = sorted(
        f["path"]
        for f in observed.get("files") or ()
        if f.get("blob") is not None and matches_file_pattern(pattern, f.get("path", ""))
    )
    if not hits:
        return [_none_matched(item, pattern, observed, folder)]
    left = [path for path in hits if path not in claimed]
    # Only what it shows: a file past the cap is still a later pattern's to show.
    kept = left[:PATTERN_MAX_MATCHES]
    claimed.update(kept)
    files = {f["path"]: f for f in observed.get("files") or ()}
    prefix = f"{folder}/" if folder else ""
    return [
        SelectedFile(
            key=f"{item.path}#{path}",
            label=path.removeprefix(prefix),
            status="published",
            path=path,
            blob=files[path]["blob"],
            commit=observed.get("commit"),
            size=files[path].get("size"),
            changed=files[path].get("changed"),
            matches=(),
            reason=None,
            group=item.label,
            group_total=len(left),
            folder=folder,
        )
        for path in kept
    ]


def _none_matched(
    item: PreviewFile, pattern: str, observed: Mapping[str, Any], folder: str
) -> SelectedFile:
    if observed.get("cut_off"):
        # Past GitHub's limit the worker may not have seen them: not proof the step wrote none.
        status: FileStatus = "lookup_failed"
        reason = (
            "This attempt changed more than 300 files, so a file matching "
            f"{pattern} may not be listed."
        )
    else:
        status = "missing"
        reason = f"The attempt left no file matching {pattern} on {observed.get('branch')}."
    return SelectedFile(
        key=item.path,
        label=item.display_label,
        status=status,
        path=None,
        blob=None,
        commit=None,
        size=None,
        changed=None,
        matches=(),
        reason=reason,
        group=item.label,
        folder=folder,
    )


def _select(item: PreviewFile, observed: Mapping[str, Any] | None, ticket: str) -> SelectedFile:
    label = item.display_label

    def entry(status: FileStatus, **found: Any) -> SelectedFile:
        fields: dict[str, Any] = {
            "path": None,
            "blob": None,
            "commit": None,
            "size": None,
            "changed": None,
            "matches": (),
            "reason": None,
        }
        return SelectedFile(key=item.path, label=label, status=status, **(fields | found))

    if observed is None:
        return entry("pending")
    recorded = observed.get("status")
    if recorded == "no_branch":
        return entry(
            "missing",
            reason=f"The agent didn't push {branch_phrase(str(observed.get('branch')))}.",
        )
    if recorded == "ambiguous":
        return entry("ambiguous", matches=tuple(observed.get("matches") or ()))
    if recorded != "observed":
        reason = observed.get("reason") or f"Unknown record status {recorded!r}."
        return entry("lookup_failed", reason=reason)

    slug = observed.get("slug")
    pattern = expand(item.path, {"ticket": ticket, "slug": slug if isinstance(slug, str) else "*"})
    hits = [f for f in observed.get("files") or () if matches_path(pattern, f.get("path", ""))]
    if not hits and observed.get("cut_off"):
        # Past GitHub's limit the worker may not have seen it: not proof the step did not write it.
        reason = "This attempt changed more than 300 files, so this file may not be listed."
        return entry("lookup_failed", reason=reason)
    if not hits:
        return entry(
            "missing", reason=f"The attempt left no {pattern} on {observed.get('branch')}."
        )
    if len(hits) > 1:
        return entry("ambiguous", matches=tuple(sorted(f["path"] for f in hits)))
    (hit,) = hits
    if hit.get("blob") is None:
        reason = (
            "This attempt removed it."
            if hit.get("changed")
            else "It was not on the branch at this attempt's end."
        )
        return entry("missing", path=hit["path"], reason=reason)
    return entry(
        "published",
        path=hit["path"],
        blob=hit["blob"],
        commit=observed.get("commit"),
        size=hit.get("size"),
        changed=hit.get("changed"),
    )
