"""step_run.model, .cached_input_tokens, .cost_source — a priced codex run

Codex reports tokens and no dollar figure, so the worker prices the run at list from the model
it stamped on the row at spawn and labels the result (tech-stack.md decision 34). `cost_source`
says which kind of figure `cost_usd` is: `reported` is what the substrate measured, `estimated`
is the worker's list price. NULL on both is a run nothing could price. `cached_input_tokens` is
the cache-read part of `input_tokens`, kept because it is priced at its own rate.

TEXT + CHECK like every other enumeration here (`COST_SOURCES` in `db/models.py`).

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops all three; a run keeps its `cost_usd` and forgets what kind of figure it was.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COST_SOURCE = "cost_source IN ('reported', 'estimated')"


def upgrade() -> None:
    op.add_column("step_run", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("step_run", sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("step_run", sa.Column("cost_source", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_step_run_cost_source"), "step_run", _COST_SOURCE)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_step_run_cost_source"), "step_run", type_="check")
    op.drop_column("step_run", "cost_source")
    op.drop_column("step_run", "cached_input_tokens")
    op.drop_column("step_run", "model")
