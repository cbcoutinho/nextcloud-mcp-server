FROM ghcr.io/astral-sh/uv:0.9.8-python3.11-alpine@sha256:6c842c49ad032f46b62f32a7e7779f45f12671a8e0d82ea24c766ab62d58b396

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apk add --no-cache git sqlite

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
