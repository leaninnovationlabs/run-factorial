"""GitHub webhook receiver: a wake-up signal and nothing else (design.md §2.1).

Two URLs, because a project brings its own GitHub connection. `/github/apps/{app_id}` is the one
URL of a GitHub App that several projects may share; `/github/projects/{project_id}` is a
token-mode project's own. Each project's webhook secret decides whether it owns a delivery, so
a signature that verifies for one project never reaches another. A verified delivery stamps
`ticket_source.webhook_verified_at`, and a candidate whose stored secret fails stamps
`webhook_failed_at` at most once per `FAILED_STAMP_GAP`; both overwrite in place, and a delivery
nobody verifies records no event. Anyone can POST here, so a project URL answers a project that
is missing or unconnected with the same 401 as a bad signature, and a candidate with no secret
is never stamped failed.

It verifies the signature, records that a delivery arrived, and issues `NOTIFY` so the worker
polls now instead of on its next tick. It must never create or modify a ticket, step run, label
or PR: observed state comes from polling GitHub, which is what makes a dropped, duplicated or
reordered delivery harmless. The payload is read for what to wake, and for the facts the audit
row shows (`_facts`) — never a title, a body or a comment's text. Every project on the repo,
matched ignoring case, records the delivery. One that names an open ticket, by its issue in the
project's intake repo or by a branch named `<prefix>/<issue>-…` in its artifact repo, wakes that
ticket alone, which the worker serves at once and merges with any wake for it still pending. An
issue or issue comment that names no open ticket, and whose action can make it admissible
(`_ADMITTING_ACTIONS`), asks the worker to judge that one issue for admission, also at once,
since only admission can act on it. Any other delivery wakes the fleet pass, which
`worker.min_pass_gap_seconds` spaces. The App's ping,
installation and authorization deliveries name no repository, and are accepted with neither a
record nor a wake; on the App's URL they still verify, and so stamp each project they verify.

`/gitlab` keeps the same contract for GitLab: the same body cap, and every project whose ticket
source is the delivery's repo checks `X-Gitlab-Token` against its own secret. An issue event, or
a comment on an issue, that names an open ticket wakes that ticket alone, as GitHub's does, and
one that names no open ticket asks admission about that issue; any other wakes the fleet pass,
which leases a long-exited ticket only every `EXITED_PASS_INTERVAL`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import SecretStr
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute

from orchestrator.api.deps import SessionDep
from orchestrator.db.models import (
    Project,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
    intake_column,
)
from orchestrator.events import EventType, append, wake_admission, wake_fleet, wake_ticket
from orchestrator.github.connection import WEBHOOK_SECRET, connection_for_project
from orchestrator.secrets import store

router = APIRouter(tags=["webhooks"])

_SIGNATURE_PREFIX = "sha256="
#: GitHub sends `github_app_authorization` to every App webhook, subscribed or not.
_NO_REPOSITORY = frozenset({"ping", "github_app_authorization"})
#: GitHub's own cap is 25 MB; nothing we act on is remotely that big. Enforced before the body is
#: buffered: the signature needs the whole body, so the cap is all an anonymous sender meets.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: An anonymous loop of bad deliveries rewrites the stamp, and locks the row, once per gap.
FAILED_STAMP_GAP = timedelta(minutes=1)


def _verify(secret: str, body: bytes, signature: str | None) -> None:
    if not signature or not signature.startswith(_SIGNATURE_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Hub-Signature-256",
        )
    expected = _SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Bytes: compare_digest raises on a str that is not ASCII, and the header is the sender's.
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad webhook signature",
        )


#: factorial's branches carry the issue number first after their prefix: `spec/149-…`,
#: `impl/149-…`. A branch of any other shape names no ticket.
_TICKET_BRANCH = re.compile(r"^[^/]+/(\d+)(?:-|$)")
#: Deliveries about an issue, numbered in the repo the issues come from. Every other event that
#: names a ticket does it by a branch, which lives in the artifact repo.
_ISSUE_EVENTS = frozenset({"issues", "issue_comment"})
#: The actions that can make an issue that is no ticket admissible. Admission reads open issues
#: only, so a close is never worth a read, and reading a deleted or transferred issue fails.
_ADMITTING_ACTIONS = {
    "issues": frozenset(
        {"opened", "reopened", "edited", "labeled", "unlabeled", "assigned", "unassigned"}
    ),
    "issue_comment": frozenset({"created", "edited"}),
}
#: GitHub bounds what these fields hold; the cap only keeps a malformed one off the audit row.
_FACT_MAX = 200


def _names_no_repository(event: str | None) -> bool:
    return event in _NO_REPOSITORY or (event or "").startswith("installation")


def _payload(body: bytes) -> dict[str, Any]:
    try:
        payload: Any = json.loads(body)
    except (ValueError, RecursionError):
        # Deeply nested input raises RecursionError, and this is reached before any signature.
        return {}
    return payload if isinstance(payload, dict) else {}


def _field(payload: dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _branch(event: str | None, payload: dict[str, Any]) -> str | None:
    """The branch a delivery is about, or None. A push to a tag is about no branch."""
    if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        branch = _field(payload, "pull_request", "head", "ref")
    elif event == "push":
        ref = payload.get("ref")
        branch = (
            ref.removeprefix("refs/heads/")
            if isinstance(ref, str) and ref.startswith("refs/heads/")
            else None
        )
    elif event == "check_suite":
        branch = _field(payload, "check_suite", "head_branch")
    else:
        return None
    return branch if isinstance(branch, str) else None


def _issue_named(event: str | None, payload: dict[str, Any]) -> str | None:
    """The issue number a delivery is about, as a ticket's `external_key`, or None."""
    if event in _ISSUE_EVENTS:
        issue = payload.get("issue")
        # A comment on a pull request arrives as an issue_comment on the PR's own number.
        if isinstance(issue, dict) and "pull_request" not in issue:
            number = issue.get("number")
            return str(number) if type(number) is int else None
        return None
    branch = _branch(event, payload)
    match = _TICKET_BRANCH.match(branch) if branch is not None else None
    return match.group(1) if match else None


def _may_admit(event: str | None, payload: dict[str, Any]) -> bool:
    return payload.get("action") in _ADMITTING_ACTIONS.get(event or "", frozenset())


def _facts(event: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """What the audit row says about a delivery, beyond its event: GitHub's metadata only."""

    def text(*path: str) -> str | None:
        value = _field(payload, *path)
        return value[:_FACT_MAX] if isinstance(value, str) and value else None

    issue = payload.get("issue")
    on_pull_request = isinstance(issue, dict) and "pull_request" in issue
    number = _field(payload, "pull_request", "number") or _field(payload, "issue", "number")
    merged = _field(payload, "pull_request", "merged")
    facts: dict[str, Any] = {
        "action": text("action"),
        "sender": text("sender", "login"),
        "number": number if type(number) is int else None,
        "on_pull_request": True if on_pull_request else None,
        "branch": (_branch(event, payload) or "")[:_FACT_MAX] or None,
        "merged": True if merged is True and payload.get("action") == "closed" else None,
        "label": text("label", "name"),
        "review_state": text("review", "state"),
        "conclusion": text("check_suite", "conclusion"),
    }
    return {key: value for key, value in facts.items() if value is not None}


def _repo_full_name(payload: dict[str, Any]) -> str | None:
    """The repository the delivery comes from; the project is found from it."""
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    full_name = repository.get("full_name")
    return full_name if isinstance(full_name, str) else None


def _too_large() -> HTTPException:
    return HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="payload too large")


async def _body(request: Request) -> bytes:
    """The body, refused once it passes the cap: on the declared length before any of it is
    read, and on the bytes read so far when no length is declared."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise _too_large()
    return bytes(body)


def _has(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value())


def _verifies(secret: SecretStr | None, body: bytes, signature: str | None) -> bool:
    """False for an unset or empty secret, which verifies nothing."""
    if secret is None or not secret.get_secret_value():
        return False
    try:
        _verify(secret.get_secret_value(), body, signature)
    except HTTPException:
        return False
    return True


async def _stamp(
    session: SessionDep,
    column: InstrumentedAttribute[datetime | None],
    project_ids: Collection[int],
) -> None:
    """Overwrite the column on each project's existing ticket source; a project with none has
    nowhere to show it, and a delivery never creates one. A failure stamp younger than
    `FAILED_STAMP_GAP` is kept."""
    if not project_ids:
        return
    now = datetime.now(UTC)
    statement = update(TicketSource).where(TicketSource.project_id.in_(project_ids))
    if column is TicketSource.webhook_failed_at:
        statement = statement.where(or_(column.is_(None), column < now - FAILED_STAMP_GAP))
    await session.execute(statement.values({column.key: now}))


def _no_verified(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


@router.post("/github/projects/{project_id}", status_code=status.HTTP_202_ACCEPTED)
async def github_project_webhook(
    project_id: int,
    request: Request,
    session: SessionDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> Response:
    """A token-mode project's own URL: the signature is checked against that project's secret
    before the payload is parsed."""
    project = await session.get(Project, project_id)
    connection = (
        await connection_for_project(session, project, with_webhook_secret=True)
        if project is not None
        else None
    )
    if connection is None or not _has(connection.webhook_secret):
        raise _no_verified("bad webhook signature")

    body = await _body(request)
    # Signature first: unverified input may touch only the failed-delivery stamp.
    if not _verifies(connection.webhook_secret, body, x_hub_signature_256):
        await _stamp(session, TicketSource.webhook_failed_at, [project_id])
        await session.commit()
        raise _no_verified("bad webhook signature")
    await _stamp(session, TicketSource.webhook_verified_at, [project_id])
    return await _deliver(session, body, x_github_event, x_github_delivery, {project_id})


@router.post("/github/apps/{app_id}", status_code=status.HTTP_202_ACCEPTED)
async def github_app_webhook(
    app_id: str,
    request: Request,
    session: SessionDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> Response:
    """An App's one URL, shared by every project that uses it. The payload is read only to pick
    which projects might own the delivery; each candidate's own secret decides, so two projects
    on one App never see each other's deliveries."""
    body = await _body(request)
    payload = _payload(body)
    repo = _repo_full_name(payload)
    names_no_repo = _names_no_repository(x_github_event)
    if repo is None and not names_no_repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="payload has no repository.full_name"
        )

    candidates = await _app_projects(session, app_id)
    if not names_no_repo and repo is not None:
        on_repo = {row[0] for row in await _projects_for_repo(session, repo)}
        candidates = [pid for pid in candidates if pid in on_repo]

    verified: set[int] = set()
    failed: set[int] = set()
    for project_id in candidates:
        secret = await store.get(session, project_id, WEBHOOK_SECRET)
        if _has(secret):
            (verified if _verifies(secret, body, x_hub_signature_256) else failed).add(project_id)
    await _stamp(session, TicketSource.webhook_verified_at, verified)
    await _stamp(session, TicketSource.webhook_failed_at, failed)
    if candidates and not verified:
        await session.commit()
        raise _no_verified("bad webhook signature")
    return await _deliver(session, body, x_github_event, x_github_delivery, verified)


async def _app_projects(session: SessionDep, app_id: str) -> list[int]:
    github = Project.settings["github"]
    rows = await session.execute(
        select(Project.id)
        .where(github["mode"].as_string() == "app", github["app_id"].as_string() == app_id)
        .order_by(Project.id)
    )
    return [int(pid) for (pid,) in rows]


async def _deliver(
    session: SessionDep,
    body: bytes,
    event: str | None,
    delivery: str | None,
    verified: Collection[int],
) -> Response:
    """Record a verified delivery against the projects that verified it and wake the worker."""
    if _names_no_repository(event):
        await session.commit()
        return Response(status_code=status.HTTP_202_ACCEPTED)

    payload = _payload(body)
    repo = _repo_full_name(payload)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="payload has no repository.full_name"
        )

    projects = [row for row in await _projects_for_repo(session, repo) if row[0] in verified]
    if not projects:
        # Verified, but for a repo no project reacts to. Not an error worth alarming on.
        await session.commit()
        return Response(status_code=status.HTTP_202_ACCEPTED)

    key = _issue_named(event, payload)
    issue_event = event in _ISSUE_EVENTS
    facts = _facts(event, payload)
    wakes = _Wakes()
    for project_id, intake_matches, artifacts_match in projects:
        # An issue's number is a ticket's only in the repo its issues come from, and a branch's
        # only in the repo its PRs live in; the other repo's #7 is a different issue.
        names_this = intake_matches if issue_event else artifacts_match
        ticket_id = (
            await _open_ticket(session, project_id, key) if key is not None and names_this else None
        )
        admits = issue_event and names_this and _may_admit(event, payload)
        woke = wakes.add(project_id, ticket_id, key if admits else None)
        await append(
            session,
            type=EventType.WEBHOOK_RECEIVED,
            project_id=project_id,
            # Which ticket the delivery woke, if any; the ticket itself is read, never touched.
            ticket_id=ticket_id,
            repo=repo,
            github_event=event,
            delivery=delivery,
            woke=woke,
            **facts,
        )
    await wakes.send(session, repo)
    return Response(status_code=status.HTTP_202_ACCEPTED)


class _Wakes:
    """What one delivery wakes, sent together once every project has recorded it."""

    def __init__(self) -> None:
        self.tickets: list[int] = []
        self.admissions: list[tuple[int, str]] = []
        self.fleet_project: int | None = None

    def add(self, project_id: int, ticket_id: int | None, issue: str | None) -> str:
        """`issue` is the issue the delivery is about in this project's intake repo, if any."""
        if ticket_id is not None:
            self.tickets.append(ticket_id)
            return "ticket"
        if issue is not None:
            # No open ticket: a new issue, a done ticket's, or one still waiting on its entry
            # rule. Only admission can act on it, and a fleet pass would wait out its gap.
            self.admissions.append((project_id, issue))
            return "admission"
        if self.fleet_project is None:
            self.fleet_project = project_id
        return "project"

    async def send(self, session: SessionDep, repo: str) -> None:
        # Committed here rather than by the request scope, so the NOTIFY has left before the
        # 202 does. One fleet pass reconciles every project.
        for ticket_id in self.tickets:
            await wake_ticket(session, ticket_id)
        for project_id, issue in self.admissions:
            await wake_admission(session, project_id=project_id, issue=issue)
        if self.fleet_project is not None:
            await wake_fleet(session, project_id=self.fleet_project, repo=repo)
        await session.commit()


async def _open_ticket(session: SessionDep, project_id: int, key: str) -> int | None:
    """The project's ticket for this issue, unless it is done: a done ticket is never polled."""
    row = (
        await session.execute(
            select(Ticket.id).where(
                Ticket.project_id == project_id,
                Ticket.external_key == key,
                Ticket.status != TicketStatus.DONE,
            )
        )
    ).first()
    return int(row[0]) if row is not None else None


async def _projects_for_repo(session: SessionDep, repo: str) -> list[tuple[int, bool, bool]]:
    """Every project the repo feeds, as `(project_id, intake_matches, artifacts_match)`.

    Intake and artifacts can be different repos (data-model.md), and nothing stops two projects
    naming one repo. GitHub owner and repo names are case-insensitive, and the stored name is
    whatever was typed.
    """
    wanted = repo.lower()
    intake = func.lower(intake_column())
    artifacts = func.lower(Project.github_repo)
    rows = await session.execute(
        select(Project.id, intake == wanted, artifacts == wanted)
        .outerjoin(TicketSource, TicketSource.project_id == Project.id)
        .where(or_(intake == wanted, artifacts == wanted))
        .order_by(Project.id)
    )
    return [
        (int(pid), bool(intake_ok), bool(artifacts_ok)) for pid, intake_ok, artifacts_ok in rows
    ]


@router.post("/gitlab", status_code=status.HTTP_202_ACCEPTED)
async def gitlab_webhook(
    request: Request,
    session: SessionDep,
    x_gitlab_token: Annotated[str | None, Header()] = None,
    x_gitlab_event: Annotated[str | None, Header()] = None,
) -> Response:
    """Wake-up only. Polling remains authoritative, same contract as the GitHub routes. One URL
    serves every GitLab project, and projects in different organizations may name one repo, so
    each candidate's own secret decides whether the delivery is its."""
    body = await _body(request)
    payload = _payload(body)
    repo = _gitlab_path(payload)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload has no project.path_with_namespace",
        )

    candidates = await _gitlab_projects(session, repo)
    if not candidates:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    if not x_gitlab_token:
        raise _no_verified("missing X-Gitlab-Token")

    from orchestrator.gitlab.project_client import webhook_secret_for

    verified: list[int] = []
    for project in candidates:
        secret = await webhook_secret_for(session, project)
        if secret and hmac.compare_digest(x_gitlab_token.encode(), secret.encode()):
            verified.append(project.id)
    if not verified:
        raise _no_verified("bad X-Gitlab-Token")

    key = _gitlab_issue(payload)
    wakes = _Wakes()
    for project_id in verified:
        ticket_id = await _open_ticket(session, project_id, key) if key is not None else None
        await append(
            session,
            type=EventType.WEBHOOK_RECEIVED,
            project_id=project_id,
            ticket_id=ticket_id,
            repo=repo,
            gitlab_event=x_gitlab_event,
            woke=wakes.add(project_id, ticket_id, key if _gitlab_may_admit(payload) else None),
        )
    await wakes.send(session, repo)
    return Response(status_code=status.HTTP_202_ACCEPTED)


def _gitlab_issue(payload: dict[str, Any]) -> str | None:
    """The iid of the issue an issue event or a comment on an issue is about, as a ticket's
    `external_key`, or None."""
    kind = payload.get("object_kind")
    if kind == "issue":
        iid = _field(payload, "object_attributes", "iid")
    elif kind == "note" and _field(payload, "object_attributes", "noteable_type") == "Issue":
        iid = _field(payload, "issue", "iid")
    else:
        return None
    return str(iid) if type(iid) is int else None


def _gitlab_may_admit(payload: dict[str, Any]) -> bool:
    """`_may_admit` for GitLab: an issue opened, reopened or updated, or a note on one."""
    action = _field(payload, "object_attributes", "action")
    if payload.get("object_kind") == "issue":
        return action in ("open", "reopen", "update")
    return action in (None, "create", "update")


def _gitlab_path(payload: dict[str, Any]) -> str | None:
    project = payload.get("project")
    if isinstance(project, dict):
        path = project.get("path_with_namespace")
        if isinstance(path, str):
            return path
    return None


async def _gitlab_projects(session: SessionDep, repo: str) -> list[Project]:
    """Every project whose GitLab ticket source is `repo`."""
    rows = await session.execute(
        select(Project)
        .join(TicketSource, TicketSource.project_id == Project.id)
        .where(
            TicketSource.kind == TicketSourceKind.GITLAB,
            TicketSource.external_ref == repo,
        )
        .order_by(Project.id)
    )
    return list(rows.scalars())
