"""process_version.agent_pins — the shas a snapshot's agents were read from

A ticket pins its process config at admission and keeps it until done (design.md §2.8), but
`process_version.content` is the process YAML alone. Agent definitions stayed in the repo and were
resolved live through one project-wide `agent` row per name, whose `pinned_commit_sha` every
registration rewrote. So a sync of anything moved the manifests, prompts and skills under every
ticket already in flight: its steps were frozen, the agent executing them was not.

This column records, per snapshot, where each referenced agent was read from — `{"<name>":
{"sha": ..., "path": ...}}`. The path travels with the sha because it is not derivable from the
name at an arbitrary commit: `resolve_dir` reads the config folder that commit actually had, so a
pin carrying only a sha would look for the folder today's HEAD has.

Nullable, with no backfill. Every row that exists when this lands resolves agents exactly as it
does now, so tickets mid-flight are undisturbed and the guarantee starts at the next sync.
Backfilling `source_commit_sha` into the map would be a lie for any row whose agents have since
been re-pinned — it would claim a freeze that never happened.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade drops the column; a version keeps its content and its sha either way, and resolution
falls back to the `agent` row it used before.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "process_version", sa.Column("agent_pins", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("process_version", "agent_pins")
