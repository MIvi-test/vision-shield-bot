"""Users, immutable campaign rules, consent evidence, submissions and status audit."""

import sqlalchemy as sa
from alembic import op

revision = "0002_submissions"
down_revision = "0001_max_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("max_inbox_events", sa.Column("sender_id", sa.BigInteger()))
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("author_ref", sa.String(40), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "bot_id", "max_user_id", name="uq_max_user"),
    )
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "campaign_rules",
        sa.Column(
            "campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), primary_key=True
        ),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "consent_prompts",
        sa.Column(
            "source_inbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("max_inbox_events.id"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id", "rules_version"],
            ["campaign_rules.campaign_id", "campaign_rules.version"],
        ),
    )
    op.create_index(
        "ix_consent_prompt",
        "consent_prompts",
        ["user_id", "campaign_id", "rules_version"],
    )
    op.create_table(
        "consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_inbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("max_inbox_events.id"),
            unique=True,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "rules_version"],
            ["campaign_rules.campaign_id", "campaign_rules.version"],
        ),
        sa.UniqueConstraint(
            "user_id", "campaign_id", "rules_version", name="uq_consent"
        ),
        sa.UniqueConstraint("id", "user_id", "campaign_id", name="uq_consent_owner"),
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), nullable=False
        ),
        sa.Column("consent_id", sa.Uuid()),
        sa.Column(
            "source_inbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("max_inbox_events.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["consent_id", "user_id", "campaign_id"],
            ["consents.id", "consents.user_id", "consents.campaign_id"],
        ),
        sa.CheckConstraint("version >= 1", name="ck_submission_version"),
        sa.CheckConstraint(
            "status IN ('awaiting_consent', 'validating', 'moderating', 'review_required', 'approved', 'rejected')",
            name="ck_submission_status",
        ),
        sa.CheckConstraint(
            "status = 'awaiting_consent' OR consent_id IS NOT NULL",
            name="ck_submission_consent",
        ),
    )
    op.create_index(
        "ix_submission_queue", "submissions", ["campaign_id", "created_at", "id"]
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "submission_id", sa.Uuid(), sa.ForeignKey("submissions.id"), nullable=False
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_inbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("max_inbox_events.id"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Uuid()),
        sa.UniqueConstraint("submission_id", "sequence", name="uq_audit_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_audit_sequence"),
        sa.CheckConstraint(
            "actor IN ('system', 'user', 'moderator')", name="ck_audit_actor"
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_index("ix_submission_queue", table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("consents")
    op.drop_index("ix_consent_prompt", table_name="consent_prompts")
    op.drop_table("consent_prompts")
    op.drop_table("campaign_rules")
    op.drop_table("campaigns")
    op.drop_table("users")
    op.drop_column("max_inbox_events", "sender_id")
