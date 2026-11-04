"""step_run.status admits 'idle', and step_run.interactive marks a run with a terminal session

An interactive run whose agent has stopped at its prompt is alive and owes the ticket a decision
— finish or wait — so it is neither `running` nor any terminal state (tech-stack.md decision 56).
`interactive` is stamped at spawn: the dashboard's Resume and the idle branch apply only to such
runs, and the event that says so is not where a list query should have to look.

Nothing is backfilled. Every existing row passed the narrower CHECK, and every existing run was
headless, which is what `interactive`'s default says.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade refuses while any row is `idle` rather than rewriting it, as 0004 does for
`interrupted`: `running` would have the loop poll a session it can no longer read, and any
terminal status would let it retry or advance past a session that is still open.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "status IN ('running', 'waiting', 'succeeded', 'failed', 'timeout', 'cancelled', "
    "'approved', 'rejected', 'interrupted')"
)
_NEW = (
    "status IN ('running', 'waiting', 'succeeded', 'failed', 'timeout', 'cancelled', "
    "'approved', 'rejected', 'interrupted', 'idle')"
)


def upgrade() -> None:
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice
    # (0003 has the full reason).
    op.drop_constraint(op.f("ck_step_run_status"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_status"), "step_run", _NEW)
    op.add_column(
        "step_run",
        sa.Column("interactive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    stranded = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM step_run WHERE status = 'idle'"))
        .scalar_one()
    )
    if stranded:
        raise RuntimeError(
            f"{stranded} step_run row(s) are 'idle', which revision 0019's vocabulary cannot "
            "express. Finish or interrupt those sessions, or update the rows explicitly, before "
            "downgrading — this migration will not guess what an open session should become."
        )
    op.drop_column("step_run", "interactive")
    op.drop_constraint(op.f("ck_step_run_status"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_status"), "step_run", _OLD)
