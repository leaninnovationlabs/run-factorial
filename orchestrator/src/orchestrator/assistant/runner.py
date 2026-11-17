"""One assistant turn, from a claimed `chat_turn` to its items, its usage and its end.

The turn reads the assistant's settings and credential when it starts, so a change on the
Assistant settings page reaches the next turn and never the one running. The API hands it the
credential (`credential.py`): this process holds no master key, so it cannot open one. It builds
the spec and the turn's toolbox, whose tools run in this process (design.md §7.18), refreshes the
read-only GitHub token they read with when it is stale, and writes what the backend says as
items, each in a short transaction of its own with a NOTIFY, so the Drawer sees it as it lands: a
reply at most every `text_flush_ms`, everything else as it arrives.

Beside the backend runs a keeper. It renews the lease every third of `lease_seconds`, and on
every wake of the queue channel; the same statement reads `cancel_requested`, and a cancel goes
to `backend.cancel`. A turn whose lease this process lost (another process recovered it) is
cancelled the same way. A backend that ends cancelled when nobody asked was stopped by the
process shutting down, so the turn ends interrupted, like one a restart recovers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, cast

from pydantic import SecretStr
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import Permission
from orchestrator.access.decision import allowed, caller_named
from orchestrator.api.services.assistant_settings import (
    host_bedrock_refused,
    load_assistant_settings,
)
from orchestrator.assistant.attachments import IMAGE_TYPES, TEXT_TYPE
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.credential import CredentialFetch, SettingsChanged
from orchestrator.assistant.github_token import (
    OrganizationTokens,
    ReadTokenCache,
    factory_repos,
)
from orchestrator.assistant.items import (
    SUMMARY_CAP,
    TEXT_CAP,
    append_item,
    append_notice,
    lock_conversation,
    notify_items,
    update_item,
)
from orchestrator.assistant.prompt import (
    HISTORY_FILE_CHARS,
    history_message,
    system_prompt,
    turn_message,
)
from orchestrator.assistant.state_store import PostgresStateStore
from orchestrator.assistant.toolbox import TurnToolbox
from orchestrator.assistant.turns import NOT_SET_UP, RESTART_NOTICE, finish_turn, renew_lease
from orchestrator.assistant.vocabulary import BACKENDS, HOST_ROLE_MODE, takes_images
from orchestrator.db.base import JsonDict
from orchestrator.db.models import (
    ChatAttachment,
    ChatConversation,
    ChatItem,
    ChatItemKind,
    ChatProposal,
    ChatTurn,
    ChatTurnStatus,
    ChatTurnTrigger,
    CostSource,
    Organization,
    Project,
    Ticket,
)
from orchestrator.db.session import SessionScope
from orchestrator.factorial.bedrock_token import BedrockMintError, mint_bearer
from orchestrator.logs import note
from orchestrator.mcp_server.chat.routes import route_for
from orchestrator.mcp_server.chat.scope import ChatScope
from orchestrator.mcp_server.context import McpDeps
from orchestrator.settings import Settings
from runtime.assistant.contract import (
    ChatBackend,
    ChatEvent,
    ChatImage,
    ChatTurnSpec,
    HistoryItem,
    TextDelta,
    TextDone,
    ToolCall,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnFailure,
    TurnLimits,
    TurnStop,
    Usage,
)
from runtime.contract import AdapterUnavailable, AttachmentMediaType

__all__ = ["LIMIT_NOTICE", "RunnerDeps", "Wakes", "run_turn"]

log = logging.getLogger(__name__)

LIMIT_NOTICE: Final = "Stopped at the per-turn limit."
SETTINGS_CHANGED: Final = "The assistant's settings changed during the turn. Send it again."
_FAILURE_NOTICES: Final[dict[TurnFailure, str]] = {
    TurnFailure.AUTH: "The {label} credential was rejected.",
    TurnFailure.RATE_LIMIT: "The model is rate limited. Wait, then retry.",
    TurnFailure.REFUSAL: "The model declined this request.",
    TurnFailure.CONFIG: "The assistant is misconfigured.",
    TurnFailure.TIMEOUT: "The turn ran out of time.",
    TurnFailure.INFRASTRUCTURE: "The assistant could not finish this turn.",
}

#: Text items a turn carries for a backend that keeps no session, or loses the one it kept.
HISTORY: Final = 20


class Wakes:
    """A broadcast: `fire` wakes every waiter on the event `next` handed out before it."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def next(self) -> asyncio.Event:
        return self._event

    def fire(self) -> None:
        self._event.set()
        self._event = asyncio.Event()


@dataclass(frozen=True)
class RunnerDeps:
    sessions: SessionScope
    settings: Callable[[], Settings]
    backend_for: Callable[[str], ChatBackend]
    clock: Callable[[], datetime]
    #: The tools' ports on one organization's token: GitHub reads, and no writer
    #: (`github_token.tool_deps`).
    tools: Callable[[ReadTokenCache], McpDeps]
    #: One read-only GitHub token per organization, so a turn reads its own tenant's repos alone.
    github_tokens: OrganizationTokens
    #: Each turn's model credential, opened by the API (`credential.api_credential`).
    credential: CredentialFetch
    #: Fired by the loop on every NOTIFY of the queue channel: a cancel may have landed.
    wakes: Wakes = field(default_factory=Wakes)


@dataclass(frozen=True)
class _Outcome:
    status: ChatTurnStatus
    stop: TurnStop | None = None
    failure: TurnFailure | None = None
    message: str | None = None


_STREAM_ENDED: Final = _Outcome(
    ChatTurnStatus.FAILED,
    failure=TurnFailure.INFRASTRUCTURE,
    message="The backend's stream ended without a result.",
)
_STOPPING: Final = _Outcome(ChatTurnStatus.INTERRUPTED)


@dataclass(frozen=True)
class _Prepared:
    spec: ChatTurnSpec
    backend: ChatBackend
    backend_kind: str
    conversation_id: int
    process: Settings
    tools: TurnToolbox
    organization_id: int
    github_token: ReadTokenCache


async def run_turn(turn_id: int, deps: RunnerDeps, *, owner: str) -> None:
    """Run a turn `owner` claimed. Returns once the turn has ended; never raises for the turn's
    own failures, which end up on the row."""
    # Each turn is a task of its own (`loop.serve`), so the fields end with it.
    note(turn_id=turn_id)
    try:
        prepared = await _prepare(turn_id, deps, owner)
    except Exception as exc:
        log.exception("assistant turn %s could not start", turn_id)
        await _end(
            turn_id,
            deps,
            owner,
            None,
            _Outcome(
                ChatTurnStatus.FAILED,
                failure=TurnFailure.INFRASTRUCTURE,
                message=type(exc).__name__,
            ),
        )
        return
    if prepared is not None:
        await _refresh_github(deps, prepared)
        await _run(turn_id, prepared, deps, owner)


async def _turn_credential(
    deps: RunnerDeps, organization: Organization, assistant: AssistantSettings
) -> SecretStr | None:
    """The stored credential, from the API, or a Bedrock bearer minted from the factory host
    role. The API's answer is a database read on loopback, unlike the GitHub fetch, so it may
    wait under the conversation's lock."""
    if host_bedrock_refused(assistant, multi_org=deps.settings().multi_org.enabled):
        return None
    if assistant.auth_mode == HOST_ROLE_MODE:
        try:
            return SecretStr(await asyncio.to_thread(mint_bearer, region=assistant.bedrock_region))
        except BedrockMintError:
            return None
    return await deps.credential(organization.slug, assistant)


async def _refresh_github(deps: RunnerDeps, prepared: _Prepared) -> None:
    """Outside `_prepare`'s transaction, since the fetch is an HTTP call and the conversation's
    lock must not wait on it. Never raises: a turn with no token still runs."""
    try:
        async with deps.sessions() as session:
            wanted = await factory_repos(session, prepared.organization_id)
        await prepared.github_token.refresh(
            wanted,
            wall_clock=timedelta(seconds=prepared.process.assistant.turn_wall_clock_seconds),
        )
    except Exception:
        log.warning("assistant: the GitHub token refresh failed", exc_info=True)


async def _prepare(turn_id: int, deps: RunnerDeps, owner: str) -> _Prepared | None:
    async with deps.sessions() as session:
        turn = await session.get(ChatTurn, turn_id)
        if turn is None or turn.status != ChatTurnStatus.RUNNING or turn.lease_owner != owner:
            return None
        conversation = await session.get_one(ChatConversation, turn.conversation_id)
        note(conversation_id=conversation.id, org_id=conversation.organization_id)
        now = deps.clock()
        if turn.cancel_requested:
            await finish_turn(
                session,
                turn,
                owner=owner,
                status=ChatTurnStatus.CANCELLED,
                now=now,
                stop=TurnStop.CANCELLED,
            )
            return None
        # Before the turn's own row, in the order a delete takes them (0024).
        await lock_conversation(session, turn.conversation_id)
        organization_id = conversation.organization_id
        if not await allowed(
            session,
            caller_named(conversation.owner),
            organization_id,
            Permission.ASSISTANT_USE,
        ):
            # A member removed, or demoted, after the message was queued (design.md §7.19).
            await _fail_config(
                session,
                turn,
                owner,
                now,
                "you may no longer use the assistant in this organization",
                notice=None,
            )
            return None
        assistant = await load_assistant_settings(session, organization_id)
        organization = await session.get_one(Organization, organization_id)
        try:
            credential = (
                await _turn_credential(deps, organization, assistant) if assistant.enabled else None
            )
        except SettingsChanged:
            await _fail_config(
                session,
                turn,
                owner,
                now,
                "the assistant's settings changed during the turn",
                notice=SETTINGS_CHANGED,
            )
            return None
        if credential is None:
            await _fail_config(
                session, turn, owner, now, "the assistant is off or has no credential"
            )
            return None
        try:
            backend = deps.backend_for(assistant.backend)
        except (AdapterUnavailable, ImportError) as exc:
            await _fail_config(session, turn, owner, now, str(exc), notice=None)
            return None

        process = deps.settings()
        wall_clock = timedelta(seconds=process.assistant.turn_wall_clock_seconds)
        turn.model = assistant.model
        message, images = await _asked(
            session,
            turn,
            conversation.organization_id,
            takes_images=backend.supports_images
            and takes_images(assistant.backend, assistant.auth_mode),
        )
        spec = ChatTurnSpec(
            turn_id=str(turn.id),
            conversation_id=str(conversation.id),
            model=assistant.model,
            effort=assistant.effort if backend.supports_effort else None,
            # AssistantSettings has refused any mode its backend does not offer.
            auth_mode=cast(Literal["api_key", "subscription", "bedrock"], assistant.auth_mode),
            credential=credential,
            system_prompt=system_prompt(),
            message=message,
            images=images,
            history=await _history(session, turn),
            resume_session=(
                conversation.backend_session_id
                if backend.supports_resume and conversation.backend_kind == assistant.backend
                else None
            ),
            limits=TurnLimits(
                max_steps=assistant.max_steps,
                max_budget_usd=assistant.turn_budget_usd,
                wall_clock=wall_clock,
            ),
            bedrock_region=(
                assistant.bedrock_region
                if assistant.auth_mode == "bedrock"
                else None
            ),
        )
        github_token = deps.github_tokens.for_organization(organization.id, organization.slug)
        scope = ChatScope(turn.id, conversation.id, conversation.owner, organization.id)
        return _Prepared(
            spec,
            backend,
            assistant.backend,
            conversation.id,
            process,
            TurnToolbox(deps.tools(github_token), scope),
            organization.id,
            github_token,
        )


async def _fail_config(
    session: AsyncSession,
    turn: ChatTurn,
    owner: str,
    now: datetime,
    message: str,
    *,
    notice: str | None = NOT_SET_UP,
) -> None:
    _log_failure(turn.id, TurnFailure.CONFIG, message)
    ended = await finish_turn(
        session,
        turn,
        owner=owner,
        status=ChatTurnStatus.FAILED,
        now=now,
        failure=TurnFailure.CONFIG,
        message=message,
    )
    if not ended:
        return
    await append_notice(
        session,
        conversation_id=turn.conversation_id,
        turn_id=turn.id,
        text=notice or _FAILURE_NOTICES[TurnFailure.CONFIG],
        reason=TurnFailure.CONFIG,
    )


async def _asked(
    session: AsyncSession, turn: ChatTurn, organization_id: int, *, takes_images: bool
) -> tuple[str, tuple[ChatImage, ...]]:
    """The person's message, after every decision the model has not heard: a decline or an
    expiry queues no turn of its own. With it, the images they attached, for a backend that
    takes them; one that takes none has its model told what it cannot see."""
    decisions = await _decisions(session, turn)
    if turn.trigger == ChatTurnTrigger.APPROVAL:
        told = "\n".join(decisions) or "The person decided a proposal; its card shows the outcome."
        return told, ()
    sent = (
        await session.scalars(
            select(ChatItem)
            .where(
                ChatItem.conversation_id == turn.conversation_id,
                ChatItem.kind == ChatItemKind.USER_TEXT,
            )
            .order_by(ChatItem.id.desc())
            .limit(1)
        )
    ).one()
    attached = (
        await session.scalars(
            select(ChatAttachment)
            .where(ChatAttachment.item_id == sent.id)
            .order_by(ChatAttachment.id)
        )
    ).all()
    pictures = [file for file in attached if file.media_type in IMAGE_TYPES]
    page = sent.payload.get("page")
    context = sent.payload.get("context")
    said = turn_message(
        str(sent.payload.get("text", "")),
        page,
        context=context if isinstance(context, list) else (),
        project_id=await _ticket_project(session, page, organization_id),
        files=[
            (file.name, file.content.decode("utf-8", "replace"))
            for file in attached
            if file.media_type == TEXT_TYPE
        ],
        images=[file.name for file in pictures],
        images_seen=takes_images,
    )
    images = tuple(
        ChatImage(
            name=f"image-{number}",
            media_type=cast(AttachmentMediaType, file.media_type),
            content=file.content,
        )
        for number, file in enumerate(pictures, start=1)
    )
    return "\n".join([*decisions, said]), images if takes_images else ()


async def _ticket_project(session: AsyncSession, page: object, organization_id: int) -> int | None:
    """The project of the ticket whose page the message came from, in the conversation's own
    organization: the page's address names only the ticket."""
    if not isinstance(page, str) or route_for(page) != "/tickets/:ticketId":
        return None
    number = page.rsplit("/", 1)[1]
    # The route takes any run of digits; a row's id is a 64-bit number.
    if len(number) > 18:
        return None
    return await session.scalar(
        select(Ticket.project_id)
        .join(Project, Project.id == Ticket.project_id)
        .where(
            Ticket.id == int(number),
            Project.organization_id == organization_id,
        )
    )


async def _decisions(session: AsyncSession, turn: ChatTurn) -> list[str]:
    """Each proposal decided since the start of the last earlier turn that ended done: two
    approvals in a row share one continuation, and one decided while that turn ran is news to
    this one. A turn that failed, was cancelled or was interrupted may never have told the
    model."""
    since = await session.scalar(
        select(ChatTurn.started_at)
        .where(
            ChatTurn.conversation_id == turn.conversation_id,
            ChatTurn.id < turn.id,
            ChatTurn.status == ChatTurnStatus.DONE,
            ChatTurn.started_at.is_not(None),
        )
        .order_by(ChatTurn.id.desc())
        .limit(1)
    )
    decided = (
        await session.scalars(
            select(ChatProposal)
            .where(
                ChatProposal.conversation_id == turn.conversation_id,
                ChatProposal.decided_at.is_not(None),
                *([] if since is None else [ChatProposal.decided_at >= since]),
            )
            .order_by(ChatProposal.decided_at, ChatProposal.id)
        )
    ).all()
    return [_decision(proposal) for proposal in decided]


def _decision(proposal: ChatProposal) -> str:
    line = f"The person decided proposal P{proposal.id}: {proposal.status}."
    if proposal.result is None:
        return line
    return f"{line} Result: {json.dumps(proposal.result)[:SUMMARY_CAP]}."


async def _history(session: AsyncSession, turn: ChatTurn) -> tuple[HistoryItem, ...]:
    earlier = (
        await session.scalars(
            select(ChatItem)
            .where(
                ChatItem.conversation_id == turn.conversation_id,
                ChatItem.kind.in_((ChatItemKind.USER_TEXT, ChatItemKind.ASSISTANT_TEXT)),
                or_(ChatItem.turn_id.is_(None), ChatItem.turn_id != turn.id),
            )
            .order_by(ChatItem.id.desc())
            .limit(HISTORY)
        )
    ).all()
    # Only as much of each text file as the history tells: a character is four bytes at most.
    files: dict[int, list[tuple[str, str]]] = {}
    for item_id, name, head in await session.execute(
        select(
            ChatAttachment.item_id,
            ChatAttachment.name,
            func.substring(ChatAttachment.content, 1, 4 * HISTORY_FILE_CHARS + 4),
        )
        .where(
            ChatAttachment.item_id.in_([item.id for item in earlier]),
            ChatAttachment.media_type == TEXT_TYPE,
        )
        .order_by(ChatAttachment.id)
    ):
        files.setdefault(item_id, []).append((name, bytes(head).decode("utf-8", "ignore")))
    return tuple(
        HistoryItem(
            role="user" if item.kind == ChatItemKind.USER_TEXT else "assistant",
            text=(
                history_message(item.payload, files=files.get(item.id, ()))
                if item.kind == ChatItemKind.USER_TEXT
                else str(item.payload.get("text", ""))
            ),
        )
        for item in reversed(earlier)
    )


async def _run(turn_id: int, prepared: _Prepared, deps: RunnerDeps, owner: str) -> None:
    backend = prepared.backend
    writer = _Writer(deps, prepared)
    keeper = _Keeper(deps, turn_id, owner, backend, prepared.process.assistant.lease_seconds)
    keeping = asyncio.create_task(keeper.run())
    store = PostgresStateStore(deps.sessions, prepared.conversation_id)
    seconds = prepared.process.assistant.turn_wall_clock_seconds
    deadline = asyncio.timeout(seconds)
    outcome: _Outcome | None = None
    try:
        try:
            async with (
                deadline,
                _closing(backend.turn(prepared.spec, store, prepared.tools)) as events,
            ):
                async for event in events:
                    outcome = await writer.write(event) or outcome
        except TimeoutError:
            if not deadline.expired():
                raise
            await backend.cancel(prepared.spec.turn_id)
            outcome = _Outcome(
                ChatTurnStatus.FAILED,
                failure=TurnFailure.TIMEOUT,
                message=f"The turn outlived its {seconds:g} s wall clock.",
            )
        if outcome is None:
            outcome = _STREAM_ENDED
        elif outcome.status == ChatTurnStatus.CANCELLED and not keeper.cancelled:
            outcome = _STOPPING
    except asyncio.CancelledError:
        outcome = _STOPPING
        raise
    except Exception as exc:
        log.exception("assistant turn %s failed", turn_id)
        with suppress(Exception):
            await backend.cancel(prepared.spec.turn_id)
        outcome = _Outcome(
            ChatTurnStatus.FAILED, failure=TurnFailure.INFRASTRUCTURE, message=type(exc).__name__
        )
    finally:
        keeping.cancel()
        await asyncio.gather(keeping, return_exceptions=True)
        await _end(turn_id, deps, owner, writer, outcome or _STREAM_ENDED)


def _log_failure(turn_id: int, failure: TurnFailure, message: str | None) -> None:
    """A backend that reports its own failure raises nothing, so no traceback says why: without
    this line the reason is on the turn's row and nowhere a person watching the process looks."""
    log.warning("assistant turn %s failed (%s): %s", turn_id, failure, message)


async def _end(
    turn_id: int, deps: RunnerDeps, owner: str, writer: _Writer | None, outcome: _Outcome
) -> None:
    if outcome.failure is not None:
        _log_failure(turn_id, outcome.failure, outcome.message)
    if writer is not None:
        try:
            await writer.close()
        except Exception:
            log.exception("assistant turn %s: closing its items failed", turn_id)
    async with deps.sessions() as session:
        now = deps.clock()
        turn = await session.get(ChatTurn, turn_id)
        if turn is None:
            # Its conversation was deleted.
            return
        # The conversation before the turn: the order every writer of a conversation keeps.
        await lock_conversation(session, turn.conversation_id)
        ended = await finish_turn(
            session,
            turn,
            owner=owner,
            status=outcome.status,
            now=now,
            stop=None if outcome.stop is None else str(outcome.stop),
            failure=outcome.failure,
            message=outcome.message,
        )
        # Recovered by another process, which wrote its own notice: not this turn's to end.
        if not ended:
            return
        notice = _notice(outcome, BACKENDS[writer.backend_kind].label if writer else "")
        if notice is not None:
            await append_notice(
                session,
                conversation_id=turn.conversation_id,
                turn_id=turn.id,
                text=notice[0],
                reason=notice[1],
            )


@asynccontextmanager
async def _closing(events: AsyncIterator[ChatEvent]) -> AsyncIterator[AsyncIterator[ChatEvent]]:
    """Close a stream the turn stops reading, so the backend releases its child at once. The
    contract promises an iterator; an async generator, the usual one, also has `aclose`."""
    try:
        yield events
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()


def _notice(outcome: _Outcome, label: str) -> tuple[str, str | None] | None:
    if outcome.status == ChatTurnStatus.INTERRUPTED:
        return RESTART_NOTICE, None
    if outcome.failure is not None:
        return _FAILURE_NOTICES[outcome.failure].format(label=label), str(outcome.failure)
    if outcome.stop in (TurnStop.BUDGET, TurnStop.STEPS):
        return LIMIT_NOTICE, str(outcome.stop)
    return None


def _ended(stop: TurnStop) -> _Outcome:
    if stop == TurnStop.CANCELLED:
        return _Outcome(ChatTurnStatus.CANCELLED, stop=stop)
    return _Outcome(ChatTurnStatus.DONE, stop=stop)


class _Keeper:
    def __init__(
        self,
        deps: RunnerDeps,
        turn_id: int,
        owner: str,
        backend: ChatBackend,
        lease_seconds: float,
    ) -> None:
        self._deps = deps
        self._turn_id = turn_id
        self._owner = owner
        self._backend = backend
        self._lease = timedelta(seconds=lease_seconds)
        #: The backend was told to stop because the turn asked it, not because the process is.
        self.cancelled = False

    async def run(self) -> None:
        interval = self._lease.total_seconds() / 3
        while True:
            woken = self._deps.wakes.next()
            try:
                await self._renew()
            except Exception:
                log.warning("assistant turn %s: lease renewal failed", self._turn_id, exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(woken.wait(), timeout=interval)

    async def _renew(self) -> None:
        async with self._deps.sessions() as session:
            cancel = await renew_lease(
                session,
                self._turn_id,
                owner=self._owner,
                until=self._deps.clock() + self._lease,
            )
        # True: a person pressed Stop. None: the turn is no longer this process's to run.
        if cancel is not False and not self.cancelled:
            self.cancelled = True
            await self._backend.cancel(str(self._turn_id))


@dataclass
class _Text:
    item_id: int
    text: str
    flushed_at: datetime
    dirty: bool = False
    final: bool = False


class _Writer:
    """The backend's events as items. Text is held per block and written at most every
    `text_flush_ms`; a tool call is one item, updated with its result."""

    def __init__(self, deps: RunnerDeps, prepared: _Prepared) -> None:
        self._deps = deps
        self._turn_id = int(prepared.spec.turn_id)
        self._conversation_id = prepared.conversation_id
        self.backend_kind = prepared.backend_kind
        self._flush = timedelta(milliseconds=prepared.process.assistant.text_flush_ms)
        self._texts: dict[str, _Text] = {}
        self._tools: dict[str, tuple[int, JsonDict]] = {}

    async def write(self, event: ChatEvent) -> _Outcome | None:
        """The turn's outcome when `event` ends it."""
        match event:
            case TextDelta():
                await self._delta(event)
            case TextDone():
                await self._done(event)
            case ToolCall():
                await self._flush_texts()
                await self._call(event)
            case ToolResult():
                await self._result(event)
            case Usage():
                await self._usage(event)
            case TurnDone():
                return _ended(event.stop)
            case TurnFailed():
                return _Outcome(ChatTurnStatus.FAILED, failure=event.reason, message=event.message)
        return None

    async def close(self) -> None:
        """Mark what the turn left open as final: a reply cut off, a call with no result."""
        for text in self._texts.values():
            if not text.final:
                await self._update(text.item_id, {"text": text.text}, final=True)
                text.final = True
        for item_id, payload in self._tools.values():
            await self._update(item_id, {**payload, "status": "stopped"}, final=True)
        self._tools.clear()

    async def _delta(self, event: TextDelta) -> None:
        now = self._deps.clock()
        text = self._texts.get(event.block)
        if text is None:
            body = event.text[:TEXT_CAP]
            item_id = await self._append(ChatItemKind.ASSISTANT_TEXT, {"text": body}, final=False)
            self._texts[event.block] = _Text(item_id, body, now)
            return
        text.text = (text.text + event.text)[:TEXT_CAP]
        text.dirty = True
        if now - text.flushed_at >= self._flush:
            await self._write_text(text, now)

    async def _done(self, event: TextDone) -> None:
        body = event.text[:TEXT_CAP]
        text = self._texts.get(event.block)
        if text is None:
            item_id = await self._append(ChatItemKind.ASSISTANT_TEXT, {"text": body}, final=True)
            self._texts[event.block] = _Text(item_id, body, self._deps.clock(), final=True)
            return
        text.text, text.final, text.dirty = body, True, False
        await self._update(text.item_id, {"text": body}, final=True)

    async def _flush_texts(self) -> None:
        now = self._deps.clock()
        for text in self._texts.values():
            if text.dirty and not text.final:
                await self._write_text(text, now)

    async def _write_text(self, text: _Text, now: datetime) -> None:
        await self._update(text.item_id, {"text": text.text}, final=False)
        text.flushed_at, text.dirty = now, False

    async def _call(self, event: ToolCall) -> None:
        encoded = json.dumps(event.input, default=str)
        payload: JsonDict = {"call_id": event.call_id, "name": event.name}
        if len(encoded) <= SUMMARY_CAP:
            payload["input"] = json.loads(encoded)
        else:
            payload["input_preview"] = encoded[:SUMMARY_CAP]
        payload["status"] = "running"
        item_id = await self._append(ChatItemKind.TOOL, payload, final=False)
        self._tools[event.call_id] = (item_id, payload)

    async def _result(self, event: ToolResult) -> None:
        called = self._tools.pop(event.call_id, None)
        if called is None:
            log.warning(
                "assistant turn %s: a result for unknown call %s", self._turn_id, event.call_id
            )
            return
        item_id, payload = called
        result = {
            **payload,
            "status": "done",
            "ok": event.ok,
            "summary": event.summary[:SUMMARY_CAP],
        }
        await self._update(item_id, result, final=True)

    async def _usage(self, event: Usage) -> None:
        source = None
        if event.cost_usd is not None:
            source = CostSource.REPORTED if event.cost_reported else CostSource.ESTIMATED
        async with self._deps.sessions() as session:
            # The conversation before the turn: the order every writer of a conversation keeps.
            if event.session_id is not None:
                await session.execute(
                    update(ChatConversation)
                    .where(ChatConversation.id == self._conversation_id)
                    .values(backend_session_id=event.session_id, backend_kind=self.backend_kind)
                )
            await session.execute(
                update(ChatTurn)
                .where(ChatTurn.id == self._turn_id)
                .values(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    cost_usd=event.cost_usd,
                    cost_source=source,
                )
            )

    async def _append(self, kind: ChatItemKind, payload: JsonDict, *, final: bool) -> int:
        async with self._deps.sessions() as session:
            item = await append_item(
                session,
                conversation_id=self._conversation_id,
                turn_id=self._turn_id,
                kind=kind,
                payload=payload,
                final=final,
            )
            await notify_items(session, self._conversation_id)
        return item.id

    async def _update(self, item_id: int, payload: JsonDict, *, final: bool) -> None:
        async with self._deps.sessions() as session:
            await update_item(session, item_id, payload=payload, final=final)
            await notify_items(session, self._conversation_id)
