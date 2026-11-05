"""A step's declared files (tech-stack decisions 58 and 71), served inert.

Not under `/api`: a frame and a new tab cannot send a bearer token, so this route authenticates by
the link `GET /api/step-runs/{id}/outputs` signs (`api/sessions.py`), the way the webhook
authenticates by its HMAC. A link names one run and one blob, and lasts at most two hours. The blob
must be one the run shows, a file its step declares (`api/services/outputs.py`); a link to any
other blob reads nothing. The request must also carry the session cookie, which a frame and a new
tab do send and every dashboard reader holds, so a copied link alone reads nothing. A browser that
opens the link without one is sent to the dashboard's sign-in screen, which brings it back to the
link once it is signed in. Membership of the run's organization is checked on every read, not when
the link was signed: a link travels, and outlives a removal. A non-member gets the 404 an unknown
page gets.

What makes an agent-written page safe to answer from the API's origin, whatever its type, is the
response's CSP: `sandbox` without `allow-scripts` gives the page an opaque origin and no script,
`default-src 'none'` lets it load nothing, and `form-action 'none'` lets it send nothing (design.md
§7.15). `api/page_formats.py` decides only the body and the media type inside that envelope: HTML
and plain text are the blob's bytes, never rewritten, and Markdown is rendered to escaped HTML.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Final
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from orchestrator.access import Permission
from orchestrator.access.accounts import needs_verification, session_ended
from orchestrator.access.decision import allowed_on_run, caller_named
from orchestrator.api.deps import PageBlobsDep, SessionDep, SettingsDep
from orchestrator.api.page_formats import RENDER_TAG, NotText, RenderTooLarge, render
from orchestrator.api.services.outputs import servable_blobs
from orchestrator.api.sessions import SESSION_COOKIE, verified_session, verify_page_link
from orchestrator.db.models import Project, StepRun, Ticket, TicketSource, TicketSourceKind
from orchestrator.github.contents import ContentsNotFound
from orchestrator.settings import Settings
from runtime.contract import PAGE_MAX_BYTES

log = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
#: The dashboard route that signs a page link's reader in and returns it to the link.
SIGN_IN_PATH: Final = "/sign-in"


def _origin(url: str) -> str | None:
    """The origin `url` names, or None for an entry that is not a URL: an operator's typo in
    `cors_origins` must cost that entry, not every page answer."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    shown = f":{port}" if port and port != _DEFAULT_PORTS.get(parts.scheme) else ""
    return f"{parts.scheme}://{host}{shown}"


def page_headers(settings: Settings, blob: str) -> dict[str, str]:
    """Every page answer's headers, a 304 included. `frame-ancestors` names the dashboard's
    origins — the list `events.previews` keeps a framed URL off."""
    origins = sorted(
        {
            origin
            for url in (settings.auth.external_url, *settings.cors_origins)
            if url and (origin := _origin(url))
        }
    )
    return {
        "Content-Security-Policy": (
            "sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; "
            f"base-uri 'none'; frame-ancestors {' '.join(["'self'", *origins])}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-cache",
        "ETag": f'"{blob}-{RENDER_TAG}"',
    }


@router.get("/pages/{step_run_id}")
async def get_page(
    step_run_id: int,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    blobs: PageBlobsDep,
    blob: Annotated[str, Query(pattern=r"^[0-9a-f]{40}$")],
    expires: int,
    signature: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
) -> Response:
    token = settings.api_token.get_secret_value()
    now = datetime.now(UTC)
    if not verify_page_link(step_run_id, blob, expires, signature, api_token=token, now=now):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This page link is invalid or has expired. Open the preview again.",
        )
    signed = verified_session(request.cookies.get(SESSION_COOKIE), api_token=token, now=now)
    if (
        signed is None
        or await session_ended(
            session, email=signed.email, verified=signed.verified, epoch=signed.epoch
        )
        or needs_verification(verified=signed.verified, mail=settings.mail.enabled)
    ):
        # Only a browser opening the link is sent to sign in. A frame keeps the refusal: its
        # sandbox runs no script, so the dashboard would draw nothing there. A script gets it too.
        if request.headers.get("sec-fetch-dest") == "document":
            # Both are browser paths, so both carry the app's mount.
            root = request.scope.get("root_path", "")
            link = urlencode({"blob": blob, "expires": expires, "signature": signature})
            back = urlencode({"next": f"{root}/pages/{step_run_id}?{link}"})
            return RedirectResponse(
                f"{settings.auth.external_url.rstrip('/')}{root}{SIGN_IN_PATH}?{back}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Sign in to the dashboard to read this page."
        )
    if not await allowed_on_run(
        session, caller_named(signed.email), step_run_id, Permission.ORG_READ
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Step run {step_run_id} has no such page.")
    run = await session.get(StepRun, step_run_id)
    ticket = await session.get(Ticket, run.ticket_id) if run is not None else None
    shown = await servable_blobs(session, ticket, run) if run and ticket else {}
    seen = shown.get(blob)
    if run is None or seen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run has no such page.")
    headers = page_headers(settings, blob)
    if request.headers.get("if-none-match") == headers["ETag"]:
        # The blob is the content's own hash and the revision is the renderer's: a match is the
        # same answer, with no GitHub call.
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    size = seen.size
    if size is not None and size > PAGE_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"The page is {size} bytes, and pages over {PAGE_MAX_BYTES} bytes are not served.",
        )
    project = (
        await session.execute(
            select(Project)
            .join(Ticket, Ticket.project_id == Project.id)
            .where(Ticket.id == run.ticket_id)
        )
    ).scalar_one()
    ticket_source_id = (
        await session.execute(select(Ticket.ticket_source_id).where(Ticket.id == run.ticket_id))
    ).scalar_one()
    source = await session.get(TicketSource, ticket_source_id)
    kind = source.kind if source is not None else TicketSourceKind.GITHUB
    if kind == TicketSourceKind.GITLAB:
        from orchestrator.gitlab.contents import GitLabContents
        from orchestrator.gitlab.project_client import client_for_project

        client = await client_for_project(session, project)
        read_blob = None if client is None else GitLabContents(client).read_blob
        forge = "GitLab"
    else:
        read_blob, forge = await blobs(project), "GitHub"
    if read_blob is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{forge} is not configured for this project, so no page can be read.",
        )
    try:
        body = await read_blob(project.github_repo, blob)
    except ContentsNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"The page is no longer on {forge}."
        ) from exc
    except Exception as exc:
        log.warning(
            "reading page blob %s of %s failed", blob[:7], project.github_repo, exc_info=True
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Factorial couldn't read {forge}. Try again."
        ) from exc
    if len(body) > PAGE_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"The page is {len(body)} bytes, and pages over {PAGE_MAX_BYTES} bytes are not served.",
        )
    try:
        # Markdown is CPU work on up to PAGE_MAX_BYTES: keep it off the event loop.
        rendered = await run_in_threadpool(render, seen.path, body)
    except NotText as exc:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"The page is not UTF-8 text. View it on {forge}.",
        ) from exc
    except RenderTooLarge as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"The page renders too large to show. View it on {forge}.",
        ) from exc
    return Response(content=rendered.body, media_type=rendered.media_type, headers=headers)
