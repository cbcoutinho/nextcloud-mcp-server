FROM python:3.14-slim-trixie@sha256:9813eecff3a08a6ac88aea5b43663c82a931fd9557f6aceaa847f0d8ce738978

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    git \
    sqlite3

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev --no-editable

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
