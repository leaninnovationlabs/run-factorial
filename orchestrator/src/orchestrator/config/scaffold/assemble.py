"""Turning an operator's chosen files into a bundle the App may commit.

This is where the guarantee that the route cannot be steered outside `.factorial/` is enforced.
A picked file must be in the proposal computed from the parsed config and the repo's own
contents, and never over a file the repo already has: the catalog does not rewrite a human's
prompt. An edited file must be under `.factorial/` in the config branch's own listing at the head
the request read, and the branch must still hold the blob the edit started from (`EditConflict`
otherwise), so an edit never overwrites a change it did not see. A created file is the one path
the client names freely, so it is proven rather than looked up: every segment under
`.factorial/` a safe name (`_SAFE_NAME`, so no `..` and no dot-folder), and nothing on the branch
at that path, above it or below it — a create never overwrites. A submitted body is data: it is
written and compared, never executed and never interpolated into a command.

The process file is taken from `yaml_text` and never from the selection. Accepting a body for it
would let the committed YAML differ from the config that was parsed and used to compute the
legal path set. Its PATH is derived here from the parsed config's own `name:` —
server-side, never from the request — which is the same bound applied to one more segment, not a
weaker one.

One layout is written: `.factorial/processes/process-<name>.yaml`. The legacy root
`process.yaml` is read for ever and never written again (decision 37), and a repo still carrying
it is refused before this function runs (`api.routes.admin._refuse_legacy_layout`) — writing the
new path beside the old file would create the mixed layout the loader refuses at sync.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orchestrator.config.loader import AGENT_FILE, AGENTS_DIR, PROCESSES_DIR, process_file_name
from orchestrator.config.models import AgentStep, ProcessConfig
from orchestrator.config.scaffold import CONFIG_DIR, ScaffoldBundle
from orchestrator.config.scaffold.catalog import TEMPLATE_ROOT
from orchestrator.config.scaffold.facts import derive_agent_facts
from orchestrator.config.scaffold.propose import Proposal, substitute

__all__ = [
    "EditConflict",
    "FileEdit",
    "SelectionError",
    "assemble",
    "process_bundle_path",
]

#: An agent name, and now a process name, becomes a path segment this function builds itself
#: rather than looks up, so these are the one place the "every path is a member of a
#: server-computed proposal" guarantee does not reach — `AgentStep.agent` carries no pattern
#: (`config/models.py`), so a config could otherwise name an agent `../../.github/workflows` and
#: walk the stub write outside `.factorial/`, onto exactly the path the App is never granted
#: `workflows: write` to protect (design.md §7).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def process_bundle_path(name: str) -> str:
    """Where a bundle writes the process named `name`.

    Guarded here rather than trusted from upstream: the loader's slug rule is stricter and the
    route applies it, but this is the write site, and a path segment must be proven safe where
    it is built.
    """
    if not _SAFE_NAME.match(name):
        raise SelectionError(
            f"process name {name!r} must be a single file name (match {_SAFE_NAME.pattern!r}) "
            "to be written"
        )
    return f"{CONFIG_DIR}/{PROCESSES_DIR}/{process_file_name(name)}"


class SelectionError(Exception):
    """A selection the server will not commit. The detail names the offending path."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FileEdit:
    """A file the config branch already has, rewritten by the operator from its own copy."""

    path: str
    body: str
    #: The git blob the edit started from.
    base_sha: str


class EditConflict(Exception):
    """An edit whose file changed on the branch after the operator opened it."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _check_created(path: str, blobs: Mapping[str, str], proposal: Proposal) -> None:
    prefix = f"{CONFIG_DIR}/"
    segments = path.removeprefix(prefix).split("/") if path.startswith(prefix) else []
    if not segments or not all(_SAFE_NAME.match(segment) for segment in segments):
        raise SelectionError(
            f"{path} cannot be created: a new file goes under {prefix}, and every folder and "
            f"file name in its path must match {_SAFE_NAME.pattern!r}"
        )
    ancestors = {"/".join(path.split("/")[:end]) for end in range(1, path.count("/") + 1)}
    if path in blobs or any(other in ancestors or other.startswith(f"{path}/") for other in blobs):
        raise SelectionError(
            f"{path} cannot be created: the config branch already has a file at that path, "
            "above it or inside it"
        )
    if path in proposal.files:
        raise SelectionError(f"{path} is offered by the catalog; pick it from the proposal instead")


def assemble(
    config: ProcessConfig,
    yaml_text: str,
    selected: Mapping[str, str],
    proposal: Proposal,
    *,
    edits: Sequence[FileEdit] = (),
    created: Mapping[str, str] | None = None,
    repo_blobs: Mapping[str, str] | None = None,
) -> ScaffoldBundle:
    process_path = process_bundle_path(config.name)
    for path in sorted(selected):
        if path == process_path:
            raise SelectionError(
                f"{process_path} is committed from the submitted config text, not from the file "
                "selection; remove it from `files`"
            )
        proposed = proposal.files.get(path)
        if proposed is None:
            raise SelectionError(f"{path} was not proposed for this config and will not be written")
        if proposed.present_in_repo:
            raise SelectionError(
                f"{path} is already in the target repository, and the catalog never rewrites it; "
                "send the operator's change as an edit"
            )

    blobs = repo_blobs or {}
    edited: dict[str, str] = {}
    for edit in sorted(edits, key=lambda e: e.path):
        if edit.path == process_path:
            raise SelectionError(
                f"{process_path} is committed from the submitted config text, not from `edits`"
            )
        if edit.path in selected or edit.path in edited:
            raise SelectionError(f"{edit.path} was submitted twice; send it once")
        if not edit.path.startswith(f"{CONFIG_DIR}/") or edit.path not in blobs:
            raise SelectionError(
                f"{edit.path} is not a file on the config branch and will not be written"
            )
        if blobs[edit.path] != edit.base_sha:
            raise EditConflict(
                f"{edit.path} changed on the config branch after it was opened; "
                "reload it and edit again"
            )
        edited[edit.path] = edit.body

    new = created or {}
    for path in sorted(new):
        if path == process_path:
            raise SelectionError(
                f"{process_path} is committed from the submitted config text, not as a new file"
            )
        if path in selected or path in edited:
            raise SelectionError(f"{path} was submitted twice; send it once")
        _check_created(path, blobs, proposal)

    files: dict[str, str] = {process_path: yaml_text, **selected, **edited, **new}
    stub_paths: set[str] = set()

    # The authoritative presence signal: `proposal.components` carries one `ComponentProposal`
    # for every agent the config references, catalogued or not. `proposal.files` alone
    # under-reports this — `propose()` emits no `FileProposal` for an agent with no catalog
    # entry — so relying on it here would stub over a real manifest the repo already owns for
    # exactly the agents the catalog cannot template.
    present_agents = frozenset(
        c.name for c in proposal.components if c.kind == "agent" and c.present_in_repo
    )

    # Any referenced agent left without a manifest gets the comments-only stub, so unticking
    # everything reproduces the behaviour that existed before per-file choice.
    stub = (TEMPLATE_ROOT / "stub" / "agent.yaml").read_text()
    for name in sorted({s.agent for s in config.steps if isinstance(s, AgentStep)}):
        if not _SAFE_NAME.match(name):
            raise SelectionError(
                f"agent name {name!r} must be a single folder name (match "
                f"{_SAFE_NAME.pattern!r}) to be scaffolded"
            )
        manifest = f"{CONFIG_DIR}/{AGENTS_DIR}/{name}/{AGENT_FILE}"
        if manifest in files:
            continue
        if name in present_agents:
            continue
        body, _ = substitute(stub, derive_agent_facts(config, name))
        files[manifest] = body
        stub_paths.add(manifest)

    def components(kind: str, *, present: bool) -> frozenset[str]:
        return frozenset(
            f.component
            for f in proposal.files.values()
            if f.kind == kind and (f.present_in_repo if present else f.path in selected)
        )

    return ScaffoldBundle(
        files=files,
        stub_paths=frozenset(stub_paths),
        shared_skills=components("skill", present=False),
        present_skills=components("skill", present=True),
        present_mcp=components("mcp", present=True),
    )
