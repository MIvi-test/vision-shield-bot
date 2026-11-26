"""Panel API v0: fixed lifetime sessions and private submission projections."""

import base64
import binascii
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from backend.database import (
    AuditEvent,
    Consent,
    LoginThrottle,
    PanelAccount,
    PanelSession,
    Submission,
    User,
)
from backend.passwords import password_hash, token_hash, verify_password

Status = Literal[
    "awaiting_consent",
    "validating",
    "moderating",
    "review_required",
    "approved",
    "rejected",
]
MESSAGES = {
    "unauthorized": "Нужно войти в систему.",
    "forbidden": "Недостаточно прав.",
    "csrf_failed": "Не удалось подтвердить запрос. Обновите страницу.",
    "invalid_credentials": "Неверный логин или пароль.",
    "validation_error": "Неверные параметры запроса.",
    "rate_limited": "Слишком много попыток. Повторите позже.",
    "not_found": "Заявка не найдена.",
    "internal_error": "Что-то пошло не так. Повторите позже.",
}


def fail(status: int, code: str, headers=None):
    raise HTTPException(
        status, {"code": code, "message": MESSAGES[code]}, headers=headers
    )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256, repr=False)


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Invalid origin")
    # Access .port to reject malformed ports before comparing trusted origins.
    port = parsed.port
    host = parsed.hostname
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}:{port or default}"


def account_view(account: PanelAccount) -> dict:
    return {"id": str(account.id), "username": account.username, "role": account.role}


def submission_view(submission: Submission, author: str) -> dict:
    return {
        "id": str(submission.id),
        "status": submission.status,
        "created_at": submission.created_at,
        "updated_at": submission.updated_at,
        "author_ref": author,
        "campaign_id": str(submission.campaign_id),
        "version": submission.version,
    }


def encode_cursor(row: Submission, status: str | None) -> str:
    value = {"v": 1, "t": row.created_at.isoformat(), "i": str(row.id), "s": status}
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(cursor: str, status: str | None):
    try:
        if len(cursor) > 256:
            raise ValueError()
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(raw)
        if (
            set(value) != {"v", "t", "i", "s"}
            or value["v"] != 1
            or value["s"] != status
        ):
            raise ValueError()
        timestamp = datetime.fromisoformat(value["t"])
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError()
        return timestamp, UUID(value["i"])
    except ValueError, TypeError, KeyError, binascii.Error, OverflowError:
        fail(422, "validation_error")


def router(
    session_dependency, allowed_origins: list[str], secure_cookie: bool
) -> APIRouter:
    api = APIRouter(prefix="/api")
    trusted = {origin(value) for value in allowed_origins}
    dummy_hash = password_hash(secrets.token_urlsafe(32))

    def current(request: Request, session=Depends(session_dependency)):
        token = request.cookies.get("session", "")
        if not 16 <= len(token) <= 256:
            fail(401, "unauthorized")
        row = session.execute(
            select(PanelSession, PanelAccount)
            .join(PanelAccount, PanelAccount.id == PanelSession.account_id)
            .where(
                PanelSession.token_hash == token_hash(token),
                PanelSession.expires_at > datetime.now(UTC),
                PanelSession.revoked.is_(False),
                PanelAccount.active.is_(True),
            )
        ).one_or_none()
        if row is None:
            fail(401, "unauthorized")
        return row

    def moderator(identity=Depends(current)):
        if identity[1].role not in {"moderator", "admin"}:
            fail(403, "forbidden")
        return identity

    @api.post("/auth/login")
    def login(
        data: LoginRequest,
        request: Request,
        response: Response,
        session=Depends(session_dependency),
    ):
        # Fail closed, including requests missing both headers. CLI clients must
        # explicitly supply an allowed Origin. No CORS/forwarded-header trust.
        try:
            supplied = request.headers.get("origin")
            if supplied is not None:
                parsed = urlsplit(supplied)
                if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                    raise ValueError()
            else:
                supplied = request.headers.get("referer", "")
            valid = origin(supplied) in trusted
        except ValueError:
            valid = False
        if not valid:
            fail(403, "csrf_failed")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            fail(422, "validation_error")
        now = datetime.now(UTC)
        # Lock both IP and username buckets across processes. Only failed
        # attempts consume the budget; successful authentication resets neither
        # bucket, so one account cannot erase attacks against others.
        keys = [
            token_hash("ip:" + (request.client.host if request.client else "unknown")),
            token_hash("user:" + data.username),
        ]
        limited = False
        authenticated = False
        result = None
        with session.begin():
            buckets = []
            for key in keys:
                if limited:
                    break
                session.execute(
                    insert(LoginThrottle)
                    .values(key=key, failures=0, started_at=now)
                    .on_conflict_do_nothing()
                )
                bucket = session.execute(
                    select(LoginThrottle)
                    .where(LoginThrottle.key == key)
                    .with_for_update()
                ).scalar_one()
                if now - bucket.started_at >= timedelta(minutes=1):
                    bucket.started_at, bucket.failures = now, 0
                limited = limited or bucket.failures >= 5
                buckets.append(bucket)
            if not limited:
                account = session.execute(
                    select(PanelAccount).where(PanelAccount.username == data.username)
                ).scalar_one_or_none()
                encoded = account.password_hash if account else dummy_hash
                authenticated = (
                    verify_password(data.password, encoded)
                    and account is not None
                    and account.active
                )
                if authenticated and account is not None:
                    if not account.password_hash.startswith("scrypt16384p5$"):
                        account.password_hash = password_hash(data.password)
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    old = request.cookies.get("session", "")
                    previous = (
                        session.get(PanelSession, token_hash(old)) if old else None
                    )
                    if previous is not None:
                        previous.revoked = True
                    session.add(
                        PanelSession(
                            token_hash=token_hash(token),
                            account_id=account.id,
                            csrf_token=csrf,
                            created_at=now,
                            expires_at=now + timedelta(hours=8),
                            revoked=False,
                        )
                    )
                    result = {"user": account_view(account), "csrf_token": csrf}
                    response.set_cookie(
                        "session",
                        token,
                        max_age=8 * 3600,
                        httponly=True,
                        secure=secure_cookie,
                        samesite="strict",
                        path="/api",
                    )
                else:
                    for bucket in buckets:
                        bucket.failures += 1
        if limited:
            fail(429, "rate_limited", {"Retry-After": "60"})
        if not authenticated:
            fail(401, "invalid_credentials")
        return result

    @api.get("/auth/me")
    def me(identity=Depends(current)):
        return {"user": account_view(identity[1]), "csrf_token": identity[0].csrf_token}

    @api.post("/auth/logout", status_code=204)
    def logout(
        request: Request,
        response: Response,
        identity=Depends(current),
        session=Depends(session_dependency),
    ):
        csrf = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(csrf.encode(), identity[0].csrf_token.encode()):
            fail(403, "csrf_failed")
        identity[0].revoked = True
        session.commit()
        response.delete_cookie(
            "session",
            path="/api",
            secure=secure_cookie,
            httponly=True,
            samesite="strict",
        )

    @api.get("/submissions")
    def listing(
        status: Status | None = None,
        limit: int = 20,
        cursor: str | None = None,
        identity=Depends(moderator),
        session=Depends(session_dependency),
    ):
        if not 1 <= limit <= 100:
            fail(422, "validation_error")
        query = select(Submission, User.author_ref).join(
            User, User.id == Submission.user_id
        )
        if status is not None:
            query = query.where(Submission.status == status)
        if cursor is not None:
            timestamp, last_id = decode_cursor(cursor, status)
            query = query.where(
                tuple_(Submission.created_at, Submission.id)
                < tuple_(timestamp, last_id)
            )
        rows = session.execute(
            query.order_by(Submission.created_at.desc(), Submission.id.desc()).limit(
                limit + 1
            )
        ).all()
        more = len(rows) > limit
        page = rows[:limit]
        return {
            "items": [submission_view(row, author) for row, author in page],
            "next_cursor": encode_cursor(page[-1][0], status) if more else None,
        }

    @api.get("/submissions/{submission_id}")
    def detail(
        submission_id: UUID,
        identity=Depends(moderator),
        session=Depends(session_dependency),
    ):
        row = session.execute(
            select(Submission, User.author_ref)
            .join(User, User.id == Submission.user_id)
            .where(Submission.id == submission_id)
        ).one_or_none()
        if row is None:
            fail(404, "not_found")
        submission, author = row
        consent = (
            session.get(Consent, submission.consent_id)
            if submission.consent_id
            else None
        )
        history = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.submission_id == submission.id)
            .order_by(AuditEvent.sequence)
        ).all()
        return submission_view(submission, author) | {
            "consent": {
                "rules_version": consent.rules_version,
                "accepted_at": consent.accepted_at,
            }
            if consent
            else None,
            "history": [
                {
                    "id": h.sequence,
                    "occurred_at": h.occurred_at,
                    "from_status": h.from_status,
                    "to_status": h.to_status,
                    "reason_code": h.reason_code,
                    "actor": h.actor,
                }
                for h in history
            ],
        }

    return api
