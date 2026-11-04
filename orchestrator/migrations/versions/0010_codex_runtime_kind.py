"""`codex` joins the runtime-kind vocabulary (tech-stack decision 34, stage 2)

Both `runtime_kind` CHECKs widen to permit `'codex'`: `ck_agent_runtime_kind` (created by
`0001_initial.py`) and `ck_step_run_runtime_kind` (created by `0003_runtime_kinds.py`). The
declared vocabulary deliberately still excludes the development-only executing kinds
(`local_claude`, `codex_local`) — a manifest declares what substrate an agent is written FOR,
and which adapter executes is deployment configuration recorded only in the `agent_spawned`
event (`worker/context.py`).

Hand-written, both directions, like its predecessors (tech-stack.md decision 2). Renumbered
twice while the branch waited: an id is only free until main merges one, and two revisions
sharing an id leave `alembic_version` unable to say which one actually ran.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDENED = "runtime_kind IN ('scion', 'claude_managed', 'codex')"
_NARROW = "runtime_kind IN ('scion', 'claude_managed')"


def upgrade() -> None:
    # `op.f(...)` marks the names as already-final — 0003's own double-prefixing lesson.
    op.drop_constraint(op.f("ck_agent_runtime_kind"), "agent", type_="check")
    op.create_check_constraint(op.f("ck_agent_runtime_kind"), "agent", _WIDENED)
    op.drop_constraint(op.f("ck_step_run_runtime_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_runtime_kind"), "step_run", _WIDENED)


def downgrade() -> None:
    # Re-narrowing does NOT silently drop a `'codex'` row: Postgres validates every existing row
    # when a CHECK is added, so `create_check_constraint` atomically blocks the whole downgrade
    # the moment one exists (0003's documented behaviour, same mechanism). The round-trip test
    # only exercises this against an otherwise-empty schema.
    op.drop_constraint(op.f("ck_step_run_runtime_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_runtime_kind"), "step_run", _NARROW)
    op.drop_constraint(op.f("ck_agent_runtime_kind"), "agent", type_="check")
    op.create_check_constraint(op.f("ck_agent_runtime_kind"), "agent", _NARROW)
