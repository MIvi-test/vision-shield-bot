"""Confirmed outbox publication and conservative MAX notification delivery."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

import pika
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import InboxEvent, ReplyOutbox, database_engine
from backend.inbox import InboxStore
from backend.polling import create_mock_bot


def queue_name(source: str, bot_id: int) -> str:
    suffix = hashlib.sha256(f"{source}:{bot_id}".encode()).hexdigest()[:24]
    return f"notification.tasks.{suffix}"


def connect(queue: str):
    # Pika may include credentials/parameters in its diagnostics. Only our
    # classified process errors are logged; never log AMQP frames or payloads.
    logging.getLogger("pika").setLevel(logging.CRITICAL)
    params = pika.URLParameters(os.environ["BACKEND_AMQP_URL"])
    params.heartbeat = 60
    params.blocked_connection_timeout = 10
    params.socket_timeout = 10
    params.stack_timeout = 15
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.confirm_delivery()
    return connection, channel


def dispatch_one(engine, channel, store: InboxStore) -> bool:
    """Hold one row lock through confirm; a crash can republish the same ID."""
    with Session(engine) as session, session.begin():
        row = session.execute(
            select(ReplyOutbox)
            .join(InboxEvent, InboxEvent.id == ReplyOutbox.event_id)
            .where(
                ReplyOutbox.published_at.is_(None),
                InboxEvent.source_id == store.source_id,
                InboxEvent.bot_id == store.bot_id,
            )
            .order_by(InboxEvent.received_at, InboxEvent.id)
            .with_for_update(of=ReplyOutbox, skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        channel.basic_publish(
            exchange="",
            routing_key=queue_name(store.source_id, store.bot_id),
            body=json.dumps({"event_id": str(row.event_id)}).encode(),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=str(row.event_id),
            ),
            mandatory=True,
        )
        row.published_at = datetime.now(UTC)
        return True


def parse_notification(body: bytes) -> UUID:
    if len(body) > 128:
        raise ValueError("Invalid notification")
    value = json.loads(body)
    if not isinstance(value, dict) or set(value) != {"event_id"}:
        raise ValueError("Invalid notification")
    return UUID(value["event_id"])


async def notify(bot, store: InboxStore, event_id: UUID) -> None:
    # pending -> sending commits BEFORE the external request. Redelivery of
    # sending/unknown/sent is acknowledged without repeating the request.
    reply = store.claim_reply(event_id)
    if reply is None:
        return
    try:
        result = await bot.send_message(chat_id=reply.chat_id, text=reply.text)
        if result is None:
            raise RuntimeError("SDK did not confirm delivery")
    except Exception as exc:
        store.finish_reply(event_id, "unknown")
        print(f"Notification uncertain: {type(exc).__name__}", flush=True)
        return
    store.finish_reply(event_id, "sent")


async def run(role: str) -> None:
    engine = database_engine()
    bot = create_mock_bot()
    connection = None
    # Pika connections/channels are confined to ONE thread. The asyncio loop
    # stays running so the MAX client's timers and sockets are serviced.
    with ThreadPoolExecutor(max_workers=1) as worker:

        async def call(fn, *args, **kwargs):
            return await asyncio.get_running_loop().run_in_executor(
                worker, partial(fn, *args, **kwargs)
            )

        try:
            identity = await bot.get_me()
            store = InboxStore(
                engine, os.getenv("MAX_SOURCE_ID", "mock-local"), identity.user_id
            )
            queue = queue_name(store.source_id, store.bot_id)
            connection, channel = await call(connect, queue)
            print(f"{role} ready", flush=True)
            while True:
                await call(connection.process_data_events)
                if role == "dispatcher":
                    if not await call(dispatch_one, engine, channel, store):
                        await asyncio.sleep(0.5)
                else:
                    method, _, body = await call(
                        channel.basic_get, queue, auto_ack=False
                    )
                    if method is None:
                        await asyncio.sleep(0.5)
                        continue
                    try:
                        event_id = parse_notification(body)
                    except ValueError, TypeError, KeyError:
                        await call(
                            channel.basic_reject, method.delivery_tag, requeue=False
                        )
                        print("Invalid notification rejected", flush=True)
                        continue
                    await notify(bot, store, event_id)
                    await call(channel.basic_ack, method.delivery_tag)
        finally:
            await bot.close_session()
            if connection is not None and connection.is_open:
                await call(connection.close)
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["dispatcher", "notifier"])
    args = parser.parse_args()
    try:
        asyncio.run(run(args.role))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Delivery stopped: {type(exc).__name__}", flush=True)
        raise SystemExit(1) from None
