"""runtime.metadata — operator key/value notes on a registry row

A flat string-to-string map an operator edits as rows on the Runtime page: who owns the row,
which cost centre it bills to, whatever the deployment wants to remember about it. Not
`details`: that bag is per-type, `extra=forbid`, and the place an adapter may one day read a
key from. Nothing reads `metadata` but the API and the page, and nothing ever will, so a key
there is a label rather than a setting.

The SQL column is `metadata`; the SQLAlchemy attribute is `metadata_` because declarative
reserves the bare name for the registry.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column and its notes with it — there is nothing else they inform.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("runtime", "metadata")
