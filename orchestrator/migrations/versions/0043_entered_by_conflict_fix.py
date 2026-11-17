"""step_run.entered_by gains conflict_fix and conflict_fixed

A merging gate can send a conflicting pull request back to an agent step. `conflict_fix` names the
first run of that step, and `conflict_fixed` the gate's own re-entry once the fix lands, so the
timeline can say why each run exists.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade refuses while any run carries either value: the narrower CHECK would reject the rows.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry', 'restart', 'redo')"
_NEW = (
    "entered_by IN ('next', 'reject_rerun', 'admission', 'retry', 'restart', 'redo', "
    "'conflict_fix', 'conflict_fixed')"
)


def upgrade() -> None:
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice.
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _NEW)


def downgrade() -> None:
    fixed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM step_run "
                "WHERE entered_by IN ('conflict_fix', 'conflict_fixed')"
            )
        )
        .scalar_one()
    )
    if fixed:
        raise RuntimeError(
            f"{fixed} run(s) were entered by a conflict fix, and revision 0042 can name neither "
            "reason. This migration will not guess."
        )
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _OLD)
