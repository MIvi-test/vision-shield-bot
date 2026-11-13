import asyncio
import socket

import httpx
import pytest
import uvicorn
from backend.polling import MockPollingBot, create_mock_bot
from max_emulator import LOCAL_TOKEN, create_app
from maxapi import Bot


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def emulator_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(token=LOCAL_TOKEN), log_level="error", lifespan="off"
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.mark.anyio
async def test_bot_answers_commands_photo_and_repeated_batch(emulator_url):
    bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
    bot.set_api_url(emulator_url)
    adapter = MockPollingBot(bot)
    try:
        async with httpx.AsyncClient(
            base_url=emulator_url, headers={"Authorization": LOCAL_TOKEN}
        ) as client:
            inputs = [
                {"user_id": 2001, "text": "/start"},
                {"user_id": 2002, "text": "/help"},
                {"user_id": 2001, "photo": True},
                {"user_id": 2002, "text": "hello"},
                {"user_id": 2001, "text": "/start"},
            ]
            for content in inputs:
                (
                    await client.post("/__test__/messages", json=content)
                ).raise_for_status()
            marker = await adapter.poll_once(0, timeout=0)
            assert marker == 5
            # Replay the batch: no second reply for the same message IDs.
            assert await adapter.poll_once(0, timeout=0) == marker
            assert await adapter.poll_once(marker, timeout=0) == marker
            outgoing = (await client.get("/__test__/outgoing")).json()["messages"]
            assert len(outgoing) == 5
            texts = [message["body"]["text"] for message in outgoing]
            assert texts[0].startswith("Привет!")
            assert "/start — приветствие" in texts[1]
            assert "файл не скачан" in texts[2]
            assert texts[3].startswith("Я понимаю /start и /help")
            assert texts[4] == texts[0]
            assert [message["recipient"]["chat_id"] for message in outgoing] == [
                item["user_id"] for item in inputs
            ]
    finally:
        await bot.close_session()


@pytest.mark.parametrize("mode", ["polling", "webhook"])
def test_rejects_unimplemented_modes(monkeypatch, mode):
    monkeypatch.setenv("MAX_MODE", mode)
    with pytest.raises(ValueError, match="MAX_MODE=mock"):
        create_mock_bot()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8081/updates",
        "http://user:pass@localhost",
    ],
)
def test_rejects_non_emulator_addresses(monkeypatch, url):
    monkeypatch.setenv("MAX_MODE", "mock")
    monkeypatch.setenv("MAX_API_BASE_URL", url)
    with pytest.raises(ValueError):
        create_mock_bot()
