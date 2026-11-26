import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from backend.database import InboxEvent, ReplyOutbox
from backend.delivery import dispatch_one, notify, parse_notification, queue_name
from backend.inbox import InboxStore
from max_emulator import LOCAL_TOKEN
from maxapi import Bot
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_backend_bot import emulator_url as emulator_url
from test_backend_inbox import db_engine as db_engine
from test_backend_inbox import message


@pytest.fixture
def anyio_backend():
    return "asyncio"


def intake(engine):
    store = InboxStore(engine, "delivery-test", 1001)
    store.marker()
    store.save_batch(0, 1, [message()])
    with Session(engine) as session:
        return store, session.scalars(select(ReplyOutbox.event_id)).one()


def test_outbox_dedup_and_confirm_rollback(db_engine):
    store, event_id = intake(db_engine)
    store.save_batch(1, 2, [message()])

    class Publisher:
        sent = []
        fail = True

        def basic_publish(self, **kwargs):
            self.sent.append(kwargs)
            if self.fail:
                raise RuntimeError("unconfirmed")

    publisher = Publisher()
    with pytest.raises(RuntimeError):
        dispatch_one(db_engine, publisher, store)
    with Session(db_engine) as session:
        row = session.get(ReplyOutbox, event_id)
        assert row is not None and row.published_at is None
    publisher.fail = False
    assert dispatch_one(db_engine, publisher, store)
    assert not dispatch_one(db_engine, publisher, store)
    assert [json.loads(x["body"])["event_id"] for x in publisher.sent] == [
        str(event_id)
    ] * 2
    assert all(
        x["mandatory"] and x["properties"].delivery_mode == 2 for x in publisher.sent
    )


@pytest.mark.anyio
async def test_notifier_redelivery_and_source_isolation(db_engine):
    store, event_id = intake(db_engine)
    bot = AsyncMock()
    await notify(bot, InboxStore(db_engine, "another", 1001), event_id)
    bot.send_message.assert_not_called()
    await notify(bot, store, event_id)
    await notify(bot, store, event_id)
    bot.send_message.assert_awaited_once()
    with Session(db_engine) as session:
        row = session.get(InboxEvent, event_id)
        assert row is not None and row.state == "sent"


@pytest.mark.anyio
async def test_uncertain_or_interrupted_send_is_never_repeated(db_engine):
    store, event_id = intake(db_engine)
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    await notify(bot, store, event_id)
    await notify(bot, store, event_id)
    assert bot.send_message.await_count == 1
    with Session(db_engine) as session:
        row = session.get(InboxEvent, event_id)
        assert row is not None and row.state == "unknown"
    store.save_batch(1, 2, [message("two")])
    pending = store.claim_reply()
    assert pending is not None
    await notify(bot, store, pending.event_id)
    assert bot.send_message.await_count == 1


@pytest.mark.parametrize(
    "body", [b"[]", b"{}", b'{"event_id":"bad"}', b"x" * 129, b"null"]
)
def test_rejects_invalid_notification(body):
    with pytest.raises((ValueError, TypeError)):
        parse_notification(body)


def test_queue_is_stable_and_scoped():
    assert queue_name("a", 1) == queue_name("a", 1)
    assert queue_name("a", 1) != queue_name("a", 2)
    assert (
        parse_notification(json.dumps({"event_id": str(value := uuid4())}).encode())
        == value
    )


@pytest.mark.anyio
async def test_notifier_with_real_sdk_and_emulator(db_engine, emulator_url):
    store, event_id = intake(db_engine)
    bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
    bot.set_api_url(emulator_url)
    try:
        async with httpx.AsyncClient(
            base_url=emulator_url, headers={"Authorization": LOCAL_TOKEN}
        ) as client:
            (
                await client.post(
                    "/__test__/messages", json={"user_id": 2001, "text": "/start"}
                )
            ).raise_for_status()
            await bot.get_me()
            await notify(bot, store, event_id)
            await notify(bot, store, event_id)
            assert len((await client.get("/__test__/outgoing")).json()["messages"]) == 1
    finally:
        await bot.close_session()
