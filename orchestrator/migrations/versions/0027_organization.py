"""organization — the tenant root, seeded with `default`, and the assistant's settings per tenant

An organization owns what a tenant brings and what its people act through (design.md §2.11):
projects, runtimes, MCP tokens, assistant conversations, and the assistant's settings,
credential and daily budget. Everything a project owns is reached through `project_id`, so no
other table gains a column. The upgrade seeds the one organization there is, `default`, and puts
every existing row in it, so a deployment upgrades with no operator step.

`project.name` and `runtime.slug` become unique per organization: two tenants may both want a
project called `web`. RESTRICT on the four new foreign keys: deleting an organization with
projects would cascade through every ticket and event.

The assistant's settings document moves from `app_setting` to `organization_setting`, for
`default`; `app_setting` keeps only the process's health. The credential moves to
`organization_credential`, then `app_credential` is dropped. Its ciphertext is bound to
`app:<name>`, so the move decrypts each value and encrypts it again under `org:<id>:<name>`,
with the master key the migrate step reads from the same environment as the API. With a row to
move and no master key the upgrade fails rather than lose the credential; a row the key cannot
open, which no reader could open either, is dropped and logged.

`chat_spend` is keyed `(organization_id, owner, day)`: a budget is an organization's, so spend in
one never uses another's. `chat_turn_record_spend` reads both from the turn's conversation.

The downgrade refuses while more than one organization exists: dropping the columns would merge
tenants' names, budgets and settings silently. With one, it copies the settings document and the
credential back, the credential encrypted again under `app:<name>`, and restores the global
uniques.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-17
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from orchestrator.secrets.crypto import DecryptionError, EncryptedValue, decrypt, encrypt

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT = "(SELECT id FROM organization WHERE slug = 'default')"
#: The tables that gain an owner; `chat_spend` re-keys separately, its column is in the key.
_OWNED = ("project", "runtime", "mcp_token", "chat_conversation")
_NEW_MUTABLE = ("organization", "organization_setting", "organization_credential")

_SPEND_BY_ORGANIZATION = (
    "CREATE OR REPLACE FUNCTION chat_turn_record_spend() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "DECLARE delta numeric; "
    "BEGIN "
    "delta := NEW.cost_usd - coalesce(OLD.cost_usd, 0); "
    "IF delta IS NULL OR delta = 0 THEN RETURN NULL; END IF; "
    "INSERT INTO chat_spend (organization_id, owner, day, cost_usd) "
    "SELECT c.organization_id, c.owner, "
    "(coalesce(NEW.started_at, now()) AT TIME ZONE 'UTC')::date, delta "
    "FROM chat_conversation c WHERE c.id = NEW.conversation_id "
    "ON CONFLICT (organization_id, owner, day) "
    "DO UPDATE SET cost_usd = chat_spend.cost_usd + EXCLUDED.cost_usd; "
    "RETURN NULL; END $$"
)
#: Revision 0024's body, verbatim.
_SPEND_BY_OWNER = (
    "CREATE OR REPLACE FUNCTION chat_turn_record_spend() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "DECLARE delta numeric; "
    "BEGIN "
    "delta := NEW.cost_usd - coalesce(OLD.cost_usd, 0); "
    "IF delta IS NULL OR delta = 0 THEN RETURN NULL; END IF; "
    "INSERT INTO chat_spend (owner, day, cost_usd) "
    "SELECT c.owner, (coalesce(NEW.started_at, now()) AT TIME ZONE 'UTC')::date, delta "
    "FROM chat_conversation c WHERE c.id = NEW.conversation_id "
    "ON CONFLICT (owner, day) "
    "DO UPDATE SET cost_usd = chat_spend.cost_usd + EXCLUDED.cost_usd; "
    "RETURN NULL; END $$"
)


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


def _owned_by(table: str, *, ondelete: str = "RESTRICT") -> None:
    op.create_foreign_key(
        op.f(f"fk_{table}_organization_id_organization"),
        table,
        "organization",
        ["organization_id"],
        ["id"],
        ondelete=ondelete,
    )


def _move_credentials(source: str, target: str, organization_id: int, *, to_org: bool) -> None:
    """Copy each credential from `source` to `target`, decrypted under one table's AAD and
    encrypted again under the other's: `app:<name>` and `org:<organization_id>:<name>`."""
    app = "app:{}"
    org = f"org:{organization_id}:{{}}"
    old, new = (app, org) if to_org else (org, app)
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT name, value_ciphertext, enc_key_id, updated_by, created_at, updated_at "
            f"FROM {source}"
        )
    ).all()
    for row in rows:
        try:
            plaintext = decrypt(
                EncryptedValue(row.value_ciphertext, row.enc_key_id),
                aad=old.format(row.name).encode(),
            )
        except DecryptionError:
            logging.getLogger("alembic.runtime.migration").warning(
                "dropping credential %r: the master key cannot open it", row.name
            )
            continue
        value = encrypt(plaintext.get_secret_value(), aad=new.format(row.name).encode())
        owner_column, owner = ("organization_id, ", f"{organization_id}, ") if to_org else ("", "")
        bind.execute(
            sa.text(
                f"INSERT INTO {target} ({owner_column}name, value_ciphertext, enc_key_id, "
                "updated_by, created_at, updated_at) "
                f"VALUES ({owner}:name, :ciphertext, :key_id, :by, :created, :updated)"
            ),
            {
                "name": row.name,
                "ciphertext": value.ciphertext,
                "key_id": value.enc_key_id,
                "by": row.updated_by,
                "created": row.created_at,
                "updated": row.updated_at,
            },
        )


def _add_owner(table: str, default_id: int) -> None:
    """Every existing row goes to `default` through a constant column default, not an UPDATE: an
    UPDATE fires `set_updated_at`, which would reset each conversation's retention clock and the
    history's order to the deploy's time."""
    op.add_column(
        table,
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            server_default=sa.text(str(default_id)),
            nullable=False,
        ),
    )
    op.alter_column(table, "organization_id", server_default=None)


def upgrade() -> None:
    op.create_table(
        "organization",
        _id(),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization")),
        sa.UniqueConstraint("slug", name=op.f("uq_organization_slug")),
    )
    op.execute(
        "INSERT INTO organization (slug, name, updated_by) VALUES ('default', 'Default', "
        "'migration')"
    )

    default_id = op.get_bind().execute(sa.text(f"SELECT {_DEFAULT}")).scalar_one()
    for table in _OWNED:
        _add_owner(table, default_id)
        _owned_by(table)

    op.drop_constraint(op.f("uq_project_name"), "project", type_="unique")
    op.create_unique_constraint(
        op.f("uq_project_organization_id_name"), "project", ["organization_id", "name"]
    )
    op.drop_constraint(op.f("uq_runtime_slug"), "runtime", type_="unique")
    op.create_unique_constraint(
        op.f("uq_runtime_organization_id_slug"), "runtime", ["organization_id", "slug"]
    )
    op.create_index(op.f("ix_mcp_token_organization_id"), "mcp_token", ["organization_id"])
    op.drop_index("chat_conversation_owner_idx", table_name="chat_conversation")
    op.create_index(
        "chat_conversation_owner_idx",
        "chat_conversation",
        ["organization_id", "owner", sa.text("updated_at DESC")],
    )

    op.create_table(
        "organization_setting",
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _updated_at(),
        sa.PrimaryKeyConstraint("organization_id", "key", name=op.f("pk_organization_setting")),
    )
    _owned_by("organization_setting", ondelete="CASCADE")
    op.create_table(
        "organization_credential",
        _id(),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enc_key_id", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_credential")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_organization_credential_organization_id_name")
        ),
    )
    _owned_by("organization_credential", ondelete="CASCADE")
    op.execute(
        "INSERT INTO organization_setting (organization_id, key, value, updated_by, updated_at) "
        f"SELECT {_DEFAULT}, key, value, updated_by, updated_at FROM app_setting "
        "WHERE key = 'assistant'"
    )
    op.execute("DELETE FROM app_setting WHERE key = 'assistant'")
    _move_credentials("app_credential", "organization_credential", default_id, to_org=True)
    op.drop_table("app_credential")

    _add_owner("chat_spend", default_id)
    _owned_by("chat_spend")
    op.drop_constraint(op.f("pk_chat_spend"), "chat_spend", type_="primary")
    op.create_primary_key(op.f("pk_chat_spend"), "chat_spend", ["organization_id", "owner", "day"])
    op.execute(_SPEND_BY_ORGANIZATION)

    for table in _NEW_MUTABLE:
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF (SELECT count(*) FROM organization) > 1 THEN RAISE EXCEPTION "
        "'revision 0027 cannot be downgraded while more than one organization exists: dropping "
        "organization_id would merge their projects, runtimes and budgets'; END IF; END $$"
    )

    op.execute(_SPEND_BY_OWNER)
    op.drop_constraint(op.f("pk_chat_spend"), "chat_spend", type_="primary")
    op.drop_constraint(
        op.f("fk_chat_spend_organization_id_organization"), "chat_spend", type_="foreignkey"
    )
    op.drop_column("chat_spend", "organization_id")
    op.create_primary_key(op.f("pk_chat_spend"), "chat_spend", ["owner", "day"])

    op.create_table(
        "app_credential",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enc_key_id", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_credential")),
        sa.UniqueConstraint("name", name=op.f("uq_app_credential_name")),
    )
    op.execute(
        "CREATE TRIGGER app_credential_set_updated_at BEFORE UPDATE ON app_credential "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "INSERT INTO app_setting (key, value, updated_by, updated_at) "
        "SELECT key, value, updated_by, updated_at FROM organization_setting "
        "WHERE key = 'assistant' "
        "ON CONFLICT (key) DO NOTHING"
    )
    default_id = op.get_bind().execute(sa.text(f"SELECT {_DEFAULT}")).scalar_one()
    _move_credentials("organization_credential", "app_credential", default_id, to_org=False)
    op.drop_table("organization_credential")
    op.drop_table("organization_setting")

    op.drop_index("chat_conversation_owner_idx", table_name="chat_conversation")
    op.create_index(
        "chat_conversation_owner_idx", "chat_conversation", ["owner", sa.text("updated_at DESC")]
    )
    op.drop_index(op.f("ix_mcp_token_organization_id"), table_name="mcp_token")
    op.drop_constraint(op.f("uq_runtime_organization_id_slug"), "runtime", type_="unique")
    op.create_unique_constraint(op.f("uq_runtime_slug"), "runtime", ["slug"])
    op.drop_constraint(op.f("uq_project_organization_id_name"), "project", type_="unique")
    op.create_unique_constraint(op.f("uq_project_name"), "project", ["name"])
    for table in _OWNED:
        op.drop_constraint(
            op.f(f"fk_{table}_organization_id_organization"), table, type_="foreignkey"
        )
        op.drop_column(table, "organization_id")

    op.drop_table("organization")
