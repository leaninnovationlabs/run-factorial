"""Partial indexes on `event` for the preview and inspect-shell lifecycles

The awake cap (`events.previews.awake_workspaces`) and the inspect pass (`worker.inspect`) read
these events deployment-wide, the pass on every poll interval. `event` has no index on `type`, so
without these each read is a scan of the whole log. A partial index holds only the few rows a
lifecycle writes, keyed as the reads group them: by run, latest first.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIEW = (
    "type IN ('preview_requested', 'preview_stop_requested', 'preview_started', "
    "'preview_failed', 'preview_stopped')"
)
_INSPECT = "type IN ('inspect_requested', 'inspect_started', 'inspect_parked')"


def upgrade() -> None:
    op.create_index(
        "event_preview_lifecycle_idx",
        "event",
        ["step_run_id", "id"],
        postgresql_where=sa.text(_PREVIEW),
    )
    op.create_index(
        "event_inspect_lifecycle_idx",
        "event",
        ["step_run_id", "id"],
        postgresql_where=sa.text(_INSPECT),
    )


def downgrade() -> None:
    op.drop_index("event_inspect_lifecycle_idx", table_name="event")
    op.drop_index("event_preview_lifecycle_idx", table_name="event")
