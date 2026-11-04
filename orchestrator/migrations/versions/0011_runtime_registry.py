"""The runtime registry: `runtime`, `runtime_credential`, and `step_run.runtime_id`

A product-level Runtime is operator-owned state a repo's `process.yaml` may only reference by
slug — design.md §7.5's L2 shape applied to substrates, the same trust direction as
`project.settings.mcp_allowlist`. `project.settings.agent_runtime` was documented for this and
never read; these tables replace that slot. `runtime.type` is TEXT + CHECK like every other
enumeration here (`RUNTIME_TYPES` in `db/models.py`); `details` is a per-type JSONB bag that
Pydantic validates at the boundary, empty for every type today.

`runtime_credential` is `secret`'s twin keyed by runtime. `step_run.runtime_id` is SET NULL on
delete so history outlives a deleted runtime; the API refuses the delete while a live run still
references it, which the database cannot know.

Both new tables are mutable, so each gets its own `set_updated_at` trigger — 0001 wired
`MUTABLE_TABLES` once, at creation, and nothing re-reads that tuple.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).
Downgrade drops the column before the tables it references; a `runtime_credential` row goes
with its runtime, and a `step_run` that named one simply forgets which — the deployment
default is what every run before this revision meant anyway.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MUTABLE_TABLES: tuple[str, ...] = ("runtime", "runtime_credential")


def upgrade() -> None:
    op.create_table(
        "runtime",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('codex_api', 'codex_subscription', 'claude_api', 'claude_subscription')",
            name=op.f("ck_runtime_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime")),
        sa.UniqueConstraint("slug", name=op.f("uq_runtime_slug")),
    )

    op.create_table(
        "runtime_credential",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("runtime_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enc_key_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["runtime_id"],
            ["runtime.id"],
            name=op.f("fk_runtime_credential_runtime_id_runtime"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_credential")),
        sa.UniqueConstraint(
            "runtime_id", "name", name=op.f("uq_runtime_credential_runtime_id_name")
        ),
    )

    op.add_column("step_run", sa.Column("runtime_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_step_run_runtime_id_runtime"),
        "step_run",
        "runtime",
        ["runtime_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for table in MUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in MUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON {table}")

    op.drop_constraint(op.f("fk_step_run_runtime_id_runtime"), "step_run", type_="foreignkey")
    op.drop_column("step_run", "runtime_id")
    op.drop_table("runtime_credential")
    op.drop_table("runtime")
