import asyncio
import hashlib
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from max_emulator import LOCAL_TOKEN, create_app
from maxapi import Bot, Dispatcher
from maxapi.enums.update import UpdateType
from maxapi.methods.types.getted_updates import process_update_request
from maxapi.types import MessageCreated

HEADERS = {"Authorization": LOCAL_TOKEN}
FIXTURES = Path(__file__).parents[1] / "packages/contracts/max/fixtures"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(token=LOCAL_TOKEN)),
        base_url="http://emulator",
        headers=HEADERS,
    ) as client:
        yield client


async def test_marker_replay_latest_limit_and_filter(client):
    assert (await client.get("/updates?timeout=0")).json() == {
        "updates": [],
        "marker": 0,
    }
    first = (await client.post("/__test__/messages", json={"text": "/start"})).json()
    second = (await client.post("/__test__/messages", json={"photo": True})).json()
    assert (await client.get("/__test__/outgoing")).json() == {"messages": []}
    assert (await client.get("/updates?timeout=0")).json()["updates"] == [
        second["update"]
    ]
    page = (await client.get("/updates?marker=0&limit=1&timeout=0")).json()
    assert page == {"updates": [first["update"]], "marker": 1}
    assert (await client.get("/updates?marker=0&limit=1&timeout=0")).json() == page
    assert (await client.get("/updates?marker=1&timeout=0")).json() == {
        "updates": [second["update"]],
        "marker": 2,
    }
    assert (
        await client.get("/updates?marker=0&types=message_callback&timeout=0")
    ).json() == {
        "updates": [],
        "marker": 2,
    }
    assert (
        len(
            (
                await client.get(
                    "/updates?marker=0&types=message_created,message_callback&timeout=0"
                )
            ).json()["updates"]
        )
        == 2
    )


@pytest.mark.parametrize(
    "path",
    [
        "/me",
        "/subscriptions",
        "/updates?timeout=0",
        "/__test__/outgoing",
        "/__test__/messages",
        "/messages?chat_id=2001",
    ],
)
async def test_auth_required(client, path):
    method = "POST" if "messages" in path else "GET"
    response = await client.request(
        method,
        path,
        headers={"Authorization": "wrong"},
        **({"json": {"text": "test"}} if method == "POST" else {}),
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=1001", "timeout=-1", "timeout=91", "marker=-1", "marker=abc"],
)
async def test_invalid_poll_parameters(client, query):
    assert (await client.get(f"/updates?timeout=0&{query}")).status_code == 422


async def test_invalid_message_requests(client):
    assert (await client.post("/__test__/messages", json={})).status_code == 422
    (await client.post("/__test__/messages", json={"text": "test"}))
    assert (await client.get("/updates?marker=2&timeout=0")).status_code == 400
    assert (await client.post("/messages", json={"text": "reply"})).status_code == 400
    assert (
        await client.post("/messages?chat_id=2001&user_id=2001", json={"text": "reply"})
    ).status_code == 400
    assert (
        await client.post("/messages?user_id=9999", json={"text": "reply"})
    ).status_code == 404
    assert (
        await client.post("/messages?chat_id=2001", json={"text": ""})
    ).status_code == 422
    assert (
        await client.post(
            "/messages?chat_id=2001",
            json={"text": "reply", "attachments": [{"type": "image"}]},
        )
    ).status_code == 422


def test_long_poll_wakes_on_message_and_times_out():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(token=LOCAL_TOKEN)),
            base_url="http://emulator",
            headers=HEADERS,
        ) as client:
            waiting = asyncio.create_task(client.get("/updates?marker=0&timeout=2"))
            await asyncio.sleep(0.05)
            assert not waiting.done()
            await client.post("/__test__/messages", json={"text": "wake up"})
            response = await asyncio.wait_for(waiting, 1)
            assert response.json()["updates"][0]["message"]["body"]["text"] == "wake up"
            started = asyncio.get_running_loop().time()
            empty = await client.get("/updates?marker=1&timeout=1")
            assert asyncio.get_running_loop().time() - started >= 0.9
            assert empty.json() == {"updates": [], "marker": 1}

    asyncio.run(scenario())


def assert_shape(actual, recorded):
    """Check recorded required keys and scalar types, ignoring dynamic values."""
    assert type(actual) is type(recorded)
    if isinstance(recorded, dict):
        for key, value in recorded.items():
            assert key in actual
            assert_shape(actual[key], value)
    elif isinstance(recorded, list):
        assert len(actual) == len(recorded)
        for value, reference in zip(actual, recorded, strict=True):
            assert_shape(value, reference)


@pytest.mark.parametrize(
    "fixture,content",
    [
        ("updates-text.json", {"text": "/start"}),
        ("updates-image.json", {"photo": True}),
    ],
)
async def test_incoming_matches_recorded_core_shape(client, fixture, content):
    (await client.post("/__test__/messages", json=content))
    recorded = json.loads((FIXTURES / fixture).read_text())["body"]
    assert_shape((await client.get("/updates?marker=0&timeout=0")).json(), recorded)


@asynccontextmanager
async def live_emulator():
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


def test_maxapi_over_http_receives_text_photo_and_stores_reply():
    async def scenario():
        async with live_emulator() as url:
            bot = Bot(token=LOCAL_TOKEN, auto_requests=False)
            bot.set_api_url(url)
            dp = Dispatcher()
            received = []

            @dp.message_created()
            async def receive(event: MessageCreated):
                received.append(event)
                await event.message.answer("SDK test reply")

            try:
                # Exercise the same startup checks as start_polling.
                dp.polling = True
                await dp.startup(bot)
                assert bot.me is not None
                assert bot.me.user_id == 1001
                async with httpx.AsyncClient(base_url=url, headers=HEADERS) as client:
                    for content in ({"text": "/start"}, {"photo": True}):
                        await client.post("/__test__/messages", json=content)
                    updates = await bot.get_updates(
                        marker=0, timeout=0, types=[UpdateType.MESSAGE_CREATED]
                    )
                    for event in await process_update_request(updates, bot):
                        await dp.handle(event)
                    assert len(received) == 2
                    assert received[0].message.body.text == "/start"
                    assert received[1].message.body.attachments[0].type == "image"
                    photo = updates["updates"][1]["message"]["body"]["attachments"][0]
                    # Download the actual URL from polling without bot credentials.
                    async with httpx.AsyncClient() as downloader:
                        downloaded = await downloader.get(photo["payload"]["url"])
                    assert downloaded.status_code == 200
                    assert downloaded.headers["content-type"] == "image/png"
                    fixture = (
                        Path(__file__).parents[1]
                        / "apps/max-emulator/assets/seedling.png"
                    )
                    assert (
                        hashlib.sha256(downloaded.content).digest()
                        == hashlib.sha256(fixture.read_bytes()).digest()
                    )
                    assert downloaded.content.startswith(b"\x89PNG\r\n\x1a\n")
                    sent = await bot.send_message(user_id=2001, text="Direct SDK reply")
                    assert sent is not None
                    assert sent.message.body is not None
                    stored = (await client.get("/__test__/outgoing")).json()["messages"]
                    assert [m["body"]["text"] for m in stored] == [
                        "SDK test reply",
                        "SDK test reply",
                        "Direct SDK reply",
                    ]
                    assert stored[-1]["body"]["mid"] == sent.message.body.mid
                    reference = json.loads(
                        (FIXTURES / "post-message-text.json").read_text()
                    )["body"]
                    assert_shape({"message": stored[-1]}, reference)
                    assert len({m["body"]["mid"] for m in stored}) == 3
                    assert await bot.get_updates(marker=2, timeout=0) == {
                        "updates": [],
                        "marker": 2,
                    }
            finally:
                await dp.shutdown()
                await bot.close_session()

    asyncio.run(scenario())
