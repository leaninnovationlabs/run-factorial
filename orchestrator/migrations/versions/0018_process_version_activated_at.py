"""process_version.activated_at — re-syncing a known commit makes it current again

"Current" was the newest row per lane, and a re-sync of a commit the project already holds finds
its row without writing one, so syncing back to an older commit left the newer snapshot current.
Every sync now stamps the row it lands on. Backfilled from `created_at`, which is what "current"
meant before this revision.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "process_version",
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("UPDATE process_version SET activated_at = created_at")


def downgrade() -> None:
    op.drop_column("process_version", "activated_at")
