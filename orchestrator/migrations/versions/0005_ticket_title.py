"""ticket.title — the observer's one stored copy of issue text

Reverses the "no issue titles" decision (tech-stack.md 27). That decision
priced titles at a GitHub round trip per board row, but `worker.reconcile` already fetches the
whole `IssueState` every tick to evaluate predicates against it, so keeping the title costs one
column and no extra API call. Without it a board of hundreds of tickets can only be searched by
issue number.

Nullable, with no backfill and no default. NULL means "not observed yet", which is a different
fact from an issue whose title really is empty, and it is what lets the UI fall back to the bare
number honestly rather than printing a blank cell. Existing rows fill in on their next tick;
tickets already `done` never tick again and stay NULL, which is exactly the behavior those rows
have today. A backfill would need the round trip per ticket that this design avoids.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column: it is a cache of text GitHub owns, so nothing is lost that the next
tick would not re-read.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "title")
