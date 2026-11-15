# syntax=docker/dockerfile:1
# ---- builder: resolve and install dependencies with uv (pinned version)
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# ---- runtime: slim image, non-root user, no build tools
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY migrations ./migrations
COPY config ./config
COPY alembic.ini start.sh ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD python -c \
  "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=2)"
# Migrations are NOT run here: run them once per release (compose 'migrate' service,
# or the platform's release/pre-deploy command). See docs/DEPLOYMENT.md.
CMD ["sh", "-c", "exec uvicorn toolgate.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --timeout-graceful-shutdown 20"]
