"""Backend application entry point."""

import os
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from backend.database import database_engine
from backend.panel import MESSAGES, router


class HealthResponse(BaseModel):
    status: str


def create_app(*, engine=None, allowed_origins=None, secure_cookie=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        if engine is None and app.state.engine is not None:
            app.state.engine.dispose()

    app = FastAPI(title="Vision Shield Backend", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    engine_lock = Lock()

    def session_dependency():
        with engine_lock:
            if app.state.engine is None:
                app.state.engine = database_engine()
        with Session(app.state.engine) as session:
            yield session

    def error(status, code, headers=None):
        return JSONResponse(
            {"code": code, "message": MESSAGES[code]},
            status_code=status,
            headers={"Cache-Control": "no-store"} | (headers or {}),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        code = (
            code
            if code in MESSAGES
            else {404: "not_found", 401: "unauthorized", 403: "forbidden"}.get(
                exc.status_code, "validation_error"
            )
        )
        return error(exc.status_code, code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(422, "validation_error")

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            return error(500, "internal_error")
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    allowed = (
        allowed_origins
        if allowed_origins is not None
        else os.getenv(
            "BACKEND_ALLOWED_ORIGINS",
            "http://127.0.0.1:8080,http://localhost:8080,http://127.0.0.1:8000",
        ).split(",")
    )
    secure = (
        secure_cookie
        if secure_cookie is not None
        else os.getenv("BACKEND_COOKIE_SECURE", "true") != "false"
    )
    app.include_router(router(session_dependency, allowed, secure))

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
