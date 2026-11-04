"""The one admission rule the gateway, `GET /terminal` and the inspect pass share (decision 57)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import StepKind, StepRun, StepRunStatus, TicketStatus
from orchestrator.events import EventType
from orchestrator.events.terminals import (
    READ_ONLY_MOVED,
    READ_ONLY_VIEW,
    Admission,
    terminal_admission,
)
from orchestrator.terminal.protocol import CloseCode
from runtime.contract import TerminalTarget
from tests.terminal_rows import record, seed_run

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

SESSION = TerminalTarget(substrate="host", socket="/tmp/s", session="run")
SHELL = TerminalTarget(substrate="host", socket="/tmp/s-i", session="inspect")


async def _admit(session: AsyncSession, run_id: int, *, enabled: bool = True) -> Admission:
    return await terminal_admission(
        session, run_id, now=datetime.now(UTC), retention=timedelta(days=7), enabled=enabled
    )


def _outcome(admission: Admission) -> object:
    if admission.refusal is not None:
        return admission.refusal.code
    return admission.kind, admission.target


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("switched off", CloseCode.FORBIDDEN),
        ("quarantined", CloseCode.FORBIDDEN),
        ("live session", ("session", SESSION)),
        ("idle session", ("session", SESSION)),
        ("live session with no recorded target", CloseCode.NO_TERMINAL),
        ("parked session", CloseCode.PARKED),
        ("parked session with an open shell", ("inspect", SHELL)),
        ("parked session whose shell parked", CloseCode.PARKED),
        ("finished run with an open shell", ("inspect", SHELL)),
        ("finished run", CloseCode.NO_TERMINAL),
        ("gate", CloseCode.NO_TERMINAL),
    ],
)
async def test_the_admission_rule(async_session: AsyncSession, case: str, expected: object) -> None:
    status = {
        "idle": StepRunStatus.IDLE,
        "parked": StepRunStatus.INTERRUPTED,
        "finished": StepRunStatus.SUCCEEDED,
    }
    run = await seed_run(
        async_session,
        target=None if "no recorded target" in case else SESSION,
        status=next((s for word, s in status.items() if word in case), StepRunStatus.RUNNING),
        kind=StepKind.HUMAN_GATE if case == "gate" else StepKind.AGENT,
        quarantined=case == "quarantined",
    )
    if "shell" in case:
        await record(async_session, run, EventType.INSPECT_STARTED, terminal=SHELL.model_dump())
    if "shell parked" in case:
        await record(async_session, run, EventType.INSPECT_PARKED, reason="idle")

    admission = await _admit(async_session, run.id, enabled=case != "switched off")

    assert _outcome(admission) == expected
    if admission.refusal is not None:
        assert len(admission.refusal.sentence.encode()) <= 123


async def test_a_resume_that_recorded_no_target_leaves_the_session_unattachable(
    async_session: AsyncSession,
) -> None:
    """The spawn's target may name a session the resume replaced: attaching to it could put a
    person in front of the wrong session, so the latest spawn or resume decides alone."""
    run = await seed_run(async_session, target=SESSION)
    await record(async_session, run, EventType.AGENT_RESUMED)

    admission = await _admit(async_session, run.id)

    assert _outcome(admission) == CloseCode.NO_TERMINAL


async def _later_attempt(session: AsyncSession, run: StepRun) -> None:
    session.add(
        StepRun(
            ticket_id=run.ticket_id,
            step_name=run.step_name,
            kind=StepKind.AGENT,
            attempt=run.attempt + 1,
            status=StepRunStatus.SUCCEEDED,
        )
    )
    await session.flush()


@pytest.mark.parametrize(
    ("case", "writable", "read_only"),
    [
        ("current run", True, None),
        ("ticket on another step", False, READ_ONLY_MOVED),
        ("a later attempt exists", False, READ_ONLY_MOVED),
        ("ticket done", False, READ_ONLY_MOVED),
        ("open session", False, READ_ONLY_VIEW),
    ],
)
async def test_only_the_ticket_s_current_run_is_writable(
    async_session: AsyncSession, case: str, writable: bool, read_only: str | None
) -> None:
    """Typing into a session the ticket has moved past would drive an agent whose work no longer
    counts, so only the latest attempt at the ticket's current step may be typed into."""
    run = await seed_run(
        async_session,
        target=None if case == "open session" else SESSION,
        status=StepRunStatus.SUCCEEDED if case == "open session" else StepRunStatus.RUNNING,
        ticket_step="review" if case == "ticket on another step" else "develop",
        ticket_status=TicketStatus.DONE if case == "ticket done" else TicketStatus.RUNNING,
    )
    if case == "a later attempt exists":
        await _later_attempt(async_session, run)
    if case == "open session":
        await record(async_session, run, EventType.INSPECT_STARTED, terminal=SHELL.model_dump())

    admission = await _admit(async_session, run.id)

    assert admission.refusal is None
    assert admission.writable is writable
    assert admission.read_only == read_only
