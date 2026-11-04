"""sessions only — runtime.details loses `interactive`, and no headless agent run may be live

Every agent run is now a session (tech-stack.md decision 56), so a runtime row's opt-in means
nothing. The key is stripped rather than left: `_Details` forbids extra keys, and the Runtime page
sends the whole `details` map back on every edit, so a stored `interactive` would make every edit
of an older row fail with a 422.

The upgrade refuses while an agent run that started headless is still live, as 0020's downgrade
refuses an `idle` row: this release cannot poll or resume a headless run, and a tick that met one
would roll back on every pass. Only agent rows count: a check or action step's row stays
`running`, never interactive, while CI runs. Before merging, read the runs this would refuse with

    SELECT p.github_repo, t.external_key, s.id, s.step_name, s.status FROM step_run s
    JOIN ticket t ON t.id = s.ticket_id JOIN project p ON p.id = t.project_id
    WHERE s.kind = 'agent' AND NOT s.interactive AND s.status IN ('running','idle','interrupted')
    ORDER BY s.id;

and, while the old image still runs, let a running run finish and release or exit an interrupted
one, until the query returns nothing. A row that never finishes, its run orphaned, is ended by
hand: run the query, then fail each row it names (`UPDATE step_run SET status = 'failed' WHERE
id = ...`).

A deploy stops the old task before the new one starts (tech-stack.md decision 64), so when this
upgrade refuses, no worker runs. The old task comes back only when the circuit breaker rolls back
after repeated failed starts, or when the merge is reverted; then clear the rows as above and
deploy again.

Rollback: once this upgrade has run, run `alembic downgrade 0030` from the new image first, then
revert. The downgrade puts `"interactive": true` on every runtime, which reads as the opt-in every
row had in effect.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE_HEADLESS = sa.text(
    "SELECT p.github_repo, t.external_key, s.id, s.step_name, s.status FROM step_run s "
    "JOIN ticket t ON t.id = s.ticket_id JOIN project p ON p.id = t.project_id "
    "WHERE s.kind = 'agent' AND NOT s.interactive "
    "AND s.status IN ('running', 'idle', 'interrupted') ORDER BY s.id"
)


def upgrade() -> None:
    bind = op.get_bind()
    live = bind.execute(_LIVE_HEADLESS).all()
    if live:
        runs = ", ".join(
            f"{repo}#{key} (step_run {run_id}, {step}, {status})"
            for repo, key, run_id, step, status in live
        )
        raise RuntimeError(
            f"{len(live)} agent run(s) still run headless, which this release cannot poll or "
            f"resume: {runs}. The old task stopped before this one started, and returns only "
            "when the circuit breaker rolls back or the merge is reverted. On it, let a running "
            "run finish and release or exit an interrupted one; fail a row that never finishes "
            "by hand (this revision's docstring has the SQL); then deploy again"
        )
    bind.execute(
        sa.text(
            "UPDATE runtime SET details = details - 'interactive' WHERE details ? 'interactive'"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("""UPDATE runtime SET details = details || '{"interactive": true}'::jsonb""")
    )
