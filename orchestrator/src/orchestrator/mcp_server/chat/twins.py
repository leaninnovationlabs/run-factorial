"""The proposal twins: every write tool of `/mcp/`, with its name, title and input, on the chat
mount (design.md §7.18).

A twin checks that the action is possible now, so a card offers only what is possible when it is
drawn, then records a proposal and its card. It writes nothing to the factory: the write runs
when the conversation's owner approves it (`assistant/proposals.py`). Exit and interrupt are
judged by their service at Approve, because `next_actions` never names them. A config commit
also stores the branch head its diff was drawn against, so Approve refuses it once the branch
has moved. A restart stores the lane head its preview was drawn against, for the same reason.
"""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from fastmcp.tools.base import ToolResult
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from orchestrator.api.schemas import RestartPrOut
from orchestrator.api.services.config import ConfigDraft, PlannedWrite, check_config
from orchestrator.api.services.errors import Invalid
from orchestrator.api.services.refs import current_run, resolve_project, resolve_ticket
from orchestrator.api.services.restarts import restart_preview, restart_target
from orchestrator.assistant.proposals import record_proposal
from orchestrator.db.base import JsonDict
from orchestrator.db.models import ChatProposalStatus, Project, Ticket
from orchestrator.events.read_models import TicketRow, ticket_row
from orchestrator.factorial import RepoConfigSource
from orchestrator.mcp_server.chat.scope import chat_scope
from orchestrator.mcp_server.context import ToolContext
from orchestrator.mcp_server.registry import ToolSpec
from orchestrator.mcp_server.results import respond, untrusted
from orchestrator.mcp_server.tokens import CHAT_SCOPE, WRITE_SCOPE
from orchestrator.mcp_server.tools import ALL_TOOLS
from orchestrator.mcp_server.tools.config import config_draft, forge_config_source
from orchestrator.mcp_server.tools.controls import require_run, ticket_ref
from orchestrator.mcp_server.tools.tickets import next_actions

__all__ = ["PRECHECKS", "TWINS", "Checked", "ProposalOut", "twin_of"]


@dataclass(frozen=True, slots=True)
class Checked:
    """The card's preview, and what Approve runs with beside the tool's own arguments."""

    preview: JsonDict
    pinned: JsonDict = field(default_factory=dict)


#: A twin's check: its arguments as the tool received them, to the card. It raises a
#: `ServiceError` for an action that is not possible now.
Precheck = Callable[[ToolContext, Mapping[str, Any]], Awaitable[Checked]]

_NOTE: Final = (
    "Here this records a proposal the person approves; do not call it again for the same action."
)


class ProposalOut(BaseModel):
    proposal_id: int
    status: ChatProposalStatus
    card_item_id: int
    result: JsonDict | None = None


async def _ticket(tc: ToolContext, args: Mapping[str, Any]) -> tuple[Ticket, TicketRow, JsonDict]:
    """The ticket the arguments name, its row, and the card's words for it."""
    ticket = await resolve_ticket(tc.session, tc.organization_id, args["ticket"], args["project"])
    row = await ticket_row(tc.session, ticket.id)
    preview: JsonDict = {
        "ticket": await ticket_ref(tc.session, ticket),
        "ticket_id": ticket.id,
        "title": untrusted(row.title) or None,
        "step": row.current_step,
        "status": row.status,
    }
    return ticket, row, preview


async def _exit(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
    _, _, preview = await _ticket(tc, args)
    return Checked(preview)


async def _interrupt(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
    ticket, _, preview = await _ticket(tc, args)
    run = await require_run(tc.session, ticket, preview["ticket"], "interrupt")
    return Checked(preview | {"attempt": run.attempt})


def _pr_card(pr: RestartPrOut) -> dict[str, Any]:
    return {
        "number": pr.number,
        "url": pr.url,
        "head": untrusted(pr.head),
        "base": untrusted(pr.base),
    }


async def _restart(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
    """The service's refusals and the preview's blockers, now; on the card, what Approve would
    cancel, close and delete. The lane head the preview was drawn against is pinned in place of
    the tool's own `process_version_id`, so a sync before Approve refuses rather than restarting
    onto a snapshot the person never saw. Merged work is refused without `allow_merged`, so the
    model has to ask the person; with it, the card names that work."""
    ticket, _, preview = await _ticket(tc, args)
    target = await restart_target(
        tc.session,
        ticket,
        process_version_id=args["process_version_id"],
        allow_current_pin=args["allow_current_pin"],
    )
    project = await tc.session.get_one(Project, ticket.project_id)
    found = await restart_preview(tc.session, ticket, await tc.repo_reader(project))
    if found.blockers:
        raise Invalid(
            f"{preview['ticket']} cannot restart now: "
            + " ".join(untrusted(blocker.detail, 300) for blocker in found.blockers)
        )
    merged = [_pr_card(pr) for pr in found.merged_outside]
    if merged and not args["allow_merged"]:
        raise Invalid(
            f"{preview['ticket']} has work already merged ("
            + ", ".join(f"#{pr['number']} into {pr['base']}" for pr in merged)
            + "), which a restart may repeat. Ask the person; if they still want it, propose "
            "again with allow_merged=true."
        )
    return Checked(
        preview
        | {
            "process": f"From {found.current.sha[:7]} to {target.source_commit_sha[:7]}",
            "live_run": None if found.live_run is None else found.live_run.model_dump(),
            "parked_run": None if found.parked_run is None else found.parked_run.model_dump(),
            "close_prs": [_pr_card(pr) for pr in found.close_prs],
            "delete_branches": [untrusted(branch) for branch in found.delete_branches],
            "merged_outside": merged,
        },
        pinned={"process_version_id": target.id},
    )


def _next_action(tool: str) -> Precheck:
    async def check(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
        ticket, row, preview = await _ticket(tc, args)
        possible = next_actions(row, await current_run(tc.session, ticket))
        if tool not in possible:
            raise Invalid(
                f"{preview['ticket']} is {row.status}, so {tool} is not possible now.",
                hint=None if possible else "No write applies to it now.",
                candidates=possible,
            )
        return Checked(preview | {key: args[key] for key in ("answer",) if key in args})

    return check


async def _create(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
    target = await resolve_project(tc.session, tc.organization_id, args["project"])
    return Checked(
        {
            "project": target.name,
            "repo": target.github_repo,
            "title": args["title"],
            "assignee": args["assignee"],
            "process": args["process"],
            "body": args["body"],
        }
    )


def _lines(text: str) -> list[str]:
    return [line if line.endswith("\n") else f"{line}\n" for line in text.splitlines(True)]


async def _diff(
    source: RepoConfigSource,
    repo: str,
    base_sha: str,
    draft: ConfigDraft,
    would_write: list[PlannedWrite],
) -> str:
    """The process file and every edited file against the base. A catalog file the draft adds
    is new, and `would_write` already names it."""
    listed = {entry.path for entry in await source.list_files(repo, base_sha)}
    after = {w.path: draft.content for w in would_write if w.origin == "process"}
    after |= {edit.path: edit.body for edit in draft.edits}
    lines: list[str] = []
    for path in sorted(after):
        before = await source.read_file(repo, path, base_sha) if path in listed else ""
        lines += difflib.unified_diff(
            _lines(before),
            _lines(after[path]),
            fromfile=f"a/{path}" if path in listed else "/dev/null",
            tofile=f"b/{path}",
        )
    return "".join(lines)


async def _config_commit(tc: ToolContext, args: Mapping[str, Any]) -> Checked:
    target = await resolve_project(tc.session, tc.organization_id, args["project"])
    source = await forge_config_source(tc, target)
    draft = config_draft(args["content"], args["files"], args["edits"])
    check = await check_config(tc.session, source, target, draft, surface="chat")
    if check.errors:
        raise Invalid(
            "The draft does not parse: " + "; ".join(untrusted(e, 600) for e in check.errors),
            hint="Fix it, and check it with factorial_config_validate.",
        )
    if not check.valid or check.base_sha is None:
        raise Invalid(
            "The commit would be refused: " + "; ".join(untrusted(b, 600) for b in check.blockers),
            hint="A change only a person may commit goes to factorial_editor_open.",
        )
    return Checked(
        {
            "project": target.name,
            "base_sha": check.base_sha,
            "would_write": [{"path": w.path, "origin": w.origin} for w in check.would_write],
            "diff": await _diff(
                source, target.github_repo, check.base_sha, draft, check.would_write
            ),
        },
        pinned={"expected_head": check.base_sha},
    )


PRECHECKS: Final[Mapping[str, Precheck]] = MappingProxyType(
    {
        "factorial_ticket_exit": _exit,
        "factorial_ticket_interrupt": _interrupt,
        "factorial_ticket_restart": _restart,
        **{
            name: _next_action(name)
            for name in (
                "factorial_ticket_reenter",
                "factorial_ticket_resume",
                "factorial_ticket_unblock",
                "factorial_ticket_retry",
                "factorial_ticket_requeue",
            )
        },
        "factorial_ticket_create": _create,
        "factorial_config_commit": _config_commit,
    }
)


def twin_of(spec: ToolSpec) -> ToolSpec:
    """The write tool's signature, evaluated, so FastMCP builds the same input schema and injects
    the same `tc`; its own annotations are already objects, so no module needs to resolve them."""
    signature = inspect.signature(spec.fn, eval_str=True)
    precheck = PRECHECKS[spec.name]

    async def twin(**arguments: Any) -> ToolResult:
        bound = signature.bind(**arguments)
        bound.apply_defaults()
        args = dict(bound.arguments)
        tc: ToolContext = args.pop("tc")
        scope = await chat_scope(tc)
        checked = await precheck(tc, args)
        proposal = await record_proposal(
            tc.session,
            scope=scope,
            tool=spec.name,
            args=to_jsonable_python(args | checked.pinned),
            preview=checked.preview,
            ttl=timedelta(hours=tc.settings.assistant.proposal_ttl_hours),
            now=datetime.now(UTC),
        )
        out = ProposalOut(
            proposal_id=proposal.id,
            status=ChatProposalStatus.PENDING,
            card_item_id=proposal.item_id,
        )
        return respond(
            out, f"proposal P{proposal.id} pending: the person approves or declines its card"
        )

    twin.__name__ = twin.__qualname__ = spec.name
    twin.__doc__ = f"{inspect.getdoc(spec.fn)}\n\n{_NOTE}"
    twin.__signature__ = signature  # type: ignore[attr-defined]
    annotations = {
        name: parameter.annotation for name, parameter in signature.parameters.items()
    } | {"return": signature.return_annotation}

    # Python 3.14's functools.wraps copies `__annotate__`, not `__annotations__`, so
    # `register`'s wrapper sees these only through an `__annotate__` (PEP 649). It is set
    # last because assigning `__annotations__` clears it.
    def annotate(format: int) -> dict[str, Any]:
        if format != 1:
            raise NotImplementedError
        return dict(annotations)

    twin.__annotations__ = annotations
    twin.__annotate__ = annotate  # type: ignore[attr-defined]
    return ToolSpec(
        twin,
        CHAT_SCOPE,
        spec.title,
        ProposalOut,
        destructive=False,
        idempotent=False,
        open_world=spec.open_world,
    )


TWINS: Final[list[ToolSpec]] = [twin_of(s) for s in ALL_TOOLS if s.scope == WRITE_SCOPE]
