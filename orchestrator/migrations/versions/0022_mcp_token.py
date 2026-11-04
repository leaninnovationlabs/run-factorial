"""The operator MCP server's tokens, and a partial index for closures per project

`mcp_token` is the authority for every request on `/mcp/` (design.md §7.16): a JWT's signature
only finds its row, and the row decides principal, scopes, expiry and revocation. The JWT carries
the raw `jti` and the row keeps its sha256, so a database read plus the API token cannot re-sign
a live token. Rows are never deleted and are not events: `event.project_id` is NOT NULL and a
token belongs to no project. No `updated_at`, so no trigger: a revocation and the last-use stamp
each carry a timestamp of their own.

`event_closure_idx` serves the delivery metrics, which count `ticket_done` and `ticket_exited`
per project and week. `event_project_idx` is `(project_id, id)` and would scan every event the
project has. Partial, like 0021's lifecycle indexes.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLOSURES = "type IN ('ticket_done', 'ticket_exited')"


def upgrade() -> None:
    op.create_table(
        "mcp_token",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("jti_sha256", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("minted_via", sa.Text(), nullable=False),
        sa.Column("access", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_client", sa.Text(), nullable=True),
        sa.Column("last_used_ip", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "minted_via IN ('session', 'api_token')", name=op.f("ck_mcp_token_minted_via")
        ),
        sa.CheckConstraint("access IN ('read', 'write')", name=op.f("ck_mcp_token_access")),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name=op.f("ck_mcp_token_revoked")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_token")),
        sa.UniqueConstraint("jti_sha256", name=op.f("uq_mcp_token_jti_sha256")),
    )
    op.create_index(
        "event_closure_idx",
        "event",
        ["project_id", "created_at"],
        postgresql_where=sa.text(_CLOSURES),
    )


def downgrade() -> None:
    op.drop_index("event_closure_idx", table_name="event")
    op.drop_table("mcp_token")
