"""API tests: the auth properties, the read models, and the webhook's narrow contract.

The DB-backed tests run against real Postgres (tech-stack decision 11) and reuse the session
fixtures from `conftest.py`. The auth tests deliberately do *not*: their app overrides the session
dependency with one that raises, which proves the token is checked before anything touches the
database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from githubkit.exception import RequestFailed
from githubkit.response import Response
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    config_source_dependency,
    issue_reader_dependency,
)
from orchestrator.api.routes import tickets as tickets_routes
from orchestrator.api.services.assistant_settings import save_assistant_status
from orchestrator.config import load_process
from orchestrator.db.models import (
    CostSource,
    EnteredBy,
    Event,
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    TicketSource,
    TicketStatus,
)
from orchestrator.events import EventType, UnsafeEventDetails, append
from orchestrator.events.read_models import NEXT_ACTION, blocked_on, select_tickets
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import (
    ContentsNotFound,
    IssueState,
    PullRequestState,
)
from orchestrator.github.client import RateLimited
from tests.api_harness import (
    APP_ID,
    TOKEN,
    WEBHOOK_SECRET,
    build_app,
    client_for,
    make_run,
    make_settings,
    seed_connected,
    seed_project,
    seed_ticket,
)
from tests.api_probes import (
    MINI_PROCESS_YAML,
    MINI_SHA,
    ProbeContents,
    lane_files,
    mini_config_files,
    probe_app,
    seed_probe_project,
)
from tests.paths import SAMPLE_PROCESS

pytestmark = [
    pytest.mark.usefixtures("master_key"),
    pytest.mark.asyncio(loop_scope="session"),
]


#: Every authenticated route, as (method, path). The 401/503 properties are per-route.
API_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/projects/1"),
    ("PATCH", "/api/projects/1"),
    ("PUT", "/api/projects/1/active"),
    ("GET", "/api/projects/1/github-connection"),
    ("PUT", "/api/projects/1/github-connection"),
    ("GET", "/api/projects/1/ticket-source"),
    ("PUT", "/api/projects/1/ticket-source"),
    ("GET", "/api/projects/1/board"),
    ("GET", "/api/projects/1/tickets"),
    ("GET", "/api/projects/1/tickets/97"),
    ("GET", "/api/projects/1/events"),
    ("GET", "/api/projects/1/costs"),
    ("GET", "/api/projects/1/dashboard"),
    ("GET", "/api/projects/1/secrets"),
    ("PUT", "/api/projects/1/secrets/GH_TOKEN"),
    ("PATCH", "/api/projects/1/secrets/GH_TOKEN"),
    ("DELETE", "/api/projects/1/secrets/GH_TOKEN"),
    ("POST", "/api/projects/1/process-versions/sync"),
    ("GET", "/api/projects/1/process-versions"),
    ("GET", "/api/tickets/1"),
    ("POST", "/api/tickets/1/refresh"),
    ("GET", "/api/tickets/1/process"),
    ("GET", "/api/tickets/1/issue"),
    ("GET", "/api/tickets/1/gate-pull-request"),
    ("GET", "/api/tickets/1/timeline"),
    ("GET", "/api/orgs"),
    ("GET", "/api/orgs/default"),
    ("PATCH", "/api/orgs/default"),
    ("GET", "/api/orgs/default/projects"),
    ("POST", "/api/orgs/default/projects"),
    ("GET", "/api/orgs/default/runtimes"),
    ("GET", "/api/orgs/default/overview"),
    ("POST", "/api/orgs/default/refresh"),
    ("GET", "/api/orgs/default/settings/assistant"),
]


# ---------------------------------------------------------------------------------- health


async def test_healthz_needs_no_auth_and_no_database(no_db_client: AsyncClient) -> None:
    response = await no_db_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.postgres
async def test_readyz_needs_no_auth_but_touches_the_database(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.postgres
async def test_health_reports_the_assistant_status(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """`assistant` is the process's last preflight, or null when it never started. A failed one
    leaves the API ready: only a send is refused, with 503."""
    assert (await client.get("/readyz")).json() == {"status": "ready", "assistant": None}

    await save_assistant_status(
        async_session,
        {
            "ok": False,
            "message": "claude_sdk: drift",
            "version": "claude_sdk 0.2.161",
            "at": "2026-10-12T15:00:00+00:00",
        },
    )
    response = await client.get("/readyz")
    await async_session.rollback()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "assistant": {
            "ok": False,
            "message": "claude_sdk: drift",
            "at": "2026-10-12T15:00:00Z",
        },
    }


# ------------------------------------------------------------------------------------ auth


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
async def test_api_routes_require_a_token(
    no_db_client: AsyncClient, method: str, path: str
) -> None:
    response = await no_db_client.request(method, path, json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
async def test_api_routes_reject_the_wrong_token(
    no_db_client: AsyncClient, method: str, path: str
) -> None:
    response = await no_db_client.request(
        method, path, json={}, headers={"Authorization": "Bearer not-the-token"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
async def test_unset_api_token_fails_closed(method: str, path: str) -> None:
    """An unconfigured deployment must be *broken*, never open (tech-stack decision 12)."""
    app = build_app(make_settings(api_token=""), None)
    async with client_for(app) as unconfigured:
        assert (await unconfigured.request(method, path, json={})).status_code == 503
        with_token = await unconfigured.request(
            method, path, json={}, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert with_token.status_code == 503
        assert "FACTORIAL_API_TOKEN" in with_token.json()["detail"]


@pytest.mark.postgres
async def test_authenticated_routes_answer_200(client: AsyncClient, auth: dict[str, str]) -> None:
    for path in (
        "/api/orgs/default/projects",
        "/api/projects/1/board",
        "/api/projects/1/events",
        "/api/projects/1/costs",
        "/api/projects/1/dashboard",
    ):
        assert (await client.get(path, headers=auth)).status_code == 200, path


# ----------------------------------------------------------------------------- read models


@pytest.mark.postgres
async def test_board_groups_open_tickets_by_step_and_status(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    await seed_ticket(async_session, project, step="develop")
    await seed_ticket(async_session, project, step="develop")
    await seed_ticket(async_session, project, step="spec", status=TicketStatus.WAITING_GATE)
    closed = await seed_ticket(async_session, project, step="develop", status=TicketStatus.DONE)
    closed.closed_at = datetime.now(UTC)  # closed tickets leave the board
    run = StepRun(
        ticket_id=closed.id,
        step_name="develop",
        kind=StepKind.AGENT,
        status=StepRunStatus.RUNNING,
    )
    async_session.add(run)
    await async_session.flush()

    body = (await client.get(f"/api/projects/{project.id}/board", headers=auth)).json()
    assert body["cells"] == [
        {"current_step": "develop", "status": "running", "count": 2},
        {"current_step": "spec", "status": "waiting_gate", "count": 1},
    ]
    assert [r["step_run_id"] for r in body["in_flight"]] == [run.id]
    assert body["in_flight"][0]["external_key"] == closed.external_key


@pytest.mark.postgres
async def test_a_run_at_the_token_cap_does_not_break_the_totals(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The worker holds each count to the bigint because an agent's log reports it
    (`worker.pricing`), so a run can sit at the cap. Input plus output must not overflow the
    reads that total them: the factory board, the cost report and the dashboard."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    cap = 2**63 - 1
    async_session.add(
        StepRun(
            ticket_id=ticket.id,
            step_name="develop",
            kind=StepKind.AGENT,
            attempt=1,
            status=StepRunStatus.SUCCEEDED,
            input_tokens=cap,
            output_tokens=cap,
        )
    )
    await async_session.commit()

    board = await client.get("/api/orgs/default/overview", headers=auth)
    assert board.status_code == 200, board.text
    row = next(t for t in board.json()["tickets"] if t["ticket_id"] == ticket.id)
    assert row["tokens"] == 2 * cap
    for path in (f"/api/projects/{project.id}/costs", f"/api/projects/{project.id}/dashboard"):
        assert (await client.get(path, headers=auth)).status_code == 200, path


@pytest.mark.postgres
async def test_board_totals_what_the_project_has_spent(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The project header's summary rides on the board, which is the project read the dashboard
    polls — see `BoardResponse.spend`. It spans every ticket, closed ones included: a project's
    spend is what it has spent, not what it is spending now."""
    project = await seed_project(async_session)
    running = await seed_ticket(async_session, project, step="develop")
    closed = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    closed.closed_at = datetime.now(UTC)
    async_session.add_all(
        [
            StepRun(
                ticket_id=running.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.RUNNING,
                cost_usd=Decimal("1.50"),
                input_tokens=1000,
                output_tokens=500,
            ),
            StepRun(
                ticket_id=closed.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                cost_usd=Decimal("0.25"),
                input_tokens=40,
                output_tokens=8,
            ),
            # A gate reports no usage at all. Its NULLs must not swallow the sums beside them.
            StepRun(
                ticket_id=closed.id,
                step_name="spec_gate",
                kind=StepKind.HUMAN_GATE,
                attempt=1,
                status=StepRunStatus.APPROVED,
            ),
        ]
    )
    await async_session.flush()

    spend = (await client.get(f"/api/projects/{project.id}/board", headers=auth)).json()["spend"]
    # Split rather than added: input and output are priced differently, so a single total would
    # hide which half the project is heavy in.
    assert spend["input_tokens"] == 1040
    assert spend["output_tokens"] == 508
    assert Decimal(spend["cost_usd"]) == Decimal("1.75")
    assert spend["cost_estimated"] is False


@pytest.mark.postgres
async def test_every_total_says_when_any_part_of_it_is_an_estimate(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A codex run's figure is a list-price estimate (decision 34). Summed with a measured one
    the total is no longer a bill, and every read that sums must say so."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="develop")
    async_session.add_all(
        [
            StepRun(
                ticket_id=ticket.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                model="claude-haiku-4-5-20251001",
                cost_usd=Decimal("1.50"),
                cost_source="reported",
                input_tokens=1000,
                output_tokens=500,
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                model="gpt-5.3-codex",
                cost_usd=Decimal("0.25"),
                cost_source="estimated",
                input_tokens=40,
                cached_input_tokens=30,
                output_tokens=8,
            ),
        ]
    )
    await async_session.flush()

    spend = (await client.get(f"/api/projects/{project.id}/board", headers=auth)).json()["spend"]
    assert Decimal(spend["cost_usd"]) == Decimal("1.75")
    assert spend["cost_estimated"] is True

    rows = (await client.get(f"/api/projects/{project.id}/tickets", headers=auth)).json()["tickets"]
    (row,) = [r for r in rows if r["id"] == ticket.id]
    assert row["cost_estimated"] is True

    overview = (await client.get("/api/orgs/default/overview", headers=auth)).json()
    (board_row,) = [t for t in overview["tickets"] if t["project_id"] == project.id]
    assert board_row["cost_estimated"] is True

    detail = (await client.get(f"/api/tickets/{ticket.id}", headers=auth)).json()
    by_step = {r["step_name"]: r for r in detail["step_runs"]}
    assert by_step["spec"]["cost_source"] == "reported"
    assert by_step["spec"]["model"] == "claude-haiku-4-5-20251001"
    assert by_step["develop"]["cost_source"] == "estimated"
    assert by_step["develop"]["cached_input_tokens"] == 30


@pytest.mark.postgres
async def test_board_spend_is_zero_for_a_project_that_has_not_run(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    # Zero, never null: "nothing spent" is a figure, and the header decides for itself what to
    # draw for it. A project with no tickets at all still has to answer.
    project = await seed_project(async_session)
    await seed_ticket(async_session, project)
    await async_session.flush()

    spend = (await client.get(f"/api/projects/{project.id}/board", headers=auth)).json()["spend"]
    assert spend["input_tokens"] == 0
    assert spend["output_tokens"] == 0
    # The scale is whatever the empty SUM's COALESCE produced, so compare the value rather than
    # the spelling — the client reads this back through `Number()` either way.
    assert Decimal(spend["cost_usd"]) == Decimal(0)


@pytest.mark.postgres
async def test_timeline_is_in_log_order(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    for event_type in (EventType.TICKET_ADMITTED, EventType.STEP_STARTED, EventType.STEP_COMPLETED):
        await append(
            async_session,
            type=event_type,
            project_id=project.id,
            ticket_id=ticket.id,
            step="develop",
        )
    # Another ticket's events must not bleed in.
    other = await seed_ticket(async_session, project)
    await append(async_session, type=EventType.ESCALATED, project_id=project.id, ticket_id=other.id)

    body = (await client.get(f"/api/tickets/{ticket.id}/timeline", headers=auth)).json()
    assert [entry["type"] for entry in body["entries"]] == [
        "ticket_admitted",
        "step_started",
        "step_completed",
    ]
    assert body["entries"][0]["details"] == {"step": "develop"}


@pytest.mark.postgres
async def test_ticket_detail_carries_its_attempts(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    async_session.add_all(
        [
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.FAILED,
                failure_reason="postconditions not met",
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=2,
                status=StepRunStatus.SUCCEEDED,
            ),
        ]
    )
    await async_session.flush()

    body = (await client.get(f"/api/tickets/{ticket.id}", headers=auth)).json()
    assert body["current_step"] == "develop"
    assert [(r["attempt"], r["status"]) for r in body["step_runs"]] == [
        (1, "failed"),
        (2, "succeeded"),
    ]

    assert (await client.get("/api/tickets/999999999", headers=auth)).status_code == 404


@pytest.mark.postgres
async def test_ticket_detail_by_external_key_is_the_by_id_detail(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The operator holds the issue number, never the internal id; the by-key route answers with
    the identical body, step-run progression included."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    async_session.add(
        StepRun(
            ticket_id=ticket.id,
            step_name="develop",
            kind=StepKind.AGENT,
            attempt=1,
            status=StepRunStatus.SUCCEEDED,
        )
    )
    await async_session.flush()

    by_key = await client.get(
        f"/api/projects/{project.id}/tickets/{ticket.external_key}", headers=auth
    )
    assert by_key.status_code == 200
    body = by_key.json()
    assert body == (await client.get(f"/api/tickets/{ticket.id}", headers=auth)).json()
    [run] = body["step_runs"]
    assert {"step_name", "attempt", "status", "started_at", "ended_at", "created_at"} <= run.keys()

    # Scoped: the same key under another project is a 404, not a cross-project read.
    other = await seed_project(async_session)
    missing = await client.get(
        f"/api/projects/{other.id}/tickets/{ticket.external_key}", headers=auth
    )
    assert missing.status_code == 404
    unknown = await client.get(f"/api/projects/{project.id}/tickets/no-such-key", headers=auth)
    assert unknown.status_code == 404


# ---------------------------------------------------------------------- the ticket's issue
#
# The one ticket route that leaves the database. The issue body is never stored — GitHub owns it
# and the loop only observes it — so these tests drive `issue_reader_dependency`, the seam that
# stands for that read.


def issue_app(
    session: AsyncSession, answer: IssueState | Exception, seen: list[tuple[str, int]] | None = None
) -> FastAPI:
    """`build_app` with the issue reader answering `answer` (or raising it), recording its
    arguments in `seen` so a test can assert WHICH repo and issue were asked for."""

    async def read_issue(repo: str, number: int) -> IssueState:
        if seen is not None:
            seen.append((repo, number))
        if isinstance(answer, Exception):
            raise answer
        return answer

    app = build_app(make_settings(), session)
    app.dependency_overrides[issue_reader_dependency] = lambda: read_issue
    return app


def an_issue(number: int = 204, body: str = "The remit-to address is wrong.") -> IssueState:
    return IssueState(
        number=number,
        title="Invoices show the wrong remit-to address",
        body=body,
        state="open",
        labels=frozenset({"factorial:plan"}),
        assignees=("operator",),
    )


@pytest.mark.postgres
async def test_ticket_issue_reads_the_project_repo_live(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The description comes back verbatim, and it is fetched from `project.github_repo` under the
    ticket's own key — the same pair `worker.reconcile` observes, and the same repo the dashboard
    builds its issue links from."""
    project = await seed_project(async_session, repo="northwind/harbor-invoices")
    ticket = await seed_ticket(async_session, project, external_key="204")
    # The two repos are the same field in every other fixture, which would make the assertion
    # below true either way. Split them, so this pins `github_repo` and not just "some repo".
    source = (
        await async_session.execute(
            select(TicketSource).where(TicketSource.project_id == project.id)
        )
    ).scalar_one()
    source.external_ref = "northwind/intake"
    await async_session.flush()
    seen: list[tuple[str, int]] = []

    async with client_for(issue_app(async_session, an_issue(), seen)) as client:
        body = (await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)).json()

    assert seen == [("northwind/harbor-invoices", 204)]
    assert body == {
        "ticket_id": ticket.id,
        "repo": "northwind/harbor-invoices",
        "number": 204,
        "title": "Invoices show the wrong remit-to address",
        "body": "The remit-to address is wrong.",
        "state": "open",
    }


@pytest.mark.postgres
async def test_ticket_issue_is_not_served_from_a_stored_copy(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Two reads of an issue that was edited in between give the two different bodies.

    The property under test is that nothing caches the description server-side: an operator who
    rewrites an issue to unblock a stuck ticket must see the rewrite, not what was true at
    admission.
    """
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="7")
    bodies = iter(["before the edit", "after the edit"])

    async def read_issue(repo: str, number: int) -> IssueState:
        return an_issue(number=7, body=next(bodies))

    app = build_app(make_settings(), async_session)
    app.dependency_overrides[issue_reader_dependency] = lambda: read_issue
    async with client_for(app) as client:
        first = (await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)).json()
        second = (await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)).json()

    assert first["body"] == "before the edit"
    assert second["body"] == "after the edit"


@pytest.mark.postgres
async def test_ticket_issue_uses_gitlab_without_calling_the_github_reader(
    auth: dict[str, str],
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await seed_project(async_session, repo="group/app")
    ticket = await seed_ticket(async_session, project, external_key="40")
    source = await async_session.get(TicketSource, ticket.ticket_source_id)
    assert source is not None
    source.kind = "gitlab"
    await async_session.flush()

    class Client:
        async def get(self, path: str, **params: object) -> object:
            assert path == "/projects/group%2Fapp/issues/40"
            return {
                "iid": 40,
                "title": "GitLab issue",
                "description": "Read from GitLab",
                "state": "opened",
                "labels": [],
                "assignees": [],
            }

    async def project_client(session: AsyncSession, row: Project) -> Client:
        return Client()

    monkeypatch.setattr(
        "orchestrator.gitlab.project_client.client_for_project", project_client
    )

    async def github_reader(repo: str, number: int) -> IssueState:
        raise AssertionError("GitHub must not be called for a GitLab ticket")

    app = build_app(make_settings(), async_session)
    app.dependency_overrides[issue_reader_dependency] = lambda: github_reader
    async with client_for(app) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)

    assert response.status_code == 200
    assert response.json()["body"] == "Read from GitLab"


@pytest.mark.postgres
async def test_ticket_issue_separates_a_missing_ticket_from_a_missing_issue(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Both are 404s, and the detail is what tells an operator which one happened: a ticket this
    orchestrator never admitted, or an issue GitHub will not hand over any more."""
    project = await seed_project(async_session, repo="northwind/harbor-invoices")
    ticket = await seed_ticket(async_session, project, external_key="204")

    async with client_for(issue_app(async_session, an_issue())) as client:
        unknown = await client.get("/api/tickets/999999999/issue", headers=auth)
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Ticket not found."

    gone = _a_request_failed(404)
    async with client_for(issue_app(async_session, gone)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)
    assert response.status_code == 404
    assert "northwind/harbor-invoices" in response.json()["detail"]


@pytest.mark.postgres
async def test_ticket_issue_blames_github_for_githubs_failures(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A 403 from GitHub is a 502 here, not a 500: the orchestrator is fine, its upstream is not,
    and a 500 would send an operator looking in the wrong logs."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="204")

    async with client_for(issue_app(async_session, _a_request_failed(403))) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)

    assert response.status_code == 502
    assert "403" in response.json()["detail"]


@pytest.mark.postgres
async def test_ticket_issue_answers_503_with_retry_after_while_github_s_limit_is_spent(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A connection waiting out its rate limit is neither this server's fault nor GitHub
    refusing the request: no handler made it a 500 on every GitHub-backed route."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="204")
    spent = RateLimited(datetime.now(UTC) + timedelta(minutes=10))

    async with client_for(issue_app(async_session, spent)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)

    assert response.status_code == 503
    assert 590 <= int(response.headers["retry-after"]) <= 600
    assert "rate limit" in response.json()["detail"]


@pytest.mark.postgres
async def test_ticket_issue_refuses_a_key_that_is_not_an_issue_number(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """`external_key` is free text and a future source kind may not number its tickets. That is a
    422 naming the key, never an unhandled `int()` surfacing as a 500."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="PROJ-14")

    async with client_for(issue_app(async_session, an_issue())) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)

    assert response.status_code == 422
    assert "PROJ-14" in response.json()["detail"]


@pytest.mark.postgres
async def test_ticket_issue_fails_closed_without_a_github_connection(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """No override, so the real dependency runs against a project with no GitHub connection.

    Fail closed, unlike the readiness probes: reading the issue IS this request, so a 503 naming
    the missing configuration beats a description modal that opens empty.
    """
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="204")

    response = await client.get(f"/api/tickets/{ticket.id}/issue", headers=auth)

    assert response.status_code == 503
    assert "no GitHub connection" in response.json()["detail"]


@pytest.mark.postgres
async def test_a_gate_pull_request_github_cannot_list_is_no_live_pick(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page polls this while a ticket waits at a gate and falls back to the number the
    `gate_waiting` event recorded; a GitHub outage must leave it there, not turn into 500s."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="204")

    async def unavailable(*_: object, **__: object) -> None:
        raise _a_request_failed(502)

    monkeypatch.setattr(tickets_routes, "observed_gate_pull_request", unavailable)
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/gate-pull-request", headers=auth)

    assert response.status_code == 200
    assert response.json()["number"] is None


@pytest.mark.postgres
async def test_a_gate_pull_request_says_whether_it_is_closed(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate holds on a pick closed without a merge (decision 79), and the page tells that
    pick from an open one only by these two fields."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, external_key="204")

    async def closed(*_: object, **__: object) -> PullRequestState:
        return PullRequestState(
            number=57,
            head_ref="spec/204-widget",
            base_ref="main",
            head_sha="sha-1",
            state="closed",
            merged=False,
            url="https://github.test/pr/57",
        )

    monkeypatch.setattr(tickets_routes, "observed_gate_pull_request", closed)
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/gate-pull-request", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert (body["number"], body["state"], body["merged"]) == (57, "closed", False)


def _a_request_failed(status_code: int) -> RequestFailed:
    """A real `RequestFailed`, built the way githubkit builds one — the concrete class the route's
    except arm names, not a stand-in for it."""
    request = httpx.Request("GET", "https://api.github.test/repos/acme/x/issues/204")
    raw = httpx.Response(status_code, request=request, json={"message": "Not Found"})
    return RequestFailed(Response(raw, dict[str, Any]))


@pytest.mark.postgres
async def test_ticket_process_is_the_snapshot_the_ticket_pinned(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Pinned, not latest: a config edit after admission must not change this ticket's graph."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    pinned = (
        await async_session.execute(
            select(ProcessVersion).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    pinned.content = {
        "specVersion": "adlc/v1",
        "entry": {"step": "develop"},
        "steps": [{"name": "develop", "kind": "agent", "next": "done"}],
        "terminal": {"step": "done"},
    }
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            process_key="p",
            content={"specVersion": "adlc/v1"},
            source_commit_sha="b" * 40,
        )
    )
    await async_session.flush()

    body = (await client.get(f"/api/tickets/{ticket.id}/process", headers=auth)).json()
    assert body["ticket_id"] == ticket.id
    assert body["process_version_id"] == ticket.process_version_id
    # The lane and the commit it was read at, which is what the ticket page shows on its stage.
    assert (body["process_key"], body["source_commit_sha"]) == ("p", "a" * 40)
    # camelCase survives the round trip: the graph reads `process.yaml`'s own spelling.
    assert body["content"]["specVersion"] == "adlc/v1"
    assert body["content"]["terminal"] == {"step": "done"}

    assert (await client.get("/api/tickets/999999999/process", headers=auth)).status_code == 404


@pytest.mark.postgres
async def test_costs_sum_per_ticket(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    async_session.add_all(
        [
            StepRun(
                ticket_id=ticket.id,
                step_name="spec",
                kind=StepKind.AGENT,
                status=StepRunStatus.SUCCEEDED,
                input_tokens=1000,
                output_tokens=250,
                cost_usd=Decimal("1.2500"),
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                status=StepRunStatus.SUCCEEDED,
                input_tokens=4000,
                output_tokens=750,
                cost_usd=Decimal("4.1200"),
            ),
            # A gate has no usage; it must not break the sum.
            StepRun(
                ticket_id=ticket.id,
                step_name="spec_gate",
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.APPROVED,
            ),
        ]
    )
    await async_session.flush()

    body = (await client.get(f"/api/projects/{project.id}/costs", headers=auth)).json()
    assert body["tickets"] == [
        {
            "ticket_id": ticket.id,
            "external_key": ticket.external_key,
            "cost_usd": "5.3700",
            "tokens": 6000,
        }
    ]
    assert Decimal(body["total_cost_usd"]) == Decimal("5.37")
    assert body["total_tokens"] == 6000


@pytest.mark.postgres
async def test_dashboard_carries_every_figure_the_tab_draws(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """One answer, every section (decision 61). Three tickets cover the three outcomes; a rework
    attempt, a retry and an estimated run cover the rules the figures rest on. A field dropped
    from the answer is a tile that silently draws nothing."""
    now = datetime.now(UTC)
    project = await seed_project(async_session)
    done = await seed_ticket(
        async_session, project, step="done", status=TicketStatus.DONE, title="Fix the remit-to"
    )
    exited = await seed_ticket(async_session, project, step="develop", status=TicketStatus.EXITED)
    fresh = await seed_ticket(async_session, project, step="plan")
    # `all` starts at the first admission, which `seed_ticket` leaves at now(): the runs below
    # would otherwise fall before it.
    for ticket in (done, exited, fresh):
        ticket.admitted_at = now - timedelta(days=5)
    done.closed_at = now - timedelta(days=1)
    exited.closed_at = now - timedelta(days=3)
    t0 = now - timedelta(days=4)
    claude = "claude-sonnet-4-5-20250929"
    async_session.add_all(
        [
            make_run(
                done.id,
                "spec",
                1,
                cost="1.50",
                entered_by=EnteredBy.ADMISSION,
                model=claude,
                tokens=(1000, 500),
                cached_input_tokens=300,
                started_at=t0,
                took=timedelta(minutes=30),
            ),
            make_run(
                done.id,
                "develop",
                1,
                cost="2.00",
                model=claude,
                tokens=(2000, 100),
                started_at=t0 + timedelta(hours=1),
                took=timedelta(minutes=40),
            ),
            make_run(
                done.id,
                "code_gate",
                1,
                cost=None,
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.REJECTED,
                source=None,
                tokens=None,
                decided_by="operator",
                started_at=t0 + timedelta(hours=2),
                took=timedelta(hours=24),
            ),
            # Rework: the gate sent the ticket back. It failed, so the worker retried it.
            make_run(
                done.id,
                "develop",
                2,
                cost="0.25",
                entered_by=EnteredBy.REJECT_RERUN,
                status=StepRunStatus.FAILED,
                source=CostSource.ESTIMATED,
                model="gpt-5.3-codex",
                tokens=(40, 8),
                started_at=t0 + timedelta(hours=27),
            ),
            make_run(
                done.id,
                "develop",
                3,
                cost="0.30",
                entered_by=EnteredBy.RETRY,
                model=claude,
                tokens=(60, 12),
                started_at=t0 + timedelta(hours=28),
                took=timedelta(minutes=20),
            ),
            make_run(
                done.id,
                "code_gate",
                2,
                cost=None,
                kind=StepKind.HUMAN_GATE,
                status=StepRunStatus.APPROVED,
                source=None,
                tokens=None,
                decided_by="operator",
                started_at=t0 + timedelta(hours=29),
                took=timedelta(hours=1),
            ),
            # A failed attempt on a ticket that then exited: in two unmerged buckets, summed once.
            make_run(
                exited.id,
                "develop",
                1,
                cost="0.40",
                status=StepRunStatus.FAILED,
                model=claude,
                tokens=(400, 10),
                started_at=now - timedelta(days=3, minutes=5),
                took=timedelta(minutes=5),
            ),
            Event(
                project_id=project.id,
                ticket_id=done.id,
                type=EventType.TICKET_DONE,
                details={},
                created_at=now - timedelta(days=1),
            ),
            Event(
                project_id=project.id,
                ticket_id=exited.id,
                type=EventType.TICKET_EXITED,
                details={},
                created_at=now - timedelta(days=3),
            ),
        ]
    )
    await async_session.flush()

    response = await client.get(f"/api/projects/{project.id}/dashboard", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["project_id"] == project.id
    assert body["span"] == "all"
    # `all` starts where the project did.
    assert datetime.fromisoformat(body["since"]) == now - timedelta(days=5)
    assert datetime.fromisoformat(body["as_of"]) >= now
    assert body["limits"] == {
        "by_ticket": 5,
        "step_costs": 5,
        "open_gates": 5,
        "p90_min_runs": 7,
    }

    lifetime = body["lifetime"]
    assert lifetime["spend"]["input_tokens"] == 3500
    assert lifetime["spend"]["output_tokens"] == 630
    assert Decimal(lifetime["spend"]["cost_usd"]) == Decimal("4.45")
    assert lifetime["spend"]["cost_estimated"] is True
    assert lifetime["tickets"] == {"done": 1, "exited": 1, "open": 1, "total": 3}
    assert lifetime["per_done"]["tickets"] == 1
    assert Decimal(lifetime["per_done"]["mean_usd"]) == Decimal("4.05")
    assert Decimal(lifetime["per_done"]["median_usd"]) == Decimal("4.05")
    assert lifetime["per_done"]["cost_estimated"] is True
    assert lifetime["runs"] == {
        "total": 7,
        "priced": 5,
        "estimated": 1,
        "subscription": 0,
        "cached_input_tokens": 300,
    }

    assert [row["outcome"] for row in body["by_outcome"]] == ["done", "open", "exited"]
    outcomes = {row["outcome"]: row for row in body["by_outcome"]}
    assert outcomes["done"]["tickets"] == 1
    assert Decimal(outcomes["done"]["cost_usd"]) == Decimal("4.05")
    assert outcomes["done"]["tokens"] == 3720
    assert outcomes["done"]["cost_estimated"] is True
    # An admitted ticket with no runs is still a ticket in its outcome, at zero.
    assert outcomes["open"]["tickets"] == 1
    assert Decimal(outcomes["open"]["cost_usd"]) == Decimal(0)
    assert outcomes["open"]["cost_estimated"] is False
    assert Decimal(outcomes["exited"]["cost_usd"]) == Decimal("0.40")
    assert outcomes["exited"]["tokens"] == 410

    assert [t["ticket_id"] for t in body["by_ticket"]] == [done.id, exited.id]
    top = body["by_ticket"][0]
    assert top["external_key"] == done.external_key
    assert top["title"] == "Fix the remit-to"
    assert top["status"] == "done"
    assert top["attempts"] == 6
    assert top["tokens"] == 3720
    assert Decimal(top["cost_usd"]) == Decimal("4.05")
    assert top["cost_estimated"] is True
    assert body["by_ticket_rest"] is None
    assert body["unrun_tickets"] == 1
    assert fresh.id not in {t["ticket_id"] for t in body["by_ticket"]}

    assert [t["ticket_id"] for t in body["step_costs"]] == [done.id, exited.id]
    assert body["step_costs"][0]["title"] == "Fix the remit-to"
    assert Decimal(body["step_costs"][0]["cost_usd"]) == Decimal("4.05")
    segments = body["step_costs"][0]["segments"]
    assert [(s["step"], s["attempt"]) for s in segments] == [
        ("spec", 1),
        ("develop", 1),
        ("code_gate", 1),
        ("develop", 2),
        ("develop", 3),
        ("code_gate", 2),
    ]
    by_attempt = {(s["step"], s["attempt"]): s for s in segments}
    # The rework rule: `reject_rerun` is rework, `retry` is not.
    assert by_attempt[("develop", 2)]["entered_by"] == "reject_rerun"
    assert by_attempt[("develop", 3)]["entered_by"] == "retry"
    assert by_attempt[("develop", 1)]["entered_by"] == "next"
    assert by_attempt[("spec", 1)]["entered_by"] == "admission"
    # The estimated run marks its segment, and every sum it is part of above.
    assert by_attempt[("develop", 2)]["cost_estimated"] is True
    assert by_attempt[("develop", 2)]["model"] == "gpt-5.3-codex"
    assert by_attempt[("develop", 2)]["status"] == "failed"
    assert by_attempt[("develop", 1)]["seconds"] == 2400.0
    assert by_attempt[("develop", 1)]["tokens"] == 2100
    assert by_attempt[("develop", 1)]["runtime_type"] is None
    # A gate is a zero-cost segment, not a missing one.
    assert Decimal(by_attempt[("code_gate", 1)]["cost_usd"]) == Decimal(0)
    assert by_attempt[("code_gate", 1)]["kind"] == "human_gate"

    # By first start: spec began at t0, develop an hour later. develop's four durations are 2400,
    # 600, 1200 and 300 seconds; the gate's two are 86400 and 3600. Under seven runs, no p90.
    assert body["steps"] == [
        {
            "process_key": "p",
            "step": "spec",
            "kind": "agent",
            "runs": 1,
            "median_seconds": 1800.0,
            "p90_seconds": None,
            "max_seconds": 1800.0,
        },
        {
            "process_key": "p",
            "step": "develop",
            "kind": "agent",
            "runs": 4,
            "median_seconds": 900.0,
            "p90_seconds": None,
            "max_seconds": 2400.0,
        },
    ]
    assert body["gates"] == [
        {
            "process_key": "p",
            "step": "code_gate",
            "kind": "human_gate",
            "runs": 2,
            "median_seconds": 45000.0,
            "p90_seconds": None,
            "max_seconds": 86400.0,
        }
    ]

    unmerged = body["unmerged"]
    buckets = (unmerged["rework"], unmerged["exited"], unmerged["failed"])
    assert [(b["count"], Decimal(b["cost_usd"]), b["cost_estimated"]) for b in buckets] == [
        (1, Decimal("0.25"), True),
        (1, Decimal("0.40"), False),
        (2, Decimal("0.65"), True),
    ]
    # The exited ticket's only run is one of the failed attempts: a set of runs, summed once.
    assert Decimal(unmerged["total_usd"]) == Decimal("0.65")
    assert unmerged["cost_estimated"] is True

    weeks = body["weeks"]
    assert [w["week_start"] for w in weeks] == sorted(w["week_start"] for w in weeks)
    assert sum(w["done"] for w in weeks) == 1
    assert sum(w["exited"] for w in weeks) == 1
    assert sum(Decimal(w["cost_usd"]) for w in weeks) == Decimal("4.45")
    assert any(w["cost_estimated"] for w in weeks)
    closed = {(c["ticket_id"], c["external_key"], c["outcome"]) for w in weeks for c in w["closed"]}
    assert closed == {
        (done.id, done.external_key, "done"),
        (exited.id, exited.external_key, "exited"),
    }

    extremes = body["extremes"]
    largest = extremes["largest_run"]
    assert (largest["ticket_id"], largest["step"], largest["attempt"]) == (done.id, "develop", 1)
    assert largest["title"] == "Fix the remit-to"
    assert Decimal(largest["cost_usd"]) == Decimal("2.00")
    assert largest["seconds"] == 2400.0
    assert largest["tokens"] == 2100
    most = extremes["most_attempts"]
    assert (most["ticket_id"], most["step"], most["attempts"]) == (done.id, "develop", 3)
    assert (most["rework"], most["retries"]) == (1, 1)
    assert Decimal(most["cost_usd"]) == Decimal("2.55")
    assert Decimal(most["rework_cost_usd"]) == Decimal("0.25")
    assert most["cost_estimated"] is True
    gate = extremes["longest_gate"]
    assert (gate["ticket_id"], gate["step"], gate["attempt"]) == (done.id, "code_gate", 1)
    assert (gate["status"], gate["seconds"]) == ("rejected", 86400.0)

    # 12 weeks reach back past this 5-day-old project, so the window is clipped to its first
    # admission.
    windowed = (
        await client.get(f"/api/projects/{project.id}/dashboard?span=12w", headers=auth)
    ).json()
    assert windowed["span"] == "12w"
    assert datetime.fromisoformat(windowed["since"]) == now - timedelta(days=5)
    assert windowed["lifetime"] == lifetime
    # A typo is a 422, as an unknown ticket status is: it must not read as the default span.
    bogus = await client.get(f"/api/projects/{project.id}/dashboard?span=2w", headers=auth)
    assert bogus.status_code == 422


@pytest.mark.postgres
async def test_overview_carries_every_figure_the_board_draws(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="develop")
    quarantined = await seed_ticket(async_session, project, step="spec")
    quarantined_at = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    quarantined.quarantined_at = quarantined_at
    started = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    async_session.add_all(
        [
            StepRun(
                ticket_id=ticket.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
                cost_usd=Decimal("1.50"),
                input_tokens=1000,
                output_tokens=500,
                started_at=started,
                ended_at=started + timedelta(minutes=30),
            ),
            # Unpriced and never started, so it moves no figure but the two counts.
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.FAILED,
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=2,
                status=StepRunStatus.RUNNING,
                cost_usd=Decimal("0.50"),
                input_tokens=100,
                output_tokens=0,
                started_at=started + timedelta(hours=1),
                ended_at=None,
            ),
        ]
    )
    await async_session.flush()

    body = (await client.get("/api/orgs/default/overview", headers=auth)).json()

    # Scoped to the seeded project, not asserted as the whole answer: the route spans every
    # project, so anything another test committed is legitimately in the same response.
    assert {"id": project.id, "name": project.name, "active": project.active} in body["projects"]
    rows = {t["ticket_id"]: t for t in body["tickets"] if t["project_id"] == project.id}
    assert set(rows) == {ticket.id, quarantined.id}
    # Pinned from both sides: a column inserted into the select shifts every index after it,
    # and a sum of seconds in this slot coerces to a 1970 datetime rather than failing.
    assert datetime.fromisoformat(rows[quarantined.id]["quarantined_at"]) == quarantined_at
    row = rows[ticket.id]
    assert row["quarantined_at"] is None
    assert row["ticket_id"] == ticket.id
    assert row["project_id"] == project.id
    assert row["current_step"] == "develop"
    assert row["status"] == "running"
    # Every step_run row, across every step — the board reads this as "has it run at all".
    assert row["runs"] == 3
    # The attempt at the step it is ON, which is what separates Running from Retrying.
    assert row["attempt"] == 2
    assert Decimal(row["cost_usd"]) == Decimal("2.00")
    assert row["cost_estimated"] is False
    assert row["tokens"] == 1600
    # Only the run that finished has a duration; the one still going contributes nothing.
    assert row["minutes"] == 30.0


@pytest.mark.postgres
async def test_overview_keeps_recently_done_and_drops_old_ones(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    # Done is most of a healthy factory's board. Dropping closed tickets entirely would leave the
    # largest lane permanently empty; keeping them all would swamp the board within a month.
    project = await seed_project(async_session)
    fresh = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    fresh.closed_at = datetime.now(UTC) - timedelta(days=2)
    stale = await seed_ticket(async_session, project, step="done", status=TicketStatus.DONE)
    stale.closed_at = datetime.now(UTC) - timedelta(days=30)
    await async_session.flush()

    def seeded(body: dict[str, Any]) -> set[int]:
        return {t["ticket_id"] for t in body["tickets"] if t["project_id"] == project.id}

    overview = await client.get("/api/orgs/default/overview", headers=auth)
    assert seeded(overview.json()) == {fresh.id}

    wide = (await client.get("/api/orgs/default/overview?done_days=60", headers=auth)).json()
    assert seeded(wide) == {fresh.id, stale.id}


@pytest.mark.postgres
async def test_overview_reports_a_ticket_that_has_never_run(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    # An admitted ticket has no step_run rows at all. A join would drop it; the board needs it,
    # because "admitted" is a lane.
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="plan")
    await async_session.flush()

    body = (await client.get("/api/orgs/default/overview", headers=auth)).json()
    (row,) = [t for t in body["tickets"] if t["project_id"] == project.id]
    assert row["ticket_id"] == ticket.id
    assert row["runs"] == 0
    assert row["attempt"] is None
    assert row["latest_run_status"] is None
    assert Decimal(row["cost_usd"]) == Decimal("0")
    assert row["tokens"] == 0
    assert row["minutes"] == 0.0


@pytest.mark.postgres
async def test_overview_says_how_the_current_step_ended(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """`status` alone cannot tell an interrupt from an escalation.

    Three unrelated endings park a ticket as `blocked` — a run a human stopped, a gate rejected
    with nowhere to loop back to, and a step that exhausted its retries — and the board draws them
    in two different lanes. The run at the current step is what separates them, so it goes on the
    wire beside the status rather than being re-derived from a second request.
    """
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project, step="develop", status=TicketStatus.BLOCKED)
    async_session.add_all(
        [
            # The earlier attempt failed; the latest one is the interrupt. Highest attempt wins,
            # not last-inserted — a retry that is then stopped must read as stopped.
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.FAILED,
            ),
            StepRun(
                ticket_id=ticket.id,
                step_name="develop",
                kind=StepKind.AGENT,
                attempt=2,
                status=StepRunStatus.INTERRUPTED,
            ),
            # A later attempt at a DIFFERENT step must not be read as this step's ending.
            StepRun(
                ticket_id=ticket.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=9,
                status=StepRunStatus.SUCCEEDED,
            ),
        ]
    )
    await async_session.flush()

    body = (await client.get("/api/orgs/default/overview", headers=auth)).json()
    (row,) = [t for t in body["tickets"] if t["project_id"] == project.id]
    assert row["status"] == "blocked"
    assert row["latest_run_status"] == "interrupted"

    # The project table's rows carry the same refinement, from the same subquery.
    listing = (await client.get(f"/api/projects/{project.id}/tickets", headers=auth)).json()
    (listed,) = [t for t in listing["tickets"] if t["id"] == ticket.id]
    assert listed["status"] == "blocked"
    assert listed["latest_run_status"] == "interrupted"


@pytest.mark.postgres
async def test_tickets_say_why_the_current_run_was_parked(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """An idle park and a human's stop both leave the run `interrupted` and the ticket `blocked`;
    only the park event tells "waiting on a human" from "a human stopped it"."""
    project = await seed_project(async_session)

    async def parked(
        *events: tuple[EventType, dict[str, Any]],
        status: str = TicketStatus.BLOCKED,
        run_status: str = StepRunStatus.INTERRUPTED,
    ) -> int:
        ticket = await seed_ticket(async_session, project, step="develop", status=status)
        run = StepRun(
            ticket_id=ticket.id,
            step_name="develop",
            kind=StepKind.AGENT,
            attempt=1,
            status=run_status,
        )
        async_session.add(run)
        await async_session.flush()
        for type_, details in events:
            await append(
                async_session,
                type=type_,
                project_id=project.id,
                ticket_id=ticket.id,
                step_run_id=run.id,
                **details,
            )
        return ticket.id

    # Stopped by a human, resumed, then parked idle: the latest park is the one that holds.
    idle = await parked(
        (EventType.AGENT_INTERRUPTED, {"step": "develop"}),
        (EventType.AGENT_RESUMED, {"step": "develop"}),
        (EventType.AGENT_PARKED, {"step": "develop", "reason": "idle"}),
    )
    stopped = await parked(
        (EventType.AGENT_PARKED, {"step": "develop", "reason": "idle"}),
        (EventType.AGENT_RESUMED, {"step": "develop"}),
        (EventType.AGENT_INTERRUPTED, {"step": "develop"}),
    )
    exited = await parked((EventType.AGENT_PARKED, {"step": "develop", "reason": "exited"}))
    escalated = await seed_ticket(
        async_session, project, step="develop", status=TicketStatus.BLOCKED
    )
    # A park is history once the run moved on: resumed, then failed until it escalated, or
    # resumed and running again.
    idle_then_failed = await parked(
        (EventType.AGENT_PARKED, {"step": "develop", "reason": "idle"}),
        (EventType.AGENT_RESUMED, {"step": "develop"}),
        run_status=StepRunStatus.FAILED,
    )
    idle_then_running = await parked(
        (EventType.AGENT_PARKED, {"step": "develop", "reason": "idle"}),
        (EventType.AGENT_RESUMED, {"step": "develop"}),
        status=TicketStatus.RUNNING,
        run_status=StepRunStatus.RUNNING,
    )
    await async_session.flush()

    expected = {
        idle: "idle",
        stopped: "user",
        exited: "exited",
        escalated.id: None,
        idle_then_failed: None,
        idle_then_running: None,
    }
    body = (await client.get("/api/orgs/default/overview", headers=auth)).json()
    overview = {t["ticket_id"]: t for t in body["tickets"] if t["project_id"] == project.id}
    assert {k: overview[k]["park_reason"] for k in expected} == expected

    listing = (await client.get(f"/api/projects/{project.id}/tickets", headers=auth)).json()
    listed = {t["id"]: t for t in listing["tickets"]}
    assert {k: listed[k]["park_reason"] for k in expected} == expected

    pages = {k: (await client.get(f"/api/tickets/{k}", headers=auth)).json() for k in expected}
    assert {k: page["park_reason"] for k, page in pages.items()} == expected

    rows, _ = await select_tickets(
        async_session,
        organization_id=project.organization_id,
        project_ids=[project.id],
        state="blocked",
        closed_since=datetime.now(UTC) - timedelta(days=7),
        limit=50,
    )
    (row,) = [r for r in rows if r.id == exited]
    blocking = blocked_on(row)
    assert blocking == "interrupted"
    assert NEXT_ACTION[blocking] == "factorial_ticket_resume"


@pytest.mark.postgres
async def test_event_feed_paginates_on_id(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    written = [
        (
            await append(
                async_session,
                type=EventType.STEP_STARTED,
                project_id=project.id,
                ticket_id=ticket.id,
            )
        ).id
        for _ in range(5)
    ]

    first = (await client.get(f"/api/projects/{project.id}/events?limit=2", headers=auth)).json()
    assert [e["id"] for e in first["events"]] == written[:2]
    assert first["next_after_id"] == written[1]

    rest = (
        await client.get(
            f"/api/projects/{project.id}/events?after_id={first['next_after_id']}", headers=auth
        )
    ).json()
    assert [e["id"] for e in rest["events"]] == written[2:]

    drained = (
        await client.get(
            f"/api/projects/{project.id}/events?after_id={rest['next_after_id']}", headers=auth
        )
    ).json()
    assert drained["events"] == []
    assert drained["next_after_id"] is None


@pytest.mark.postgres
async def test_event_feed_filters_by_ticket_external_key(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """`?ticket=` takes the external key — the issue number the operator holds — and narrows the
    feed to that ticket. Composes with the cursor; an unknown key is a 404, never an empty feed."""
    project = await seed_project(async_session)
    ticket = await seed_ticket(async_session, project)
    other = await seed_ticket(async_session, project)
    mine = [
        (
            await append(
                async_session,
                type=EventType.STEP_STARTED,
                project_id=project.id,
                ticket_id=ticket.id,
            )
        ).id
        for _ in range(3)
    ]
    # Neither the other ticket's events nor project-level ones may bleed into the filtered feed.
    await append(
        async_session, type=EventType.STEP_STARTED, project_id=project.id, ticket_id=other.id
    )
    await append(async_session, type=EventType.WEBHOOK_RECEIVED, project_id=project.id)

    base = f"/api/projects/{project.id}/events"
    body = (await client.get(base, params={"ticket": ticket.external_key}, headers=auth)).json()
    assert [e["id"] for e in body["events"]] == mine
    assert body["ticket"] == ticket.external_key

    paged = (
        await client.get(
            base,
            params={"ticket": ticket.external_key, "after_id": mine[0], "limit": 1},
            headers=auth,
        )
    ).json()
    assert [e["id"] for e in paged["events"]] == [mine[1]]
    assert paged["next_after_id"] == mine[1]

    unfiltered = (await client.get(base, headers=auth)).json()
    assert unfiltered["ticket"] is None
    assert len(unfiltered["events"]) == 5

    unknown = await client.get(base, params={"ticket": "999999"}, headers=auth)
    assert unknown.status_code == 404
    assert "999999" in unknown.json()["detail"]


@pytest.mark.postgres
async def test_event_writer_refuses_credential_shaped_details(
    async_session: AsyncSession,
) -> None:
    """`details` is the audit trail; design.md §7.1 says injected values are never logged."""
    project = await seed_project(async_session)
    with pytest.raises(UnsafeEventDetails):
        await append(
            async_session,
            type=EventType.AGENT_SPAWNED,
            project_id=project.id,
            gh_token="ghp_realish",
        )
    with pytest.raises(UnsafeEventDetails):
        await append(
            async_session,
            type=EventType.AGENT_SPAWNED,
            project_id=project.id,
            spawn={"env": {"password": "hunter2"}},
        )
    # `external_key` and token *counts* are legitimate details, not credentials — the cache
    # split and the cost label the completion carries included.
    ok = await append(
        async_session,
        type=EventType.AGENT_COMPLETED,
        project_id=project.id,
        external_key="42",
        input_tokens=10,
        cached_input_tokens=4,
        cost_source="estimated",
        model="gpt-5.3-codex",
    )
    assert ok.id > 0


# -------------------------------------------------------------------------------- webhooks


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def _project_url(project: Project) -> str:
    return f"/webhooks/github/projects/{project.id}"


def _app_url(app_id: str = APP_ID) -> str:
    return f"/webhooks/github/apps/{app_id}"


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def event_count(session: AsyncSession, project_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Event).where(Event.project_id == project_id)
            )
        ).scalar_one()
    )


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_with_a_bad_signature_writes_nothing(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session)
    body = json.dumps({"repository": {"full_name": project.github_repo}}).encode()

    response = await client.post(
        _project_url(project),
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, "wrong-secret"),
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401
    assert await event_count(async_session, project.id) == 0

    unsigned = await client.post(_project_url(project), content=body)
    assert unsigned.status_code == 401
    assert await event_count(async_session, project.id) == 0


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_is_only_a_wake_up_signal(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """202 + exactly one `webhook_received` event + NOT ONE byte of ticket state changed
    (design.md §2.1: polling is authoritative, the webhook only shortens latency)."""
    project = await seed_connected(async_session)
    ticket = await seed_ticket(async_session, project, step="develop")
    before = (ticket.current_step, ticket.status, ticket.updated_at)

    body = json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": project.github_repo},
            "issue": {"number": 7, "labels": [{"name": "factorial:done"}]},
        }
    ).encode()
    response = await client.post(
        _project_url(project),
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body),
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "d-1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202

    events = (
        (
            await async_session.execute(
                select(Event).where(Event.project_id == project.id).order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )
    assert [e.type for e in events] == ["webhook_received"]
    assert events[0].ticket_id is None  # the receiver does not resolve a ticket
    assert events[0].details["delivery"] == "d-1"

    await async_session.refresh(ticket)
    assert (ticket.current_step, ticket.status, ticket.updated_at) == before
    assert (
        await async_session.execute(
            select(func.count()).select_from(StepRun).where(StepRun.ticket_id == ticket.id)
        )
    ).scalar_one() == 0


_ISSUE = 7


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
@pytest.mark.parametrize(
    ("event", "fields", "wakes_ticket"),
    [
        ("issues", {"action": "labeled", "issue": {"number": _ISSUE}}, True),
        ("issue_comment", {"action": "created", "issue": {"number": _ISSUE}}, True),
        ("pull_request", {"pull_request": {"head": {"ref": f"impl/{_ISSUE}-thing"}}}, True),
        ("pull_request_review", {"pull_request": {"head": {"ref": f"spec/{_ISSUE}-x"}}}, True),
        ("push", {"ref": f"refs/heads/spec/{_ISSUE}-thing"}, True),
        ("check_suite", {"check_suite": {"head_branch": f"impl/{_ISSUE}"}}, True),
        # A comment on a pull request names the PR's number, not the ticket's issue.
        (
            "issue_comment",
            {"issue": {"number": _ISSUE, "pull_request": {"url": "u"}}},
            False,
        ),
        ("push", {"ref": f"refs/heads/feature/{_ISSUE}a"}, False),
        ("push", {"ref": "refs/heads/main"}, False),
        ("push", {"ref": f"refs/tags/v{_ISSUE}"}, False),
        ("pull_request", {"pull_request": {"head": {"ref": "spec/70-other"}}}, False),
    ],
)
async def test_webhook_wakes_the_ticket_it_names_and_the_fleet_otherwise(
    client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    fields: dict[str, Any],
    wakes_ticket: bool,
) -> None:
    """A ticket wake is served at once and is not held to the fleet pass's gap, so a wrong
    mapping either slows every reaction to the gap or wakes a ticket the delivery is not about."""
    from orchestrator.api.routes import webhooks

    project = await seed_connected(async_session)
    ticket = await seed_ticket(async_session, project, external_key=str(_ISSUE))
    woken: list[object] = []

    async def ticket_wake(session: AsyncSession, ticket_id: int) -> None:
        woken.append(("ticket", ticket_id))

    async def fleet_wake(session: AsyncSession, **_: object) -> None:
        woken.append(("fleet",))

    monkeypatch.setattr(webhooks, "wake_ticket", ticket_wake)
    monkeypatch.setattr(webhooks, "wake_fleet", fleet_wake)

    body = json.dumps({"repository": {"full_name": project.github_repo}, **fields}).encode()
    response = await client.post(
        _project_url(project),
        content=body,
        headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": event},
    )
    assert response.status_code == 202
    assert woken == ([("ticket", ticket.id)] if wakes_ticket else [("fleet",)])


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
@pytest.mark.parametrize(
    ("event", "fields", "seeded"),
    [
        ("issues", {"action": "opened", "issue": {"number": _ISSUE}}, None),
        ("issues", {"action": "labeled", "issue": {"number": _ISSUE}}, None),
        ("issue_comment", {"action": "created", "issue": {"number": _ISSUE}}, None),
        # A done ticket is never polled again; admission sees its key and leaves it.
        ("issues", {"action": "reopened", "issue": {"number": _ISSUE}}, TicketStatus.DONE),
    ],
)
async def test_webhook_asks_admission_about_an_issue_that_is_no_open_ticket(
    client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    fields: dict[str, Any],
    seeded: TicketStatus | None,
) -> None:
    """A fleet pass waits out `worker.min_pass_gap_seconds`, so a new issue's delivery that
    woke the fleet waited up to 30 s to be admitted (#476). It asks about that issue alone."""
    woken = _record_wakes(monkeypatch)
    project = await seed_connected(async_session)
    if seeded is not None:
        await seed_ticket(async_session, project, external_key=str(_ISSUE), status=seeded)

    await _deliver(
        client,
        _project_url(project),
        event,
        {"repository": {"full_name": project.github_repo}, **fields},
    )

    assert woken == [("admit", project.id, str(_ISSUE))]
    [received] = await _webhooks(async_session, project.id)
    assert (received.ticket_id, received.details["woke"]) == (None, "admission")


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
@pytest.mark.parametrize(
    ("event", "action"),
    [("issues", "deleted"), ("issues", "transferred"), ("issue_comment", "deleted")],
)
async def test_webhook_does_not_ask_admission_about_an_issue_that_is_gone(
    client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    action: str,
) -> None:
    """Reading a deleted or transferred issue 404s, and admission logs that as a failure with a
    traceback: a routine deletion in a tenant's repo would read as a broken project."""
    woken = _record_wakes(monkeypatch)
    project = await seed_connected(async_session)

    await _deliver(
        client,
        _project_url(project),
        event,
        {
            "action": action,
            "repository": {"full_name": project.github_repo},
            "issue": {"number": _ISSUE},
        },
    )

    assert woken == [("fleet",)]


async def _deliver(
    client: AsyncClient,
    url: str,
    event: str,
    payload: dict[str, Any],
    delivery: str | None = None,
) -> None:
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": sign(body), "X-GitHub-Event": event}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    response = await client.post(url, content=body, headers=headers)
    assert response.status_code == 202


async def _webhooks(session: AsyncSession, project_id: int) -> list[Event]:
    return list(
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.type == "webhook_received")
                .order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )


def _record_wakes(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    from orchestrator.api.routes import webhooks

    woken: list[object] = []

    async def ticket_wake(session: AsyncSession, ticket_id: int) -> None:
        woken.append(("ticket", ticket_id))

    async def fleet_wake(session: AsyncSession, **_: object) -> None:
        woken.append(("fleet",))

    async def admission_wake(session: AsyncSession, *, project_id: int, issue: str) -> None:
        woken.append(("admit", project_id, issue))

    monkeypatch.setattr(webhooks, "wake_ticket", ticket_wake)
    monkeypatch.setattr(webhooks, "wake_fleet", fleet_wake)
    monkeypatch.setattr(webhooks, "wake_admission", admission_wake)
    return woken


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_records_what_the_delivery_was_about_and_never_its_text(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    project = await seed_connected(async_session)
    ticket = await seed_ticket(async_session, project, external_key=str(_ISSUE))
    await _deliver(
        client,
        _project_url(project),
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": project.github_repo},
            "sender": {"login": "operator"},
            "pull_request": {
                "number": 42,
                "merged": True,
                "title": "TITLE-TEXT",
                "body": "BODY-TEXT",
                "head": {"ref": f"spec/{_ISSUE}-auth"},
            },
        },
        delivery="d-42",
    )
    [event] = await _webhooks(async_session, project.id)
    assert event.ticket_id == ticket.id
    assert event.details == {
        "repo": project.github_repo,
        "github_event": "pull_request",
        "delivery": "d-42",
        "woke": "ticket",
        "action": "closed",
        "sender": "operator",
        "number": 42,
        "branch": f"spec/{_ISSUE}-auth",
        "merged": True,
    }


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_reaches_every_project_on_the_repo_whatever_its_case(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub names a repo in its own case and nothing stops two projects naming one repo: a
    delivery that matched only an exact spelling, or only the first project, was dropped for
    the rest without a record or a wake."""
    woken = _record_wakes(monkeypatch)
    tag = uuid4().hex[:8]
    first = await seed_connected(async_session, f"Acme/Shared-{tag}", app_id=APP_ID)
    second = await seed_connected(async_session, f"acme/shared-{tag}", app_id=APP_ID)
    tickets = [
        await seed_ticket(async_session, project, external_key=str(_ISSUE))
        for project in (first, second)
    ]
    await _deliver(
        client,
        _app_url(),
        "issues",
        {
            "action": "labeled",
            "repository": {"full_name": f"ACME/shared-{tag}"},
            "issue": {"number": _ISSUE},
        },
    )
    for project, ticket in zip((first, second), tickets, strict=True):
        [event] = await _webhooks(async_session, project.id)
        assert event.ticket_id == ticket.id
    assert woken == [("ticket", tickets[0].id), ("ticket", tickets[1].id)]


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_reads_an_issue_number_only_in_the_repo_issues_come_from(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issues from one repo, PRs in another: the artifact repo's issue #7 is not ticket #7, but
    its branch `spec/7-…` is."""
    woken = _record_wakes(monkeypatch)
    project = await seed_connected(async_session)
    ticket = await seed_ticket(async_session, project, external_key=str(_ISSUE))
    source = (
        await async_session.execute(
            select(TicketSource).where(TicketSource.project_id == project.id)
        )
    ).scalar_one()
    source.external_ref = f"acme/issues-{uuid4().hex[:8]}"
    await async_session.flush()
    repo = {"full_name": project.github_repo}

    url = _project_url(project)
    await _deliver(client, url, "issues", {"repository": repo, "issue": {"number": _ISSUE}})
    await _deliver(
        client,
        url,
        "pull_request",
        {"repository": repo, "pull_request": {"head": {"ref": "spec/7-x"}}},
    )

    assert [event.ticket_id for event in await _webhooks(async_session, project.id)] == [
        None,
        ticket.id,
    ]
    assert woken == [("fleet",), ("ticket", ticket.id)]


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_webhook_for_an_unknown_repo_is_accepted_but_silent(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session)
    body = json.dumps({"repository": {"full_name": "nobody/nothing"}}).encode()
    before = (await async_session.execute(select(func.count()).select_from(Event))).scalar_one()
    response = await client.post(
        _project_url(project), content=body, headers={"X-Hub-Signature-256": sign(body)}
    )
    assert response.status_code == 202
    after = (await async_session.execute(select(func.count()).select_from(Event))).scalar_one()
    assert after == before


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
@pytest.mark.parametrize(
    "event", ["ping", "installation", "installation_repositories", "github_app_authorization"]
)
async def test_a_ping_or_installation_delivery_is_accepted_and_writes_nothing(
    client: AsyncClient, async_session: AsyncSession, event: str
) -> None:
    """The App's own deliveries name no repository to wake. GitHub marks anything but a 2xx as
    a failed delivery, and an event for one would be a wake for nothing."""
    project = await seed_connected(async_session)
    # A repository a project reacts to, so only the event name keeps this from being recorded.
    body = json.dumps(
        {"zen": "Keep it logically awesome.", "repository": {"full_name": project.github_repo}}
    ).encode()
    response = await client.post(
        _project_url(project),
        content=body,
        headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": event},
    )
    assert response.status_code == 202
    assert await event_count(async_session, project.id) == 0


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_an_oversized_webhook_with_no_declared_length_is_refused_as_it_streams(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """The route is open to the internet and checks nothing until it has the whole body. Were it
    read whole first, one large chunked request could fill the task's memory and kill every host
    run in it."""
    project = await seed_connected(async_session)
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(64):
            sent += 1
            yield b"x" * (1024 * 1024)

    response = await client.post(
        _project_url(project), content=chunks(), headers={"X-GitHub-Event": "push"}
    )
    assert response.status_code == 413
    assert sent < 64


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
@pytest.mark.parametrize("secret", [None, ""])
async def test_a_project_with_no_webhook_secret_verifies_nothing(
    client: AsyncClient, async_session: AsyncSession, secret: str | None
) -> None:
    """An unset or empty secret is no way to verify: the delivery is refused, not trusted, and
    nothing is recorded from it."""
    project = await seed_connected(async_session, secret=secret)
    body = json.dumps({"repository": {"full_name": project.github_repo}}).encode()
    response = await client.post(
        _project_url(project), content=body, headers={"X-Hub-Signature-256": sign(body, "")}
    )
    assert response.status_code == 401
    assert await event_count(async_session, project.id) == 0


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_a_project_url_verifies_with_that_project_s_secret(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    mine = await seed_connected(async_session, secret="mine")
    other = await seed_connected(async_session, mine.github_repo, secret="other")
    body = json.dumps({"repository": {"full_name": mine.github_repo}}).encode()
    headers = {"X-GitHub-Event": "issues"}

    wrong = await client.post(
        _project_url(mine),
        content=body,
        headers={**headers, "X-Hub-Signature-256": sign(body, "other")},
    )
    assert wrong.status_code == 401
    assert await _webhooks(async_session, mine.id) == []

    right = await client.post(
        _project_url(mine),
        content=body,
        headers={**headers, "X-Hub-Signature-256": sign(body, "mine")},
    )
    assert right.status_code == 202
    assert len(await _webhooks(async_session, mine.id)) == 1
    # Another project on the same repo is not reached through this project's URL.
    assert await _webhooks(async_session, other.id) == []


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_a_project_url_answers_missing_unconnected_and_bad_signatures_alike(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Anyone can POST here: a 404 for a project with no connection told a stranger which ids
    have one."""
    unconnected = await seed_project(async_session)
    connected = await seed_connected(async_session)
    body = b"{}"
    answers = {
        (response.status_code, response.json()["detail"])
        for url in (
            _project_url(unconnected),
            "/webhooks/github/projects/999999999",
            _project_url(connected),
        )
        for response in [
            await client.post(
                url, content=body, headers={"X-Hub-Signature-256": sign(body, "wrong")}
            )
        ]
    }
    assert answers == {(401, "bad webhook signature")}


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_a_signature_header_outside_ascii_is_a_401_not_a_500(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session)
    body = json.dumps({"repository": {"full_name": project.github_repo}}).encode()
    response = await client.post(
        _project_url(project),
        content=body,
        headers={b"X-Hub-Signature-256": "sha256=\xff".encode("latin-1")},
    )
    assert response.status_code == 401


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_a_loop_of_bad_deliveries_rewrites_the_failure_stamp_once_a_minute(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session)
    await seed_ticket(async_session, project)  # creates the ticket_source row
    await async_session.commit()
    body = b"{}"
    source = select(TicketSource.webhook_failed_at).where(TicketSource.project_id == project.id)

    stamps = []
    for _ in range(2):
        response = await client.post(
            _project_url(project), content=body, headers={"X-Hub-Signature-256": sign(body, "x")}
        )
        assert response.status_code == 401
        stamps.append(await async_session.scalar(source))
    assert stamps[0] is not None
    assert stamps[1] == stamps[0]


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_an_app_url_never_stamps_a_project_with_no_secret_as_failing(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """With no secret nothing could have verified: the stamp would only be a stranger's."""
    repo = f"acme/secretless-{uuid4().hex[:8]}"
    project = await seed_connected(async_session, repo, app_id=APP_ID, secret=None)
    await seed_ticket(async_session, project)
    await async_session.commit()
    body = json.dumps({"repository": {"full_name": repo}}).encode()

    response = await client.post(
        _app_url(), content=body, headers={"X-Hub-Signature-256": sign(body, "x")}
    )

    assert response.status_code == 401
    stamp = await async_session.scalar(
        select(TicketSource.webhook_failed_at).where(TicketSource.project_id == project.id)
    )
    assert stamp is None


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_an_app_url_wakes_only_projects_whose_secret_verifies(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One App can serve two organizations with different secrets; a delivery signed for one
    must not wake, or even be recorded by, the other."""
    _record_wakes(monkeypatch)
    repo = f"acme/shared-{uuid4().hex[:8]}"
    theirs = await seed_connected(async_session, repo, app_id=APP_ID, secret="theirs")
    ours = await seed_connected(async_session, repo, app_id=APP_ID, secret="ours")
    elsewhere = await seed_connected(async_session, repo, app_id="999", secret="ours")
    body = json.dumps({"repository": {"full_name": repo}}).encode()

    response = await client.post(
        _app_url(),
        content=body,
        headers={"X-Hub-Signature-256": sign(body, "ours"), "X-GitHub-Event": "issues"},
    )
    assert response.status_code == 202
    assert len(await _webhooks(async_session, ours.id)) == 1
    assert await _webhooks(async_session, theirs.id) == []
    assert await _webhooks(async_session, elsewhere.id) == []


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_an_app_url_with_no_verifying_secret_is_401(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session, app_id=APP_ID, secret="ours")
    body = json.dumps({"repository": {"full_name": project.github_repo}}).encode()
    response = await client.post(
        _app_url(), content=body, headers={"X-Hub-Signature-256": sign(body, "nope")}
    )
    assert response.status_code == 401
    assert await event_count(async_session, project.id) == 0

    nobody = json.dumps({"repository": {"full_name": "nobody/nothing"}}).encode()
    silent = await client.post(
        _app_url(), content=nobody, headers={"X-Hub-Signature-256": sign(nobody, "nope")}
    )
    assert silent.status_code == 202


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_deeply_nested_json_on_the_app_url_is_a_client_error(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """The App route parses before any signature is checked, so a body that recurses past the
    parser's limit must not become a 500 for an anonymous caller."""
    before = await _count(async_session, Event)
    response = await client.post(
        _app_url(), content=b"[" * 200_000, headers={"X-Hub-Signature-256": sign(b"")}
    )
    assert 400 <= response.status_code < 500
    assert await _count(async_session, Event) == before


@pytest.mark.postgres
async def test_the_old_shared_url_is_gone(client: AsyncClient) -> None:
    body = b"{}"
    response = await client.post(
        "/webhooks/github", content=body, headers={"X-Hub-Signature-256": sign(body)}
    )
    assert response.status_code in (404, 405)


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_delivery_outcomes_are_stamped_per_project_without_new_rows(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    repo = f"acme/shared-{uuid4().hex[:8]}"
    good = await seed_connected(async_session, repo, app_id=APP_ID, secret="good")
    bad = await seed_connected(async_session, repo, app_id=APP_ID, secret="bad")
    for project in (good, bad):
        await seed_ticket(async_session, project)  # creates the ticket_source row
    sources = (
        select(TicketSource).where(TicketSource.project_id.in_([good.id, bad.id]))
    )
    rows_before = await _count(async_session, TicketSource)
    events_before = await _count(async_session, Event)
    body = json.dumps({"repository": {"full_name": repo}}).encode()

    response = await client.post(
        _app_url(),
        content=body,
        headers={"X-Hub-Signature-256": sign(body, "good"), "X-GitHub-Event": "issues"},
    )
    assert response.status_code == 202

    result = await async_session.execute(sources.execution_options(populate_existing=True))
    stamps = {row.project_id: row for row in result.scalars()}
    assert stamps[good.id].webhook_verified_at is not None
    assert stamps[good.id].webhook_failed_at is None
    assert stamps[bad.id].webhook_failed_at is not None
    assert stamps[bad.id].webhook_verified_at is None
    assert await _count(async_session, TicketSource) == rows_before
    # Only the verified project's delivery is recorded.
    assert await _count(async_session, Event) == events_before + 1

    # A delivery that fails for everyone records nothing at all.
    refused = await client.post(
        _app_url(), content=body, headers={"X-Hub-Signature-256": sign(body, "zzz")}
    )
    assert refused.status_code == 401
    assert await _count(async_session, Event) == events_before + 1




def test_project_repo_accepts_nested_gitlab_path() -> None:
    """Nested groups are a GitLab path. GitHub stays owner/name."""
    from pydantic import ValidationError

    from orchestrator.api.schemas import ProjectIn, TicketSourceIn

    nested = "acme/platform/tools/agent-workshop"
    gitlab = {"forge": "gitlab", "gitlab": {"base_url": "https://gitlab.example"}}
    assert (
        ProjectIn(name="license-renewal", github_repo=nested, settings=gitlab).github_repo
        == nested
    )
    assert TicketSourceIn(kind="gitlab", external_ref=nested).external_ref == nested
    with pytest.raises(ValidationError):
        ProjectIn(name="x", github_repo=nested)
    with pytest.raises(ValidationError):
        TicketSourceIn(kind="github", external_ref=nested)
    with pytest.raises(ValidationError):
        ProjectIn(name="x", github_repo="nopath")


# ----------------------------------------------------------------------------------- admin


@pytest.mark.postgres
async def test_create_and_list_projects(client: AsyncClient, auth: dict[str, str]) -> None:
    name = f"proj-{uuid4().hex[:8]}"
    created = await client.post(
        "/api/orgs/default/projects",
        headers=auth,
        json={
            "name": name,
            "github_repo": "acme/widgets",
            "description": "demo",
            "settings": {"agent_runtime": "claude"},
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["settings"] == {"agent_runtime": "claude"}

    duplicate = await client.post(
        "/api/orgs/default/projects",
        headers=auth,
        json={"name": name, "github_repo": "acme/widgets"},
    )
    assert duplicate.status_code == 409

    listed = await client.get("/api/orgs/default/projects", headers=auth)
    assert name in [p["name"] for p in listed.json()]


@pytest.mark.postgres
async def test_a_new_project_is_inactive_and_says_why(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The wizard's contract: created inactive, with the remaining steps spelled out."""
    created = await client.post(
        "/api/orgs/default/projects",
        headers=auth,
        json={"name": f"proj-{uuid4().hex[:8]}", "github_repo": "acme/widgets"},
    )
    assert created.status_code == 201
    assert created.json()["active"] is False
    project_id = created.json()["id"]

    detail = (await client.get(f"/api/projects/{project_id}", headers=auth)).json()
    assert detail["ticket_source"] is None
    assert detail["processes"] == []
    assert detail["default_process"] is None
    assert detail["ticket_count"] == 0
    assert len(detail["blockers"]) == 3

    refused = await client.put(
        f"/api/projects/{project_id}/active", headers=auth, json={"active": True}
    )
    assert refused.status_code == 409
    assert "not ready to activate" in refused.json()["detail"]
    assert refused.json()["code"] == "not_ready"


@pytest.mark.postgres
async def test_exited_tickets_are_counted_apart_from_done(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """`open_ticket_count` filters on `closed_at`, and an exit sets it. Without a count of its
    own, the caller's `ticket_count - open_ticket_count` reads every exited ticket as done."""
    project = await seed_project(async_session)
    for status in (TicketStatus.DONE, TicketStatus.EXITED):
        ticket = await seed_ticket(async_session, project, status=status)
        ticket.closed_at = datetime.now(UTC)
    await async_session.flush()

    detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()

    assert detail["ticket_count"] == 2
    assert detail["open_ticket_count"] == 0
    assert detail["exited_ticket_count"] == 1


@pytest.mark.postgres
async def test_activation_needs_a_ticket_source_and_a_process_version(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_connected(async_session, repo="acme/widgets")
    base = f"/api/projects/{project.id}"

    source = await client.put(
        f"{base}/ticket-source", headers=auth, json={"external_ref": "acme/widgets"}
    )
    assert source.status_code == 200
    assert source.json()["kind"] == "github"

    # One source per project: a second PUT replaces rather than duplicates.
    moved = await client.put(
        f"{base}/ticket-source",
        headers=auth,
        json={"external_ref": "acme/other", "webhook_secret_ref": "WEBHOOK_SECRET"},
    )
    assert moved.status_code == 200
    assert moved.json()["id"] == source.json()["id"]
    assert moved.json()["external_ref"] == "acme/other"

    # Still one blocker — the process version.
    still_blocked = await client.put(f"{base}/active", headers=auth, json={"active": True})
    assert still_blocked.status_code == 409
    assert "No process yet" in still_blocked.json()["detail"]
    assert still_blocked.json()["code"] == "not_ready"

    # Snapshots come from a sync, which needs GitHub; this test is about activation, so the row
    # is seeded directly with the sha a sync would have recorded.
    sample = load_process(SAMPLE_PROCESS)
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            process_key=sample.name,
            content=sample.model_dump(mode="json", by_alias=True),
            source_commit_sha="f" * 40,
        )
    )
    await async_session.flush()

    activated = await client.put(f"{base}/active", headers=auth, json={"active": True})
    assert activated.status_code == 200
    assert activated.json()["active"] is True

    detail = (await client.get(base, headers=auth)).json()
    assert detail["blockers"] == []
    assert [(p["process_key"], p["latest"]["source_commit_sha"]) for p in detail["processes"]] == [
        (sample.name, "f" * 40)
    ]

    # Deactivating is always allowed — it only stops admission.
    paused = await client.put(f"{base}/active", headers=auth, json={"active": False})
    assert paused.json()["active"] is False


# ------------------------------------------------------------------ several lanes per project


@pytest.mark.postgres
async def test_sync_registers_every_lane_at_one_sha(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A process and its agents move together or not at all, and so do the lanes: one resolved
    sha covers them all, which is also what makes `unchanged` meaningful."""
    project = await seed_project(async_session)
    await async_session.flush()
    app = probe_app(async_session, files=lane_files("standard", "hotfix"))
    async with client_for(app) as client:
        first = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["sha"] == MINI_SHA
        assert sorted((r["process"], r["outcome"]) for r in body["results"]) == [
            ("hotfix", "created"),
            ("standard", "created"),
        ]

        # Editing one lane must leave the other at a version number that already exists without
        # 409ing the whole call — the outcome the route needs to be usable at all.
        again = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        assert again.status_code == 201
        assert {r["outcome"] for r in again.json()["results"]} == {"unchanged"}

        rows = (await client.get(f"/api/projects/{project.id}/processes", headers=auth)).json()
    assert [(r["process_key"], r["latest"]["source_commit_sha"]) for r in rows] == [
        ("hotfix", MINI_SHA),
        ("standard", MINI_SHA),
    ]
    assert [r["selection_label"] for r in rows] == [
        "factorial:process:hotfix",
        "factorial:process:standard",
    ]
    assert not any(r["is_default"] for r in rows)  # nothing is default until an operator says so


@pytest.mark.postgres
async def test_setting_the_default_renames_and_retires_nothing(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Promotion is a settings write: both lanes keep their version series (design.md §4)."""
    project = await seed_project(async_session)
    await async_session.flush()
    app = probe_app(async_session, files=lane_files("standard", "hotfix"))
    async with client_for(app) as client:
        await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        set_default = await client.put(
            f"/api/projects/{project.id}/default-process",
            headers=auth,
            json={"process_key": "standard"},
        )
        assert set_default.status_code == 200
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["default_process"] == "standard"
        assert [(r["process_key"], r["is_default"]) for r in detail["processes"]] == [
            ("hotfix", False),
            ("standard", True),
        ]

        unknown = await client.put(
            f"/api/projects/{project.id}/default-process",
            headers=auth,
            json={"process_key": "nope"},
        )
        assert unknown.status_code == 422
        assert "nope" in unknown.json()["detail"]

        cleared = await client.put(
            f"/api/projects/{project.id}/default-process", headers=auth, json={"process_key": None}
        )
        assert cleared.status_code == 200
        assert (await client.get(f"/api/projects/{project.id}", headers=auth)).json()[
            "default_process"
        ] is None


@pytest.mark.postgres
async def test_a_sync_that_retires_the_default_moves_it_and_says_so(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A project whose default names nothing admits no issue that does not label itself, so the
    sync re-points it. Refusing asked for a settings edit this API will not accept either: the
    default may only name a lane the project offers, and the commit has just retired this one."""
    project = await seed_project(async_session)
    await async_session.flush()
    async with client_for(
        probe_app(async_session, files=lane_files("standard", "hotfix"))
    ) as client:
        await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        await client.put(
            f"/api/projects/{project.id}/default-process",
            headers=auth,
            json={"process_key": "standard"},
        )
    async with client_for(probe_app(async_session, files=lane_files("hotfix"))) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        lanes = (await client.get(f"/api/projects/{project.id}/processes", headers=auth)).json()

    assert response.status_code == 201
    assert response.json()["default_process_moved"] == {"previous": "standard", "now": "hotfix"}
    assert [(lane["process_key"], lane["is_default"]) for lane in lanes] == [("hotfix", True)]


class _GoneBranch(ProbeContents):
    """Answers an unknown ref the way `RepoContents` does for a branch GitHub no longer has."""

    async def resolve_ref(self, repo: str, ref: str) -> str:
        raise ContentsNotFound(f"no branch, tag or commit named {ref!r}")


@pytest.mark.postgres
async def test_syncing_a_branch_that_is_gone_is_refused_by_name(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The form offers the branch the last sync read, which may be deleted since. That answered
    a 500; the operator needs the name of the ref to clear."""
    project = await seed_project(async_session)
    await async_session.flush()
    app = probe_app(async_session, files=lane_files("standard"))
    source = RepoConfigSource(_GoneBranch(lane_files("standard")))
    app.dependency_overrides[config_source_dependency] = lambda: source
    async with client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync",
            headers=auth,
            json={"ref": "claude-subscription-canary"},
        )
    assert response.status_code == 422, response.text
    assert "claude-subscription-canary" in response.json()["detail"]


@pytest.mark.postgres
async def test_a_lane_deleted_from_the_repo_is_retired_and_stops_being_offered(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    await async_session.flush()
    async with client_for(
        probe_app(async_session, files=lane_files("standard", "hotfix"))
    ) as client:
        await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    async with client_for(probe_app(async_session, files=lane_files("standard"))) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        assert ("hotfix", "retired") in [
            (r["process"], r["outcome"]) for r in response.json()["results"]
        ]
        offered = (await client.get(f"/api/projects/{project.id}/processes", headers=auth)).json()
        assert [r["process_key"] for r in offered] == ["standard"]
        # History stays readable; `retired_at` is what says the lane is not selectable.
        history = (
            await client.get(f"/api/projects/{project.id}/process-versions", headers=auth)
        ).json()
    retired = [r for r in history if r["process_key"] == "hotfix"]
    assert len(retired) == 1
    assert retired[0]["retired_at"] is not None


@pytest.mark.postgres
async def test_two_lanes_at_one_commit_are_addressed_by_row_id(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """One sync writes every lane at the same commit, so the sha alone does not address a row
    either — which is why every snapshot route keys on the row's own id."""
    project = await seed_project(async_session)
    await async_session.flush()
    app = probe_app(async_session, files=lane_files("standard", "hotfix"))
    async with client_for(app) as client:
        results = (
            await client.post(
                f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
            )
        ).json()["results"]
        by_key = {r["process"]: r["version_id"] for r in results}
        assert len(set(by_key.values())) == 2
        for key, version_id in by_key.items():
            body = (
                await client.get(
                    f"/api/projects/{project.id}/process-versions/{version_id}", headers=auth
                )
            ).json()
            assert (body["process_key"], body["source_commit_sha"]) == (key, MINI_SHA)
            assert body["content"]["name"] == key


@pytest.mark.postgres
async def test_the_pending_listing_is_empty_without_a_configured_app(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The fail-open contract the optional config source uses: a diagnostic read answers with
    nothing rather than a 503."""
    project = await seed_probe_project(async_session)
    async with client_for(probe_app(async_session)) as client:
        response = await client.get(f"/api/projects/{project.id}/pending", headers=auth)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.postgres
async def test_a_repo_still_on_the_root_process_file_syncs_and_is_offered(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Back-compat, end to end: the legacy layout is read for ever, under whatever `name:` the
    file carries — the identity rules bind a NEW lane, and a pinned snapshot has nothing to
    migrate its name to."""
    project = await seed_project(async_session)
    await async_session.flush()
    files = mini_config_files([])
    files[".factorial/process.yaml"] = MINI_PROCESS_YAML
    async with client_for(probe_app(async_session, files=files)) as client:
        synced = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        assert synced.status_code == 201, synced.text
        assert [(r["process"], r["outcome"]) for r in synced.json()["results"]] == [
            ("mini", "created")
        ]

        # Offered under the file's own name, and settable as the default like any other lane.
        rows = (await client.get(f"/api/projects/{project.id}/processes", headers=auth)).json()
        assert [r["process_key"] for r in rows] == ["mini"]
        assert (
            await client.put(
                f"/api/projects/{project.id}/default-process",
                headers=auth,
                json={"process_key": "mini"},
            )
        ).status_code == 200


@pytest.mark.postgres
async def test_a_repo_carrying_both_layouts_is_refused_at_sync(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Silently preferring one would leave an operator editing a file nothing reads."""
    project = await seed_project(async_session)
    await async_session.flush()
    files = lane_files("standard")
    files[".factorial/process.yaml"] = MINI_PROCESS_YAML
    async with client_for(probe_app(async_session, files=files)) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    assert response.status_code == 422
    assert "both layouts" in response.json()["detail"]


def test_a_github_repo_must_be_on_github() -> None:
    """A URL on another host is a mistake on a GitHub project, not `owner/name` to keep: GitHub
    would resolve the path to someone else's repository."""
    from pydantic import ValidationError

    from orchestrator.api.schemas import ProjectIn, TicketSourceIn

    pasted = "https://gitlab.com/acme/api"
    with pytest.raises(ValidationError):
        ProjectIn(name="x", github_repo=pasted)
    with pytest.raises(ValidationError):
        TicketSourceIn(kind="github", external_ref=pasted)
    assert ProjectIn(name="x", github_repo="https://github.com/acme/api.git").github_repo == (
        "acme/api"
    )


def test_a_gitlab_repo_is_normalized_from_its_own_host() -> None:
    from orchestrator.api.schemas import ProjectIn, TicketSourceIn

    gitlab = {"forge": "gitlab", "gitlab": {"base_url": "https://gitlab.example.com"}}
    for pasted in (
        "https://gitlab.example.com/group/sub/proj.git",
        "git@gitlab.example.com:group/sub/proj.git",
    ):
        assert ProjectIn(name="x", github_repo=pasted, settings=gitlab).github_repo == (
            "group/sub/proj"
        )
        assert TicketSourceIn(kind="gitlab", external_ref=pasted).external_ref == "group/sub/proj"
