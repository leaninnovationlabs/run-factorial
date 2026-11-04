"""Admission — deciding which issues enter a process, which lane they enter, and pinning it.

This is the **one** place that reads a project's *current* `process_version` — one per available
lane now, not one per project. Everything after admission re-parses the snapshot the ticket
pinned (design.md §2.8), so editing config in the repo creates a new version row and never
touches a ticket already in flight.

A pass runs in two steps and the order is load-bearing: `worker.selection` resolves which lane an
issue enters, from an explicit `factorial:process:<key>` label or the project default, and only
then is THAT lane's `entry.when` evaluated. A lane whose entry condition is false means the issue
waits; it never tries another lane.

Admission is idempotent by construction: `(ticket_source_id, external_key)` is unique, so a second
pass over the same issue loses the insert race and moves on. It is never a re-admission — a ticket
that has already finished stays finished.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import partial
from typing import Final, Literal, Protocol

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import (
    AllOf,
    AnyOf,
    Condition,
    Not,
    PredicateCall,
    ProcessConfig,
    expand,
    placeholders,
)
from orchestrator.config.conditions import iter_calls
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
    intake_repo,
)
from orchestrator.db.session import SessionScope
from orchestrator.events import EventType, append
from orchestrator.factorial.process_policy import DefaultProcessInvalid, parse_default_process
from orchestrator.factorial.registry import latest_process_versions
from orchestrator.github import (
    BLOCKED_LABELS,
    DONE_LABELS,
    EXIT_LABELS,
    INTERRUPTED_LABELS,
    WAITING_LABELS,
    IssueState,
    StateSource,
    list_issues,
)
from orchestrator.github.client import GitHubClient, is_rate_limited
from orchestrator.github.state import SeededIssue
from orchestrator.gitlab.client import GitLabClient
from orchestrator.gitlab.observer import GitLabStateSource
from orchestrator.predicates import EvalContext, Evaluation, evaluate, failures, needs
from orchestrator.worker.context import ProjectGitHub, ReconcileDeps
from orchestrator.worker.selection import (
    NotSelected,
    SelectionRefusal,
    process_label,
    select_process,
    selected_keys,
)
from orchestrator.worker.tick_context import _enter_step

__all__ = [
    "CandidateFactory",
    "CandidateSource",
    "EntryPlan",
    "GitHubCandidates",
    "GitLabCandidates",
    "InMemoryCandidates",
    "Lane",
    "NoLane",
    "Pending",
    "PendingReason",
    "admit_all",
    "admit_gitlab_all",
    "admit_issue",
    "admit_project",
    "entry_plan",
    "lane_for",
    "pending_issue",
    "pending_issues",
]

#: Why one open issue is not a ticket yet. The three selection refusals are misconfiguration and
#: get an `ADMISSION_REFUSED` event; `not_ready` is the ordinary state of every issue nobody has
#: marked for the factory yet, and gets none — an event per unlabelled issue would bury the three
#: that mean something.
PendingReason = SelectionRefusal | Literal["not_ready"]

logger = logging.getLogger(__name__)

#: One project's whole admission pass. Projects are admitted one after another, so without it one
#: hung connection would hold admission for every tenant. Generous: each open issue nobody has
#: admitted costs a read every pass.
ADMISSION_DEADLINE_SECONDS: Final = 300.0


@dataclass(frozen=True, slots=True)
class Pending:
    """One open issue this pass declined, and why — recomputed, never read back from the log.

    A refusal has to read as *current*. The event log answers "what happened"; an operator
    waiting on their issue is asking "is this still true", which only a fresh pass can answer.
    """

    external_key: str
    title: str
    #: The lane the issue selected, when selection got that far. None for a selection refusal.
    process_key: str | None
    reason: PendingReason
    detail: str
    #: The leaves of `entry.when` that are still false, as `failures()` writes them. Empty for a
    #: selection refusal, which never reached a lane whose entry condition could be evaluated.
    awaiting: tuple[str, ...] = ()
    #: `awaiting` in words, as `needs()` writes them.
    needs: tuple[str, ...] = ()


class CandidateSource(Protocol):
    """Which issues to consider for admission.

    A separate seam from `StateSource` (github/observer.py), which observes exactly one issue:
    widening the observer to list a repo's open issues would put a repo-wide fetch behind an
    interface every predicate call goes through.
    """

    async def open_issues(self, repo: str) -> Sequence[IssueState]: ...


#: A project's client to the candidate source read through it, because two organizations may
#: point at one repo with different credentials. `GitHubCandidates` is the production one.
CandidateFactory = Callable[[GitHubClient], CandidateSource]


@dataclass(frozen=True, slots=True)
class InMemoryCandidates:
    """A `CandidateSource` built from literals — what tests drive admission with."""

    issues_by_repo: Mapping[str, Sequence[IssueState]] = field(default_factory=dict)

    async def open_issues(self, repo: str) -> Sequence[IssueState]:
        return self.issues_by_repo.get(repo, ())


@dataclass(frozen=True, slots=True)
class GitHubCandidates:
    """The production `CandidateSource`: a repo's open issues, through `github.list_issues`."""

    client: GitHubClient

    async def open_issues(self, repo: str) -> Sequence[IssueState]:
        return await list_issues(self.client, repo, state="open", listing="admission")


@dataclass(frozen=True, slots=True)
class GitLabCandidates:
    """Open issues on a GitLab project. Never used for a GitHub ticket source."""

    client: GitLabClient

    async def open_issues(self, repo: str) -> Sequence[IssueState]:
        from orchestrator.gitlab.observer import list_issues as gitlab_list_issues

        return await gitlab_list_issues(self.client, repo, state="opened")


async def admit_project(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    candidates: CandidateSource,
    *,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> Sequence[Ticket]:
    """Admit every candidate issue that resolves a lane and whose lane's `entry.when` holds,
    observed through the project's own forge client."""
    lanes, default, default_note = await _lanes(session, project)
    if not lanes:
        logger.warning("project %s has no process version; nothing can be admitted", project.name)
        return ()

    admitted: list[Ticket] = []
    async for issue, outcome in _verdicts(
        session,
        project,
        ticket_source,
        candidates,
        lanes,
        default,
        default_note,
        state_source,
        bot_login,
    ):
        if isinstance(outcome, Pending):
            await _record_refusal(session, project, outcome)
            continue
        version, config, owner = outcome
        ticket = await _admit(session, project, ticket_source, version, config, issue, owner)
        if ticket is not None:
            admitted.append(ticket)
    return admitted


async def pending_issues(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    candidates: CandidateSource,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
    *,
    limit: int | None = None,
) -> Sequence[Pending]:
    """The same pass `admit_project` runs, reporting what it declines instead of admitting.

    Writes nothing, so a read route may call it, and it re-derives every verdict rather than
    reading the event log back — see `Pending`. It takes only what `entry.when` reads, so the
    route builds no runtime adapters. `limit` stops the walk once that many are found, because
    every issue it judges costs a GitHub read.
    """
    lanes, default, default_note = await _lanes(session, project)
    if not lanes:
        return ()  # the project's readiness checklist already says "no process version"
    out: list[Pending] = []
    verdicts = _verdicts(
        session,
        project,
        ticket_source,
        candidates,
        lanes,
        default,
        default_note,
        state_source,
        bot_login,
    )
    async with aclosing(verdicts):
        async for _, outcome in verdicts:
            if isinstance(outcome, Pending):
                out.append(outcome)
                if limit is not None and len(out) >= limit:
                    break
    return tuple(out)


async def _verdicts(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    candidates: CandidateSource,
    lanes: Mapping[str, ProcessVersion],
    default: str | None,
    default_note: str | None,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> AsyncGenerator[tuple[IssueState, tuple[ProcessVersion, ProcessConfig, str] | Pending]]:
    repo = intake_repo(project, ticket_source)
    known = await _known_keys(session, ticket_source.id)
    for issue in await candidates.open_issues(repo):
        if str(issue.number) in known:
            continue  # already admitted — including tickets that are long done
        yield (
            issue,
            await _decide(issue, repo, lanes, default, default_note, state_source, bot_login),
        )


async def pending_issue(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    number: int,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> Pending | None:
    """`pending_issues` for one issue: why the pass declines it, or None when the issue is a
    ticket already or the next pass admits it.

    Reads that one issue instead of listing the repo, so asking about one number costs one GitHub
    read rather than one per open issue.
    """
    verdict = await _judge_issue(session, project, ticket_source, number, state_source, bot_login)
    return verdict if isinstance(verdict, Pending) else None


type _Admissible = tuple[IssueState, tuple[ProcessVersion, ProcessConfig, str]]


async def _judge_issue(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    number: int,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> _Admissible | Pending | None:
    """One issue's verdict from one read of it: None when it is a ticket already, whatever its
    status, so a finished ticket is never admitted again."""
    admitted = await session.scalar(
        select(Ticket.id).where(
            Ticket.ticket_source_id == ticket_source.id, Ticket.external_key == str(number)
        )
    )
    if admitted is not None:
        return None
    repo = intake_repo(project, ticket_source)
    lanes, default, default_note = await _lanes(session, project)
    if not lanes:
        return Pending(
            external_key=str(number),
            title="",
            process_key=None,
            reason="not_ready",
            detail="the project has no process version, so nothing can be admitted",
        )
    source = state_source(repo, number)
    issue = await source.issue()
    if issue.state != "open":
        return Pending(
            external_key=str(number),
            title=issue.title,
            process_key=None,
            reason="not_ready",
            detail="the issue is closed, and admission reads open issues only",
        )
    # `_decide` builds its own source; handing this one back reuses the issue it already read.
    outcome = await _decide(
        issue, repo, lanes, default, default_note, lambda _repo, _number: source, bot_login
    )
    return outcome if isinstance(outcome, Pending) else (issue, outcome)


async def _admit_named(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    number: int,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> Sequence[Ticket]:
    verdict = await _judge_issue(session, project, ticket_source, number, state_source, bot_login)
    if verdict is None:
        return ()
    if isinstance(verdict, Pending):
        await _record_refusal(session, project, verdict)
        return ()
    issue, (version, config, owner) = verdict
    ticket = await _admit(session, project, ticket_source, version, config, issue, owner)
    return (ticket,) if ticket is not None else ()


async def _decide(
    issue: IssueState,
    repo: str,
    lanes: Mapping[str, ProcessVersion],
    default: str | None,
    default_note: str | None,
    state_source: Callable[[str, int], StateSource],
    bot_login: str,
) -> tuple[ProcessVersion, ProcessConfig, str] | Pending:
    """Resolve this issue's lane, then ask that lane whether it is ready. One issue, one verdict.

    Shared by the admitting pass and the diagnostic one, so an operator can never be shown a
    reason the worker does not actually act on; `_verdicts` shares the iteration, so the two
    passes also agree on which issues they consider.
    """
    selection = select_process(issue.labels, available=lanes.keys(), default=default)
    if isinstance(selection, NotSelected):
        return Pending(
            external_key=str(issue.number),
            title=issue.title,
            process_key=None,
            reason=selection.reason,
            # A settings bag that will not parse is the real cause of a missing default, and
            # "no default is set" would send an operator to set one that is already there.
            detail=default_note or selection.detail,
        )

    version = lanes[selection.key]
    config = ProcessConfig.model_validate(version.content)
    # Judged first on the listing, which already carries labels and assignees: a GET per open
    # issue every pass cost a repo's whole backlog in calls, for issues nobody marked for the
    # factory, and on GitLab kept the dashboard's pending route so slow that Admission stayed
    # at 0. One the listing admits is read again and judged on that read, because the listing
    # can lag a fresh assignment, and a gate whose approvers are `{ticket_owner}` would wait for
    # ever on "".
    seeded = SeededIssue(state_source(repo, issue.number), issue)
    owner = issue.assignees[0] if issue.assignees else ""
    entry = await _entry_result(config, repo, issue.number, seeded, owner, bot_login)
    if entry.value:
        source = seeded.inner
        observed = await source.issue()
        owner = observed.assignees[0] if observed.assignees else ""
        entry = await _entry_result(config, repo, issue.number, source, owner, bot_login)
    if not entry.value:
        return Pending(
            external_key=str(issue.number),
            title=issue.title,
            process_key=selection.key,
            reason="not_ready",
            detail=(
                f"Uses the {selection.key} process"
                + ("" if selection.explicit else " (the project default)")
                + ". Waiting for its entry rule."
            ),
            awaiting=failures(entry),
            needs=needs(entry),
        )
    return version, config, owner


async def _lanes(
    session: AsyncSession, project: Project
) -> tuple[dict[str, ProcessVersion], str | None, str | None]:
    """`(available lanes by key, the default lane, why there is no readable default)`.

    A settings bag that will not parse is reported as having no default rather than raised: one
    project's typo must not stop `admit_all` for every other project on the same tick.
    """
    lanes = await latest_process_versions(session, project.id)
    try:
        return lanes, parse_default_process(project.settings), None
    except DefaultProcessInvalid as exc:
        logger.warning("project %s has an unusable default process: %s", project.name, exc)
        return lanes, None, str(exc)


async def _record_refusal(session: AsyncSession, project: Project, pending: Pending) -> None:
    """One `ADMISSION_REFUSED` per `(project, issue, reason)`, for the audit trail.

    Selection failures only. `not_ready` is the ordinary state of every open issue nobody has
    labelled for the factory, and an event for each would be the log's largest and least useful
    family. Suppressed on repeat by looking for a matching row first, so a permanently
    misconfigured issue cannot grow the table by one row per tick.
    """
    if pending.reason == "not_ready":
        return
    seen = (
        await session.execute(
            select(Event.id)
            .where(
                Event.project_id == project.id,
                Event.type == EventType.ADMISSION_REFUSED,
                Event.details["external_key"].astext == pending.external_key,
                Event.details["reason"].astext == pending.reason,
            )
            .limit(1)
        )
    ).first()
    if seen is not None:
        return
    await append(
        session,
        type=EventType.ADMISSION_REFUSED,
        project_id=project.id,
        external_key=pending.external_key,
        reason=pending.reason,
        detail=pending.detail,
    )


async def _entry_result(
    config: ProcessConfig,
    repo: str,
    number: int,
    source: StateSource,
    owner: str,
    bot_login: str,
) -> Evaluation:
    """`entry.when` against the issue as observed — the admission guard, not the driver (§2.3).

    The whole `Evaluation` rather than its verdict, so the diagnostic pass can say WHICH leaves
    are still false. Admitting reads only `.value`; the walk is the same one either way.
    """
    # No ticket row exists yet, so `{ticket_owner}` is read off the issue itself.
    values = {"ticket": str(number), "repo": repo, "ticket_owner": owner}
    ctx = EvalContext(source=source, values=values, bot_login=bot_login)
    return await evaluate(config.entry.when, ctx)


#: The labels the orchestrator writes as its projection of a ticket (`github/projector.py`). An
#: issue opened carrying one would read as already parked, exited or done.
_PROJECTION_LABELS: Final = (
    BLOCKED_LABELS | INTERRUPTED_LABELS | WAITING_LABELS | EXIT_LABELS | DONE_LABELS
)


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """What opening an issue can do toward a lane's `entry.when`: the labels to set, whether the
    condition reads an assignee, and the leaves nothing at creation can make true, as config
    writes them. A prediction: `_decide` on the opened issue gives the verdict."""

    labels: tuple[str, ...]
    needs_assignee: bool
    unmet: tuple[str, ...]


_NOTHING: Final = EntryPlan(labels=(), needs_assignee=False, unmet=())


def entry_plan(when: Condition, values: Mapping[str, str]) -> EntryPlan:
    """Walk `entry.when` for an issue that does not exist yet.

    `values` holds `repo` and `ticket_owner`. `{ticket}` is unknown until GitHub numbers the
    issue, so a label that still names a placeholder is unmet, as is a status or selection label:
    the lane is chosen through `process`, never by a label written here. `any` takes the child
    with the fewest unmet leaves, then the fewest labels, then the first. `not` sets nothing; a
    planned label or the assignee (always set) under a `not` is reported unmet.
    """
    plan = _plan(when, values)
    clashes = tuple(
        f"not {call.source}"
        for call in _negated(when)
        if call.name == "ticket_has_assignee"
        or (call.name == "ticket_label" and expand(call.args[0], values) in plan.labels)
    )
    return EntryPlan(
        labels=plan.labels,
        needs_assignee=plan.needs_assignee,
        unmet=_unique((*plan.unmet, *clashes)),
    )


def _plan(node: Condition, values: Mapping[str, str]) -> EntryPlan:
    if isinstance(node, AllOf):
        parts = [_plan(child, values) for child in node.all_of]
        return EntryPlan(
            labels=_unique(label for part in parts for label in part.labels),
            needs_assignee=any(part.needs_assignee for part in parts),
            unmet=_unique(leaf for part in parts for leaf in part.unmet),
        )
    if isinstance(node, AnyOf):
        # `min` keeps the first of equal keys, which is the tie rule.
        return min(
            (_plan(child, values) for child in node.any_of),
            key=lambda part: (len(part.unmet), len(part.labels)),
        )
    if isinstance(node, Not):
        return _NOTHING
    if node.name == "ticket_has_assignee":
        return EntryPlan(labels=(), needs_assignee=True, unmet=())
    if node.name == "ticket_label":
        label = expand(node.args[0], values)
        if not (placeholders(label) or label in _PROJECTION_LABELS or selected_keys((label,))):
            return EntryPlan(labels=(label,), needs_assignee=False, unmet=())
    return EntryPlan(labels=(), needs_assignee=False, unmet=(node.source,))


def _negated(node: Condition) -> Iterator[PredicateCall]:
    """Every call that sits under a `not`, at any depth."""
    if isinstance(node, Not):
        yield from iter_calls(node.not_)
    elif isinstance(node, AllOf):
        for child in node.all_of:
            yield from _negated(child)
    elif isinstance(node, AnyOf):
        for child in node.any_of:
            yield from _negated(child)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


@dataclass(frozen=True, slots=True)
class Lane:
    """The lane an issue opened now would enter, chosen the way admission chooses it."""

    key: str
    config: ProcessConfig
    #: The caller named the lane, so the issue must carry its selection label.
    explicit: bool


@dataclass(frozen=True, slots=True)
class NoLane:
    detail: str
    #: Every lane the project offers, for the caller to choose from.
    offered: tuple[str, ...]


async def lane_for(
    session: AsyncSession, project: Project, process_key: str | None
) -> Lane | NoLane:
    """`select_process` for an issue that does not exist yet: `process_key` stands in for its
    selection label, and without one the project default applies."""
    lanes, default, default_note = await _lanes(session, project)
    labels = (process_label(process_key),) if process_key is not None else ()
    selection = select_process(labels, available=lanes.keys(), default=default)
    if isinstance(selection, NotSelected):
        detail = (
            default_note
            if selection.reason == "no_default" and default_note
            else selection.detail
        )
        return NoLane(detail=detail, offered=tuple(sorted(lanes)))
    return Lane(
        key=selection.key,
        config=ProcessConfig.model_validate(lanes[selection.key].content),
        explicit=selection.explicit,
    )


async def admit_all(
    scope: SessionScope, deps: ReconcileDeps, candidates: CandidateFactory
) -> Sequence[Ticket]:
    """Run admission for every ACTIVE project with a GitHub ticket source, through each project's
    own GitHub; a project with no connection admits nothing.

    `project.active` is the operator's switch (api/routes/admin.py): a project mid-configuration
    must not start pulling issues in before someone says so. Deactivating stops admission only —
    tickets already in flight keep reconciling, because abandoning one halfway would strand a
    branch and a PR.
    """
    admit = partial(_admit_github, deps=deps, candidates=candidates)
    return await _admit_active(scope, TicketSourceKind.GITHUB, admit)


async def admit_gitlab_all(scope: SessionScope) -> Sequence[Ticket]:
    """`admit_all` for GitLab ticket sources, each through the project's own credentials. A
    project without them admits nothing: the deployment's token must never read a repository a
    tenant named."""
    return await _admit_active(scope, TicketSourceKind.GITLAB, _admit_gitlab)


type _Admit = Callable[[AsyncSession, Project, TicketSource], Awaitable[Sequence[Ticket]]]


def _active(kind: TicketSourceKind) -> Select[tuple[Project, TicketSource]]:
    return (
        select(Project, TicketSource)
        .join(TicketSource, TicketSource.project_id == Project.id)
        .where(Project.active.is_(True), TicketSource.kind == kind)
        .order_by(Project.id)
    )


async def _admit_active(
    scope: SessionScope, kind: TicketSourceKind, admit: _Admit
) -> Sequence[Ticket]:
    async with scope() as session:
        project_ids = [project.id for project, _ in (await session.execute(_active(kind))).all()]
    admitted: list[Ticket] = []
    for project_id in project_ids:
        admitted.extend(await _isolated(scope, kind, project_id, admit))
    return admitted


async def admit_issue(
    scope: SessionScope, deps: ReconcileDeps, project_id: int, number: int
) -> Sequence[int]:
    """Admit the one issue a webhook named, if its lane's entry rule holds now: the ids of the
    tickets admitted, none or one.

    The delivery only names the issue; the verdict comes from reading it through the project's
    own forge client, as the fleet pass's does, so a forged or stale delivery admits nothing
    the pass would not. It runs isolated like one project's pass, and a paused project admits
    nothing here either. The fleet pass still lists every open issue, so a dropped wake waits
    for it.
    """
    async with scope() as session:
        kind = await session.scalar(
            select(TicketSource.kind).where(TicketSource.project_id == project_id)
        )
    admit: _Admit
    if kind == TicketSourceKind.GITHUB:
        admit = partial(_admit_github_issue, deps=deps, number=number)
    elif kind == TicketSourceKind.GITLAB:
        admit = partial(_admit_gitlab_issue, number=number)
    else:
        return ()
    admitted = await _isolated(scope, TicketSourceKind(kind), project_id, admit)
    return [ticket.id for ticket in admitted]


async def _admit_github(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    *,
    deps: ReconcileDeps,
    candidates: CandidateFactory,
) -> Sequence[Ticket]:
    github = await _github_to_admit_through(session, project, ticket_source, deps)
    if github is None:
        return ()
    return await admit_project(
        session,
        project,
        ticket_source,
        candidates(github.client),
        state_source=github.state_source,
        bot_login=github.bot_login,
    )


async def _admit_github_issue(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    *,
    deps: ReconcileDeps,
    number: int,
) -> Sequence[Ticket]:
    github = await _github_to_admit_through(session, project, ticket_source, deps)
    if github is None:
        return ()
    return await _admit_named(
        session, project, ticket_source, number, github.state_source, github.bot_login
    )


async def _github_to_admit_through(
    session: AsyncSession, project: Project, ticket_source: TicketSource, deps: ReconcileDeps
) -> ProjectGitHub | None:
    github = await deps.github_for(session, project)
    if github is None:
        logger.debug("project %s has no GitHub connection; admitting nothing", project.name)
        return None
    if (until := github.client.backoff_until(intake_repo(project, ticket_source))) is not None:
        logger.info("project %s waits for GitHub until %s; admitting nothing", project.name, until)
        return None
    return github


async def _admit_gitlab(
    session: AsyncSession, project: Project, ticket_source: TicketSource
) -> Sequence[Ticket]:
    client = await _gitlab_to_admit_through(session, project)
    if client is None:
        return ()
    return await admit_project(
        session,
        project,
        ticket_source,
        GitLabCandidates(client),
        state_source=partial(GitLabStateSource, client),
        bot_login=client.settings.bot_login,
    )


async def _admit_gitlab_issue(
    session: AsyncSession, project: Project, ticket_source: TicketSource, *, number: int
) -> Sequence[Ticket]:
    client = await _gitlab_to_admit_through(session, project)
    if client is None:
        return ()
    return await _admit_named(
        session,
        project,
        ticket_source,
        number,
        partial(GitLabStateSource, client),
        client.settings.bot_login,
    )


async def _gitlab_to_admit_through(session: AsyncSession, project: Project) -> GitLabClient | None:
    from orchestrator.gitlab.project_client import client_for_project

    client = await client_for_project(session, project)
    if client is None:
        logger.warning(
            "project %s has no GitLab credentials or names a refused host; admitting nothing",
            project.name,
        )
    return client


async def _isolated(
    scope: SessionScope, kind: TicketSourceKind, project_id: int, admit: _Admit
) -> Sequence[Ticket]:
    """One project's admission in its own session and transaction, under a deadline: a 404 repo,
    a bad config, or a hung or broken connection costs that project its pass, never another
    tenant's. A deadline that cancels a query mid-flight can leave a connection unusable, so no
    other project's admission shares it, and each project's commits before the next starts.

    Logged, not an event, like a failed reconcile (worker/loop.py): an infrastructure failure is
    not a state transition, and the next pass retries it.
    """
    name = f"id={project_id}"
    try:
        async with scope() as session, asyncio.timeout(ADMISSION_DEADLINE_SECONDS):
            row = (await session.execute(_active(kind).where(Project.id == project_id))).first()
            if row is None:
                return ()
            project, ticket_source = row
            name = project.name
            return await admit(session, project, ticket_source)
    except Exception as exc:
        if is_rate_limited(exc):
            logger.warning("admission for project %s waits for its forge: %s", name, exc)
        else:
            logger.exception("admission for project %s failed; the others go on", name)
        return ()


async def _admit(
    session: AsyncSession,
    project: Project,
    ticket_source: TicketSource,
    version: ProcessVersion,
    config: ProcessConfig,
    issue: IssueState,
    owner: str,
) -> Ticket | None:
    ticket = Ticket(
        project_id=project.id,
        ticket_source_id=ticket_source.id,
        external_key=str(issue.number),
        # Pinned here and never re-read (design.md §2.8).
        process_version_id=version.id,
        # `{ticket_owner}` — the assignee at admission, stable across later reassignment.
        owner_at_admission=owner,
        title=issue.title,
        current_step=config.entry.step,
        status=TicketStatus.RUNNING,
    )
    try:
        # SAVEPOINT: another runner admitting the same issue is a lost race, not a failed pass.
        async with session.begin_nested():
            session.add(ticket)
            await session.flush()
            await append(
                session,
                type=EventType.TICKET_ADMITTED,
                project_id=project.id,
                ticket_id=ticket.id,
                external_key=ticket.external_key,
                step=config.entry.step,
                # The lane and the commit it was read at: what identifies the config this ticket
                # will run for the rest of its life.
                process=version.process_key,
                process_sha=version.source_commit_sha,
                owner=ticket.owner_at_admission,
            )
            await _enter_step(
                session,
                ticket,
                config,
                step=config.entry.step,
                from_step=None,
                reason="admission",
            )
    except IntegrityError:
        logger.info("issue %s in %s was already admitted", issue.number, project.github_repo)
        return None
    return ticket


async def _known_keys(session: AsyncSession, ticket_source_id: int) -> set[str]:
    rows = (
        await session.execute(
            select(Ticket.external_key).where(Ticket.ticket_source_id == ticket_source_id)
        )
    ).all()
    return {str(row[0]) for row in rows}
