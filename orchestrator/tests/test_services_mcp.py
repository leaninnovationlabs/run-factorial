"""The transport-neutral services (`api/services`) that the dashboard routes and the MCP tools
share."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.actor import Actor
from orchestrator.api.services.errors import (
    Conflict,
    Invalid,
    NotFound,
    Unavailable,
    UpstreamFailed,
    render,
)
from orchestrator.api.services.intake import CreatedTicket, create_ticket
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.api.services.refs import resolve_issue, resolve_project
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events import EventType, RequestedVia
from orchestrator.events.read_models import blocked_on, select_tickets
from orchestrator.factorial import DEFAULT_KEY
from orchestrator.github import IssueState, SnapshotState, StateSource
from orchestrator.github.issues import marker_line
from tests.api_harness import seed_project, seed_ticket
from tests.reconcile_world import _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_resolve_project_refuses_a_repo_two_projects_read_issues_from(
    async_session: AsyncSession,
) -> None:
    """One project reads issues from its own repo. The other names that repo as its ticket
    source. Either guess would exit, un-block or open issues on a project the caller did not
    mean, so the ref is refused and both names come back. Naming the project settles it."""
    shared = f"acme/intake-{uuid4().hex[:12]}"
    own = await seed_project(async_session, repo=shared)
    other = await seed_project(async_session)
    async_session.add(
        TicketSource(project_id=other.id, kind=TicketSourceKind.GITHUB, external_ref=shared)
    )
    await async_session.flush()
    ours = await default_organization_id(async_session)

    with pytest.raises(Invalid) as refused:
        await resolve_project(async_session, ours, shared.upper())
    named = await resolve_issue(async_session, ours, f"{shared.upper()}#5", other.name)

    assert set(refused.value.candidates) == {own.name, other.name}
    assert (named.project.id, named.repo, named.number) == (other.id, shared, 5)
    with pytest.raises(Invalid, match="not in project"):
        await resolve_issue(
            async_session, ours, f"{shared}#5", (await seed_project(async_session)).name
        )


async def test_resolve_project_takes_the_id_its_dashboard_page_shows(
    async_session: AsyncSession,
) -> None:
    """`/projects/3` names the project by its id, so `id:3` resolves it as `id:N` does a ticket.
    An id no row has is not found; one too long for the key is not found either, rather than an
    error from the database."""
    project = await seed_project(async_session)
    ours = await default_organization_id(async_session)

    found = await resolve_project(async_session, ours, f"id:{project.id}")

    assert found.id == project.id
    with pytest.raises(NotFound, match="No project has ID"):
        await resolve_project(async_session, ours, f"id:{project.id + 1_000_000}")
    with pytest.raises(NotFound, match="No project"):
        await resolve_project(async_session, ours, "id:" + "9" * 30)
    # A bare number is a name, never a guessed id, but the refusal says how to pass the id.
    with pytest.raises(NotFound) as bare:
        await resolve_project(async_session, ours, str(project.id))
    assert f"Next: Pass id:{project.id}" in render(bare.value)


async def test_blocked_state_includes_quarantined_and_waiting_gate_tickets(
    async_session: AsyncSession,
) -> None:
    """`blocked` is wider than the status column: a gate waits on a person, and quarantine hides
    behind any status (decision 31). Narrowing it back to `status == 'blocked'` silently drops
    both from triage and from the projects list's counts."""
    project = await seed_project(async_session)
    escalated = await seed_ticket(async_session, project, status=TicketStatus.BLOCKED)
    gate = await seed_ticket(async_session, project, status=TicketStatus.WAITING_GATE)
    quarantined = await seed_ticket(async_session, project, status=TicketStatus.RUNNING)
    quarantined.quarantined_at = datetime.now(UTC)
    running = await seed_ticket(async_session, project, status=TicketStatus.RUNNING)
    closed = await seed_ticket(async_session, project, status=TicketStatus.BLOCKED)
    closed.closed_at = datetime.now(UTC)
    await async_session.flush()
    week_ago = datetime.now(UTC) - timedelta(days=7)

    rows, total = await select_tickets(
        async_session,
        organization_id=project.organization_id,
        project_ids=[project.id],
        state="blocked",
        closed_since=week_ago,
        limit=50,
    )

    assert {row.id: blocked_on(row) for row in rows} == {
        escalated.id: "escalated",
        gate.id: "gate",
        quarantined.id: "quarantined",
    }
    assert total == 3
    running_rows, _ = await select_tickets(
        async_session,
        organization_id=project.organization_id,
        project_ids=[project.id],
        state="running",
        closed_since=week_ago,
        limit=50,
    )
    assert [row.id for row in running_rows] == [running.id]


async def test_a_limited_list_still_reports_the_full_total(async_session: AsyncSession) -> None:
    """Tools set `truncated` from `total`; a total that counted only the page hides tickets."""
    project = await seed_project(async_session)
    for _ in range(2):
        await seed_ticket(async_session, project, status=TicketStatus.BLOCKED)
    await async_session.flush()

    rows, total = await select_tickets(
        async_session,
        organization_id=project.organization_id,
        project_ids=[project.id],
        state="blocked",
        closed_since=datetime.now(UTC) - timedelta(days=7),
        limit=1,
    )

    assert (len(rows), total) == (1, 2)


# --- ticket creation (spec §8 #9) -------------------------------------------------------------


@dataclass
class _FakeIssues:
    """An `IssueWriter` over a list. `lose_next_reply` raises after the issue exists, as a
    timeout after GitHub acted does; `drop_next_request` raises before it does. `calls` names
    every GitHub call, in order."""

    opened: list[IssueState] = field(default_factory=list)
    lose_next_reply: bool = False
    drop_next_request: bool = False
    calls: list[str] = field(default_factory=list)

    async def assignable(self, repo: str, login: str) -> bool:
        self.calls.append("assignable")
        return True

    async def labels(self, repo: str) -> frozenset[str]:
        self.calls.append("labels")
        return frozenset({"factorial:new"})

    async def create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
        assignees: Sequence[str],
    ) -> IssueState:
        self.calls.append("create")
        if self.drop_next_request:
            self.drop_next_request = False
            raise TimeoutError("the request never reached GitHub")
        issue = IssueState(
            number=len(self.opened) + 1,
            title=title,
            body=body,
            state="open",
            labels=frozenset(labels),
            assignees=tuple(assignees),
        )
        self.opened.append(issue)
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise TimeoutError("the reply was lost after GitHub opened the issue")
        return issue

    async def find_marked(
        self, repo: str, marker: str, *, creator: str, since: datetime
    ) -> IssueState | None:
        self.calls.append("find_marked")
        line = marker_line(marker)
        return next(
            (issue for issue in self.opened if issue.body.rstrip().splitlines()[-1] == line),
            None,
        )

    def state_source(self, repo: str, number: int) -> StateSource:
        issue = next(issue for issue in self.opened if issue.number == number)
        return SnapshotState(repo=repo, issue_state=issue)


async def _intake_project(session: AsyncSession) -> Project:
    """An active project whose default lane admits on `factorial:new` plus an assignee."""
    project = await seed_project(session)
    project.active = True
    project.settings = {DEFAULT_KEY: "standard"}
    session.add(
        TicketSource(
            project_id=project.id,
            kind=TicketSourceKind.GITHUB,
            external_ref=project.github_repo,
        )
    )
    session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key="standard",
            content=_process("standard").model_dump(mode="json", by_alias=True),
            source_commit_sha="b" * 40,
        )
    )
    await session.flush()
    return project


async def _create(
    session: AsyncSession,
    issues: _FakeIssues,
    project: Project,
    *,
    key: str | None,
    body: str = "Make the build green.",
) -> CreatedTicket:
    return await create_ticket(
        session,
        issues,
        project,
        title="Fix the flaky build",
        body=body,
        assignee="operator",
        process=None,
        idempotency_key=key,
        actor=Actor(by="dev@example.com", via=RequestedVia.MCP, mcp_token_id=None),
        bot_login="factorial-test[bot]",
        state_source=issues.state_source,
    )


async def test_a_retried_create_with_the_same_key_opens_no_second_issue(
    async_session: AsyncSession,
) -> None:
    """A client retries a call whose answer it never saw. A second issue is a second ticket: a
    second paid run of every agent and a second set of gates for one request."""
    project = await _intake_project(async_session)
    issues = _FakeIssues()

    first = await _create(async_session, issues, project, key="retry-1")
    second = await _create(async_session, issues, project, key="retry-1")

    assert len(issues.opened) == 1
    assert (second.number, second.replayed) == (first.number, True)


async def test_a_reply_lost_after_github_opened_the_issue_is_recovered_by_its_marker(
    async_session: AsyncSession,
) -> None:
    """The request is recorded before GitHub is asked, so a reply lost after GitHub acted leaves
    it unanswered. The retry must adopt that issue by its marker, not open another."""
    project = await _intake_project(async_session)
    issues = _FakeIssues(lose_next_reply=True)
    with pytest.raises(UpstreamFailed):
        await _create(async_session, issues, project, key="lost-1")
    await async_session.rollback()
    await async_session.refresh(project)

    again = await _create(async_session, issues, project, key="lost-1")

    assert len(issues.opened) == 1
    assert again.number == issues.opened[0].number
    answers = (
        await async_session.scalars(
            select(Event).where(
                Event.project_id == project.id, Event.type == EventType.ISSUE_CREATED
            )
        )
    ).all()
    assert [answer.details["recovered"] for answer in answers] == [True]


async def test_no_issue_writer_is_unavailable_with_its_next_step(
    async_session: AsyncSession,
) -> None:
    """An unconfigured App is a 503 that tells a human what to set, never a 500."""
    project = await _intake_project(async_session)

    with pytest.raises(Unavailable) as refused:
        await create_ticket(
            async_session,
            None,
            project,
            title="Fix the flaky build",
            body="",
            assignee="operator",
            process=None,
            idempotency_key=None,
            actor=Actor(by="dev@example.com", via=RequestedVia.MCP, mcp_token_id=None),
            bot_login="factorial-test[bot]",
            state_source=_FakeIssues().state_source,
        )

    assert refused.value.message.startswith("This project has no GitHub connection")
    assert refused.value.hint is not None and "Ticket source" in refused.value.hint


async def test_a_marker_in_the_callers_text_is_refused(async_session: AsyncSession) -> None:
    """Recovery adopts an issue whose body ends with a request's marker. A caller who could
    write one could have a later recovery adopt an issue they chose."""
    project = await _intake_project(async_session)
    issues = _FakeIssues()

    with pytest.raises(Invalid):
        await _create(
            async_session,
            issues,
            project,
            key=None,
            body="Fix it.\n<!-- factorial-request:1:0123456789abcdef -->",
        )

    assert issues.opened == []


async def test_a_key_reused_for_different_arguments_is_refused(
    async_session: AsyncSession,
) -> None:
    """Replaying the first issue would tell the caller their second ticket exists when it does
    not."""
    project = await _intake_project(async_session)
    issues = _FakeIssues()
    await _create(async_session, issues, project, key="reuse-1")

    with pytest.raises(Invalid, match="different arguments"):
        await _create(async_session, issues, project, key="reuse-1", body="A different ask.")

    assert len(issues.opened) == 1


async def test_a_paused_project_refuses_before_github_is_asked(
    async_session: AsyncSession,
) -> None:
    """An issue opened on a paused project waits for ever, and the caller is told nothing."""
    project = await _intake_project(async_session)
    project.active = False
    await async_session.flush()
    issues = _FakeIssues()

    with pytest.raises(Conflict, match="paused"):
        await _create(async_session, issues, project, key=None)

    assert issues.calls == []


async def test_a_keyed_retry_on_a_since_paused_project_opens_nothing(
    async_session: AsyncSession,
) -> None:
    """A retry finds its request recorded and skips the preflight. With no issue to recover, it
    would otherwise open one on a project paused since the first call, which waits for ever."""
    project = await _intake_project(async_session)
    issues = _FakeIssues(drop_next_request=True)
    with pytest.raises(UpstreamFailed):
        await _create(async_session, issues, project, key="paused-1")
    await async_session.rollback()
    await async_session.refresh(project)
    project.active = False
    await async_session.flush()
    issues.calls.clear()

    with pytest.raises(Conflict, match="paused"):
        await _create(async_session, issues, project, key="paused-1")

    assert "create" not in issues.calls
    assert issues.opened == []


async def test_a_lost_reply_is_recovered_after_the_issue_was_retitled_and_reassigned(
    async_session: AsyncSession,
) -> None:
    """The projector replaces assignees and anyone may retitle the issue before the retry. Only
    the marker proves the issue is the request's; judging it by anything else opens a second."""
    project = await _intake_project(async_session)
    issues = _FakeIssues(lose_next_reply=True)
    with pytest.raises(UpstreamFailed):
        await _create(async_session, issues, project, key="moved-1")
    await async_session.rollback()
    await async_session.refresh(project)
    issues.opened[0] = replace(
        issues.opened[0], title="Fix the flaky build (triaged)", assignees=("reviewer-1",)
    )

    again = await _create(async_session, issues, project, key="moved-1")

    assert len(issues.opened) == 1
    assert again.number == issues.opened[0].number
