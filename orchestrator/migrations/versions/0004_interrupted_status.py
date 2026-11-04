"""step_run.status admits 'interrupted' — a run a human stopped, not one that failed

`ck_step_run_status` has permitted the same eight values since `0001_initial.py`. A human
stopping a run to redirect it is a ninth state, and it has to be its own rather than reusing
`cancelled`, because the orchestrator does opposite things with them (tech-stack.md decision 26):
a cancelled step is in `worker.tick_context._FAILED_STATUSES` and is retried by the loop, while an
interrupted one parks its ticket and waits for a human to release it.

Nothing is backfilled. Every existing row was written under the old vocabulary and is still
correct under the new one — widening a CHECK cannot invalidate a row that already passed it.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade narrows the constraint again and therefore has to say what it does about rows the old
vocabulary cannot express: it refuses rather than rewriting them, because turning a human's
interrupt into a `cancelled` on the way down would hand those tickets to the retry path, which
is the one outcome the state exists to prevent.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "status IN ('running', 'waiting', 'succeeded', 'failed', 'timeout', 'cancelled', "
    "'approved', 'rejected')"
)
_NEW = (
    "status IN ('running', 'waiting', 'succeeded', 'failed', 'timeout', 'cancelled', "
    "'approved', 'rejected', 'interrupted')"
)


def upgrade() -> None:
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice
    # (0003 has the full reason).
    op.drop_constraint(op.f("ck_step_run_status"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_status"), "step_run", _NEW)


def downgrade() -> None:
    # Refuse rather than rewrite. A row saying `interrupted` describes a run a human stopped and
    # a ticket parked waiting for them; mapping it to `cancelled` on the way down would put it
    # back in `_FAILED_STATUSES` and let the loop retry work someone deliberately halted.
    # Failing loudly leaves the operator to decide what those runs should become.
    stranded = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM step_run WHERE status = 'interrupted'"))
        .scalar_one()
    )
    if stranded:
        raise RuntimeError(
            f"{stranded} step_run row(s) are 'interrupted', which revision 0003's vocabulary "
            "cannot express. Decide what they should become and update them explicitly before "
            "downgrading — this migration will not guess, because mapping them to 'cancelled' "
            "would hand runs a human stopped back to the retry path."
        )
    op.drop_constraint(op.f("ck_step_run_status"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_status"), "step_run", _OLD)
