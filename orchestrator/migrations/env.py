"""Alembic environment — async engine, metadata and URL both taken from the application.

`compare_type` / `compare_server_default` are on because the schema leans on `TEXT` + CHECK
and on `now()` / `'{}'::jsonb` server defaults; without them a drifting column type or default
would autogenerate as nothing at all (tech-stack.md decision 2 still requires hand review).

A migration can run beside a live worker — by hand, against a database a worker uses; an ECS
deploy stops the old task first (tech-stack decision 47) — and the worker holds locks on
`ticket` for whole passes. An ALTER waiting on them stalls every read of the table
behind it, and a lock timeout alone fails the deploy — 0019's first did. So each attempt waits
briefly for its locks and gives way, and the whole run is retried until it lands between passes.
The run is one transaction, so an attempt that gives way leaves nothing behind; a revision that
committed on its own (`autocommit_block`) would break that. A pass that never ends — a GitHub
call hung inside the worker's transaction — outlasts every attempt, and the deploy fails after
about three minutes of short stalls: the transaction around network calls is the cause, and no
migration setting removes it.
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from orchestrator.db import Base
from orchestrator.settings import get_settings

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False`: fileConfig's own default is True, which DISABLES every
    # logger that already exists at call time and is not named in alembic.ini's `[loggers]`
    # (root/sqlalchemy/alembic only) — every other project logger (`orchestrator.worker.
    # reconcile`, etc.), process-wide, for the rest of the run. The session-scoped
    # `migrated_database` test fixture runs this once per test session, so without this flag
    # `caplog` silently captures nothing repo-wide from the first Postgres test onward. Strictly
    # more logging from migrations themselves as a result — benign, and not the intent this file
    # was pinning.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
log = logging.getLogger("alembic.env")

#: One attempt's wait for its locks, which is also how long a reader of a locked table stalls
#: behind it. Short, because an attempt that loses costs readers this much every time.
LOCK_TIMEOUT = config.get_main_option("factorial.lock_timeout", "2s")
#: Between attempts. With the timeout, 40 attempts cover about three minutes of a worker's
#: passes, which a 30 s tick interval leaves gaps in.
LOCK_PAUSE_SECONDS = float(config.get_main_option("factorial.lock_pause_seconds", "2.5"))
LOCK_ATTEMPTS = 40
#: Giving way: `lock_not_available` (the timeout ran out), or `deadlock_detected` — a run that
#: already holds one table can meet a worker pass that holds `ticket` and wants that table next.
_GAVE_WAY = frozenset({"55P03", "40P01"})


def _database_url() -> str:
    # Precedence: programmatic override (tests point this at a container) > -x db_url > env.
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    from_x: str | None = context.get_x_argument(as_dictionary=True).get("db_url")
    if from_x:
        return from_x
    return get_settings().database_url


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        # Local to this transaction, which is this attempt.
        connection.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"), {"timeout": LOCK_TIMEOUT}
        )
        context.run_migrations()


def _gave_way(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) in _GAVE_WAY


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        for attempt in range(1, LOCK_ATTEMPTS + 1):
            try:
                async with engine.connect() as connection:
                    await connection.run_sync(_run_migrations)
                return
            except DBAPIError as exc:
                if not _gave_way(exc) or attempt == LOCK_ATTEMPTS:
                    raise
                log.warning(
                    "attempt %d/%d gave way to a lock held past %s; retrying in %.1fs",
                    attempt,
                    LOCK_ATTEMPTS,
                    LOCK_TIMEOUT,
                    LOCK_PAUSE_SECONDS,
                )
                await asyncio.sleep(LOCK_PAUSE_SECONDS)
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        # A script has one attempt: it fails fast rather than stall every reader, and is re-run.
        context.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # asyncio.run owns the loop, so callers already inside one (tests) must invoke alembic
    # through a thread.
    asyncio.run(_run_async_migrations())
