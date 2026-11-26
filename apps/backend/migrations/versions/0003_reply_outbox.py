"""Transactional reply outbox, including previously committed pending replies."""

import sqlalchemy as sa
from alembic import op

revision = "0003_reply_outbox"
down_revision = "0002_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reply_outbox",
        sa.Column(
            "event_id",
            sa.Uuid(),
            sa.ForeignKey("max_inbox_events.id"),
            primary_key=True,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "INSERT INTO reply_outbox (event_id) SELECT id FROM max_inbox_events WHERE state = 'pending'"
    )


def downgrade() -> None:
    op.drop_table("reply_outbox")
