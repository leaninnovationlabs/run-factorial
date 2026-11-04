"""Settings are the admin's, and the terminal is a permission of its own (design.md §7.19)

Two permissions join the catalogue: `tickets:terminal`, cut out of `tickets:operate`, because a
run's terminal shows the secrets its session holds; and `mcp_tokens:manage`, to see and revoke
another person's MCP token, which `mcp_tokens:mint` no longer does. The owner and the admin hold
both, and the admin now holds `org:manage` too, so the admin role is everything but assigning
roles. The member role keeps the tickets and loses nothing else it held.

The seed is spelled out here rather than imported, as in 0035: a revision must mean the same
thing for ever. No table or column changes.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW: Final = (
    ("tickets:terminal", "Open a run's terminal, which shows the secrets its session holds"),
    ("mcp_tokens:manage", "See and revoke anyone's MCP token in the organization"),
)

#: (key, description after, description before)
_DESCRIPTIONS: Final = (
    (
        "tickets:operate",
        "Interrupt, resume, restart, requeue and exit tickets",
        "Interrupt, resume, restart, requeue and exit tickets; open a terminal",
    ),
    (
        "mcp_tokens:mint",
        "Mint MCP tokens, and see and revoke your own",
        "Mint MCP tokens, and revoke any of the organization's",
    ),
)

#: (role, description after, description before)
_ROLE_DESCRIPTIONS: Final = (
    (
        "admin",
        "Everything but assigning roles",
        "Everything but roles and the organization's settings",
    ),
    ("member", "Work with tickets", "Work with projects and tickets"),
)

#: (role, permission) grants this revision adds.
_GRANTS: Final = (
    ("owner", "tickets:terminal"),
    ("owner", "mcp_tokens:manage"),
    ("admin", "tickets:terminal"),
    ("admin", "mcp_tokens:manage"),
    ("admin", "org:manage"),
)


def _grant(bind: sa.Connection, role: str, permission: str) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO role_permission (role_id, permission_key) "
            "SELECT id, :key FROM role WHERE key = :role AND organization_id IS NULL"
        ),
        {"role": role, "key": permission},
    )


def _revoke(bind: sa.Connection, role: str, permission: str) -> None:
    bind.execute(
        sa.text(
            "DELETE FROM role_permission WHERE permission_key = :key AND role_id = "
            "(SELECT id FROM role WHERE key = :role AND organization_id IS NULL)"
        ),
        {"role": role, "key": permission},
    )


def _describe(bind: sa.Connection, table: str, key: str, description: str) -> None:
    where = "key = :key" + (" AND organization_id IS NULL" if table == "role" else "")
    bind.execute(
        sa.text(f"UPDATE {table} SET description = :description WHERE {where}"),
        {"key": key, "description": description},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for key, description in _NEW:
        module, action = key.split(":")
        bind.execute(
            sa.text(
                "INSERT INTO permission (key, module, action, description) "
                "VALUES (:key, :module, :action, :description)"
            ),
            {"key": key, "module": module, "action": action, "description": description},
        )
    for role, permission in _GRANTS:
        _grant(bind, role, permission)
    for key, after, _ in _DESCRIPTIONS:
        _describe(bind, "permission", key, after)
    for key, after, _ in _ROLE_DESCRIPTIONS:
        _describe(bind, "role", key, after)


def downgrade() -> None:
    bind = op.get_bind()
    for key, _, before in _ROLE_DESCRIPTIONS:
        _describe(bind, "role", key, before)
    for key, _, before in _DESCRIPTIONS:
        _describe(bind, "permission", key, before)
    for role, permission in _GRANTS:
        _revoke(bind, role, permission)
    for key, _ in _NEW:
        bind.execute(sa.text("DELETE FROM permission WHERE key = :key"), {"key": key})
