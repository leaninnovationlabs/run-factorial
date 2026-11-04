"""process_version.source_ref — the ref a sync was asked for, beside the sha it resolved to

`source_commit_sha` records what was read; it cannot say what was asked for. A sha is not a
branch, and no cheap read turns it back into one: the same commit is reachable from any number of
refs, and the branch that carried it has usually moved on. So an operator who syncs from
`staging` sees a row that looks exactly like one synced from the default branch, and has to
retype the ref on every subsequent sync.

Nullable, with no backfill and no default. NULL means "the repo's default branch" — the sync
route's own spelling for an omitted ref — which is also the honest reading of every row written
before this column existed. Storing the requested ref verbatim rather than a resolved branch name
keeps this free of a GitHub round trip and keeps it true: it is a record of the request.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column; a version keeps its sha and its content either way.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("process_version", sa.Column("source_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("process_version", "source_ref")
