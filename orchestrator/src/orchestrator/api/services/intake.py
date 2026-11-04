"""Open an issue that a project's lane will admit, for an MCP client (design.md §7.16).

The API opens the issue as the App and wakes the worker. Admission stays the worker's job, so
nothing here inserts a ticket. Decision 53's "record before the ref" applies: the request event
commits before GitHub is asked. A reply lost after GitHub acted therefore leaves an unanswered
request, and a retry with the same key recovers the issue by the marker in its body instead of
opening a second one.

Two short transactions, each fenced by a transaction-scoped advisory lock. The first serialises
callers that share an idempotency key while they find or record the request. The second fences
that request across the one GitHub write. Every GitHub call inside them has a timeout, and no lock
is held across the admission wait.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import httpx
from githubkit.exception import RequestError, RequestFailed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import (
    Conflict,
    Invalid,
    Unavailable,
    UpstreamFailed,
    as_sentence,
)
from orchestrator.api.services.refs import ticket_source
from orchestrator.api.sessions import derive_key
from orchestrator.api.wakes import await_admission
from orchestrator.db.models import Event, Project, Ticket, TicketSourceKind, intake_repo
from orchestrator.events import EventType, append, wake_fleet
from orchestrator.github import IssueState, StateSource, refusal_reason
from orchestrator.github.issues import IssueWriter, MarkerUnconfirmed, marker_line
from orchestrator.gitlab.client import GitLabError
from orchestrator.gitlab.urls import gitlab_issue_url
from orchestrator.settings import get_settings
from orchestrator.worker.admission import NoLane, Pending, entry_plan, lane_for, pending_issue
from orchestrator.worker.selection import process_label

__all__ = ["MARKER_PREFIX", "CreatedTicket", "create_ticket"]

log = logging.getLogger(__name__)

#: What every request marker starts with. Refused in a caller's title and body, because recovery
#: adopts an issue whose body ends with a request's marker.
MARKER_PREFIX: Final = "factorial-request:"
_MARKER_KEY: Final = b"issue-marker-v1"
#: How long before its request a recovered issue may have been created: the clock skew between
#: Postgres and GitHub, and nothing more.
_RECOVERY_SLACK: Final = timedelta(minutes=5)
#: A backtick would end the footer's code span. Nothing outside these characters belongs in an
#: email address or the `api-token` principal.
_FOOTER_UNSAFE: Final = re.compile(r"[^A-Za-z0-9 ._@+:-]")
_NO_GITHUB: Final = "This project has no GitHub connection, so Factorial cannot open issues."
_NO_GITHUB_HINT: Final = (
    "A person connects the project to GitHub on Ticket source, as "
    "https://www.runfactorial.com/docs/github-app/ describes."
)
#: A hung GitHub call would otherwise hold an advisory lock and its connection.
_GITHUB_TIMEOUT: Final = 30.0
#: Failures that may come after GitHub acted. githubkit wraps a transport failure in a
#: `RequestError`; anything else is a local bug and propagates.
_NO_ANSWER: Final = (RequestError, httpx.TransportError, TimeoutError)
_RETRY_HINT: Final = (
    "The request is kept, so a retry with the same idempotency_key finds the issue if GitHub "
    "opened it."
)


@dataclass(frozen=True, slots=True)
class CreatedTicket:
    repo: str
    number: int
    url: str
    process: str
    labels_set: tuple[str, ...]
    assignee: str
    #: The key had already opened this issue, so nothing new was opened.
    replayed: bool
    #: The ticket that the woken pass admitted inside the wait.
    ticket: Ticket | None
    #: Why admission declines the issue. None with no ticket and no `pending_error`: the next
    #: pass admits it.
    pending: Pending | None
    #: Reading the verdict failed after the issue was opened, so `pending` is unknown.
    pending_error: str | None
    #: Entry leaves that opening the issue cannot make true (`entry_plan`). Empty on any call
    #: that finds its request already recorded, which skips the preflight.
    unsatisfiable: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Prepared:
    repo: str
    process: str
    labels: tuple[str, ...]
    unmet: tuple[str, ...]


async def create_ticket(
    session: AsyncSession,
    writer: IssueWriter | None,
    project: Project,
    *,
    title: str,
    body: str,
    assignee: str,
    process: str | None,
    idempotency_key: str | None,
    actor: Actor,
    bot_login: str,
    state_source: Callable[[str, int], StateSource],
) -> CreatedTicket:
    """Open an issue the lane admits, wake the worker, and report what the woken pass did.

    The same `(project, actor.by, idempotency_key)` never opens a second issue. An answered
    request is replayed, and an unanswered one is recovered by its marker before anything is
    created. Without a key, every call opens an issue.
    """
    title = title.strip()
    if not title or any(unicodedata.category(char) == "Cc" for char in title):
        raise Invalid(
            "The title must be one line of text.",
            hint="Remove line breaks and control characters.",
        )
    digest = _args_digest(title=title, body=body, assignee=assignee, process=process)
    request: Event | None = None
    if idempotency_key is not None:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    _lock_key("issue-create", project.id, actor.by, idempotency_key)
                )
            )
        )
        request = await _keyed_request(session, project.id, actor.by, idempotency_key)
    recovering = request is not None
    unmet: tuple[str, ...] = ()
    if request is None:
        prepared = await _preflight(
            session, writer, project, title=title, body=body, assignee=assignee, process=process
        )
        unmet = prepared.unmet
        details: dict[str, object] = {
            **actor.fields(),
            "args_digest": digest,
            "repo": prepared.repo,
            "process": prepared.process,
            "labels": list(prepared.labels),
            "assignee": assignee,
        }
        if idempotency_key is not None:
            details["idempotency_key"] = idempotency_key
        request = await append(
            session,
            type=EventType.ISSUE_CREATE_REQUESTED,
            project_id=project.id,
            ticket_id=None,
            step_run_id=None,
            **details,
        )
    elif request.details.get("args_digest") != digest:
        raise Invalid(
            "This idempotency_key was already used with different arguments.",
            hint="Use a new key for a different issue.",
        )
    # Releases the key's lock; the request's own lock below fences the GitHub write.
    await session.commit()

    if not await session.scalar(
        select(func.pg_try_advisory_xact_lock(_lock_key("issue-create-request", request.id)))
    ):
        raise Conflict(
            "A create with this idempotency_key is in progress.",
            hint="Call again shortly with the same key.",
        )
    answer = await _answer(session, project.id, request.id)
    replayed = answer is not None
    if answer is None:
        answer = await _open(
            session,
            writer,
            project,
            request,
            title=title,
            body=body,
            actor=actor,
            bot_login=bot_login,
            recovering=recovering,
        )
    await session.commit()
    return await _report(
        session,
        project,
        request,
        answer,
        replayed=replayed,
        unmet=unmet,
        state_source=state_source,
        bot_login=bot_login,
    )


async def _preflight(
    session: AsyncSession,
    writer: IssueWriter | None,
    project: Project,
    *,
    title: str,
    body: str,
    assignee: str,
    process: str | None,
) -> _Prepared:
    """Every refusal that needs no write, run before the request is recorded."""
    if writer is None:
        message, hint = await _missing_writer(session, project)
        raise Unavailable(message, hint=hint)
    if MARKER_PREFIX in title or MARKER_PREFIX in body:
        raise Invalid(
            f"The title and body must not contain {MARKER_PREFIX}.",
            hint="Factorial marks the issues it opens with it. Remove it.",
        )
    _refuse_paused(project)
    source = await ticket_source(session, project.id)
    if source is None or source.kind not in (
        TicketSourceKind.GITHUB,
        TicketSourceKind.GITLAB,
    ):
        raise Conflict(
            f"Project {project.name} has no GitHub ticket source.",
            hint="A person adds one on the project page.",
        )
    repo = intake_repo(project, source)
    # Admission reads the intake repo, while reconcile and projection read `github_repo`. A
    # ticket admitted from another repo would be reconciled against the wrong issue.
    if repo.lower() != project.github_repo.lower():
        raise Conflict(
            f"Project {project.name} reads issues from {repo}, "
            f"not from its repository {project.github_repo}.",
            hint="A person makes the ticket source match the project's repository.",
        )
    lane = await lane_for(session, project, process)
    if isinstance(lane, NoLane):
        raise Invalid(lane.detail, candidates=lane.offered)
    plan = entry_plan(lane.config.entry.when, {"repo": repo, "ticket_owner": assignee})
    labels = (*plan.labels, process_label(lane.key)) if lane.explicit else plan.labels
    await _refuse_unwritable(writer, repo, labels, assignee, hint=None)
    return _Prepared(repo=repo, process=lane.key, labels=labels, unmet=plan.unmet)


def _refuse_paused(project: Project) -> None:
    if not project.active:
        raise Conflict(
            f"Admission is paused for {project.name}.",
            hint="A person resumes it on the project page.",
        )


async def _refuse_unwritable(
    writer: IssueWriter, repo: str, labels: Sequence[str], assignee: str, *, hint: str | None
) -> None:
    try:
        async with asyncio.timeout(_GITHUB_TIMEOUT):
            defined = await writer.labels(repo)
            assignable = await writer.assignable(repo, assignee)
    except RequestFailed as exc:
        raise UpstreamFailed(
            f"Factorial couldn't read {repo} on GitHub: {as_sentence(refusal_reason(exc))}",
            hint=hint,
        ) from exc
    except GitLabError as exc:
        raise UpstreamFailed(
            f"Factorial couldn't read {repo} on GitLab: {as_sentence(str(exc))}", hint=hint
        ) from exc
    except _NO_ANSWER as exc:
        log.warning("no answer from GitHub reading %s", repo, exc_info=True)
        raise UpstreamFailed(
            f"GitHub did not answer while Factorial read {repo}: {as_sentence(_failure(exc))}",
            hint=hint,
        ) from exc
    # GitHub's create would define a missing label itself, and tools never create labels.
    if missing := [label for label in labels if label not in defined]:
        raise Conflict(
            f"These labels are not defined in {repo}: {', '.join(missing)}.",
            hint="Create them on the project page.",
        )
    # GitHub drops an assignee it cannot assign without an error, and the issue would wait for
    # ever on `ticket_has_assignee()`.
    if not assignable:
        raise Invalid(
            f"{assignee} cannot be assigned issues in {repo}.",
            hint="Name someone with access to the repository.",
        )


async def _open(
    session: AsyncSession,
    writer: IssueWriter | None,
    project: Project,
    request: Event,
    *,
    title: str,
    body: str,
    actor: Actor,
    bot_login: str,
    recovering: bool,
) -> Event:
    """Answer `request`: adopt the issue a lost reply left behind, or open the issue now."""
    if writer is None:
        message, hint = await _missing_writer(session, project)
        raise Unavailable(message, hint=hint)
    repo = str(request.details["repo"])
    assignee = str(request.details["assignee"])
    labels = [str(label) for label in request.details["labels"]]
    marker = _marker(request.id)
    hint = _RETRY_HINT if "idempotency_key" in request.details else None
    if recovering:
        found = await _recover(writer, request, repo, marker, bot_login=bot_login, hint=hint)
        if found is not None:
            return await _record(session, project, request, repo, found, recovered=True)
        # The preflight ran when the request was recorded, maybe long before this retry.
        _refuse_paused(project)
        await _refuse_unwritable(writer, repo, labels, assignee, hint=hint)
    try:
        async with asyncio.timeout(_GITHUB_TIMEOUT):
            issue = await writer.create(
                repo,
                title=title,
                body=_issue_body(body, actor.by, marker),
                labels=labels,
                assignees=[assignee],
            )
    except RequestFailed as exc:
        raise UpstreamFailed(
            f"GitHub refused to open the issue: {as_sentence(refusal_reason(exc))}", hint=hint
        ) from exc
    except GitLabError as exc:
        raise UpstreamFailed(
            f"GitLab refused to open the issue: {as_sentence(str(exc))}", hint=hint
        ) from exc
    except _NO_ANSWER as exc:
        # A timeout or a dropped connection may come after GitHub opened the issue, so the
        # request stays unanswered for a keyed retry to recover.
        log.warning("no answer from GitHub opening an issue in %s", repo, exc_info=True)
        raise UpstreamFailed(
            f"GitHub did not answer while Factorial opened the issue: {as_sentence(_failure(exc))}",
            hint=hint,
        ) from exc
    return await _record(session, project, request, repo, issue, recovered=False)


async def _recover(
    writer: IssueWriter,
    request: Event,
    repo: str,
    marker: str,
    *,
    bot_login: str,
    hint: str | None,
) -> IssueState | None:
    """The issue an earlier attempt opened before its reply was lost, if GitHub has it.

    The creator filter keeps out issues a person opened. The marker's HMAC keeps out an agent's
    issues, which carry the same App as their creator. Nothing else is compared: the projector
    replaces assignees and anyone may retitle the issue, and a retry that judged the issue by
    either would open a second one.
    """
    if not bot_login.strip():
        raise Unavailable(
            f"An earlier call with this key may have opened an issue in {repo}, and Factorial "
            f"cannot look for it. Its body ends with {marker_line(marker)}.",
            hint="Look for it on GitHub. If it is not there, call again with a new "
            "idempotency_key. A person saves the project's GitHub connection again on Ticket "
            "source, so Factorial knows its bot login and can look.",
        )
    try:
        async with asyncio.timeout(_GITHUB_TIMEOUT):
            return await writer.find_marked(
                repo, marker, creator=bot_login, since=request.created_at - _RECOVERY_SLACK
            )
    except MarkerUnconfirmed as exc:
        raise Conflict(
            "Factorial cannot confirm whether the issue was opened. Try again later with the same "
            "idempotency_key.",
            hint=f"If the issue exists, its body ends with {marker_line(marker)}.",
        ) from exc
    except RequestFailed as exc:
        raise UpstreamFailed(
            "Factorial cannot look for the issue an earlier call opened: "
            f"{as_sentence(refusal_reason(exc))}",
            hint=hint,
        ) from exc
    except GitLabError as exc:
        raise UpstreamFailed(
            f"Factorial cannot look for the issue an earlier call opened: {as_sentence(str(exc))}",
            hint=hint,
        ) from exc
    except _NO_ANSWER as exc:
        log.warning(
            "no answer from GitHub recovering request %d in %s", request.id, repo, exc_info=True
        )
        raise UpstreamFailed(
            "GitHub did not answer while Factorial looked for the issue an earlier call opened: "
            f"{as_sentence(_failure(exc))}",
            hint=hint,
        ) from exc


async def _record(
    session: AsyncSession,
    project: Project,
    request: Event,
    repo: str,
    issue: IssueState,
    *,
    recovered: bool,
) -> Event:
    return await append(
        session,
        type=EventType.ISSUE_CREATED,
        project_id=project.id,
        request_id=request.id,
        repo=repo,
        number=issue.number,
        labels=sorted(issue.labels),
        assignees=list(issue.assignees),
        recovered=recovered,
    )


async def _report(
    session: AsyncSession,
    project: Project,
    request: Event,
    answer: Event,
    *,
    replayed: bool,
    unmet: tuple[str, ...],
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> CreatedTicket:
    repo = str(answer.details["repo"])
    number = int(answer.details["number"])
    source = await ticket_source(session, project.id)
    if source is None:
        raise Conflict(
            f"{repo}#{number} is open, but project {project.name} no longer has a ticket source.",
            hint="A person adds one on the project page.",
        )
    await wake_fleet(session, project_id=project.id, repo=repo)
    await session.commit()
    ticket = await await_admission(session, source.id, str(number))
    pending: Pending | None = None
    pending_error: str | None = None
    if ticket is None:
        # The issue is open and recorded. Failing the call now would send a caller without a
        # key to retry into a second issue.
        try:
            pending = await pending_issue(session, project, source, number, state_source, bot_login)
        except Exception as exc:
            log.warning("cannot say why %s#%d waits for admission", repo, number, exc_info=True)
            pending_error = _failure(exc)
    return CreatedTicket(
        repo=repo,
        number=number,
        url=_issue_url(project, source, number),
        process=str(request.details["process"]),
        labels_set=tuple(str(label) for label in answer.details["labels"]),
        assignee=str(request.details["assignee"]),
        replayed=replayed,
        ticket=ticket,
        pending=pending,
        pending_error=pending_error,
        unsatisfiable=unmet,
    )


async def _missing_writer(session: AsyncSession, project: Project) -> tuple[str, str]:
    source = await ticket_source(session, project.id)
    if source is not None and source.kind == TicketSourceKind.GITLAB:
        return (
            "GitLab is not configured for this project, so Factorial cannot open issues.",
            "Set the GitLab URL and save GITLAB_TOKEN on the project.",
        )
    return _NO_GITHUB, _NO_GITHUB_HINT


def _issue_url(project: Project, source, number: int) -> str:
    if source.kind == TicketSourceKind.GITLAB:
        return gitlab_issue_url(project.settings, project.github_repo, number)
    return f"https://github.com/{project.github_repo}/issues/{number}"


async def _keyed_request(session: AsyncSession, project_id: int, by: str, key: str) -> Event | None:
    return await session.scalar(
        select(Event)
        .where(
            Event.project_id == project_id,
            Event.type == EventType.ISSUE_CREATE_REQUESTED,
            Event.details["requested_by"].astext == by,
            Event.details["idempotency_key"].astext == key,
        )
        .order_by(Event.id.desc())
        .limit(1)
    )


async def _answer(session: AsyncSession, project_id: int, request_id: int) -> Event | None:
    return await session.scalar(
        select(Event)
        .where(
            Event.project_id == project_id,
            Event.type == EventType.ISSUE_CREATED,
            Event.details["request_id"].astext == str(request_id),
        )
        .limit(1)
    )


def _failure(exc: Exception) -> str:
    return refusal_reason(exc) if isinstance(exc, RequestFailed) else type(exc).__name__


def _args_digest(*, title: str, body: str, assignee: str, process: str | None) -> str:
    canonical = json.dumps(
        {"title": title, "body": body, "assignee": assignee, "process": process},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _lock_key(*parts: object) -> int:
    """A signed 64-bit advisory-lock key, hashed the way `worker/singleton.py` hashes its own."""
    digest = hashlib.sha256(json.dumps(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _marker(request_id: int) -> str:
    """`factorial-request:<id>:<hmac16>`. Keyed from the API token like the other derived keys
    (decision 12), so only the orchestrator can mint one for a given request."""
    key = derive_key(get_settings().api_token.get_secret_value(), _MARKER_KEY)
    tag = hmac.new(key, str(request_id).encode(), hashlib.sha256).hexdigest()[:16]
    return f"{MARKER_PREFIX}{request_id}:{tag}"


def _issue_body(body: str, by: str, marker: str) -> str:
    footer = (
        f"---\nRequested by `{_FOOTER_UNSAFE.sub('', by)}` through the Factorial MCP server\n"
        f"{marker_line(marker)}"
    )
    text = body.rstrip()
    # The blank line keeps `---` a rule: directly under text, it would make that text a heading.
    return f"{text}\n\n{footer}\n" if text else f"{footer}\n"
