"""A spawn carries the images its inputs embed — and only the ones its agent may read."""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import StepRunStatus, TicketStatus
from orchestrator.events import EventType
from orchestrator.github.media import MediaTransient
from orchestrator.github.state import CommentState
from tests.reconcile_world import (
    BOT_LOGIN,
    PNG_BYTES,
    FakeAdapter,
    World,
    _build_world,
    _process_with_comment_inputs,
    advance_to_gate,
    event_rows,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "66666666-7777-4888-9999-aaaaaaaaaaaa"


class _NoAttachments(FakeAdapter):
    supports_attachments: ClassVar[bool] = False


async def test_an_image_in_the_ticket_body_reaches_the_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    link = world.media.image(UUID_A)
    world.github.issue = replace(
        world.github.issue, body=f'Extract the text.\n\n<img alt="Image" src="{link}" />'
    )

    await run_tick(world)

    (spec,) = world.adapter.spawns
    (item,) = spec.attachments
    assert (item.name, item.source, item.content) == ("image-1", "ticket_body", PNG_BYTES)
    spawned = next(e for e in await event_rows(world) if e.type == EventType.AGENT_SPAWNED)
    assert spawned.details["attachments"][0]["name"] == "image-1"
    assert "content" not in spawned.details["attachments"][0]


async def test_a_ticket_with_no_images_costs_no_media_call(world: World) -> None:
    await run_tick(world)
    assert world.media.calls == []
    assert world.adapter.spawns[0].attachments == ()


async def test_a_third_partys_image_is_never_requested(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_comment_inputs("proc"), bot_login=BOT_LOGIN
    )
    world.github.add_issue_comment("outsider", f"look ![x]({world.media.image(UUID_A)})")

    await run_tick(world)

    assert world.media.calls == []
    assert world.adapter.spawns[0].attachments == ()


async def test_the_owners_comment_image_is_attributed_to_that_comment(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_comment_inputs("proc"), bot_login=BOT_LOGIN
    )
    world.github.add_issue_comment("operator", f"match this ![x]({world.media.image(UUID_A)})")

    await run_tick(world)

    ((item, _),) = world.media.calls[0]
    assert (item.kind, item.number) == ("issue_comment", world.issue_number)
    assert item.id == world.github.issue_comments[-1].id
    assert world.adapter.spawns[0].attachments[0].source == "ticket_comments"


async def test_an_image_cut_by_the_digest_cap_is_never_requested(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(
        maker, config=_process_with_comment_inputs("proc"), bot_login=BOT_LOGIN
    )
    world.github.add_issue_comment(
        "operator", f"old ![x]({world.media.image(UUID_A)})", "2026-08-19T10:00:00Z"
    )
    world.github.add_issue_comment("operator", "n" * 20_000, "2026-08-19T11:00:00Z")

    await run_tick(world)

    assert world.media.calls == []


async def test_an_unavailable_image_is_named_and_the_run_still_starts(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    link = world.media.unavailable(UUID_A, "not accessible from this repository")
    world.github.issue = replace(world.github.issue, body=f"![x]({link})")

    await run_tick(world)

    (spec,) = world.adapter.spawns
    assert spec.attachments == ()
    assert spec.missing_attachments[0].reason == "not accessible from this repository"


async def test_a_transient_media_failure_defers_the_spawn_to_the_next_tick(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN)
    world.github.issue = replace(world.github.issue, body=f"![x]({world.media.image(UUID_A)})")
    world.media.error = MediaTransient(
        "https://private-user-images.githubusercontent.com/1/x.png answered 503"
    )

    with pytest.raises(MediaTransient):
        await run_tick(world)
    assert await step_runs(world) == []
    assert world.minted == []

    world.media.error = None
    await run_tick(world)
    assert [a.name for a in world.adapter.spawns[0].attachments] == ["image-1"]


async def test_an_adapter_that_cannot_take_images_is_refused_before_the_mint(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _build_world(maker, bot_login=BOT_LOGIN, adapter_cls=_NoAttachments)
    world.github.issue = replace(world.github.issue, body=f"![x]({world.media.image(UUID_A)})")

    await run_tick(world)

    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.FAILED
    assert run.failure_reason is not None
    assert "can't pass images to the agent" in run.failure_reason
    assert world.minted == []
    assert (await ticket_row(world)).status == TicketStatus.BLOCKED


async def test_an_approvers_review_image_reaches_the_rework_attempt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Review Focus 2: an image in a trusted review is attributed to the review, not dropped."""
    world = await _build_world(
        maker,
        config=_process_with_comment_inputs(
            "proc", plan_inputs=("ticket_body",), gate_reject_inputs=("pr_review_comments",)
        ),
        bot_login=BOT_LOGIN,
    )
    await advance_to_gate(world)
    link = world.media.image(UUID_B)
    world.github.request_changes(
        "reviewer-1",
        body="closer to this please",
        comments=(
            CommentState(
                author="reviewer-1",
                body=f"like ![x]({link})",
                created_at="2026-08-19T12:00:00Z",
                path="spec.md",
                line=3,
                id=5001,
            ),
        ),
    )

    await run_tick(world)  # spec_gate rejects (reviewer-1's changes-requested review)

    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns, carrying the gate's declared inputs

    rework = world.adapter.spawns[1]
    (item,) = rework.attachments
    assert item.source == "pr_review_comments"
    ((wanted_item, _),) = world.media.calls[-1]
    assert (wanted_item.kind, wanted_item.id) == ("review_comment", 5001)
    assert wanted_item.number == world.github.pulls[0].number
