"""Separate panel accounts, expiring sessions and shared login throttles."""

import sqlalchemy as sa
from alembic import op

revision = "0004_panel"
down_revision = "0003_reply_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(64), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "role IN ('moderator', 'organizer', 'admin')", name="ck_panel_role"
        ),
    )
    op.create_table(
        "panel_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "account_id", sa.Uuid(), sa.ForeignKey("panel_accounts.id"), nullable=False
        ),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "panel_login_throttle",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("panel_login_throttle")
    op.drop_table("panel_sessions")
    op.drop_table("panel_accounts")
