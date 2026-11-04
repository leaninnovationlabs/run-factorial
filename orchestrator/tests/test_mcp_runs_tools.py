"""Run history and cost per ticket, on the operator mount and in the assistant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

import pytest
from fastmcp.client.client import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.assistant.toolbox import registry
from orchestrator.db.models import CostSource, StepKind, StepRun, StepRunStatus
from orchestrator.mcp_server.tools.runs import RunStatus
from tests.api_harness import (
    build_mcp_app,
    mcp_client,
    mcp_settings,
    seed_mcp_token,
)
from tests.reconcile_world import World

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

_NEW_READS = {"factorial_runs_list", "factorial_costs_get"}


def _data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content


def _run(world: World, step: str, *, started: datetime, cost: str, **extra: Any) -> StepRun:
    return StepRun(
        ticket_id=world.ticket_id,
        step_name=step,
        kind=StepKind.AGENT,
        status=StepRunStatus.SUCCEEDED,
        model="claude-opus-5-5",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=Decimal(cost),
        cost_source=CostSource.ESTIMATED,
        started_at=started,
        ended_at=started + timedelta(seconds=90),
        **extra,
    )


async def test_runs_list_gives_durations_and_pages(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    start = datetime.now(UTC) - timedelta(hours=3)
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        runs = [
            _run(world, f"step-{n}", started=start + timedelta(hours=n), cost="0.1000")
            for n in range(3)
        ]
        session.add_all(runs)
        await session.commit()
    ids = sorted((run.id for run in runs), reverse=True)
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        first = _data(
            await client.call_tool(
                "factorial_runs_list", {"ticket": f"id:{world.ticket_id}", "limit": 2}
            )
        )
        rest = _data(
            await client.call_tool(
                "factorial_runs_list",
                {"project": world.repo, "limit": 2, "after_id": first["next_after_id"]},
            )
        )
        failed = _data(
            await client.call_tool(
                "factorial_runs_list", {"project": world.repo, "status": "failed"}
            )
        )

    assert [run["step_run_id"] for run in first["runs"]] == ids[:2]
    assert first["next_after_id"] == ids[1]
    assert [run["step_run_id"] for run in rest["runs"]] == ids[2:]
    assert rest["next_after_id"] is None
    for run in first["runs"] + rest["runs"]:
        assert run["duration_seconds"] == 90.0
        assert run["ticket_id"] == world.ticket_id
        assert run["ref"] == f"{world.repo}#{world.issue_number}"
        assert run["cost_estimated"] is True
        assert run["input_tokens"] == 1000
    assert failed["runs"] == []


async def test_costs_get_respects_since(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    now = datetime.now(UTC)
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        session.add_all(
            [
                _run(world, "plan", started=now - timedelta(days=10), cost="1.0000"),
                _run(world, "build", started=now - timedelta(days=1), cost="0.2500"),
            ]
        )
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        whole = _data(await client.call_tool("factorial_costs_get", {"project": world.repo}))
        recent = _data(
            await client.call_tool("factorial_costs_get", {"project": world.repo, "since_days": 3})
        )

    assert [(t["ticket_id"], t["cost_usd"]) for t in whole["tickets"]] == [(world.ticket_id, 1.25)]
    assert whole["since"] is None
    assert [(t["ticket_id"], t["cost_usd"]) for t in recent["tickets"]] == [(world.ticket_id, 0.25)]
    assert recent["tickets"][0]["tokens"] == 1200
    assert recent["total_cost_usd"] == 0.25


async def test_both_reads_are_on_the_operator_mount_and_in_the_assistant(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        _, operator = await seed_mcp_token(session)
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, operator) as client:
        on_operator = {item.name for item in await client.list_tools()}

    assert on_operator >= _NEW_READS
    assert set(registry()) >= _NEW_READS


async def test_the_status_filter_names_every_run_status() -> None:
    assert set(get_args(RunStatus)) == set(StepRunStatus)


async def test_a_forced_run_carries_who_forced_it_and_is_not_listed_as_succeeded(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Listed as succeeded, a step a person pushed past its failed checks reads as one the
    predicates accepted."""
    start = datetime.now(UTC) - timedelta(hours=2)
    async with maker() as session:
        _, token = await seed_mcp_token(session)
        accepted = _run(world, "plan", started=start, cost="0.1000")
        forced = _run(
            world,
            "build",
            started=start + timedelta(hours=1),
            cost="0.1000",
            forced_by="op@example.com",
        )
        session.add_all([accepted, forced])
        await session.commit()
    app = build_mcp_app(mcp_settings(), maker)

    async with mcp_client(app, token) as client:
        succeeded = _data(
            await client.call_tool(
                "factorial_runs_list", {"ticket": f"id:{world.ticket_id}", "status": "succeeded"}
            )
        )
        every = _data(
            await client.call_tool("factorial_runs_list", {"ticket": f"id:{world.ticket_id}"})
        )

    assert [run["step_run_id"] for run in succeeded["runs"]] == [accepted.id]
    by_id = {run["step_run_id"]: run for run in every["runs"]}
    assert by_id[forced.id]["forced_by"] == "op@example.com"
    assert by_id[accepted.id]["forced_by"] is None
