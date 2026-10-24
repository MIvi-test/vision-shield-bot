"""Local MAX HTTP emulator. Bot behavior belongs to Backend."""

import asyncio
import os
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

LOCAL_TOKEN = "local-emulator-token"
BOT_ID = 1001


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(default=2001, gt=BOT_ID)
    text: str = Field(default="", max_length=4000)
    photo: bool = False

    @model_validator(mode="after")
    def require_content(self):
        if not self.text and not self.photo:
            raise ValueError("Provide text or photo=true")
        return self


class OutgoingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=3999)
    attachments: list[Any] = Field(default_factory=list, max_length=0)
    notify: bool = True


def user(user_id: int, timestamp: int, *, is_bot: bool = False) -> dict:
    name = "Emulator bot" if is_bot else f"Test user {user_id}"
    result = {
        "user_id": user_id,
        "first_name": name,
        "name": name,
        "is_bot": is_bot,
        "last_activity_time": timestamp,
    }
    if is_bot:
        result["username"] = "emulator_bot"
    else:
        result["last_name"] = ""
    return result


class State:
    """One process, one ordered update log; reset on restart."""

    def __init__(self):
        self.updates: list[dict] = []
        self.outgoing: list[dict] = []
        self.users: set[int] = set()
        self.sequence = 0
        self.changed = asyncio.Condition()

    def message(self, user_id: int, text: str, *, incoming: bool) -> dict:
        self.sequence += 1
        timestamp = time.time_ns() // 1_000_000
        return {
            "recipient": {
                "chat_type": "dialog",
                "chat_id": user_id,
                "user_id": BOT_ID if incoming else user_id,
            },
            "timestamp": timestamp,
            "body": {
                "mid": f"mock-message-{self.sequence}",
                "seq": self.sequence,
                "text": text,
            },
            "sender": user(
                user_id if incoming else BOT_ID, timestamp, is_bot=not incoming
            ),
        }

    async def poll(
        self, marker: int | None, limit: int, timeout: int, types: set[str]
    ) -> dict:
        async with self.changed:
            # A cursor is the next log position, independent of message.seq.
            cursor = max(len(self.updates) - 1, 0) if marker is None else marker
            if cursor > len(self.updates):
                raise HTTPException(400, "Unknown marker")
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                updates = []
                while cursor < len(self.updates) and len(updates) < limit:
                    event = self.updates[cursor]
                    cursor += 1
                    if not types or event["update_type"] in types:
                        updates.append(event)
                remaining = deadline - asyncio.get_running_loop().time()
                if updates or remaining <= 0:
                    return {"updates": updates, "marker": cursor}
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except TimeoutError:
                    return {"updates": [], "marker": cursor}


def create_app(token: str | None = None) -> FastAPI:
    expected_token = token or os.environ.get("MAX_EMULATOR_TOKEN", LOCAL_TOKEN)
    state = State()

    async def authorize(authorization: Annotated[str | None, Header()] = None):
        if authorization != expected_token:
            raise HTTPException(401, "Invalid emulator token")

    app = FastAPI(
        title="MAX emulator v0",
        dependencies=[Depends(authorize)],
    )
    app.mount(
        "/media",
        StaticFiles(directory=Path(__file__).parent / "assets"),
        name="media",
    )

    @app.get("/me")
    async def get_me():
        return user(BOT_ID, time.time_ns() // 1_000_000, is_bot=True)

    @app.get("/subscriptions")
    async def get_subscriptions():
        # SDK startup checks subscriptions even when only polling is used.
        return {"subscriptions": []}

    @app.get("/updates")
    async def get_updates(
        marker: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        timeout: Annotated[int, Query(ge=0, le=90)] = 30,
        types: str | None = None,
    ):
        return await state.poll(
            marker, limit, timeout, set(types.split(",")) if types else set()
        )

    @app.post("/messages")
    async def send_message(
        body: OutgoingMessage,
        chat_id: Annotated[int | None, Query()] = None,
        user_id: Annotated[int | None, Query()] = None,
    ):
        if (chat_id is None) == (user_id is None):
            raise HTTPException(400, "Provide exactly one of chat_id or user_id")
        recipient = chat_id if chat_id is not None else user_id
        if recipient not in state.users:
            raise HTTPException(404, "Create a test user message first")
        message = state.message(recipient, body.text, incoming=False)
        state.outgoing.append(message)
        return {"message": message}

    @app.post("/__test__/messages")
    async def inject_message(body: IncomingMessage, request: Request):
        async with state.changed:
            state.users.add(body.user_id)
            message = state.message(body.user_id, body.text, incoming=True)
            if body.photo:
                seq = message["body"]["seq"]
                message["body"]["attachments"] = [
                    {
                        "type": "image",
                        "payload": {
                            "photo_id": seq,
                            "token": f"mock-photo-{seq}",
                            "url": str(request.url_for("media", path="seedling.png")),
                        },
                    }
                ]
            event = {
                "update_type": "message_created",
                "timestamp": message["timestamp"],
                "user_locale": "ru",
                "message": message,
            }
            state.updates.append(event)
            state.changed.notify_all()
            return {"update": event, "marker": len(state.updates)}

    @app.get("/__test__/outgoing")
    async def get_outgoing():
        return {"messages": state.outgoing}

    return app
