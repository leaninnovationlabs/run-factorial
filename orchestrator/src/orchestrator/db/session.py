"""Async engine and session factory.

Nothing connects at import time: the API, the worker, Alembic and the tests all import this
module, and only some of them should ever open a pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orchestrator.settings import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        # pool_pre_ping: the worker holds connections across long GitHub polls.
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        # expire_on_commit=False: an expired attribute would need lazy IO to reload, which
        # async code cannot do implicitly.
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Release the pool — application shutdown, and between tests that swap the URL."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on clean exit, roll back on anything else."""
    session = get_sessionmaker()()
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()


SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy's URL carries the `+asyncpg` driver tag; asyncpg wants a bare libpq DSN. What a
    raw connection outside the pool connects with: a `LISTEN` (`events.listener`) and the
    worker's singleton lock."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
