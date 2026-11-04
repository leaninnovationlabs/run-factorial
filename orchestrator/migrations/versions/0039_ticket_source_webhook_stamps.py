"""When a project's GitHub webhook last verified, and last failed (design.md §7)

Each project now holds its own webhook secret, so a delivery can reach the orchestrator and still
fail for one project and pass for another on the same App. `ticket_source` gains the two moments
the receiver stamps, so a person can see whether the webhook works without reading the events.
Both are nullable with no default: a project that never received a delivery has neither.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ticket_source", sa.Column("webhook_verified_at", sa.DateTime(timezone=True)))
    op.add_column("ticket_source", sa.Column("webhook_failed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("ticket_source", "webhook_failed_at")
    op.drop_column("ticket_source", "webhook_verified_at")
