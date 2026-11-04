"""Preview URLs: where a step run's output can be viewed, as metadata about the run.

A producer — a CI job that serves the build, a fixture — publishes the URL; this module
validates it, records it as a `preview_url_updated` event against the run, and reads the latest
one back. There is no table: the log already binds an event to one `step_run`, which is the
identity a preview must never be separated from (a URL looked up "anywhere on the ticket" would
show one attempt's output as another's).

The dashboard frames the URL in a sandboxed iframe with scripts and same-origin allowed, which is
only safe while the framed page is on a different origin from the dashboard itself — hence the
origin check. Everything about the page behind the URL (hosting, sign-in, lifetime) is the
producer's; nothing here fetches it.

The change requests a reviewer sends from beside a preview are read here too, because the API
(which records them) and the gate (which honours them) must agree on what "pending" means. So is
the deployment's awake cap, which previews and inspect shells share, and the inspect shell's
lifecycle it counts.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, StepRun, Ticket, TicketStatus
from orchestrator.events.read_models import is_one_of, latest_run_event
from orchestrator.events.types import EventType
from orchestrator.events.workspaces import LIVE_STATUSES
from orchestrator.events.writer import append
from orchestrator.settings import Settings, normalized_origin

__all__ = [
    "INSPECT_LIFECYCLE",
    "LOOP_REQUESTER",
    "MAX_AWAKE_WORKSPACES",
    "MAX_PREVIEW_URL_LENGTH",
    "ChangeRequestOutcome",
    "InspectStatus",
    "PreviewLink",
    "PreviewStatus",
    "awake_inspect_shells",
    "awake_previews",
    "awake_workspaces",
    "change_request_by_key",
    "change_request_outcome",
    "check_preview_url",
    "inspect_lifecycle",
    "pending_change_request",
    "preview_keep_until",
    "preview_lifecycle",
    "preview_links",
    "publish_preview_links",
    "publish_preview_url",
]

MAX_PREVIEW_URL_LENGTH: Final = 2048


def check_preview_url(url: str, *, dashboard_origins: Collection[str], allow_http: bool) -> str:
    """The URL unchanged, or a ValueError saying what makes it unsafe to frame.
    `dashboard_origins` are normalized, as `Settings.dashboard_origins` holds them."""
    if len(url) > MAX_PREVIEW_URL_LENGTH:
        raise ValueError(f"The preview URL is longer than {MAX_PREVIEW_URL_LENGTH} characters.")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise ValueError("The preview URL contains whitespace or control characters.")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("The preview URL must be an https URL.")
    if scheme == "http" and not allow_http:
        raise ValueError(
            "The preview URL must use https. Factorial accepts http only when "
            "FACTORIAL_PREVIEW_ALLOW_HTTP is set for local development."
        )
    if parts.username is not None or parts.password is not None:
        raise ValueError("The preview URL must not embed a username or password.")
    if parts.query or parts.fragment:
        # The URL is kept in the append-only log for ever: a signed URL would keep its credential.
        raise ValueError("The preview URL must carry no query string or fragment.")
    if not parts.hostname:
        raise ValueError("The preview URL has no host.")
    origin = normalized_origin(url)
    if origin is None:
        raise ValueError("The preview URL has a port out of range or a host that is not ASCII.")
    if origin in dashboard_origins:
        raise ValueError("The preview URL must not be on the dashboard's own origin.")
    return url


#: `(service, url)`: `service` is None for a URL an outside producer published.
PreviewLink = tuple[str | None, str]


def _links(details: Mapping[str, Any] | None) -> list[PreviewLink]:
    if details is None:
        return []
    if "urls" not in details:
        # Written before a run could have several services.
        url = details.get("url")
        return [(None, url)] if isinstance(url, str) else []
    return [
        (entry.get("service"), entry["url"])
        for entry in details["urls"]
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    ]


async def preview_links(session: AsyncSession, run: StepRun) -> list[PreviewLink]:
    """The run's current URLs, first-declared service first; empty when none is published."""
    details = (
        await session.execute(
            latest_run_event(run.ticket_id, run.id, [EventType.PREVIEW_URL_UPDATED], Event.details)
        )
    ).scalar_one_or_none()
    return _links(details)


async def publish_preview_links(
    session: AsyncSession,
    run: StepRun,
    links: Sequence[PreviewLink],
    *,
    settings: Settings,
    published_via: str,
) -> bool:
    """Record `links` as the run's preview (`[]` withdraws it). Returns whether anything changed.

    The one way in for every producer, so validation cannot be skipped by calling from somewhere
    other than the API. The run's row is locked first: two deliveries of the same URLs would
    otherwise both read "changed" and write twice. Committing is the caller's.
    """
    for _, url in links:
        check_preview_url(
            url,
            dashboard_origins=settings.dashboard_origins,
            allow_http=settings.preview_allow_http,
        )
    await session.execute(select(StepRun.id).where(StepRun.id == run.id).with_for_update())
    if await preview_links(session, run) == list(links):
        return False
    project_id = (
        await session.execute(select(Ticket.project_id).where(Ticket.id == run.ticket_id))
    ).scalar_one()
    await append(
        session,
        type=EventType.PREVIEW_URL_UPDATED,
        project_id=project_id,
        ticket_id=run.ticket_id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        urls=[{"service": service, "url": url} for service, url in links],
        published_via=published_via,
    )
    return True


async def publish_preview_url(
    session: AsyncSession, run: StepRun, url: str | None, *, settings: Settings, published_via: str
) -> bool:
    """An outside producer's one URL, or None to withdraw it."""
    links: list[PreviewLink] = [] if url is None else [(None, url)]
    return await publish_preview_links(
        session, run, links, settings=settings, published_via=published_via
    )


PreviewStatus = Literal["none", "starting", "ready", "failed", "stopping", "stopped"]

_LIFECYCLE: Final = (
    EventType.PREVIEW_REQUESTED,
    EventType.PREVIEW_STOP_REQUESTED,
    EventType.PREVIEW_STARTED,
    EventType.PREVIEW_FAILED,
    EventType.PREVIEW_STOPPED,
)
_STATUS: Final[Mapping[str, PreviewStatus]] = {
    EventType.PREVIEW_REQUESTED: "starting",
    EventType.PREVIEW_STOP_REQUESTED: "stopping",
    EventType.PREVIEW_STARTED: "ready",
    EventType.PREVIEW_FAILED: "failed",
    EventType.PREVIEW_STOPPED: "stopped",
}
#: Nothing stays awake longer: the adapter parks a preview at `stopAfter`, at most 55m, and the
#: inspect pass parks a shell at `worker.inspect.INSPECT_LIMIT`, 55m.
_AWAKE_AT_MOST: Final = timedelta(hours=1)
#: Workspaces a preview or an inspect shell keeps awake at once, per deployment.
MAX_AWAKE_WORKSPACES: Final = 3


async def preview_lifecycle(
    session: AsyncSession, run: StepRun
) -> tuple[PreviewStatus, Event | None]:
    """What the run's runtime-served preview is doing, from its latest lifecycle event."""
    latest = (
        await session.scalars(latest_run_event(run.ticket_id, run.id, _LIFECYCLE, Event))
    ).first()
    return ("none", None) if latest is None else (_STATUS[latest.type], latest)


#: Who the loop names as the requester of a start it makes itself, after a renewal.
LOOP_REQUESTER: Final = "factorial"


async def preview_keep_until(
    session: AsyncSession, run: StepRun, stop_after: timedelta, *, now: datetime
) -> datetime:
    """When the run's preview closes for idle: `stop_after` past the latest of a person's start
    or restart, the run's end, and a Keep running. While the run is live it never comes, so the
    answer is always `stop_after` from now (design.md §4.2.5). The loop's own restart after a
    renewal is not a person's, and moves nothing. A preview a rework carried over has no event
    on this run, so the run's end counts."""
    if run.status in LIVE_STATUSES:
        return now + stop_after
    rows = (
        await session.scalars(
            select(Event)
            .where(
                Event.ticket_id == run.ticket_id,
                Event.step_run_id == run.id,
                Event.type.in_([EventType.PREVIEW_REQUESTED, EventType.PREVIEW_KEPT]),
            )
            .order_by(Event.id.desc())
            .limit(20)
        )
    ).all()
    person = next((row.created_at for row in rows if not row.details.get("renewal")), None)
    marks = [mark for mark in (run.ended_at, person) if mark is not None]
    return max(marks, default=now) + stop_after


async def awake_previews(session: AsyncSession) -> set[int]:
    """Step runs, anywhere in the deployment, whose preview is starting or serving.

    A done ticket gets no pass to record its preview's end, so its last word can say "started"
    for ever: a start older than any preview can serve does not count, and neither does a done
    ticket. The database's clock, because that is the clock `created_at` was written by.

    A preview beside a live run is not counted: the run keeps that sandbox awake anyway.
    """
    latest = (
        select(Event.step_run_id, Event.type, Event.created_at, Event.ticket_id)
        .where(is_one_of(_LIFECYCLE))
        .distinct(Event.step_run_id)
        .order_by(Event.step_run_id, Event.id.desc())
        .subquery()
    )
    rows = await session.execute(
        select(latest.c.step_run_id)
        .join(Ticket, Ticket.id == latest.c.ticket_id)
        .join(StepRun, StepRun.id == latest.c.step_run_id)
        .where(
            latest.c.type.in_([EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED]),
            StepRun.status.not_in(LIVE_STATUSES),
            latest.c.created_at >= func.now() - _AWAKE_AT_MOST,
            Ticket.status != TicketStatus.DONE,
        )
    )
    return {step_run_id for (step_run_id,) in rows if step_run_id is not None}


InspectStatus = Literal["none", "starting", "open", "parked", "failed"]

INSPECT_LIFECYCLE: Final = (
    EventType.INSPECT_REQUESTED,
    EventType.INSPECT_STARTED,
    EventType.INSPECT_PARKED,
)


async def inspect_lifecycle(
    session: AsyncSession, run: StepRun
) -> tuple[InspectStatus, Event | None]:
    """What the run's inspect shell is doing, from its latest lifecycle event: `starting` while a
    request is unanswered, `failed` for a park that refused one or a start that failed."""
    latest = (
        await session.scalars(latest_run_event(run.ticket_id, run.id, INSPECT_LIFECYCLE, Event))
    ).first()
    if latest is None:
        return "none", None
    if latest.type == EventType.INSPECT_REQUESTED:
        return "starting", latest
    if latest.type == EventType.INSPECT_STARTED:
        return "open", latest
    return ("failed" if latest.details.get("failed") is True else "parked"), latest


async def awake_inspect_shells(session: AsyncSession) -> set[int]:
    """Step runs, anywhere in the deployment, done tickets included, whose shell is starting or
    open.

    The inspect pass records every shell's end and refuses a request it leaves waiting, but a
    start older than any shell stays open does not count either, as a preview's does not.
    """
    latest = (
        select(Event.step_run_id, Event.type, Event.created_at)
        .where(is_one_of(INSPECT_LIFECYCLE))
        .distinct(Event.step_run_id)
        .order_by(Event.step_run_id, Event.id.desc())
        .subquery()
    )
    rows = await session.execute(
        select(latest.c.step_run_id).where(
            latest.c.type.in_([EventType.INSPECT_REQUESTED, EventType.INSPECT_STARTED]),
            latest.c.created_at >= func.now() - _AWAKE_AT_MOST,
        )
    )
    return {step_run_id for (step_run_id,) in rows if step_run_id is not None}


async def awake_workspaces(session: AsyncSession) -> set[int]:
    """The runs whose workspace a preview or an inspect shell keeps awake: what
    `MAX_AWAKE_WORKSPACES` caps. A run with both is one awake workspace."""
    return await awake_previews(session) | await awake_inspect_shells(session)


async def pending_change_request(
    session: AsyncSession, ticket_id: int, gate_run_id: int
) -> Event | None:
    """The oldest change request against this gate run that no refusal answered.

    Only meaningful while the run is WAITING: the rejection that honours a request is what ends
    the WAITING, so a decided run's requests are history whatever this returns for them.
    """
    rows = (
        await session.scalars(
            select(Event)
            .where(
                Event.ticket_id == ticket_id,
                Event.step_run_id == gate_run_id,
                Event.type.in_([EventType.STEP_CHANGES_REQUESTED, EventType.STEP_CHANGES_REFUSED]),
            )
            .order_by(Event.id)
        )
    ).all()
    refused = {
        row.details.get("request_id") for row in rows if row.type == EventType.STEP_CHANGES_REFUSED
    }
    return next(
        (
            row
            for row in rows
            if row.type == EventType.STEP_CHANGES_REQUESTED and row.id not in refused
        ),
        None,
    )


async def change_request_by_key(
    session: AsyncSession, ticket_id: int, idempotency_key: str
) -> Event | None:
    return (
        await session.scalars(
            select(Event).where(
                Event.ticket_id == ticket_id,
                Event.type == EventType.STEP_CHANGES_REQUESTED,
                Event.details["idempotency_key"].as_string() == idempotency_key,
            )
        )
    ).first()


ChangeRequestOutcome = Literal["pending", "accepted", "refused"]


async def change_request_outcome(
    session: AsyncSession, ticket_id: int, request_id: int
) -> tuple[ChangeRequestOutcome, str | None]:
    """What became of a request: the rejection that honoured it, the refusal that answered it
    (with its reason), or neither yet."""
    answer = (
        await session.execute(
            select(Event.type, Event.details)
            .where(
                Event.ticket_id == ticket_id,
                (
                    (Event.type == EventType.GATE_REJECTED)
                    & (Event.details["change_request_id"].as_integer() == request_id)
                )
                | (
                    (Event.type == EventType.STEP_CHANGES_REFUSED)
                    & (Event.details["request_id"].as_integer() == request_id)
                ),
            )
            .limit(1)
        )
    ).first()
    if answer is None:
        return "pending", None
    kind, details = answer
    if kind == EventType.GATE_REJECTED:
        return "accepted", None
    reason = details.get("reason")
    return "refused", reason if isinstance(reason, str) else None
