"""Durable local intake, separate from the stateless connectivity demo."""

import asyncio
import os
from uuid import UUID

from maxapi import Bot
from maxapi.enums import UpdateType
from sqlalchemy.orm import Session

from backend.database import Campaign, database_engine
from backend.inbox import InboxStore, normalize_message
from backend.polling import create_mock_bot


class DurablePollingBot:
    def __init__(self, bot: Bot, store: InboxStore):
        self.bot = bot
        self.store = store

    async def deliver_pending(self) -> None:
        while reply := await asyncio.to_thread(self.store.claim_reply):
            try:
                result = await self.bot.send_message(
                    chat_id=reply.chat_id, text=reply.text
                )
                if result is None:
                    raise RuntimeError("SDK не подтвердил отправку")
            except Exception:
                await asyncio.to_thread(
                    self.store.finish_reply, reply.event_id, "unknown"
                )
                raise
            await asyncio.to_thread(self.store.finish_reply, reply.event_id, "sent")

    async def poll_once(self, *, timeout: int = 20) -> int:
        # Recover committed but not yet attempted replies before receiving more.
        await self.deliver_pending()
        marker = await asyncio.to_thread(self.store.marker)
        batch = await self.bot.get_updates(
            marker=marker, timeout=timeout, types=[UpdateType.MESSAGE_CREATED]
        )
        messages = [normalize_message(raw) for raw in batch["updates"]]
        next_marker = batch["marker"]
        await asyncio.to_thread(self.store.save_batch, marker, next_marker, messages)
        await self.deliver_pending()
        return next_marker


async def run() -> None:
    engine = database_engine()
    bot = create_mock_bot()
    try:
        configured_campaign = os.getenv("BACKEND_CAMPAIGN_ID")
        campaign_id = UUID(configured_campaign) if configured_campaign else None
        if campaign_id is not None:

            def check_campaign():
                with Session(engine) as session:
                    if session.get(Campaign, campaign_id) is None:
                        raise ValueError("Кампания не найдена; выполните manage.py")

            await asyncio.to_thread(check_campaign)
        identity = await bot.get_me()
        store = InboxStore(
            engine,
            os.getenv("MAX_SOURCE_ID", "mock-local"),
            identity.user_id,
            campaign_id=campaign_id,
        )
        await asyncio.to_thread(store.marker)
        unresolved = await asyncio.to_thread(store.unresolved_count)
        print(
            f"Бот с PostgreSQL запущен. Неопределённых отправок: {unresolved}. "
            f"Приём заявок: {'включён' if campaign_id else 'выключен'}. "
            "Неопределённые отправки не повторяются автоматически.",
            flush=True,
        )
        adapter = DurablePollingBot(bot, store)
        while True:
            await adapter.poll_once()
    finally:
        await bot.close_session()
        engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except Exception as exc:
        print(
            f"Бот остановлен: {type(exc).__name__}. Проверьте PostgreSQL, миграции "
            "и настройки эмулятора. Неопределённые отправки не повторяются."
        )
        raise SystemExit(1) from None
