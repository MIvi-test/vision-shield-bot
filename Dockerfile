FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b AS runtime
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
USER 10001:10001

FROM runtime AS backend
COPY apps/backend/ apps/backend/
CMD ["uvicorn", "backend:create_app", "--factory", "--app-dir", "apps/backend", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS max-emulator
COPY apps/max-emulator/ apps/max-emulator/
CMD ["uvicorn", "max_emulator:create_app", "--factory", "--app-dir", "apps/max-emulator", "--host", "0.0.0.0", "--port", "8080"]
