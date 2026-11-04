"""ticket.status admits 'exited' — a ticket taken off the factory floor that can come back

`ck_ticket_status` has permitted four values since `0001_initial.py`. A ticket a human sent
away is a fifth (tech-stack.md decision 29): not `blocked`, or the UI could not tell "escalated"
from "sent away"; not `done`, because done is never leased again and an exited ticket is
observed every tick for the label removal that brings it back.

Nothing is backfilled: widening a CHECK cannot invalidate a row that already passed it.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade narrows the vocabulary and refuses when rows use the value it removes, for 0004's
reason: an exited ticket rewritten to `blocked` would be released by the removal of a label it
does not carry, and rewritten to `done` it could never come back.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('running', 'waiting_gate', 'blocked', 'done')"
_NEW = "status IN ('running', 'waiting_gate', 'blocked', 'done', 'exited')"


def upgrade() -> None:
    # `op.f(...)`: the name is already final, so the naming convention cannot prefix it twice
    # (0003 has the full reason).
    op.drop_constraint(op.f("ck_ticket_status"), "ticket", type_="check")
    op.create_check_constraint(op.f("ck_ticket_status"), "ticket", _NEW)


def downgrade() -> None:
    stranded = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM ticket WHERE status = 'exited'"))
        .scalar_one()
    )
    if stranded:
        raise RuntimeError(
            f"{stranded} ticket row(s) are 'exited', which revision 0007's vocabulary cannot "
            "express. Decide what they should become and update them explicitly before "
            "downgrading — this migration will not guess, because 'blocked' would release "
            "them on a label they do not carry and 'done' would strand them for ever."
        )
    op.drop_constraint(op.f("ck_ticket_status"), "ticket", type_="check")
    op.create_check_constraint(op.f("ck_ticket_status"), "ticket", _OLD)
