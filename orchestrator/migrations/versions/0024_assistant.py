"""The assistant's tables: settings, credential, conversations, turns, items, proposals, spend

`app_setting` and `app_credential` are the deployment's own settings and secrets (the assistant's
backend, model, budgets and credential), keyed by name rather than by project or runtime.
`chat_conversation` and the tables under it hold the chat. Every item write moves the item's
`rev` through the `chat_item_bump_rev` trigger, so no writer can forget to bump it. The trigger
takes the conversation's row lock before it draws the `rev`, so one conversation's writes commit
in `rev` order and a stream that has read up to a `rev` has seen every item below it. A
transaction that also writes the conversation's turns, tokens or proposals takes that lock first,
so two of them queue on it instead of deadlocking. `chat_turn` allows one queued and one running
turn per conversation by two partial unique indexes. An item insert touches its conversation
through `chat_item_touch_conversation`; retention counts from `chat_conversation.updated_at`.

`chat_spend` is each person's estimated spend per UTC day, kept for ever: the
`chat_turn_record_spend` trigger adds every change of a turn's `cost_usd` to its owner's day, so
deleting a conversation cannot give back the daily budget its turns used.

`mcp_token` gains the `chat` access and mint route, and `chat_turn_id`, which is SET NULL so a
token row outlives its conversation. The downgrade deletes `access = 'chat'` rows first: those
tokens last one turn and are worthless once the tables are gone.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MUTABLE_TABLES = (
    "app_setting",
    "app_credential",
    "chat_conversation",
    "chat_turn",
    "chat_item",
    "chat_proposal",
)

_TURN_STATUSES = "'queued', 'running', 'done', 'failed', 'cancelled', 'interrupted'"
_ITEM_KINDS = (
    "'user_text', 'assistant_text', 'tool', 'proposal', 'show_me', 'chart', 'editor_draft', "
    "'notice'"
)
_PROPOSAL_STATUSES = "'pending', 'approved', 'succeeded', 'refused', 'declined', 'expired'"


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
        "app_setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _updated_at(),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_setting")),
    )
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
    op.create_table(
        "chat_conversation",
        _id(),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("backend_kind", sa.Text(), nullable=False),
        sa.Column("backend_session_id", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_conversation")),
    )
    op.create_index(
        "chat_conversation_owner_idx", "chat_conversation", ["owner", sa.text("updated_at DESC")]
    )

    op.create_table(
        "chat_turn",
        _id(),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(f"status IN ({_TURN_STATUSES})", name=op.f("ck_chat_turn_status")),
        sa.CheckConstraint("trigger IN ('user', 'approval')", name=op.f("ck_chat_turn_trigger")),
        sa.CheckConstraint(
            "cost_source IN ('reported', 'estimated')", name=op.f("ck_chat_turn_cost_source")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversation.id"],
            name=op.f("fk_chat_turn_conversation_id_chat_conversation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_turn")),
        sa.UniqueConstraint(
            "conversation_id", "seq", name=op.f("uq_chat_turn_conversation_id_seq")
        ),
    )
    for name, status in (("chat_turn_one_queued", "queued"), ("chat_turn_one_running", "running")):
        op.create_index(
            name,
            "chat_turn",
            ["conversation_id"],
            unique=True,
            postgresql_where=sa.text(f"status = '{status}'"),
        )
    op.create_index(
        "chat_turn_queue_idx",
        "chat_turn",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    op.execute("CREATE SEQUENCE chat_item_rev")
    op.create_table(
        "chat_item",
        _id(),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("final", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("rev", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(f"kind IN ({_ITEM_KINDS})", name=op.f("ck_chat_item_kind")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversation.id"],
            name=op.f("fk_chat_item_conversation_id_chat_conversation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["chat_turn.id"],
            name=op.f("fk_chat_item_turn_id_chat_turn"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_item")),
    )
    op.create_index("chat_item_conversation_rev_idx", "chat_item", ["conversation_id", "rev"])
    op.execute(
        "CREATE FUNCTION chat_item_bump_rev() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN "
        "PERFORM 1 FROM chat_conversation WHERE id = NEW.conversation_id FOR NO KEY UPDATE; "
        "NEW.rev := nextval('chat_item_rev'); RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER chat_item_bump_rev BEFORE INSERT OR UPDATE ON chat_item "
        "FOR EACH ROW EXECUTE FUNCTION chat_item_bump_rev()"
    )

    op.execute(
        "CREATE FUNCTION chat_item_touch_conversation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN UPDATE chat_conversation SET updated_at = now() WHERE id = NEW.conversation_id; "
        "RETURN NULL; END $$"
    )
    op.execute(
        "CREATE TRIGGER chat_item_touch_conversation AFTER INSERT ON chat_item "
        "FOR EACH ROW EXECUTE FUNCTION chat_item_touch_conversation()"
    )

    op.create_table(
        "chat_proposal",
        _id(),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.BigInteger(), nullable=True),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mcp_token_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            f"status IN ({_PROPOSAL_STATUSES})", name=op.f("ck_chat_proposal_status")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversation.id"],
            name=op.f("fk_chat_proposal_conversation_id_chat_conversation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["chat_turn.id"],
            name=op.f("fk_chat_proposal_turn_id_chat_turn"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["chat_item.id"],
            name=op.f("fk_chat_proposal_item_id_chat_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_token_id"], ["mcp_token.id"], name=op.f("fk_chat_proposal_mcp_token_id_mcp_token")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_proposal")),
    )

    op.create_table(
        "chat_backend_state",
        _id(),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("entry", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversation.id"],
            name=op.f("fk_chat_backend_state_conversation_id_chat_conversation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_backend_state")),
        sa.UniqueConstraint(
            "conversation_id",
            "key",
            "seq",
            name=op.f("uq_chat_backend_state_conversation_id_key_seq"),
        ),
    )

    op.create_table(
        "chat_spend",
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("owner", "day", name=op.f("pk_chat_spend")),
    )
    op.execute(
        "CREATE FUNCTION chat_turn_record_spend() RETURNS trigger LANGUAGE plpgsql AS $$ "
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
    op.execute(
        "CREATE TRIGGER chat_turn_record_spend AFTER INSERT OR UPDATE OF cost_usd ON chat_turn "
        "FOR EACH ROW EXECUTE FUNCTION chat_turn_record_spend()"
    )

    op.drop_constraint(op.f("ck_mcp_token_minted_via"), "mcp_token", type_="check")
    op.create_check_constraint(
        op.f("ck_mcp_token_minted_via"),
        "mcp_token",
        "minted_via IN ('session', 'api_token', 'chat')",
    )
    op.drop_constraint(op.f("ck_mcp_token_access"), "mcp_token", type_="check")
    op.create_check_constraint(
        op.f("ck_mcp_token_access"), "mcp_token", "access IN ('read', 'write', 'chat')"
    )
    op.add_column("mcp_token", sa.Column("chat_turn_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_mcp_token_chat_turn_id_chat_turn"),
        "mcp_token",
        "chat_turn",
        ["chat_turn_id"],
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

    op.drop_constraint(op.f("fk_mcp_token_chat_turn_id_chat_turn"), "mcp_token", type_="foreignkey")
    op.drop_column("mcp_token", "chat_turn_id")
    op.drop_table("chat_proposal")
    op.execute("DELETE FROM mcp_token WHERE access = 'chat' OR minted_via = 'chat'")
    op.drop_constraint(op.f("ck_mcp_token_access"), "mcp_token", type_="check")
    op.create_check_constraint(
        op.f("ck_mcp_token_access"), "mcp_token", "access IN ('read', 'write')"
    )
    op.drop_constraint(op.f("ck_mcp_token_minted_via"), "mcp_token", type_="check")
    op.create_check_constraint(
        op.f("ck_mcp_token_minted_via"), "mcp_token", "minted_via IN ('session', 'api_token')"
    )

    op.execute("DROP TRIGGER IF EXISTS chat_turn_record_spend ON chat_turn")
    op.execute("DROP FUNCTION chat_turn_record_spend()")
    op.drop_table("chat_spend")
    op.drop_table("chat_backend_state")
    op.execute("DROP TRIGGER IF EXISTS chat_item_touch_conversation ON chat_item")
    op.execute("DROP FUNCTION chat_item_touch_conversation()")
    op.execute("DROP TRIGGER IF EXISTS chat_item_bump_rev ON chat_item")
    op.drop_table("chat_item")
    op.execute("DROP FUNCTION chat_item_bump_rev()")
    op.execute("DROP SEQUENCE chat_item_rev")
    op.drop_table("chat_turn")
    op.drop_table("chat_conversation")
    op.drop_table("app_credential")
    op.drop_table("app_setting")
