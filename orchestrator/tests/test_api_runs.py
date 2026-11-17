"""The two run routes, and the states they must not turn into a 500.

A 5xx here is worse than it looks. These routes are cross-origin (decision 17), and FastAPI's
default 500 handler attaches no CORS headers — so the browser blocks the response, cannot read
the status, and the dashboard reports a connectivity or CORS problem instead of the real fault.
An unhandled exception in this module therefore surfaces to a user as "cannot reach the API",
which sends them to look at the wrong thing entirely. That is why the expected states below are
pinned as codes rather than left to the default handler.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from githubkit.exception import RequestError, RequestTimeout
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.state import BranchState, IssueState, SnapshotState, StateSource
from orchestrator.api.deps import RunObserver, run_observer_dependency, runtime_adapter_dependency
from orchestrator.api.routes import runs as runs_routes
from orchestrator.api.services import controls
from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import Conflict, Forbidden, UpstreamFailed
from orchestrator.api.services.results import RequestResult
from orchestrator.api.sessions import SESSION_COOKIE, mint_session
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.events.approvals import latest_approval
from orchestrator.events.types import RequestedVia
from orchestrator.gitlab.client import GitLabError
from orchestrator.settings import AuthSettings, Settings
from runtime.adapters.claude_cli.transcript import Transcript
from runtime.contract import AdapterUnavailable, UnknownRunError
from tests.api_harness import (
    TOKEN,
    build_app,
    client_for,
    make_settings,
    seed_project,
    seed_ticket,
)
from tests.members import ensure_member
from tests.reconcile_world import _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

ASK = Path(__file__).parents[2] / "runtime/tests/fixtures/claude-2.1.284/ask.session.jsonl"


class _Adapter:
    """A substrate that yields the lines it was given, or raises what it was told to raise."""

    def __init__(self, *, lines: list[str] | None = None, error: Exception | None = None) -> None:
        self._lines = lines or []
        self._error = error

    async def logs(self, run_id: str) -> AsyncIterator[str]:
        if self._error is not None:
            raise self._error
        for line in self._lines:
            yield line


def _app(session: AsyncSession, adapter: object | None) -> FastAPI:
    app = build_app(make_settings(), session)
    app.dependency_overrides[runtime_adapter_dependency] = lambda: adapter
    return app


async def _step_run(
    session: AsyncSession,
    *,
    runtime_run_id: str | None,
    kind: str = StepKind.AGENT,
    status: str = StepRunStatus.SUCCEEDED,
    quarantined: bool = False,
) -> StepRun:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    if quarantined:
        ticket.quarantined_at = datetime.now(UTC)
    row = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=kind,
        attempt=1,
        status=status,
        runtime_run_id=runtime_run_id,
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_purged_run_is_gone_not_a_server_error(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The state this route hits most often after a run finishes.

    A run directory is cleaned up with its worktree, and an operator clearing old runs does the
    same deliberately, so `UnknownRunError` is routine — its own docstring says "expired, purged,
    or never accepted". 410 distinguishes it from a step run that never existed at all, and keeps
    it out of the 500 path whose CORS-less response would be reported to the user as an
    unreachable API.
    """
    row = await _step_run(async_session, runtime_run_id="112-plan-agent-abc")
    adapter = _Adapter(error=UnknownRunError("no local run 112-plan-agent-abc"))

    async with client_for(_app(async_session, adapter)) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 410
    assert "no longer kept" in response.json()["detail"]


async def test_an_unreachable_substrate_is_503(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """`AdapterUnavailable` is not this run's fault, and is not permanent — 503 says retry, where
    410 would tell the dashboard to stop asking forever."""
    row = await _step_run(async_session, runtime_run_id="112-plan-agent-abc")
    adapter = _Adapter(error=AdapterUnavailable("docker is not running"))

    async with client_for(_app(async_session, adapter)) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 503


async def test_a_step_that_never_ran_on_a_substrate_is_refused_by_name(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """A human gate has no `runtime_run_id`. An empty list would read like a run whose output was
    lost; saying which kind of step it is says what actually happened."""
    row = await _step_run(async_session, runtime_run_id=None, kind=StepKind.HUMAN_GATE)

    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This is a gate step, so it has no transcript.",
        "code": "no_transcript",
    }


async def test_interrupting_a_check_run_is_refused_by_kind(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """A check step's RUNNING row is the loop's own wait on observed state — routinely minutes
    long, so the dashboard's stop gesture can reach it. There is no session to stop, and only
    the agent path honours interrupts; accepting would record a request that resolves to
    silence, the exact state the route's docstring forbids."""
    row = await _step_run(
        async_session,
        runtime_run_id=None,
        kind=StepKind.CHECK,
        status=StepRunStatus.RUNNING,
    )

    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.post(f"/api/step-runs/{row.id}/interrupt", headers=auth)

    assert response.status_code == 409
    assert "Only an agent's run can be interrupted" in response.json()["detail"]
    assert response.json()["code"] == "not_an_agent_run"


async def test_interrupting_a_quarantined_ticket_is_refused(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The motivating case, not a corner: quarantine leaves `status` and the `step_run` row
    exactly where they stood, so a quarantined ticket routinely still has a RUNNING agent run.
    `acquire_leases` never hands the ticket out again, so the request would be filed and never
    read — the same refusal `exit` and `reenter` make."""
    row = await _step_run(
        async_session,
        runtime_run_id="112-plan-agent-abc",
        status=StepRunStatus.RUNNING,
        quarantined=True,
    )

    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.post(f"/api/step-runs/{row.id}/interrupt", headers=auth)

    assert response.status_code == 409
    assert "stopped after repeated errors" in response.json()["detail"]
    assert response.json()["code"] == "quarantined"
    events = (
        (await async_session.execute(select(Event).where(Event.ticket_id == row.ticket_id)))
        .scalars()
        .all()
    )
    assert events == []


async def test_resume_session_is_refused_unless_the_run_is_the_parked_one(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """Accepted, each of these is a request the loop never honours: it sits pending and releases
    whatever parks next."""
    cases: list[tuple[dict[str, Any], str]] = [
        ({"status": StepRunStatus.RUNNING}, "not_parked"),
        ({"kind": StepKind.HUMAN_GATE, "status": StepRunStatus.WAITING}, "not_parked"),
        ({"ticket_status": TicketStatus.RUNNING}, "not_parked"),
        ({"quarantined": True}, "quarantined"),
        ({"later_attempt": True}, "not_parked"),
        ({"current_step": "build"}, "not_parked"),
    ]
    refused: list[int] = []
    async with client_for(_app(async_session, _Adapter())) as client:
        for overrides, why in cases:
            row = await _parked_run(async_session, **overrides)
            refused.append(row.ticket_id)
            response = await client.post(
                f"/api/step-runs/{row.id}/resume-session", headers=auth, json={}
            )
            assert response.status_code == 409, overrides
            assert response.json()["code"] == why, overrides
        for answer in ("   ", "x" * 8001):
            row = await _parked_run(async_session)
            refused.append(row.ticket_id)
            response = await client.post(
                f"/api/step-runs/{row.id}/resume-session", headers=auth, json={"answer": answer}
            )
            assert response.status_code == 422
        events = await async_session.execute(select(Event).where(Event.ticket_id.in_(refused)))
        assert events.scalars().all() == []

        row = await _parked_run(async_session)
        response = await client.post(
            f"/api/step-runs/{row.id}/resume-session", headers=auth, json={}
        )
    assert response.status_code == 202


async def _parked_run(
    session: AsyncSession,
    *,
    status: str = StepRunStatus.INTERRUPTED,
    kind: str = StepKind.AGENT,
    ticket_status: str = TicketStatus.BLOCKED,
    quarantined: bool = False,
    later_attempt: bool = False,
    current_step: str = "plan",
) -> StepRun:
    row = await _step_run(
        session,
        runtime_run_id="112-plan-agent-abc",
        kind=kind,
        status=status,
        quarantined=quarantined,
    )
    ticket = (await session.execute(select(Ticket).where(Ticket.id == row.ticket_id))).scalar_one()
    ticket.status = ticket_status
    ticket.current_step = current_step
    if later_attempt:
        session.add(
            StepRun(
                ticket_id=ticket.id,
                step_name="plan",
                kind=kind,
                attempt=2,
                status=StepRunStatus.FAILED,
                runtime_run_id="112-plan-agent-def",
            )
        )
    await session.flush()
    return row


async def test_a_missing_step_run_is_404(async_session: AsyncSession, auth: dict[str, str]) -> None:
    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.get("/api/step-runs/99999999/activity", headers=auth)

    assert response.status_code == 404


async def test_no_adapter_configured_is_503_and_names_the_kind(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """`None` from the dependency means the configured kind has no registered factory. The route
    answers rather than raising, so the rest of the API stays unaffected."""
    row = await _step_run(async_session, runtime_run_id="112-plan-agent-abc")

    async with client_for(_app(async_session, None)) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 503
    assert "transcript can't be read" in response.json()["detail"]
    assert response.json()["code"] == "runtime_unavailable"


async def test_a_healthy_run_returns_its_activity(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    row = await _step_run(async_session, runtime_run_id="112-plan-agent-abc")
    line = (
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Bash","input":{"command":"npm test"}}]}}'
    )

    async with client_for(_app(async_session, _Adapter(lines=[line]))) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 200
    body = response.json()
    (turn,) = body["turns"]
    assert turn["index"] == 1
    assert turn["tools"] == [
        {
            "tool": "Bash",
            "target": "npm test",
            "ok": None,
            "result": None,
            "question": None,
            "answer": None,
        }
    ]
    assert body["truncated"] is False


async def test_an_askuserquestion_call_shows_its_question_and_answer_and_nothing_else(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The real TUI session, translated as the adapter does, with keys the agent slipped into
    the call's input. Only the question is read off the input, so neither key is ever looked at."""
    lines = ASK.read_text("utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if '"tool_use"' in line)
    record = json.loads(lines[index])
    record["message"]["content"][0]["input"] |= {"token": "ghs_x", "command": "echo ghs_y"}
    lines[index] = json.dumps(record)
    # The CLI renders the tool result from the input, so input content can arrive there too.
    answered = next(i for i, line in enumerate(lines) if '"tool_result"' in line)
    record = json.loads(lines[answered])
    record["message"]["content"][0]["content"] += " ghs_z"
    lines[answered] = json.dumps(record)
    transcript = Transcript("s1", turn=True)
    translated = [event for line in lines for event in transcript.feed(line)]
    translated += transcript.end_segment()
    row = await _step_run(async_session, runtime_run_id="112-plan-agent-abc")

    async with client_for(_app(async_session, _Adapter(lines=translated))) as client:
        response = await client.get(f"/api/step-runs/{row.id}/activity", headers=auth)

    assert response.status_code == 200
    assert "ghs_" not in response.text
    (call,) = [call for turn in response.json()["turns"] for call in turn["tools"]]
    assert (call["tool"], call["target"]) == ("AskUserQuestion", None)
    assert (call["question"], call["answer"]) == ("Include archived rows?", "Active only")
    assert call["result"] is None


# ----------------------------------------------------------------------- previews


_DASHBOARD = "https://factorial.example"


def _preview_settings() -> Settings:
    return make_settings(auth=AuthSettings(external_url=_DASHBOARD))


async def _waiting_for_review(
    session: AsyncSession,
    *,
    preview: dict[str, Any] | None = None,
    without_preview: bool = False,
    on_reject: bool = True,
    reruns_check: bool = False,
) -> tuple[StepRun, StepRun]:
    """A ticket pinned to a process whose `plan` step has a preview reviewed at `spec_gate`
    (none when `without_preview`), stopped at that gate: plan attempt 1 succeeded, gate attempt 1
    waits. `on_reject=False` drops the gate's rejection route. `reruns_check` puts a `lint` check
    step between them, run and succeeded, that the gate's rejection reruns; its run is returned
    in plan's place."""
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, step="spec_gate", status=TicketStatus.WAITING_GATE)
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    if not without_preview:
        raw["steps"][0]["preview"] = preview or {"reviewGate": "spec_gate"}
    if not on_reject:
        del raw["steps"][1]["gate"]["onReject"]
    if reruns_check:
        raw["steps"][0]["next"] = "lint"
        raw["steps"].append(
            {
                "name": "lint",
                "kind": "check",
                "next": "spec_gate",
                "conditions": "ticket_has_assignee()",
            }
        )
        raw["steps"][1]["gate"]["onReject"]["rerunStep"] = "lint"
    version = (
        await session.execute(
            select(ProcessVersion).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    version.content = raw
    runs = [
        StepRun(
            ticket_id=ticket.id,
            step_name="plan",
            kind=StepKind.AGENT,
            attempt=1,
            status=StepRunStatus.SUCCEEDED,
            runtime_run_id="run-1",
        )
    ]
    if reruns_check:
        runs.append(
            StepRun(
                ticket_id=ticket.id,
                step_name="lint",
                kind=StepKind.CHECK,
                attempt=1,
                status=StepRunStatus.SUCCEEDED,
            )
        )
    runs.append(
        StepRun(
            ticket_id=ticket.id,
            step_name="spec_gate",
            kind=StepKind.HUMAN_GATE,
            attempt=1,
            status=StepRunStatus.WAITING,
        )
    )
    for run in runs:
        session.add(run)
        await session.flush()
    return runs[-2], runs[-1]


async def test_a_published_preview_is_read_back_by_its_run_and_written_once(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _waiting_for_review(async_session)
    url = f"/api/step-runs/{plan.id}/preview"

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        empty = await client.get(url, headers=auth)
        body = {"preview_url": "https://preview.example/1"}
        published = await client.put(url, json=body, headers=auth)
        republished = await client.put(url, json=body, headers=auth)
        read = await client.get(url, headers=auth)
        withdrawn = await client.put(url, json={"preview_url": None}, headers=auth)

    assert empty.json()["urls"] == [] and empty.json()["status"] == "none"
    assert published.status_code == republished.status_code == 200
    assert read.json()["urls"] == [{"service": None, "url": "https://preview.example/1"}]
    assert read.json()["served_by_runtime"] is False
    assert withdrawn.json()["urls"] == []
    urls = (
        await async_session.scalars(
            select(Event.details["urls"])
            .where(Event.step_run_id == plan.id, Event.type == "preview_url_updated")
            .order_by(Event.id)
        )
    ).all()
    assert urls == [[{"service": None, "url": "https://preview.example/1"}], []]


@pytest.mark.parametrize(
    ("preview_url", "why"),
    [
        ("javascript:alert(1)", "must be an https URL"),
        ("http://preview.example/1", "must use https"),
        ("https://user:pw@preview.example/1", "must not embed a username or password"),
        (f"{_DASHBOARD}:443/preview", "must not be on the dashboard's own origin"),
        ("https://preview.example/1?token=abc", "no query string or fragment"),
        ("https://preview.example/1#access_token=abc", "no query string or fragment"),
    ],
)
async def test_a_preview_url_unsafe_to_frame_is_refused(
    async_session: AsyncSession, auth: dict[str, str], preview_url: str, why: str
) -> None:
    plan, _ = await _waiting_for_review(async_session)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.put(
            f"/api/step-runs/{plan.id}/preview", json={"preview_url": preview_url}, headers=auth
        )

    assert response.status_code == 422
    assert why in response.json()["detail"]


async def test_a_step_without_a_preview_accepts_no_url(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    _, gate = await _waiting_for_review(async_session)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.put(
            f"/api/step-runs/{gate.id}/preview",
            json={"preview_url": "https://preview.example/1"},
            headers=auth,
        )

    assert response.status_code == 409


async def test_a_change_request_is_recorded_once_against_the_waiting_gate(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """A resubmission with the same key answers with the first request; a second request
    under a new key is refused while the first waits. Either way one request, one attempt."""
    plan, gate = await _waiting_for_review(async_session)
    url = f"/api/step-runs/{plan.id}/changes"
    body = {"feedback": "Make the button blue.", "idempotency_key": "form-0001"}

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        first = await client.post(url, json=body, headers=auth)
        again = await client.post(url, json=body, headers=auth)
        other = await client.post(url, json={**body, "idempotency_key": "form-0002"}, headers=auth)

    assert first.status_code == 202
    assert first.json()["outcome"] == "pending"  # no worker behind this API
    assert again.json()["request_id"] == first.json()["request_id"]
    assert other.status_code == 409
    [event] = (
        await async_session.scalars(
            select(Event).where(
                Event.type == "step_changes_requested", Event.step_run_id == gate.id
            )
        )
    ).all()
    assert event.details["feedback"] == "Make the button blue."
    assert event.details["reviewed_step_run_id"] == plan.id
    assert event.details["requested_by"] == "api-token"


async def test_a_change_request_is_refused_once_the_gate_stops_waiting(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, gate = await _waiting_for_review(async_session)
    gate.status = StepRunStatus.APPROVED
    await async_session.flush()

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{plan.id}/changes",
            json={"feedback": "Too late.", "idempotency_key": "form-0003"},
            headers=auth,
        )

    assert response.status_code == 409
    assert "isn't waiting for review" in response.json()["detail"]


async def test_a_change_request_needs_signing_in(async_session: AsyncSession) -> None:
    plan, _ = await _waiting_for_review(async_session)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{plan.id}/changes",
            json={"feedback": "Anyone?", "idempotency_key": "form-0004"},
        )

    assert response.status_code == 401


async def test_a_change_request_needs_no_preview_on_the_reviewed_step(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, gate = await _waiting_for_review(async_session, without_preview=True)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{plan.id}/changes",
            json={"feedback": "Rename the endpoint.", "idempotency_key": "form-0005"},
            headers=auth,
        )

    assert response.status_code == 202
    [event] = (
        await async_session.scalars(
            select(Event).where(
                Event.type == "step_changes_requested", Event.step_run_id == gate.id
            )
        )
    ).all()
    assert event.details["reviewed_step"] == "plan"


async def test_a_change_request_is_refused_at_a_gate_with_no_rejection_route(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _waiting_for_review(async_session, without_preview=True, on_reject=False)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{plan.id}/changes",
            json={"feedback": "Anything.", "idempotency_key": "form-0006"},
            headers=auth,
        )

    assert response.status_code == 409
    assert "sends work back to plan." in response.json()["detail"]


async def test_a_change_request_is_refused_at_a_gate_that_reruns_no_agent_step(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """A change request's feedback is an agent's next turn: a gate whose rejection reruns a
    check takes none, even for that check's own latest run."""
    lint, _ = await _waiting_for_review(async_session, without_preview=True, reruns_check=True)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{lint.id}/changes",
            json={"feedback": "Anything.", "idempotency_key": "form-0008"},
            headers=auth,
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "isn't waiting for a review that sends work back to lint." in detail


async def test_a_change_request_names_the_reviewed_run_not_the_gate(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    _, gate = await _waiting_for_review(async_session, without_preview=True)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{gate.id}/changes",
            json={"feedback": "Anything.", "idempotency_key": "form-0007"},
            headers=auth,
        )

    assert response.status_code == 409
    assert "sends work back to spec_gate." in response.json()["detail"]


async def _edit_pinned(session: AsyncSession, run: StepRun, edit: Any) -> None:
    ticket = await session.get_one(Ticket, run.ticket_id)
    version = await session.get_one(ProcessVersion, ticket.process_version_id)
    raw = json.loads(json.dumps(version.content))
    edit(raw)
    version.content = raw
    await session.flush()


async def _conflict_events(session: AsyncSession, gate: StepRun) -> list[Event]:
    return list(
        (
            await session.scalars(
                select(Event).where(
                    Event.type == "conflict_fix_requested", Event.step_run_id == gate.id
                )
            )
        ).all()
    )


async def test_a_conflict_fix_is_recorded_once_against_the_waiting_gate(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    _, gate = await _waiting_for_review(async_session, without_preview=True)
    url = f"/api/step-runs/{gate.id}/conflict-fix"
    body = {"idempotency_key": "press-0001"}

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        first = await client.post(url, json=body, headers=auth)
        again = await client.post(url, json=body, headers=auth)
        other = await client.post(url, json={"idempotency_key": "press-0002"}, headers=auth)

    assert first.status_code == 202
    assert first.json()["outcome"] == "pending"  # no worker behind this API
    assert again.json()["request_id"] == first.json()["request_id"]
    assert other.status_code == 409
    assert other.json()["code"] == "conflict_fix_pending"
    [event] = await _conflict_events(async_session, gate)
    assert event.details["requested_by"] == "api-token"
    assert event.details["idempotency_key"] == "press-0001"


@pytest.mark.parametrize(
    ("why", "sentence"),
    [
        ("not_a_gate", "plan isn't a gate, so it has no pull request to fix."),
        ("not_waiting", "spec_gate isn't waiting for review."),
        ("no_merge", "spec_gate doesn't merge a pull request."),
        ("no_fix_step", "This lane has no agent step to send spec_gate's work to."),
    ],
)
async def test_a_conflict_fix_is_refused_where_it_could_never_apply(
    async_session: AsyncSession, auth: dict[str, str], why: str, sentence: str
) -> None:
    plan, gate = await _waiting_for_review(async_session, without_preview=True)
    target = gate
    if why == "not_a_gate":
        target = plan
    elif why == "not_waiting":
        gate.status = StepRunStatus.APPROVED
    elif why == "no_merge":
        await _edit_pinned(
            async_session,
            gate,
            lambda raw: raw["steps"][1]["gate"]["onApprove"].update(merge=False),
        )
    else:
        await _edit_pinned(
            async_session, gate, lambda raw: raw["steps"][1]["gate"].pop("onReject")
        )
    await async_session.flush()

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{target.id}/conflict-fix",
            json={"idempotency_key": "press-0003"},
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == sentence
    assert await _conflict_events(async_session, gate) == []


async def test_a_conflict_fix_is_refused_when_the_gate_ends_before_the_lock(
    async_session: AsyncSession, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick approves and merges the gate between the route's first read and its row lock: no
    request may be left on a run nothing will ever answer."""
    _, gate = await _waiting_for_review(async_session, without_preview=True)
    first_read = runs_routes.conflict_fix_by_key

    async def approved_meanwhile(session: AsyncSession, *args: Any) -> Any:
        # Behind the identity map's back, as another transaction's commit would be.
        await session.execute(
            update(StepRun)
            .where(StepRun.id == gate.id)
            .values(status=StepRunStatus.APPROVED)
            .execution_options(synchronize_session=False)
        )
        return await first_read(session, *args)

    monkeypatch.setattr(runs_routes, "conflict_fix_by_key", approved_meanwhile)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{gate.id}/conflict-fix",
            json={"idempotency_key": "press-0005"},
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "spec_gate isn't waiting for review."
    assert await _conflict_events(async_session, gate) == []


async def test_a_conflict_fix_needs_signing_in(async_session: AsyncSession) -> None:
    _, gate = await _waiting_for_review(async_session, without_preview=True)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{gate.id}/conflict-fix", json={"idempotency_key": "press-0004"}
        )

    assert response.status_code == 401


async def _gate_entry(
    session: AsyncSession, auth: dict[str, str], gate: StepRun
) -> dict[str, Any]:
    async with client_for(build_app(_preview_settings(), session)) as client:
        response = await client.get(f"/api/tickets/{gate.ticket_id}", headers=auth)
    assert response.status_code == 200
    [entry] = [run for run in response.json()["step_runs"] if run["id"] == gate.id]
    return entry


async def _reading(session: AsyncSession, gate: StepRun, state: str) -> None:
    ticket = await session.get_one(Ticket, gate.ticket_id)
    await append(
        session,
        type=EventType.GATE_MERGEABILITY,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=gate.id,
        step=gate.step_name,
        attempt=gate.attempt,
        pr_number=7,
        head_sha="a" * 40,
        base_ref="main",
        state=state,
    )
    await session.flush()


async def test_a_waiting_gate_reports_its_pull_requests_last_mergeability(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    _, gate = await _waiting_for_review(async_session, without_preview=True)

    bare = await _gate_entry(async_session, auth, gate)
    await _reading(async_session, gate, "conflicting")
    conflicting = await _gate_entry(async_session, auth, gate)
    await _reading(async_session, gate, "clean")
    clean = await _gate_entry(async_session, auth, gate)

    assert (bare["mergeability"], bare["base_ref"], bare["can_fix_conflict"]) == (None, None, True)
    assert (conflicting["mergeability"], conflicting["base_ref"]) == ("conflicting", "main")
    assert clean["mergeability"] is None


async def test_a_conflict_send_back_does_not_read_as_a_rejection_and_a_pending_fix_shows(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    _, gate = await _waiting_for_review(async_session, without_preview=True)
    ticket = await async_session.get_one(Ticket, gate.ticket_id)
    async with client_for(build_app(_preview_settings(), async_session)) as client:
        await client.post(
            f"/api/step-runs/{gate.id}/conflict-fix",
            json={"idempotency_key": "press-0005"},
            headers=auth,
        )
    pending = await _gate_entry(async_session, auth, gate)
    gate.status = StepRunStatus.REJECTED
    await append(
        async_session,
        type=EventType.GATE_CONFLICT_SENT_BACK,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=gate.id,
        step=gate.step_name,
        attempt=gate.attempt,
        target_step="plan",
    )
    await async_session.flush()
    sent_back = await _gate_entry(async_session, auth, gate)

    assert pending["conflict_fix_pending"] is True
    assert pending["conflict_send_back"] is False
    assert sent_back["conflict_send_back"] is True
    assert sent_back["conflict_fix_pending"] is False


# --------------------------------------------------------------------------- runtime-served

_SERVED: dict[str, Any] = {"services": [{"name": "web", "command": "npm start", "port": 3000}]}


async def _kept_for_review(
    session: AsyncSession, *, kept: bool = True, key: str = "public"
) -> tuple[StepRun, StepRun]:
    """`_waiting_for_review`, with `plan` serving its own preview, spawned kept or not."""
    plan, gate = await _waiting_for_review(session, preview={"reviewGate": "spec_gate", **_SERVED})
    plan.ended_at = datetime.now(UTC)
    ticket = await session.get(Ticket, plan.ticket_id)
    assert ticket is not None
    await append(
        session,
        type=EventType.AGENT_SPAWNED,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=plan.id,
        step="plan",
        attempt=1,
        **{key: kept},
    )
    await session.flush()
    return plan, gate


@pytest.fixture
def _no_deployment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test database is shared: another module's awake preview must not fill the limit."""
    monkeypatch.setattr("orchestrator.api.routes.runs.MAX_AWAKE_WORKSPACES", 10**6)


async def test_a_runtime_served_step_takes_no_producer_url(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.put(
            f"/api/step-runs/{plan.id}/preview",
            json={"preview_url": "https://preview.example/1"},
            headers=auth,
        )

    assert response.status_code == 409
    assert "served by its runtime" in response.json()["detail"]


@pytest.mark.usefixtures("_no_deployment_limit")
# `retain_workspace` is the key a spawn wrote before `public`.
@pytest.mark.parametrize("key", ["public", "retain_workspace"])
async def test_a_kept_run_offers_start_and_records_one_request(
    async_session: AsyncSession, auth: dict[str, str], key: str
) -> None:
    plan, _ = await _kept_for_review(async_session, key=key)
    url = f"/api/step-runs/{plan.id}/preview"

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        before = (await client.get(url, headers=auth)).json()
        first = await client.post(f"{url}/start", headers=auth)
        second = await client.post(f"{url}/start", headers=auth)

    assert before["served_by_runtime"] is True and before["startable"] is True
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "starting"
    requests = (
        await async_session.scalars(
            select(Event).where(
                Event.step_run_id == plan.id, Event.type == EventType.PREVIEW_REQUESTED
            )
        )
    ).all()
    assert len(requests) == 1, "a second Start while one is pending records nothing"


async def test_a_run_that_was_not_kept_says_why_and_refuses_start(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session, kept=False)
    url = f"/api/step-runs/{plan.id}/preview"

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        read = (await client.get(url, headers=auth)).json()
        start = await client.post(f"{url}/start", headers=auth)

    assert read["startable"] is False
    assert "cloud sandbox" in read["unavailable"]
    assert start.status_code == 409


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_stop_records_a_request_only_for_an_awake_preview(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    url = f"/api/step-runs/{plan.id}/preview"

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        idle = await client.post(f"{url}/stop", headers=auth)
        await client.post(f"{url}/start", headers=auth)
        stop = await client.post(f"{url}/stop", headers=auth)

    assert idle.status_code == 202 and idle.json()["status"] == "none"
    assert stop.json()["status"] == "stopping"


async def test_a_start_past_the_deployment_limit_is_refused(
    async_session: AsyncSession, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator.api.routes.runs.MAX_AWAKE_WORKSPACES", 0)
    plan, _ = await _kept_for_review(async_session)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        start = await client.post(f"/api/step-runs/{plan.id}/preview/start", headers=auth)

    assert start.status_code == 409
    assert "already running. Stop one first." in start.json()["detail"]


async def test_a_done_ticket_reports_its_preview_stopped(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """A done ticket's preview cannot be served, whatever its log last said."""
    plan, _ = await _kept_for_review(async_session)
    ticket = await async_session.get(Ticket, plan.ticket_id)
    assert ticket is not None
    for kind in (EventType.PREVIEW_REQUESTED, EventType.PREVIEW_STARTED):
        await append(
            async_session,
            type=kind,
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            step_run_id=plan.id,
        )
    ticket.status = TicketStatus.DONE
    await async_session.flush()

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert read["status"] == "stopped" and read["urls"] == []
    assert read["startable"] is False


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_quarantined_tickets_preview_is_not_startable(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The dashboard starts a startable preview by itself; a quarantined ticket refuses that
    Start, and the person would see an error they never caused."""
    plan, _ = await _kept_for_review(async_session)
    ticket = await async_session.get(Ticket, plan.ticket_id)
    assert ticket is not None
    ticket.quarantined_at = datetime.now(UTC)
    await async_session.flush()

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        read = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert read["served_by_runtime"] is True and read["startable"] is False


async def _lifecycle(session: AsyncSession, run: StepRun, type: EventType, **details: Any) -> None:
    ticket = await session.get(Ticket, run.ticket_id)
    assert ticket is not None
    await append(
        session,
        type=type,
        project_id=ticket.project_id,
        ticket_id=ticket.id,
        step_run_id=run.id,
        step=run.step_name,
        attempt=run.attempt,
        **details,
    )
    await session.flush()


async def _events(session: AsyncSession, run: StepRun, type: EventType) -> list[Event]:
    return list(
        (
            await session.scalars(
                select(Event).where(Event.type == type, Event.step_run_id == run.id)
            )
        ).all()
    )


async def _live(session: AsyncSession, run: StepRun) -> None:
    run.status = StepRunStatus.RUNNING
    run.ended_at = None
    await session.flush()


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_keep_running_records_who_kept_it(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(f"/api/step-runs/{plan.id}/preview/keep", headers=auth)

    assert response.status_code == 202
    [kept] = await _events(async_session, plan, EventType.PREVIEW_KEPT)
    assert kept.details["requested_by"] == "api-token"


async def test_keep_running_is_refused_once_the_preview_stopped(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STOPPED, reason="idle")

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(f"/api/step-runs/{plan.id}/preview/keep", headers=auth)

    assert response.status_code == 409
    assert "No preview is running. Start it first." in response.json()["detail"]
    assert await _events(async_session, plan, EventType.PREVIEW_KEPT) == []


async def test_restart_while_starting_records_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(
        async_session, plan, EventType.PREVIEW_REQUESTED, requested_by="operator@example.com"
    )

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        response = await client.post(
            f"/api/step-runs/{plan.id}/preview/start", json={"restart": True}, headers=auth
        )

    assert response.json()["status"] == "starting"
    assert len(await _events(async_session, plan, EventType.PREVIEW_REQUESTED)) == 1


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_restart_of_a_serving_preview_is_recorded(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        await client.post(
            f"/api/step-runs/{plan.id}/preview/start", json={"restart": True}, headers=auth
        )

    [request] = await _events(async_session, plan, EventType.PREVIEW_REQUESTED)
    assert request.details["restart"] is True
    assert request.details["requested_by"] == "api-token"


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_start_without_restart_on_a_serving_preview_records_nothing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        await client.post(f"/api/step-runs/{plan.id}/preview/start", headers=auth)

    assert await _events(async_session, plan, EventType.PREVIEW_REQUESTED) == []


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_live_runs_preview_is_startable_and_says_it_runs_beside_a_session(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _live(async_session, plan)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        before = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()
        await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)
        serving = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert before["startable"] is True and before["beside_session"] is True
    assert serving["beside_session"] is True and serving["stops_at"] is None


async def test_a_start_on_a_live_run_passes_the_deployment_limit(
    async_session: AsyncSession, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator.api.routes.runs.MAX_AWAKE_WORKSPACES", 0)
    plan, _ = await _kept_for_review(async_session)
    await _live(async_session, plan)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        start = await client.post(f"/api/step-runs/{plan.id}/preview/start", headers=auth)

    assert start.status_code == 202
    assert len(await _events(async_session, plan, EventType.PREVIEW_REQUESTED)) == 1


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_serving_preview_says_when_it_closes_and_offers_restart(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        body = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert body["restartable"] is True and body["beside_session"] is False
    assert datetime.fromisoformat(body["stops_at"]) > datetime.now(UTC)


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_an_open_session_makes_the_preview_beside_a_session(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STARTED)
    await _lifecycle(async_session, plan, EventType.INSPECT_STARTED)

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        body = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert body["beside_session"] is True


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_preview_stopped_for_a_renewal_is_renewing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(async_session, plan, EventType.PREVIEW_STOPPED, reason="renewal")

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        body = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert body["renewing"] is True and body["restartable"] is False


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_a_renewal_whose_park_failed_is_not_renewing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The park killed the sandbox, so nothing will start the preview again."""
    plan, _ = await _kept_for_review(async_session)
    await _lifecycle(
        async_session, plan, EventType.PREVIEW_STOPPED, reason="renewal\nkilled: park failed"
    )

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        body = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert body["renewing"] is False


@pytest.mark.usefixtures("_no_deployment_limit")
async def test_the_loops_renewal_request_on_a_live_rework_is_renewing(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    plan, _ = await _kept_for_review(async_session)
    await _live(async_session, plan)
    await _lifecycle(
        async_session,
        plan,
        EventType.PREVIEW_REQUESTED,
        requested_by="factorial",
        renewal=True,
    )

    async with client_for(build_app(_preview_settings(), async_session)) as client:
        body = (await client.get(f"/api/step-runs/{plan.id}/preview", headers=auth)).json()

    assert body["renewing"] is True and body["beside_session"] is True
    assert body["stops_at"] is None


# ------------------------------------------------------------------------------- retry now


async def _waiting_to_retry(
    session: AsyncSession,
    *,
    ended_ago: timedelta = timedelta(minutes=2),
    status: str = StepRunStatus.FAILED,
    ticket_status: str = TicketStatus.RUNNING,
) -> StepRun:
    """A ticket on `plan` whose attempt 1 ended `ended_ago` as `status`, under a process that
    retries twice with a 10-minute backoff."""
    project = await seed_project(session)
    ticket = await seed_ticket(session, project, step="plan", status=ticket_status)
    version = await session.get(ProcessVersion, ticket.process_version_id)
    assert version is not None
    version.content = _process("proc").model_dump(mode="json", by_alias=True)
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=status,
        runtime_run_id="run-1",
        ended_at=datetime.now(UTC) - ended_ago,
    )
    session.add(run)
    await session.flush()
    return run


async def _retry_requests(session: AsyncSession, run: StepRun) -> list[Event]:
    return list(
        (
            await session.execute(
                select(Event).where(
                    Event.step_run_id == run.id, Event.type == EventType.RETRY_REQUESTED
                )
            )
        ).scalars()
    )


async def test_a_retry_now_is_recorded_once_against_the_failed_run(
    async_session: AsyncSession,
) -> None:
    run = await _waiting_to_retry(async_session)
    actor = Actor.dashboard("operator@example.com")

    first = await controls.request_retry(async_session, run, actor)
    again = await controls.request_retry(async_session, run, actor)

    assert first.status == again.status == "requested"
    (requested,) = await _retry_requests(async_session, run)
    assert requested.details["step"] == "plan"
    assert (requested.details["attempt"], requested.details["next_attempt"]) == (1, 2)
    assert requested.details["requested_by"] == "operator@example.com"


async def _refused(session: AsyncSession, run: StepRun) -> Conflict:
    with pytest.raises(Conflict) as refusal:
        await controls.request_retry(session, run, Actor.dashboard("operator@example.com"))
    assert await _retry_requests(session, run) == []
    return refusal.value


async def test_a_retry_now_is_refused_once_the_backoff_has_run(
    async_session: AsyncSession,
) -> None:
    run = await _waiting_to_retry(async_session, ended_ago=timedelta(minutes=11))
    assert (await _refused(async_session, run)).code == "already_due"


async def test_a_retry_now_is_refused_for_a_run_that_did_not_fail(
    async_session: AsyncSession,
) -> None:
    run = await _waiting_to_retry(async_session, status=StepRunStatus.SUCCEEDED)
    assert "isn't waiting to be retried" in (await _refused(async_session, run)).message


async def test_a_retry_now_is_refused_for_a_blocked_ticket(
    async_session: AsyncSession,
) -> None:
    """Blocked is where an exhausted retry ends: the un-block grants the next attempt."""
    run = await _waiting_to_retry(async_session, ticket_status=TicketStatus.BLOCKED)
    assert "isn't waiting to be retried" in (await _refused(async_session, run)).message


async def test_a_retry_now_is_refused_for_an_attempt_that_is_not_the_latest(
    async_session: AsyncSession,
) -> None:
    run = await _waiting_to_retry(async_session)
    async_session.add(
        StepRun(
            ticket_id=run.ticket_id,
            step_name="plan",
            kind=StepKind.AGENT,
            attempt=2,
            status=StepRunStatus.RUNNING,
        )
    )
    await async_session.flush()
    assert "isn't waiting to be retried" in (await _refused(async_session, run)).message


async def test_a_retry_now_is_refused_while_quarantined(
    async_session: AsyncSession,
) -> None:
    run = await _waiting_to_retry(async_session)
    ticket = await async_session.get(Ticket, run.ticket_id)
    assert ticket is not None
    ticket.quarantined_at = datetime.now(UTC)
    await async_session.flush()
    assert (await _refused(async_session, run)).code == "quarantined"


# ----------------------------------------------------------------------------- step retry


@pytest.mark.parametrize("via", [RequestedVia.DASHBOARD, RequestedVia.MCP, RequestedVia.CHAT])
async def test_only_the_dashboard_may_press_retry(
    async_session: AsyncSession, via: RequestedVia
) -> None:
    run = await _waiting_to_retry(async_session)
    actor = Actor(
        by="operator@example.com", via=via, chat_proposal_id=1 if via is RequestedVia.CHAT else None
    )
    if via is RequestedVia.DASHBOARD:
        result = await controls.request_step_retry(async_session, run, actor)
        assert result.status == "requested"
        (asked,) = await _events(async_session, run, EventType.STEP_RETRY_REQUESTED)
        assert (asked.details["attempt"], asked.details["next_attempt"]) == (1, 2)
    else:
        with pytest.raises(Forbidden):
            await controls.request_step_retry(async_session, run, actor)
        assert await _events(async_session, run, EventType.STEP_RETRY_REQUESTED) == []


# ------------------------------------------------------------------------- force complete


@pytest.mark.parametrize("via", [RequestedVia.MCP, RequestedVia.CHAT])
async def test_force_complete_is_dashboard_only(
    async_session: AsyncSession, via: RequestedVia
) -> None:
    run = await _waiting_to_retry(async_session)
    with pytest.raises(Forbidden):
        await controls.request_completion(
            async_session, run, Actor(by="x", via=via), allow_unmet=False, seen_failures=()
        )
    assert await _events(async_session, run, EventType.COMPLETION_REQUESTED) == []


async def test_force_complete_records_one_request_until_the_loop_answers(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    run = await _waiting_to_retry(async_session)

    async with client_for(_app(async_session, _Adapter())) as client:
        first = await client.post(f"/api/step-runs/{run.id}/complete", json={}, headers=auth)
        again = await client.post(f"/api/step-runs/{run.id}/complete", json={}, headers=auth)

    assert first.status_code == again.status_code == 202
    assert (first.json()["status"], again.json()["status"]) == ("requested", "pending")
    assert first.json()["request_id"] == again.json()["request_id"]
    assert first.json()["outcome"] == "pending"
    (asked,) = await _events(async_session, run, EventType.COMPLETION_REQUESTED)
    assert asked.details["allow_unmet"] is False and asked.details["requested_by"] == "api-token"


async def test_continue_with_warning_while_the_plain_check_is_pending_is_refused(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """Folded into the pending check, the dialog would show the check's `unmet` as the answer
    to Continue with warning."""
    run = await _waiting_to_retry(async_session)
    await _lifecycle(
        async_session,
        run,
        EventType.COMPLETION_REQUESTED,
        allow_unmet=False,
        seen_failures=[],
        requested_by="op@example.com",
        requested_via="dashboard",
    )
    body = {"allow_unmet": True, "seen_failures": ["branch_exists(spec/12-*) = false"]}

    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.post(f"/api/step-runs/{run.id}/complete", json=body, headers=auth)

    assert response.status_code == 409
    assert len(await _events(async_session, run, EventType.COMPLETION_REQUESTED)) == 1


async def test_force_complete_on_a_newer_attempt_while_a_check_is_pending_is_refused(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The dialog would poll the pending request through this run's URL, which answers 404."""
    first = await _waiting_to_retry(async_session, ended_ago=timedelta(minutes=20))
    await _lifecycle(
        async_session,
        first,
        EventType.COMPLETION_REQUESTED,
        allow_unmet=False,
        seen_failures=[],
        requested_by="op@example.com",
        requested_via="dashboard",
    )
    second = StepRun(
        ticket_id=first.ticket_id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=2,
        status=StepRunStatus.FAILED,
        runtime_run_id="run-2",
        ended_at=datetime.now(UTC),
    )
    async_session.add(second)
    await async_session.flush()

    async with client_for(_app(async_session, _Adapter())) as client:
        response = await client.post(f"/api/step-runs/{second.id}/complete", json={}, headers=auth)

    assert response.status_code == 409
    assert await _events(async_session, second, EventType.COMPLETION_REQUESTED) == []


# ------------------------------------------------------------------------------- approve

REVIEWER = "reviewer@example.com"
HEAD = "c" * 40
RETENTION = timedelta(days=7)


async def _awaiting_review(
    session: AsyncSession,
    *,
    status: str = StepRunStatus.IDLE,
    ticket_status: str = TicketStatus.RUNNING,
    reviewed: bool = True,
) -> StepRun:
    """Attempt 1 of `plan`, whose postconditions call `request_approval()` when `reviewed`, on
    issue 12, so its branch is `spec/12-*`."""
    project = await seed_project(session)
    ticket = await seed_ticket(
        session, project, step="plan", status=ticket_status, external_key="12"
    )
    version = await session.get(ProcessVersion, ticket.process_version_id)
    assert version is not None
    content = _process("proc").model_dump(mode="json", by_alias=True)
    if reviewed:
        content["steps"][0]["postconditions"] = {
            "all": ['branch_exists("spec/{ticket}-*")', "request_approval()"]
        }
    version.content = content
    run = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=status,
        runtime_run_id="run-1",
    )
    session.add(run)
    await session.flush()
    return run


def _observing(*branches: BranchState) -> RunObserver:
    """The repo as the forge shows it: `spec/12-widget` at `HEAD` unless `branches` are given."""
    held = branches or (BranchState(name="spec/12-widget", sha=HEAD),)

    async def observe() -> StateSource | None:
        return SnapshotState(
            repo="acme/widgets",
            issue_state=IssueState(
                number=12,
                title="Add a widget",
                body="",
                state="open",
                labels=frozenset(),
                assignees=(),
            ),
            branch_states=held,
        )

    return observe


def _review_app(session: AsyncSession, observed: RunObserver) -> FastAPI:
    app = _app(session, _Adapter())
    app.dependency_overrides[run_observer_dependency] = lambda: observed
    return app


async def _press_approve(
    session: AsyncSession, run: StepRun, *, signed_in: bool = True, bearer: bool = False
) -> httpx.Response:
    headers = {"Sec-Fetch-Site": "same-origin"}
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    async with client_for(_review_app(session, _observing())) as client:
        if signed_in:
            await ensure_member(session, REVIEWER)
            client.cookies.set(
                SESSION_COOKIE,
                mint_session(
                    REVIEWER, verified=True, api_token=TOKEN, now=datetime.now(UTC), max_age=600
                ),
            )
        return await client.post(
            f"/api/step-runs/{run.id}/approve", json={"head_sha": HEAD}, headers=headers
        )


async def _approve(
    session: AsyncSession, run: StepRun, *, head_sha: str = HEAD, actor: Actor | None = None
) -> RequestResult:
    return await controls.request_approval(
        session,
        run,
        actor or Actor.dashboard(REVIEWER),
        head_sha=head_sha,
        observed=_observing(),
        retention=RETENTION,
    )


async def _approval_refused(session: AsyncSession, run: StepRun, **kwargs: Any) -> Conflict:
    with pytest.raises(Conflict) as refusal:
        await _approve(session, run, **kwargs)
    assert await _events(session, run, EventType.STEP_APPROVED) == []
    return refusal.value


async def test_an_approval_records_who_approved_which_commit_as_the_predicate_reads_it(
    async_session: AsyncSession,
) -> None:
    run = await _awaiting_review(async_session)

    response = await _press_approve(async_session, run)

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "requested"
    (approved,) = await _events(async_session, run, EventType.STEP_APPROVED)
    assert approved.details == {
        "step": "plan",
        "attempt": 1,
        "head_sha": HEAD,
        "branch": "spec/12-widget",
        "approved_by": REVIEWER,
        "requested_via": "dashboard",
    }
    approval = await latest_approval(async_session, run.ticket_id, run.id)
    assert approval is not None
    assert (approval.head_sha, approval.approved_by) == (HEAD, REVIEWER)


async def test_the_shared_bearer_alone_cannot_approve(async_session: AsyncSession) -> None:
    """It names no one, and an approval is a person vouching for a commit."""
    run = await _awaiting_review(async_session)

    response = await _press_approve(async_session, run, signed_in=False, bearer=True)

    assert response.status_code == 403
    assert await _events(async_session, run, EventType.STEP_APPROVED) == []


@pytest.mark.parametrize("via", [RequestedVia.MCP, RequestedVia.CHAT])
async def test_approve_is_dashboard_only(async_session: AsyncSession, via: RequestedVia) -> None:
    run = await _awaiting_review(async_session)
    with pytest.raises(Forbidden):
        await _approve(async_session, run, actor=Actor(by=REVIEWER, via=via))
    assert await _events(async_session, run, EventType.STEP_APPROVED) == []


async def test_an_approval_of_a_commit_the_branch_has_moved_past_is_refused(
    async_session: AsyncSession,
) -> None:
    run = await _awaiting_review(async_session)
    assert (await _approval_refused(async_session, run, head_sha="d" * 40)).code == "head_moved"


async def test_an_approval_while_the_agent_works_is_refused(async_session: AsyncSession) -> None:
    run = await _awaiting_review(async_session, status=StepRunStatus.RUNNING)
    assert (await _approval_refused(async_session, run)).code == "mid_turn"


async def test_a_step_that_awaits_no_review_takes_no_approval(
    async_session: AsyncSession,
) -> None:
    run = await _awaiting_review(async_session, reviewed=False)
    assert (await _approval_refused(async_session, run)).code == "not_reviewed"


@pytest.mark.parametrize(
    ("parked", "refusal"),
    [
        (EventType.AGENT_PARKED, None),
        (EventType.AGENT_INTERRUPTED, "not_waiting"),
    ],
)
async def test_a_parked_run_is_approved_only_where_its_session_sat_at_its_prompt(
    async_session: AsyncSession, parked: EventType, refusal: str | None
) -> None:
    """A person's stop is theirs to release: the re-check never accepts it, so an approval of it
    would wait on nothing."""
    run = await _awaiting_review(
        async_session, status=StepRunStatus.INTERRUPTED, ticket_status=TicketStatus.BLOCKED
    )
    run.ended_at = datetime.now(UTC)
    await _lifecycle(
        async_session,
        run,
        parked,
        **({"reason": "idle", "at_prompt": True} if parked == EventType.AGENT_PARKED else {}),
    )
    if refusal is None:
        assert (await _approve(async_session, run)).status == "requested"
    else:
        assert (await _approval_refused(async_session, run)).code == refusal


async def test_a_run_the_worker_parks_during_the_branch_read_is_approved(
    async_session: AsyncSession,
) -> None:
    """The ticket is judged as it stands once the run is locked, not as it was read before the
    forge answered."""
    run = await _awaiting_review(async_session)
    seen = _observing()

    async def worker_parks_meanwhile() -> StateSource | None:
        # Behind the session's back, as the worker's own commit would land.
        stale = {"synchronize_session": False}
        await async_session.execute(
            update(StepRun)
            .where(StepRun.id == run.id)
            .values(status=StepRunStatus.INTERRUPTED, ended_at=datetime.now(UTC))
            .execution_options(**stale)
        )
        await async_session.execute(
            update(Ticket)
            .where(Ticket.id == run.ticket_id)
            .values(status=TicketStatus.BLOCKED)
            .execution_options(**stale)
        )
        await _lifecycle(async_session, run, EventType.AGENT_PARKED, reason="idle", at_prompt=True)
        return await seen()

    result = await controls.request_approval(
        async_session,
        run,
        Actor.dashboard(REVIEWER),
        head_sha=HEAD,
        observed=worker_parks_meanwhile,
        retention=RETENTION,
    )
    assert result.status == "requested"


async def test_a_second_press_by_the_same_person_records_nothing(
    async_session: AsyncSession,
) -> None:
    run = await _awaiting_review(async_session)

    first = await _approve(async_session, run)
    again = await _approve(async_session, run)

    assert (first.status, again.status) == ("requested", "pending")
    assert len(await _events(async_session, run, EventType.STEP_APPROVED)) == 1


async def test_the_review_of_a_running_run_says_why_it_cannot_be_approved_and_reads_no_forge(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The dashboard polls the review while Approve is mounted: a refusal already decided must
    not list every branch of the repository on each poll."""

    async def unread() -> StateSource | None:
        raise AssertionError("the forge was read")

    run = await _awaiting_review(async_session, status=StepRunStatus.RUNNING)

    async with client_for(_review_app(async_session, unread)) as client:
        response = await client.get(f"/api/step-runs/{run.id}/review", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "awaits_review": True,
        "branch": None,
        "head_sha": None,
        "approval": None,
        "offered": False,
        "refusal": "The agent is still working. Approve when it stops.",
        "pushed": False,
    }


@pytest.mark.parametrize(
    ("at_spawn", "pushed"),
    [
        # Another step on the same branch left it here: this run has pushed nothing.
        (HEAD, False),
        ("b" * 40, True),
        # The spawn recorded no head for the branch, as for a step that declares no files.
        (None, True),
    ],
)
async def test_the_review_says_whether_the_run_has_pushed(
    async_session: AsyncSession, auth: dict[str, str], at_spawn: str | None, pushed: bool
) -> None:
    """On a lane whose steps share a branch, the branch has a head before the run pushes: read as
    the run's work, the dashboard asks a person to approve a commit the agent never made."""
    run = await _awaiting_review(async_session)
    heads = [] if at_spawn is None else [{"name": "spec/12-widget", "sha": at_spawn}]
    await _lifecycle(async_session, run, EventType.AGENT_SPAWNED, branch_heads=heads)

    async with client_for(_review_app(async_session, _observing())) as client:
        body = (await client.get(f"/api/step-runs/{run.id}/review", headers=auth)).json()

    assert (body["pushed"], body["head_sha"], body["refusal"]) == (pushed, HEAD, None)


def _unanswering(error: Exception) -> RunObserver:
    """A forge that gives no answer: `branches()` raises `error`."""

    class _Down(SnapshotState):
        async def branches(self) -> Sequence[BranchState]:
            raise error

    async def observe() -> StateSource | None:
        source = await _observing()()
        assert isinstance(source, SnapshotState)
        return _Down(repo=source.repo, issue_state=source.issue_state)

    return observe


@pytest.mark.parametrize(
    "error",
    [
        RequestTimeout(httpx.ReadTimeout("timed out")),
        RequestError(httpx.ConnectError("connection refused")),
        # What the GitLab client makes of a refused connection, and a timeout it passes on.
        GitLabError("cannot reach GitLab at https://gitlab.test", status_code=None),
        httpx.ReadTimeout("timed out"),
    ],
    ids=["github-timeout", "github-connection", "gitlab-connection", "gitlab-timeout"],
)
async def test_a_forge_that_gives_no_answer_refuses_approve_as_unread(
    async_session: AsyncSession, auth: dict[str, str], error: Exception
) -> None:
    """A 500 has no CORS headers, so the dashboard reports it as an unreachable API."""
    run = await _awaiting_review(async_session)

    with pytest.raises(UpstreamFailed) as refusal:
        await controls.request_approval(
            async_session,
            run,
            Actor.dashboard(REVIEWER),
            head_sha=HEAD,
            observed=_unanswering(error),
            retention=RETENTION,
        )
    async with client_for(_review_app(async_session, _unanswering(error))) as client:
        review = await client.get(f"/api/step-runs/{run.id}/review", headers=auth)

    assert refusal.value.message == "Factorial couldn't read this step's branch."
    assert review.status_code == 200, review.text
    assert review.json()["refusal"] == controls._UNREAD
    assert await _events(async_session, run, EventType.STEP_APPROVED) == []


async def test_the_review_of_a_waiting_run_with_nothing_pushed_offers_a_refused_approve(
    async_session: AsyncSession, auth: dict[str, str]
) -> None:
    """The dashboard draws Approve when it is offered and disables it with the refusal."""
    run = await _awaiting_review(async_session)

    observed = _observing(BranchState(name="main", sha=HEAD))

    async with client_for(_review_app(async_session, observed)) as client:
        body = (await client.get(f"/api/step-runs/{run.id}/review", headers=auth)).json()

    assert (body["offered"], body["refusal"]) == (True, controls._NO_BRANCH)
