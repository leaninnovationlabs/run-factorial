"""A message to the assistant can carry files

`chat_attachment` holds the bytes of each file a person attached to a message: an image, or a
small text file (`assistant/attachments.py` decides which, from the bytes). The API writes a row
in the transaction that queues the message's turn, and the assistant's process reads it when it
runs the turn, which may be seconds or minutes later and in another process: that is why the
bytes are in the database and not on either process's disk. A row belongs to its message's item
and goes with it, so deleting a conversation, and the retention that does so, removes its files
with nothing more to remember.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_attachment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["chat_item.id"],
            name=op.f("fk_chat_attachment_item_id_chat_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_attachment")),
    )
    op.create_index("chat_attachment_item_idx", "chat_attachment", ["item_id"])


def downgrade() -> None:
    op.drop_index("chat_attachment_item_idx", table_name="chat_attachment")
    op.drop_table("chat_attachment")
