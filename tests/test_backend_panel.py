import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from backend import create_app
from backend.database import PanelAccount, PanelSession, Submission
from backend.inbox import InboxStore
from backend.passwords import password_hash, token_hash
from backend.submissions import register_campaign
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_backend_inbox import db_engine as db_engine
from test_backend_submissions import receive


@dataclass
class PanelHarness:
    client: TestClient
    password: str = field(repr=False)
    engine: object = field(repr=False)

    def __iter__(self):
        yield self.client
        yield self.password
        yield self.engine


@pytest.fixture
def panel(db_engine):
    password = secrets.token_urlsafe(24)
    with Session(db_engine) as session, session.begin():
        session.add(
            PanelAccount(
                id=uuid4(),
                username="mod",
                password_hash=password_hash(password),
                role="moderator",
                active=True,
            )
        )
    app = create_app(
        engine=db_engine, allowed_origins=["http://testserver"], secure_cookie=False
    )
    with TestClient(app) as client:
        yield PanelHarness(client, password, db_engine)


def login(panel, **headers):
    client, password, _ = panel
    return client.post(
        "/api/auth/login",
        json={"username": "mod", "password": password},
        headers={"Origin": "http://testserver"} | headers,
    )


def test_sessions_cookie_logout_and_no_sliding_expiry(panel):
    client, _, engine = panel
    assert client.get("/api/submissions").status_code == 401
    response = login(panel)
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert (
        "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api" in cookie
    )
    token = client.cookies["session"]
    with Session(engine) as session:
        saved = session.get(PanelSession, token_hash(token))
        assert saved is not None and saved.token_hash != token
        expiry = saved.expires_at
        assert saved.expires_at - saved.created_at == timedelta(hours=8)
    assert client.get("/api/auth/me").json() == response.json()
    assert client.get("/api/auth/me").headers["cache-control"] == "no-store"
    with Session(engine) as session:
        row = session.get(PanelSession, token_hash(token))
        assert row is not None and row.expires_at == expiry
    assert client.post("/api/auth/logout").status_code == 403
    assert (
        client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 204
    )
    client.cookies.set("session", token)
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize(
    "source",
    [
        None,
        "null",
        "https://evil.invalid",
        "http://testserver.evil.invalid",
        "http://testserver/path",
        "http://testserver:bad",
    ],
)
def test_origin_fail_closed(panel, source):
    client, password, _ = panel
    headers = {"Origin": source} if source else {}
    response = client.post(
        "/api/auth/login",
        json={"username": "mod", "password": password},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_referer_fallback_and_rotation(panel):
    client, password, _ = panel
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "mod", "password": password},
            headers={"Referer": "http://testserver/login"},
        ).status_code
        == 200
    )
    old = client.cookies["session"]
    assert login(panel).status_code == 200
    new = client.cookies["session"]
    assert new != old
    client.cookies.set("session", old)
    assert client.get("/api/auth/me").status_code == 401


def test_expired_disabled_and_role_checks(panel):
    client, _, engine = panel
    login(panel)
    with Session(engine) as session, session.begin():
        session.execute(update(PanelAccount).values(role="organizer"))
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/submissions").status_code == 403
    with Session(engine) as session, session.begin():
        session.execute(update(PanelAccount).values(role="admin"))
    assert client.get("/api/submissions").status_code == 200
    with Session(engine) as session, session.begin():
        session.execute(
            update(PanelSession).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert client.get("/api/auth/me").status_code == 401
    login(panel)
    with Session(engine) as session, session.begin():
        session.execute(update(PanelAccount).values(active=False))
    assert client.get("/api/auth/me").status_code == 401


def test_login_failures_share_throttle_across_instances(panel):
    client, _, engine = panel
    body = {"username": "mod", "password": "wrong"}
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json=body, headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"
    other_app = create_app(
        engine=engine, allowed_origins=["http://testserver"], secure_cookie=False
    )
    with TestClient(other_app) as other:
        response = other.post(
            "/api/auth/login", json=body, headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0


def seed(engine):
    with Session(engine) as session, session.begin():
        campaign = register_campaign(session, "panel", "v1", "Тестовые правила")
    store = InboxStore(engine, "panel", 1001, campaign_id=campaign)
    store.marker()
    for _ in range(3):
        receive(store, photo=True)
    # An identical timestamp must still yield a stable, complete traversal.
    with Session(engine) as session, session.begin():
        session.execute(
            update(Submission).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    return store


def test_cursor_history_consent_and_private_projection(panel):
    client, _, engine = panel
    store = seed(engine)
    login(panel)
    first = client.get("/api/submissions?limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(
        "/api/submissions", params={"cursor": first["next_cursor"], "limit": 2}
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    ids = [row["id"] for row in first["items"] + second["items"]]
    assert len(set(ids)) == 3 and ids == sorted(ids, reverse=True)
    assert (
        client.get(
            "/api/submissions",
            params={"cursor": first["next_cursor"], "status": "validating"},
        ).status_code
        == 422
    )
    detail = client.get(f"/api/submissions/{ids[0]}").json()
    assert detail["consent"] is None and detail["history"][0]["from_status"] is None
    assert detail["history"][0]["actor"] == "user"
    receive(store, "/agree v1")
    detail = client.get(f"/api/submissions/{ids[0]}").json()
    assert detail["status"] == "validating" and detail["version"] == 2
    assert detail["consent"]["rules_version"] == "v1"
    assert [h["id"] for h in detail["history"]] == [1, 2]
    forbidden = {
        "user_id",
        "max_user_id",
        "sender_id",
        "message_text",
        "token",
        "url",
        "password_hash",
        "actor_id",
    }
    assert not forbidden & set(detail)
    assert set(detail["history"][0]) == {
        "id",
        "occurred_at",
        "from_status",
        "to_status",
        "reason_code",
        "actor",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/submissions?limit=0",
        "/api/submissions?limit=101",
        "/api/submissions?status=unknown",
        "/api/submissions?cursor=bad",
        "/api/submissions/not-a-uuid",
    ],
)
def test_validation_uses_contract_error(panel, path):
    client, _, _ = panel
    login(panel)
    response = client.get(path)
    assert response.status_code == 422
    assert set(response.json()) == {"code", "message"}
    assert response.json()["code"] == "validation_error"


def test_not_found_and_generic_server_error(panel, monkeypatch):
    client, _, _ = panel
    login(panel)
    assert client.get(f"/api/submissions/{uuid4()}").status_code == 404

    def fail(*args, **kwargs):
        raise RuntimeError("sensitive database URL")

    monkeypatch.setattr(Session, "execute", fail)
    response = client.get("/api/submissions")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "sensitive" not in response.text


def test_legacy_development_hash_is_upgraded_on_successful_login(panel):
    _, password, engine = panel
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    with Session(engine) as session, session.begin():
        session.execute(
            update(PanelAccount).values(password_hash=f"scrypt16384${salt}${digest}")
        )
    assert login(panel).status_code == 200
    with Session(engine) as session:
        saved = session.scalars(select(PanelAccount)).one()
        assert saved.password_hash.startswith("scrypt16384p5$")
