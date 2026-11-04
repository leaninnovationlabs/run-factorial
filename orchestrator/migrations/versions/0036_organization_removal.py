"""Who was removed from an organization, or left it (design.md §7.19)

One table, `organization_removal`, keyed by organization and email. Removing a member still
deletes the `organization_member` row; this row is what keeps the allowed-domain SSO join from
adding the person back on their next sign-in. Accepting an invite, or `grant-owner`, deletes it.
No existing column changes.

The downgrade refuses while any removal exists: the older image would add every removed person
in an allowed domain back to `default` the next time they sign in.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_removal",
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("removed_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_organization_removal_email_lowercase")
        ),
        sa.CheckConstraint(
            "reason IN ('removed', 'left')", name=op.f("ck_organization_removal_reason")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_organization_removal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", "email", name=op.f("pk_organization_removal")),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM organization_removal) THEN RAISE EXCEPTION "
        "'revision 0036 cannot be downgraded while a removal exists: the older image would add "
        "every removed person in an allowed domain back to default'; END IF; END $$"
    )
    op.drop_table("organization_removal")
