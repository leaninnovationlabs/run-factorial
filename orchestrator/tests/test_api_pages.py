"""A step's declared files over HTTP (tech-stack decision 58): the link the outputs answer hands
out, and the route outside `/api` that serves a file inert to whoever holds a link the guarded API
signed — and, when Google sign-in is on, a session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import markdown_it
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import page_blobs_dependency
from orchestrator.api.page_formats import RENDER_REVISION
from orchestrator.api.routes.auth import GoogleIdentity, google_identity_dependency
from orchestrator.api.sessions import SESSION_COOKIE, mint_session, sign_page_link
from orchestrator.db.models import (
    AppUser,
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.github.contents import ContentsNotFound
from orchestrator.settings import AuthSettings, Settings
from runtime.contract import PAGE_MAX_BYTES
from tests.api_harness import (
    TOKEN,
    blobs_for,
    build_app,
    client_for,
    make_settings,
    seed_project,
    seed_ticket,
)
from tests.members import ensure_member
from tests.reconcile_world import _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

BLOB = "b" * 40
COMMIT = "c" * 40
DASHBOARD = "https://factorial.example"
SUMMARY = "specs/12-widget/summary.html"
#: A file as the worker records it in a run's footprint (`run_outputs_observed`).
PUBLISHED: dict[str, Any] = {"path": SUMMARY, "blob": BLOB, "size": 13, "changed": True}
CSP = (
    "sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
    "style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self' https://factorial.example"
)
ETAG = f'"{BLOB}-r{RENDER_REVISION}-{markdown_it.__version__}"'


class _Blobs:
    def __init__(self, body: bytes | Exception = b"<h1>Plan</h1>") -> None:
        self.body = body
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, repo: str, sha: str) -> bytes:
        self.calls.append((repo, sha))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _settings(cors: tuple[str, ...] = ()) -> Settings:
    settings = make_settings(auth=AuthSettings(external_url=DASHBOARD))
    return settings.model_copy(update={"cors_origins": list(cors)})


def _google() -> Settings:
    return make_settings(
        auth=AuthSettings(
            google_client_id="cid-123.apps.googleusercontent.com",
            google_client_secret=SecretStr("csecret"),
            allowed_domains=["leaninnovationlabs.com"],
            external_url=DASHBOARD,
        )
    )


def _tampered(text: str) -> str:
    return text[:-1] + ("0" if text[-1] != "0" else "1")


READER = "operator@leaninnovationlabs.com"


def _session(now: datetime | None = None, *, email: str = READER) -> str:
    return mint_session(
        email,
        verified=True, api_token=TOKEN, now=now or datetime.now(UTC), max_age=3600
    )


def _app(session: AsyncSession, blobs: _Blobs | None, settings: Settings | None = None) -> FastAPI:
    app = build_app(settings or _settings(), session)
    app.dependency_overrides[page_blobs_dependency] = lambda: blobs_for(blobs)
    return app


def _reader(app: FastAPI) -> AsyncClient:
    """A browser signed in to the dashboard, as every reader of a page is: the link alone reads
    nothing (design.md §7.15)."""
    client = client_for(app)
    client.cookies.set(SESSION_COOKIE, _session())
    return client


async def _paged_run(
    session: AsyncSession,
    seen: dict[str, Any] | None = PUBLISHED,
    *,
    preview: dict[str, Any] | None = None,
) -> StepRun:
    """A plan run whose step declares the file `seen` names, as its template, and whose recorded
    footprint holds `seen`: nothing recorded when None. `READER` is a member of its
    organization."""
    project = await seed_project(session)
    await ensure_member(session, READER, organization_id=project.organization_id)
    ticket = await seed_ticket(
        session, project, step="spec_gate", status=TicketStatus.WAITING_GATE, external_key="12"
    )
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    declared = (seen or PUBLISHED)["path"].replace("12-widget", "{ticket}-{slug}")
    raw["steps"][0]["preview"] = (
        preview if preview is not None else {"files": [{"path": declared, "label": "Summary"}]}
    )
    version = (
        await session.execute(
            select(ProcessVersion).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    version.content = raw
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=StepRunStatus.SUCCEEDED,
        runtime_run_id="run-1",
    )
    session.add(run)
    await session.flush()
    if seen is not None:
        await append(
            session,
            type=EventType.RUN_OUTPUTS_OBSERVED,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=run.id,
            step="plan",
            attempt=1,
            status="observed",
            branch="spec/12-widget",
            commit=COMMIT,
            start="m0",
            start_at_spawn=False,
            slug="widget",
            cut_off=False,
            files=[seen],
        )
    return run


async def _repo(session: AsyncSession, run: StepRun) -> str:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    project = await session.get(Project, ticket.project_id)
    assert project is not None
    return project.github_repo


def _signed(run: StepRun, blob: str = BLOB, now: datetime | None = None) -> str:
    expires, signature = sign_page_link(run.id, blob, api_token=TOKEN, now=now or datetime.now(UTC))
    return f"/pages/{run.id}?blob={blob}&expires={expires}&signature={signature}"


# --------------------------------------------------------------------------- the outputs answer


async def _entry(client: Any, run: StepRun, auth: dict[str, str]) -> dict[str, Any]:
    answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
    (entry,) = answer["entries"]
    return entry


async def test_a_declared_file_is_served_inert_through_the_link_the_outputs_hand_out(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _paged_run(async_session)
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        entry = await _entry(client, run, auth)
        served = await client.get(entry["url"])  # no Authorization header

    repo = await _repo(async_session, run)
    assert entry["github_url"] == f"https://github.com/{repo}/blob/{COMMIT}/{SUMMARY}"
    assert served.status_code == 200
    assert served.content == b"<h1>Plan</h1>"
    assert served.headers["content-type"] == "text/html; charset=utf-8"
    assert served.headers["content-security-policy"] == CSP
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["referrer-policy"] == "no-referrer"
    assert served.headers["cache-control"] == "private, no-cache"
    assert served.headers["etag"] == ETAG
    assert blobs.calls == [(repo, BLOB)]


async def test_the_preview_answer_carries_no_page(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _paged_run(
        async_session,
        preview={"page": {"branch": "pages/{ticket}-{slug}", "path": "summary.html"}},
    )

    async with _reader(_app(async_session, _Blobs())) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/preview", headers=auth)).json()

    assert "page" not in answer


async def test_a_step_pinned_with_only_a_retired_page_has_no_preview(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """It once showed its page and nothing else, so it shows nothing now, and no outside producer
    publishes for it: it never waited for one."""
    run = await _paged_run(
        async_session,
        preview={"page": {"branch": "pages/{ticket}-{slug}", "path": "summary.html"}},
    )

    async with _reader(_app(async_session, _Blobs())) as client:
        answer = await client.put(
            f"/api/step-runs/{run.id}/preview",
            json={"preview_url": "https://preview.example/1"},
            headers=auth,
        )

    assert answer.status_code == 409
    assert "no enabled preview" in answer.json()["detail"]


async def test_a_retired_page_beside_declared_files_leaves_the_files_shown(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _paged_run(
        async_session,
        preview={
            "page": {"branch": "pages/{ticket}-{slug}", "path": "summary.html"},
            "files": [{"path": "specs/{ticket}-{slug}/summary.html", "label": "Summary"}],
        },
    )

    async with _reader(_app(async_session, _Blobs())) as client:
        entry = await _entry(client, run, auth)

    assert (entry["label"], entry["status"]) == ("Summary", "published")


# --------------------------------------------------------------------------- the page route


async def test_a_matching_etag_answers_304_without_reading_github(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = await client.get(_signed(run), headers={"If-None-Match": ETAG})

    assert answer.status_code == 304
    assert answer.headers["content-security-policy"] == CSP
    assert blobs.calls == []


async def test_a_bad_or_expired_link_is_refused_without_reading_github(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)
    other = await _paged_run(async_session)
    blobs = _Blobs()
    tampered = _tampered(_signed(run))
    stale = _signed(run, now=datetime.now(UTC) - timedelta(hours=3))
    elsewhere = _signed(other).replace(f"/pages/{other.id}?", f"/pages/{run.id}?")

    async with _reader(_app(async_session, blobs)) as client:
        answers = [await client.get(link) for link in (tampered, stale, elsewhere)]

    assert [answer.status_code for answer in answers] == [403, 403, 403]
    assert blobs.calls == []


async def test_a_link_alone_is_refused_without_reading_github(
    async_session: AsyncSession,
) -> None:
    """A copied link must not read the page for someone who never signed in, nor for a session
    that has lapsed or that this deployment did not mint; a cached copy earns no 304 either. No
    SSO is configured: every deployment signs its readers in (design.md §7.19)."""
    run = await _paged_run(async_session)
    blobs = _Blobs()
    lapsed = _session(now=datetime.now(UTC) - timedelta(hours=2))
    forged = _tampered(_session())

    async with client_for(_app(async_session, blobs)) as client:
        bare = await client.get(_signed(run))
        cached = await client.get(_signed(run), headers={"If-None-Match": ETAG})
        refused = []
        for cookie in (lapsed, forged):
            client.cookies.set(SESSION_COOKIE, cookie)
            refused.append(await client.get(_signed(run)))

    assert [a.status_code for a in (bare, cached, *refused)] == [403, 403, 403, 403]
    assert "Sign in" in bare.json()["detail"]
    assert blobs.calls == []


NAVIGATION = {"Sec-Fetch-Dest": "document"}


async def _signs_in() -> GoogleIdentity:
    async def exchange(code: str, redirect_uri: str) -> tuple[str, bool]:
        return "operator@leaninnovationlabs.com", True

    return exchange


async def test_a_browser_opening_the_link_is_sent_to_sign_in(
    async_session: AsyncSession,
) -> None:
    """A new tab or a pasted link lands on the dashboard's sign-in screen, never on a refusal in
    JSON, and the screen is told to come back to the link — which `/auth/login` accepts."""
    run = await _paged_run(async_session)
    blobs = _Blobs()
    lapsed = _session(now=datetime.now(UTC) - timedelta(hours=2))
    app = _app(async_session, blobs, _google())
    app.dependency_overrides[google_identity_dependency] = _signs_in

    async with client_for(app, base_url=DASHBOARD) as client:
        bare = await client.get(_signed(run), headers=NAVIGATION)
        client.cookies.set(SESSION_COOKIE, lapsed)
        expired = await client.get(_signed(run), headers=NAVIGATION)
        client.cookies.delete(SESSION_COOKIE)
        assert [bare.status_code, expired.status_code] == [303, 303], bare.text
        location = urlsplit(bare.headers["location"])
        (back,) = parse_qs(location.query)["next"]
        login = await client.get("/auth/login", params={"next": back})
        state = login.cookies.get("sf_oauth_state")
        returned = await client.get("/auth/callback", params={"code": "c", "state": state})

    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{DASHBOARD}/sign-in"
    assert back == _signed(run)
    assert returned.headers["location"] == back
    assert blobs.calls == []


async def test_a_frame_or_a_bad_link_is_refused_not_redirected(
    async_session: AsyncSession,
) -> None:
    """A frame's sandbox runs no script, so the sign-in screen would draw nothing in it; and a
    link that is not ours sends nobody to sign in on its behalf."""
    run = await _paged_run(async_session)

    async with client_for(_app(async_session, _Blobs(), _google())) as client:
        framed = await client.get(_signed(run), headers={"Sec-Fetch-Dest": "iframe"})
        forged = await client.get(_tampered(_signed(run)), headers=NAVIGATION)

    assert [framed.status_code, forged.status_code] == [403, 403]
    assert "Sign in" in framed.json()["detail"]
    assert "invalid or has expired" in forged.json()["detail"]


async def test_a_signed_in_browser_reads_the_page_by_its_link(
    async_session: AsyncSession,
) -> None:
    """The session is added to the link, never put in its place."""
    run = await _paged_run(async_session)

    async with client_for(_app(async_session, _Blobs(), _google())) as client:
        client.cookies.set(SESSION_COOKIE, _session())
        served = await client.get(_signed(run))
        tampered = await client.get(_tampered(_signed(run)))

    assert served.status_code == 200
    assert served.content == b"<h1>Plan</h1>"
    assert tampered.status_code == 403


async def test_a_link_reads_nothing_for_someone_outside_the_organization(
    async_session: AsyncSession,
) -> None:
    """Membership is read at every open, not when the link was signed: a link travels, and
    outlives a removal."""
    run = await _paged_run(async_session)
    blobs = _Blobs()
    stranger = "stranger@leaninnovationlabs.com"
    await async_session.execute(
        insert(AppUser).values(email=stranger, name="stranger").on_conflict_do_nothing()
    )

    async with client_for(_app(async_session, blobs)) as client:
        client.cookies.set(SESSION_COOKIE, _session(email=stranger))
        answer = await client.get(_signed(run))

    assert answer.status_code == 404
    assert blobs.calls == []


async def test_a_link_serves_only_the_blob_it_was_signed_for(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)

    async with _reader(_app(async_session, _Blobs())) as client:
        answer = await client.get(_signed(run, blob="d" * 40))

    assert answer.status_code == 404


async def test_a_page_github_lost_is_404_and_an_outage_is_502(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)

    async with _reader(_app(async_session, _Blobs(ContentsNotFound("gone")))) as client:
        lost = await client.get(_signed(run))
    async with _reader(_app(async_session, _Blobs(RuntimeError("down")))) as client:
        outage = await client.get(_signed(run))

    assert (lost.status_code, outage.status_code) == (404, 502)


async def test_a_page_over_the_limit_is_neither_framed_nor_served(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _paged_run(async_session, PUBLISHED | {"size": PAGE_MAX_BYTES + 1})
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        entry = await _entry(client, run, auth)
        served = await client.get(_signed(run))

    assert (entry["too_large"], entry["url"]) == (True, None)
    assert entry["github_url"] is not None
    assert served.status_code == 413
    assert blobs.calls == []


async def test_a_page_with_no_recorded_size_is_still_capped(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session, PUBLISHED | {"size": None})

    async with _reader(_app(async_session, _Blobs(b"x" * (PAGE_MAX_BYTES + 1)))) as client:
        answer = await client.get(_signed(run))

    assert answer.status_code == 413


async def test_no_github_is_503(async_session: AsyncSession) -> None:
    run = await _paged_run(async_session)

    async with _reader(_app(async_session, None)) as client:
        answer = await client.get(_signed(run))

    assert answer.status_code == 503


async def test_frame_ancestors_name_every_dashboard_origin(async_session: AsyncSession) -> None:
    """A dashboard served apart from the API (Vite on :5173 in development) must still be allowed
    to frame the page; default ports are written without the port."""
    run = await _paged_run(async_session)
    settings = _settings(cors=("http://localhost:5173", "https://dash.example:443/app"))

    async with _reader(_app(async_session, _Blobs(), settings)) as client:
        answer = await client.get(_signed(run))

    assert answer.headers["content-security-policy"].endswith(
        "frame-ancestors 'self' http://localhost:5173 https://dash.example "
        "https://factorial.example"
    )


async def test_one_malformed_dashboard_origin_costs_only_itself(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)
    settings = _settings(cors=("http://localhost:abc", "http://[::1", "http://localhost:5173"))

    async with _reader(_app(async_session, _Blobs(), settings)) as client:
        answer = await client.get(_signed(run))

    assert answer.status_code == 200
    assert answer.headers["content-security-policy"].endswith(
        "frame-ancestors 'self' http://localhost:5173 https://factorial.example"
    )


async def test_an_ipv6_dashboard_origin_keeps_its_brackets(async_session: AsyncSession) -> None:
    run = await _paged_run(async_session)
    settings = _settings(cors=("http://[::1]:5173",))

    async with _reader(_app(async_session, _Blobs(), settings)) as client:
        answer = await client.get(_signed(run))

    assert answer.headers["content-security-policy"].endswith(
        "frame-ancestors 'self' http://[::1]:5173 https://factorial.example"
    )


# -------------------------------------------------------------- what each type is served as


async def test_a_markdown_page_is_rendered_to_html_inside_the_same_envelope(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session, PUBLISHED | {"path": "summary.md"})

    async with _reader(_app(async_session, _Blobs(b"# Plan\n\n<script>x</script>\n"))) as client:
        served = await client.get(_signed(run))

    assert served.status_code == 200
    assert served.headers["content-type"] == "text/html; charset=utf-8"
    assert "<main><h1>Plan</h1>\n<p>&lt;script&gt;x&lt;/script&gt;</p>\n</main>" in served.text
    assert served.headers["content-security-policy"] == CSP
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["etag"] == ETAG


@pytest.mark.parametrize("path", ["notes.txt", "diagram.svg", "summary"])
async def test_a_type_without_a_renderer_is_served_as_plain_text(
    async_session: AsyncSession, path: str
) -> None:
    body = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    run = await _paged_run(async_session, PUBLISHED | {"path": path})

    async with _reader(_app(async_session, _Blobs(body))) as client:
        served = await client.get(_signed(run))

    assert served.status_code == 200
    assert served.content == body
    assert served.headers["content-type"] == "text/plain; charset=utf-8"
    assert served.headers["content-security-policy"] == CSP
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_a_text_page_that_is_not_utf8_is_refused_with_415(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session, PUBLISHED | {"path": "chart.png"})

    async with _reader(_app(async_session, _Blobs(b"\x89PNG\r\n\x1a\n\xff"))) as client:
        served = await client.get(_signed(run))

    assert served.status_code == 415
    assert served.json()["detail"] == "The page is not UTF-8 text. View it on GitHub."


async def test_an_answer_cached_before_the_render_revision_is_served_again(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session)
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = await client.get(_signed(run), headers={"If-None-Match": f'"{BLOB}"'})

    assert answer.status_code == 200
    assert answer.headers["etag"] == ETAG
    assert len(blobs.calls) == 1


async def test_a_markdown_page_that_would_render_too_large_is_refused_with_413(
    async_session: AsyncSession,
) -> None:
    run = await _paged_run(async_session, PUBLISHED | {"path": "summary.md"})

    async with _reader(_app(async_session, _Blobs(b"- a\n" * 60_000))) as client:
        served = await client.get(_signed(run))

    assert served.status_code == 413
    assert served.json()["detail"] == "The page renders too large to show. View it on GitHub."
