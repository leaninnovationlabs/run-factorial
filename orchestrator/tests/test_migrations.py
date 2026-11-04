"""Revision 0001 applies both ways, and the models agree with it.

The autogenerate check is the guard that keeps `orchestrator.db.models` and the migration from
drifting once the models become the source of truth (tech-stack.md decision 2).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from orchestrator.db import Base
from orchestrator.secrets.crypto import EncryptedValue, decrypt, encrypt

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

#: The organization revision 0027 seeds, for a raw insert at head.
DEFAULT_ORG = "(SELECT id FROM organization WHERE slug = 'default')"
#: The assistant's credential slot for one backend and auth mode.
SLOT = "assistant:claude_sdk:api_key"

EXPECTED_TABLES = {
    "agent",
    "app_user",
    "artifact",
    "event",
    "mcp_token",
    "organization",
    "organization_credential",
    "organization_invite",
    "organization_member",
    "organization_removal",
    "organization_setting",
    "permission",
    "process_version",
    "project",
    "role",
    "role_permission",
    "runtime",
    "runtime_credential",
    "secret",
    "step_run",
    "ticket",
    "ticket_source",
}


def _table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "target_metadata": Base.metadata,
        },
    )
    # No filtering: alembic already ignores its own version table, and it never inspects
    # functions or triggers, so `set_updated_at` cannot show up here as noise.
    return list(compare_metadata(context, Base.metadata))


async def test_upgrade_and_downgrade_round_trip(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    alembic_config, engine = migration_database
    async with engine.connect() as connection:
        assert await connection.run_sync(_table_names) >= EXPECTED_TABLES

    # env.py owns its own event loop (asyncio.run), so alembic must not run on ours.
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    async with engine.connect() as connection:
        assert await connection.run_sync(_table_names) & EXPECTED_TABLES == set()

    # Restore head: the other downgrade tests share this database.
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    async with engine.connect() as connection:
        assert await connection.run_sync(_table_names) >= EXPECTED_TABLES


def _ticket_columns(connection: Connection) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns("ticket")}


async def test_an_upgrade_gives_way_to_a_held_lock_and_lands_once_it_is_released(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """0019's first deploy failed: the old worker still held locks on `ticket` when the deploy
    migrated, and the ALTER's lock timeout ran out. Waiting without a limit is no better: every
    read of the table queues behind a waiting ALTER. So a held lock must cost short, retried
    attempts — readers stall for one attempt at most, and the run lands once the lock goes."""
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0018")
    alembic_config.set_main_option("factorial.lock_timeout", "300ms")
    alembic_config.set_main_option("factorial.lock_pause_seconds", "0.1")
    clock = asyncio.get_running_loop()
    try:
        async with engine.connect() as holder:
            try:
                # A pass mid-transaction: any lock on the table blocks the ALTER's ACCESS EXCLUSIVE.
                await holder.execute(text("LOCK TABLE ticket IN ACCESS SHARE MODE"))
                upgrade = asyncio.create_task(
                    asyncio.to_thread(command.upgrade, alembic_config, "head")
                )
                await asyncio.sleep(1.0)
                async with engine.connect() as reader:
                    started = clock.time()
                    # Bounded: with no lock timeout the reader would queue for as long as the
                    # holder holds, and the holder lets go only after this read returns.
                    try:
                        await asyncio.wait_for(
                            reader.execute(text("SELECT count(*) FROM ticket")), timeout=5
                        )
                    except TimeoutError:
                        pytest.fail("a reader was still queued behind the ALTER after 5s")
                    stalled = clock.time() - started
                await asyncio.sleep(2.0)
                if upgrade.done():
                    upgrade.result()  # surfaces the upgrade's own error, if it raised
                    pytest.fail("the upgrade finished while the lock was still held")
            finally:
                await holder.rollback()
        await asyncio.wait_for(upgrade, timeout=30)

        assert stalled < 1.0, f"a reader waited {stalled:.1f}s behind the queued ALTER"

        async with engine.connect() as connection:
            assert "last_leased_at" in await connection.run_sync(_ticket_columns)
    finally:
        alembic_config.remove_main_option("factorial.lock_timeout")
        alembic_config.remove_main_option("factorial.lock_pause_seconds")
        # Restore head: the other downgrade tests share this database.
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_models_match_migration(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        diff = await connection.run_sync(_diff)
    assert diff == [], f"models and revision 0001 have drifted: {diff}"


async def test_step_run_cost_source_is_a_checked_vocabulary(engine: AsyncEngine) -> None:
    """Revision 0015: `cost_source` is TEXT + CHECK like every other enumeration here, and
    alembic does not diff CHECK bodies, so the body is asserted directly."""
    async with engine.connect() as connection:
        body = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_step_run_cost_source'"
                )
            )
        ).scalar_one()
    assert "'reported'" in body and "'estimated'" in body, body


async def test_step_run_entered_by_is_a_checked_vocabulary(engine: AsyncEngine) -> None:
    """Revision 0023: `entered_by` is TEXT + CHECK like `cost_source`, and the CHECK body is not
    diffed by alembic, so it is asserted directly. A value the dashboard never reads, such as a
    misspelt `reject_rerun`, would otherwise file every rework under "first run"."""
    async with engine.connect() as connection:
        body = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_step_run_entered_by'"
                )
            )
        ).scalar_one()
    assert all(
        f"'{word}'" in body
        for word in ("next", "reject_rerun", "admission", "retry", "restart", "redo")
    ), body


async def test_generation_columns_default_to_the_first(engine: AsyncEngine) -> None:
    """Revision 0026: every ticket and run from before a restart existed is generation 1, and a
    row inserted without one is too — the loop filters on the column, so NULL would hide a run."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT table_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE column_name = 'generation' AND table_name IN ('ticket', 'step_run') "
                    "ORDER BY table_name"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [("step_run", "NO", "1"), ("ticket", "NO", "1")]


async def test_the_runtime_kind_checks_accept_claude(engine: AsyncEngine) -> None:
    """Revision 0012 widens both `runtime_kind` CHECKs to `claude`, the Claude family's own
    name — `claude_managed` stays accepted for ever (decision 32) but is the deferred adapter's
    name, not what a manifest should have to write. Proven by an INSERT on both tables."""
    async with engine.connect() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (organization_id, name, github_repo) "
                    f"VALUES ({DEFAULT_ORG}, 'claude-check', 'acme/claude-check') RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO agent (project_id, name, repo_path, runtime_kind) "
                f"VALUES ({project_id}, 'planner', '.factorial/agents/planner', 'claude')"
            )
        )
        for table in ("agent", "step_run"):
            body = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        f"WHERE conname = 'ck_{table}_runtime_kind'"
                    )
                )
            ).scalar_one()
            assert "'claude'" in body, f"{table}: {body}"
        await connection.rollback()


async def test_the_runtime_kind_checks_accept_codex(engine: AsyncEngine) -> None:
    """Revision 0010 widens both `runtime_kind` CHECKs to `codex` (tech-stack decision 34,
    stage 2). Proven by an INSERT, not by the autogenerate diff above — alembic does not
    compare CHECK constraint bodies, so this is the only guard that catches a migration that
    forgot one of the two tables."""
    async with engine.connect() as connection:
        # A real row through the agent CHECK, rolled back — the suite shares this database.
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (organization_id, name, github_repo) "
                    f"VALUES ({DEFAULT_ORG}, 'codex-check', 'acme/codex-check') RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO agent (project_id, name, repo_path, runtime_kind) "
                f"VALUES ({project_id}, 'builder', '.adlc/agents/builder', 'codex')"
            )
        )
        # step_run's row needs a ticket graph; its constraint BODY is asserted instead.
        definition = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_step_run_runtime_kind'"
                )
            )
        ).scalar_one()
        assert "codex" in definition
        await connection.rollback()


async def test_the_runtime_type_check_is_the_enum(engine: AsyncEngine) -> None:
    """Revision 0011's `ck_runtime_type` — proven by INSERTs, since alembic never compares
    CHECK bodies: every `RUNTIME_TYPES` value goes in, anything else is refused."""
    from sqlalchemy.exc import IntegrityError

    from orchestrator.db.models import RUNTIME_TYPES

    async with engine.connect() as connection:
        for index, value in enumerate(RUNTIME_TYPES):
            await connection.execute(
                text(
                    "INSERT INTO runtime (organization_id, slug, name, type) "
                    f"VALUES ({DEFAULT_ORG}, 'ck-{index}', 'ck', '{value}')"
                )
            )
        with pytest.raises(IntegrityError, match="ck_runtime_type"):
            await connection.execute(
                text(
                    "INSERT INTO runtime (organization_id, slug, name, type) "
                    f"VALUES ({DEFAULT_ORG}, 'ck-x', 'ck', 'gemini_api')"
                )
            )
        await connection.rollback()


async def test_a_lane_may_be_snapshotted_at_many_commits_but_each_only_once(
    engine: AsyncEngine,
) -> None:
    """Revision 0017 moves uniqueness to `(project_id, process_key, source_commit_sha)`.

    Proven by INSERTs rather than by the autogenerate diff: the diff would catch a dropped
    constraint, but not that the new one admits exactly what the feature needs — one lane at many
    commits, and two lanes at one commit, which is what a single sync writes — while still
    refusing the same lane twice at the same commit.
    """
    from sqlalchemy.exc import IntegrityError

    async with engine.connect() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (organization_id, name, github_repo) "
                    f"VALUES ({DEFAULT_ORG}, 'lanes', 'acme/lanes') RETURNING id"
                )
            )
        ).scalar_one()
        insert = (
            "INSERT INTO process_version (project_id, process_key, source_commit_sha, content) "
            "VALUES (:project, :key, :sha, '{}'::jsonb)"
        )
        a, b = "a" * 40, "b" * 40
        # One sync writes every lane at one commit.
        await connection.execute(text(insert), {"project": project_id, "key": "standard", "sha": a})
        await connection.execute(text(insert), {"project": project_id, "key": "hotfix", "sha": a})
        # A later sync writes the same lane at another.
        await connection.execute(text(insert), {"project": project_id, "key": "standard", "sha": b})
        with pytest.raises(IntegrityError, match="uq_process_version_project_id_process_key"):
            await connection.execute(
                text(insert), {"project": project_id, "key": "standard", "sha": a}
            )
        await connection.rollback()


async def test_the_upgrade_refuses_a_snapshot_with_no_commit(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """A row written by the removed JSON-upload route has no sha to be identified by, and there
    is no honest one to invent — so revision 0017 stops rather than guessing.

    The row is committed so the migration sees it, and removed afterwards however the assertion
    goes, because the other downgrade tests share this database.
    """
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0016")
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (name, github_repo) "
                    "VALUES ('uploaded', 'acme/uploaded') RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO process_version (project_id, process_key, version, content) "
                "VALUES (:project, 'standard', 1, '{}'::jsonb)"
            ),
            {"project": project_id},
        )
    try:
        with pytest.raises(RuntimeError, match="carry no source_commit_sha"):
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_downgrade_renumbers_from_registration_order(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """A NOT NULL unique `version` has to hold something, and the numbers the files carried are
    gone. Registration order is the ordering `version DESC` was read for, so the shape comes
    back even though the values cannot — and no pin re-points, because a ticket pins the row id.
    """
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (organization_id, name, github_repo) "
                    f"VALUES ({DEFAULT_ORG}, 'renumber', 'acme/renumber') RETURNING id"
                )
            )
        ).scalar_one()
        for sha in ("c" * 40, "d" * 40, "e" * 40):
            await connection.execute(
                text(
                    "INSERT INTO process_version "
                    "(project_id, process_key, source_commit_sha, content) "
                    "VALUES (:project, 'standard', :sha, '{}'::jsonb)"
                ),
                {"project": project_id, "sha": sha},
            )
    try:
        await asyncio.to_thread(command.downgrade, alembic_config, "0016")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT source_commit_sha, version FROM process_version "
                        "WHERE project_id = :project ORDER BY version"
                    ),
                    {"project": project_id},
                )
            ).all()
        assert [row[1] for row in rows] == [1, 2, 3]
        assert [row[0][0] for row in rows] == ["c", "d", "e"]
    finally:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})


async def _seed_ticket(
    connection: AsyncConnection, name: str, external_key: str
) -> tuple[int, int]:
    """A project `name` on `acme/<name>` and its ticket `external_key` on step `build`, in the
    default organization: `(project id, ticket id)`."""
    project_id = (
        await connection.execute(
            text(
                "INSERT INTO project (organization_id, name, github_repo) "
                f"VALUES ({DEFAULT_ORG}, :name, :repo) RETURNING id"
            ),
            {"name": name, "repo": f"acme/{name}"},
        )
    ).scalar_one()
    source_id = (
        await connection.execute(
            text(
                "INSERT INTO ticket_source (project_id, kind, external_ref) "
                "VALUES (:project, 'github', :repo) RETURNING id"
            ),
            {"project": project_id, "repo": f"acme/{name}"},
        )
    ).scalar_one()
    version_id = (
        await connection.execute(
            text(
                "INSERT INTO process_version "
                "(project_id, process_key, source_commit_sha, content) "
                "VALUES (:project, 'standard', :sha, '{}'::jsonb) RETURNING id"
            ),
            {"project": project_id, "sha": "f" * 40},
        )
    ).scalar_one()
    ticket_id = (
        await connection.execute(
            text(
                "INSERT INTO ticket (project_id, ticket_source_id, external_key, "
                "process_version_id, owner_at_admission, current_step, status) "
                "VALUES (:project, :source, :key, :version, 'operator', 'build', 'running') "
                "RETURNING id"
            ),
            {
                "project": project_id,
                "source": source_id,
                "key": external_key,
                "version": version_id,
            },
        )
    ).scalar_one()
    return project_id, ticket_id


async def test_the_0020_downgrade_refuses_while_a_run_is_idle(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """An `idle` run is a live session at its prompt, and revision 0019's vocabulary has no word
    for it, so the downgrade refuses rather than guessing. The INSERT is also the proof that the
    CHECK admits `idle`, which the autogenerate diff cannot see."""
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (organization_id, name, github_repo) "
                    f"VALUES ({DEFAULT_ORG}, 'idle', 'acme/idle') RETURNING id"
                )
            )
        ).scalar_one()
        source_id = (
            await connection.execute(
                text(
                    "INSERT INTO ticket_source (project_id, kind, external_ref) "
                    "VALUES (:project, 'github', 'acme/idle') RETURNING id"
                ),
                {"project": project_id},
            )
        ).scalar_one()
        version_id = (
            await connection.execute(
                text(
                    "INSERT INTO process_version "
                    "(project_id, process_key, source_commit_sha, content) "
                    "VALUES (:project, 'standard', :sha, '{}'::jsonb) RETURNING id"
                ),
                {"project": project_id, "sha": "f" * 40},
            )
        ).scalar_one()
        ticket_id = (
            await connection.execute(
                text(
                    "INSERT INTO ticket (project_id, ticket_source_id, external_key, "
                    "process_version_id, owner_at_admission, current_step, status) "
                    "VALUES (:project, :source, '1', :version, 'operator', 'build', 'running') "
                    "RETURNING id"
                ),
                {"project": project_id, "source": source_id, "version": version_id},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO step_run (ticket_id, step_name, kind, status) "
                "VALUES (:ticket, 'build', 'agent', 'idle')"
            ),
            {"ticket": ticket_id},
        )
    try:
        with pytest.raises(RuntimeError, match="'idle'"):
            await asyncio.to_thread(command.downgrade, alembic_config, "0019")
        async with engine.connect() as connection:
            body = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_step_run_status'"
                    )
                )
            ).scalar_one()
        assert "'idle'" in body, body
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0023_backfill_copies_the_entry_and_guesses_nothing_else(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """The rework figure's history, classified once: a rework attempt reads `reject_rerun` from
    the `step_entered` that predicted it, an attempt above 1 that no entry names reads `retry`
    once its ticket writes entries, and a first attempt, or any attempt from before its ticket's
    first entry, stays NULL. A wrong JSONB cast in the match would misclassify every row the
    project has, and an early gate rejection read as a retry would hide rework, silently, at
    deploy.

    Rows are committed so the migration sees them, and removed afterwards however the assertion
    goes, because the other downgrade tests share this database.
    """
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0022")
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (name, github_repo) "
                    "VALUES ('backfill', 'acme/backfill') RETURNING id"
                )
            )
        ).scalar_one()
        source_id = (
            await connection.execute(
                text(
                    "INSERT INTO ticket_source (project_id, kind, external_ref) "
                    "VALUES (:project, 'github', 'acme/backfill') RETURNING id"
                ),
                {"project": project_id},
            )
        ).scalar_one()
        version_id = (
            await connection.execute(
                text(
                    "INSERT INTO process_version "
                    "(project_id, process_key, source_commit_sha, content) "
                    "VALUES (:project, 'standard', :sha, '{}'::jsonb) RETURNING id"
                ),
                {"project": project_id, "sha": "e" * 40},
            )
        ).scalar_one()
        ticket_ids: list[int] = []
        for key in ("7", "8"):
            ticket_ids.append(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO ticket (project_id, ticket_source_id, external_key, "
                            "process_version_id, owner_at_admission, current_step, status) "
                            "VALUES (:project, :source, :key, :version, 'operator', 'build', "
                            "'running') RETURNING id"
                        ),
                        {
                            "project": project_id,
                            "source": source_id,
                            "key": key,
                            "version": version_id,
                        },
                    )
                ).scalar_one()
            )
        ticket_id, pre_entry_id = ticket_ids
        runs = [
            (ticket_id, 1, datetime(2026, 9, 2, 9, 0, tzinfo=UTC)),
            (ticket_id, 2, datetime(2026, 9, 2, 10, 5, tzinfo=UTC)),
            (ticket_id, 3, datetime(2026, 9, 2, 10, 10, tzinfo=UTC)),
            (pre_entry_id, 1, datetime(2026, 8, 20, 9, 0, tzinfo=UTC)),
            (pre_entry_id, 2, datetime(2026, 8, 20, 10, 0, tzinfo=UTC)),
        ]
        for ticket, attempt, created in runs:
            await connection.execute(
                text(
                    "INSERT INTO step_run (ticket_id, step_name, kind, attempt, status, "
                    "created_at) VALUES (:ticket, 'build', 'agent', :attempt, 'succeeded', "
                    ":created)"
                ),
                {"ticket": ticket, "attempt": attempt, "created": created},
            )
        await connection.execute(
            text(
                "INSERT INTO event (project_id, ticket_id, type, details, created_at) "
                "VALUES (:project, :ticket, 'step_entered', CAST(:details AS jsonb), :created)"
            ),
            {
                "project": project_id,
                "ticket": ticket_id,
                "created": datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
                "details": '{"step": "build", "attempt": 2, "reason": "reject_rerun"}',
            },
        )
    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT ticket_id, attempt, entered_by FROM step_run "
                        "WHERE ticket_id IN (:ticket, :pre_entry) ORDER BY ticket_id, attempt"
                    ),
                    {"ticket": ticket_id, "pre_entry": pre_entry_id},
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            (ticket_id, 1, None),
            (ticket_id, 2, "reject_rerun"),
            (ticket_id, 3, "retry"),
            (pre_entry_id, 1, None),
            (pre_entry_id, 2, None),
        ]
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0025_backfill_sums_each_pause_a_person_released(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """Every duration on the dashboard subtracts `parked_seconds`, so a backfill that pairs a park
    with the wrong resume, or counts a rollover's own park and resume, skews every resumed run's
    time at deploy, with no error. The first run is stopped and released by its label 2h later,
    rolls over (not a pause), then idles and is resumed from the dashboard 30m later. The second
    is a rollover whose resume was refused, released 1h later by a resume with no `via`, as older
    label releases carry. The third is parked now: nothing has ended the pause.

    Rows are committed so the migration sees them, and removed afterwards however the assertion
    goes, because the other downgrade tests share this database.
    """
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0024")
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (name, github_repo) "
                    "VALUES ('parked', 'acme/parked') RETURNING id"
                )
            )
        ).scalar_one()
        source_id = (
            await connection.execute(
                text(
                    "INSERT INTO ticket_source (project_id, kind, external_ref) "
                    "VALUES (:project, 'github', 'acme/parked') RETURNING id"
                ),
                {"project": project_id},
            )
        ).scalar_one()
        version_id = (
            await connection.execute(
                text(
                    "INSERT INTO process_version "
                    "(project_id, process_key, source_commit_sha, content) "
                    "VALUES (:project, 'standard', :sha, '{}'::jsonb) RETURNING id"
                ),
                {"project": project_id, "sha": "d" * 40},
            )
        ).scalar_one()
        ticket_id = (
            await connection.execute(
                text(
                    "INSERT INTO ticket (project_id, ticket_source_id, external_key, "
                    "process_version_id, owner_at_admission, current_step, status) "
                    "VALUES (:project, :source, '9', :version, 'operator', 'build', 'running') "
                    "RETURNING id"
                ),
                {"project": project_id, "source": source_id, "version": version_id},
            )
        ).scalar_one()
        run_ids: list[int] = []
        for attempt, status in ((1, "succeeded"), (2, "running"), (3, "interrupted")):
            run_ids.append(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO step_run (ticket_id, step_name, kind, attempt, status, "
                            "interactive) VALUES (:ticket, 'build', 'agent', :attempt, :status, "
                            "true) RETURNING id"
                        ),
                        {"ticket": ticket_id, "attempt": attempt, "status": status},
                    )
                ).scalar_one()
            )
        resumed, released, parked = run_ids
        t0 = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
        marks = [
            (resumed, "agent_interrupted", {}, t0),
            (resumed, "agent_resumed", {"via": "label"}, t0 + timedelta(hours=2)),
            (resumed, "agent_parked", {"reason": "rollover"}, t0 + timedelta(hours=3)),
            (resumed, "agent_resumed", {"via": "rollover"}, t0 + timedelta(hours=3, seconds=5)),
            (resumed, "agent_parked", {"reason": "idle"}, t0 + timedelta(hours=4)),
            (resumed, "agent_resumed", {"via": "dashboard"}, t0 + timedelta(hours=4, minutes=30)),
            (released, "agent_parked", {"reason": "rollover"}, t0),
            (released, "agent_resumed", {}, t0 + timedelta(hours=1)),
            (parked, "agent_interrupted", {}, t0),
        ]
        for run_id, kind, details, at in marks:
            await connection.execute(
                text(
                    "INSERT INTO event (project_id, ticket_id, step_run_id, type, details, "
                    "created_at) VALUES (:project, :ticket, :run, :type, "
                    "CAST(:details AS jsonb), :at)"
                ),
                {
                    "project": project_id,
                    "ticket": ticket_id,
                    "run": run_id,
                    "type": kind,
                    "details": json.dumps(details),
                    "at": at,
                },
            )
    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, parked_seconds FROM step_run WHERE ticket_id = :ticket "
                        "ORDER BY attempt"
                    ),
                    {"ticket": ticket_id},
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            (resumed, 9000.0),
            (released, 3600.0),
            (parked, 0.0),
        ]
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0027_upgrade_puts_everything_in_the_default_organization(
    migration_database: tuple[Config, AsyncEngine], master_key: None
) -> None:
    """Every existing project, runtime, token, conversation and spend row lands in `default`, and
    the assistant's settings document and credential follow it. The credential is encrypted again
    under `default`'s AAD, so the assistant answers on the same key without anyone entering it;
    a blob the master key cannot open is dropped."""
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0026")
    async with engine.begin() as connection:
        project_id = (
            await connection.execute(
                text(
                    "INSERT INTO project (name, github_repo) "
                    "VALUES ('tenant', 'acme/tenant') RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text("INSERT INTO runtime (slug, name, type) VALUES ('tenant-rt', 'rt', 'codex_api')")
        )
        await connection.execute(
            text(
                "INSERT INTO app_setting (key, value, updated_by) VALUES "
                "('assistant', '{\"enabled\": true}'::jsonb, 'operator'), "
                "('assistant_status', '{\"ok\": true}'::jsonb, 'assistant')"
            )
        )
        stored = encrypt("sk-ant-kept", aad=f"app:{SLOT}".encode())
        await connection.execute(
            text(
                "INSERT INTO app_credential (name, value_ciphertext, enc_key_id, updated_by) "
                "VALUES (:name, :ciphertext, :key_id, 'operator'), "
                "('assistant:codex_cli:api_key', 'x'::bytea, 'k', 'operator')"
            ),
            {"name": SLOT, "ciphertext": stored.ciphertext, "key_id": stored.enc_key_id},
        )
        conversation_id = (
            await connection.execute(
                text(
                    "INSERT INTO chat_conversation (owner, backend_kind, updated_at) "
                    "VALUES ('tenant@example.com', 'claude_sdk', '2026-01-01T00:00:00Z') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO chat_turn (conversation_id, seq, status, trigger, cost_usd, "
                "started_at) VALUES (:conversation, 1, 'done', 'user', 1.5, now())"
            ),
            {"conversation": conversation_id},
        )
    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            default = (await connection.execute(text(DEFAULT_ORG))).scalar_one()
            owners = (
                await connection.execute(
                    text(
                        "SELECT (SELECT organization_id FROM project WHERE id = :project), "
                        "(SELECT organization_id FROM runtime WHERE slug = 'tenant-rt'), "
                        "(SELECT organization_id FROM chat_conversation WHERE id = :conv), "
                        "(SELECT organization_id FROM chat_spend "
                        "WHERE owner = 'tenant@example.com')"
                    ),
                    {"project": project_id, "conv": conversation_id},
                )
            ).one()
            moved = (
                await connection.execute(
                    text("SELECT organization_id, key, value FROM organization_setting")
                )
            ).all()
            kept = (await connection.execute(text("SELECT key FROM app_setting"))).scalars().all()
            clock_kept = (
                await connection.execute(
                    text(
                        "SELECT updated_at = '2026-01-01T00:00:00Z' FROM chat_conversation "
                        "WHERE id = :conv"
                    ),
                    {"conv": conversation_id},
                )
            ).scalar_one()
            credentials = (
                await connection.execute(
                    text(
                        "SELECT organization_id, name, value_ciphertext, enc_key_id, updated_by "
                        "FROM organization_credential"
                    )
                )
            ).all()
            tables = await connection.run_sync(_table_names)
        assert tuple(owners) == (default, default, default, default)
        [credential] = credentials
        assert (credential.organization_id, credential.name, credential.updated_by) == (
            default,
            SLOT,
            "operator",
        )
        moved_value = EncryptedValue(credential.value_ciphertext, credential.enc_key_id)
        assert decrypt(moved_value, aad=f"org:{default}:{SLOT}".encode()).get_secret_value() == (
            "sk-ant-kept"
        )
        # The retention clock: an upgrade that touched the row would keep it 30 more days.
        assert clock_kept
        assert [tuple(row) for row in moved] == [(default, "assistant", {"enabled": True})]
        assert kept == ["assistant_status"]
        assert "app_credential" not in tables
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_conversation"))
            await connection.execute(text("DELETE FROM chat_spend"))
            await connection.execute(text("DELETE FROM organization_setting"))
            await connection.execute(text("DELETE FROM organization_credential"))
            await connection.execute(text("DELETE FROM app_setting"))
            await connection.execute(text("DELETE FROM runtime WHERE slug = 'tenant-rt'"))
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0027_downgrade_copies_the_settings_and_the_credential_back(
    migration_database: tuple[Config, AsyncEngine], master_key: None
) -> None:
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO organization_setting (organization_id, key, value, updated_by) "
                f"VALUES ({DEFAULT_ORG}, 'assistant', '{{\"enabled\": true}}'::jsonb, 'operator')"
            )
        )
        default = (await connection.execute(text(f"SELECT {DEFAULT_ORG}"))).scalar_one()
        stored = encrypt("sk-ant-kept", aad=f"org:{default}:{SLOT}".encode())
        await connection.execute(
            text(
                "INSERT INTO organization_credential "
                "(organization_id, name, value_ciphertext, enc_key_id, updated_by) "
                "VALUES (:org, :name, :ciphertext, :key_id, 'operator')"
            ),
            {
                "org": default,
                "name": SLOT,
                "ciphertext": stored.ciphertext,
                "key_id": stored.enc_key_id,
            },
        )
    try:
        await asyncio.to_thread(command.downgrade, alembic_config, "0026")
        async with engine.connect() as connection:
            settings = (await connection.execute(text("SELECT key, value FROM app_setting"))).all()
            [credential] = (
                await connection.execute(
                    text("SELECT name, value_ciphertext, enc_key_id FROM app_credential")
                )
            ).all()
        assert [tuple(row) for row in settings] == [("assistant", {"enabled": True})]
        assert credential.name == SLOT
        moved_value = EncryptedValue(credential.value_ciphertext, credential.enc_key_id)
        assert decrypt(moved_value, aad=f"app:{SLOT}".encode()).get_secret_value() == "sk-ant-kept"
    finally:
        # The credential too: the upgrade would move it into `organization_credential`, where a
        # later downgrade without this test's master key cannot open it.
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM app_setting"))
            await connection.execute(text("DELETE FROM app_credential"))
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0027_downgrade_refuses_while_two_organizations_exist(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """Dropping `organization_id` would merge two tenants' projects, runtimes and budgets, and
    the restored global uniques could not even hold."""
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO organization (slug, name, updated_by) VALUES ('acme', 'Acme', 't')")
        )
    try:
        with pytest.raises(DBAPIError, match="more than one organization"):
            await asyncio.to_thread(command.downgrade, alembic_config, "0026")
        async with engine.connect() as connection:
            assert "organization" in await connection.run_sync(_table_names)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM organization WHERE slug = 'acme'"))


async def test_the_0031_upgrade_strips_interactive_from_runtime_details(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """`_Details` forbids extra keys and the Runtime page sends the whole map back, so a key left
    on a row would refuse every edit of it with a 422."""
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0030")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO runtime (organization_id, slug, name, type, details) VALUES "
                f"({DEFAULT_ORG}, 'opt-in', 'opt-in', 'claude_api', CAST(:opt_in AS jsonb)), "
                f"({DEFAULT_ORG}, 'opt-out', 'opt-out', 'claude_api', CAST(:opt_out AS jsonb))"
            ),
            {
                "opt_in": json.dumps({"isolation": "e2b", "template": "t:b", "interactive": True}),
                "opt_out": json.dumps({"interactive": False}),
            },
        )
    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT slug, details FROM runtime WHERE slug IN ('opt-in', 'opt-out')")
                )
            ).all()
        details_by_slug = {slug: details for slug, details in rows}
        assert details_by_slug == {"opt-in": {"isolation": "e2b", "template": "t:b"}, "opt-out": {}}
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM runtime WHERE slug IN ('opt-in', 'opt-out')")
            )
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0031_upgrade_refuses_while_a_headless_agent_run_is_live(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """This release cannot poll or resume a headless run, and a tick that met one would roll back
    on every pass. A finished run does not count, nor a row of another kind, which is never
    interactive: a check step's row stays running while CI runs, so counting it would refuse
    every deploy made during a CI wait."""
    alembic_config, engine = migration_database
    await asyncio.to_thread(command.downgrade, alembic_config, "0030")
    async with engine.begin() as connection:
        project_id, ticket_id = await _seed_ticket(connection, "headless", "7")
        await connection.execute(
            text(
                "INSERT INTO step_run (ticket_id, step_name, kind, attempt, status, interactive) "
                "VALUES (:ticket, 'build', 'agent', 1, 'succeeded', false), "
                "(:ticket, 'build', 'agent', 2, 'running', false), "
                "(:ticket, 'review', 'human_gate', 1, 'waiting', false), "
                "(:ticket, 'ci', 'check', 1, 'running', false)"
            ),
            {"ticket": ticket_id},
        )
    try:
        with pytest.raises(RuntimeError, match=r"1 agent run.*acme/headless#7"):
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        assert version == "0030"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE step_run SET status = 'failed' WHERE ticket_id = :ticket "
                    "AND kind = 'agent' AND status = 'running'"
                ),
                {"ticket": ticket_id},
            )
        # The check row alone, still running: the upgrade passes.
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0032_downgrade_marks_every_agent_run_a_session(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """The revert path (`alembic downgrade 0030`, then the old image): that image reads
    `interactive = false` as a headless run, and would neither resume nor attach to the session
    an agent row holds."""
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        project_id, ticket_id = await _seed_ticket(connection, "revert", "29")
        await connection.execute(
            text(
                "INSERT INTO runtime (organization_id, slug, name, type, details) VALUES "
                f"({DEFAULT_ORG}, 'revert', 'revert', 'claude_api', '{{}}'::jsonb)"
            )
        )
        agent, action = (
            await connection.execute(
                text(
                    "INSERT INTO step_run (ticket_id, step_name, kind, attempt, status) "
                    "VALUES (:ticket, 'build', 'agent', 1, 'interrupted'), "
                    "(:ticket, 'ship', 'action', 1, 'succeeded') RETURNING id"
                ),
                {"ticket": ticket_id},
            )
        ).scalars()
    try:
        await asyncio.to_thread(command.downgrade, alembic_config, "0030")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT id, interactive FROM step_run WHERE ticket_id = :ticket"),
                    {"ticket": ticket_id},
                )
            ).all()
            default = (
                await connection.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = 'step_run' AND column_name = 'interactive'"
                    )
                )
            ).scalar_one()
            details = (
                await connection.execute(
                    text("SELECT details->>'interactive' FROM runtime WHERE slug = 'revert'")
                )
            ).scalar_one()
        interactive = {run_id: value for run_id, value in rows}
        assert interactive[agent] is True
        assert interactive[action] is False
        assert default == "false"
        assert details == "true"
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM project WHERE id = :id"), {"id": project_id})
            await connection.execute(text("DELETE FROM runtime WHERE slug = 'revert'"))
        await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_the_0035_downgrade_refuses_while_a_membership_exists(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """The older image has no membership, so it would show every organization to everyone who
    signs in."""
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        user_id = (
            await connection.execute(
                text(
                    "INSERT INTO app_user (email, name) VALUES ('downgrade@example.com', 'D') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO organization_member (organization_id, user_id, role_id, added_by) "
                f"VALUES ({DEFAULT_ORG}, :user, "
                "(SELECT id FROM role WHERE key = 'member' AND built_in), 't')"
            ),
            {"user": user_id},
        )
    try:
        with pytest.raises(DBAPIError, match="a membership or an invite exists"):
            await asyncio.to_thread(command.downgrade, alembic_config, "0034")
        async with engine.connect() as connection:
            assert "organization_member" in await connection.run_sync(_table_names)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": user_id})


async def test_the_0036_downgrade_refuses_while_a_removal_exists(
    migration_database: tuple[Config, AsyncEngine],
) -> None:
    """The older image would add a removed person in an allowed domain back to `default`."""
    alembic_config, engine = migration_database
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO organization_removal "
                "(organization_id, email, reason, removed_by, removed_at) "
                f"VALUES ({DEFAULT_ORG}, 'gone@example.com', 'removed', 't', now())"
            )
        )
    try:
        with pytest.raises(DBAPIError, match="a removal exists"):
            await asyncio.to_thread(command.downgrade, alembic_config, "0035")
        async with engine.connect() as connection:
            assert "organization_removal" in await connection.run_sync(_table_names)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM organization_removal"))
