"""The `.factorial/` folder every scaffold path is written under, and the committable output of
`assemble.assemble`: every file to write, which are stubs, which shared components are new
versus already owned by the target repo, and every file to delete."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["CONFIG_DIR", "ScaffoldBundle"]

CONFIG_DIR = ".factorial"


@dataclass(frozen=True, slots=True)
class ScaffoldBundle:
    files: Mapping[str, str]
    #: Paths in `files` that are comments-only stubs. They do not parse, so validation stands
    #: something synthetic in for them rather than reading these (`scaffold.validate`).
    stub_paths: frozenset[str]
    #: Shared skills written under `.factorial/skills/`. Empty when none was selected, and when the
    #: target repo already has them — a skill is protected exactly as a prompt is.
    shared_skills: frozenset[str] = frozenset()
    #: Shared skills the target repo already had. Deliberately absent from `files`, which is why
    #: validation must be told about them; without it their absence reads as a defect.
    present_skills: frozenset[str] = frozenset()
    #: Shared MCP servers the target repo already had. Deliberately absent from `files`, so
    #: validation must be told about them; without it their absence reads as a defect.
    present_mcp: frozenset[str] = frozenset()
    #: Files the commit removes from the branch. Never in `files`.
    deleted: frozenset[str] = frozenset()
