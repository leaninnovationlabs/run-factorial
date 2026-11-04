"""The process templates the config editor offers: whole process files, comments and all.

A template is the file an operator starts from, not a set of answers rendered into one. Only a
file keeps the comments that explain each choice and the blocks left commented out for an
operator to turn on, such as the build step's `preview:` (docs/enable-previews.md).

A template is a folder shaped like a `.factorial/` one, and each of its lanes is a file in that
folder's `processes/`, so a template can hold several lanes and an import writes every one of
them to the `factorial` branch. Each folder ships as package data beside the scaffold catalog, at
`scaffold/templates/process-templates/<key>/processes/process-<lane>.yaml`, because the image
carries no `docs/`.

`simple` is the GitHub template: the lanes of docs/samples/factorial-simple. `simple-gitlab` is
the GitLab counterpart, the lanes of docs/samples/factorial-gitlab, started from a working
`.factorial` on GitLab. The tests hold each template's parsed content equal to its sample's, and
hold a first commit of each template to every path its sample folder has, with the sample's text.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config.loader import PROCESS_PREFIX, PROCESS_SUFFIX, PROCESSES_DIR
from orchestrator.config.scaffold.catalog import TEMPLATE_ROOT, Forge

__all__ = ["PROCESS_TEMPLATES", "ProcessTemplate", "TemplateLane"]

TEMPLATES_DIR = TEMPLATE_ROOT / "process-templates"


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
    #: The forge whose agents the gallery previews for it. An import writes the agents of the
    #: project's own forge, whichever template it came from.
    forge: Forge

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
            "Two lanes: simple plans, builds and tests each ticket; primary skips the test step "
            "for a repo without CI on pull requests. A person reviews the spec and the code."
        ),
        forge="github",
    ),
    ProcessTemplate(
        key="simple-gitlab",
        title="Simple template (GitLab)",
        description=(
            "Two lanes on GitLab: simple plans, builds and tests each ticket; primary skips the "
            "test step for a repo without CI on merge requests. "
            "A person reviews the spec and the code."
        ),
        forge="gitlab",
    ),
)
