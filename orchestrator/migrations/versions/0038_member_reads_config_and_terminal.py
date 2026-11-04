"""A member reads project configuration and opens a run's terminal (design.md §7.19)

`projects:read` joins the catalogue, cut out of `projects:manage`: a project's settings, secret
names, readiness, processes and agents, read but never changed. Every built-in role holds it,
because the people who work a ticket need to read the process and the prompts that run it. The
member role also gains `tickets:terminal`: a member answers an agent where it is waiting, as
every signed-in person did before roles. The terminal still shows the secrets its session holds,
and on a host runtime its writer seat is a shell as `factorial` (design.md §7.14), so it stays a
permission of its own that a narrower role can leave out.

The seed is spelled out here rather than imported, as in 0035 and 0037: a revision must mean the
same thing for ever. No table or column changes.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW: Final = (
    (
        "projects:read",
        "Read a project's settings, secret names, readiness, processes and agents",
    ),
)

#: (role, permission) grants this revision adds.
_GRANTS: Final = (
    ("owner", "projects:read"),
    ("admin", "projects:read"),
    ("member", "projects:read"),
    ("member", "tickets:terminal"),
)

#: (role, description after, description before)
_ROLE_DESCRIPTIONS: Final = (
    (
        "member",
        "Work with tickets and their terminals, and read project configuration",
        "Work with tickets",
    ),
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


def _describe_role(bind: sa.Connection, key: str, description: str) -> None:
    bind.execute(
        sa.text(
            "UPDATE role SET description = :description "
            "WHERE key = :key AND organization_id IS NULL"
        ),
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
    for key, after, _ in _ROLE_DESCRIPTIONS:
        _describe_role(bind, key, after)


def downgrade() -> None:
    bind = op.get_bind()
    for key, _, before in _ROLE_DESCRIPTIONS:
        _describe_role(bind, key, before)
    for role, permission in _GRANTS:
        _revoke(bind, role, permission)
    for key, _ in _NEW:
        bind.execute(sa.text("DELETE FROM permission WHERE key = :key"), {"key": key})
