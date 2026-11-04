"""ticket.generation and step_run.generation — the restart boundary

A restart puts a ticket on its lane's current snapshot and starts it fresh at the entry step
(tech-stack.md decision 67). The loop must then decide with the new generation's runs alone, while
every read model that totals or lists history keeps seeing all of them. `ticket.generation` is
bumped by the restart and `step_run.generation` stamped at insert. Attempt numbers keep counting
per `(ticket, step)`, so the unique `(ticket_id, step_name, attempt)` is unchanged.

Every existing row is generation 1: nothing could restart before this revision.

`entered_by` gains `restart`, the reason a generation's first run exists.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade refuses while any ticket has restarted: its later runs would read as retries.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry')"
_NEW = "entered_by IN ('next', 'reject_rerun', 'admission', 'retry', 'restart')"


def upgrade() -> None:
    for table in ("ticket", "step_run"):
        op.add_column(
            table,
            sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        )
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice.
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _NEW)


def downgrade() -> None:
    restarted = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM ticket WHERE generation > 1"))
        .scalar_one()
    )
    if restarted:
        raise RuntimeError(
            f"{restarted} ticket(s) have restarted, and revision 0025 cannot tell their later "
            "runs from retries of the earlier ones. Downgrading would misfile them; this "
            "migration will not guess."
        )
    op.drop_constraint(op.f("ck_step_run_entered_by"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_entered_by"), "step_run", _OLD)
    op.drop_column("step_run", "generation")
    op.drop_column("ticket", "generation")
