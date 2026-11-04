"""One worker, asserted at startup — not merely assumed (tech-stack decision 40).

Policy layered on top of `leases.py`, which stays correct for N runners: leases arbitrate *which
ticket* a runner may touch, not *how many runners exist*. This module adds the ceiling — at most
one worker process per database, enforced by a session-scoped advisory lock. A second worker fails
fast at startup with a message naming the first, instead of silently doubling every spawn, poll
and projection this factory makes.

The lock must be held on a dedicated connection, never one borrowed from `db.session`'s pool.
`pg_advisory_lock` / `pg_try_advisory_lock` are *session*-scoped: the lock lives exactly as long
as the backend that took it and is released by `pg_advisory_unlock` or by that connection closing,
not by COMMIT or ROLLBACK. A pooled connection can be returned, recycled, or discarded on a failed
pre-ping — any of which drops the lock without anything here noticing, letting a second worker
acquire it while the first still believes it owns the singleton. So `acquire()` opens a raw
`asyncpg` connection outside any pool and hands it back: closing it is the only way the lock goes
away. (`events.listener.listen_forever` does the same for each `LISTEN` connection.)

Accepted gap: a dead connection ends the guarantee silently and nothing polls to detect it. A
killed backend releases the lock at once, so a replacement `acquire()` succeeds while the first
worker may still run, and that replacement's startup releases the live worker's leases (the
`step_run` unique constraint still blocks a double spawn); conversely a vanished host leaves
Postgres holding the backend until its keepalives reap it (`tcp_keepalives_idle`/`_interval`/
`_count`, far longer than this factory's poll interval), and a replacement keeps raising
`WorkerAlreadyRunning` against a lock nobody uses, with no override flag. Closing either means a
heartbeat (the polling this lock avoids) or keepalive tuning, which is deployment configuration.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Final

import asyncpg

from orchestrator.db.session import asyncpg_dsn

__all__ = ["SINGLETON_LOCK_KEY", "WorkerAlreadyRunning", "acquire", "release"]

#: A stable name hashed to a signed 64-bit key for `pg_advisory_lock` / `pg_try_advisory_lock`
#: (both take one `bigint`). Hashed in Python rather than with Postgres's `hashtext()`, whose
#: algorithm is internal and not contracted to stay identical across major versions — the "same
#: lock" claim must not depend on which Postgres this runs against. The first 8 bytes of SHA-256
#: as a signed int64 stay inside `bigint`'s range by construction.
_SINGLETON_LOCK_NAME: Final = "sf-worker-singleton"
SINGLETON_LOCK_KEY: Final[int] = int.from_bytes(
    hashlib.sha256(_SINGLETON_LOCK_NAME.encode()).digest()[:8], "big", signed=True
)


class WorkerAlreadyRunning(RuntimeError):
    """Another process already holds the singleton lock (decision 40)."""


#: `pg_locks` splits a bigint advisory-lock key across two `oid` columns: `classid` holds the high
#: 32 bits, `objid` the low 32 bits, and `objsubid = 1` marks the single-bigint form (the
#: two-int32 form is `objsubid = 2`). Reassembled in SQL so it matches Postgres's own bit layout
#: rather than a hand-rolled Python copy of the same split.
_HOLDER_QUERY: Final = """
    SELECT l.pid, a.application_name, a.client_addr
    FROM pg_locks l
    LEFT JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory'
      AND l.classid = ($1::bigint >> 32)::bit(32)::int
      AND l.objid = ($1::bigint & 4294967295)::bit(32)::int
      AND l.objsubid = 1
      AND l.granted
    LIMIT 1
"""


async def _describe_holder(connection: asyncpg.Connection) -> str:
    """Best-effort: who holds the lock right now, for `acquire()`'s failure message. Runs on the
    connection that just lost `pg_try_advisory_lock`, before it is closed. Never raises — a
    `pg_locks` / `pg_stat_activity` permission issue must not crash the error path."""
    try:
        row = await connection.fetchrow(_HOLDER_QUERY, SINGLETON_LOCK_KEY)
    except Exception:
        return "holder unknown (could not query pg_locks/pg_stat_activity)"
    if row is None:
        return "holder unknown (it released between the try-lock and this query)"
    name = row["application_name"] or "unnamed"
    addr = row["client_addr"] or "local/unix-socket"
    return f"pid={row['pid']} application_name={name!r} client_addr={addr}"


async def acquire(
    database_url: str, *, worker_name: str = "factorial-worker"
) -> asyncpg.Connection:
    """Take the process-wide advisory lock on a fresh, dedicated connection, or raise.

    The lock lives exactly as long as the returned connection stays open (module docstring). The
    caller owns that connection for as long as it wants the lock held; `release()` is the intended
    way to give it back, but closing it any other way works too.
    """
    # `application_name` is what `_describe_holder` reads back from `pg_stat_activity`; without it
    # the losing worker's message cannot name the holding worker.
    connection = await asyncpg.connect(
        asyncpg_dsn(database_url), server_settings={"application_name": worker_name}
    )
    try:
        got = await connection.fetchval("SELECT pg_try_advisory_lock($1)", SINGLETON_LOCK_KEY)
    except BaseException:
        await connection.close()
        raise
    if not got:
        holder = await _describe_holder(connection)
        await connection.close()
        raise WorkerAlreadyRunning(
            "another worker process already holds the singleton lock "
            f"(key={SINGLETON_LOCK_KEY}): {holder}. Refusing to start a second one. Only one "
            "worker runs per database, so a second `python -m orchestrator.worker` always stops "
            "here."
        )
    return connection


async def release(connection: asyncpg.Connection) -> None:
    """Explicitly drop the lock, then close the connection that held it. Never raises.

    Closing the connection alone already releases a session-scoped advisory lock, so the explicit
    `pg_advisory_unlock` only makes the release observable rather than implicit in a socket
    teardown. Both steps are best-effort: a connection whose backend already died still raises
    client-side, and this runs from `__main__._run`'s `finally`, where a raise would turn a clean
    shutdown into a crash or mask whatever `run_forever` propagated.
    """
    with contextlib.suppress(Exception):
        await connection.execute("SELECT pg_advisory_unlock($1)", SINGLETON_LOCK_KEY)
    with contextlib.suppress(Exception):
        await connection.close()
