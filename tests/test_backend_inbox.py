import asyncio
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from backend.database import Base, InboxEvent
from backend.durable_polling import DurablePollingBot
from backend.inbox import InboxStore, ReceivedMessage, normalize_message
from max_emulator import LOCAL_TOKEN
from maxapi import Bot
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session
from test_backend_bot import emulator_url as emulator_url


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db_engine():
    url = os.getenv("BACKEND_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Нужен BACKEND_TEST_DATABASE_URL для PostgreSQL-интеграции")
    admin = create_engine(url, hide_parameters=True)
    schema = "test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
    )
    config = Config(str(Path(__file__).parents[1] / "apps/backend/alembic.ini"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def message(mid="one"):
    return ReceivedMessage(mid, 2001, "/start", False, "reply")


def event_count(engine):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(InboxEvent))


def test_migration_matches_models_and_downgrade(db_engine):
    config = Config(str(Path(__file__).parents[1] / "apps/backend/alembic.ini"))
    with db_engine.begin() as connection:
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_restart_replay_and_source_isolation(db_engine):
    store = InboxStore(db_engine, "first", 1001)
    assert store.marker() == 0
    store.save_batch(0, 1, [message()])
    restarted = InboxStore(db_engine, "first", 1001)
    assert restarted.marker() == 1
    restarted.save_batch(1, 2, [message()])
    assert event_count(db_engine) == 1
    other = InboxStore(db_engine, "second", 1001)
    assert other.marker() == 0
    other.save_batch(0, 1, [message()])
    assert event_count(db_engine) == 2


def test_failed_batch_rolls_back_events_and_marker(db_engine):
    store = InboxStore(db_engine, "rollback", 1001)
    assert store.marker() == 0
    with pytest.raises(DataError):
        store.save_batch(0, 2, [message(), message("x" * 300)])
    assert store.marker() == 0
    assert event_count(db_engine) == 0


def test_stale_poller_cannot_overwrite_marker(db_engine):
    store = InboxStore(db_engine, "stale", 1001)
    assert store.marker() == 0
    store.save_batch(0, 1, [message()])
    with pytest.raises(RuntimeError):
        store.save_batch(0, 2, [message("second")])
    assert store.marker() == 1
    assert event_count(db_engine) == 1


def test_crash_during_send_is_not_retried(db_engine):
    store = InboxStore(db_engine, "crash", 1001)
    store.marker()
    store.save_batch(0, 1, [message()])
    reply = store.claim_reply()
    assert reply is not None
    # Simulate termination before recording the outcome of an HTTP request.
    restarted = InboxStore(db_engine, "crash", 1001)
    assert restarted.unresolved_count() == 1
    assert restarted.claim_reply() is None


@pytest.mark.anyio
async def test_persistent_bot_recovers_pending_without_duplicate(
    db_engine, emulator_url
):
    bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
    bot.set_api_url(emulator_url)
    store = InboxStore(db_engine, "e2e", 1001)
    adapter = DurablePollingBot(bot, store)
    try:
        async with httpx.AsyncClient(
            base_url=emulator_url, headers={"Authorization": LOCAL_TOKEN}
        ) as client:
            await client.post("/__test__/messages", json={"text": "/start"})
            batch = (await client.get("/updates?marker=0&timeout=0")).json()
            await asyncio.to_thread(store.marker)
            await asyncio.to_thread(
                store.save_batch,
                0,
                batch["marker"],
                [normalize_message(raw) for raw in batch["updates"]],
            )
            # Events committed, process stopped before sending. New process resumes.
            adapter = DurablePollingBot(bot, InboxStore(db_engine, "e2e", 1001))
            assert await adapter.poll_once(timeout=0) == 1
            assert await adapter.poll_once(timeout=0) == 1
            outgoing = (await client.get("/__test__/outgoing")).json()["messages"]
            assert len(outgoing) == 1
            assert outgoing[0]["body"]["text"].startswith("Привет!")
    finally:
        await bot.close_session()


@pytest.mark.anyio
async def test_send_failure_marks_unknown(db_engine, monkeypatch):
    bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
    store = InboxStore(db_engine, "unknown", 1001)
    store.marker()
    store.save_batch(0, 1, [message()])

    async def fail(**kwargs):
        raise TimeoutError("simulated")

    monkeypatch.setattr(bot, "send_message", fail)
    try:
        with pytest.raises(TimeoutError):
            await DurablePollingBot(bot, store).deliver_pending()
        assert store.unresolved_count() == 1
        assert store.claim_reply() is None
    finally:
        await bot.close_session()
