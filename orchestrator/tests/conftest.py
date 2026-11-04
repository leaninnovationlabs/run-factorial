"""Postgres-backed fixtures.

Everything here needs Docker: the loop's correctness is lease behaviour, unique-constraint
idempotency and trigger-maintained audit columns, none of which SQLite can exercise
(tech-stack.md decision 11). Without a usable container these tests error rather than skip: a
skipped Postgres test would read as a pass. Every test that needs one is marked `postgres`, so
`-m "not postgres"` runs the rest without Docker.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy import NullPool, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from orchestrator.api import wakes
from orchestrator.secrets.crypto import load_keyring
from orchestrator.settings import get_settings
from tests.api_harness import (
    TOKEN,
    SeededChatTurn,
    build_app,
    client_for,
    make_settings,
    seed_running_chat,
)
from tests.paths import REPO_ROOT
from tests.reconcile_world import World, _build_world

ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS = REPO_ROOT / "orchestrator" / "migrations"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:17", driver="asyncpg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    # script_location in the ini is relative to the repo root; make it absolute so the tests
    # do not depend on pytest's working directory.
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture(scope="session")
async def migrated_database(alembic_config: Config, database_url: str) -> str:
    # env.py owns its own event loop (asyncio.run), so alembic must not run on ours.
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    return database_url


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: the container goes away with the session, and nothing here is hot enough to
    # care about connection reuse.
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
async def migration_database(database_url: str) -> AsyncIterator[tuple[Config, AsyncEngine]]:
    """A second database on the same container, for tests that downgrade.

    A downgrade on the shared database refuses while any test's committed rows use a value it
    removes, so the round trips run here and no test depends on sorting after them.
    """
    admin = create_async_engine(database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text("CREATE DATABASE migrations"))
    await admin.dispose()
    url = make_url(database_url).set(database="migrations").render_as_string(hide_password=False)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        yield cfg, engine
    finally:
        await engine.dispose()


@pytest.fixture
async def async_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def world(maker: async_sessionmaker[AsyncSession]) -> World:
    return await _build_world(maker)


@pytest.fixture
async def chat_turn(maker: async_sessionmaker[AsyncSession]) -> SeededChatTurn:
    """A conversation with a running turn."""
    async with maker() as session:
        seeded = await seed_running_chat(session)
        await session.commit()
    return seeded


@pytest.fixture
async def no_db_client() -> AsyncIterator[AsyncClient]:
    """An app whose session dependency raises — anything that reaches the DB fails loudly."""
    async with client_for(build_app(make_settings(), None)) as client:
        yield client


@pytest.fixture
async def client(async_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with client_for(build_app(make_settings(), async_session)) as client:
        yield client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _short_pass_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dashboard request waits for the worker's pass (`api.wakes`). No test has a worker behind
    the API unless it stands one in, so the wait is cut short for all of them."""
    monkeypatch.setattr(wakes, "WAIT_SECONDS", 0.5)
    monkeypatch.setattr(wakes, "_POLL_SECONDS", 0.02)


@pytest.fixture
def master_key() -> Iterator[None]:
    """The secret store reads the *global* settings, not the dependency-injected ones, so the
    envelope key has to come from the environment (and both caches have to be cleared)."""
    previous = os.environ.get("FACTORIAL_MASTER_KEY")
    os.environ["FACTORIAL_MASTER_KEY"] = base64.b64encode(os.urandom(32)).decode()
    get_settings.cache_clear()
    load_keyring.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("FACTORIAL_MASTER_KEY", None)
        else:
            os.environ["FACTORIAL_MASTER_KEY"] = previous
        get_settings.cache_clear()
        load_keyring.cache_clear()
