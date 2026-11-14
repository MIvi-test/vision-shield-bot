from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from backend.database import (
    AuditEvent,
    CampaignRules,
    Consent,
    ConsentPrompt,
    InboxEvent,
    Submission,
    User,
)
from backend.durable_polling import DurablePollingBot
from backend.inbox import InboxStore, ReceivedMessage
from backend.submissions import register_campaign
from max_emulator import LOCAL_TOKEN
from maxapi import Bot
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session
from test_backend_bot import emulator_url as emulator_url
from test_backend_inbox import db_engine as db_engine


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def campaign_id(db_engine):
    with Session(db_engine) as session, session.begin():
        return register_campaign(
            session, "test", "v1", "Синтетические тестовые правила."
        )


def store_for(engine, campaign_id):
    store = InboxStore(engine, "domain", 1001, campaign_id=campaign_id)
    store.marker()
    return store


def receive(store, text="", *, photo=False, user=2001, mid=None, delivered=True):
    marker = store.marker()
    message = ReceivedMessage(
        mid or uuid4().hex, user, text, photo, "placeholder", sender_id=user
    )
    store.save_batch(marker, marker + 1, [message])
    replies = []
    if delivered:
        while reply := store.claim_reply():
            replies.append(reply.text)
            store.finish_reply(reply.event_id, "sent")
    return replies


def count(engine, model):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_upgrade_preserves_existing_inbox(db_engine):
    config = Config(str(Path(__file__).parents[1] / "apps/backend/alembic.ini"))
    event_id = uuid4()
    with db_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_max_inbox")
        connection.execute(
            text(
                "INSERT INTO max_inbox_events "
                "(id, source_id, bot_id, update_type, message_id, chat_id, message_text, "
                "has_photo, reply_text, state, received_at) "
                "VALUES (:id, 'legacy', 1001, 'message_created', 'old', 2001, '/start', "
                "false, 'old reply', 'sent', :now)"
            ),
            {"id": event_id, "now": datetime.now(UTC)},
        )
        command.upgrade(config, "head")
    with Session(db_engine) as session:
        event = session.get(InboxEvent, event_id)
        assert event is not None
        assert event.state == "sent"
        assert event.reply_text == "old reply"
        assert event.sender_id is None


def test_photo_then_consent_replay_and_restart(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    reply = receive(store, photo=True, mid="photo-1")[0]
    with Session(db_engine) as session:
        submission = session.scalars(select(Submission)).one()
        submission_id = submission.id
        assert str(submission.id) in reply
        assert submission.status == "awaiting_consent"
        assert submission.version == 1
        assert submission.consent_id is None
    assert count(db_engine, Consent) == 0

    store = store_for(
        db_engine, campaign_id
    )  # Restart without losing consent evidence.
    receive(store, "/agree v1", mid="agree-1")
    receive(store, "/agree v1", mid="agree-1")  # Same event delivered again.
    receive(store, photo=True, mid="photo-1")
    receive(store, "/agree v1", mid="agree-2")  # New explicit repetition.
    with Session(db_engine) as session:
        submission = session.get(Submission, submission_id)
        assert submission is not None
        assert submission.status == "validating"
        assert submission.version == 2
        consent = session.get(Consent, submission.consent_id)
        assert consent is not None
        assert consent.rules_version == "v1"
        assert consent.accepted_at.tzinfo is not None
        history = session.scalars(
            select(AuditEvent).order_by(AuditEvent.sequence)
        ).all()
        assert [(h.from_status, h.to_status, h.sequence) for h in history] == [
            (None, "awaiting_consent", 1),
            ("awaiting_consent", "validating", 2),
        ]
        assert submission.created_at <= submission.updated_at
    assert count(db_engine, Submission) == 1
    assert count(db_engine, Consent) == 1
    assert count(db_engine, InboxEvent) == 3


def test_consent_before_photo_and_repeated_start(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    receive(store, "/start")
    receive(store, "/agree")
    receive(store, "/start")
    replies = receive(store, photo=True)
    with Session(db_engine) as session:
        submission = session.scalars(select(Submission)).one()
        assert submission.status == "validating"
        assert submission.version == 1
        assert submission.consent_id is not None
        assert str(submission.id) in replies[0]
    assert count(db_engine, Consent) == 1
    assert count(db_engine, AuditEvent) == 1


def test_agree_without_delivered_rules_cannot_accept(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    receive(store, "/agree v1", delivered=False)
    assert count(db_engine, Consent) == 0
    pending = store.claim_reply()
    assert pending is not None
    store.finish_reply(pending.event_id, "unknown")
    receive(store, "/agree v1")
    assert count(db_engine, Consent) == 0
    receive(store, "/agree v1")
    assert count(db_engine, Consent) == 1


def test_start_and_agree_in_same_unanswered_batch(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    store.save_batch(
        0,
        2,
        [
            ReceivedMessage("start", 2001, "/start", False, "placeholder", 2001),
            ReceivedMessage("agree", 2001, "/agree v1", False, "placeholder", 2001),
        ],
    )
    assert count(db_engine, Consent) == 0
    assert store.marker() == 2


def test_wrong_version_and_updated_rules(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    receive(store, "/start")
    receive(store, "/agree wrong")
    assert count(db_engine, Consent) == 0
    receive(store, "/agree v1")
    receive(store, photo=True)
    with Session(db_engine) as session, session.begin():
        register_campaign(session, "test", "v2", "Новая версия тестовых правил.")
    receive(store, photo=True)
    with Session(db_engine) as session:
        statuses = list(session.scalars(select(Submission.status)))
        assert sorted(statuses) == ["awaiting_consent", "validating"]
    receive(store, "/agree v1")
    assert count(db_engine, Consent) == 1
    receive(store, "/agree v2")
    with Session(db_engine) as session:
        assert set(session.scalars(select(Submission.status))) == {"validating"}
        assert set(session.scalars(select(Consent.rules_version))) == {"v1", "v2"}


def test_user_and_campaign_isolation(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    receive(store, photo=True, user=2001)
    receive(store, photo=True, user=2002)
    receive(store, "/agree v1", user=2001)
    with Session(db_engine) as session:
        rows = session.execute(
            select(User.max_user_id, Submission.status).join(
                Submission, Submission.user_id == User.id
            )
        ).all()
        assert {row[0]: row[1] for row in rows} == {
            2001: "validating",
            2002: "awaiting_consent",
        }
    with Session(db_engine) as session, session.begin():
        other = register_campaign(session, "other", "v1", "Другие тестовые правила.")
    receive(store_for(db_engine, other), photo=True, user=2001)
    with Session(db_engine) as session:
        other_submission = session.scalars(
            select(Submission).where(Submission.campaign_id == other)
        ).one()
        assert other_submission.status == "awaiting_consent"
        assert other_submission.consent_id is None


def test_domain_failure_rolls_back_inbox_user_submission_audit_marker(
    db_engine, campaign_id
):
    store = store_for(db_engine, campaign_id)
    with pytest.raises(DataError):
        store.save_batch(
            0,
            2,
            [
                ReceivedMessage("photo", 2001, "", True, "placeholder", 2001),
                ReceivedMessage("x" * 300, 2001, "", True, "placeholder", 2001),
            ],
        )
    for model in (InboxEvent, User, Submission, AuditEvent, ConsentPrompt):
        assert count(db_engine, model) == 0
    assert store.marker() == 0


def test_photo_caption_cannot_grant_consent(db_engine, campaign_id):
    store = store_for(db_engine, campaign_id)
    receive(store, "/start")
    receive(store, "/agree v1", photo=True)
    assert count(db_engine, Consent) == 0
    with Session(db_engine) as session:
        assert session.scalars(select(Submission.status)).one() == "awaiting_consent"


def test_rules_version_is_immutable(db_engine, campaign_id):
    with pytest.raises(ValueError, match="нельзя менять"):
        with Session(db_engine) as session, session.begin():
            register_campaign(session, "test", "v1", "Подменённый текст.")
    with Session(db_engine) as session:
        rules = session.get(CampaignRules, (campaign_id, "v1"))
        assert rules is not None
        assert rules.text == "Синтетические тестовые правила."


@pytest.mark.anyio
async def test_sdk_photo_agreement_flow_without_fetch(
    db_engine, campaign_id, emulator_url
):
    bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
    bot.set_api_url(emulator_url)
    adapter = DurablePollingBot(bot, store_for(db_engine, campaign_id))
    try:
        async with httpx.AsyncClient(
            base_url=emulator_url, headers={"Authorization": LOCAL_TOKEN}
        ) as client:
            response = await client.post(
                "/__test__/messages", json={"user_id": 2001, "photo": True}
            )
            response.raise_for_status()
            await adapter.poll_once(timeout=0)
            assert count(db_engine, Submission) == 1
            response = await client.post(
                "/__test__/messages", json={"user_id": 2001, "text": "/agree v1"}
            )
            response.raise_for_status()
            await adapter.poll_once(timeout=0)
            await adapter.poll_once(timeout=0)
            with Session(db_engine) as session:
                assert session.scalars(select(Submission.status)).one() == "validating"
                assert session.scalars(select(User.author_ref)).one().startswith("usr_")
            outgoing = (await client.get("/__test__/outgoing")).json()["messages"]
            assert len(outgoing) == 2
            assert "ожидает согласия" in outgoing[0]["body"]["text"]
            assert "переведено в validating: 1" in outgoing[1]["body"]["text"]
    finally:
        await bot.close_session()
