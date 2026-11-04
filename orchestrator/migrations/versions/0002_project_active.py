"""project.active — the admission switch

A project is configured in several steps (repo, ticket source, process version, secrets), and
until all of them are done it must not pull issues into a process. `active` is the operator's
explicit "go" and the only thing `worker.admission.admit_all` looks at beyond the ticket source.

Default `false`, deliberately: a project created by a half-finished wizard, or by a future
importer, is inert until someone says otherwise. Existing rows are backfilled to `true` — they
predate the switch and were already being admitted, and flipping them off would silently stop a
running pipeline.

Hand-written, like 0001 (tech-stack.md decision 2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Rows that existed before the switch were already being admitted; see the docstring.
    op.execute("UPDATE project SET active = true")


def downgrade() -> None:
    op.drop_column("project", "active")
