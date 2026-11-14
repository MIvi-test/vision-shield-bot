"""PostgreSQL schema for MAX intake and Sprint 1 submissions."""

import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PollingCursor(Base):
    __tablename__ = "max_polling_cursors"
    __table_args__ = (CheckConstraint("marker >= 0", name="ck_cursor_marker"),)

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    marker: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InboxEvent(Base):
    __tablename__ = "max_inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "bot_id", "update_type", "message_id", name="uq_max_event"
        ),
        CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'unknown', 'ignored')",
            name="ck_inbox_state",
        ),
        Index("ix_inbox_pending", "source_id", "bot_id", "state", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    has_photo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reply_text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sender_id: Mapped[int | None] = mapped_column(BigInteger)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("source_id", "bot_id", "max_user_id", name="uq_max_user"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_ref: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CampaignRules(Base):
    __tablename__ = "campaign_rules"

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id"), primary_key=True
    )
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ConsentPrompt(Base):
    __tablename__ = "consent_prompts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "rules_version"],
            ["campaign_rules.campaign_id", "campaign_rules.version"],
        ),
        Index("ix_consent_prompt", "user_id", "campaign_id", "rules_version"),
    )

    source_inbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("max_inbox_events.id"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "rules_version"],
            ["campaign_rules.campaign_id", "campaign_rules.version"],
        ),
        UniqueConstraint("user_id", "campaign_id", "rules_version", name="uq_consent"),
        UniqueConstraint("id", "user_id", "campaign_id", name="uq_consent_owner"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_inbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("max_inbox_events.id"), unique=True, nullable=False
    )


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["consent_id", "user_id", "campaign_id"],
            ["consents.id", "consents.user_id", "consents.campaign_id"],
        ),
        CheckConstraint("version >= 1", name="ck_submission_version"),
        CheckConstraint(
            "status IN ('awaiting_consent', 'validating', 'moderating', "
            "'review_required', 'approved', 'rejected')",
            name="ck_submission_status",
        ),
        CheckConstraint(
            "status = 'awaiting_consent' OR consent_id IS NOT NULL",
            name="ck_submission_consent",
        ),
        Index("ix_submission_queue", "campaign_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False
    )
    consent_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_inbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("max_inbox_events.id"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("submission_id", "sequence", name="uq_audit_sequence"),
        CheckConstraint("sequence >= 1", name="ck_audit_sequence"),
        CheckConstraint(
            "actor IN ('system', 'user', 'moderator')", name="ck_audit_actor"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_inbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("max_inbox_events.id"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)


def database_engine():
    url = os.environ.get("BACKEND_DATABASE_URL")
    if not url:
        raise ValueError("Задайте BACKEND_DATABASE_URL для PostgreSQL")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("Режим с БД требует PostgreSQL")
    return engine
