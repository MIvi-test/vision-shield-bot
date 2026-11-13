"""PostgreSQL schema for durable MAX intake; no submission policy here."""

import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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


def database_engine():
    url = os.environ.get("BACKEND_DATABASE_URL")
    if not url:
        raise ValueError("Задайте BACKEND_DATABASE_URL для PostgreSQL")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("Режим с БД требует PostgreSQL")
    return engine
