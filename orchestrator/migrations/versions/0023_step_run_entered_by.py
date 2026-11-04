"""step_run.entered_by — why an attempt exists, on the row that carries its cost

The dashboard's rework figure is the spend on attempts a gate sent back (tech-stack.md decision
61). Until now that fact lived only on the `step_entered` event, which has no `step_run_id` (it is
appended before the run exists) and matches a run on `details->>'step'` and `details->>'attempt'`.
Every other cost figure is a query over `step_run ⋈ ticket`, and reading the log per request would
put a JSONB join inside a cost read model. So the worker stamps the reason on the row at creation
(`worker.tick_context._insert_run`) and this revision copies it onto the rows that already exist.

The backfill is the worker's own predicate: the newest `step_entered` whose predicted attempt is
this row's. A row no entry names is `retry` when its attempt is above 1 and it was created at or
after its ticket's first `step_entered` — from then on every re-entry writes one, so only a failure
retry or a human-granted attempt re-runs a step without it. Every other unnamed row stays NULL: it
predates `step_entered` (2026-09-01) on its ticket, and a gate's rework from then looks the same as
a retry, so nothing in the log can say what it was.

TEXT + CHECK like every other enumeration here (`ENTERED_BY` in `db/models.py`), added after the
backfill so the copied values are checked too.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column; a run keeps its cost and forgets why it ran.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTERED_BY = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry')"

# The terminal step's entry carries a null attempt and names no run.
_FROM_ENTRIES = """
UPDATE step_run AS r
SET entered_by = e.reason
FROM (
    SELECT DISTINCT ON (ticket_id, details->>'step', (details->>'attempt')::int)
        ticket_id,
        details->>'step' AS step,
        (details->>'attempt')::int AS attempt,
        details->>'reason' AS reason
    FROM event
    WHERE type = 'step_entered' AND details->>'attempt' IS NOT NULL
    ORDER BY ticket_id, details->>'step', (details->>'attempt')::int, id DESC
) AS e
WHERE e.ticket_id = r.ticket_id AND e.step = r.step_name AND e.attempt = r.attempt
"""

_RETRIES = """
UPDATE step_run AS r
SET entered_by = 'retry'
WHERE r.entered_by IS NULL AND r.attempt > 1 AND r.created_at >= (
    SELECT min(e.created_at) FROM event AS e
    WHERE e.ticket_id = r.ticket_id AND e.type = 'step_entered'
)
"""


def upgrade() -> None:
    op.add_column("step_run", sa.Column("entered_by", sa.Text(), nullable=True))
    op.execute(sa.text(_FROM_ENTRIES))
    op.execute(sa.text(_RETRIES))
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _ENTERED_BY)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.drop_column("step_run", "entered_by")
