"""step_run.superseded_at — the runs a redo set aside

A redo throws away a step's runs and every later step's, and runs the step again from a clean
start (decision 76). A restart does that for a whole ticket by starting a new generation; a redo
does it within one, so it marks the runs instead. The loop decides without a set-aside run, while
attempt numbering and answered reviews still see it: the attempt number is the idempotent-spawn
key, and a review answered before the redo stays answered. History and spend count it too.

`entered_by` gains `redo`, the reason the redone step's first run exists.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade refuses while any run is set aside or entered by a redo: those runs would read as
current, and those entries as unnamed.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry', 'restart')"
_NEW = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry', 'restart', 'redo')"


def upgrade() -> None:
    op.add_column(
        "step_run", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice.
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _NEW)


def downgrade() -> None:
    redone = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM step_run "
                "WHERE superseded_at IS NOT NULL OR entered_by = 'redo'"
            )
        )
        .scalar_one()
    )
    if redone:
        raise RuntimeError(
            f"{redone} run(s) were set aside or entered by a redo, and revision 0033 can say "
            "neither: the set-aside runs would read as current. This migration will not guess."
        )
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _OLD)
    op.drop_column("step_run", "superseded_at")
