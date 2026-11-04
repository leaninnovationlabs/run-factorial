"""Which lane an issue enters, and what happens when that question has no good answer.

Selection runs BEFORE any predicate: the label or the project default picks a lane, and only
then is that lane's `entry.when` evaluated. The two halves are tested apart and together —
`select_process` on literals, and the admitting pass on a real project with two lanes, because
the interesting failures (a typoed label silently defaulting, a second lane's readiness being
consulted) only exist once both halves run.

Real Postgres for the pass, like `test_reconcile.py`: what is under test includes which
`process_version` row a ticket ends up pinned to.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import Condition, ProcessConfig
from orchestrator.db.models import Event, ProcessVersion, Project, Ticket, TicketSource
from orchestrator.factorial import DEFAULT_KEY
from orchestrator.github import IssueState, desired_status_labels
from orchestrator.worker.admission import (
    EntryPlan,
    InMemoryCandidates,
    admit_project,
    entry_plan,
    pending_issue,
    pending_issues,
)
from orchestrator.worker.selection import (
    NotSelected,
    Selected,
    process_label,
    select_process,
    selected_keys,
)

from .reconcile_world import World, _build_world, _process

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# --- the policy on its own ----------------------------------------------------------------------


def test_no_label_takes_the_project_default() -> None:
    outcome = select_process({"adlc:new"}, available={"standard", "hotfix"}, default="standard")
    assert outcome == Selected(key="standard", explicit=False)


def test_a_process_label_overrides_the_default() -> None:
    outcome = select_process(
        {"adlc:new", process_label("hotfix")}, available={"standard", "hotfix"}, default="standard"
    )
    assert outcome == Selected(key="hotfix", explicit=True)


def test_no_default_and_no_label_is_refused_rather_than_guessed() -> None:
    outcome = select_process({"adlc:new"}, available={"standard"}, default=None)
    assert isinstance(outcome, NotSelected)
    assert outcome.reason == "no_default"
    assert "standard" in outcome.detail


def test_an_unknown_label_never_falls_back_to_the_default() -> None:
    """The two failure modes are not symmetrical: an operator who typos `hotfxi` and silently
    gets the default has their hotfix run the standard lane, and finds out days later, at the
    gate."""
    outcome = select_process(
        {"adlc:new", process_label("hotfxi")}, available={"standard", "hotfix"}, default="standard"
    )
    assert isinstance(outcome, NotSelected)
    assert outcome.reason == "unknown_process"
    assert "hotfxi" in outcome.detail


def test_a_retired_lane_is_not_selectable() -> None:
    """Availability is what the last sync found, not what the project has rows for."""
    outcome = select_process(
        {process_label("spike")}, available={"standard"}, default="standard"
    )
    assert isinstance(outcome, NotSelected)
    assert outcome.reason == "unknown_process"


def test_two_process_labels_are_refused() -> None:
    outcome = select_process(
        {process_label("hotfix"), process_label("docs")},
        available={"hotfix", "docs"},
        default="hotfix",
    )
    assert isinstance(outcome, NotSelected)
    assert outcome.reason == "ambiguous_process"
    assert "hotfix" in outcome.detail and "docs" in outcome.detail


def test_a_default_the_repo_no_longer_offers_is_refused() -> None:
    outcome = select_process({"adlc:new"}, available={"hotfix"}, default="standard")
    assert isinstance(outcome, NotSelected)
    assert outcome.reason == "unknown_process"


def test_both_label_vocabularies_are_read_and_one_is_written() -> None:
    """A repo onboarded before issue #138 still spells the namespace `adlc:`, and a gesture the
    loop refuses to read is a gesture an operator cannot make."""
    assert process_label("hotfix") == "factorial:process:hotfix"
    assert selected_keys({"adlc:process:docs", "factorial:process:hotfix"}) == ("docs", "hotfix")
    assert selected_keys({"factorial:new", "factorial:blocked"}) == ()


def test_the_selection_label_is_spent_input_and_gets_swept() -> None:
    """No projector change is needed for it: by the time projection runs, selection has already
    happened and been pinned, so the label is spent — exactly like `factorial:new`, which the
    namespace sweep already removes on the first tick after admission."""
    kept = desired_status_labels(
        {"bug", "factorial:new", process_label("hotfix"), "factorial:plan"}, step="build"
    )
    assert kept == frozenset({"bug", "factorial:build"})


def test_entry_plan_sets_only_what_opening_an_issue_may_set() -> None:
    """`factorial_ticket_create` opens the issue with exactly these labels. A status or
    selection label set here would be the tool writing the orchestrator's own projection; a
    leaf silently dropped would leave the issue waiting for ever with no reason given. The
    entry label every wizard lane uses, `factorial:new`, must stay settable."""
    when = TypeAdapter(Condition).validate_python(
        {
            "all": [
                'ticket_label("factorial:new")',
                "ticket_has_assignee()",
                {
                    "any": [
                        'branch_exists("spec/{ticket}-*")',
                        'ticket_label("team:{ticket_owner}")',
                    ]
                },
                'ticket_label("factorial:blocked")',
                f'ticket_label("{process_label("hotfix")}")',
                {"not": 'ticket_label("factorial:new")'},
            ]
        }
    )

    plan = entry_plan(when, {"repo": "acme/app", "ticket_owner": "operator"})

    assert plan == EntryPlan(
        labels=("factorial:new", "team:operator"),
        needs_assignee=True,
        unmet=(
            'ticket_label("factorial:blocked")',
            'ticket_label("factorial:process:hotfix")',
            'not ticket_label("factorial:new")',
        ),
    )


# --- the pass, on a project with two lanes ------------------------------------------------------


async def _add_lane(world: World, key: str, *, entry_label: str = "adlc:new") -> int:
    """A second `process_version` row for the world's project, admitting on `entry_label`.

    Its entry condition is deliberately the same shape as the first lane's, which is what the
    wizard emits for every lane it generates: under selection-by-label that is valid, because
    only the SELECTED lane's condition is ever evaluated.
    """
    raw = _process(key).model_dump(mode="json", by_alias=True)
    raw["entry"]["when"] = {"all": [f'ticket_label("{entry_label}")', "ticket_has_assignee()"]}
    config = ProcessConfig.model_validate(raw)
    async with world.sessions() as session:
        row = ProcessVersion(
            project_id=world.project_id,
            process_key=key,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha=f"{key}{'0' * (40 - len(key))}",
        )
        session.add(row)
        await session.flush()
        return row.id


async def _set_default(world: World, key: str | None) -> None:
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        assert project is not None
        settings = dict(project.settings)
        if key is None:
            settings.pop(DEFAULT_KEY, None)
        else:
            settings[DEFAULT_KEY] = key
        project.settings = settings


async def _pass(world: World, *listed: IssueState) -> list[Ticket]:
    candidates = InMemoryCandidates({world.repo: listed or (world.github.issue,)})
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        return list(await admit_project(session, project, source, candidates, **world.observed_by))


async def _pending(world: World) -> list[Any]:
    candidates = InMemoryCandidates({world.repo: (world.github.issue,)})
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        return list(
            await pending_issues(
                session,
                project,
                source,
                candidates,
                world.project_github.state_source,
                world.project_github.bot_login,
            )
        )


async def _pending_one(world: World) -> Any:
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        source = await session.get(TicketSource, world.source_id)  # type: ignore[attr-defined]
        assert project is not None and source is not None
        return await pending_issue(
            session,
            project,
            source,
            world.github.issue.number,
            world.project_github.state_source,
            world.project_github.bot_login,
        )


async def _refusals(world: World) -> list[Event]:
    async with world.sessions() as session:
        return list(
            (
                await session.execute(
                    select(Event).where(
                        Event.project_id == world.project_id,
                        Event.type == "admission_refused",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _default_key(world: World) -> str:
    async with world.sessions() as session:
        project = await session.get(Project, world.project_id)
        assert project is not None
        return str(project.settings[DEFAULT_KEY])


async def test_an_unlabelled_issue_pins_the_default_lane(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, admit=False)
    hotfix_id = await _add_lane(world, "hotfix")

    tickets = await _pass(world)

    assert len(tickets) == 1
    assert tickets[0].process_version_id != hotfix_id
    async with world.sessions() as session:
        version = await session.get(ProcessVersion, tickets[0].process_version_id)
        assert version is not None
        assert version.process_key == await _default_key(world)


async def test_a_process_label_pins_that_lane_instead(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, admit=False, labels=frozenset({"adlc:new", process_label("hotfix")})
    )
    hotfix_id = await _add_lane(world, "hotfix")

    tickets = await _pass(world)

    assert [t.process_version_id for t in tickets] == [hotfix_id]
    async with world.sessions() as session:
        admitted = (
            await session.execute(
                select(Event).where(
                    Event.project_id == world.project_id, Event.type == "ticket_admitted"
                )
            )
        ).scalar_one()
    # The lane and the commit its config was read at: what identifies the process this ticket
    # will run for the rest of its life.
    assert admitted.details["process"] == "hotfix"
    assert admitted.details["process_sha"].startswith("hotfix")


async def test_a_process_label_alone_is_not_an_admission_signal(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Selection says which lane; `entry.when` still says whether to start."""
    world = await _build_world(maker, admit=False, labels=frozenset({process_label("hotfix")}))
    await _add_lane(world, "hotfix")

    assert await _pass(world) == []
    pending = await _pending(world)
    assert [(p.reason, p.process_key) for p in pending] == [("not_ready", "hotfix")]
    # Waiting for readiness is the ordinary state of an unlabelled issue, so it appends nothing.
    assert await _refusals(world) == []


async def test_admission_reads_an_issue_only_when_its_listing_would_admit_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A GET per open issue every pass cost a repo's whole backlog in calls. The listing judges
    first, and the issue's own read has the last word, because a listing can lag."""
    world = await _build_world(maker, admit=False, labels=frozenset({process_label("hotfix")}))
    await _add_lane(world, "hotfix")

    assert await _pass(world) == []
    assert world.github.issue_reads == 0

    lagging = replace(world.github.issue, labels=world.github.issue.labels | {"adlc:new"})
    assert await _pass(world, lagging) == []
    assert world.github.issue_reads == 1

    world.github.issue = replace(lagging, assignees=("operator",))
    (ticket,) = await _pass(world, lagging)
    assert ticket.owner_at_admission == "operator"


async def test_readiness_is_evaluated_for_the_selected_lane_only(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A false entry condition means the issue waits; it never tries another lane."""
    world = await _build_world(
        maker, admit=False, labels=frozenset({"adlc:new", process_label("hotfix")})
    )
    await _add_lane(world, "hotfix", entry_label="adlc:urgent")

    assert await _pass(world) == []
    assert [(p.reason, p.process_key) for p in await _pending(world)] == [("not_ready", "hotfix")]


async def test_a_waiting_issue_names_the_conditions_it_is_still_waiting_on(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`not_ready` is the answer to "why is my issue not a ticket", and the dashboard column
    that asks it needs the leaves, not the verdict: "its entry condition is not satisfied" sends
    an operator to read the config, the failing leaf tells them what to do to the issue."""
    world = await _build_world(maker, admit=False, labels=frozenset({process_label("hotfix")}))
    await _add_lane(world, "hotfix")

    (pending,) = await _pending(world)
    assert pending.reason == "not_ready"
    assert pending.awaiting == ('ticket_label("adlc:new") = false',)
    assert pending.needs == ("the adlc:new label on the issue",)
    assert pending.detail == "Uses the hotfix process. Waiting for its entry rule."


async def test_a_selection_refusal_waits_on_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """No lane was resolved, so no `entry.when` was ever evaluated — an empty list rather than a
    misleading one."""
    world = await _build_world(maker, admit=False)
    await _set_default(world, None)

    (pending,) = await _pending(world)
    assert pending.reason == "no_default"
    assert pending.awaiting == ()


async def test_an_unknown_process_label_refuses_and_records_it_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, admit=False, labels=frozenset({"adlc:new", process_label("hotfxi")})
    )
    await _add_lane(world, "hotfix")

    assert await _pass(world) == []
    assert [p.reason for p in await _pending(world)] == ["unknown_process"]

    events = await _refusals(world)
    assert [e.details["reason"] for e in events] == ["unknown_process"]
    assert events[0].ticket_id is None  # there is no ticket, which is the finding
    assert events[0].details["external_key"] == str(world.issue_number)

    # A refusal that stays true is still true next tick, and one row per tick would be the log's
    # largest family.
    await _pass(world)
    assert len(await _refusals(world)) == 1


async def test_two_process_labels_refuse_rather_than_pick_one(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker,
        admit=False,
        labels=frozenset({"adlc:new", process_label("hotfix"), process_label("docs")}),
    )
    await _add_lane(world, "hotfix")
    await _add_lane(world, "docs")

    assert await _pass(world) == []
    assert [p.reason for p in await _pending(world)] == ["ambiguous_process"]


async def test_no_default_configured_refuses_and_explains(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Allowed, and it means every issue must name its lane."""
    world = await _build_world(maker, admit=False)
    await _set_default(world, None)

    assert await _pass(world) == []
    (pending,) = await _pending(world)
    assert pending.reason == "no_default"
    assert pending.process_key is None
    assert [e.details["reason"] for e in await _refusals(world)] == ["no_default"]


async def test_a_default_changed_after_admission_leaves_the_pin_alone(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A ticket's process is locked in at admission and never changes mid-cycle (design.md §2.8),
    which is now load-bearing for lane identity too."""
    world = await _build_world(maker, admit=False)
    await _add_lane(world, "hotfix")
    (ticket,) = await _pass(world)
    pinned = ticket.process_version_id

    await _set_default(world, "hotfix")
    assert await _pass(world) == []  # already admitted

    async with world.sessions() as session:
        row = (
            await session.execute(select(Ticket).where(Ticket.id == ticket.id))
        ).scalar_one()
    assert row.process_version_id == pinned


async def test_a_label_added_after_admission_does_not_move_the_pin(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, admit=False)
    await _add_lane(world, "hotfix")
    (ticket,) = await _pass(world)
    pinned = ticket.process_version_id

    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels | {process_label("hotfix")}
    )
    assert await _pass(world) == []

    async with world.sessions() as session:
        row = (
            await session.execute(select(Ticket).where(Ticket.id == ticket.id))
        ).scalar_one()
    assert row.process_version_id == pinned


async def test_a_retired_lane_is_not_offered_and_does_not_disturb_its_tickets(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """In-flight tickets hold `process_version_id` and read the row by id, so retiring a lane
    cannot touch work already in it (design.md §4)."""
    world = await _build_world(
        maker, admit=False, labels=frozenset({"adlc:new", process_label("hotfix")})
    )
    hotfix_id = await _add_lane(world, "hotfix")
    (ticket,) = await _pass(world)
    assert ticket.process_version_id == hotfix_id

    async with world.sessions() as session:
        row = await session.get(ProcessVersion, hotfix_id)
        assert row is not None
        row.retired_at = row.created_at

    async with world.sessions() as session:
        still = (
            await session.execute(select(Ticket).where(Ticket.id == ticket.id))
        ).scalar_one()
    assert still.process_version_id == hotfix_id

    # A second issue naming the retired lane is refused rather than defaulted.
    world.github.issue = replace(world.github.issue, number=99)
    assert await _pass(world) == []
    assert [p.reason for p in await _pending(world)] == ["unknown_process"]


async def test_one_issue_gets_the_verdict_the_whole_repo_pass_gives_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`factorial_ticket_get` asks about one issue. A reason the worker's own pass would not give
    sends the operator to fix the wrong thing, and None for an issue that still waits reads as
    "admitted on the next pass" for ever."""
    world = await _build_world(maker, admit=False, labels=frozenset({process_label("hotfix")}))
    await _add_lane(world, "hotfix")

    (listed,) = await _pending(world)
    assert await _pending_one(world) == listed

    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels | {"adlc:new"}
    )
    assert await _pending_one(world) is None
