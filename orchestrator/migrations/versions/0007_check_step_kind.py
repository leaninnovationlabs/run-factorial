"""step_run.kind admits 'check' — a step that completes on observed conditions alone

`kind: check` is the fourth step kind: no agent, no action, no human — the step IS its
acceptance gate, waiting attempt-free until its conditions hold and consuming an attempt only
at its timeout. Its rows carry `runtime_kind` NULL like an action's, because nothing ran.

Nothing is backfilled: widening a CHECK cannot invalidate a row that already passed it.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade narrows the vocabulary and refuses when rows use the value it removes, for 0004's
reason: rewriting them to another kind would misname what happened in an append-only history.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "kind IN ('agent', 'human_gate', 'action')"
_NEW = "kind IN ('agent', 'human_gate', 'action', 'check')"


def upgrade() -> None:
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice
    # (0003 has the full reason).
    op.drop_constraint(op.f("ck_step_run_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_kind"), "step_run", _NEW)


def downgrade() -> None:
    stranded = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM step_run WHERE kind = 'check'"))
        .scalar_one()
    )
    if stranded:
        raise RuntimeError(
            f"{stranded} step_run row(s) are 'check', which revision 0006's vocabulary cannot "
            "express. Decide what they should become and update them explicitly before "
            "downgrading — this migration will not guess, because renaming them would misname "
            "what actually happened."
        )
    op.drop_constraint(op.f("ck_step_run_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_kind"), "step_run", _OLD)
