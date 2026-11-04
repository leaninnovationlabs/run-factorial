"""ticket_source.kind admits 'gitlab' — a parallel intake forge, beside github.

GitHub and Jira rows are untouched. Downgrade refuses if any gitlab source exists.

This follows 0027 (organization). Main took 0026 and 0027.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "kind IN ('github', 'jira')"
_NEW = "kind IN ('github', 'jira', 'gitlab')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ticket_source_kind"), "ticket_source", type_="check")
    op.create_check_constraint(op.f("ck_ticket_source_kind"), "ticket_source", _NEW)


def downgrade() -> None:
    conn = op.get_bind()
    leftover = conn.execute(
        sa.text("SELECT count(*) FROM ticket_source WHERE kind = 'gitlab'")
    ).scalar_one()
    if leftover:
        raise RuntimeError(
            f"cannot narrow ticket_source.kind: {leftover} gitlab source(s) still exist"
        )
    op.drop_constraint(op.f("ck_ticket_source_kind"), "ticket_source", type_="check")
    op.create_check_constraint(op.f("ck_ticket_source_kind"), "ticket_source", _OLD)
