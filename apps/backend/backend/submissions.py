"""Sprint 1 domain operations. The caller owns the transaction."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.database import (
    AuditEvent,
    Campaign,
    CampaignRules,
    Consent,
    ConsentPrompt,
    InboxEvent,
    Submission,
    User,
)


def register_campaign(session: Session, slug: str, version: str, text: str) -> UUID:
    """Explicit administrative setup; a stored rules version is immutable."""
    if not slug or len(slug) > 64:
        raise ValueError("Slug кампании должен содержать от 1 до 64 символов")
    if not version or len(version) > 64 or any(c.isspace() for c in version):
        raise ValueError("Версия правил: 1–64 символа без пробелов")
    if not text.strip() or len(text) > 2400:
        raise ValueError("Текст правил должен содержать от 1 до 2400 символов")
    now = datetime.now(UTC)
    session.execute(
        insert(Campaign)
        .values(id=uuid4(), slug=slug, rules_version=version, created_at=now)
        .on_conflict_do_nothing(index_elements=[Campaign.slug])
    )
    campaign = session.execute(
        select(Campaign).where(Campaign.slug == slug).with_for_update()
    ).scalar_one()
    rules = session.get(CampaignRules, (campaign.id, version))
    if rules is not None and rules.text != text:
        raise ValueError(
            "Текст существующей версии правил нельзя менять; задайте новую"
        )
    if rules is None:
        session.add(
            CampaignRules(
                campaign_id=campaign.id, version=version, text=text, created_at=now
            )
        )
    campaign.rules_version = version
    session.flush()
    return campaign.id


def record_audit(
    session: Session,
    submission: Submission,
    event_id: UUID,
    previous: str | None,
    user_id: UUID,
    reason: str,
) -> None:
    session.add(
        AuditEvent(
            id=uuid4(),
            submission_id=submission.id,
            sequence=submission.version,
            source_inbox_event_id=event_id,
            occurred_at=submission.updated_at,
            from_status=previous,
            to_status=submission.status,
            reason_code=reason,
            actor="user",
            actor_id=user_id,
        )
    )


def process_message(session: Session, event: InboxEvent, campaign_id: UUID) -> str:
    """No network requests or commits: inbox, business state and reply stay atomic."""
    if event.sender_id is None:
        raise ValueError("Для приёма заявки нужен подтверждённый отправитель события")
    campaign = session.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update(read=True)
    ).scalar_one()
    rules = session.get(CampaignRules, (campaign.id, campaign.rules_version))
    if rules is None:
        raise ValueError("У кампании отсутствует текущая версия правил")
    now = datetime.now(UTC)
    user_id = uuid4()
    session.execute(
        insert(User)
        .values(
            id=user_id,
            source_id=event.source_id,
            bot_id=event.bot_id,
            max_user_id=event.sender_id,
            author_ref="usr_" + uuid4().hex,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_max_user")
    )
    user = session.execute(
        select(User)
        .where(
            User.source_id == event.source_id,
            User.bot_id == event.bot_id,
            User.max_user_id == event.sender_id,
        )
        .with_for_update()
    ).scalar_one()
    consent = session.execute(
        select(Consent).where(
            Consent.user_id == user.id,
            Consent.campaign_id == campaign.id,
            Consent.rules_version == rules.version,
        )
    ).scalar_one_or_none()

    def prompt() -> str:
        session.add(
            ConsentPrompt(
                source_inbox_event_id=event.id,
                user_id=user.id,
                campaign_id=campaign.id,
                rules_version=rules.version,
            )
        )
        return (
            f"Правила кампании {campaign.slug}, версия {rules.version}:\n{rules.text}\n\n"
            f"Если согласны, отправьте /agree {rules.version}. "
            "Скачивание и публикация фото в этом этапе не выполняются."
        )

    if event.has_photo:
        submission = Submission(
            id=uuid4(),
            user_id=user.id,
            campaign_id=campaign.id,
            consent_id=consent.id if consent else None,
            source_inbox_event_id=event.id,
            status="validating" if consent else "awaiting_consent",
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(submission)
        session.flush()
        record_audit(session, submission, event.id, None, user.id, "PHOTO_RECEIVED")
        if consent is None:
            return f"Заявка {submission.id} ожидает согласия.\n\n" + prompt()
        return f"Заявка {submission.id} принята: validating. Файл пока не скачивается."

    command = event.message_text.strip()
    if command == "/start":
        return "Привет! Отправьте фото для создания заявки.\n\n" + prompt()
    if command == "/help":
        return "/start — правила. /agree ВЕРСИЯ — согласие после чтения правил. Отправьте фото для заявки."
    parts = command.split()
    if parts and parts[0] == "/agree":
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != rules.version):
            return "Версия согласия не совпадает с текущими правилами.\n\n" + prompt()
        presented = session.execute(
            select(ConsentPrompt.source_inbox_event_id)
            .join(InboxEvent, InboxEvent.id == ConsentPrompt.source_inbox_event_id)
            .where(
                ConsentPrompt.user_id == user.id,
                ConsentPrompt.campaign_id == campaign.id,
                ConsentPrompt.rules_version == rules.version,
                InboxEvent.state == "sent",
            )
            .limit(1)
        ).scalar_one_or_none()
        if consent is None and presented is None:
            return (
                "Сначала прочитайте текущие правила, затем повторите согласие.\n\n"
                + prompt()
            )
        if consent is None:
            consent = Consent(
                id=uuid4(),
                user_id=user.id,
                campaign_id=campaign.id,
                rules_version=rules.version,
                accepted_at=now,
                source_inbox_event_id=event.id,
            )
            session.add(consent)
            session.flush()
        waiting = (
            session.execute(
                select(Submission)
                .where(
                    Submission.user_id == user.id,
                    Submission.campaign_id == campaign.id,
                    Submission.status == "awaiting_consent",
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for submission in waiting:
            submission.consent_id = consent.id
            submission.status = "validating"
            submission.version += 1
            submission.updated_at = now
            record_audit(
                session,
                submission,
                event.id,
                "awaiting_consent",
                user.id,
                "CONSENT_ACCEPTED",
            )
        return f"Согласие с {rules.version} записано. Заявок переведено в validating: {len(waiting)}. Можно отправлять фото."
    return "Начните с /start. Для помощи отправьте /help."
