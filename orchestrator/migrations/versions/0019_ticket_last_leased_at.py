"""ticket.last_leased_at — when a pass last claimed the ticket

A refresh asks the worker to reconcile one ticket now and waits for a claim later than the request.
No backfill: NULL means no claim since this revision, and the next tick fills it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Held locks on `ticket` are env.py's to wait out; this revision's own timeout failed its
    # first deploy.
    op.add_column("ticket", sa.Column("last_leased_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "last_leased_at")
