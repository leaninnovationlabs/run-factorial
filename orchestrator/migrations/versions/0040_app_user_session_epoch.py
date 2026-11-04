"""app_user.session_epoch: the sessions an account has not revoked (design.md §7.19)

A session cookie is signed, so nothing about it could be revoked: a stolen one kept working
until it expired, after its owner signed out and after a password change. Each cookie now
carries the account's epoch at the time it was minted, and a cookie whose epoch is not the
account's is refused. Signing out everywhere and setting a password bump it. Existing rows
start at 0.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("app_user", "session_epoch")
