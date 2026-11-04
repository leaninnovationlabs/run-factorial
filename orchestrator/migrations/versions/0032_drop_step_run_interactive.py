"""step_run loses `interactive` — every agent run is a session, and nothing reads the column

Revision 0020 stamped `interactive` at spawn to tell a session from a headless process. Every
agent run is now a session (tech-stack.md decision 56) and 0031 refused to upgrade past a live
headless one, so the column says nothing the kind does not.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade restores 0031's column exactly: `true` on every agent row, because each one ran as a
session, and `false` on gate, action and check rows, then the old `false` default. 0031's own
downgrade then puts `"interactive": true` on every runtime, so `alembic downgrade 0030` from this
image leaves the old image a database it reads as it wrote it.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("step_run", "interactive")


def downgrade() -> None:
    op.add_column(
        "step_run",
        sa.Column("interactive", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.get_bind().execute(sa.text("UPDATE step_run SET interactive = false WHERE kind <> 'agent'"))
    op.alter_column("step_run", "interactive", server_default=sa.text("false"))
