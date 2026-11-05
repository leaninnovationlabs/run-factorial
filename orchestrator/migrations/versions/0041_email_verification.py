"""email_verification: links that prove a mailbox (design.md §7.19 item 16)

One table for both links the mailer sends: `verify`, after a password signup, and `reset`, to
set a new password. Only the token's SHA-256 is stored. A partial unique index keeps one live
token per account and purpose; a newer one sets the older one's `consumed_at`. No existing
column changes.

The downgrade drops the table. The older image never reads it, and the verifications it
recorded stay in `app_user.email_verified_at`.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_verification",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_email_verification_email_lowercase")
        ),
        sa.CheckConstraint(
            "purpose IN ('verify', 'reset')", name=op.f("ck_email_verification_purpose")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_email_verification_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_verification_token_hash")),
    )
    op.create_index(
        "email_verification_live_idx",
        "email_verification",
        ["user_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index(
        op.f("ix_email_verification_user_id"), "email_verification", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("email_verification")
