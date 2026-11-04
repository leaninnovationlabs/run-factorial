"""The tools only the assistant has. Each draws one card in the turn's conversation, or reads a
reference back to the model; none writes to the factory, and a card does nothing until a person
clicks it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Final, Literal

from fastmcp.dependencies import Depends
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, Field, FiniteFloat

from orchestrator.api.services.config import check_config
from orchestrator.api.services.errors import Invalid
from orchestrator.api.services.refs import resolve_project
from orchestrator.assistant.items import append_item, notify_items
from orchestrator.config import parse_process
from orchestrator.db.base import JsonDict
from orchestrator.db.models import ChatItemKind
from orchestrator.mcp_server.chat.routes import (
    ASSIST_ANCHORS,
    DASHBOARD_ROUTES,
    route_for,
)
from orchestrator.mcp_server.chat.scope import ChatScope, chat_scope
from orchestrator.mcp_server.context import ToolContext, tool_context
from orchestrator.mcp_server.registry import ToolSpec, tool
from orchestrator.mcp_server.results import Untrusted, respond, untrusted
from orchestrator.mcp_server.teaching import RESOURCES, vocabulary_text
from orchestrator.mcp_server.tokens import CHAT_SCOPE, READ_SCOPE
from orchestrator.mcp_server.tools import ALL_TOOLS
from orchestrator.mcp_server.tools.config import (
    Content,
    Edits,
    Files,
    WouldWriteOut,
    config_draft,
    forge_config_source,
)
from orchestrator.mcp_server.tools.params import ProjectArg

__all__ = ["CHAT_TOOLS", "REFERENCE_LIMIT"]

#: A reference is returned whole up to this; past it, cut and marked.
REFERENCE_LIMIT: Final = 40_000

_READ_TOOLS: Final = tuple(sorted(spec.name for spec in ALL_TOOLS if spec.scope == READ_SCOPE))

_REFERENCES: Final[Mapping[str, Callable[[], str]]] = {
    "operator-skill": lambda: (RESOURCES / "skills" / "factorial-operator" / "SKILL.md").read_text(
        encoding="utf-8"
    ),
    "process-yaml": lambda: (RESOURCES / "process-yaml.md").read_text(encoding="utf-8"),
    "vocabulary": vocabulary_text,
}


class CardOut(BaseModel):
    card_item_id: int


class SeriesIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    values: list[FiniteFloat] = Field(min_length=1, max_length=60)


class EditorDraftOut(BaseModel):
    card_item_id: int
    process: str
    would_write: list[WouldWriteOut]
    blockers: list[Untrusted] = Field(
        description="What the editor will show the person before they can commit."
    )


class ReferenceOut(BaseModel):
    name: str
    section: str | None = None
    sections: list[str] = Field(description="Every `##` heading, each a valid `section`.")
    text: str
    truncated: bool


async def _card(tc: ToolContext, scope: ChatScope, kind: ChatItemKind, payload: JsonDict) -> int:
    item = await append_item(
        tc.session,
        conversation_id=scope.conversation_id,
        turn_id=scope.turn_id,
        kind=kind,
        payload=payload,
    )
    await notify_items(tc.session, scope.conversation_id)
    return item.id


@tool(CHAT_SCOPE, title="Show me", output=CardOut, idempotent=False, open_world=False)
async def factorial_ui_show(
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="A dashboard page, a :param filled: " + ", ".join(DASHBOARD_ROUTES),
        ),
    ],
    anchor: Annotated[
        str | None,
        Field(description="The control to point at: " + ", ".join(sorted(ASSIST_ANCHORS))),
    ] = None,
    note: Annotated[
        str | None, Field(max_length=140, description="One line shown beside the control.")
    ] = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Give the person a Show me chip: a click opens that dashboard page and points at the
    control. Use it when they ask where something is or how to do something in the dashboard,
    instead of describing the page."""
    scope = await chat_scope(tc)
    route = route_for(path)
    if route is None:
        raise Invalid(
            f"{untrusted(path, 100)!r} is not a dashboard page.", candidates=DASHBOARD_ROUTES
        )
    if anchor is not None and anchor not in ASSIST_ANCHORS:
        raise Invalid(
            f"{untrusted(anchor, 60)!r} is not a control Show me can point at.",
            candidates=sorted(ASSIST_ANCHORS),
        )
    card = await _card(
        tc,
        scope,
        ChatItemKind.SHOW_ME,
        {"path": path, "anchor": anchor, "note": note},
    )
    return respond(CardOut(card_item_id=card), f"Show me card {card} for {route}")


@tool(CHAT_SCOPE, title="Chart", output=CardOut, idempotent=False, open_world=False)
async def factorial_chart(
    kind: Literal["line", "bar", "grouped_bar"],
    title: Annotated[str, Field(min_length=1, max_length=80)],
    unit: Literal["USD", "minutes", "hours", "tickets", "tokens"],
    x: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=40)]],
        Field(min_length=1, max_length=60, description="One label per value, in order."),
    ],
    series: Annotated[
        list[SeriesIn],
        Field(min_length=1, max_length=6, description="Each with one value per x label."),
    ],
    source: Annotated[
        str,
        Field(min_length=1, max_length=100, description="The read tool the numbers came from."),
    ],
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Draw a chart in the conversation. Chart only numbers a read tool returned in this turn,
    copied exactly, and name that tool as source. line for a quantity over time; bar or
    grouped_bar for one measure across steps, tickets or projects."""
    scope = await chat_scope(tc)
    if source not in _READ_TOOLS:
        raise Invalid(
            f"Source {untrusted(source, 100)!r} is not a read tool.", candidates=_READ_TOOLS
        )
    for line in series:
        if len(line.values) != len(x):
            raise Invalid(
                f"Series {untrusted(line.name, 60)!r} has {len(line.values)} values for "
                f"{len(x)} x labels."
            )
    card = await _card(
        tc,
        scope,
        ChatItemKind.CHART,
        {
            "kind": kind,
            "title": title,
            "unit": unit,
            "x": x,
            "series": [line.model_dump() for line in series],
            "source": source,
        },
    )
    return respond(CardOut(card_item_id=card), f"chart card {card}: {len(series)} series")


@tool(CHAT_SCOPE, title="Open in the process editor", output=EditorDraftOut, idempotent=False)
async def factorial_editor_open(
    project: ProjectArg,
    content: Content,
    files: Files = None,
    edits: Edits = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Hand a process draft to the dashboard's process editor: the person gets a chip that opens
    it there, to review and commit with Commit and sync. Use it for a change under agents/ or
    skills/ or to a gate's approvers, which only a person may commit. The YAML must parse."""
    scope = await chat_scope(tc)
    row = await resolve_project(tc.session, tc.organization_id, project)
    # The dashboard's verdict: the person commits this draft from the editor, so the chat
    # surface's refusals of agents/, skills/ and approvers are the reason to be here.
    check = await check_config(
        tc.session,
        await forge_config_source(tc, row),
        row,
        config_draft(content, files, edits),
        surface="dashboard",
    )
    if check.errors:
        raise Invalid(
            "The draft does not parse: " + "; ".join(untrusted(e, 600) for e in check.errors),
            hint="Fix it, and check it with factorial_config_validate.",
        )
    process = parse_process(content, origin=f"project {row.id} editor draft").name
    would_write = [WouldWriteOut(path=w.path, origin=w.origin) for w in check.would_write]
    card = await _card(
        tc,
        scope,
        ChatItemKind.EDITOR_DRAFT,
        {
            "project_id": row.id,
            "process": process,
            "content": content,
            "files": [f.model_dump() for f in files or ()],
            "edits": [e.model_dump() for e in edits or ()],
            "base_sha": check.base_sha,
            "would_write": [w.model_dump() for w in would_write],
        },
    )
    out = EditorDraftOut(
        card_item_id=card,
        process=process,
        would_write=would_write,
        blockers=[untrusted(b, 600) for b in check.blockers],
    )
    return respond(
        out, f"editor card {card}: {len(would_write)} files, {len(out.blockers)} blockers"
    )


def _sections(text: str) -> list[tuple[str, str]]:
    """Each `##` block, as its heading and its text up to the next `##`. A `##` inside a code
    fence is code."""
    blocks: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            blocks.append((line[3:].strip(), [line]))
        elif blocks:
            blocks[-1][1].append(line)
    return [(heading, "".join(lines)) for heading, lines in blocks]


@tool(CHAT_SCOPE, title="Read a reference", output=ReferenceOut, open_world=False)
async def factorial_reference_read(
    name: Literal["operator-skill", "process-yaml", "vocabulary"],
    section: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=100,
            description="Part of a `##` heading: returns that block only.",
        ),
    ] = None,
    tc: ToolContext = Depends(tool_context),
) -> ToolResult:
    """Read the operator skill (how to work the factory), the process-file reference, or the
    closed config vocabulary. A long one is cut: pass section to read one `##` block of it."""
    await chat_scope(tc)
    text = _REFERENCES[name]()
    sections = _sections(text)
    if section is not None:
        wanted = section.casefold()
        found = next((block for heading, block in sections if wanted in heading.casefold()), None)
        if found is None:
            raise Invalid(
                f"{name} has no section {untrusted(section, 100)!r}.",
                candidates=[heading for heading, _ in sections],
            )
        text = found
    truncated = len(text) > REFERENCE_LIMIT
    if truncated:
        text = (
            f"{text[:REFERENCE_LIMIT]}\n\n"
            f"[cut at {REFERENCE_LIMIT:,} characters: pass section to read one part]"
        )
    out = ReferenceOut(
        name=name,
        section=section,
        sections=[heading for heading, _ in sections],
        text=text,
        truncated=truncated,
    )
    return respond(out, f"{name}: {len(text)} characters" + (", cut" if truncated else ""))


CHAT_TOOLS: list[ToolSpec] = [
    factorial_ui_show,
    factorial_chart,
    factorial_editor_open,
    factorial_reference_read,
]
