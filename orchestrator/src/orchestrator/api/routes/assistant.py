"""The assistant's conversations, messages, turns and stream (`/api/assistant/*`).

A conversation is its owner's: every route answers 404 for anyone else's, as if it did not
exist. A message is a row, not a request held open: the send writes the person's item and a
queued turn in one transaction and answers 202, and the assistant's process claims the turn.
A refusal names a closed `code` beside its `detail`, for the Drawer to key on.

A proposal is decided only by its conversation's owner, and only from the dashboard: approve and
decline need an `Origin` naming it, because the session cookie rides along on a request from any
page. Approve runs the proposal as that person (`assistant/proposals.py`).

The stream holds no request-scoped session: FastAPI keeps a dependency's session open until a
streamed response ends, which would hold a pooled connection, in a transaction, for as long as
the Drawer stays open. It reads through `stream_sessions_dependency` instead, one short session
per read.

`github-token` hands the assistant's process its read-only GitHub access, one entry per App and
owner, over the repos of every project on that App under that owner, minted concurrently. That
process holds no GitHub credential, so the API mints for it, and only for the API bearer: no page
asks for this. A token project's entry is the read proxy below instead, which adds the project's
stored token here, so that token never leaves this process.

A GitLab project's entry is always the GitLab read proxy below, for the same reason: the token
opens only with the master key, which that process does not hold. The proxy parses the raw path,
because Starlette has decoded `{path:path}`, and `projects/group%2Frepo` must not read as two
segments.

`credential` hands that process each turn's model credential, for the API bearer alone too: the
process holds no master key, so every stored secret is opaque to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal
from urllib.parse import parse_qsl, quote, unquote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from githubkit.exception import RequestError, RequestFailed
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import WRITE_TOOLS, Permission
from orchestrator.access.decision import Forbidden
from orchestrator.api import chat_stream
from orchestrator.api.access import GrantDep, needs, require_dashboard_origin
from orchestrator.api.auth import PrincipalDep, require_bearer
from orchestrator.api.deps import (
    GitHubForDep,
    GitHubProxyHttpDep,
    GitLabProxyHttpDep,
    OrganizationDep,
    ReadTokenMinterDep,
    SessionDep,
    SettingsDep,
)
from orchestrator.api.schemas import (
    ChatConversationIn,
    ChatConversationOut,
    ChatConversationPatch,
    ChatItemOut,
    ChatItemsOut,
    ChatMessageAccepted,
    ChatMessageIn,
    ChatTurnOut,
    GitHubTokenOut,
    TurnCredentialIn,
    TurnCredentialOut,
)
from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.assistant_send import SendRefused, queue_user_message
from orchestrator.api.services.errors import Conflict, NotFound, as_sentence
from orchestrator.api.services.github_connection import github_repos
from orchestrator.api.services.refs import ticket_source
from orchestrator.assistant import proposals
from orchestrator.assistant.github_token import GITLAB_PROXY_PATH, PROXY_PATH
from orchestrator.assistant.items import items_after, notify_items
from orchestrator.assistant.turns import current_turn, wake_queue
from orchestrator.db.base import JsonDict
from orchestrator.db.models import (
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatProposalStatus,
    ChatTurn,
    ChatTurnStatus,
    Project,
    TicketSource,
    TicketSourceKind,
    intake_repo,
)
from orchestrator.db.session import SessionScope, session_scope
from orchestrator.github.client import (
    GITHUB_API_URL,
    MAX_TOKEN_REPOS,
    GitHubClient,
    split_repo,
)
from orchestrator.github.connection import connection_for_project
from orchestrator.gitlab.project_client import settings_for_project
from orchestrator.mcp_server.chat.twins import ProposalOut
from orchestrator.mcp_server.context import McpDeps, ToolContext
from runtime.assistant.contract import TurnStop

__all__ = ["router", "stream_sessions_dependency", "tool_deps_dependency"]

router = APIRouter(tags=["assistant"])
#: Lists and creates within one organization, mounted at `/orgs/{org}` (`api.main`).
org_router = APIRouter(tags=["assistant"])
log = logging.getLogger(__name__)

_NOT_FOUND: Final = "Conversation not found."
_STREAM_HEADERS: Final = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def stream_sessions_dependency() -> SessionScope:
    return session_scope


StreamSessionsDep = Annotated[SessionScope, Depends(stream_sessions_dependency)]


def tool_deps_dependency(request: Request) -> McpDeps:
    """What an approved proposal runs with: the deps the MCP tools were mounted with."""
    deps = getattr(request.app.state, "mcp_deps", None)
    if deps is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The MCP server is not mounted on this API."
        )
    return deps


ToolDepsDep = Annotated[McpDeps, Depends(tool_deps_dependency)]


def _from_dashboard(request: Request, settings: SettingsDep) -> None:
    require_dashboard_origin(request, settings, "Decide a proposal from the dashboard.")


_DashboardOnly = Depends(_from_dashboard)


def _refused(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


async def _owned(
    session: AsyncSession,
    conversation_id: int,
    principal: str,
    *,
    lock: Literal["none", "write", "delete"] = "none",
) -> ChatConversation:
    """`write` takes the lock every writer of the conversation takes first
    (`items.lock_conversation`); `delete` takes the stronger one the DELETE needs, at the
    start, so the delete queues behind a writer rather than upgrading under one."""
    query = select(ChatConversation).where(ChatConversation.id == conversation_id)
    if lock == "write":
        query = query.with_for_update(key_share=True)
    elif lock == "delete":
        query = query.with_for_update()
    conversation = (await session.scalars(query)).one_or_none()
    if conversation is None or conversation.owner != principal:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return conversation


@org_router.get("/assistant/conversations", response_model=list[ChatConversationOut])
@needs(Permission.ASSISTANT_USE)
async def list_conversations(
    session: SessionDep, principal: PrincipalDep, organization: OrganizationDep
) -> list[ChatConversation]:
    return list(
        (
            await session.scalars(
                select(ChatConversation)
                .where(
                    ChatConversation.organization_id == organization.id,
                    ChatConversation.owner == principal,
                    ChatConversation.archived_at.is_(None),
                )
                .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
            )
        ).all()
    )


@org_router.post(
    "/assistant/conversations",
    response_model=ChatConversationOut,
    status_code=status.HTTP_201_CREATED,
)
@needs(Permission.ASSISTANT_USE)
async def create_conversation(
    body: ChatConversationIn,
    session: SessionDep,
    principal: PrincipalDep,
    organization: OrganizationDep,
) -> ChatConversation:
    assistant = await svc.load_assistant_settings(session, organization.id)
    conversation = ChatConversation(
        organization_id=organization.id,
        owner=principal,
        title=body.title,
        backend_kind=assistant.backend,
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    await session.commit()
    return conversation


@router.patch("/assistant/conversations/{conversation_id}", response_model=ChatConversationOut)
@needs(Permission.ASSISTANT_USE)
async def update_conversation(
    conversation_id: int, body: ChatConversationPatch, session: SessionDep, principal: PrincipalDep
) -> ChatConversation:
    conversation = await _owned(session, conversation_id, principal)
    if "title" in body.model_fields_set:
        conversation.title = body.title
    if body.archived is not None:
        conversation.archived_at = datetime.now(UTC) if body.archived else None
    await session.flush()
    await session.refresh(conversation)
    await session.commit()
    return conversation


@router.delete("/assistant/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
@needs(Permission.ASSISTANT_USE)
async def delete_conversation(
    conversation_id: int, session: SessionDep, principal: PrincipalDep
) -> Response:
    """A live turn's process stops it when its next lease renewal finds the turn gone."""
    await _owned(session, conversation_id, principal, lock="delete")
    await session.execute(delete(ChatConversation).where(ChatConversation.id == conversation_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/assistant/conversations/{conversation_id}/items", response_model=ChatItemsOut)
@needs(Permission.ASSISTANT_USE)
async def get_items(
    conversation_id: int, session: SessionDep, principal: PrincipalDep, after: int = 0
) -> ChatItemsOut:
    await _owned(session, conversation_id, principal)
    items = await items_after(session, conversation_id, after)
    turn = await current_turn(session, conversation_id)
    return ChatItemsOut(
        items=[ChatItemOut.model_validate(item) for item in items],
        turn=None if turn is None else ChatTurnOut.model_validate(turn),
    )


@router.post(
    "/assistant/conversations/{conversation_id}/messages",
    response_model=ChatMessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "turn_live or not_configured"}, 429: {}, 503: {}},
)
@needs(Permission.ASSISTANT_USE)
async def send_message(
    conversation_id: int,
    body: ChatMessageIn,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> ChatMessageAccepted | JSONResponse:
    # Serialises two sends on one conversation, so the live-turn check below holds.
    await _owned(session, conversation_id, principal, lock="write")
    try:
        turn = await queue_user_message(
            session,
            conversation_id,
            principal,
            text=body.text,
            page=body.page,
            multi_org=settings.multi_org.enabled,
        )
    except SendRefused as refusal:
        return _refused(refusal.status, refusal.code, refusal.detail)
    await session.commit()
    return ChatMessageAccepted(turn_id=turn.id)


@router.post("/assistant/turns/{turn_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
@needs(Permission.ASSISTANT_USE)
async def cancel_turn(turn_id: int, session: SessionDep, principal: PrincipalDep) -> Response:
    """A running turn is stopped by its process, which the NOTIFY wakes; a queued one has no
    process yet, so it ends here. One statement, owner check included, so neither a claim nor a
    delete racing it can slip between."""
    queued = ChatTurn.status == ChatTurnStatus.QUEUED
    owned = select(ChatConversation.id).where(ChatConversation.owner == principal)
    conversation_id = await session.scalar(
        update(ChatTurn)
        .where(ChatTurn.id == turn_id, ChatTurn.conversation_id.in_(owned))
        .values(
            cancel_requested=True,
            status=case((queued, ChatTurnStatus.CANCELLED), else_=ChatTurn.status),
            stop_reason=case((queued, TurnStop.CANCELLED.value), else_=ChatTurn.stop_reason),
            ended_at=case((queued, func.now()), else_=ChatTurn.ended_at),
        )
        .returning(ChatTurn.conversation_id)
    )
    if conversation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turn not found.")
    await notify_items(session, conversation_id)
    await wake_queue(session)
    await session.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get(
    "/assistant/conversations/{conversation_id}/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
@needs(Permission.ASSISTANT_USE)
async def stream_conversation(
    conversation_id: int,
    request: Request,
    principal: PrincipalDep,
    sessions: StreamSessionsDep,
    after: int = 0,
    last_event_id: Annotated[int | None, Header()] = None,
) -> StreamingResponse:
    """Server-sent events: `item` (id = its `rev`), `turn`, and a `: ping` comment when idle.
    `Last-Event-ID`, which a reconnect sends, wins over `?after`."""
    async with sessions() as session:
        await _owned(session, conversation_id, principal)
    return StreamingResponse(
        chat_stream.conversation_events(
            sessions,
            request.app.state.chat_hub,
            conversation_id,
            after=last_event_id if last_event_id is not None else after,
            ping_seconds=chat_stream.PING_SECONDS,
        ),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )


async def _decided(deciding: Awaitable[ChatProposal]) -> ProposalOut | JSONResponse:
    try:
        proposal = await deciding
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    except proposals.Expired as exc:
        return _refused(status.HTTP_410_GONE, "proposal_expired", exc.message)
    except proposals.Unrunnable as exc:
        return _refused(status.HTTP_409_CONFLICT, "proposal_unrunnable", exc.message)
    except Conflict as exc:
        return _refused(status.HTTP_409_CONFLICT, "proposal_decided", exc.message)
    return ProposalOut(
        proposal_id=proposal.id,
        status=ChatProposalStatus(proposal.status),
        card_item_id=proposal.item_id,
        result=proposal.result,
    )


@router.post(
    "/assistant/proposals/{proposal_id}/approve",
    response_model=ProposalOut,
    dependencies=[_DashboardOnly],
    responses={
        409: {"description": "proposal_decided or proposal_unrunnable"},
        410: {"description": "proposal_expired"},
    },
)
@needs(Permission.ASSISTANT_USE)
async def approve_proposal(
    proposal_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    deps: ToolDepsDep,
    grant: GrantDep,
) -> ProposalOut | JSONResponse:
    """200 whether the action succeeded or its service refused: `status` says which, and
    `result` holds the service's answer. A person approves only what they could do themselves
    (design.md §7.19); a refusal leaves the proposal pending."""
    tool = await session.scalar(select(ChatProposal.tool).where(ChatProposal.id == proposal_id))
    needed = WRITE_TOOLS.get(tool or "")
    # The guard read the caller's permissions in the proposal's organization.
    if needed is not None and needed not in grant.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(Forbidden(needed)))
    organization_id = await proposals.proposal_organization_id(session, proposal_id)

    async def approving() -> ChatProposal:
        tc = ToolContext(
            session=session, settings=deps.settings(), deps=deps, organization_id=organization_id
        )
        return await proposals.approve(
            session, proposal_id, caller=principal, tc=tc, now=datetime.now(UTC)
        )

    return await _decided(approving())


@router.post(
    "/assistant/proposals/{proposal_id}/decline",
    response_model=ProposalOut,
    dependencies=[_DashboardOnly],
    responses={409: {"description": "proposal_decided"}, 410: {"description": "proposal_expired"}},
)
@needs(Permission.ASSISTANT_USE)
async def decline_proposal(
    proposal_id: int, session: SessionDep, principal: PrincipalDep
) -> ProposalOut | JSONResponse:
    return await _decided(
        proposals.decline(session, proposal_id, caller=principal, now=datetime.now(UTC))
    )


@router.get("/assistant/drafts/{item_id}", response_model=dict[str, Any])
@needs(Permission.ASSISTANT_USE)
async def get_draft(item_id: int, session: SessionDep, principal: PrincipalDep) -> JsonDict:
    """The draft an `editor_draft` card hands the process editor."""
    payload = await session.scalar(
        select(ChatItem.payload)
        .join(ChatConversation, ChatConversation.id == ChatItem.conversation_id)
        .where(
            ChatItem.id == item_id,
            ChatItem.kind == ChatItemKind.EDITOR_DRAFT,
            ChatConversation.owner == principal,
        )
    )
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found.")
    return payload


@org_router.post(
    "/assistant/credential",
    response_model=TurnCredentialOut,
    dependencies=[Depends(require_bearer)],
    responses={409: {"description": "not_configured or settings_changed"}},
)
@needs(Permission.ASSISTANT_USE)
async def turn_credential(
    body: TurnCredentialIn,
    session: SessionDep,
    response: Response,
    organization: OrganizationDep,
) -> TurnCredentialOut | JSONResponse:
    """The organization's stored credential for the backend and auth mode a turn will run.
    Settings switched since the turn read them answer 409, so a credential stored for one mode
    never runs another."""
    response.headers["Cache-Control"] = "no-store"
    assistant = await svc.load_assistant_settings(session, organization.id)
    if (assistant.backend, assistant.auth_mode) != (body.backend, body.auth_mode):
        return _refused(
            status.HTTP_409_CONFLICT,
            "settings_changed",
            "The assistant's settings changed after the turn read them.",
        )
    credential = (
        await svc.assistant_credential(session, organization.id, assistant)
        if assistant.enabled
        else None
    )
    if credential is None:
        return _refused(
            status.HTTP_409_CONFLICT, "not_configured", "The assistant is off or has no credential."
        )
    return TurnCredentialOut(credential=credential.get_secret_value())


#: How long a read proxy entry stands before the assistant asks again, so a project whose
#: connection changes mode is read the new way within the hour.
PROXY_ENTRY_LIFETIME: Final = timedelta(hours=1)


async def _github_projects(session: AsyncSession, organization_id: int) -> list[Project]:
    """The organization's projects a chat tool reads on GitHub: every one but GitLab's."""
    return list(
        (
            await session.execute(
                select(Project)
                .outerjoin(TicketSource, TicketSource.project_id == Project.id)
                .where(
                    Project.organization_id == organization_id,
                    or_(TicketSource.kind.is_(None), TicketSource.kind != TicketSourceKind.GITLAB),
                )
                .order_by(Project.id)
            )
        ).scalars()
    )


async def _gitlab_projects(
    session: AsyncSession, organization_id: int
) -> list[tuple[Project, TicketSource]]:
    return [
        (project, source)
        for project, source in (
            await session.execute(
                select(Project, TicketSource)
                .join(TicketSource, TicketSource.project_id == Project.id)
                .where(
                    Project.organization_id == organization_id,
                    TicketSource.kind == TicketSourceKind.GITLAB,
                )
                .order_by(Project.id)
            )
        ).tuples()
    ]


def _gitlab_repos(project: Project, source: TicketSource) -> list[str]:
    return list(dict.fromkeys([project.github_repo, intake_repo(project, source)]))


@org_router.post(
    "/assistant/github-token",
    response_model=list[GitHubTokenOut],
    dependencies=[Depends(require_bearer)],
)
@needs(Permission.ASSISTANT_USE)
async def github_token(
    session: SessionDep,
    settings: SettingsDep,
    github_for: GitHubForDep,
    mint: ReadTokenMinterDep,
    response: Response,
    organization: OrganizationDep,
) -> list[GitHubTokenOut]:
    """The assistant process's read-only GitHub access for one organization: an entry per App
    and owner, over every repo a chat tool in it may read and none of another organization's.
    One App and owner's refusal costs only that group's entries, one project's unreadable
    connection only that project's."""
    response.headers["Cache-Control"] = "no-store"
    entries: list[GitHubTokenOut] = []
    refusals: list[str] = []
    # (the pooled client, so one App's credentials; owner) -> lower-cased repo -> repo, and the
    # projects behind them.
    groups: dict[tuple[GitHubClient, str], tuple[dict[str, str], list[int]]] = {}
    for project in await _github_projects(session, organization.id):
        try:
            github = await github_for(project)
            if github is None:
                continue
            repos = await github_repos(session, project)
        except Exception as exc:
            log.warning("assistant GitHub access failed for project %s: %s", project.id, exc)
            refusals.append(f"Project {project.id}'s GitHub connection could not be read.")
            continue
        connection = github.client.connection
        if connection is not None and connection.mode == "token":
            entries.append(
                GitHubTokenOut(
                    repos=repos,
                    token="",
                    expires_at=datetime.now(UTC) + PROXY_ENTRY_LIFETIME,
                    base_url=settings.assistant.api_url.rstrip("/")
                    + PROXY_PATH.format(project_id=project.id),
                )
            )
            continue
        for repo in repos:
            owned, behind = groups.setdefault(
                (github.client, split_repo(repo)[0].lower()), ({}, [])
            )
            owned.setdefault(repo.lower(), repo)
            if project.id not in behind:
                behind.append(project.id)

    async def mint_group(
        client: GitHubClient, repos: list[str], project_ids: list[int]
    ) -> GitHubTokenOut | str:
        try:
            minted = await mint(client, repos)
        except (ValueError, RuntimeError, RequestFailed, RequestError) as exc:
            # ValueError: nothing reachable or an impossible repo list. RuntimeError: the grant
            # did not match (`TokenScopeNotGranted`) or the App's credential was refused.
            log.warning(
                "assistant read-only token refused for project %s: %s",
                ", ".join(map(str, project_ids)),
                exc,
            )
            return as_sentence(str(exc))
        return GitHubTokenOut(
            repos=list(minted.repos),
            token=minted.token.get_secret_value(),
            expires_at=minted.expires_at,
            base_url=GITHUB_API_URL,
        )

    minting: list[Awaitable[GitHubTokenOut | str]] = []
    for (client, _owner), (owned, behind) in groups.items():
        repos = sorted(owned.values())
        for start in range(0, len(repos), MAX_TOKEN_REPOS):
            minting.append(mint_group(client, repos[start : start + MAX_TOKEN_REPOS], behind))
    for result in await asyncio.gather(*minting):
        if isinstance(result, str):
            refusals.append(result)
        else:
            entries.append(result)
    for project, source in await _gitlab_projects(session, organization.id):
        try:
            gitlab = await settings_for_project(session, project)
        except Exception as exc:
            log.warning("assistant GitLab access failed for project %s: %s", project.id, exc)
            refusals.append(f"Project {project.id}'s GitLab connection could not be read.")
            continue
        if gitlab is None:
            continue
        entries.append(
            GitHubTokenOut(
                repos=_gitlab_repos(project, source),
                token="",
                expires_at=datetime.now(UTC) + PROXY_ENTRY_LIFETIME,
                base_url=settings.assistant.api_url.rstrip("/")
                + GITLAB_PROXY_PATH.format(project_id=project.id),
                forge="gitlab",
                project_id=project.id,
                bot_login=gitlab.bot_login,
            )
        )
    if refusals and not entries:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Factorial has no read-only access: {refusals[0]}",
        )
    log.info("handed the assistant %d read-only entries", len(entries))
    return entries


@router.get(
    "/assistant/github/projects/{project_id}/{path:path}",
    dependencies=[Depends(require_bearer)],
)
@needs(Permission.ASSISTANT_USE)
async def github_read_proxy(
    project_id: int, path: str, request: Request, session: SessionDep, http: GitHubProxyHttpDep
) -> Response:
    """The assistant's GitHub reads for a token project (design.md §7.18): GET only, and only
    under this project's repositories. The stored token is added here, never echoed, and none of
    GitHub's headers but the content type come back."""
    project = await session.get(Project, project_id)
    connection = None if project is None else await connection_for_project(session, project)
    if project is None or connection is None or connection.token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This project has no GitHub token.")
    parts = path.split("/")
    allowed = {repo.lower() for repo in await github_repos(session, project)}
    # Starlette has already decoded the path, so `..` here is a traversal however it was sent.
    if (
        len(parts) < 3
        or parts[0] != "repos"
        or f"{parts[1]}/{parts[2]}".lower() not in allowed
        or any(part in ("", ".", "..") for part in parts[:-1])
        or parts[-1] in (".", "..")
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "The read proxy reads this project's repositories alone."
        )
    token = connection.token.get_secret_value()
    # The upstream read can take its full timeout; hold no pooled connection for it.
    await session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    if accept := request.headers.get("accept"):
        headers["Accept"] = accept
    try:
        upstream = await http.get(
            "/" + "/".join(quote(part, safe="") for part in parts),
            params=request.url.query,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub could not be reached.") from exc
    return Response(
        content=upstream.content.replace(token.encode(), b"[redacted]"),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


#: The paging headers GitLab answers a list with. Not `Link`, which names the GitLab host.
_GITLAB_PAGING: Final = (
    "X-Next-Page",
    "X-Page",
    "X-Per-Page",
    "X-Prev-Page",
    "X-Total",
    "X-Total-Pages",
)
#: Query parameters GitLab reads as a credential, or as one user acting as another.
_GITLAB_CREDENTIAL_PARAMS: Final = frozenset({"private_token", "access_token", "job_token", "sudo"})
_SEPARATORS: Final = str.maketrans({"\\": "/"})
#: What the chat tools' GitLab readers read under a project, besides the project itself. The
#: token is a Maintainer's, so anything else (`variables`, `jobs`, `hooks`, ...) can hand out a
#: CI secret.
_GITLAB_READS: Final = frozenset({"repository", "issues", "merge_requests", "members"})


def _gitlab_path(request: Request, project_id: int, allowed: set[str]) -> list[str] | None:
    """The decoded segments of `projects/<repo>/...` from the raw request path, or None when it
    names a repo outside `allowed` or anything a server could read as a climb."""
    raw = request.scope.get("raw_path")
    if not isinstance(raw, bytes):
        return None
    try:
        text = raw.decode("ascii")
        _, found, tail = text.split("?", 1)[0].partition(
            f"/assistant/gitlab/projects/{project_id}/"
        )
        segments = tail.split("/")
        decoded = [unquote(segment, errors="strict") for segment in segments]
    except UnicodeDecodeError:
        return None
    if not found or len(segments) < 2 or segments[0] != "projects":
        return None
    if decoded[1].lower() not in allowed or (len(decoded) > 2 and decoded[2] not in _GITLAB_READS):
        return None
    for segment in decoded[2:]:
        if "%" in segment or any(
            part in ("", ".", "..") for part in segment.translate(_SEPARATORS).split("/")
        ):
            return None
    return decoded


def _without_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_tokens(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.endswith("_token"))
        }
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    return value


@router.get(
    "/assistant/gitlab/projects/{project_id}/{path:path}",
    dependencies=[Depends(require_bearer)],
)
@needs(Permission.ASSISTANT_USE)
async def gitlab_read_proxy(
    project_id: int, request: Request, session: SessionDep, http_for: GitLabProxyHttpDep
) -> Response:
    """The assistant's GitLab reads (design.md §7.18): GET only, and only under this project's
    GitLab repo and intake repo. The stored token is added here, never echoed, and none of
    GitLab's headers but the content type and paging come back."""
    project = await session.get(Project, project_id)
    source = None if project is None else await ticket_source(session, project.id)
    gitlab = (
        None
        if project is None or source is None or source.kind != TicketSourceKind.GITLAB
        else await settings_for_project(session, project)
    )
    if project is None or source is None or gitlab is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This project has no GitLab connection.")
    parts = _gitlab_path(
        request, project_id, {repo.lower() for repo in _gitlab_repos(project, source)}
    )
    if parts is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "The read proxy reads this project's repositories alone."
        )
    token = gitlab.token.get_secret_value()
    # The upstream read can take its full timeout; hold no pooled connection for it.
    await session.commit()
    params = tuple(
        (name, value)
        for name, value in parse_qsl(request.url.query, keep_blank_values=True)
        if name.lower() not in _GITLAB_CREDENTIAL_PARAMS
    )
    try:
        async with http_for(gitlab) as http:
            upstream = await http.get(
                "/" + "/".join(quote(part, safe="") for part in parts),
                params=params,
                headers={"PRIVATE-TOKEN": token},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitLab could not be reached.") from exc
    media_type = upstream.headers.get("content-type", "")
    # Never the body of an error or a non-JSON answer: a host that is not GitLab would have its
    # page relayed through the API.
    if not upstream.is_success:
        raise HTTPException(upstream.status_code, f"GitLab answered {upstream.status_code}.")
    if media_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitLab did not answer with JSON.")
    content = upstream.content
    # The bare project carries `runners_token`, which registers a runner that would see CI secrets.
    if len(parts) == 2:
        with contextlib.suppress(ValueError):
            content = json.dumps(_without_tokens(upstream.json())).encode()
    return Response(
        content=content.replace(token.encode(), b"[redacted]"),
        status_code=upstream.status_code,
        media_type=media_type,
        headers={
            name: upstream.headers[name] for name in _GITLAB_PAGING if name in upstream.headers
        },
    )
