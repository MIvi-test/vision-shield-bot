"""Transactional event intake and conservative reply delivery tracking."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from maxapi.enums import ChatType
from maxapi.types import MessageCreated
from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.database import InboxEvent, PollingCursor
from backend.replies import choose_reply


@dataclass(frozen=True)
class ReceivedMessage:
    message_id: str
    chat_id: int | None
    text: str
    has_photo: bool
    reply_text: str | None


def normalize_message(raw_event: dict) -> ReceivedMessage:
    event = MessageCreated.model_validate(raw_event)
    message = event.message
    if message.body is None or not message.body.mid:
        raise ValueError("У события нет идентификатора сообщения")
    text = message.body.text or ""
    has_photo = any(a.type == "image" for a in message.body.attachments or [])
    can_reply = (
        message.recipient.chat_type == ChatType.DIALOG
        and message.recipient.chat_id is not None
        and message.sender is not None
        and not message.sender.is_bot
    )
    return ReceivedMessage(
        message_id=message.body.mid,
        chat_id=message.recipient.chat_id if can_reply else None,
        text=text if can_reply else "",
        has_photo=has_photo if can_reply else False,
        reply_text=choose_reply(text, has_photo=has_photo) if can_reply else None,
    )


@dataclass(frozen=True)
class PendingReply:
    event_id: UUID
    chat_id: int
    text: str


class InboxStore:
    def __init__(self, engine: Engine, source_id: str, bot_id: int):
        if not source_id or len(source_id) > 128:
            raise ValueError("MAX_SOURCE_ID должен содержать от 1 до 128 символов")
        self.engine = engine
        self.source_id = source_id
        self.bot_id = bot_id

    def marker(self) -> int:
        with Session(self.engine) as session, session.begin():
            session.execute(
                insert(PollingCursor)
                .values(source_id=self.source_id, bot_id=self.bot_id, marker=0)
                .on_conflict_do_nothing()
            )
            return session.execute(
                select(PollingCursor.marker).where(
                    PollingCursor.source_id == self.source_id,
                    PollingCursor.bot_id == self.bot_id,
                )
            ).scalar_one()

    def save_batch(
        self, marker: int, next_marker: int, messages: list[ReceivedMessage]
    ) -> None:
        if next_marker < marker:
            raise ValueError("Маркер не должен уменьшаться")
        with Session(self.engine) as session, session.begin():
            cursor = session.execute(
                select(PollingCursor)
                .where(
                    PollingCursor.source_id == self.source_id,
                    PollingCursor.bot_id == self.bot_id,
                )
                .with_for_update()
            ).scalar_one()
            if cursor.marker != marker:
                raise RuntimeError("Маркер изменён другим процессом; остановите дубль")
            for message in messages:
                session.execute(
                    insert(InboxEvent)
                    .values(
                        id=uuid4(),
                        source_id=self.source_id,
                        bot_id=self.bot_id,
                        update_type="message_created",
                        message_id=message.message_id,
                        chat_id=message.chat_id,
                        message_text=message.text,
                        has_photo=message.has_photo,
                        reply_text=message.reply_text,
                        state="pending"
                        if message.reply_text is not None
                        else "ignored",
                        received_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(constraint="uq_max_event")
                )
            cursor.marker = next_marker

    def claim_reply(self) -> PendingReply | None:
        with Session(self.engine) as session, session.begin():
            event = session.execute(
                select(InboxEvent)
                .where(
                    InboxEvent.source_id == self.source_id,
                    InboxEvent.bot_id == self.bot_id,
                    InboxEvent.state == "pending",
                )
                .order_by(InboxEvent.received_at, InboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if event is None:
                return None
            if event.chat_id is None or event.reply_text is None:
                raise RuntimeError("Неполные данные ответа в inbox")
            event.state = "sending"
            return PendingReply(event.id, event.chat_id, event.reply_text)

    def finish_reply(self, event_id: UUID, state: str) -> None:
        if state not in {"sent", "unknown"}:
            raise ValueError("Недопустимый результат отправки")
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(InboxEvent)
                .where(
                    InboxEvent.id == event_id,
                    InboxEvent.source_id == self.source_id,
                    InboxEvent.bot_id == self.bot_id,
                    InboxEvent.state == "sending",
                )
                .values(state=state)
            )

    def unresolved_count(self) -> int:
        from sqlalchemy import func

        with Session(self.engine) as session:
            return session.execute(
                select(func.count())
                .select_from(InboxEvent)
                .where(
                    InboxEvent.source_id == self.source_id,
                    InboxEvent.bot_id == self.bot_id,
                    InboxEvent.state.in_(["sending", "unknown"]),
                )
            ).scalar_one()
