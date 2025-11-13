FROM ghcr.io/astral-sh/uv:0.9.9-python3.11-alpine@sha256:0faa7934fac1db7f5056f159c1224d144bab864fd2677a4066d25a686ae32edd

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apk add --no-cache git sqlite

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev --no-editable

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
