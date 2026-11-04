"""`secret` records how a run is supplied each project secret.

`sandbox_delivery` is `env` (the agent's environment, every existing row) or `header` (a cloud
sandbox's egress proxy sets `egress_header` on `egress_host`). Tech-stack decision 75.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROUTE = (
    "(sandbox_delivery = 'header' AND egress_host IS NOT NULL AND egress_header IS NOT NULL) "
    "OR (sandbox_delivery = 'env' AND egress_host IS NULL AND egress_header IS NULL)"
)


def upgrade() -> None:
    op.add_column(
        "secret",
        sa.Column("sandbox_delivery", sa.Text(), server_default=sa.text("'env'"), nullable=False),
    )
    op.add_column("secret", sa.Column("egress_host", sa.Text(), nullable=True))
    op.add_column("secret", sa.Column("egress_header", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_secret_sandbox_delivery"), "secret", "sandbox_delivery IN ('header', 'env')"
    )
    op.create_check_constraint(op.f("ck_secret_egress_route"), "secret", _ROUTE)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_secret_egress_route"), "secret", type_="check")
    op.drop_constraint(op.f("ck_secret_sandbox_delivery"), "secret", type_="check")
    op.drop_column("secret", "egress_header")
    op.drop_column("secret", "egress_host")
    op.drop_column("secret", "sandbox_delivery")
