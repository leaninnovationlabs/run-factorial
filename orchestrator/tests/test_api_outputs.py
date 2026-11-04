"""A run's declared files over HTTP (tech-stack decision 71): the outputs answer lists them, and
the page route serves a declared file through its signed link and inert envelope, and nothing the
config did not declare. A ticket pinned with the retired `preview.page` shows nothing of it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import page_blobs_dependency
from orchestrator.api.sessions import SESSION_COOKIE, mint_session, sign_page_link
from orchestrator.db.models import (
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
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

COMMIT = "c" * 40
PAGE_BLOB = "b" * 40
SPEC_BLOB = "d" * 40
NOTES_BLOB = "e" * 40
PLAN_BLOB = "f" * 40
SPEC = "specs/12-widget/spec.md"
PLAN = "specs/12-widget/plan.md"
FILES: list[dict[str, str]] = [
    {"path": "specs/{ticket}-{slug}/spec.md", "label": "Spec"},
    {"path": "specs/{ticket}-{slug}/plan.md", "label": "Plan"},
]
PAGE_SEEN: dict[str, Any] = {
    "status": "published",
    "branch": "pages/12-widget",
    "path": "summary.html",
    "commit": COMMIT,
    "blob": PAGE_BLOB,
    "size": 13,
}


def _footprint(spec_size: int = 20) -> dict[str, Any]:
    return {
        "status": "observed",
        "branch": "spec/12-widget",
        "commit": COMMIT,
        "start": "m0",
        "start_at_spawn": False,
        "slug": "widget",
        "cut_off": False,
        "files": [
            {"path": SPEC, "blob": SPEC_BLOB, "size": spec_size, "changed": True},
            {"path": "notes.txt", "blob": NOTES_BLOB, "size": 5, "changed": True},
            {"path": PLAN, "blob": PLAN_BLOB, "size": 30, "changed": False},
        ],
    }


class _Blobs:
    def __init__(self, body: bytes = b"# Spec\n\nThe *outcome*.") -> None:
        self.body = body
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, repo: str, sha: str) -> bytes:
        self.calls.append((repo, sha))
        return self.body


def _app(session: AsyncSession, blobs: _Blobs | None = None) -> FastAPI:
    app = build_app(make_settings(), session)
    app.dependency_overrides[page_blobs_dependency] = lambda: blobs_for(blobs or _Blobs())
    return app


def _reader(app: FastAPI) -> AsyncClient:
    """Signed in, as every dashboard reader is: a page's link alone reads nothing."""
    client = client_for(app)
    client.cookies.set(
        SESSION_COOKIE,
        mint_session(
            "operator@example.com",
            verified=True,
            api_token=TOKEN,
            now=datetime.now(UTC),
            max_age=3600,
        ),
    )
    return client


async def _run(
    session: AsyncSession,
    *,
    preview: dict[str, Any] | None,
    page_seen: dict[str, Any] | None = PAGE_SEEN,
    footprint: dict[str, Any] | None = None,
) -> StepRun:
    project = await seed_project(session)
    await ensure_member(session, "operator@example.com", organization_id=project.organization_id)
    ticket = await seed_ticket(
        session, project, step="spec_gate", status=TicketStatus.WAITING_GATE, external_key="12"
    )
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    if preview is not None:
        raw["steps"][0]["preview"] = preview
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
    for kind, details in (
        (EventType.PREVIEW_PAGE_OBSERVED, page_seen),
        (EventType.RUN_OUTPUTS_OBSERVED, footprint),
    ):
        if details is not None:
            await append(
                session,
                type=kind,
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                step_run_id=run.id,
                step="plan",
                attempt=1,
                **details,
            )
    return run


def _filed(page: bool = True) -> dict[str, Any]:
    preview: dict[str, Any] = {"files": FILES}
    if page:
        preview["page"] = {"branch": "pages/{ticket}-{slug}", "path": "summary.html"}
    return preview


async def _repo(session: AsyncSession, run: StepRun) -> str:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    project = await session.get(Project, ticket.project_id)
    assert project is not None
    return project.github_repo


def _signed(run: StepRun, blob: str) -> str:
    expires, signature = sign_page_link(run.id, blob, api_token=TOKEN, now=datetime.now(UTC))
    return f"/pages/{run.id}?blob={blob}&expires={expires}&signature={signature}"


async def test_the_outputs_list_each_declared_file_and_nothing_of_a_retired_page(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=_footprint())

    async with _reader(_app(async_session)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()

    repo = await _repo(async_session, run)
    entries = answer["entries"]
    assert [(e["kind"], e["key"], e["label"], e["status"]) for e in entries] == [
        ("file", "specs/{ticket}-{slug}/spec.md", "Spec", "published"),
        ("file", "specs/{ticket}-{slug}/plan.md", "Plan", "published"),
    ]
    spec = entries[0]
    assert spec | {"url": None} == {
        "kind": "file",
        "key": "specs/{ticket}-{slug}/spec.md",
        "label": "Spec",
        "status": "published",
        "path": SPEC,
        "commit": COMMIT,
        "size": 20,
        "too_large": False,
        "matches": [],
        "reason": None,
        "url": None,
        "github_url": f"https://github.com/{repo}/blob/{COMMIT}/{SPEC}",
        "changed": True,
        "group": None,
        "group_total": None,
        "group_url": None,
    }
    assert spec["url"].startswith(f"/pages/{run.id}?blob={SPEC_BLOB}&")
    assert entries[1]["changed"] is False
    assert answer["cut_off"] is False


async def test_a_declared_file_is_served_through_its_own_link(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=_footprint())
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
        served = await client.get(answer["entries"][0]["url"])  # no Authorization header

    assert served.status_code == 200
    assert served.headers["content-type"] == "text/html; charset=utf-8"  # Markdown, rendered
    assert b"<em>outcome</em>" in served.content
    assert served.headers["content-security-policy"].startswith("sandbox allow-popups")
    assert blobs.calls == [(await _repo(async_session, run), SPEC_BLOB)]


REPORTS = {"path": "reports/{ticket}/*.html", "label": "Reports"}


def _reported(*reports: tuple[str, str]) -> dict[str, Any]:
    footprint = _footprint()
    footprint["files"] += [
        {"path": path, "blob": blob, "size": 12, "changed": True} for path, blob in reports
    ]
    return footprint


async def test_a_pattern_lists_each_match_and_serves_it_through_its_link(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    report = "a" * 40
    run = await _run(
        async_session,
        preview={"files": [*FILES, REPORTS]},
        page_seen=None,
        footprint=_reported(("reports/12/a.html", report)),
    )
    blobs = _Blobs(b"<p>coverage</p>")

    async with _reader(_app(async_session, blobs)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
        (entry,) = [e for e in answer["entries"] if e["group"] == "Reports"]
        served = await client.get(entry["url"])

    repo = await _repo(async_session, run)
    assert (entry["key"], entry["label"], entry["group_total"]) == (
        "reports/{ticket}/*.html#reports/12/a.html",
        "a.html",
        1,
    )
    assert entry["group_url"] == f"https://github.com/{repo}/tree/{COMMIT}/reports/12"
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/html")
    assert blobs.calls == [(repo, report)]


async def test_a_match_past_the_cap_is_not_served(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    reports = [(f"reports/12/f{n:02}.html", f"{n:02}".rjust(40, "a")) for n in range(51)]
    run = await _run(
        async_session,
        preview={"files": [*FILES, REPORTS]},
        page_seen=None,
        footprint=_reported(*reports),
    )
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
        past = await client.get(_signed(run, reports[50][1]))

    assert len([e for e in answer["entries"] if e["group"] == "Reports"]) == 50
    assert past.status_code == 404
    assert blobs.calls == []


async def test_the_outputs_list_the_files_the_step_left_on_its_branch(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=_footprint())
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
        notes = await client.get(_signed(run, NOTES_BLOB))

    repo = await _repo(async_session, run)
    assert answer["changed_files"] == [
        {"path": path, "github_url": f"https://github.com/{repo}/blob/{COMMIT}/{path}"}
        for path in ("notes.txt", PLAN, SPEC)
    ]
    assert notes.status_code == 404
    assert blobs.calls == []


async def test_a_run_not_yet_observed_lists_no_changed_files(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=None)

    async with _reader(_app(async_session)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()

    assert answer["changed_files"] == []


async def test_a_recorded_blob_the_config_did_not_declare_is_404(
    async_session: AsyncSession,
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=_footprint())
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = await client.get(_signed(run, NOTES_BLOB))

    assert answer.status_code == 404
    assert blobs.calls == []


async def test_a_retired_pages_recorded_blob_is_404(
    async_session: AsyncSession,
) -> None:
    run = await _run(async_session, preview=_filed(), footprint=_footprint())
    blobs = _Blobs(b"<h1>Plan</h1>")

    async with _reader(_app(async_session, blobs)) as client:
        answer = await client.get(_signed(run, PAGE_BLOB))

    assert answer.status_code == 404
    assert blobs.calls == []


async def test_a_declared_file_over_the_limit_is_linked_not_served(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(
        async_session, preview=_filed(), footprint=_footprint(spec_size=PAGE_MAX_BYTES + 1)
    )
    blobs = _Blobs()

    async with _reader(_app(async_session, blobs)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()
        served = await client.get(_signed(run, SPEC_BLOB))

    spec = answer["entries"][0]
    assert (spec["too_large"], spec["url"]) == (True, None)
    assert spec["github_url"] is not None
    assert served.status_code == 413
    assert blobs.calls == []


async def test_a_run_not_yet_observed_lists_its_files_as_pending(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=_filed(page=False), page_seen=None)

    async with _reader(_app(async_session)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()

    assert [(e["kind"], e["label"], e["status"], e["url"]) for e in answer["entries"]] == [
        ("file", "Spec", "pending", None),
        ("file", "Plan", "pending", None),
    ]


async def test_a_step_without_preview_has_no_entries(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _run(async_session, preview=None, page_seen=None)

    async with _reader(_app(async_session)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()

    assert answer == {"step_run_id": run.id, "entries": [], "cut_off": False, "changed_files": []}


async def test_a_snapshot_pinned_with_only_a_page_lists_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    page_only = {"page": {"branch": "pages/{ticket}-{slug}", "path": "summary.html"}}
    run = await _run(async_session, preview=page_only)

    async with _reader(_app(async_session)) as client:
        answer = (await client.get(f"/api/step-runs/{run.id}/outputs", headers=auth)).json()

    assert answer["entries"] == []
