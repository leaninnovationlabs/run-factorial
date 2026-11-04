"""The restart routes (#321): a request recorded for the loop, the states it refuses, and the
read-only preview of what it would cancel, close and delete."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from githubkit.exception import RequestFailed
from githubkit.response import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api import deps
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.github import (
    BranchState,
    IssueState,
    PullRequestState,
    SnapshotState,
    StateSource,
)
from tests.api_harness import (
    TOKEN,
    build_app,
    client_for,
    make_settings,
    observed_repo,
    seed_lane_head,
    seed_project,
    seed_ticket,
)
from tests.members import ensure_member
from tests.reconcile_world import _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

#: The issue `on_lane_p` seeds, and so the `{ticket}` in the lane's branch templates.
KEY = "12"
#: `seed_ticket`'s snapshot, and `seed_lane_head`'s.
PINNED_SHA = "a" * 40
HEAD_SHA = "b" * 40
#: A branch `plan`'s template, `spec/{ticket}-{slug}`, matches for issue 12.
SPEC = "spec/12-widget"
EMAIL = "operator@example.com"


async def on_lane_p(session: AsyncSession) -> Ticket:
    """A running ticket at `plan`, issue #12, pinned to a parseable snapshot of lane `p`."""
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, step="plan", external_key=KEY)
    pinned = await session.get_one(ProcessVersion, ticket.process_version_id)
    pinned.content = _process("p").model_dump(mode="json", by_alias=True)
    await session.flush()
    return ticket


async def behind(session: AsyncSession) -> tuple[Ticket, ProcessVersion]:
    """`on_lane_p`, with a later sync of the lane as its head."""
    ticket = await on_lane_p(session)
    return ticket, await seed_lane_head(session, ticket)


async def _retire(session: AsyncSession, ticket: Ticket) -> None:
    """The last sync stopped finding the lane's file: it has no head now."""
    pinned = await session.get_one(ProcessVersion, ticket.process_version_id)
    pinned.retired_at = datetime.now(UTC)
    await session.flush()


async def _events(session: AsyncSession, ticket_id: int) -> list[Event]:
    return list(
        (
            await session.execute(
                select(Event).where(Event.ticket_id == ticket_id).order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )


def _add_run(session: AsyncSession, ticket: Ticket, attempt: int, status: str) -> None:
    session.add(
        StepRun(
            ticket_id=ticket.id,
            step_name=ticket.current_step,
            kind=StepKind.AGENT,
            attempt=attempt,
            status=status,
        )
    )


# --------------------------------------------------------------------------------- the request


async def test_restart_is_a_request_naming_the_lane_head(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """202, not 200: nothing has restarted when this returns. The request names the snapshot
    the worker restarts onto, and the attempt the ticket page files it under."""
    ticket, head = await behind(async_session)
    _add_run(async_session, ticket, 2, StepRunStatus.RUNNING)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/restart",
            json={"process_version_id": head.id},
            headers=auth,
        )

    assert response.status_code == 202
    # No worker behind this API, so the pass the request woke never lands.
    assert response.json() == {"status": "requested", "landed": False}
    [event] = await _events(async_session, ticket.id)
    assert event.type == "restart_requested"
    assert event.step_run_id is None
    assert event.details == {
        "requested_by": "api-token",
        "requested_via": "dashboard",
        "target_process_version_id": head.id,
        "target_sha": HEAD_SHA,
        "allow_current_pin": False,
        "allow_merged": False,
        "step": "plan",
        "attempt": 2,
    }


async def test_a_signed_in_person_restarts_with_no_body_as_themselves(
    async_session: AsyncSession,
) -> None:
    """The session is the route's other credential. No body takes the lane head as it is."""
    ticket, head = await behind(async_session)
    await ensure_member(async_session, EMAIL)
    cookie = mint_session(EMAIL, verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=600)

    async with client_for(build_app(make_settings(), async_session)) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        response = await client.post(
            f"/api/tickets/{ticket.id}/restart", headers={"Sec-Fetch-Site": "same-origin"}
        )

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert (event.details["requested_by"], event.details["requested_via"]) == (EMAIL, "dashboard")
    assert event.details["target_process_version_id"] == head.id


async def test_a_second_request_while_one_is_unanswered_records_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, _ = await behind(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        first = await client.post(f"/api/tickets/{ticket.id}/restart", headers=auth)
        second = await client.post(f"/api/tickets/{ticket.id}/restart", headers=auth)

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["status"] == "requested"
    assert second.json() == {"status": "pending", "landed": False}
    assert [event.type for event in await _events(async_session, ticket.id)] == [
        "restart_requested"
    ]


async def test_allow_current_pin_restarts_a_ticket_already_on_the_head(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The dashboard never sends it; a caller restarting from scratch on the same config does."""
    ticket = await on_lane_p(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/restart", json={"allow_current_pin": True}, headers=auth
        )

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.details["target_process_version_id"] == ticket.process_version_id
    assert event.details["target_sha"] == PINNED_SHA
    assert event.details["allow_current_pin"] is True


async def test_a_request_that_accepts_merged_work_says_so(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The API cannot see GitHub, so it records the acceptance and the worker decides with it."""
    ticket, _ = await behind(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/restart", json={"allow_merged": True}, headers=auth
        )

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.details["allow_merged"] is True


async def test_allow_current_pin_changes_nothing_on_a_ticket_that_is_behind(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, head = await behind(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/restart", json={"allow_current_pin": True}, headers=auth
        )

    assert response.status_code == 202
    [event] = await _events(async_session, ticket.id)
    assert event.details["target_process_version_id"] == head.id


async def _done(session: AsyncSession) -> tuple[Ticket, dict[str, Any]]:
    ticket, _ = await behind(session)
    ticket.status = TicketStatus.DONE
    return ticket, {}


async def _quarantined(session: AsyncSession) -> tuple[Ticket, dict[str, Any]]:
    ticket, _ = await behind(session)
    ticket.quarantined_at = datetime.now(UTC)
    return ticket, {}


async def _retired(session: AsyncSession) -> tuple[Ticket, dict[str, Any]]:
    ticket = await on_lane_p(session)
    await _retire(session, ticket)
    return ticket, {}


async def _moved(session: AsyncSession) -> tuple[Ticket, dict[str, Any]]:
    """The caller decided on the ticket's own snapshot; the lane has synced since."""
    ticket, _ = await behind(session)
    return ticket, {"process_version_id": ticket.process_version_id}


async def _on_the_head(session: AsyncSession) -> tuple[Ticket, dict[str, Any]]:
    return await on_lane_p(session), {}


@pytest.mark.parametrize(
    ("seed", "code", "says"),
    [
        (_done, "ticket_done", "is done"),
        (_quarantined, "quarantined", "stopped after repeated errors"),
        (_retired, "process_retired", "process was removed"),
        (_moved, "process_moved", "its current version is bbbbbbb"),
        (_on_the_head, "already_current", "already runs aaaaaaa"),
    ],
    ids=["done", "quarantined", "retired", "moved", "on_the_head"],
)
async def test_restart_is_refused_where_it_cannot_be_honoured(
    async_session: AsyncSession,
    auth: dict[str, str],
    seed: Callable[[AsyncSession], Awaitable[tuple[Ticket, dict[str, Any]]]],
    code: str,
    says: str,
) -> None:
    ticket, body = await seed(async_session)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(f"/api/tickets/{ticket.id}/restart", json=body, headers=auth)

    assert response.status_code == 409
    assert response.json()["code"] == code
    assert says in response.json()["detail"]
    assert await _events(async_session, ticket.id) == []


# --------------------------------------------------------------------------------- the preview


def pull(
    number: int, *, state: str = "open", merged: bool = False, head: str = SPEC
) -> PullRequestState:
    return PullRequestState(
        number=number,
        head_ref=head,
        base_ref="main",
        head_sha="c" * 40,
        state=state,
        merged=merged,
        url=f"https://github.test/acme/pull/{number}",
    )


def preview_app(session: AsyncSession, reader: Callable[[str, int], StateSource]) -> FastAPI:
    app = build_app(make_settings(), session)
    app.dependency_overrides[deps.repo_reader_dependency] = lambda: reader
    return app


async def _preview(
    session: AsyncSession,
    auth: dict[str, str],
    ticket: Ticket,
    reader: Callable[[str, int], StateSource],
) -> dict[str, Any]:
    async with client_for(preview_app(session, reader)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/restart/preview", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_preview_lists_what_a_restart_cancels_closes_and_deletes(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, head = await behind(async_session)
    _add_run(async_session, ticket, 1, StepRunStatus.RUNNING)
    await async_session.flush()
    repo = (await async_session.get_one(Project, ticket.project_id)).github_repo
    seen: list[tuple[str, int]] = []
    reader = observed_repo(
        branches=[
            BranchState(name="main", sha="d" * 40),
            BranchState(name=SPEC, sha="c" * 40),
            # Issue 120's: the `-` after `{ticket}` in the template keeps it out.
            BranchState(name="spec/120-other", sha="e" * 40),
        ],
        pulls=[pull(5), pull(4, head="spec/120-other")],
        seen=seen,
    )

    body = await _preview(async_session, auth, ticket, reader)

    assert body == {
        "current": {"process_version_id": ticket.process_version_id, "sha": PINNED_SHA},
        "target": {"process_version_id": head.id, "sha": HEAD_SHA},
        "current_pin": False,
        "live_run": {"step": "plan", "attempt": 1, "kind": "agent"},
        "parked_run": None,
        "close_prs": [
            {"number": 5, "url": "https://github.test/acme/pull/5", "head": SPEC, "base": "main"}
        ],
        "delete_branches": [SPEC],
        "merged_outside": [],
        "blockers": [],
    }
    # Where the worker observes: the project's repo, under the ticket's own issue number.
    assert seen == [(repo, 12)]


async def test_a_parked_session_is_abandoned_rather_than_cancelled(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, _ = await behind(async_session)
    _add_run(async_session, ticket, 1, StepRunStatus.INTERRUPTED)
    await async_session.flush()

    body = await _preview(async_session, auth, ticket, observed_repo())

    assert body["live_run"] is None
    assert body["parked_run"] == {"step": "plan", "attempt": 1, "kind": "agent"}
    assert (body["close_prs"], body["delete_branches"]) == ([], [])


async def test_the_preview_names_what_refuses_the_restart(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, _ = await behind(async_session)
    ticket.status = TicketStatus.DONE
    ticket.quarantined_at = datetime.now(UTC)
    await async_session.flush()
    reader = observed_repo(
        issue_state="closed",
        branches=[BranchState(name=SPEC, sha="c" * 40)],
        # Merged into `main`, which the ticket does not own: deleting its head undoes nothing.
        pulls=[pull(5, state="closed", merged=True)],
    )

    body = await _preview(async_session, auth, ticket, reader)

    assert [blocker["reason"] for blocker in body["blockers"]] == [
        "done",
        "quarantined",
        "issue_closed",
    ]
    # The dialog joins them with spaces, so each is a sentence of its own.
    assert [blocker["detail"] for blocker in body["blockers"]] == [
        "This ticket is done.",
        "This ticket is stopped after repeated errors. Requeue it first.",
        "Issue #12 is closed. Reopen it first.",
    ]
    assert body["close_prs"] == []
    # A warning, not a blocker: whether the merged work still matters is the asker's call.
    assert body["merged_outside"] == [
        {"number": 5, "url": "https://github.test/acme/pull/5", "head": SPEC, "base": "main"}
    ]


async def test_a_retired_lane_has_no_target(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket = await on_lane_p(async_session)
    await _retire(async_session, ticket)

    body = await _preview(async_session, auth, ticket, observed_repo())

    assert (body["target"], body["current_pin"]) == (None, False)
    assert [blocker["reason"] for blocker in body["blockers"]] == ["lane_retired"]


async def test_a_ticket_on_the_head_previews_as_its_current_pin(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket = await on_lane_p(async_session)

    body = await _preview(async_session, auth, ticket, observed_repo())

    assert body["current_pin"] is True
    assert body["target"] == body["current"]
    assert body["blockers"] == []


class _GitHubDown(SnapshotState):
    async def branches(self) -> Sequence[BranchState]:
        request = httpx.Request("GET", "https://api.github.test/repos/acme/x/branches")
        raw = httpx.Response(502, request=request, json={"message": "Bad Gateway"})
        raise RequestFailed(Response(raw, dict[str, Any]))


def _github_down(repo: str, number: int) -> StateSource:
    issue = IssueState(
        number=number, title="", body="", state="open", labels=frozenset(), assignees=()
    )
    return _GitHubDown(repo=repo, issue_state=issue)


async def test_the_preview_fails_when_github_does(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """502, not 500: the fault is GitHub's. An empty footprint instead would read as a restart
    that loses nothing."""
    ticket, _ = await behind(async_session)

    async with client_for(preview_app(async_session, _github_down)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/restart/preview", headers=auth)

    assert response.status_code == 502
    assert "HTTP 502" in response.json()["detail"]


async def test_the_preview_needs_a_github_connection(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, _ = await behind(async_session)

    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/tickets/{ticket.id}/restart/preview", headers=auth)

    assert response.status_code == 503
    assert "no GitHub connection" in response.json()["detail"]


# ----------------------------------------------------------------------------- the ticket detail


async def test_the_ticket_detail_names_its_pin_its_lane_head_and_generations(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket, head = await behind(async_session)
    _add_run(async_session, ticket, 1, StepRunStatus.SUCCEEDED)
    await async_session.flush()

    async with client_for(build_app(make_settings(), async_session)) as client:
        body = (await client.get(f"/api/tickets/{ticket.id}", headers=auth)).json()

    assert (body["process_version_id"], body["process_sha"]) == (
        ticket.process_version_id,
        PINNED_SHA,
    )
    assert body["lane_head"] == {"process_version_id": head.id, "sha": HEAD_SHA}
    assert body["generation"] == 1
    assert [run["generation"] for run in body["step_runs"]] == [1]


async def test_a_retired_lane_has_no_head_on_the_ticket_detail(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    ticket = await on_lane_p(async_session)
    await _retire(async_session, ticket)

    async with client_for(build_app(make_settings(), async_session)) as client:
        body = (await client.get(f"/api/tickets/{ticket.id}", headers=auth)).json()

    assert body["lane_head"] is None
