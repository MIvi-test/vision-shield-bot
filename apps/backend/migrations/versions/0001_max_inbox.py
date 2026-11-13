"""Durable MAX inbox and polling cursors, independent of consent policy."""

import sqlalchemy as sa
from alembic import op

revision = "0001_max_inbox"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "max_polling_cursors",
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), primary_key=True),
        sa.Column("marker", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("marker >= 0", name="ck_cursor_marker"),
    )
    op.create_table(
        "max_inbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("update_type", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(256), nullable=False),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("has_photo", sa.Boolean(), nullable=False),
        sa.Column("reply_text", sa.Text()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id", "bot_id", "update_type", "message_id", name="uq_max_event"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'unknown', 'ignored')",
            name="ck_inbox_state",
        ),
    )
    op.create_index(
        "ix_inbox_pending",
        "max_inbox_events",
        ["source_id", "bot_id", "state", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_pending", table_name="max_inbox_events")
    op.drop_table("max_inbox_events")
    op.drop_table("max_polling_cursors")
