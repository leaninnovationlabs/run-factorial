"""The process templates the config editor offers: whole process files, comments and all.

A template is the file an operator starts from, not a set of answers rendered into one. Only a
file keeps the comments that explain each choice and the blocks left commented out for an
operator to turn on, such as the build step's `preview:` (docs/enable-previews.md).

A template is a folder shaped like a `.factorial/` one, and each of its lanes is a file in that
folder's `processes/`, so a template can hold several lanes and an import writes every one of
them to the `factorial` branch. Each folder ships as package data beside the scaffold catalog, at
`scaffold/templates/process-templates/<key>/processes/process-<lane>.yaml`, because the image
carries no `docs/`.

`simple` is the `primary` lane of docs/samples/factorial-simple. That sample keeps a second
lane, `simple`, with a test step: it is the commented reference for the format and no template
ships it. `superpowers` is the one lane of docs/samples/factorial-superpowers, whose design and
plan a person approves in the dashboard. Both serve GitHub and GitLab: a process file names no
forge, and their agents' prompts name both `gh` and `glab`. The tests hold each template's
parsed content equal to its sample's, and hold a first commit of each template, on either
forge, to the paths its sample folder has for that lane, with the sample's text.

The shells are what the editor fills a new agent, skill, MCP server or process from: one folder
per kind under `scaffold/templates/shells/`, with `__NAME__` where the name the operator types
goes. An agent shell and a skill shell parse as written. An MCP server shell and a process shell
cannot have a safe default, so the part the operator must write is a commented example between
two marker lines, and the loader refuses the file until it is filled in. The tests parse each
example once it is uncommented, so a shell cannot go stale against the models.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator.config.loader import PROCESS_PREFIX, PROCESS_SUFFIX, PROCESSES_DIR
from orchestrator.config.scaffold.catalog import TEMPLATE_ROOT

__all__ = ["CONFIG_SHELLS", "PROCESS_TEMPLATES", "ProcessTemplate", "TemplateLane"]

TEMPLATES_DIR = TEMPLATE_ROOT / "process-templates"
SHELLS_DIR = TEMPLATE_ROOT / "shells"


def _shells() -> Mapping[str, Mapping[str, str]]:
    return {
        kind.name: {
            str(path.relative_to(kind)): path.read_text()
            for path in sorted(kind.rglob("*"))
            if path.is_file()
        }
        for kind in sorted(SHELLS_DIR.iterdir())
        if kind.is_dir()
    }


#: Kind (`agent`, `skill`, `mcp`, `process`) to file name inside that shell to body.
CONFIG_SHELLS: Mapping[str, Mapping[str, str]] = _shells()


@dataclass(frozen=True, slots=True)
class TemplateLane:
    #: The lane its file declares, which is also the stem of `process-<key>.yaml`.
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class ProcessTemplate:
    #: The template's folder under `process-templates/`.
    key: str
    title: str
    description: str

    @property
    def lanes(self) -> tuple[TemplateLane, ...]:
        """Every process file in the template's folder, in name order."""
        folder = TEMPLATES_DIR / self.key / PROCESSES_DIR
        return tuple(
            TemplateLane(
                key=path.name.removeprefix(PROCESS_PREFIX).removesuffix(PROCESS_SUFFIX),
                text=path.read_text(),
            )
            for path in sorted(folder.glob(f"{PROCESS_PREFIX}*{PROCESS_SUFFIX}"))
        )


PROCESS_TEMPLATES: tuple[ProcessTemplate, ...] = (
    ProcessTemplate(
        key="simple",
        title="Simple template",
        description=(
            "One lane: an agent turns each ticket into a spec, and another builds it. A person "
            "reviews the spec and the code, each at its own pull request."
        ),
    ),
    ProcessTemplate(
        key="superpowers",
        title="Superpowers template",
        description=(
            "One lane: an agent agrees a design with a person, writes a plan of small "
            "test-first tasks, builds it and reviews the branch. A person approves the design and "
            "the plan in the dashboard, then the pull request."
        ),
    ),
)
