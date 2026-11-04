"""step_run runtime kinds — `kind: action` joins the closed step-kind set, plus `runtime_kind`

Two independent, unrelated-in-cause-but-shipped-together changes (design.md §4):

(a) `ck_step_run_kind` widens to permit `'action'`. `0001_initial.py` only ever permitted
    `'agent'` / `'human_gate'`; `kind: action` (`config.models.ActionStep`) is a third step kind,
    dispatched to a registered action rather than an agent or a human gate.

(b) `step_run.runtime_kind` — nullable TEXT, CHECK-constrained to the same value set as
    `ck_agent_runtime_kind` (`0001_initial.py`). The design doc expects this column and 0001 never
    created it. Populated at the one write site that knows the value: an agent run's own resolved
    manifest (`worker.agent_steps._start_attempt`). A human-gate row or an action row leaves it
    NULL — neither is a manifest, and Postgres's three-valued CHECK logic already lets NULL pass
    regardless of the allowed-value list, the same way `Agent.runtime_kind` uses NULL for "no
    per-agent override".

Hand-written, both directions, like 0001 and 0002 (tech-stack.md decision 2).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `op.f(...)` marks the name as already-final: `drop_constraint`/`create_check_constraint`
    # otherwise re-run it through `Base.metadata`'s naming convention (db/base.py) and double-
    # prefix it (`ck_step_run_ck_step_run_kind`) — the same reason 0001 wraps every inline
    # constraint name in `op.f()`.
    op.drop_constraint(op.f("ck_step_run_kind"), "step_run", type_="check")
    op.create_check_constraint(
        op.f("ck_step_run_kind"), "step_run", "kind IN ('agent', 'human_gate', 'action')"
    )

    op.add_column("step_run", sa.Column("runtime_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_step_run_runtime_kind"),
        "step_run",
        "runtime_kind IN ('scion', 'claude_managed')",
    )


def downgrade() -> None:
    # Reverses (b) then (a) — the exact opposite order of upgrade(). The two are NOT symmetric
    # (verified live against Postgres, not assumed):
    #   - dropping `runtime_kind` silently drops data. `DROP COLUMN` never validates against the
    #     dropped column's own CHECK, so any non-NULL `runtime_kind` value is gone with no error —
    #     ordinary, silent data loss, same as 0001's and 0002's own precedent for a downgrade.
    #   - re-narrowing `ck_step_run_kind` to `('agent', 'human_gate')` does NOT silently drop an
    #     existing `kind = 'action'` row: Postgres validates every row against a CHECK constraint
    #     at the moment it is added (unless created `NOT VALID`, which this does not do), so
    #     `create_check_constraint` here ATOMICALLY BLOCKS the whole downgrade — it raises and the
    #     migration's transaction rolls back — the moment one such row exists. The round-trip test
    #     only ever exercises this against an otherwise-empty schema (per 0001's and 0002's own
    #     precedent — see test_migrations.py), so neither behavior is what that test proves; both
    #     are documented here from having actually forced each one.
    op.drop_constraint(op.f("ck_step_run_runtime_kind"), "step_run", type_="check")
    op.drop_column("step_run", "runtime_kind")

    op.drop_constraint(op.f("ck_step_run_kind"), "step_run", type_="check")
    op.create_check_constraint(
        op.f("ck_step_run_kind"), "step_run", "kind IN ('agent', 'human_gate')"
    )
