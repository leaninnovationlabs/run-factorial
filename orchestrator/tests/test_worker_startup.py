"""One worker, asserted at startup (tech-stack decision 40).

Three layers, all against real Postgres (Docker):

* `worker.singleton` itself: a first connection acquires the lock, a second connection's try-lock
  fails and names the holder, and releasing frees it for the next acquirer. No migration needed:
  `pg_advisory_lock` is a builtin, not a table.
* `worker.__main__._run`'s own refusal: with the lock already held on a separate connection,
  `_run` must raise before touching anything else (no GitHub client, no runtime adapter, no
  `run_forever`) — `_run` IS the testable seam the task asks for; nothing needed extracting.
* `_run`'s lease release: once the lock is held, a lease a killed worker left is gone before the
  first pass.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from orchestrator.db.models import Ticket
from orchestrator.settings import Settings
from orchestrator.worker import __main__ as worker_main
from orchestrator.worker.singleton import SINGLETON_LOCK_KEY, WorkerAlreadyRunning, acquire, release
from tests.reconcile_world import World, _grant_lease, ticket_row

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


async def test_the_lock_key_is_pinned() -> None:
    """The whole point of computing this in Python (rather than Postgres's own, not
    version-contracted `hashtext()`) is that it never moves across builds — pin the value."""
    assert SINGLETON_LOCK_KEY == -9160083825654531822


async def test_first_connection_acquires_the_lock(database_url: str) -> None:
    connection = await acquire(database_url)
    try:
        assert not connection.is_closed()
    finally:
        await release(connection)


async def test_a_second_connection_fails_fast_and_names_the_holder(database_url: str) -> None:
    first = await acquire(database_url)
    try:
        with pytest.raises(WorkerAlreadyRunning) as excinfo:
            await acquire(database_url)
        message = str(excinfo.value)
        assert "already holds the singleton lock" in message
        # The docstrings promise "naming the first" — prove the holder is actually named,
        # not just that the lock was denied.
        assert "pid=" in message
    finally:
        await release(first)


async def test_release_frees_the_lock_for_the_next_acquirer(database_url: str) -> None:
    first = await acquire(database_url)
    await release(first)

    second = await acquire(database_url)
    await release(second)


async def test_closing_the_connection_without_release_also_frees_the_lock(
    database_url: str,
) -> None:
    """The module docstring's load-bearing claim: the lock is tied to the connection's session,
    not to calling `release()` specifically."""
    first = await acquire(database_url)
    await first.close()

    second = await acquire(database_url)
    await release(second)


async def test_run_refuses_when_another_worker_already_holds_the_lock(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run` must raise from the singleton guard alone, before it ever reaches `run_forever` —
    which loops forever and would hang this test (and CI) on a regression rather than failing it.
    `run_forever` is monkeypatched to raise loudly if `_run` ever reaches it: a regression
    then fails fast with an assertion naming exactly what broke, instead of hanging."""

    async def _unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("reached run_forever — the singleton guard failed")

    monkeypatch.setattr(worker_main, "run_forever", _unreachable)

    holder = await acquire(database_url)
    try:
        settings = Settings(database_url=database_url)
        with pytest.raises(WorkerAlreadyRunning):
            await worker_main._run(settings)
    finally:
        await release(holder)


async def test_run_releases_a_lease_a_killed_worker_left_before_the_first_pass(
    world: World, migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy kills the old task mid-tick and the new task's worker has a new name, so
    `acquire_leases`, which never reclaims an unexpired lease, would keep the ticket out of every
    pass for `lease_seconds`."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _grant_lease(session, ticket, owner="killed-task", ttl=timedelta(hours=1))
    seen: list[str | None] = []

    def no_deps(*args: object, **kwargs: object) -> None:
        return None

    async def first_pass(*args: object, **kwargs: object) -> None:
        seen.append((await ticket_row(world)).lease_owner)

    monkeypatch.setattr(worker_main, "session_scope", world.sessions)
    monkeypatch.setattr(worker_main, "production_deps", no_deps)
    monkeypatch.setattr(worker_main, "run_forever", first_pass)

    await worker_main._run(Settings(database_url=migrated_database))

    assert seen == [None]
