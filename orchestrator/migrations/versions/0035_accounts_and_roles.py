"""Accounts, membership, roles and invites — who may act in an organization (design.md §7.19)

Six tables. `app_user` is one account per lowercase email. `permission` holds one row per key of
`orchestrator.access.Permission`, and `role` the built-in `owner`, `admin` and `member`, each
with its keys in `role_permission`. `organization_member` puts a user in an organization as one
role, and `organization_invite` is a link for one email, kept as its token's SHA-256 only. No
existing column changes, and every table exists whatever `FACTORIAL_MULTI_ORG__ENABLED` says;
with the flag off only the seeded rows are filled.

The seed is spelled out here rather than imported from `orchestrator.access`: a revision must
mean the same thing for ever, and the enum will grow. A test compares the rows with the enum.

The downgrade refuses while any membership or invite exists: the older image has no membership,
and would show every organization to everyone who signs in.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS: Final = (
    ("org:read", "See the organization, its projects, tickets, runs, costs and factory board"),
    ("org:manage", "Rename the organization and edit its settings"),
    ("members:invite", "Invite people and revoke invites"),
    ("members:remove", "Remove a member"),
    ("members:assign_roles", "Change a member's role"),
    ("projects:manage", "Create, configure and activate projects, with their secrets and config"),
    ("tickets:operate", "Interrupt, resume, restart, requeue and exit tickets; open a terminal"),
    ("runtimes:manage", "Define runtimes and their vendor credentials"),
    ("assistant:use", "Chat with the assistant"),
    ("assistant:configure", "Set the assistant's settings, credential and daily budget"),
    ("mcp_tokens:mint", "Mint MCP tokens, and revoke any of the organization's"),
)

_MEMBER: Final = ("org:read", "tickets:operate", "assistant:use", "mcp_tokens:mint")
_ADMIN: Final = (
    *_MEMBER,
    "members:invite",
    "members:remove",
    "projects:manage",
    "runtimes:manage",
    "assistant:configure",
)

_ROLES: Final = (
    ("owner", "Owner", "Everything, including roles and the organization's settings",
     tuple(key for key, _ in _PERMISSIONS)),
    ("admin", "Admin", "Everything but roles and the organization's settings", _ADMIN),
    ("member", "Member", "Work with projects and tickets", _MEMBER),
)

_MUTABLE: Final = ("app_user", "role", "organization_member")


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated_at() -> sa.Column[object]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _id() -> sa.Column[object]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "app_user",
        _id(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_sign_ins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_app_user_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
    )

    op.create_table(
        "permission",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("module", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_permission")),
    )

    op.create_table(
        "role",
        _id(),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("built_in", sa.Boolean(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "built_in = (organization_id IS NULL)", name=op.f("ck_role_built_in")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_role_organization_id_organization"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role")),
        sa.UniqueConstraint("organization_id", "key", name=op.f("uq_role_organization_id_key")),
    )
    op.create_index(
        "role_built_in_key_idx",
        "role",
        ["key"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )

    op.create_table(
        "role_permission",
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("permission_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_key"],
            ["permission.key"],
            name=op.f("fk_role_permission_permission_key_permission"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name=op.f("fk_role_permission_role_id_role"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_key", name=op.f("pk_role_permission")),
    )

    op.create_table(
        "organization_member",
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_organization_member_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name=op.f("fk_organization_member_role_id_role"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_organization_member_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "user_id", name=op.f("pk_organization_member")
        ),
    )
    op.create_index("organization_member_user_idx", "organization_member", ["user_id"])
    op.create_index(
        op.f("ix_organization_member_role_id"), "organization_member", ["role_id"]
    )

    op.create_table(
        "organization_invite",
        _id(),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_organization_invite_email_lowercase")
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name=op.f("ck_organization_invite_revoked"),
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_organization_invite_settled_once"),
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by"],
            ["app_user.id"],
            name=op.f("fk_organization_invite_accepted_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_organization_invite_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name=op.f("fk_organization_invite_role_id_role"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_invite")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_organization_invite_token_hash")),
    )
    op.create_index(
        "organization_invite_pending_idx",
        "organization_invite",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        op.f("ix_organization_invite_role_id"), "organization_invite", ["role_id"]
    )

    for table in _MUTABLE:
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    bind = op.get_bind()
    for key, description in _PERMISSIONS:
        module, action = key.split(":")
        bind.execute(
            sa.text(
                "INSERT INTO permission (key, module, action, description) "
                "VALUES (:key, :module, :action, :description)"
            ),
            {"key": key, "module": module, "action": action, "description": description},
        )
    for key, name, description, granted in _ROLES:
        role_id = bind.execute(
            sa.text(
                "INSERT INTO role (key, name, description, built_in) "
                "VALUES (:key, :name, :description, true) RETURNING id"
            ),
            {"key": key, "name": name, "description": description},
        ).scalar_one()
        for permission in granted:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permission (role_id, permission_key) VALUES (:role, :key)"
                ),
                {"role": role_id, "key": permission},
            )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM organization_member) "
        "OR EXISTS (SELECT 1 FROM organization_invite) THEN RAISE EXCEPTION "
        "'revision 0035 cannot be downgraded while a membership or an invite exists: the older "
        "image has no membership and would show every organization to everyone'; END IF; END $$"
    )
    op.drop_table("organization_invite")
    op.drop_table("organization_member")
    op.drop_table("role_permission")
    op.drop_table("role")
    op.drop_table("permission")
    op.drop_table("app_user")
