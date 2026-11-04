"""`claude` joins the runtime-kind vocabulary

Both `runtime_kind` CHECKs widen to permit `'claude'`, the Claude family's own name. Manifests
have declared `claude_managed` — the deferred Managed Agents adapter's name — because it was the
only Claude value the vocabulary had, and `local_claude` has executed them since the cutover.
A registry runtime (decision 36) makes a manifest name its family out loud, and that name
should not be a product nobody runs. `claude_managed` stays accepted for ever (decision 32).

Hand-written, both directions, like its predecessors.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDENED = "runtime_kind IN ('scion', 'claude_managed', 'codex', 'claude')"
_NARROW = "runtime_kind IN ('scion', 'claude_managed', 'codex')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_agent_runtime_kind"), "agent", type_="check")
    op.create_check_constraint(op.f("ck_agent_runtime_kind"), "agent", _WIDENED)
    op.drop_constraint(op.f("ck_step_run_runtime_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_runtime_kind"), "step_run", _WIDENED)


def downgrade() -> None:
    # Postgres validates every existing row when a CHECK is added, so a `'claude'` row blocks
    # the whole downgrade rather than being dropped silently (0003's documented behaviour).
    op.drop_constraint(op.f("ck_step_run_runtime_kind"), "step_run", type_="check")
    op.create_check_constraint(op.f("ck_step_run_runtime_kind"), "step_run", _NARROW)
    op.drop_constraint(op.f("ck_agent_runtime_kind"), "agent", type_="check")
    op.create_check_constraint(op.f("ck_agent_runtime_kind"), "agent", _NARROW)
