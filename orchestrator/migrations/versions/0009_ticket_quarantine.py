"""ticket.consecutive_failures, ticket.quarantined_at — the poison-ticket counter and stamp

A ticket whose tick raises every time (the motivating case: a repo archived mid-flight, so
GitHub 403s every write) was retried every `poll_interval_seconds` for ever, silently — nothing
counted the failure. `consecutive_failures` counts consecutive raising ticks; `quarantined_at` is
set once that count crosses `worker.quarantine_after` and is what `leases.acquire_leases` checks
to stop handing the ticket out (tech-stack.md decision 31).

This is a different budget from `defaults.retry.maxAttempts`: that counts failed WORK — a
`step_run` row for an agent that ran and failed, or a postcondition that did not hold. Quarantine
counts ticks that died before any of that could even be recorded.

`consecutive_failures` is NOT NULL, default 0: every existing ticket has failed zero ticks in a
row as far as this counter is concerned. `quarantined_at` is nullable with no default: NULL means
not quarantined, which is every ticket today.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). Unlike
0008, the downgrade needs no refusal: dropping a counter and a stamp loses operational state (how
many ticks failed, and whether the loop had given up), not a state the old vocabulary cannot
express — a ticket rewritten to have neither column just goes back to being retried every tick,
which is the bug this revision fixes, not a distinct fact worth guarding.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ticket",
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column("ticket", sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "quarantined_at")
    op.drop_column("ticket", "consecutive_failures")
