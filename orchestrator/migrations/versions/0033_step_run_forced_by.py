"""step_run.forced_by — who pressed Continue with warning on a run whose conditions failed

A person who has read a step's failed conditions may accept it anyway (decision 78). The run
then reads SUCCEEDED, and this column names the person. NULL is every other run: one the
predicates accepted, one that did not succeed, and every run from before this revision.
A reader that counts outcomes splits on it, so a forced step never reads as a success.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column, after which a forced run reads as succeeded.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("step_run", sa.Column("forced_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("step_run", "forced_by")
