"""`codex_bedrock` joins the runtime-type vocabulary.

A Codex family runtime that authenticates with a short-lived Bedrock bearer the factory
host role mints. Existing types and rows are untouched. Downgrade refuses if any
`codex_bedrock` row exists.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "type IN ('codex_api', 'codex_subscription', 'claude_api', 'claude_subscription')"
_NEW = (
    "type IN ('codex_api', 'codex_subscription', 'codex_bedrock', "
    "'claude_api', 'claude_subscription')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_runtime_type"), "runtime", type_="check")
    op.create_check_constraint(op.f("ck_runtime_type"), "runtime", _NEW)


def downgrade() -> None:
    leftover = op.get_bind().execute(
        sa.text("SELECT count(*) FROM runtime WHERE type = 'codex_bedrock'")
    ).scalar_one()
    if leftover:
        raise RuntimeError(
            f"cannot narrow runtime.type: {leftover} codex_bedrock runtime(s) still exist"
        )
    op.drop_constraint(op.f("ck_runtime_type"), "runtime", type_="check")
    op.create_check_constraint(op.f("ck_runtime_type"), "runtime", _OLD)
