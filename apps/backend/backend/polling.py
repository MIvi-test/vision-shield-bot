"""Local emulator adapter; no persistent state or production delivery yet."""

import asyncio
import os
from collections import OrderedDict
from urllib.parse import urlsplit

from maxapi import Bot
from maxapi.client.default import DefaultConnectionProperties
from maxapi.enums import ChatType, UpdateType
from maxapi.types import MessageCreated

from backend.replies import choose_reply


class MockPollingBot:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.replied: OrderedDict[tuple[int, str], None] = OrderedDict()

    async def poll_once(self, marker: int, *, timeout: int = 20) -> int:
        batch = await self.bot.get_updates(
            marker=marker,
            timeout=timeout,
            types=[UpdateType.MESSAGE_CREATED],
        )
        for raw_event in batch["updates"]:
            event = MessageCreated.model_validate(raw_event)
            message = event.message
            if (
                message.recipient.chat_type != ChatType.DIALOG
                or message.recipient.chat_id is None
                or message.sender is None
                or message.sender.is_bot
                or message.body is None
            ):
                continue

            key = (message.recipient.chat_id, message.body.mid)
            if key in self.replied:
                continue

            has_photo = any(
                attachment.type == "image"
                for attachment in message.body.attachments or []
            )
            text = choose_reply(message.body.text or "", has_photo=has_photo)
            result = await self.bot.send_message(
                chat_id=message.recipient.chat_id, text=text
            )
            if result is None:
                raise RuntimeError("SDK не подтвердил отправку ответа")
            self.replied[key] = None
            if len(self.replied) > 1000:
                self.replied.popitem(last=False)

        return batch["marker"]


def create_mock_bot() -> Bot:
    if os.getenv("MAX_MODE", "mock") != "mock":
        raise ValueError("Этот процесс поддерживает только MAX_MODE=mock")
    url = os.getenv("MAX_API_BASE_URL", "http://127.0.0.1:8081")
    address = urlsplit(url)
    if (
        address.scheme != "http"
        or address.hostname not in {"localhost", "127.0.0.1", "::1", "max-emulator"}
        or address.username is not None
        or address.password is not None
        or address.path not in {"", "/"}
        or address.query
        or address.fragment
    ):
        raise ValueError("Укажите локальный HTTP-адрес эмулятора без пути")
    bot = Bot(
        token=os.getenv("MAX_EMULATOR_TOKEN", "local-emulator-token"),
        auto_requests=False,
        default_connection=DefaultConnectionProperties(timeout=30, max_retries=0),
    )
    bot.set_api_url(url.rstrip("/"))
    return bot


async def run() -> None:
    bot = create_mock_bot()
    try:
        await bot.get_me()
        adapter = MockPollingBot(bot)
        marker = 0
        print("Бот подключён к эмулятору. Остановка: Ctrl+C.", flush=True)
        while True:
            marker = await adapter.poll_once(marker)
    finally:
        await bot.close_session()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except Exception as exc:
        # SDK exceptions can contain response bodies; do not print them.
        print(
            f"Бот остановлен: {type(exc).__name__}. "
            "Проверьте эмулятор, MAX_MODE, адрес и синтетический токен. "
            "Перед повторным запуском учтите повторное чтение истории."
        )
        raise SystemExit(1) from None
