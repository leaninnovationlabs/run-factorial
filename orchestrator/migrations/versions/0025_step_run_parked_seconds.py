"""step_run.parked_seconds — the time a person held a run paused, kept out of its active time

A park writes `ended_at` and a resume clears it while `started_at` stays the spawn, so
`ended_at - started_at` of a resumed run counts every pause. The dashboard's durations are the
time the agent worked (tech-stack.md decision 61). The worker now adds each pause at resume
(`worker.interrupts._resume_session`); this revision sums the pauses already in the log.

Each `agent_resumed` is paired with the run's event just before it among its parks and resumes:
an `agent_interrupted` (a human's stop) or an `agent_parked` (idle, ceiling, or a rollover whose
resume was refused and a person later released). A resume `via` `rollover` adds nothing: it
parks for no person and resumes on the same tick. Both ends are the events' `created_at`, one
clock; the worker's own `tick.now` differs from it by less than a tick.

DOUBLE PRECISION, NOT NULL DEFAULT 0: a run never paused has paused for no time.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column, and a resumed run's time counts its pauses again.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FROM_PARKS = """
UPDATE step_run
SET parked_seconds = gaps.seconds
FROM (
    SELECT step_run_id, sum(extract(epoch FROM created_at - parked_at))::double precision AS seconds
    FROM (
        SELECT
            step_run_id,
            type,
            details->>'via' AS via,
            created_at,
            lag(type) OVER marks AS before,
            lag(created_at) OVER marks AS parked_at
        FROM event
        WHERE step_run_id IS NOT NULL
            AND type IN ('agent_interrupted', 'agent_parked', 'agent_resumed')
        WINDOW marks AS (PARTITION BY step_run_id ORDER BY id)
    ) AS marks
    WHERE type = 'agent_resumed'
        AND before IN ('agent_interrupted', 'agent_parked')
        AND via IS DISTINCT FROM 'rollover'
    GROUP BY step_run_id
) AS gaps
WHERE step_run.id = gaps.step_run_id
"""


def upgrade() -> None:
    op.add_column(
        "step_run",
        sa.Column("parked_seconds", sa.Double(), server_default=sa.text("0"), nullable=False),
    )
    op.execute(sa.text(_FROM_PARKS))


def downgrade() -> None:
    op.drop_column("step_run", "parked_seconds")
