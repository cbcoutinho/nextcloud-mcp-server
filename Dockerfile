FROM python:3.12-slim-trixie@sha256:d86b4c74b936c438cd4cc3a9f7256b9a7c27ad68c7caf8c205e18d9845af0164

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:f6e3549ed287fee0ddde2460a2a74a2d74366f84b04aaa34c1f19fec40da8652 /uv /uvx /bin/

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
